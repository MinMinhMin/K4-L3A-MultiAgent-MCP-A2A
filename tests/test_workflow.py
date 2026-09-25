from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

TOOL_NAMES = [
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_order_payments",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_sellers",
    "get_shipment_summary",
]


def case_fixture() -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "claimed_order_id": "o-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "canceled_order_paid"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def envelope(ref_number: int, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{ref_number:020d}",
        "result_hash": "sha256:" + f"{ref_number:064x}"[-64:],
        "domain": domain,
        "data": data,
    }


def evidence_for_tool(tool_name: str, ref_number: int) -> dict[str, Any]:
    data_by_tool = {
        "get_order": {
            "order_id": "o-1",
            "order_status": "canceled",
            "order_amount": "42.50",
        },
        "get_order_items": {
            "items": [{"order_item_id": "i-1", "seller_id": "s-1", "price": "42.50"}]
        },
        "get_order_payments": {
            "payments": [
                {"payment_reference": "p-1", "payment_value": "42.50", "payment_status": "paid"}
            ]
        },
        "get_payment_timeline": {"events": [{"status": "paid"}]},
        "get_refund_timeline": {"events": []},
        "get_sellers": {"sellers": [{"seller_id": "s-1"}]},
        "get_shipment_summary": {"shipment_id": "sh-1", "events": []},
        "get_product_context": {"products": [{"product_id": "prod-1"}]},
        "get_policy": {"policy_version": "EC_POLICY_V1", "rules": []},
    }
    domain_by_tool = {
        "get_order": "order",
        "get_order_items": "item",
        "get_order_payments": "payment",
        "get_payment_timeline": "payment",
        "get_refund_timeline": "refund",
        "get_sellers": "seller",
        "get_shipment_summary": "shipment",
        "get_product_context": "product",
        "get_policy": "policy",
    }
    return envelope(ref_number, domain_by_tool[tool_name], data_by_tool[tool_name])


class FakeGateway:
    def __init__(
        self,
        *,
        fail_once: set[str] | None = None,
        permanent_fail: set[str] | None = None,
    ) -> None:
        self.fail_once = set(fail_once or ())
        self.permanent_fail = set(permanent_fail or ())
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self._next_ref = 1

    async def list_tools(self) -> list[str]:
        return TOOL_NAMES

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        if tool_name in self.permanent_fail:
            raise RuntimeError("not found")
        if tool_name in self.fail_once:
            self.fail_once.remove(tool_name)
            raise TimeoutError("timeout")
        result = evidence_for_tool(tool_name, self._next_ref)
        self._next_ref += 1
        return result


def trace_writer(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))


def trace_events(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_solve_case_emits_scoped_specialists_and_valid_output(tmp_path: Path) -> None:
    gateway = FakeGateway()
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    output = asyncio.run(solve_case(case_fixture(), gateway, trace_writer(tmp_path)))

    contracts.validate_output(output, "workflow output")
    assert output["case_id"] == "L3A_CASE_001"
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["evidence_refs"]
    assert all(call[1] == "L3A_CASE_001" for call in gateway.calls)
    events = trace_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert event_types[:3] == ["task_assigned", "task_assigned", "task_assigned"]
    assert event_types[-3:] == ["handoff", "policy_decided", "verification_completed"]
    consumed_refs = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event.get("evidence_refs", [])
    }
    assert set(output["evidence_refs"]).issubset(consumed_refs)
    assert event_types[3:-3].count("tool_result_consumed") == len(consumed_refs)


def test_solve_case_retries_transient_failure_with_case_scope(tmp_path: Path) -> None:
    gateway = FakeGateway(fail_once={"get_order"})

    output = asyncio.run(solve_case(case_fixture(), gateway, trace_writer(tmp_path)))

    order_calls = [call for call in gateway.calls if call[0] == "get_order"]
    assert len(order_calls) == 2
    assert all(call[1] == "L3A_CASE_001" for call in order_calls)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"


def test_solve_case_degrades_without_fabricating_when_order_fails(tmp_path: Path) -> None:
    gateway = FakeGateway(permanent_fail={"get_order"})

    output = asyncio.run(solve_case(case_fixture(), gateway, trace_writer(tmp_path)))

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"]
    assert all(ref.startswith("ev_") for ref in output["evidence_refs"])
    assert all(
        ref in output["evidence_refs"]
        for claim in output["claim_assessments"]
        for ref in claim["evidence_refs"]
    )


def test_solve_case_refund_lines_sum_to_recommended_refund(tmp_path: Path) -> None:
    gateway = FakeGateway()

    output = asyncio.run(solve_case(case_fixture(), gateway, trace_writer(tmp_path)))

    resolution = output["financial_resolution"]
    assert sum(line["amount_brl"] for line in resolution["refund_lines"]) == resolution[
        "recommended_refund_brl"
    ]
