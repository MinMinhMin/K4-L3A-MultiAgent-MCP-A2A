"""Phase 3/4 hard-gate regression tests: MCP Gateway usage discipline and the
deterministic policy application on top of it.

`FakeGateway` mirrors the *real* live L3A gateway's tool surface, confirmed by
live discovery: every specialist tool (order/item/payment/payment_timeline/
shipment/seller/product/refund_timeline) is keyed by `order_id`, only
`get_customer_history` (customer_unique_id) and `get_policy` (policy_version)
differ, and `get_policy` returns a rule table keyed by primary_issue.

These exercise `solve_case` end-to-end against that fake gateway and a fake
Policy LLM (no network, no real credentials) and assert the rules that carry
the harshest penalties in the competition rubric:

1. every MCP call carries the case being solved (`case_id`);
2. no `evidence_ref` in the final output is fabricated — every one traces back
   to a `tool_result_consumed` trace event recorded for this case;
3. evidence never leaks across cases;
4. only evidence that actually backs something in the output is cited;
5. financial resolution/responsible party/actions come from the authoritative
   MCP policy table, not a free-form LLM guess.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@dataclass
class FakeTool:
    name: str
    description: str
    input_schema: dict[str, Any]


def _evidence_ref(seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).hexdigest()[:32]
    return f"ev_{digest}"


ORDER_ID_TOOL = lambda name: FakeTool(  # noqa: E731
    name, "", {"required": ["case_id", "order_id"], "properties": {"case_id": {}, "order_id": {}}}
)


class FakeGateway:
    """Stand-in for EvidenceGateway shaped exactly like the live L3A gateway."""

    def __init__(
        self,
        orders: dict[str, dict[str, Any]],
        policy: dict[str, Any] | None = None,
    ) -> None:
        self._orders = orders
        self._policy = policy or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tool_specs(self) -> list[FakeTool]:
        return [
            ORDER_ID_TOOL("get_order"),
            ORDER_ID_TOOL("get_order_items"),
            ORDER_ID_TOOL("get_order_payments"),
            ORDER_ID_TOOL("get_shipment_summary"),
            ORDER_ID_TOOL("get_sellers"),
            ORDER_ID_TOOL("get_product_context"),
            ORDER_ID_TOOL("get_payment_timeline"),
            ORDER_ID_TOOL("get_refund_timeline"),
            FakeTool(
                "get_customer_history",
                "",
                {
                    "required": ["case_id", "customer_unique_id"],
                    "properties": {"case_id": {}, "customer_unique_id": {}},
                },
            ),
            FakeTool(
                "get_policy",
                "",
                {
                    "required": ["case_id", "policy_version"],
                    "properties": {"case_id": {}, "policy_version": {}},
                },
            ),
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if not case_id:
            raise RuntimeError("case_id is required")

        if tool_name == "get_policy":
            data = self._policy
        elif tool_name == "get_customer_history":
            raise RuntimeError(f"not found: {arguments}")
        else:
            order_id = arguments["order_id"]
            order = self._orders.get(order_id)
            if order is None:
                raise RuntimeError(f"order not found: {order_id}")
            if tool_name == "get_order":
                data = order["order"]
            elif tool_name == "get_order_items":
                data = order["items"]
            elif tool_name == "get_order_payments":
                data = order["payments"]
            elif tool_name == "get_sellers":
                data = order["sellers"]
            elif tool_name == "get_shipment_summary":
                data = order["shipment"]
            elif tool_name in ("get_product_context", "get_payment_timeline"):
                data = {}
            elif tool_name == "get_refund_timeline":
                raise RuntimeError(f"not found: refund history for {order_id}")
            else:
                data = {}

        domain = tool_name.removeprefix("get_")
        key = arguments.get("order_id") or arguments.get("policy_version") or "x"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": _evidence_ref(f"{case_id}:{tool_name}:{key}"),
            "result_hash": f"sha256:{hashlib.sha256(json.dumps(data).encode()).hexdigest()}",
            "domain": domain,
            "data": data,
        }


class FakePolicyLLM:
    def __init__(self, draft: dict[str, Any]) -> None:
        self._draft = draft

    async def draft_assessment(self, case_context: dict[str, Any]) -> dict[str, Any]:
        return self._draft


ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"
SELLER_ID = "seller-e2a03ccf5ea8"

ORDERS = {
    ORDER_ID: {
        "order": {
            "order_id": ORDER_ID,
            "customer_id": "customer-row-e2a03ccf5ea8",
            "order_status": "canceled",
        },
        "items": [
            {
                "order_id": ORDER_ID,
                "order_item_id": "item-1",
                "seller_id": SELLER_ID,
                "price": "79.00",
            }
        ],
        "payments": [{"order_id": ORDER_ID, "payment_sequential": "1", "payment_value": "79.00"}],
        "sellers": [{"seller_id": SELLER_ID, "seller_city": "sao_paulo"}],
        "shipment": {"order_id": ORDER_ID, "order_status": "canceled"},
    }
}

POLICY_DATA = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            # A template party id shared by every case on this policy version:
            # must NOT end up in the output, which must use this case's own seller.
            "responsible_parties": [
                {"party_id": "seller-TEMPLATE-DO-NOT-USE", "party_type": "seller"}
            ],
        }
    },
}

CASE = {
    "case_id": "L3A_CASE_900",
    "customer_request": {
        "claimed_order_id": ORDER_ID,
        "claims": [{"claim_id": "claim-a", "topic": "canceled_order_paid"}],
    },
    "policy_version": "EC_POLICY_V1",
}

DRAFT = {
    "primary_issue": "canceled_order_paid",
    "case_status": "action_required",
    "confidence": 0.9,
    "claim_assessments": [
        {
            "claim_id": "claim-a",
            "verdict": "supported",
            "confidence": 0.8,
            "supporting_domains": ["order", "payment"],
        }
    ],
    "ranked_causes": [{"cause_code": "SELLER_CANCELED_AFTER_PAYMENT", "rank": 1}],
    # Deliberately wrong/free-form guesses: the deterministic policy path must
    # override these, not trust them.
    "responsible_parties": [{"party_type": "platform", "party_id_domain": None}],
    "data_conflicts": [],
    "financial_resolution": {
        "recommended_refund_brl": 999,
        "refund_lines": [{"reason_code": "made_up", "amount_brl": 999, "entity_domain": "payment"}],
    },
    "resolution_actions": ["made_up_action"],
}


def _run(gateway: FakeGateway, tmp_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(CASE, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "test output")
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    return output, events


def test_solve_case_never_fabricates_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("student_agent.agents.get_policy_llm", lambda: FakePolicyLLM(DRAFT))
    gateway = FakeGateway(ORDERS, POLICY_DATA)

    output, events = _run(gateway, tmp_path)

    # Rule 1: every call this case made was tagged with this case's own case_id.
    assert all(args["case_id"] == "L3A_CASE_900" for _, args in gateway.calls)

    consumed_refs = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event.get("evidence_refs", [])
    }

    # Rule 2: no evidence_ref in the output is invented — all trace back to a
    # tool_result_consumed event actually recorded for this case.
    assert set(output["evidence_refs"]) <= consumed_refs
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= consumed_refs

    # Rule 3 (sanity): the order tool was called with the claimed order id, not
    # a guessed/cross-case id, and every specialist call shares that order_id.
    order_calls = [args for name, args in gateway.calls if name == "get_order"]
    assert order_calls == [{"case_id": "L3A_CASE_900", "order_id": ORDER_ID}]
    assert any(
        name == "get_order_items" and args["order_id"] == ORDER_ID for name, args in gateway.calls
    )
    assert any(
        name == "get_sellers" and args["order_id"] == ORDER_ID for name, args in gateway.calls
    )


def test_financial_resolution_comes_from_policy_not_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 4: the deterministic get_policy rule must win over the LLM draft's
    made-up refund amount, action and (especially) its cross-case template
    party id, while still resolving the party id from *this* case's evidence.
    """
    monkeypatch.setattr("student_agent.agents.get_policy_llm", lambda: FakePolicyLLM(DRAFT))
    gateway = FakeGateway(ORDERS, POLICY_DATA)

    output, _ = _run(gateway, tmp_path)

    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    # The refund is owed on the order itself, so the line is keyed by the
    # verified order id rather than by whichever party is responsible.
    assert output["financial_resolution"]["refund_lines"] == [
        {"reason_code": "issue_refund", "amount_brl": 79.0, "entity_id": ORDER_ID}
    ]
    assert output["resolution_actions"] == ["issue_refund"]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER_ID}
    ]


def test_lifecycle_events_follow_required_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 4: trace must show case_received -> task_assigned -> tool_result_consumed
    -> handoff -> policy_decided -> verification_completed -> case_finalized, in that
    relative order (mirrors what cli.py emits around solve_case)."""
    monkeypatch.setattr("student_agent.agents.get_policy_llm", lambda: FakePolicyLLM(DRAFT))

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(ORDERS, POLICY_DATA)

    trace.emit(case_id=CASE["case_id"], event_type="case_received", actor="coordinator")
    asyncio.run(solve_case(CASE, gateway, trace))  # type: ignore[arg-type]
    trace.emit(case_id=CASE["case_id"], event_type="case_finalized", actor="coordinator")

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    sequence = [event["event_type"] for event in events]

    required = [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    ]
    positions = [sequence.index(event_type) for event_type in required]
    assert positions == sorted(positions), f"lifecycle order violated: {sequence}"


def test_missing_order_never_invents_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "student_agent.agents.get_policy_llm",
        lambda: (_ for _ in ()).throw(
            AssertionError("LLM must not be called without verified evidence")
        ),
    )
    gateway = FakeGateway({}, POLICY_DATA)  # no orders at all: claimed_order_id will not resolve

    output, _ = _run(gateway, tmp_path)

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == []
    assert all(claim["evidence_refs"] == [] for claim in output["claim_assessments"])
