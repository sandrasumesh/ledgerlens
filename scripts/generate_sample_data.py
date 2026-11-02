"""Generate a realistic synthetic ledger and bank feed with known problems.

Every injected problem is written to injected_issues.json so the engine can
be scored for precision and recall (see ledgerlens/evaluate.py).

    python scripts/generate_sample_data.py --out data/sample --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledgerlens.fees import expected_fee  # noqa: E402

PAYERS = [
    "Acme Supply Co.", "Brightline Dental LLC", "Cedar Ridge Property Group", "Delmarva Logistics Inc.",
    "Evergreen Pediatrics", "Fairway Auto Body", "Greenspring Bakery", "Harbor Point Marina LLC",
    "Ironwood Construction Corp.", "Juniper Home Health", "Keystone Legal Partners",
    "Lakeside Veterinary Clinic",
    "Meridian Staffing Inc.", "Northstar HVAC Services", "Oakmont Family Dentistry", "Patapsco Print Shop",
    "Quarry Lane Fitness", "Riverbend Montessori",
]
UNKNOWN_PAYERS = ["Globex Payments", "Initech Remit", "Umbrella Holdings"]
START, END = date(2026, 9, 1), date(2026, 10, 30)


def bank_name(payer: str, rng: random.Random) -> str:
    """Banks upper-case names, drop punctuation and often truncate them."""
    name = payer.upper().replace(".", "").replace(",", "")
    if rng.random() < 0.4:
        name = name[:18].rstrip()
    return name


def money(rng: random.Random) -> float:
    return round(min(rng.lognormvariate(6.4, 0.9), 12000) + 25, 2)


def rand_date(rng: random.Random, start=START, end=END) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build(seed: int):
    rng = random.Random(seed)
    ledger, n = [], 1000

    def add_entry(payer, amount, when, invoice, split_group="", invoice_total=""):
        nonlocal n
        method = rng.choices(["ACH", "CARD", "WIRE"], weights=[6, 3, 1])[0]
        if method == "WIRE" and amount < 2000:
            method = "ACH"
        fee = float(expected_fee(amount, method))
        ledger.append(dict(entry_id=f"PMT-{n}", date=when, payer=payer, invoice_id=invoice,
                           method=method, amount=amount, fee=fee, net=round(amount - fee, 2),
                           split_group=split_group, invoice_total=invoice_total))
        n += 1

    for i in range(240):
        add_entry(rng.choice(PAYERS), money(rng), rand_date(rng), f"INV-{5000 + i}")
    for i in range(10):  # invoices paid in two or three parts
        payer, total = rng.choice(PAYERS), round(money(rng) + 500, 2)
        k = rng.choice([2, 3])
        total_cents = round(total * 100)
        cuts = sorted(rng.sample(range(1, total_cents), k - 1))
        parts = [b - a for a, b in zip([0] + cuts, cuts + [total_cents])]
        when = rand_date(rng, START, END - timedelta(days=20))
        inv = f"INV-{7000 + i}"
        for j, cents in enumerate(parts):
            add_entry(payer, cents / 100, when + timedelta(days=7 * j), inv, inv, total)

    bank, t = [], 90000
    by_entry = {}
    for e in ledger:
        if e["date"] >= END - timedelta(days=2) and rng.random() < 0.5:
            continue  # still in transit at statement end
        cp = bank_name(e["payer"], rng)
        txn = dict(txn_id=f"T-{t}", date=e["date"] + timedelta(days=rng.randint(0, 2)), counterparty=cp,
                   memo=f"{e['method']} DEP {e['entry_id']} {cp[:12]}", amount=e["net"])
        bank.append(txn)
        by_entry[e["entry_id"]] = txn
        t += 1

    truth = []
    eligible = [e for e in ledger if not e["split_group"] and e["date"] < END - timedelta(days=10)
                and e["entry_id"] in by_entry]
    rng.shuffle(eligible)
    pool = iter(eligible)

    for delta in (1.00, -0.50, 2.25, 0.30):
        e = next(pool)
        e["fee"] = round(max(e["fee"] + delta, 0), 2)
        e["net"] = round(e["amount"] - e["fee"], 2)
        truth.append({"type": "FEE_MISCALC", "id": e["entry_id"]})
    for short in (25.00, 150.00, 0.75):
        e = next(pool)
        by_entry[e["entry_id"]]["amount"] = round(e["net"] - short, 2)
        truth.append({"type": "AMOUNT_MISMATCH", "id": e["entry_id"]})
    for _ in range(2):
        e = next(pool)
        dup = dict(by_entry[e["entry_id"]], txn_id=f"T-{t}")
        dup["date"] = dup["date"] + timedelta(days=1)
        bank.append(dup)
        t += 1
        truth.append({"type": "DUPLICATE_PAYMENT", "id": e["entry_id"]})
    for _ in range(3):
        e = next(pool)
        bank.remove(by_entry.pop(e["entry_id"]))
        truth.append({"type": "UNMATCHED_LEDGER", "id": e["entry_id"]})
    for _ in range(2):
        e = next(pool)
        other = next(p for p in PAYERS if p[0] != e["payer"][0] and p != e["payer"])
        by_entry[e["entry_id"]]["counterparty"] = bank_name(other, rng)
        truth.append({"type": "PAYEE_MISMATCH", "id": e["entry_id"]})
    for payer in UNKNOWN_PAYERS:
        txn = dict(txn_id=f"T-{t}", date=rand_date(rng, START, END - timedelta(days=5)),
                   counterparty=payer.upper(), memo=f"DEPOSIT REF {rng.randint(10000, 99999)}",
                   amount=money(rng))
        bank.append(txn)
        truth.append({"type": "UNMATCHED_BANK", "id": txn["txn_id"]})
        t += 1
    for _ in range(20):  # clean deposits whose memo lost the reference
        e = next(pool)
        txn = by_entry[e["entry_id"]]
        txn["memo"] = f"{e['method']} DEPOSIT {txn['counterparty'][:14]}"

    groups = sorted({e["split_group"] for e in ledger if e["split_group"]})
    for g in rng.sample(groups, 2):
        part = next(e for e in ledger if e["split_group"] == g)
        part["amount"] = round(part["amount"] + 40.00, 2)
        part["fee"] = float(expected_fee(part["amount"], part["method"]))
        part["net"] = round(part["amount"] - part["fee"], 2)
        if part["entry_id"] in by_entry:
            by_entry[part["entry_id"]]["amount"] = part["net"]
        truth.append({"type": "SPLIT_TOTAL_MISMATCH", "id": g})

    ledger.sort(key=lambda r: (r["date"], r["entry_id"]))
    bank.sort(key=lambda r: (r["date"], r["txn_id"]))
    return ledger, bank, truth


def write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/sample")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger, bank, truth = build(args.seed)
    write_csv(out / "ledger_entries.csv", ledger)
    write_csv(out / "bank_transactions.csv", bank)
    (out / "injected_issues.json").write_text(json.dumps(truth, indent=2) + "\n")
    print(f"Wrote {len(ledger)} ledger rows, {len(bank)} bank rows, {len(truth)} injected issues to {out}")


if __name__ == "__main__":
    main()
