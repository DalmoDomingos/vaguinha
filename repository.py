"""
Camada de persistência (Repository pattern).

- `Repository`        : interface / contrato.
- `SQLiteRepository`  : implementação funcional para prototipar (SQLite, em memória ou arquivo).
- `PostgresRepository`: implementação pronta para plugar no schema PostgreSQL real (psycopg2).

Toda a regra de ocupação é derivada do saldo:
    ocupados = SUM(entrada) - SUM(saida)   por (local_id, tipo_veiculo_id)
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ------------------------------------------------------------------
# Contrato
# ------------------------------------------------------------------
class Repository(ABC):

    @abstractmethod
    def registrar(self, tipo_veiculo_id: int, local_id: int, movimentacao: str) -> int:
        """Insere um evento de 'entrada' ou 'saida' com horário = agora. Retorna o id."""

    @abstractmethod
    def saldo(self, local_id: int, tipo_veiculo_id: int) -> int:
        """Ocupação atual (entradas - saídas) de um tipo em uma área."""

    @abstractmethod
    def saldos(self) -> Dict[Tuple[int, int], int]:
        """Todos os saldos: {(local_id, tipo_veiculo_id): ocupados}."""

    @abstractmethod
    def historico(self, local_id: Optional[int] = None, limite: int = 50) -> List[dict]:
        """Últimos movimentos (mais recentes primeiro)."""


# ------------------------------------------------------------------
# SQL compartilhado (placeholder trocado por implementação)
# ------------------------------------------------------------------
SQL_INSERT = (
    "INSERT INTO movimentacao (tipo_veiculo_id, local_id, movimentacao, horario) "
    "VALUES ({p}, {p}, {p}, {p})"
)
SQL_SALDO_UM = (
    "SELECT COALESCE(SUM(CASE WHEN movimentacao = 'entrada' THEN 1 ELSE -1 END), 0) "
    "FROM movimentacao WHERE local_id = {p} AND tipo_veiculo_id = {p}"
)
SQL_SALDOS = (
    "SELECT local_id, tipo_veiculo_id, "
    "SUM(CASE WHEN movimentacao = 'entrada' THEN 1 ELSE -1 END) AS ocupados "
    "FROM movimentacao GROUP BY local_id, tipo_veiculo_id"
)
SQL_HISTORICO = (
    "SELECT id, tipo_veiculo_id, local_id, movimentacao, horario "
    "FROM movimentacao {where} ORDER BY horario DESC, id DESC LIMIT {p}"
)


# ------------------------------------------------------------------
# SQLite (funcional, para prototipar)
# ------------------------------------------------------------------
class SQLiteRepository(Repository):
    PH = "?"  # placeholder do sqlite3

    def __init__(self, db_path: str = ":memory:"):
        # check_same_thread=False: Streamlit pode reusar a conexão entre reruns/threads.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._criar_schema()

    def _criar_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS movimentacao (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo_veiculo_id INTEGER NOT NULL,
                local_id        INTEGER NOT NULL,
                movimentacao    TEXT    NOT NULL CHECK (movimentacao IN ('entrada','saida')),
                horario         TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mov_local_tipo
                ON movimentacao (local_id, tipo_veiculo_id);
            """
        )
        self.conn.commit()

    def registrar(self, tipo_veiculo_id: int, local_id: int, movimentacao: str) -> int:
        if movimentacao not in ("entrada", "saida"):
            raise ValueError("movimentacao deve ser 'entrada' ou 'saida'")
        cur = self.conn.execute(
            SQL_INSERT.format(p=self.PH),
            (tipo_veiculo_id, local_id, movimentacao, datetime.now().isoformat(" ", "seconds")),
        )
        self.conn.commit()
        return cur.lastrowid

    def saldo(self, local_id: int, tipo_veiculo_id: int) -> int:
        row = self.conn.execute(
            SQL_SALDO_UM.format(p=self.PH), (local_id, tipo_veiculo_id)
        ).fetchone()
        return int(row[0])

    def saldos(self) -> Dict[Tuple[int, int], int]:
        rows = self.conn.execute(SQL_SALDOS).fetchall()
        return {(r["local_id"], r["tipo_veiculo_id"]): int(r["ocupados"]) for r in rows}

    def historico(self, local_id: Optional[int] = None, limite: int = 50) -> List[dict]:
        if local_id is None:
            sql = SQL_HISTORICO.format(where="", p=self.PH)
            params = (limite,)
        else:
            sql = SQL_HISTORICO.format(where=f"WHERE local_id = {self.PH}", p=self.PH)
            params = (local_id, limite)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


# ------------------------------------------------------------------
# PostgreSQL (pronto para plugar no schema real)
# ------------------------------------------------------------------
class PostgresRepository(Repository):
    """
    Requer psycopg2 (`pip install psycopg2-binary`) e o schema.sql aplicado.
    Exemplo:
        repo = PostgresRepository("postgresql://user:senha@host:5432/vaguinha")
    """
    PH = "%s"  # placeholder do psycopg2

    def __init__(self, dsn: str):
        import psycopg2  # import tardio: só exige a lib se realmente usar Postgres
        self._pg = psycopg2
        self.conn = psycopg2.connect(dsn)

    def registrar(self, tipo_veiculo_id: int, local_id: int, movimentacao: str) -> int:
        if movimentacao not in ("entrada", "saida"):
            raise ValueError("movimentacao deve ser 'entrada' ou 'saida'")
        sql = (
            "INSERT INTO movimentacao (tipo_veiculo_id, local_id, movimentacao, horario) "
            "VALUES (%s, %s, %s, NOW()) RETURNING id"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (tipo_veiculo_id, local_id, movimentacao))
            new_id = cur.fetchone()[0]
        self.conn.commit()
        return int(new_id)

    def saldo(self, local_id: int, tipo_veiculo_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute(SQL_SALDO_UM.format(p=self.PH), (local_id, tipo_veiculo_id))
            return int(cur.fetchone()[0])

    def saldos(self) -> Dict[Tuple[int, int], int]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_SALDOS)
            return {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}

    def historico(self, local_id: Optional[int] = None, limite: int = 50) -> List[dict]:
        with self.conn.cursor() as cur:
            if local_id is None:
                cur.execute(SQL_HISTORICO.format(where="", p=self.PH), (limite,))
            else:
                cur.execute(
                    SQL_HISTORICO.format(where=f"WHERE local_id = {self.PH}", p=self.PH),
                    (local_id, limite),
                )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
