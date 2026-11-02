"""Deterministic reconciliation engine.

Every number in a finding comes from this module, never from the language
model. The agent layer only explains and queries what is computed here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher

import pandas as pd

from .fees import expected_fee, to_money

REFERENCE_PATTERN = re.compile(r"\bPMT-\d{4,}\b", re.IGNORECASE)
_SUFFIXES = re.compile(r"\b(inc|llc|ltd|co|corp|corporation|company|group)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    id: str
    type: str
    severity: str
    summary: str
    amount_impact: float
    entry_ids: list[str] = field(default_factory=list)
    txn_ids: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReconciliationResult:
    findings: list[Finding]
    matches: pd.DataFrame
    stats: dict

    def findings_frame(self) -> pd.DataFrame:
        rows = [
            {
                "id": f.id,
                "type": f.type,
                "severity": f.severity,
                "amount_impact": f.amount_impact,
                "entries": ", ".join(f.entry_ids),
                "bank_txns": ", ".join(f.txn_ids),
                "summary": f.summary,
            }
            for f in self.findings
        ]
        return pd.DataFrame(rows)


# ---------------------------------------------------------------- helpers

def normalize_name(name: str) -> str:
    name = _NON_ALNUM.sub(" ", name.lower())
    name = _SUFFIXES.sub(" ", name)
    return " ".join(name.split())


def names_match(a: str, b: str, threshold: float = 0.6) -> bool:
    """Bank feeds upper-case and truncate names, so allow prefixes and fuzz."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na.startswith(nb) or nb.startswith(na):
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def extract_reference(memo: str) -> str | None:
    found = REFERENCE_PATTERN.search(memo or "")
    return found.group(0).upper() if found else None


def _money(x) -> float:
    return float(to_money(x))


# ---------------------------------------------------------------- engine

def reconcile(
    ledger: pd.DataFrame,
    bank: pd.DataFrame,
    date_window_days: int = 3,
    in_transit_days: int = 5,
    statement_end: pd.Timestamp | None = None,
) -> ReconciliationResult:
    raw: list[dict] = []
    ledger_by_id = ledger.set_index("entry_id", drop=False)
    statement_end = statement_end or bank["date"].max()

    bank = bank.copy()
    bank["ref"] = bank["memo"].map(extract_reference)
    bank = bank.sort_values(["date", "txn_id"])

    matched_entry: dict[str, str] = {}   # entry_id -> txn_id
    match_method: dict[str, str] = {}
    unmatched_txns: list[pd.Series] = []

    # Pass 1: explicit references in the bank memo.
    for _, txn in bank.iterrows():
        ref = txn["ref"]
        if ref and ref in ledger_by_id.index:
            if ref in matched_entry:
                first = matched_entry[ref]
                raw.append(dict(
                    type="DUPLICATE_PAYMENT",
                    amount_impact=_money(txn["amount"]),
                    entry_ids=[ref],
                    txn_ids=[first, txn["txn_id"]],
                    summary=(f"{ref} was deposited twice: {first} and {txn['txn_id']} "
                             f"(${txn['amount']:,.2f} each)."),
                    evidence={"memo": txn["memo"], "duplicate_date": txn["date"].date().isoformat()},
                ))
            else:
                matched_entry[ref] = txn["txn_id"]
                match_method[ref] = "reference"
        else:
            unmatched_txns.append(txn)

    # Pass 2: no usable reference, so match on exact amount, date window and payer.
    still_unmatched: list[pd.Series] = []
    open_entries = ledger[~ledger["entry_id"].isin(matched_entry)]
    for txn in unmatched_txns:
        cands = open_entries[
            ((open_entries["net"] - txn["amount"]).abs() < 0.005)
            & ((open_entries["date"] - txn["date"]).abs() <= timedelta(days=date_window_days))
        ]
        cands = cands[cands["payer"].map(lambda p, cp=txn["counterparty"]: names_match(p, cp))]
        if len(cands) == 1:
            eid = cands.iloc[0]["entry_id"]
            matched_entry[eid] = txn["txn_id"]
            match_method[eid] = "heuristic"
            open_entries = open_entries[open_entries["entry_id"] != eid]
        else:
            still_unmatched.append((txn, len(cands)))

    for txn, n_cands in still_unmatched:
        reason = "several possible ledger entries" if n_cands > 1 else "no ledger entry"
        raw.append(dict(
            type="UNMATCHED_BANK",
            amount_impact=_money(txn["amount"]),
            txn_ids=[txn["txn_id"]],
            summary=(f"Bank deposit {txn['txn_id']} from {txn['counterparty']} "
                     f"(${txn['amount']:,.2f}) matches {reason}."),
            evidence={"memo": txn["memo"], "date": txn["date"].date().isoformat(),
                      "candidate_count": n_cands},
        ))

    # Ledger entries never seen at the bank.
    cutoff = statement_end - timedelta(days=in_transit_days)
    in_transit = 0
    for _, e in ledger[~ledger["entry_id"].isin(matched_entry)].iterrows():
        if e["date"] > cutoff:
            in_transit += 1
            continue
        days = (statement_end - e["date"]).days
        raw.append(dict(
            type="UNMATCHED_LEDGER",
            amount_impact=_money(e["net"]),
            entry_ids=[e["entry_id"]],
            summary=(f"{e['entry_id']} ({e['payer']}, ${e['net']:,.2f} net) is in the ledger "
                     f"but has not reached the bank after {days} days."),
            evidence={"date": e["date"].date().isoformat(), "days_outstanding": days},
        ))

    # Checks on each matched pair.
    bank_by_id = bank.set_index("txn_id", drop=False)
    for eid, tid in matched_entry.items():
        e, t = ledger_by_id.loc[eid], bank_by_id.loc[tid]
        exp_fee = expected_fee(e["amount"], e["method"])
        fee_err = to_money(e["fee"]) - exp_fee            # >0 means fee overstated
        diff = to_money(t["amount"]) - to_money(e["net"])  # bank minus ledger

        if fee_err != 0:
            raw.append(dict(
                type="FEE_MISCALC",
                amount_impact=float(abs(fee_err)),
                entry_ids=[eid], txn_ids=[tid],
                summary=(f"{eid} records {'an' if e['method'] == 'ACH' else 'a'} {e['method']} fee of "
                         f"${e['fee']:,.2f}; the schedule gives ${exp_fee:,.2f} on ${e['amount']:,.2f}."),
                evidence={"ledger_fee": float(e["fee"]), "expected_fee": float(exp_fee),
                          "method": e["method"], "gross": float(e["amount"]),
                          "explains_bank_difference": diff == fee_err},
            ))
        residual = diff - fee_err if fee_err != 0 else diff
        if residual != 0:
            raw.append(dict(
                type="AMOUNT_MISMATCH",
                amount_impact=float(abs(residual)),
                entry_ids=[eid], txn_ids=[tid],
                summary=(f"{eid}: bank received ${t['amount']:,.2f} but the ledger expects "
                         f"${e['net']:,.2f} net ({'short' if residual < 0 else 'over'} by "
                         f"${abs(residual):,.2f} after fee corrections)."),
                evidence={"bank_amount": float(t["amount"]), "ledger_net": float(e["net"]),
                          "unexplained_difference": float(residual)},
            ))
        if not names_match(e["payer"], t["counterparty"]):
            raw.append(dict(
                type="PAYEE_MISMATCH",
                amount_impact=0.0,
                entry_ids=[eid], txn_ids=[tid],
                summary=(f"{eid} is booked to {e['payer']} but the deposit came from "
                         f"{t['counterparty']}."),
                evidence={"ledger_payer": e["payer"], "bank_counterparty": t["counterparty"],
                          "matched_by": match_method[eid]},
            ))

    # Split payments must add up to the invoice they belong to.
    splits = ledger[ledger["split_group"] != ""]
    for group, parts in splits.groupby("split_group"):
        total = to_money(parts["amount"].sum())
        invoice_total = to_money(parts["invoice_total"].dropna().iloc[0])
        if total != invoice_total:
            raw.append(dict(
                type="SPLIT_TOTAL_MISMATCH",
                amount_impact=float(abs(total - invoice_total)),
                entry_ids=sorted(parts["entry_id"].tolist()),
                summary=(f"Split payment {group} has {len(parts)} parts totaling ${total:,.2f}, "
                         f"but the invoice is ${invoice_total:,.2f}."),
                evidence={"split_group": group, "parts_total": float(total),
                          "invoice_total": float(invoice_total)},
            ))

    findings = _finalize(raw)
    matches = pd.DataFrame(
        [{"entry_id": e, "txn_id": t, "matched_by": match_method[e]} for e, t in matched_entry.items()]
    )
    stats = {
        "ledger_entries": len(ledger),
        "bank_transactions": len(bank),
        "matched": len(matched_entry),
        "matched_by_reference": sum(m == "reference" for m in match_method.values()),
        "matched_by_heuristic": sum(m == "heuristic" for m in match_method.values()),
        "in_transit": in_transit,
        "findings": len(findings),
        "statement_end": statement_end.date().isoformat(),
        "dollars_flagged": round(sum(f.amount_impact for f in findings), 2),
    }
    return ReconciliationResult(findings=findings, matches=matches, stats=stats)


def severity_for(item: dict) -> str:
    kind, impact = item["type"], item["amount_impact"]
    if kind in {"DUPLICATE_PAYMENT", "UNMATCHED_BANK"}:
        return "high"
    if kind in {"AMOUNT_MISMATCH", "UNMATCHED_LEDGER"}:
        return "high" if impact >= 100 else "medium"
    if kind in {"FEE_MISCALC", "SPLIT_TOTAL_MISMATCH"}:
        return "medium"
    return "low"


def _finalize(raw: list[dict]) -> list[Finding]:
    for item in raw:
        item["severity"] = severity_for(item)
    raw.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["amount_impact"], r["type"]))
    return [Finding(id=f"F-{i:03d}", **item) for i, item in enumerate(raw, start=1)]
