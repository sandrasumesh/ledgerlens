"""Load and validate the two inputs: the internal ledger and the bank feed."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

LEDGER_COLUMNS = {
    "entry_id": str,
    "date": "date",
    "payer": str,
    "invoice_id": str,
    "method": str,
    "amount": float,
    "fee": float,
    "net": float,
    "split_group": str,
    "invoice_total": float,
}

BANK_COLUMNS = {
    "txn_id": str,
    "date": "date",
    "counterparty": str,
    "memo": str,
    "amount": float,
}


class DataError(ValueError):
    """Raised when an input file is missing columns or has bad values."""


def _coerce(df: pd.DataFrame, schema: dict, name: str) -> pd.DataFrame:
    missing = [c for c in schema if c not in df.columns]
    if missing:
        raise DataError(f"{name} is missing columns: {', '.join(missing)}")
    df = df[list(schema)].copy()
    for col, kind in schema.items():
        if kind == "date":
            parsed = pd.to_datetime(df[col], errors="coerce")
            bad = parsed.isna()
            if bad.any():
                rows = ", ".join(str(i + 2) for i in df.index[bad][:5])
                raise DataError(f"{name}: unreadable dates in column '{col}' (rows {rows})")
            df[col] = parsed.dt.normalize()
        elif kind is float:
            optional = col in {"invoice_total"}
            values = df[col].replace("", pd.NA) if optional else df[col]
            numeric = pd.to_numeric(values, errors="coerce")
            bad = numeric.isna() & values.notna() if optional else numeric.isna()
            if bad.any():
                rows = ", ".join(str(i + 2) for i in df.index[bad][:5])
                raise DataError(f"{name}: non-numeric values in '{col}' (rows {rows})")
            df[col] = numeric.round(2)
        else:
            df[col] = df[col].fillna("").astype(str).str.strip()
    return df


def load_ledger(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    df = source if isinstance(source, pd.DataFrame) else pd.read_csv(source, dtype=str)
    df = _coerce(df, LEDGER_COLUMNS, "ledger")
    if df["entry_id"].duplicated().any():
        dupes = df.loc[df["entry_id"].duplicated(), "entry_id"].tolist()[:5]
        raise DataError(f"ledger: duplicate entry_id values {dupes}")
    df["method"] = df["method"].str.upper()
    return df


def load_bank(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    df = source if isinstance(source, pd.DataFrame) else pd.read_csv(source, dtype=str)
    return _coerce(df, BANK_COLUMNS, "bank feed")
