"""Command line interface.

    python -m ledgerlens reconcile data/sample
    python -m ledgerlens ask data/sample "Which customers were double-paid?"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import ReconciliationAgent, offline_summary
from .data import DataError, load_bank, load_ledger
from .reconcile import reconcile
from .store import LedgerStore


def _load(folder: str):
    root = Path(folder)
    ledger = load_ledger(root / "ledger_entries.csv")
    bank = load_bank(root / "bank_transactions.csv")
    return ledger, bank, reconcile(ledger, bank)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledgerlens")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("reconcile", help="Match ledger to bank and print findings")
    rec.add_argument("folder")
    rec.add_argument("--json", action="store_true", help="Print findings as JSON")
    ask = sub.add_parser("ask", help="Ask the AI analyst a question")
    ask.add_argument("folder")
    ask.add_argument("question")
    args = parser.parse_args(argv)

    try:
        ledger, bank, result = _load(args.folder)
    except (DataError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "reconcile":
        if args.json:
            print(json.dumps([f.to_dict() for f in result.findings], indent=2))
        else:
            print(offline_summary(result))
            print()
            for f in result.findings:
                print(f"{f.id}  [{f.severity:<6}] {f.type:<21} {f.summary}")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set, so here is the offline summary instead:\n")
        print(offline_summary(result))
        return 0
    agent = ReconciliationAgent(result, LedgerStore(ledger, bank, result))
    answer = agent.ask(args.question)
    for call in answer.tool_calls:
        print(f"  · {call['tool']} {json.dumps(call['input'])}", file=sys.stderr)
    print(answer.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
