"""Score the engine against known injected issues.

    python -m ledgerlens.evaluate data/sample
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from .data import load_bank, load_ledger
from .reconcile import Finding, reconcile


def finding_key(f: Finding) -> tuple[str, str]:
    if f.type == "UNMATCHED_BANK":
        return f.type, f.txn_ids[0]
    if f.type == "SPLIT_TOTAL_MISMATCH":
        return f.type, f.evidence["split_group"]
    return f.type, f.entry_ids[0]


def score(findings: list[Finding], truth: list[dict]) -> dict:
    predicted = {finding_key(f) for f in findings}
    expected = {(t["type"], t["id"]) for t in truth}
    per_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for k in predicted & expected:
        per_type[k[0]]["tp"] += 1
    for k in predicted - expected:
        per_type[k[0]]["fp"] += 1
    for k in expected - predicted:
        per_type[k[0]]["fn"] += 1
    tp, fp, fn = (sum(v[m] for v in per_type.values()) for m in ("tp", "fp", "fn"))
    return {
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "per_type": dict(sorted(per_type.items())),
        "false_positives": sorted(predicted - expected),
        "missed": sorted(expected - predicted),
    }


def main(folder: str) -> int:
    root = Path(folder)
    result = reconcile(load_ledger(root / "ledger_entries.csv"), load_bank(root / "bank_transactions.csv"))
    report = score(result.findings, json.loads((root / "injected_issues.json").read_text()))
    print(f"precision {report['precision']:.0%}  recall {report['recall']:.0%}")
    for kind, c in report["per_type"].items():
        print(f"  {kind:<21} tp={c['tp']} fp={c['fp']} fn={c['fn']}")
    for label in ("false_positives", "missed"):
        if report[label]:
            print(f"{label}: {report[label]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "data/sample"))
