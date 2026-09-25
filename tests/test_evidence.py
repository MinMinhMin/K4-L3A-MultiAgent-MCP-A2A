from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger, collect_tool
from student_agent.trace import TraceWriter


def valid_evidence(evidence_ref: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": evidence_ref,
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
    }


class SequenceGateway:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_collect_tool_retries_timeout_with_same_case_scope(
    tmp_path: Path, contracts: Contracts
) -> None:
    evidence = valid_evidence("ev_abcdefghijklmnopqrst", "order", {"order_id": "o-1"})
    gateway = SequenceGateway([TimeoutError("timeout"), evidence])
    ledger = EvidenceLedger()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    record = asyncio.run(
        collect_tool(
            gateway,
            ledger,
            tool_name="get_order",
            actor="order-agent",
            case_id="CASE_001",
            arguments={"order_id": "o-1"},
            trace=trace,
        )
    )

    assert record is not None
    assert record.evidence_ref == "ev_abcdefghijklmnopqrst"
    assert gateway.calls == [
        ("get_order", "CASE_001", {"order_id": "o-1"}),
        ("get_order", "CASE_001", {"order_id": "o-1"}),
    ]
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert events[0]["event_type"] == "tool_result_consumed"
    assert events[0]["evidence_refs"] == ["ev_abcdefghijklmnopqrst"]


def test_collect_tool_does_not_fabricate_after_permanent_failure(
    tmp_path: Path, contracts: Contracts
) -> None:
    gateway = SequenceGateway([RuntimeError("not found")])
    ledger = EvidenceLedger()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    record = asyncio.run(
        collect_tool(
            gateway,
            ledger,
            tool_name="get_order",
            actor="order-agent",
            case_id="CASE_001",
            arguments={"order_id": "o-1"},
            trace=trace,
        )
    )

    assert record is None
    assert gateway.calls == [("get_order", "CASE_001", {"order_id": "o-1"})]
    assert ledger.refs() == ()
    trace_path = tmp_path / "trace.jsonl"
    assert not trace_path.exists() or not trace_path.read_text()


def test_evidence_ledger_deduplicates_refs(contracts: Contracts) -> None:
    record = valid_evidence("ev_abcdefghijklmnopqrst", "order", {"order_id": "o-1"})
    ledger = EvidenceLedger()
    ledger.add_record(
        tool_name="get_order",
        actor="order-agent",
        evidence=record,
        contracts=contracts,
    )
    ledger.add_record(
        tool_name="get_order",
        actor="order-agent",
        evidence=record,
        contracts=contracts,
    )

    assert ledger.refs() == ("ev_abcdefghijklmnopqrst",)
    assert len(ledger.by_domain("order")) == 1
