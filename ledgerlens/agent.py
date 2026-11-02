"""Claude-powered analyst that answers questions about a reconciliation.

The agent gets three tools: list findings, open one finding, and run a
read-only SQL query. It is told to quote only numbers it got from a tool,
and every tool call is recorded so the UI can show its work.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .reconcile import ReconciliationResult
from .store import SCHEMA_DOC, LedgerStore, QueryError

DEFAULT_MODEL = os.environ.get("LEDGERLENS_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8

SYSTEM_PROMPT = f"""You are a payments reconciliation analyst helping an accounting team.
A deterministic engine has already matched the internal ledger to the bank feed and produced
findings. Your job is to explain them and answer questions in plain business English.

Rules:
- Only state amounts, dates and IDs that came from a tool result in this conversation.
- Cite finding IDs (e.g. F-004) and entry/transaction IDs so a person can verify you.
- If the data cannot answer the question, say so and suggest what record would.
- Recommend a concrete next step for each problem (who to contact, what to correct).
- Keep answers short: lead with the answer, then the evidence.

{SCHEMA_DOC}"""

TOOLS = [
    {
        "name": "list_findings",
        "description": "List reconciliation findings, optionally filtered by type or severity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": [
                    "DUPLICATE_PAYMENT", "UNMATCHED_BANK", "UNMATCHED_LEDGER", "AMOUNT_MISMATCH",
                    "FEE_MISCALC", "SPLIT_TOTAL_MISMATCH", "PAYEE_MISMATCH"]},
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            },
        },
    },
    {
        "name": "get_finding",
        "description": "Get one finding with its full evidence.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Finding ID such as F-003"}},
            "required": ["id"],
        },
    },
    {
        "name": "run_sql",
        "description": "Run one read-only SQLite SELECT against ledger, bank, matches and findings. "
                       "Returns at most 200 rows.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


@dataclass
class AgentAnswer:
    text: str
    tool_calls: list[dict] = field(default_factory=list)


class ReconciliationAgent:
    def __init__(self, result: ReconciliationResult, store: LedgerStore,
                 client: Any = None, model: str = DEFAULT_MODEL):
        self.result = result
        self.store = store
        self.model = model
        if client is None:
            import anthropic  # imported lazily so offline mode needs no API package
            client = anthropic.Anthropic()
        self.client = client
        self._by_id = {f.id: f for f in result.findings}

    # ----------------------------------------------------------- tools
    def _run_tool(self, name: str, args: dict) -> tuple[Any, bool]:
        if name == "list_findings":
            items = [
                {k: v for k, v in f.to_dict().items() if k != "evidence"}
                for f in self.result.findings
                if (not args.get("type") or f.type == args["type"])
                and (not args.get("severity") or f.severity == args["severity"])
            ]
            return {"stats": self.result.stats, "findings": items}, False
        if name == "get_finding":
            f = self._by_id.get(str(args.get("id", "")).upper())
            return (f.to_dict(), False) if f else (f"No finding with id {args.get('id')!r}", True)
        if name == "run_sql":
            try:
                return self.store.query(args.get("query", "")), False
            except QueryError as exc:
                return f"Query rejected: {exc}", True
        return f"Unknown tool {name!r}", True

    # ----------------------------------------------------------- loop
    def ask(self, question: str, history: list[dict] | None = None) -> AgentAnswer:
        messages = list(history or []) + [{"role": "user", "content": question}]
        calls: list[dict] = []
        for _ in range(MAX_STEPS):
            response = self.client.messages.create(
                model=self.model, max_tokens=1500, system=SYSTEM_PROMPT,
                tools=TOOLS, messages=messages,
            )
            blocks = [_block_to_dict(b) for b in response.content]
            messages.append({"role": "assistant", "content": blocks})
            if response.stop_reason != "tool_use":
                text = "\n".join(b["text"] for b in blocks if b["type"] == "text").strip()
                return AgentAnswer(text=text, tool_calls=calls)
            results = []
            for b in blocks:
                if b["type"] != "tool_use":
                    continue
                output, is_error = self._run_tool(b["name"], b["input"])
                calls.append({"tool": b["name"], "input": b["input"], "error": is_error})
                results.append({
                    "type": "tool_result", "tool_use_id": b["id"], "is_error": is_error,
                    "content": output if isinstance(output, str) else json.dumps(output, default=str),
                })
            messages.append({"role": "user", "content": results})
        return AgentAnswer(text="I stopped after too many steps without a final answer. "
                                "Try a narrower question.", tool_calls=calls)


def _block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": block.type}


def offline_summary(result: ReconciliationResult) -> str:
    """A deterministic briefing used when no API key is configured."""
    s = result.stats
    counts = Counter(f.type for f in result.findings)
    sev = Counter(f.severity for f in result.findings)
    lines = [
        f"Matched {s['matched']} of {s['ledger_entries']} ledger entries to the bank feed "
        f"({s['matched_by_heuristic']} matched without a reference, {s['in_transit']} still in transit).",
        f"{s['findings']} findings flag ${s['dollars_flagged']:,.2f}: "
        f"{sev['high']} high, {sev['medium']} medium, {sev['low']} low.",
        "",
    ]
    lines += [f"- {kind.replace('_', ' ').title()}: {n}" for kind, n in counts.most_common()]
    high = [f for f in result.findings if f.severity == "high"][:5]
    if high:
        lines += ["", "Start with:"] + [f"- {f.id}: {f.summary}" for f in high]
    return "\n".join(lines)
