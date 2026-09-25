from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .evidence import EvidenceLedger, collect_tool
from .mcp_gateway import EvidenceGateway
from .policy import Decision, analyze_case
from .trace import TraceWriter

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")


def _money_number(value: Decimal) -> float:
    return float(value)


def _decision_output(
    case_id: str,
    decision: Decision,
    claims_present: bool,
) -> dict[str, Any]:
    responsible_parties = [
        {"party_type": party_type, "party_id": party_id}
        for party_type, party_id in decision.responsible_parties
    ]
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": list(decision.order_ids),
            "item_ids": list(decision.item_ids),
            "seller_ids": list(decision.seller_ids),
            "payment_references": list(decision.payment_references),
            "shipment_ids": list(decision.shipment_ids),
        },
        "root_cause_analysis": {
            "ranked_causes": [dict(cause) for cause in decision.ranked_causes],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": list(dict.fromkeys(decision.evidence_refs)),
        "data_conflicts": [dict(conflict) for conflict in decision.data_conflicts],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money_number(decision.refund_total),
            "refund_lines": [
                {
                    "reason_code": line["reason_code"],
                    "amount_brl": _money_number(line["amount_brl"]),
                    "entity_id": line["entity_id"],
                }
                for line in decision.refund_lines
            ],
        },
        "resolution_actions": list(dict.fromkeys(decision.resolution_actions)),
    }
    if claims_present:
        output["claim_assessments"] = [
            dict(assessment) for assessment in decision.claim_assessments
        ]
    return output


def _verify_output(output: dict[str, Any], ledger: EvidenceLedger) -> None:
    refs = output["evidence_refs"]
    if len(refs) != len(set(refs)):
        raise ValueError("workflow produced duplicate evidence references")
    ledger_refs = set(ledger.refs())
    if not set(refs).issubset(ledger_refs):
        raise ValueError("workflow produced an evidence reference outside the case ledger")
    for assessment in output.get("claim_assessments", []):
        if not set(assessment["evidence_refs"]).issubset(set(refs)):
            raise ValueError("claim evidence is not a subset of top-level evidence")
    resolution = output["financial_resolution"]
    total = Decimal(str(resolution["recommended_refund_brl"]))
    line_total = sum(
        (Decimal(str(line["amount_brl"])) for line in resolution["refund_lines"]),
        Decimal("0"),
    )
    if line_total != total:
        raise ValueError("refund lines do not sum to recommended refund")
    if total < 0:
        raise ValueError("recommended refund cannot be negative")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate scoped specialist calls and return one verified L3A output."""

    case_id = case.get("case_id")
    request = case.get("customer_request")
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError("case must contain a valid case_id")
    if not isinstance(request, dict):
        raise ValueError(f"{case_id}: customer_request is missing")
    order_id = request.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError(f"{case_id}: customer_request.claimed_order_id is missing")
    policy_version = case.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError(f"{case_id}: policy_version is missing")

    available = set(await gateway.list_tools())
    ledger = EvidenceLedger()
    specialist_scopes = (
        (
            "order-agent",
            "order",
            {"get_order", "get_order_items", "get_sellers", "get_product_context"},
        ),
        (
            "payment-agent",
            "payment",
            {"get_order_payments", "get_payment_timeline", "get_refund_timeline"},
        ),
        ("shipment-agent", "shipment", {"get_shipment_summary"}),
    )
    for actor, scope, tools in specialist_scopes:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"scope": scope, "available_tool_count": len(tools & available)},
        )

    specs: list[tuple[str, str, str, dict[str, str]]] = [
        ("order-agent", "get_order", "order", {"order_id": order_id}),
        ("order-agent", "get_order_items", "item", {"order_id": order_id}),
        ("order-agent", "get_sellers", "seller", {"order_id": order_id}),
        ("order-agent", "get_product_context", "product", {"order_id": order_id}),
        ("payment-agent", "get_order_payments", "payment", {"order_id": order_id}),
        ("payment-agent", "get_payment_timeline", "payment", {"order_id": order_id}),
        ("payment-agent", "get_refund_timeline", "refund", {"order_id": order_id}),
        ("shipment-agent", "get_shipment_summary", "shipment", {"order_id": order_id}),
        ("policy-agent", "get_policy", "policy", {"policy_version": policy_version}),
    ]
    work = [
        collect_tool(
            gateway,
            ledger,
            tool_name=tool_name,
            actor=actor,
            case_id=case_id,
            arguments=arguments,
            trace=trace,
        )
        for actor, tool_name, _domain, arguments in specs
        if tool_name in available
    ]
    await asyncio.gather(*work)

    refs = list(ledger.refs())
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        evidence_refs=refs,
        attributes={"evidence_count": len(refs)},
    )
    decision = analyze_case(case, ledger)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=decision.primary_issue,
        evidence_refs=list(decision.evidence_refs),
        attributes={"confidence": decision.confidence},
    )

    claims = request.get("claims", [])
    claims_present = isinstance(claims, list) and bool(claims)
    output = _decision_output(case_id, decision, claims_present)
    _verify_output(output, ledger)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="degraded" if decision.primary_issue == "insufficient_evidence" else "passed",
        evidence_refs=output["evidence_refs"],
        attributes={
            "confidence": decision.confidence,
            "evidence_count": len(output["evidence_refs"]),
        },
    )
    return output
