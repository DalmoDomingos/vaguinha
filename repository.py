"""
Camada de persistência (Repository pattern).

Modelo:
    evento ─< local (área) ─< movimentacao

- `Repository`        : regras e SQL comuns a qualquer banco.
- `SQLiteRepository`  : protótipo (SQLite em memória ou arquivo).
- `PostgresRepository`: produção (Supabase / PostgreSQL, via psycopg2).

Toda a regra de ocupação é derivada do saldo:
    ocupados = SUM(entrada) - SUM(saida)   por (local_id, tipo_veiculo_id)

"Zerar" um evento não apaga nada: marca as movimentações com `arquivado_em`,
e o saldo considera só as ativas (`arquivado_em IS NULL`).

Senha dos eventos: o banco guarda só o hash (scrypt com sal aleatório) em
`evento.senha_hash`; a senha em si não é gravada em lugar nenhum.

Acesso lembrado no aparelho: depois da senha certa, o aparelho recebe um código
aleatório (cookie); a tabela `acesso` guarda só o SHA-256 dele, o evento, a
marca da senha e a validade. Trocar a senha ou sair invalida o código.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import queue
import secrets
import sqlite3
import threading
import time
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


QUANTIDADE_MAX = 10  # veículos de uma vez num único toque


def validar_movimento(movimentacao: str, ocupados: int, capacidade: Optional[int],
                      quantidade: int = 1) -> None:
    """Regra de saldo, avaliada com a ocupação ATUAL lida do banco (tudo ou nada)."""
    if movimentacao not in ("entrada", "saida"):
        raise ValueError("movimentacao deve ser 'entrada' ou 'saida'")
    if isinstance(quantidade, bool) or not isinstance(quantidade, int) \
            or not 1 <= quantidade <= QUANTIDADE_MAX:
        raise OperacaoInvalida(f"A quantidade deve ser de 1 a {QUANTIDADE_MAX}.")
    if movimentacao == "entrada" and capacidade is not None and ocupados + quantidade > capacidade:
        if ocupados >= capacidade:
            raise MovimentoInvalido("Área lotada para este tipo de veículo.")
        raise MovimentoInvalido(
            f"Só cabem mais {capacidade - ocupados} deste tipo nesta área. Nada foi registrado.")
    if movimentacao == "saida" and ocupados < quantidade:
        if ocupados <= 0:
            raise MovimentoInvalido("Não há veículos deste tipo para dar saída.")
        raise MovimentoInvalido(
            f"Só há {ocupados} deste tipo nesta área para dar saída. Nada foi registrado.")


def _validar_nome_evento(nome: str) -> str:
    nome = str(nome or "").strip()
    if not nome:
        raise OperacaoInvalida("Dê um nome ao evento.")
    return nome


# ------------------------------------------------------------------
# Senhas dos eventos
# ------------------------------------------------------------------
SENHA_MIN, SENHA_MAX = 6, 128
# scrypt (16 MB de memória por cálculo): lento de propósito para quem tenta
# adivinhar a senha, ~50 ms para quem a digita.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def validar_senha_nova(senha: str) -> str:
    senha = str(senha or "")
    if len(senha) < SENHA_MIN:
        raise OperacaoInvalida(f"A senha precisa ter pelo menos {SENHA_MIN} caracteres.")
    if len(senha) > SENHA_MAX:
        raise OperacaoInvalida(f"A senha pode ter no máximo {SENHA_MAX} caracteres.")
    return senha


def gerar_hash_senha(senha: str) -> str:
    """Hash da senha para gravar no banco: scrypt$n$r$p$sal$hash (hex)."""
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode("utf-8"), salt=sal, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${sal.hex()}${h.hex()}"


def conferir_hash_senha(senha: str, guardado: Optional[str]) -> bool:
    """A senha digitada confere com o hash guardado? (comparação em tempo constante)"""
    try:
        nome, n, r, p, sal, h = (guardado or "").split("$")
        if nome != "scrypt":
            return False
        calculado = hashlib.scrypt(str(senha).encode("utf-8"), salt=bytes.fromhex(sal),
                                   n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calculado.hex(), h)


# Por quanto tempo o aparelho fica lembrado depois da senha certa (um dia de evento)
ACESSO_HORAS = 12


def _hash_codigo(codigo: str) -> str:
    return hashlib.sha256(str(codigo).encode("utf-8")).hexdigest()


def marca_da_senha(guardado: Optional[str]) -> Optional[str]:
    """Identifica a senha atual sem revelá-la: muda quando a senha é trocada."""
    return hashlib.sha256(guardado.encode()).hexdigest()[:16] if guardado else None


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


# hash de uma senha qualquer: conferir contra ele gasta o mesmo tempo que uma
# conferência de verdade (quem tenta adivinhar não descobre nada pelo tempo)
_HASH_FALSO = gerar_hash_senha(secrets.token_hex(8))


# ------------------------------------------------------------------
# SQL comum (escrito com "?"; cada banco troca pelo seu placeholder)
# ------------------------------------------------------------------
SALDO_EXPR = "SUM(CASE WHEN movimentacao = 'entrada' THEN 1 ELSE -1 END)"
ATIVA = "arquivado_em IS NULL"  # movimentação que ainda conta (não foi zerada)

SQL_EVENTOS = f"""
    SELECT e.id, e.nome, e.criado_em,
           COUNT(l.id)                               AS n_areas,
           COALESCE(SUM(l.cap_carro + l.cap_moto), 0) AS capacidade,
           COALESCE(SUM(s.ocupados), 0)               AS ocupados
    FROM evento e
    LEFT JOIN local l ON l.evento_id = e.id
    LEFT JOIN (SELECT local_id, {SALDO_EXPR} AS ocupados
               FROM movimentacao WHERE {ATIVA} GROUP BY local_id) s ON s.local_id = l.id
    GROUP BY e.id, e.nome, e.criado_em
    ORDER BY e.criado_em DESC, e.id DESC
"""
SQL_SALDO_UM = (
    f"SELECT COALESCE({SALDO_EXPR}, 0) FROM movimentacao "
    f"WHERE local_id = ? AND tipo_veiculo_id = ? AND {ATIVA}"
)
SINAL = "CASE WHEN m.movimentacao = 'entrada' THEN 1 ELSE -1 END"
SQL_PAINEL = f"""
    SELECT l.id, l.nome, l.cor, l.cap_carro, l.cap_moto,
           COALESCE(SUM(CASE WHEN m.tipo_veiculo_id = {TIPO_CARRO} THEN {SINAL} END), 0) AS ocup_carro,
           COALESCE(SUM(CASE WHEN m.tipo_veiculo_id = {TIPO_MOTO}  THEN {SINAL} END), 0) AS ocup_moto
    FROM local l
    LEFT JOIN movimentacao m ON m.local_id = l.id AND m.{ATIVA}
    WHERE l.evento_id = ?
    GROUP BY l.id, l.nome, l.cor, l.cap_carro, l.cap_moto, l.ordem
    ORDER BY l.ordem, l.id
"""
SQL_SALDOS_EVENTO = f"""
    SELECT m.local_id, m.tipo_veiculo_id, {SALDO_EXPR} AS ocupados
    FROM movimentacao m JOIN local l ON l.id = m.local_id
    WHERE l.evento_id = ? AND m.{ATIVA}
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

    def _travar_areas_do_evento(self, cur, evento_id: int) -> None:
        """
        Pega a mesma trava do registrar() para todas as áreas do evento: quem
        edita espera os registros em andamento terminarem (e os novos esperam a
        edição). Padrão: nada (o lock da transação basta).
        """

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
        """
        Cria o evento inicial (Corrida da FAB) se ainda não houver nenhum. Ele
        nasce sem senha: a senha vem dos secrets (ver aplicar_senha_inicial).
        """
        if not self._todas("SELECT id FROM evento LIMIT 1"):
            self._criar_evento(EVENTO_INICIAL, [
                {"nome": n, "cap_carro": c, "cap_moto": m, "cor": cor}
                for n, c, m, cor in AREAS_EVENTO_INICIAL
            ], senha_hash=None)

    # ---- eventos ----
    def listar_eventos(self) -> List[dict]:
        """Eventos (mais recentes primeiro) com nº de áreas, capacidade e ocupação."""
        eventos = self._todas(SQL_EVENTOS)
        for e in eventos:
            for k in ("n_areas", "capacidade", "ocupados"):
                e[k] = int(e[k] or 0)
        return eventos

    def obter_evento(self, evento_id: int) -> Optional[dict]:
        """
        Evento com "tem_senha" e "senha_marca" (identifica a senha atual sem
        revelá-la — muda quando a senha é trocada). O hash não sai daqui.
        """
        rows = self._todas("SELECT id, nome, criado_em, senha_hash FROM evento WHERE id = ?",
                           (evento_id,))
        if not rows:
            return None
        evento = rows[0]
        guardado = evento.pop("senha_hash")
        evento["tem_senha"] = bool(guardado)
        evento["senha_marca"] = marca_da_senha(guardado)
        return evento

    def criar_evento(self, nome: str, areas: List[dict], senha: str) -> int:
        """Cria o evento com as áreas e a senha (obrigatória)."""
        # confere na ordem da tela: nome, áreas e por último a senha
        _validar_nome_evento(nome), _normalizar_areas(areas)
        senha_hash = gerar_hash_senha(validar_senha_nova(senha))
        return self._criar_evento(nome, areas, senha_hash=senha_hash)

    def _criar_evento(self, nome: str, areas: List[dict], senha_hash: Optional[str]) -> int:
        nome, areas = _validar_nome_evento(nome), _normalizar_areas(areas)
        with self._transacao() as cur:
            self._exec(cur, "INSERT INTO evento (nome, criado_em, senha_hash) "
                            f"VALUES (?, {self.AGORA}, ?) RETURNING id", (nome, senha_hash))
            evento_id = int(cur.fetchone()[0])
            for ordem, a in enumerate(areas):
                self._inserir_area(cur, evento_id, a, ordem)
        return evento_id

    def renomear_evento(self, evento_id: int, nome: str) -> None:
        nome = _validar_nome_evento(nome)
        with self._transacao() as cur:
            self._exec(cur, "UPDATE evento SET nome = ? WHERE id = ?", (nome, evento_id))

    # ---- senha ----
    def conferir_senha(self, evento_id: int, senha: str) -> Optional[str]:
        """
        Confere a senha do evento. Retorna a marca da senha (ver obter_evento)
        se estiver certa; None se errada, se o evento não existe ou não tem senha.
        """
        rows = self._todas("SELECT senha_hash FROM evento WHERE id = ?", (evento_id,))
        guardado = rows[0]["senha_hash"] if rows else None
        if not guardado:
            conferir_hash_senha(senha, _HASH_FALSO)  # mesmo tempo de resposta
            return None
        return marca_da_senha(guardado) if conferir_hash_senha(senha, guardado) else None

    def trocar_senha(self, evento_id: int, senha: str) -> str:
        """Define a nova senha do evento (e esquece os aparelhos lembrados). Retorna a marca dela."""
        guardado = gerar_hash_senha(validar_senha_nova(senha))
        with self._transacao() as cur:
            self._exec(cur, "UPDATE evento SET senha_hash = ? WHERE id = ?", (guardado, evento_id))
            self._exec(cur, "DELETE FROM acesso WHERE evento_id = ?", (evento_id,))
        return marca_da_senha(guardado)

    # ---- aparelho lembrado (depois da senha certa) ----
    def criar_acesso(self, evento_id: int, senha_marca: str) -> str:
        """
        Gera o código que deixa o aparelho lembrado por ACESSO_HORAS. Devolve o
        código (vai para o cookie do aparelho); o banco guarda só o hash dele.
        """
        codigo = secrets.token_urlsafe(32)
        agora = int(time.time())
        with self._transacao() as cur:
            self._exec(cur, "DELETE FROM acesso WHERE expira_em < ?", (agora,))  # limpa os vencidos
            self._exec(cur, "INSERT INTO acesso (token_hash, evento_id, senha_marca, expira_em) "
                            "VALUES (?, ?, ?, ?)",
                       (_hash_codigo(codigo), evento_id, senha_marca, agora + ACESSO_HORAS * 3600))
        return codigo

    def conferir_acesso(self, evento_id: int, codigo: Optional[str]) -> Optional[str]:
        """
        O código do aparelho vale para este evento? Devolve a marca da senha
        atual se sim; None se não existe, venceu ou a senha foi trocada depois.
        """
        if not codigo or not isinstance(codigo, str) or len(codigo) > 100:
            return None
        rows = self._todas(
            "SELECT a.senha_marca, a.expira_em, e.senha_hash FROM acesso a "
            "JOIN evento e ON e.id = a.evento_id WHERE a.token_hash = ? AND a.evento_id = ?",
            (_hash_codigo(codigo), evento_id),
        )
        if not rows or int(rows[0]["expira_em"]) < time.time():
            return None
        atual = marca_da_senha(rows[0]["senha_hash"])
        return atual if atual and hmac.compare_digest(atual, rows[0]["senha_marca"]) else None

    def apagar_acesso(self, codigo: Optional[str]) -> None:
        """Esquece este aparelho (botão Sair)."""
        if codigo and isinstance(codigo, str):
            with self._transacao() as cur:
                self._exec(cur, "DELETE FROM acesso WHERE token_hash = ?", (_hash_codigo(codigo),))

    def aplicar_senha_inicial(self, senha: str) -> int:
        """
        Dá a senha (vinda dos secrets) aos eventos que ainda não têm senha —
        os criados antes de existir senha, como a Corrida da FAB. Eventos que já
        têm senha não mudam. Retorna quantos eventos receberam a senha.
        """
        senha = validar_senha_nova(senha)
        sem_senha = self._todas("SELECT id FROM evento WHERE senha_hash IS NULL")
        with self._transacao() as cur:
            for e in sem_senha:  # um hash (com sal próprio) por evento
                self._exec(cur, "UPDATE evento SET senha_hash = ? WHERE id = ? AND senha_hash IS NULL",
                           (gerar_hash_senha(senha), e["id"]))
        return len(sem_senha)

    def excluir_evento(self, evento_id: int) -> bool:
        """
        Exclui o evento e TUDO dele: áreas e todas as movimentações (inclusive
        as arquivadas). Não tem volta. Retorna False se o evento não existia.
        """
        with self._transacao() as cur:
            # registros em andamento nas áreas dele terminam antes (e os novos
            # esperam e depois recebem "esta área não existe mais")
            self._travar_areas_do_evento(cur, evento_id)
            self._exec(cur, "DELETE FROM movimentacao WHERE local_id IN "
                            "(SELECT id FROM local WHERE evento_id = ?)", (evento_id,))
            self._exec(cur, "DELETE FROM local WHERE evento_id = ?", (evento_id,))
            self._exec(cur, "DELETE FROM acesso WHERE evento_id = ?", (evento_id,))
            self._exec(cur, "DELETE FROM evento WHERE id = ?", (evento_id,))
            return cur.rowcount > 0

    def zerar_evento(self, evento_id: int) -> int:
        """
        Zera a ocupação de todas as áreas do evento, mantendo áreas e vagas.
        As movimentações não são apagadas: ficam arquivadas (com a data/hora do
        reset) e deixam de contar. Retorna quantas foram arquivadas.
        """
        with self._transacao() as cur:
            self._exec(
                cur,
                f"UPDATE movimentacao SET arquivado_em = {self.AGORA} "
                f"WHERE {ATIVA} AND local_id IN (SELECT id FROM local WHERE evento_id = ?)",
                (evento_id,),
            )
            return cur.rowcount

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
            # antes de ler a ocupação: um registro em andamento termina primeiro
            self._travar_areas_do_evento(cur, evento_id)
            self._exec(cur, "SELECT id, nome FROM local WHERE evento_id = ?", (evento_id,))
            existentes = {int(r[0]): r[1] for r in cur.fetchall()}
            mantidas = {a["id"] for a in areas if a["id"] is not None}
            if mantidas - existentes.keys():
                raise OperacaoInvalida("Alguma área não pertence mais a este evento. "
                                       "Recarregue a página e tente de novo.")
            self._exec(cur, SQL_SALDOS_EVENTO, (evento_id,))
            ocupacao = {(int(r[0]), int(r[1])): int(r[2]) for r in cur.fetchall()}

            for local_id in existentes.keys() - mantidas:
                ocupados = ocupacao.get((local_id, TIPO_CARRO), 0) + ocupacao.get((local_id, TIPO_MOTO), 0)
                if ocupados > 0:
                    raise OperacaoInvalida(
                        f"Não dá para excluir “{existentes[local_id]}”: ainda há "
                        f"{ocupados} veículo(s) nela. Dê saída antes de excluir."
                    )
                self._exec(cur, "DELETE FROM movimentacao WHERE local_id = ?", (local_id,))
                self._exec(cur, "DELETE FROM local WHERE id = ?", (local_id,))

            # vagas não podem ficar abaixo do que já está estacionado
            for a in areas:
                for tipo, campo, nome_tipo in ((TIPO_CARRO, "cap_carro", "carro"), (TIPO_MOTO, "cap_moto", "moto")):
                    ocupados = ocupacao.get((a["id"], tipo), 0) if a["id"] is not None else 0
                    if a[campo] < ocupados:
                        raise OperacaoInvalida(
                            f"“{a['nome']}” tem {ocupados} {nome_tipo}(s) estacionado(s): as vagas "
                            f"de {nome_tipo} não podem ser menos que {ocupados}. Dê saída antes."
                        )

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
    def registrar(self, tipo_veiculo_id: int, local_id: int, movimentacao: str,
                  quantidade: int = 1) -> Dict[str, int]:
        """
        Insere `quantidade` (1 a QUANTIDADE_MAX) movimentos de 'entrada' ou
        'saida' com horário = agora — uma linha por veículo, todas ou nenhuma.
        A capacidade e o saldo são lidos na mesma transação do insert; se o
        movimento for recusado, levanta MovimentoInvalido.
        Retorna {"id", "ocupados", "capacidade"} — ocupados já com este movimento.
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
            ocupados = int(area[2])
            validar_movimento(movimentacao, ocupados, int(capacidade), quantidade)

            # um único INSERT com todas as linhas (uma ida só ao banco)
            linha = f"(?, ?, ?, {self.AGORA})"
            self._exec(
                cur,
                "INSERT INTO movimentacao (tipo_veiculo_id, local_id, movimentacao, horario) "
                f"VALUES {', '.join([linha] * quantidade)} RETURNING id",
                (tipo_veiculo_id, local_id, movimentacao) * quantidade,
            )
            novo_id = max(int(r[0]) for r in cur.fetchall())
        return {
            "id": novo_id,
            "ocupados": ocupados + (quantidade if movimentacao == "entrada" else -quantidade),
            "capacidade": int(capacidade),
        }

    def saldo(self, local_id: int, tipo_veiculo_id: int) -> int:
        """Ocupação atual (entradas - saídas) de um tipo em uma área."""
        rows = self._todas(SQL_SALDO_UM, (local_id, tipo_veiculo_id))
        return int(next(iter(rows[0].values())))

    def saldos(self, evento_id: int) -> Dict[Tuple[int, int], int]:
        """Saldos do evento: {(local_id, tipo_veiculo_id): ocupados}."""
        rows = self._todas(SQL_SALDOS_EVENTO, (evento_id,))
        return {(r["local_id"], r["tipo_veiculo_id"]): int(r["ocupados"]) for r in rows}

    def historico(self, local_id: int, limite: int = 50) -> List[dict]:
        """Últimos movimentos ativos da área (ordem de registro; mais recentes primeiro)."""
        return self._todas(
            "SELECT id, tipo_veiculo_id, local_id, movimentacao, horario FROM movimentacao "
            f"WHERE local_id = ? AND {ATIVA} ORDER BY id DESC LIMIT ?",
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
                nome       TEXT NOT NULL,
                criado_em  TEXT NOT NULL,
                senha_hash TEXT
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
                horario         TEXT    NOT NULL,
                arquivado_em    TEXT
            );
            CREATE TABLE IF NOT EXISTS acesso (
                token_hash  TEXT    PRIMARY KEY,
                evento_id   INTEGER NOT NULL REFERENCES evento(id) ON DELETE CASCADE,
                senha_marca TEXT    NOT NULL,
                expira_em   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_local_evento ON local (evento_id);
            CREATE INDEX IF NOT EXISTS idx_mov_local_tipo
                ON movimentacao (local_id, tipo_veiculo_id);
            """
        )
        colunas = {r[1] for r in self.conn.execute("PRAGMA table_info(movimentacao)")}
        if "arquivado_em" not in colunas:  # arquivo SQLite de versão anterior
            self.conn.execute("ALTER TABLE movimentacao ADD COLUMN arquivado_em TEXT")
        if "senha_hash" not in {r[1] for r in self.conn.execute("PRAGMA table_info(evento)")}:
            self.conn.execute("ALTER TABLE evento ADD COLUMN senha_hash TEXT")
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
        # banco novo: o schema.sql cria as tabelas e o evento inicial. (Não há
        # _semear_se_vazio aqui: se todos os eventos forem excluídos, a Corrida
        # da FAB não pode reaparecer sozinha ao reiniciar o app.)
        self._migrar_se_preciso()

    def _migrar_se_preciso(self) -> None:
        """
        Aplica o schema.sql se o banco não está na versão atual (vazio, sem
        eventos, sem arquivamento, sem senha ou sem a tabela de acessos). O
        script é idempotente e migra os dados.
        """
        def atualizado(cur):
            cur.execute(
                "SELECT to_regclass('evento') IS NOT NULL AND to_regclass('acesso') IS NOT NULL AND ("
                "  SELECT COUNT(*) FROM information_schema.columns"
                "  WHERE table_schema = current_schema() AND ("
                "    (table_name = 'local' AND column_name = 'evento_id') OR"
                "    (table_name = 'movimentacao' AND column_name = 'arquivado_em') OR"
                "    (table_name = 'evento' AND column_name = 'senha_hash'))"
                ") = 3"
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

    def _travar_areas_do_evento(self, cur, evento_id: int) -> None:
        # sempre na mesma ordem (id, tipo), para duas edições não se travarem
        self._exec(
            cur,
            "SELECT pg_advisory_xact_lock(l.id, t.tipo) FROM local l "
            f"CROSS JOIN (VALUES ({TIPO_CARRO}), ({TIPO_MOTO})) AS t(tipo) "
            "WHERE l.evento_id = ? ORDER BY l.id, t.tipo",
            (evento_id,),
        )
        cur.fetchall()
