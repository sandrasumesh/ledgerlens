import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from generate_sample_data import build  # noqa: E402
from ledgerlens.agent import ReconciliationAgent, offline_summary  # noqa: E402
from ledgerlens.data import DataError, load_bank, load_ledger  # noqa: E402
from ledgerlens.evaluate import score  # noqa: E402
from ledgerlens.fees import expected_fee  # noqa: E402
from ledgerlens.reconcile import names_match, reconcile  # noqa: E402
from ledgerlens.store import LedgerStore, QueryError  # noqa: E402


def frames(seed):
    ledger, bank, truth = build(seed)
    return load_ledger(pd.DataFrame(ledger)), load_bank(pd.DataFrame(bank)), truth


# ------------------------------------------------------------ fees

@pytest.mark.parametrize("amount,method,fee", [
    (100, "CARD", "3.20"),     # 2.9% + 0.30
    (100, "ACH", "0.80"),      # 0.8%
    (5000, "ACH", "5.00"),     # capped
    (2500, "WIRE", "15.00"),   # flat
])
def test_fee_schedule(amount, method, fee):
    assert str(expected_fee(amount, method)) == fee


def test_unknown_method_rejected():
    with pytest.raises(ValueError):
        expected_fee(10, "CHECK")


# ------------------------------------------------------------ matching

@pytest.mark.parametrize("a,b,ok", [
    ("Acme Supply Co.", "ACME SUPPLY CO", True),
    ("Cedar Ridge Property Group", "CEDAR RIDGE PROPER", True),   # truncated by bank
    ("Brightline Dental LLC", "ACME SUPPLY CO", False),
])
def test_names_match(a, b, ok):
    assert names_match(a, b) is ok


@pytest.mark.parametrize("seed", [1, 7, 42, 2026, 31337])
def test_engine_finds_every_injected_issue(seed):
    ledger, bank, truth = frames(seed)
    report = score(reconcile(ledger, bank).findings, truth)
    assert report["missed"] == []
    assert report["false_positives"] == []


def test_fee_error_is_not_double_reported_as_amount_mismatch():
    ledger, bank, truth = frames(7)
    result = reconcile(ledger, bank)
    fee_entries = {t["id"] for t in truth if t["type"] == "FEE_MISCALC"}
    amount_entries = {f.entry_ids[0] for f in result.findings if f.type == "AMOUNT_MISMATCH"}
    assert fee_entries.isdisjoint(amount_entries)


def test_missing_columns_give_clear_error():
    with pytest.raises(DataError, match="missing columns"):
        load_bank(pd.DataFrame({"txn_id": ["T-1"]}))


# ------------------------------------------------------------ SQL guard

@pytest.fixture
def store():
    ledger, bank, _ = frames(7)
    return LedgerStore(ledger, bank, reconcile(ledger, bank))


def test_select_works(store):
    out = store.query("SELECT COUNT(*) AS n FROM findings WHERE severity = 'high'")
    assert out["columns"] == ["n"] and out["rows"][0][0] > 0


@pytest.mark.parametrize("sql", [
    "DELETE FROM ledger",
    "UPDATE bank SET amount = 0",
    "DROP TABLE findings",
    "INSERT INTO matches VALUES ('x','y','z')",
    "SELECT 1; DELETE FROM ledger",
    "ATTACH DATABASE '/tmp/x.db' AS x",
])
def test_writes_are_blocked(store, sql):
    with pytest.raises(QueryError):
        store.query(sql)
    assert store.query("SELECT COUNT(*) FROM ledger")["rows"][0][0] > 0


# ------------------------------------------------------------ agent loop

class FakeClient:
    """Plays back scripted model turns so the tool loop can be tested offline."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        return self.turns.pop(0)


def turn(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(**b) for b in blocks])


def test_agent_runs_tools_and_returns_answer(store):
    ledger, bank, _ = frames(7)
    result = reconcile(ledger, bank)
    client = FakeClient([
        turn("tool_use", {"type": "tool_use", "id": "t1", "name": "list_findings",
                          "input": {"type": "DUPLICATE_PAYMENT"}}),
        turn("tool_use", {"type": "tool_use", "id": "t2", "name": "run_sql",
                          "input": {"query": "DELETE FROM ledger"}}),
        turn("end_turn", {"type": "text", "text": "Two deposits were duplicated: F-001 and F-004."}),
    ])
    agent = ReconciliationAgent(result, store, client=client)
    answer = agent.ask("Were any customers double-paid?")

    assert answer.text.startswith("Two deposits")
    assert [c["tool"] for c in answer.tool_calls] == ["list_findings", "run_sql"]
    assert answer.tool_calls[1]["error"] is True   # the write was refused
    first_result = client.calls[1][-1]["content"][0]
    assert first_result["tool_use_id"] == "t1"
    assert "DUPLICATE_PAYMENT" in first_result["content"]


def test_offline_summary_mentions_counts():
    ledger, bank, _ = frames(7)
    text = offline_summary(reconcile(ledger, bank))
    assert "Matched" in text and "Duplicate Payment: 2" in text
