"""Streamlit front end.  Run with:  streamlit run app.py"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from ledgerlens.agent import ReconciliationAgent, offline_summary
from ledgerlens.data import DataError, load_bank, load_ledger
from ledgerlens.reconcile import reconcile
from ledgerlens.store import LedgerStore

SAMPLE = Path(__file__).parent / "data" / "sample"
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}

st.set_page_config(page_title="LedgerLens", page_icon="🔎", layout="wide")


def api_key() -> str | None:
    try:
        return st.secrets.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    except Exception:
        return os.environ.get("ANTHROPIC_API_KEY")


@st.cache_data(show_spinner=False)
def run(ledger_csv: bytes | None, bank_csv: bytes | None):
    import io
    ledger = load_ledger(io.BytesIO(ledger_csv) if ledger_csv else SAMPLE / "ledger_entries.csv")
    bank = load_bank(io.BytesIO(bank_csv) if bank_csv else SAMPLE / "bank_transactions.csv")
    return ledger, bank, reconcile(ledger, bank)


st.title("LedgerLens")
st.caption("Reconcile a payments ledger against the bank feed, then ask an AI analyst what went wrong.")

with st.sidebar:
    st.header("Data")
    ledger_file = st.file_uploader("Ledger CSV", type="csv")
    bank_file = st.file_uploader("Bank feed CSV", type="csv")
    if not (ledger_file and bank_file):
        st.info("Using the built-in sample month. Upload both files to use your own.")
    st.markdown("Column formats are documented in the README.")

try:
    ledger, bank, result = run(
        ledger_file.getvalue() if ledger_file and bank_file else None,
        bank_file.getvalue() if ledger_file and bank_file else None,
    )
except DataError as exc:
    st.error(f"Could not read the files: {exc}")
    st.stop()

s = result.stats
c1, c2, c3, c4 = st.columns(4)
c1.metric("Matched", f"{s['matched']} / {s['ledger_entries']}")
c2.metric("Findings", s["findings"])
c3.metric("Dollars flagged", f"${s['dollars_flagged']:,.2f}")
c4.metric("Matched without a reference", s["matched_by_heuristic"])

tab_findings, tab_ask, tab_data = st.tabs(["Findings", "Ask the analyst", "Raw data"])

with tab_findings:
    frame = result.findings_frame()
    if frame.empty:
        st.success("Everything reconciles.")
    else:
        types = st.multiselect("Filter by type", sorted(frame["type"].unique()))
        shown = frame[frame["type"].isin(types)] if types else frame
        shown = shown.assign(severity=shown["severity"].map(lambda v: f"{SEVERITY_ICON[v]} {v}"))
        st.dataframe(shown, hide_index=True, use_container_width=True,
                     column_config={"amount_impact": st.column_config.NumberColumn(format="$%.2f")})
        with st.expander("Plain-English briefing"):
            st.text(offline_summary(result))

with tab_ask:
    key = api_key()
    if not key:
        st.warning("Set ANTHROPIC_API_KEY (environment or Streamlit secrets) to chat with the analyst.")
    else:
        os.environ["ANTHROPIC_API_KEY"] = key
        st.session_state.setdefault("chat", [])
        for msg in st.session_state.chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
        if not st.session_state.chat:
            st.caption("Try: *Which problems should I fix first?* · *Why is PMT-1038 short?* · "
                       "*How much did fee errors cost us?*")
        question = st.chat_input("Ask about this month's reconciliation")
        if question:
            st.session_state.chat.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"), st.spinner("Checking the records..."):
                agent = ReconciliationAgent(result, LedgerStore(ledger, bank, result))
                history = st.session_state.chat[:-1]
                answer = agent.ask(question, history=history)
                st.markdown(answer.text)
                if answer.tool_calls:
                    with st.expander(f"How I checked ({len(answer.tool_calls)} lookups)"):
                        for call in answer.tool_calls:
                            st.code(f"{call['tool']}({call['input']})", language="text")
            st.session_state.chat.append({"role": "assistant", "content": answer.text})

with tab_data:
    left, right = st.columns(2)
    left.subheader("Ledger")
    left.dataframe(ledger, hide_index=True, use_container_width=True)
    right.subheader("Bank feed")
    right.dataframe(bank.drop(columns=["ref"], errors="ignore"), hide_index=True,
                    use_container_width=True)
