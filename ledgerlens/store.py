"""Read-only SQL access for the agent.

The model can write its own SQL to answer questions, so the connection uses
SQLite's authorizer hook to refuse anything other than reads. This is
enforced by the database engine, not by string matching on the query.
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd

from .reconcile import ReconciliationResult

MAX_ROWS = 200

_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}

SCHEMA_DOC = """\
Tables (SQLite):
  ledger(entry_id TEXT, date TEXT 'YYYY-MM-DD', payer TEXT, invoice_id TEXT, method TEXT
         ['ACH'|'CARD'|'WIRE'], amount REAL gross, fee REAL, net REAL, split_group TEXT,
         invoice_total REAL)
  bank(txn_id TEXT, date TEXT 'YYYY-MM-DD', counterparty TEXT, memo TEXT, amount REAL net deposit)
  matches(entry_id TEXT, txn_id TEXT, matched_by TEXT ['reference'|'heuristic'])
  findings(id TEXT, type TEXT, severity TEXT, amount_impact REAL, entries TEXT, bank_txns TEXT,
           summary TEXT, evidence TEXT JSON)
"""


class QueryError(ValueError):
    pass


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


class LedgerStore:
    def __init__(self, ledger: pd.DataFrame, bank: pd.DataFrame, result: ReconciliationResult):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        for name, df in {"ledger": ledger, "bank": bank.drop(columns=["ref"], errors="ignore")}.items():
            out = df.copy()
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            out.to_sql(name, self.conn, index=False)
        result.matches.to_sql("matches", self.conn, index=False)
        frame = result.findings_frame()
        frame["evidence"] = [json.dumps(f.evidence) for f in result.findings]
        frame.to_sql("findings", self.conn, index=False)
        self.conn.set_authorizer(_authorizer)

    def query(self, sql: str) -> dict:
        sql = sql.strip().rstrip(";")
        if ";" in sql:
            raise QueryError("Only one statement per query is allowed.")
        try:
            cur = self.conn.execute(sql)
        except sqlite3.DatabaseError as exc:
            raise QueryError(str(exc)) from exc
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS,
        }
