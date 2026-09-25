"""Specialist agents, evidence aggregation and verification for the L3A workflow.

Coordinator role lives in `workflow.solve_case`; this module holds the
Order/Item, Payment, Shipment and Policy specialists plus the Verifier that
turns their findings into a contract-valid output without ever inventing an
evidence_ref, entity ID or unsupported claim.

Live-gateway discovery shows every specialist tool (order/item/payment/
payment_timeline/shipment/seller/product/refund_timeline) is keyed by the same
`order_id` the customer named — none of them take a separate child id. Only
`get_customer_history` (customer_unique_id) and `get_policy` (policy_version)
differ. Evidence gathering below reflects that: one call per domain per case,
all against the same verified order_id.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx2

from .facts import build_case_facts, late_handover_sellers
from .llm import HYPOTHESIS_ASSESSMENTS, POLICY_ISSUES, PRIMARY_ISSUES, get_policy_llm
from .mcp_gateway import EvidenceGateway
from .tool_catalog import ID_KEY_HINTS, ToolCatalog, extract_ids, primary_id_kwarg
from .trace import TraceWriter

RETRY_BACKOFF_SECONDS = (0.5, 1.5)
# Transport hiccups (a dropped connection, a DNS blip) are transient and worth
# three attempts. A tool that *answered* with an error (RuntimeError/ValueError
# from gateway.call) is usually deterministic — e.g. no refund history for this
# order — so it gets one retry, not three: extra attempts only add latency and
# audited calls without changing the answer.
TRANSPORT_ERRORS = (OSError, httpx2.HTTPError)
TOOL_ERRORS = (RuntimeError, ValueError)
MAX_TRANSPORT_ATTEMPTS = 3
MAX_TOOL_ERROR_ATTEMPTS = 2
CAUSE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")

VALID_PARTY_TYPES = {
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
}

# The customer's complaint is not upheld for these outcomes: nothing is at
# fault, so no refund and the claim itself is "unsupported".
NO_FAULT_ISSUES = {"unsupported_claim", "valid_split_payment"}

# Which evidence domains establish (or refute) each issue. Citations are drawn
# from these sets so every case cites the evidence groups its conclusion
# actually rests on — not just whatever one or two domains the LLM happened to
# name — and never unrelated domains (customer history, product catalogue).
ISSUE_EVIDENCE_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment", "payment_timeline"),
    "unavailable_order_paid": ("order", "item", "payment", "payment_timeline"),
    "late_delivery_seller": ("order", "shipment", "item", "seller"),
    "late_delivery_logistics": ("order", "shipment", "item"),
    "duplicate_charge": ("order", "payment", "payment_timeline"),
    "valid_split_payment": ("order", "payment", "payment_timeline"),
    "payment_mismatch": ("order", "item", "payment", "payment_timeline"),
    "refund_pending": ("order", "payment_timeline", "refund_timeline"),
    "refund_failed": ("order", "payment_timeline", "refund_timeline"),
    "unsupported_claim": ("order", "shipment", "payment"),
    "insufficient_evidence": ("order",),
}
REFUND_CLAIM_TOPIC = "requested_full_refund"
REFUND_CLAIM_DOMAINS = ("payment", "payment_timeline")
# How far the policy action applied grants a customer's "refund me in full"
# request. Actions not listed (reconcile, retry, monitor) leave the verdict to
# the Policy Agent, since the rule alone doesn't say.
REFUND_ACTION_VERDICT: dict[str, str] = {
    "issue_refund": "supported",
    "refund_freight": "partially_supported",
    "refund_duplicate_charge": "partially_supported",
    "document_no_action": "unsupported",
}

# Which known-id bucket (see tool_catalog.ID_KEY_HINTS) identifies an entity in
# each evidence domain, used to resolve a domain pointer to a real id without
# ever inventing one.
DOMAIN_ID_KEY: dict[str, str] = {
    "order": "order_id",
    "item": "item_id",
    "seller": "seller_id",
    "payment": "payment_id",
    "shipment": "shipment_id",
    "product": "product_id",
    "customer": "customer_id",
}

# Which domain actually carries a given responsible-party type's identifier.
PARTY_TYPE_DOMAIN: dict[str, str] = {"seller": "seller", "customer": "order"}

# Cross-field consistency (LLM-fallback path only, when no policy rule
# applies): which responsible-party types are even plausible for a given
# primary_issue. A logistics delay cannot be the payment provider's fault.
PRIMARY_ISSUE_PARTY_RULES: dict[str, set[str]] = {
    "late_delivery_seller": {"seller"},
    "late_delivery_logistics": {"logistics_provider"},
    "canceled_order_paid": {"seller", "platform"},
    "unavailable_order_paid": {"seller", "platform"},
    "payment_mismatch": {"payment_provider", "platform"},
    "duplicate_charge": {"payment_provider", "platform"},
    "refund_pending": {"payment_provider", "platform"},
    "refund_failed": {"payment_provider", "platform"},
    "valid_split_payment": {"customer", "unknown"},
    "unsupported_claim": {"customer", "unknown"},
    "insufficient_evidence": {"unknown"},
}

# Issues that describe an unresolved/unconfirmed situation: no refund should
# be attached to them and case_status must reflect "nothing decided yet".
NO_FINANCIAL_ACTION_ISSUES = {"insufficient_evidence", "unsupported_claim"}

# Confidence is the probability primary_issue is correct, so it is set by *how*
# the issue was established rather than by the LLM's self-report, which is
# poorly calibrated: an issue the customer claimed and the evidence confirmed
# is very likely right; overturning the customer's framing or giving up on a
# decision is much less certain.
BASIS_CONFIDENCE: dict[str, float] = {
    "claim_confirmed": 0.9,
    "claim_contradicted": 0.6,
    "other_confirmed": 0.55,
    "insufficient": 0.25,
}


@dataclass
class DomainEvidence:
    domain: str
    evidence_ref: str
    data: Any
    entity_id: str | None = None


class EvidenceUnavailableError(RuntimeError):
    """Evidence a case cannot be decided without failed for a reason other than
    a definite "not found" — typically the gateway itself failing. The case must
    be retried later, not written up as insufficient_evidence: during an outage
    that would turn every case into a confident-looking but empty output."""


async def _call_with_retry(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    *,
    required: bool = False,
    **arguments: str,
) -> dict[str, Any] | None:
    last_error: Exception | None = None
    attempt = 0
    while True:
        attempt += 1
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
        except TRANSPORT_ERRORS + TOOL_ERRORS as exc:
            last_error = exc
            if "not found" in str(exc).lower():
                break  # not-found is not retried: it is a real, idempotent answer
            limit = (
                MAX_TRANSPORT_ATTEMPTS
                if isinstance(exc, TRANSPORT_ERRORS)
                else MAX_TOOL_ERROR_ATTEMPTS
            )
            if attempt >= limit:
                break
            await asyncio.sleep(
                RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            )
            continue
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
        )
        return evidence
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        decision_code="EVIDENCE_UNAVAILABLE",
        attributes={"error": str(last_error)[:160] if last_error else "unknown"},
    )
    if required and "not found" not in str(last_error).lower():
        raise EvidenceUnavailableError(f"{tool_name} unavailable: {last_error}")
    return None


async def gather_order_evidence(
    gateway: EvidenceGateway,
    catalog: ToolCatalog,
    trace: TraceWriter,
    case_id: str,
    claimed_order_id: str | None,
) -> DomainEvidence | None:
    if not claimed_order_id:
        return None
    tool = catalog.best_tool("order", {"case_id", "order_id"})
    if tool is None:
        return None
    kwarg = primary_id_kwarg(tool) or "order_id"
    evidence = await _call_with_retry(
        gateway,
        trace,
        case_id,
        "order-agent",
        tool.name,
        required=True,
        **{kwarg: claimed_order_id},
    )
    if evidence is None:
        return None
    return DomainEvidence(
        "order", evidence["evidence_ref"], evidence["data"], entity_id=claimed_order_id
    )


async def gather_order_scoped_evidence(
    gateway: EvidenceGateway,
    catalog: ToolCatalog,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    domain: str,
    order_id: str,
) -> DomainEvidence | None:
    """Fetch a domain's evidence for the same verified order_id (one call)."""
    tool = catalog.best_tool(domain, {"case_id", "order_id"})
    if tool is None:
        return None
    kwarg = primary_id_kwarg(tool) or "order_id"
    evidence = await _call_with_retry(
        gateway, trace, case_id, actor, tool.name, **{kwarg: order_id}
    )
    if evidence is None:
        return None
    return DomainEvidence(domain, evidence["evidence_ref"], evidence["data"], entity_id=order_id)


async def gather_customer_evidence(
    gateway: EvidenceGateway,
    catalog: ToolCatalog,
    trace: TraceWriter,
    case_id: str,
    order_data: Any,
) -> DomainEvidence | None:
    """Only called with a genuine `customer_unique_id` from the order record.

    The order's `customer_id` is a different identifier; passing it as a
    customer_unique_id makes the tool fail on every case (observed on the live
    gateway), wasting audited calls for evidence that can never come back.
    """
    tool = catalog.best_tool("customer", {"case_id", "customer_unique_id"})
    if tool is None:
        return None
    kwarg = primary_id_kwarg(tool)
    unique_id = order_data.get("customer_unique_id") if isinstance(order_data, dict) else None
    if kwarg is None or not isinstance(unique_id, str) or not unique_id:
        return None
    evidence = await _call_with_retry(
        gateway, trace, case_id, "order-agent", tool.name, **{kwarg: unique_id}
    )
    if evidence is None:
        return None
    return DomainEvidence(
        "customer", evidence["evidence_ref"], evidence["data"], entity_id=unique_id
    )


async def gather_policy_evidence(
    gateway: EvidenceGateway,
    catalog: ToolCatalog,
    trace: TraceWriter,
    case_id: str,
    policy_version: str | None,
) -> DomainEvidence | None:
    tool = catalog.best_tool("policy", {"case_id", "policy_version"})
    if tool is None:
        return None
    kwargs: dict[str, str] = {}
    properties = (tool.input_schema or {}).get("properties", {})
    if policy_version and "policy_version" in properties:
        kwargs["policy_version"] = policy_version
    # Required: without the policy table the resolution (money, responsible
    # party, actions) would fall back to the LLM's guess.
    evidence = await _call_with_retry(
        gateway, trace, case_id, "policy-agent", tool.name, required=True, **kwargs
    )
    if evidence is None:
        return None
    return DomainEvidence("policy", evidence["evidence_ref"], evidence["data"])


def _evidence_payload(items: list[DomainEvidence]) -> list[dict[str, Any]]:
    return [{"entity_id": item.entity_id, "data": item.data} for item in items]


def _claimed_issues(claims: list[dict[str, Any]]) -> list[str]:
    return [
        claim["topic"]
        for claim in claims
        if isinstance(claim, dict) and claim.get("topic") in POLICY_ISSUES
    ]


async def draft_policy_assessment(
    case: dict[str, Any],
    evidence_by_domain: dict[str, list[DomainEvidence]],
    facts: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not evidence_by_domain.get("order"):
        return None  # no verified order: nothing safe to reason about
    customer_request = case.get("customer_request", {})
    context = {
        "case_id": case["case_id"],
        "customer_request": customer_request,
        "claimed_issues": _claimed_issues(customer_request.get("claims", [])),
        "policy_version": case.get("policy_version"),
        "facts": facts if facts is not None else build_case_facts(evidence_by_domain),
        "evidence": {
            domain: _evidence_payload(items) for domain, items in evidence_by_domain.items()
        },
    }
    llm = get_policy_llm()
    try:
        return await llm.draft_assessment(context)
    except Exception:  # noqa: BLE001 - any LLM/network failure falls back to insufficient_evidence
        return None


def _sanitize_cause_code(value: str) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value or "").upper().strip("_")
    if not cleaned:
        return None
    if not cleaned[0].isalpha():
        cleaned = f"C_{cleaned}"
    if len(cleaned) < 3:
        cleaned = (cleaned + "_UNKNOWN")[:80]
    cleaned = cleaned[:80]
    return cleaned if CAUSE_CODE_RE.fullmatch(cleaned) else None


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _aggregate_known_ids(
    evidence_by_domain: dict[str, list[DomainEvidence]],
) -> dict[str, set[str]]:
    """Harvest every id-shaped field from every evidence payload collected this
    case, regardless of which domain's tool happened to return it (e.g.
    `seller_id` shows up inside `get_order_items` too, not just `get_sellers`).
    """
    collected: dict[str, set[str]] = {key: set() for key in ID_KEY_HINTS}
    for items in evidence_by_domain.values():
        for item in items:
            extract_ids(item.data, collected)
    return collected


def _first_known_id(domain: str, known_ids: dict[str, set[str]]) -> str | None:
    id_key = DOMAIN_ID_KEY.get(domain)
    if id_key is None:
        return None
    ids = sorted(known_ids.get(id_key, set()))
    return ids[0] if ids else None


def _domain_refs(
    domains: list[str] | set[str] | tuple[str, ...],
    evidence_by_domain: dict[str, list[DomainEvidence]],
) -> list[str]:
    refs: list[str] = []
    for domain in domains:
        for item in evidence_by_domain.get(domain, []):
            if item.evidence_ref not in refs:
                refs.append(item.evidence_ref)
    return refs[:30]


def _affected_entities(known_ids: dict[str, set[str]]) -> dict[str, list[str]]:
    return {
        "order_ids": sorted(known_ids.get("order_id", set()))[:20],
        "item_ids": sorted(known_ids.get("item_id", set()))[:20],
        "seller_ids": sorted(known_ids.get("seller_id", set()))[:20],
        "payment_references": sorted(known_ids.get("payment_id", set()))[:20],
        "shipment_ids": sorted(known_ids.get("shipment_id", set()))[:20],
    }


def _verified_order_id(evidence_by_domain: dict[str, list[DomainEvidence]]) -> str | None:
    orders = evidence_by_domain.get("order") or []
    return orders[0].entity_id if orders else None


def _core_evidence_available(evidence_by_domain: dict[str, list[DomainEvidence]]) -> bool:
    """Enough evidence to make a determination: the order plus at least one of
    the payment/shipment/item records that every issue is decided on."""
    return bool(evidence_by_domain.get("order")) and any(
        evidence_by_domain.get(domain)
        for domain in ("payment", "payment_timeline", "shipment", "item")
    )


def _hypothesis_map(draft: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, str]:
    """issue -> assessment, from the LLM's per-issue hypotheses. Claimed fault
    issues the LLM left out of `hypotheses` fall back to its claim verdict
    (supported -> confirmed, unsupported -> contradicted)."""
    assessments: dict[str, str] = {}
    for entry in draft.get("hypotheses") or []:
        if not isinstance(entry, dict):
            continue
        issue, assessment = entry.get("issue"), entry.get("assessment")
        if issue in POLICY_ISSUES and assessment in HYPOTHESIS_ASSESSMENTS:
            assessments.setdefault(issue, assessment)

    verdict_by_claim = {
        item.get("claim_id"): item.get("verdict")
        for item in draft.get("claim_assessments", [])
        if isinstance(item, dict)
    }
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        topic = claim.get("topic")
        if topic not in POLICY_ISSUES or topic in NO_FAULT_ISSUES or topic in assessments:
            continue
        verdict = verdict_by_claim.get(claim.get("claim_id"))
        if verdict == "supported":
            assessments[topic] = "confirmed"
        elif verdict == "unsupported":
            assessments[topic] = "contradicted"
    return assessments


def reconcile_primary_issue(
    llm_primary: str,
    claimed: list[str],
    hypotheses: dict[str, str],
    core_evidence_available: bool,
) -> tuple[str, str]:
    """Decide primary_issue from the LLM's own per-issue findings, not just the
    single label it emitted.

    The LLM can confirm the customer's claimed issue and still name a different
    primary_issue after being pulled toward a secondary anomaly (an extra payment
    row, a stray event). A claimed issue the evidence confirms is the case's
    primary issue; the claim is only overturned when the evidence contradicts
    it. insufficient_evidence is reserved for genuinely missing evidence.
    Returns (primary_issue, basis), basis being a BASIS_CONFIDENCE key or "llm".
    """
    confirmed = [issue for issue, assessment in hypotheses.items() if assessment == "confirmed"]
    for topic in claimed:
        if hypotheses.get(topic) == "confirmed":
            return topic, "claim_confirmed"
    if llm_primary in confirmed:
        return llm_primary, "other_confirmed"
    if confirmed:
        return confirmed[0], "other_confirmed"
    # A contradicted *fault* claim means the complaint is unfounded. (A
    # contradicted no-fault label such as unsupported_claim means the opposite
    # — something did go wrong — so it never lands here.)
    fault_claim_contradicted = any(
        hypotheses.get(topic) == "contradicted" for topic in claimed if topic not in NO_FAULT_ISSUES
    )
    if core_evidence_available and fault_claim_contradicted:
        return "unsupported_claim", "claim_contradicted"
    if llm_primary != "insufficient_evidence" and (core_evidence_available or not hypotheses):
        return llm_primary, "llm"
    return "insufficient_evidence", "insufficient"


def _resolve_party_id(
    party_type: str,
    primary_issue: str,
    known_ids: dict[str, set[str]],
    facts: dict[str, Any],
) -> str | None:
    if party_type == "seller":
        if primary_issue == "late_delivery_seller":
            late = late_handover_sellers(facts)
            if late:
                return late[0]
        sellers = facts.get("items", {}).get("sellers") or sorted(known_ids.get("seller_id", set()))
        return sellers[0] if sellers else None
    domain = PARTY_TYPE_DOMAIN.get(party_type)
    return _first_known_id(domain, known_ids) if domain else None


def _apply_policy_rule(
    primary_issue: str,
    evidence_by_domain: dict[str, list[DomainEvidence]],
    known_ids: dict[str, set[str]],
    facts: dict[str, Any],
) -> dict[str, Any] | None:
    """Deterministically apply the authoritative `get_policy` rule table for
    `primary_issue` instead of trusting the LLM to invent a refund amount or a
    responsible party. Returns None when no policy evidence/rule is available,
    so the caller falls back to the LLM-drafted, still-verified values.
    """
    policy_items = evidence_by_domain.get("policy") or []
    if not policy_items:
        return None
    rules = (policy_items[0].data or {}).get("rules")
    if not isinstance(rules, dict):
        return None
    rule = rules.get(primary_issue)
    if not isinstance(rule, dict):
        return None

    case_status = rule.get("case_status")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        case_status = None
    recommended_action = str(rule.get("recommended_action", ""))[:80].strip()
    try:
        refund_brl = round(max(0.0, float(rule.get("refund_brl", 0.0))), 2)
    except (TypeError, ValueError):
        refund_brl = 0.0

    used_domains: set[str] = {"policy"}
    responsible_parties = []
    for party in rule.get("responsible_parties", [])[:5]:
        if not isinstance(party, dict):
            continue
        party_type = party.get("party_type")
        if party_type not in VALID_PARTY_TYPES:
            party_type = "unknown"
        # The rule table is shared by every case on this policy_version: a
        # null party_id means the rule names no specific entity (platform,
        # payment provider, ...) and must stay null; a non-null one is a
        # template example taken from some other case, so the real entity is
        # re-resolved from *this* case's evidence instead of being copied.
        party_id = None
        if party.get("party_id") is not None:
            party_id = _resolve_party_id(party_type, primary_issue, known_ids, facts)
            if party_id is not None and party_type in PARTY_TYPE_DOMAIN:
                used_domains.add(PARTY_TYPE_DOMAIN[party_type])
        responsible_parties.append({"party_type": party_type, "party_id": party_id})

    refund_lines = []
    if refund_brl > 0:
        refund_lines.append(
            {
                "reason_code": recommended_action or "policy_refund",
                "amount_brl": refund_brl,
                "entity_id": _verified_order_id(evidence_by_domain),
            }
        )

    return {
        "case_status": case_status,
        "resolution_actions": [recommended_action] if recommended_action else [],
        "responsible_parties": responsible_parties,
        "recommended_refund_brl": refund_brl,
        "refund_lines": refund_lines,
        "used_domains": used_domains,
    }


def _enforce_party_consistency(
    primary_issue: str, responsible_parties: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Reject a responsible party whose type cannot logically cause `primary_issue`."""
    allowed = PRIMARY_ISSUE_PARTY_RULES.get(primary_issue)
    if allowed is None:
        return responsible_parties, False
    fixed = []
    overridden = False
    for party in responsible_parties:
        if party["party_type"] in allowed:
            fixed.append(party)
        else:
            fixed.append({"party_type": "unknown", "party_id": None})
            overridden = True
    return fixed, overridden


def _calibrate_confidence(
    basis: str,
    llm_confidence: float,
    *,
    has_data_conflicts: bool,
    evidence_domain_count: int,
    party_overridden: bool,
) -> float:
    """Confidence tracks how the primary issue was established (see
    BASIS_CONFIDENCE), and can only drop further when evidence is thin or the
    draft needed correcting. A confirmed claimed issue is not penalised for
    secondary data conflicts: those are exactly the incidental anomalies the
    reconciliation already looked past."""
    score = BASIS_CONFIDENCE.get(basis, llm_confidence)
    if basis != "claim_confirmed" and has_data_conflicts:
        score = min(score, 0.6)
    if evidence_domain_count <= 1:
        score = min(score, 0.5)
    if party_overridden:
        score = min(score, 0.5)
    return round(max(0.0, min(1.0, score)), 2)


def _ranked_causes(draft: dict[str, Any], primary_issue: str) -> list[dict[str, Any]]:
    """The primary issue's own code always ranks first; the LLM's other causes
    follow as secondary findings."""
    codes = [primary_issue.upper()]
    for cause in sorted(
        (c for c in draft.get("ranked_causes", []) if isinstance(c, dict)),
        key=lambda c: c.get("rank") if isinstance(c.get("rank"), int) else 99,
    ):
        code = _sanitize_cause_code(str(cause.get("cause_code", "")))
        if code and code not in codes:
            codes.append(code)
    return [{"cause_code": code, "rank": rank} for rank, code in enumerate(codes[:5], start=1)]


def _data_conflicts(draft: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = []
    for conflict in draft.get("data_conflicts", [])[:5]:
        if not isinstance(conflict, dict):
            continue
        sources = [str(s)[:80] for s in conflict.get("sources", []) if str(s).strip()][:5]
        sources = list(dict.fromkeys(sources))
        if len(sources) < 2:
            continue
        field_name = str(conflict.get("field", ""))[:100].strip() or "unknown_field"
        selected = conflict.get("selected_source")
        selected = str(selected)[:80] if selected else None
        resolution_code = str(conflict.get("resolution_code", ""))[:80].strip() or "MANUAL_REVIEW"
        conflicts.append(
            {
                "field": field_name,
                "sources": sources,
                "selected_source": selected,
                "resolution_code": resolution_code,
            }
        )
    return conflicts


def _claim_verdict(
    topic: str | None,
    drafted_verdict: str | None,
    primary_issue: str,
    basis: str,
    hypotheses: dict[str, str],
    policy_action: str | None,
) -> str:
    """Keep each claim's verdict consistent with the case's final decision."""
    valid = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}
    fallback = drafted_verdict if drafted_verdict in valid else "insufficient_evidence"
    if topic == primary_issue and topic in POLICY_ISSUES:
        return "unsupported" if topic in NO_FAULT_ISSUES else "supported"
    if topic in POLICY_ISSUES:
        if hypotheses.get(topic) == "contradicted" or basis == "claim_contradicted":
            return "unsupported"
        if primary_issue in NO_FAULT_ISSUES and topic not in NO_FAULT_ISSUES:
            return "unsupported"
        return fallback
    if topic == REFUND_CLAIM_TOPIC:
        if primary_issue in NO_FAULT_ISSUES:
            return "unsupported"
        if primary_issue == "insufficient_evidence":
            return "insufficient_evidence"
        # Whether a *full* refund is warranted is decided by the policy action
        # already applied, not re-judged independently (which let the LLM call
        # a canceled-and-paid order's full-refund claim "unsupported").
        return REFUND_ACTION_VERDICT.get(policy_action or "", fallback)
    return fallback


def _fallback_output(
    case_id: str, claims: list[dict[str, Any]], evidence_by_domain: dict[str, list[DomainEvidence]]
) -> dict[str, Any]:
    known_ids = _aggregate_known_ids(evidence_by_domain)
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.1 if any(evidence_by_domain.values()) else 0.0,
        },
        "affected_entities": _affected_entities(known_ids),
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.0,
                "evidence_refs": [],
            }
            for claim in claims[:5]
            if isinstance(claim, dict) and "claim_id" in claim
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        # Only the order record was actually checked for a determination here.
        "evidence_refs": _domain_refs(["order"], evidence_by_domain),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_manual_review"],
    }


def verify_and_finalize(
    case_id: str,
    claims: list[dict[str, Any]],
    evidence_by_domain: dict[str, list[DomainEvidence]],
    draft: dict[str, Any] | None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Verifier Agent: reconcile the Policy Agent's draft against real evidence."""
    if draft is None:
        return _fallback_output(case_id, claims, evidence_by_domain)
    if facts is None:
        facts = build_case_facts(evidence_by_domain)

    known_ids = _aggregate_known_ids(evidence_by_domain)
    claims = [claim for claim in claims if isinstance(claim, dict) and "claim_id" in claim]
    claimed = _claimed_issues(claims)
    hypotheses = _hypothesis_map(draft, claims)
    llm_primary = draft.get("primary_issue")
    if llm_primary not in PRIMARY_ISSUES:
        llm_primary = "insufficient_evidence"
    primary_issue, basis = reconcile_primary_issue(
        llm_primary, claimed, hypotheses, _core_evidence_available(evidence_by_domain)
    )

    # Cite every evidence group the decision rests on: the one that establishes
    # the primary issue, and the ones that confirm or refute what was claimed.
    used_domains: set[str] = set(ISSUE_EVIDENCE_DOMAINS[primary_issue])
    for topic in claimed:
        used_domains.update(ISSUE_EVIDENCE_DOMAINS[topic])

    data_conflicts = _data_conflicts(draft)
    party_overridden = False
    policy_result = None
    if primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
        responsible_parties = [{"party_type": "unknown", "party_id": None}]
        refund_lines: list[dict[str, Any]] = []
        recommended_refund_brl = 0.0
        resolution_actions = ["escalate_manual_review"]
    else:
        policy_result = _apply_policy_rule(primary_issue, evidence_by_domain, known_ids, facts)
    if policy_result is not None:
        case_status = policy_result["case_status"] or "action_required"
        responsible_parties = policy_result["responsible_parties"]
        refund_lines = policy_result["refund_lines"]
        recommended_refund_brl = policy_result["recommended_refund_brl"]
        resolution_actions = policy_result["resolution_actions"]
        used_domains |= policy_result["used_domains"]
    elif primary_issue != "insufficient_evidence":
        case_status = draft.get("case_status")
        if case_status not in {"action_required", "no_action", "needs_investigation"}:
            case_status = "needs_investigation"
        if primary_issue in NO_FAULT_ISSUES:
            case_status = "no_action"

        responsible_parties = []
        for party in draft.get("responsible_parties", [])[:5]:
            if not isinstance(party, dict):
                continue
            party_type = party.get("party_type")
            if party_type not in VALID_PARTY_TYPES:
                party_type = "unknown"
            party_domain = party.get("party_id_domain") or ""
            party_id = _first_known_id(party_domain, known_ids)
            if party_id is not None:
                used_domains.add(party_domain)
            responsible_parties.append({"party_type": party_type, "party_id": party_id})
        responsible_parties, party_overridden = _enforce_party_consistency(
            primary_issue, responsible_parties
        )
        if party_overridden:
            data_conflicts.append(
                {
                    "field": "root_cause_analysis.responsible_parties",
                    "sources": ["policy_agent_draft", "primary_issue_policy_rules"],
                    "selected_source": "primary_issue_policy_rules",
                    "resolution_code": "PARTY_TYPE_INCONSISTENT_WITH_PRIMARY_ISSUE",
                }
            )

        no_refund_expected = (
            case_status == "no_action"
            or primary_issue in NO_FINANCIAL_ACTION_ISSUES
            or primary_issue in NO_FAULT_ISSUES
        )
        refund_lines = []
        if not no_refund_expected:
            for line in draft.get("financial_resolution", {}).get("refund_lines", [])[:10]:
                if not isinstance(line, dict):
                    continue
                amount = _clamp(line.get("amount_brl"), 0.0, 10_000_000.0, 0.0)
                reason_code = str(line.get("reason_code", ""))[:80].strip() or "REFUND_ADJUSTMENT"
                refund_lines.append(
                    {
                        "reason_code": reason_code,
                        "amount_brl": round(amount, 2),
                        "entity_id": _verified_order_id(evidence_by_domain),
                    }
                )
        recommended_refund_brl = round(sum(line["amount_brl"] for line in refund_lines), 2)

        resolution_actions = []
        for action in draft.get("resolution_actions", []):
            text = str(action)[:80].strip()
            if text and text not in resolution_actions:
                resolution_actions.append(text)
            if len(resolution_actions) == 8:
                break
    data_conflicts = data_conflicts[:5]

    evidence_domain_count = sum(1 for items in evidence_by_domain.values() if items)
    confidence = _calibrate_confidence(
        basis,
        _clamp(draft.get("confidence"), 0.0, 1.0, 0.2),
        has_data_conflicts=bool(data_conflicts),
        evidence_domain_count=evidence_domain_count,
        party_overridden=party_overridden,
    )

    drafted_claims = {
        item.get("claim_id"): item
        for item in draft.get("claim_assessments", [])
        if isinstance(item, dict)
    }
    claim_assessments = []
    for claim in claims[:5]:
        topic = claim.get("topic")
        drafted = drafted_claims.get(claim["claim_id"]) or {}
        verdict = _claim_verdict(
            topic,
            drafted.get("verdict"),
            primary_issue,
            basis,
            hypotheses,
            policy_result["resolution_actions"][0]
            if policy_result and policy_result["resolution_actions"]
            else None,
        )
        if verdict == "insufficient_evidence":
            refs: list[str] = []
            claim_confidence = min(_clamp(drafted.get("confidence"), 0.0, 1.0, 0.0), 0.3)
        else:
            if topic in POLICY_ISSUES:
                claim_domains: tuple[str, ...] = ISSUE_EVIDENCE_DOMAINS[topic]
            elif topic == REFUND_CLAIM_TOPIC:
                claim_domains = REFUND_CLAIM_DOMAINS + (("policy",) if policy_result else ())
            else:
                claim_domains = tuple(
                    d for d in drafted.get("supporting_domains", []) if d in used_domains
                )
            refs = _domain_refs(claim_domains, evidence_by_domain)
            used_domains.update(claim_domains)
            if not refs:
                verdict, claim_confidence = "insufficient_evidence", 0.0
            elif topic == primary_issue:
                claim_confidence = confidence
            else:
                claim_confidence = _clamp(drafted.get("confidence"), 0.0, 1.0, 0.5)
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": round(claim_confidence, 2),
                "evidence_refs": refs,
            }
        )

    evidence_refs = sorted(_domain_refs(sorted(used_domains), evidence_by_domain))[:30]

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": _affected_entities(known_ids),
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": _ranked_causes(draft, primary_issue),
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions,
    }
