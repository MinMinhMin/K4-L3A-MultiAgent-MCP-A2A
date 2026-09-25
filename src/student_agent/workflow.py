from __future__ import annotations

import asyncio
from typing import Any

from .agents import (
    DomainEvidence,
    draft_policy_assessment,
    gather_customer_evidence,
    gather_order_evidence,
    gather_order_scoped_evidence,
    gather_policy_evidence,
    verify_and_finalize,
)
from .facts import build_case_facts
from .mcp_gateway import EvidenceGateway
from .tool_catalog import ToolCatalog
from .trace import TraceWriter

# Every one of these is fetched with the same verified order_id (see agents.py
# module docstring for why: the live gateway keys all of them by order_id).
# None of them depends on another's result, so they are fetched concurrently
# once the order is verified — the MCP round-trip, not local CPU work, is the
# pipeline's bottleneck, and these calls have no ordering requirement between
# them (only "the order must be verified first" and "case_id must be correct",
# both already satisfied before this fan-out starts).
ORDER_SCOPED_DOMAINS = (
    "item",
    "seller",
    "product",
    "payment",
    "payment_timeline",
    "shipment",
    "refund_timeline",
)

DOMAIN_ACTOR: dict[str, str] = {
    "item": "order-agent",
    "seller": "order-agent",
    "product": "order-agent",
    "payment": "payment-agent",
    "payment_timeline": "payment-agent",
    "refund_timeline": "payment-agent",
    "shipment": "shipment-agent",
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: hands the case off to specialist agents, then to the Policy
    Agent (OpenAI-backed reasoning over verified evidence, cross-checked against
    the authoritative MCP policy table) and the Verifier Agent, which
    reconciles both against real evidence before this function returns a
    contract-valid output. See ARCHITECTURE.md for the handoff/failure design.
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", [])
    claimed_order_id = customer_request.get("claimed_order_id")
    policy_version = case.get("policy_version")

    tool_specs = await gateway.list_tool_specs()
    catalog = ToolCatalog(tool_specs)

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent"
    )
    order_evidence = await gather_order_evidence(gateway, catalog, trace, case_id, claimed_order_id)

    evidence_by_domain: dict[str, list[DomainEvidence]] = {
        "order": [order_evidence] if order_evidence else [],
    }
    for domain in ORDER_SCOPED_DOMAINS:
        evidence_by_domain[domain] = []
    evidence_by_domain["customer"] = []
    evidence_by_domain["policy"] = []

    if order_evidence is not None:
        order_id = order_evidence.entity_id
        assert order_id is not None

        # Fan out the handoff to every specialist that depends only on the now-
        # verified order_id, then run their MCP calls concurrently instead of
        # awaiting them one at a time.
        trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="order-agent")
        trace.emit(
            case_id=case_id, event_type="handoff", actor="coordinator", target="payment-agent"
        )
        trace.emit(
            case_id=case_id, event_type="handoff", actor="coordinator", target="shipment-agent"
        )

        domains = list(ORDER_SCOPED_DOMAINS)
        results = await asyncio.gather(
            *(
                gather_order_scoped_evidence(
                    gateway, catalog, trace, case_id, DOMAIN_ACTOR[domain], domain, order_id
                )
                for domain in domains
            ),
            gather_customer_evidence(gateway, catalog, trace, case_id, order_evidence.data),
        )
        for domain, evidence in zip(domains, results[:-1], strict=True):
            evidence_by_domain[domain] = [evidence] if evidence else []
        customer_evidence = results[-1]
        evidence_by_domain["customer"] = [customer_evidence] if customer_evidence else []

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="policy-agent")
    policy_evidence = await gather_policy_evidence(gateway, catalog, trace, case_id, policy_version)
    evidence_by_domain["policy"] = [policy_evidence] if policy_evidence else []

    facts = build_case_facts(evidence_by_domain)
    draft = await draft_policy_assessment(case, evidence_by_domain, facts)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=str((draft or {}).get("primary_issue", "insufficient_evidence"))[:80],
    )

    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier-agent")
    output = verify_and_finalize(case_id, claims, evidence_by_domain, draft, facts)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code=output["assessment"]["primary_issue"],
    )
    return output
