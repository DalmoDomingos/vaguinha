"""
Camada de persistência (Repository pattern).

Modelo:
    evento ─< local (área) ─< movimentacao

- `Repository`        : regras e SQL comuns a qualquer banco.
- `SQLiteRepository`  : protótipo (SQLite em memória ou arquivo).
- `PostgresRepository`: produção (Supabase / PostgreSQL, via psycopg2).

Toda a regra de ocupação é derivada do saldo:
    ocupados = SUM(entrada) - SUM(saida)   por (local_id, tipo_veiculo_id)
"""

from __future__ import annotations

import math
import queue
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import AREAS_EVENTO_INICIAL, COR_PADRAO, EVENTO_INICIAL, TIPO_CARRO, TIPO_MOTO

# Horários são gravados e exibidos no fuso de Brasília, qualquer que seja o fuso
# do servidor (o Supabase e o Streamlit Cloud rodam em UTC).
FUSO = "America/Sao_Paulo"
SCHEMA_SQL = Path(__file__).with_name("schema.sql")


# ------------------------------------------------------------------
# Regras de negócio
# ------------------------------------------------------------------
class OperacaoInvalida(ValueError):
    """Operação recusada por regra de negócio (mensagem pronta para o usuário)."""


class MovimentoInvalido(OperacaoInvalida):
    """Movimento recusado pela regra de saldo (área lotada / nada para dar saída)."""


def validar_movimento(movimentacao: str, ocupados: int, capacidade: Optional[int]) -> None:
    """Regra de saldo, avaliada com a ocupação ATUAL lida do banco."""
    if movimentacao not in ("entrada", "saida"):
        raise ValueError("movimentacao deve ser 'entrada' ou 'saida'")
    if movimentacao == "entrada" and capacidade is not None and ocupados >= capacidade:
        raise MovimentoInvalido("Área lotada para este tipo de veículo.")
    if movimentacao == "saida" and ocupados <= 0:
        raise MovimentoInvalido("Não há veículos deste tipo para dar saída.")


def _validar_nome_evento(nome: str) -> str:
    nome = str(nome or "").strip()
    if not nome:
        raise OperacaoInvalida("Dê um nome ao evento.")
    return nome


def _inteiro(valor) -> int:
    """Converte valores vindos da tela (None, NaN, float) para int."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return 0
    return int(valor)


def _normalizar_areas(areas: List[dict]) -> List[dict]:
    """Valida e limpa a lista de áreas vinda da tela (mantém a ordem)."""
    limpas, nomes = [], set()
    for a in areas:
        nome = str(a.get("nome") or "").strip()
        if not nome:
            raise OperacaoInvalida("Toda área precisa de um nome.")
        if nome.lower() in nomes:
            raise OperacaoInvalida(f"Há mais de uma área chamada “{nome}”.")
        nomes.add(nome.lower())

        cap_carro, cap_moto = _inteiro(a.get("cap_carro")), _inteiro(a.get("cap_moto"))
        if cap_carro < 0 or cap_moto < 0:
            raise OperacaoInvalida(f"“{nome}”: a quantidade de vagas não pode ser negativa.")

        area_id = a.get("id")
        if area_id is not None and isinstance(area_id, float) and math.isnan(area_id):
            area_id = None
        limpas.append({
            "id": None if area_id is None else int(area_id),
            "nome": nome,
            "cor": a.get("cor") or COR_PADRAO,
            "cap_carro": cap_carro,
            "cap_moto": cap_moto,
        })
    if not limpas:
        raise OperacaoInvalida("O evento precisa de pelo menos uma área.")
    return limpas


# ------------------------------------------------------------------
# SQL comum (escrito com "?"; cada banco troca pelo seu placeholder)
# ------------------------------------------------------------------
SALDO_EXPR = "SUM(CASE WHEN movimentacao = 'entrada' THEN 1 ELSE -1 END)"

SQL_EVENTOS = f"""
    SELECT e.id, e.nome, e.criado_em,
           COUNT(l.id)                               AS n_areas,
           COALESCE(SUM(l.cap_carro + l.cap_moto), 0) AS capacidade,
           COALESCE(SUM(s.ocupados), 0)               AS ocupados
    FROM evento e
    LEFT JOIN local l ON l.evento_id = e.id
    LEFT JOIN (SELECT local_id, {SALDO_EXPR} AS ocupados
               FROM movimentacao GROUP BY local_id) s ON s.local_id = l.id
    GROUP BY e.id, e.nome, e.criado_em
    ORDER BY e.criado_em DESC, e.id DESC
"""
SQL_SALDO_AREA = (
    f"SELECT COALESCE({SALDO_EXPR}, 0) FROM movimentacao WHERE local_id = ?"
)
SQL_SALDO_UM = (
    f"SELECT COALESCE({SALDO_EXPR}, 0) FROM movimentacao "
    "WHERE local_id = ? AND tipo_veiculo_id = ?"
)
SINAL = "CASE WHEN m.movimentacao = 'entrada' THEN 1 ELSE -1 END"
SQL_PAINEL = f"""
    SELECT l.id, l.nome, l.cor, l.cap_carro, l.cap_moto,
           COALESCE(SUM(CASE WHEN m.tipo_veiculo_id = {TIPO_CARRO} THEN {SINAL} END), 0) AS ocup_carro,
           COALESCE(SUM(CASE WHEN m.tipo_veiculo_id = {TIPO_MOTO}  THEN {SINAL} END), 0) AS ocup_moto
    FROM local l
    LEFT JOIN movimentacao m ON m.local_id = l.id
    WHERE l.evento_id = ?
    GROUP BY l.id, l.nome, l.cor, l.cap_carro, l.cap_moto, l.ordem
    ORDER BY l.ordem, l.id
"""
SQL_SALDOS_EVENTO = f"""
    SELECT m.local_id, m.tipo_veiculo_id, {SALDO_EXPR} AS ocupados
    FROM movimentacao m JOIN local l ON l.id = m.local_id
    WHERE l.evento_id = ?
    GROUP BY m.local_id, m.tipo_veiculo_id
"""


# ------------------------------------------------------------------
# Contrato + implementação comum
# ------------------------------------------------------------------
class Repository(ABC):
    PH = "?"        # placeholder de parâmetro do driver
    AGORA = ""      # expressão SQL do horário atual (no fuso FUSO)

    # ---- infraestrutura (cada banco implementa) ----
    @abstractmethod
    def _transacao(self, escrita: bool = True):
        """
        Context manager que entrega um cursor. Escrita: transação (commit no
        sucesso, rollback no erro). Leitura: pode dispensar a transação.
        """

    def _sql_trava(self) -> str:
        """
        SQL (parâmetros: local_id, tipo) que serializa movimentos do mesmo
        (área, tipo), enviado junto com a leitura do saldo. Padrão: nenhum
        (o lock da transação basta).
        """
        return ""

    def _ler(self, consulta):
        with self._transacao(escrita=False) as cur:
            return consulta(cur)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", self.PH)

    def _exec(self, cur, sql: str, params: tuple = ()) -> None:
        cur.execute(self._sql(sql), params)

    def _todas(self, sql: str, params: tuple = ()) -> List[dict]:
        def consulta(cur):
            self._exec(cur, sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        return self._ler(consulta)

    def _semear_se_vazio(self) -> None:
        """Cria o evento inicial (Corrida da FAB) se ainda não houver nenhum."""
        if not self._todas("SELECT id FROM evento LIMIT 1"):
            self.criar_evento(EVENTO_INICIAL, [
                {"nome": n, "cap_carro": c, "cap_moto": m, "cor": cor}
                for n, c, m, cor in AREAS_EVENTO_INICIAL
            ])

    # ---- eventos ----
    def listar_eventos(self) -> List[dict]:
        """Eventos (mais recentes primeiro) com nº de áreas, capacidade e ocupação."""
        eventos = self._todas(SQL_EVENTOS)
        for e in eventos:
            for k in ("n_areas", "capacidade", "ocupados"):
                e[k] = int(e[k] or 0)
        return eventos

    def obter_evento(self, evento_id: int) -> Optional[dict]:
        rows = self._todas("SELECT id, nome, criado_em FROM evento WHERE id = ?", (evento_id,))
        return rows[0] if rows else None

    def criar_evento(self, nome: str, areas: List[dict]) -> int:
        nome, areas = _validar_nome_evento(nome), _normalizar_areas(areas)
        with self._transacao() as cur:
            self._exec(cur, f"INSERT INTO evento (nome, criado_em) VALUES (?, {self.AGORA}) "
                            "RETURNING id", (nome,))
            evento_id = int(cur.fetchone()[0])
            for ordem, a in enumerate(areas):
                self._inserir_area(cur, evento_id, a, ordem)
        return evento_id

    def renomear_evento(self, evento_id: int, nome: str) -> None:
        nome = _validar_nome_evento(nome)
        with self._transacao() as cur:
            self._exec(cur, "UPDATE evento SET nome = ? WHERE id = ?", (nome, evento_id))

    # ---- áreas ----
    def listar_areas(self, evento_id: int) -> List[dict]:
        return self._todas(
            "SELECT id, nome, cor, cap_carro, cap_moto FROM local "
            "WHERE evento_id = ? ORDER BY ordem, id",
            (evento_id,),
        )

    def painel_evento(self, evento_id: int) -> List[dict]:
        """Áreas do evento (na ordem) já com a ocupação: ocup_carro / ocup_moto."""
        areas = self._todas(SQL_PAINEL, (evento_id,))
        for a in areas:
            a["ocup_carro"], a["ocup_moto"] = int(a["ocup_carro"]), int(a["ocup_moto"])
        return areas

    def _inserir_area(self, cur, evento_id: int, a: dict, ordem: int) -> None:
        self._exec(
            cur,
            "INSERT INTO local (evento_id, nome, cor, cap_carro, cap_moto, ordem) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (evento_id, a["nome"], a["cor"], a["cap_carro"], a["cap_moto"], ordem),
        )

    def salvar_areas(self, evento_id: int, areas: List[dict]) -> None:
        """
        Sincroniza as áreas do evento com a lista recebida (na ordem dela):
        com id → atualiza; sem id → cria; ausentes → exclui (com o histórico).
        Não deixa excluir área que ainda tem veículos estacionados.
        """
        areas = _normalizar_areas(areas)
        with self._transacao() as cur:
            self._exec(cur, "SELECT id, nome FROM local WHERE evento_id = ?", (evento_id,))
            existentes = {int(r[0]): r[1] for r in cur.fetchall()}
            mantidas = {a["id"] for a in areas if a["id"] is not None}
            if mantidas - existentes.keys():
                raise OperacaoInvalida("Alguma área não pertence mais a este evento. "
                                       "Recarregue a página e tente de novo.")

            for local_id in existentes.keys() - mantidas:
                self._exec(cur, SQL_SALDO_AREA, (local_id,))
                ocupados = int(cur.fetchone()[0])
                if ocupados > 0:
                    raise OperacaoInvalida(
                        f"Não dá para excluir “{existentes[local_id]}”: ainda há "
                        f"{ocupados} veículo(s) nela. Dê saída antes de excluir."
                    )
                self._exec(cur, "DELETE FROM movimentacao WHERE local_id = ?", (local_id,))
                self._exec(cur, "DELETE FROM local WHERE id = ?", (local_id,))

            for ordem, a in enumerate(areas):
                if a["id"] is None:
                    self._inserir_area(cur, evento_id, a, ordem)
                else:
                    self._exec(
                        cur,
                        "UPDATE local SET nome = ?, cor = ?, cap_carro = ?, cap_moto = ?, "
                        "ordem = ? WHERE id = ? AND evento_id = ?",
                        (a["nome"], a["cor"], a["cap_carro"], a["cap_moto"], ordem,
                         a["id"], evento_id),
                    )

    # ---- movimentos ----
    def registrar(self, tipo_veiculo_id: int, local_id: int, movimentacao: str) -> int:
        """
        Insere um evento de 'entrada' ou 'saida' com horário = agora. Retorna o id.
        A capacidade e o saldo são lidos na mesma transação do insert; se o
        movimento for recusado, levanta MovimentoInvalido.
        """
        with self._transacao() as cur:
            # trava + capacidade + saldo numa única ida ao banco
            trava = self._sql_trava()
            self._exec(
                cur,
                trava + f"SELECT l.cap_carro, l.cap_moto, ({SQL_SALDO_UM}) FROM local l WHERE l.id = ?",
                ((local_id, tipo_veiculo_id) if trava else ())
                + (local_id, tipo_veiculo_id, local_id),
            )
            area = cur.fetchone()
            if area is None:
                raise OperacaoInvalida("Esta área não existe mais (pode ter sido excluída).")
            capacidade = area[0] if tipo_veiculo_id == TIPO_CARRO else area[1]
            validar_movimento(movimentacao, int(area[2]), int(capacidade))

            self._exec(
                cur,
                "INSERT INTO movimentacao (tipo_veiculo_id, local_id, movimentacao, horario) "
                f"VALUES (?, ?, ?, {self.AGORA}) RETURNING id",
                (tipo_veiculo_id, local_id, movimentacao),
            )
            return int(cur.fetchone()[0])

    def saldo(self, local_id: int, tipo_veiculo_id: int) -> int:
        """Ocupação atual (entradas - saídas) de um tipo em uma área."""
        rows = self._todas(SQL_SALDO_UM, (local_id, tipo_veiculo_id))
        return int(next(iter(rows[0].values())))

    def saldos(self, evento_id: int) -> Dict[Tuple[int, int], int]:
        """Saldos do evento: {(local_id, tipo_veiculo_id): ocupados}."""
        rows = self._todas(SQL_SALDOS_EVENTO, (evento_id,))
        return {(r["local_id"], r["tipo_veiculo_id"]): int(r["ocupados"]) for r in rows}

    def historico(self, local_id: int, limite: int = 50) -> List[dict]:
        """Últimos movimentos da área (ordem de registro; mais recentes primeiro)."""
        return self._todas(
            "SELECT id, tipo_veiculo_id, local_id, movimentacao, horario FROM movimentacao "
            "WHERE local_id = ? ORDER BY id DESC LIMIT ?",
            (local_id, limite),
        )


# ------------------------------------------------------------------
# SQLite (funcional, para prototipar)
# ------------------------------------------------------------------
class SQLiteRepository(Repository):
    PH = "?"
    # UTC-3 fixo: o Brasil não tem horário de verão desde 2019.
    AGORA = "datetime('now', '-3 hours')"

    def __init__(self, db_path: str = ":memory:"):
        # A conexão é compartilhada entre sessões/threads, então todo acesso passa
        # pelo lock (sqlite3 não suporta uso concorrente da mesma conexão).
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._criar_schema()
        self._semear_se_vazio()

    def _criar_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS evento (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                nome      TEXT NOT NULL,
                criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                evento_id INTEGER NOT NULL REFERENCES evento(id) ON DELETE CASCADE,
                nome      TEXT    NOT NULL,
                cor       TEXT    NOT NULL DEFAULT '#1f6f8b',
                cap_carro INTEGER NOT NULL DEFAULT 0 CHECK (cap_carro >= 0),
                cap_moto  INTEGER NOT NULL DEFAULT 0 CHECK (cap_moto >= 0),
                ordem     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS movimentacao (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo_veiculo_id INTEGER NOT NULL,
                local_id        INTEGER NOT NULL REFERENCES local(id) ON DELETE CASCADE,
                movimentacao    TEXT    NOT NULL CHECK (movimentacao IN ('entrada','saida')),
                horario         TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_local_evento ON local (evento_id);
            CREATE INDEX IF NOT EXISTS idx_mov_local_tipo
                ON movimentacao (local_id, tipo_veiculo_id);
            """
        )
        self.conn.commit()

    @contextmanager
    def _transacao(self, escrita: bool = True):
        with self._lock:
            cur = self.conn.cursor()
            try:
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()


# ------------------------------------------------------------------
# PostgreSQL / Supabase
# ------------------------------------------------------------------
class PostgresRepository(Repository):
    """
    Requer psycopg2 (`pip install psycopg2-binary`) e o schema.sql aplicado.
    Exemplo:
        repo = PostgresRepository("postgresql://user:senha@host:5432/vaguinha")
    """
    PH = "%s"
    # Nas escritas o fuso da transação é fixado em FUSO (ver _transacao), então
    # NOW() grava a hora de Brasília tanto em coluna TIMESTAMP quanto TIMESTAMPTZ,
    # independente do fuso configurado no Supabase.
    AGORA = "NOW()"

    def __init__(self, dsn: str, max_conexoes: int = 15):
        import psycopg2  # import tardio: só exige a lib se realmente usar Postgres
        self._pg = psycopg2
        self._dsn = dsn
        # Pool de conexões: vários operadores consultam/registram em paralelo
        # (até max_conexoes ao mesmo tempo; quem passar disso espera uma livre).
        # 15 cabe no pooler do Supabase gratuito (use a URL do Transaction pooler).
        # As conexões são reaproveitadas: abrir uma nova no Supabase custa um
        # handshake TLS (~100-300 ms). (O pool do psycopg2 fecha a conexão
        # devolvida quando há mais que `minconn` paradas, por isso não é usado.)
        self._paradas: "queue.LifoQueue" = queue.LifoQueue()
        self._livres = threading.BoundedSemaphore(max_conexoes)
        self._migrar_se_preciso()
        self._semear_se_vazio()

    def _migrar_se_preciso(self) -> None:
        """
        Aplica o schema.sql se o banco ainda não está no formato com eventos
        (banco vazio ou versão antiga). O script é idempotente e migra os dados.
        """
        def atualizado(cur):
            cur.execute(
                "SELECT to_regclass('evento') IS NOT NULL AND EXISTS ("
                "  SELECT 1 FROM information_schema.columns"
                "  WHERE table_schema = current_schema()"
                "    AND table_name = 'local' AND column_name = 'evento_id')"
            )
            return cur.fetchone()[0]

        if not self._ler(atualizado):
            with self._transacao() as cur:
                cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))

    def _nova_conexao(self):
        conn = self._pg.connect(self._dsn)
        conn.autocommit = True  # transações de escrita são abertas explicitamente
        return conn

    @contextmanager
    def _conexao(self):
        """Empresta uma conexão do pool e a devolve no final (descarta se caiu)."""
        with self._livres:
            try:
                conn = self._paradas.get_nowait()
            except queue.Empty:
                conn = self._nova_conexao()
            if conn.closed:  # caiu enquanto estava parada
                conn = self._nova_conexao()
            quebrada = False
            try:
                yield conn
            except (self._pg.OperationalError, self._pg.InterfaceError):
                quebrada = True  # timeout do servidor, restart...: abre outra depois
                raise
            finally:
                if quebrada or conn.closed:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._paradas.put(conn)

    @contextmanager
    def _transacao(self, escrita: bool = True):
        """
        Leitura: cada consulta roda sozinha (autocommit), 1 ida ao banco.
        Escrita: transação curta com o fuso de Brasília (SET LOCAL), commit no
        sucesso e rollback no erro — a conexão nunca fica "aborted".
        """
        with self._conexao() as conn, conn.cursor() as cur:
            if not escrita:
                yield cur
                return
            cur.execute(f"BEGIN; SET LOCAL TIME ZONE '{FUSO}'")
            try:
                yield cur
            except BaseException:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            cur.execute("COMMIT")

    def _ler(self, consulta):
        """Executa uma leitura; se a conexão tinha caído, tenta mais uma vez."""
        try:
            return super()._ler(consulta)
        except (self._pg.OperationalError, self._pg.InterfaceError):
            return super()._ler(consulta)

    def _sql_trava(self) -> str:
        # Serializa movimentos do mesmo (área, tipo) também entre processos,
        # para duas entradas simultâneas não passarem da capacidade. Vai no mesmo
        # envio da leitura do saldo; como são comandos separados, a leitura só
        # acontece depois de obter a trava e já enxerga o que os outros gravaram.
        return "SELECT pg_advisory_xact_lock(?, ?); "
