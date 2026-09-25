from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

BRL = Decimal("0.01")


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    order_id: str
    policy_version: str
    opened_at: datetime | None
    claims: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any


@dataclass(frozen=True)
class OrderReport:
    status: str | None
    order_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    item_total: Decimal
    freight_total: Decimal
    shipping_limits: tuple[tuple[str, datetime], ...]
    carrier_handoff: datetime | None
    late_seller_ids: tuple[str, ...]
    order_evidence_ref: str
    item_evidence_ref: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class PaymentReport:
    payment_references: tuple[str, ...]
    payment_total: Decimal
    payment_count: int
    duplicate_charge: bool | None
    refund_state: str | None
    refunded_amount: Decimal
    payment_evidence_ref: str
    timeline_evidence_ref: str
    refund_evidence_ref: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ShipmentReport:
    shipment_ids: tuple[str, ...]
    is_late: bool | None
    late_seller_ids: tuple[str, ...]
    carrier_handoff: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    evidence_ref: str
    evidence_refs: tuple[str, ...]
    seller_evidence_ref: str | None = None


@dataclass(frozen=True)
class PolicyReport:
    payment_tolerance: Decimal
    data: Any
    evidence_ref: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    cause_code: str
    responsible_parties: tuple[tuple[str, str | None], ...]
    action: str
    refund_amount: Decimal
    refund_reason: str | None
    refund_entity_id: str | None
    evidence_refs: tuple[str, ...]


def _case_context(case: dict[str, Any]) -> CaseContext:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case_id must be a non-empty string")

    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError("customer_request must be an object")
    order_id = request.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("claimed_order_id must be a non-empty string")

    policy_version = case.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("policy_version must be a non-empty string")

    raw_claims = request.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("customer_request.claims must be a non-empty array")
    claims: list[dict[str, str]] = []
    for claim in raw_claims:
        if not isinstance(claim, dict):
            raise ValueError("each claim must be an object")
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("claim_id must be a non-empty string")
        if not isinstance(topic, str) or not topic:
            raise ValueError("claim topic must be a non-empty string")
        claims.append({"claim_id": claim_id, "topic": topic})

    return CaseContext(
        case_id=case_id,
        order_id=order_id,
        policy_version=policy_version,
        opened_at=_timestamp(case.get("opened_at")),
        claims=tuple(claims),
    )


def _rows(data: Any, wrappers: tuple[str, ...]) -> list[dict[str, Any]]:
    current = data
    for _ in range(4):
        if not isinstance(current, dict):
            break
        next_value: Any = None
        for wrapper in (*wrappers, "data"):
            wrapped = current.get(wrapper)
            if isinstance(wrapped, (dict, list)):
                next_value = wrapped
                break
        if next_value is None:
            break
        current = next_value
    if isinstance(current, dict):
        return [current]
    if isinstance(current, list):
        return [row for row in current if isinstance(row, dict)]
    return []


def _lookup(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _after(left: datetime | None, right: datetime | None) -> bool | None:
    if left is None or right is None:
        return None
    try:
        return left > right
    except TypeError:
        return None


def _status(value: Any) -> str | None:
    text = _text(value)
    return text.lower().replace(" ", "_") if text else None


def _explicit_bool(rows: list[dict[str, Any]], *names: str) -> bool | None:
    found_false = False
    for row in rows:
        value = _lookup(row, *names)
        if isinstance(value, bool):
            if value:
                return True
            found_false = True
        elif isinstance(value, (int, str)):
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "duplicate", "duplicated"}:
                return True
            if normalized in {"false", "0", "no", "unique"}:
                found_false = True
    return False if found_false else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _money(value: Decimal) -> Decimal:
    return value.quantize(BRL, rounding=ROUND_HALF_UP)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    context: CaseContext,
    actor: str,
    tool_name: str,
    **arguments: str,
) -> EvidenceRecord:
    response = await gateway.call(tool_name, case_id=context.case_id, **arguments)
    evidence_ref = response.get("evidence_ref")
    domain = response.get("domain")
    if not isinstance(evidence_ref, str) or not evidence_ref.startswith("ev_"):
        raise ValueError(f"MCP tool {tool_name} returned an invalid evidence_ref")
    if not isinstance(domain, str) or not domain:
        raise ValueError(f"MCP tool {tool_name} returned an invalid domain")
    trace.emit(
        case_id=context.case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence_ref],
    )
    return EvidenceRecord(tool_name, evidence_ref, domain, response.get("data"))


def _assigned(trace: TraceWriter, context: CaseContext, target: str, code: str) -> None:
    trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=target,
        decision_code=code,
    )


def _handoff(trace: TraceWriter, context: CaseContext, actor: str, code: str) -> None:
    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=code,
    )


async def _order_agent(
    gateway: EvidenceGateway, trace: TraceWriter, context: CaseContext
) -> OrderReport:
    actor = "order-item-agent"
    _assigned(trace, context, actor, "COLLECT_ORDER_ITEMS")
    order_evidence = await _consume(
        gateway, trace, context, actor, "get_order", order_id=context.order_id
    )
    item_evidence = await _consume(
        gateway, trace, context, actor, "get_order_items", order_id=context.order_id
    )
    order_rows = _rows(order_evidence.data, ("order", "orders"))
    order = order_rows[0] if order_rows else {}
    items = _rows(item_evidence.data, ("items", "order_items"))

    item_ids: list[str] = []
    seller_ids: list[str] = []
    shipping_limits: list[tuple[str, datetime]] = []
    item_total = Decimal("0")
    freight_total = Decimal("0")
    for item in items:
        raw_item_id = _text(_lookup(item, "item_id", "order_item_id"))
        if raw_item_id:
            item_ids.append(
                raw_item_id if ":" in raw_item_id else f"{context.order_id}:{raw_item_id}"
            )
        seller_id = _text(_lookup(item, "seller_id", "merchant_id"))
        if seller_id:
            seller_ids.append(seller_id)
        price = _decimal(_lookup(item, "price", "item_price", "price_brl"))
        freight = _decimal(
            _lookup(item, "freight_value", "freight", "freight_brl", "shipping_cost")
        )
        item_total += price or Decimal("0")
        freight_total += freight or Decimal("0")
        limit = _timestamp(_lookup(item, "shipping_limit_date", "shipping_deadline"))
        if seller_id and limit:
            shipping_limits.append((seller_id, limit))

    carrier_handoff = _timestamp(
        _lookup(
            order,
            "order_delivered_carrier_date",
            "delivered_carrier_at",
            "carrier_handoff_at",
            "shipped_at",
        )
    )
    late_sellers = [
        seller_id for seller_id, limit in shipping_limits if _after(carrier_handoff, limit) is True
    ]
    _handoff(trace, context, actor, "ORDER_ITEMS_READY")
    return OrderReport(
        status=_status(_lookup(order, "order_status", "status")),
        order_ids=(context.order_id,),
        item_ids=_unique(item_ids),
        seller_ids=_unique(seller_ids),
        item_total=_money(item_total),
        freight_total=_money(freight_total),
        shipping_limits=tuple(shipping_limits),
        carrier_handoff=carrier_handoff,
        late_seller_ids=_unique(late_sellers),
        order_evidence_ref=order_evidence.evidence_ref,
        item_evidence_ref=item_evidence.evidence_ref,
        evidence_refs=(order_evidence.evidence_ref, item_evidence.evidence_ref),
    )


async def _payment_agent(
    gateway: EvidenceGateway, trace: TraceWriter, context: CaseContext
) -> PaymentReport:
    actor = "payment-refund-agent"
    _assigned(trace, context, actor, "COLLECT_PAYMENT_REFUND")
    payment_evidence = await _consume(
        gateway, trace, context, actor, "get_order_payments", order_id=context.order_id
    )
    timeline_evidence = await _consume(
        gateway, trace, context, actor, "get_payment_timeline", order_id=context.order_id
    )
    topics = {claim["topic"] for claim in context.claims}
    refund_evidence: EvidenceRecord | None = None
    if topics & {"refund_pending", "refund_failed"}:
        refund_evidence = await _consume(
            gateway,
            trace,
            context,
            actor,
            "get_refund_timeline",
            order_id=context.order_id,
        )
    payments = _rows(payment_evidence.data, ("payments", "order_payments"))
    payment_events = _rows(
        timeline_evidence.data,
        ("payment_timeline", "events", "payments", "timeline"),
    )
    refunds = (
        _rows(
            refund_evidence.data,
            ("refund_timeline", "refunds", "events", "timeline"),
        )
        if refund_evidence is not None
        else []
    )

    references: list[str] = []
    payment_total = Decimal("0")
    for payment in payments:
        sequential = _text(_lookup(payment, "payment_sequential", "sequence", "payment_sequence"))
        reference = _text(
            _lookup(
                payment,
                "payment_reference",
                "payment_id",
                "transaction_id",
                "charge_id",
            )
        )
        if reference:
            references.append(reference)
        elif sequential:
            references.append(f"{context.order_id}:{sequential}")
        amount = _decimal(_lookup(payment, "payment_value", "amount_brl", "amount", "value"))
        payment_total += amount or Decimal("0")

    duplicate = _explicit_bool(
        [*payment_events, *payments], "is_duplicate", "duplicate", "duplicated"
    )
    if duplicate is not True:
        event_statuses = {
            _status(_lookup(event, "status", "event", "event_type")) for event in payment_events
        }
        if event_statuses & {"duplicate", "duplicated", "duplicate_charge"}:
            duplicate = True
        elif duplicate is None:
            known_refs = [
                _text(
                    _lookup(
                        payment,
                        "payment_reference",
                        "payment_id",
                        "transaction_id",
                        "charge_id",
                    )
                )
                for payment in payments
            ]
            filtered_refs = [reference for reference in known_refs if reference]
            if filtered_refs:
                duplicate = len(filtered_refs) != len(set(filtered_refs))

    refund_statuses = [
        _status(_lookup(refund, "status", "refund_status", "state", "event_type"))
        for refund in refunds
    ]
    refund_state: str | None = None
    if any(
        status in {"failed", "failure", "refund_failed", "rejected"} for status in refund_statuses
    ):
        refund_state = "failed"
    elif any(
        status in {"pending", "processing", "initiated", "refund_pending"}
        for status in refund_statuses
    ):
        refund_state = "pending"
    elif any(
        status in {"completed", "succeeded", "refunded", "refund_completed"}
        for status in refund_statuses
    ):
        refund_state = "completed"

    refund_amounts = [
        amount
        for refund in refunds
        if (
            amount := _decimal(
                _lookup(
                    refund,
                    "amount_brl",
                    "refund_amount_brl",
                    "refund_amount",
                    "amount",
                )
            )
        )
        is not None
    ]
    refunded_amount = max(refund_amounts, default=Decimal("0"))
    _handoff(trace, context, actor, "PAYMENT_REFUND_READY")
    evidence_refs = [payment_evidence.evidence_ref, timeline_evidence.evidence_ref]
    if refund_evidence is not None:
        evidence_refs.append(refund_evidence.evidence_ref)
    return PaymentReport(
        payment_references=_unique(references),
        payment_total=_money(payment_total),
        payment_count=len(payments),
        duplicate_charge=duplicate,
        refund_state=refund_state,
        refunded_amount=_money(refunded_amount),
        payment_evidence_ref=payment_evidence.evidence_ref,
        timeline_evidence_ref=timeline_evidence.evidence_ref,
        refund_evidence_ref=(refund_evidence.evidence_ref if refund_evidence is not None else None),
        evidence_refs=tuple(evidence_refs),
    )


async def _shipment_agent(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    context: CaseContext,
    order: OrderReport | None,
) -> ShipmentReport:
    actor = "shipment-agent"
    _assigned(trace, context, actor, "COLLECT_SHIPMENT")
    evidence = await _consume(
        gateway, trace, context, actor, "get_shipment_summary", order_id=context.order_id
    )
    rows = _rows(
        evidence.data,
        ("shipment_summary", "shipment", "shipments", "summary"),
    )
    shipment = rows[0] if rows else {}
    shipment_ids = [
        identifier
        for row in rows
        if (
            identifier := _text(
                _lookup(row, "shipment_id", "shipping_id", "tracking_id", "tracking_code")
            )
        )
    ]
    carrier_handoff = _timestamp(
        _lookup(
            shipment,
            "order_delivered_carrier_date",
            "delivered_carrier_at",
            "carrier_handoff_at",
            "shipped_at",
        )
    )
    delivered_at = _timestamp(
        _lookup(
            shipment,
            "order_delivered_customer_date",
            "delivered_customer_at",
            "delivered_at",
        )
    )
    estimated_at = _timestamp(
        _lookup(
            shipment,
            "order_estimated_delivery_date",
            "estimated_delivery_at",
            "estimated_at",
        )
    )
    limits = order.shipping_limits if order else ()
    handoff = carrier_handoff or (order.carrier_handoff if order else None)
    late_sellers = [seller_id for seller_id, limit in limits if _after(handoff, limit) is True]
    seller_evidence: EvidenceRecord | None = None
    if late_sellers:
        seller_evidence = await _consume(
            gateway,
            trace,
            context,
            actor,
            "get_sellers",
            order_id=context.order_id,
        )
    _handoff(trace, context, actor, "SHIPMENT_READY")
    evidence_refs = [evidence.evidence_ref]
    if seller_evidence is not None:
        evidence_refs.append(seller_evidence.evidence_ref)
    return ShipmentReport(
        shipment_ids=_unique(shipment_ids),
        is_late=_after(delivered_at, estimated_at),
        late_seller_ids=_unique(late_sellers),
        carrier_handoff=handoff,
        delivered_at=delivered_at,
        estimated_at=estimated_at,
        evidence_ref=evidence.evidence_ref,
        evidence_refs=tuple(evidence_refs),
        seller_evidence_ref=(seller_evidence.evidence_ref if seller_evidence is not None else None),
    )


async def _policy_agent(
    gateway: EvidenceGateway, trace: TraceWriter, context: CaseContext
) -> PolicyReport:
    actor = "policy-agent"
    _assigned(trace, context, actor, "LOAD_POLICY")
    evidence = await _consume(
        gateway,
        trace,
        context,
        actor,
        "get_policy",
        policy_version=context.policy_version,
    )
    rows = _rows(evidence.data, ("policy", "policies"))
    policy = rows[0] if rows else {}
    tolerance = _decimal(
        _lookup(policy, "payment_tolerance_brl", "payment_tolerance", "tolerance_brl")
    )
    _handoff(trace, context, actor, "POLICY_READY")
    return PolicyReport(
        payment_tolerance=tolerance if tolerance is not None else Decimal("0.10"),
        data=evidence.data,
        evidence_ref=evidence.evidence_ref,
        evidence_refs=(evidence.evidence_ref,),
    )


def _refs(*groups: tuple[str, ...] | str) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        values.extend(group if isinstance(group, tuple) else (group,))
    return _unique(values)


def _decision(
    *,
    primary_issue: str,
    case_status: str,
    confidence: float,
    cause_code: str,
    parties: tuple[tuple[str, str | None], ...],
    action: str,
    refund: Decimal,
    reason: str | None,
    entity_id: str | None,
    evidence_refs: tuple[str, ...],
) -> Decision:
    return Decision(
        primary_issue=primary_issue,
        case_status=case_status,
        confidence=confidence,
        cause_code=cause_code,
        responsible_parties=parties,
        action=action,
        refund_amount=_money(refund),
        refund_reason=reason,
        refund_entity_id=entity_id,
        evidence_refs=evidence_refs,
    )


def _decide(
    context: CaseContext,
    order: OrderReport,
    payment: PaymentReport,
    shipment: ShipmentReport,
    policy: PolicyReport,
) -> Decision:
    policy_ref = policy.evidence_ref
    if order.status is None or payment.payment_count == 0:
        return _decision(
            primary_issue="insufficient_evidence",
            case_status="needs_investigation",
            confidence=0.35,
            cause_code="INSUFFICIENT_AUTHORITATIVE_EVIDENCE",
            parties=(("unknown", None),),
            action="investigate_case",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(order.evidence_refs, payment.evidence_refs, policy_ref),
        )

    if payment.refund_state == "failed":
        return _decision(
            primary_issue="refund_failed",
            case_status="action_required",
            confidence=0.95,
            cause_code="REFUND_PROCESSING_FAILED",
            parties=(("payment_provider", "PAYMENT_PROVIDER"),),
            action="retry_refund",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(
                payment.payment_evidence_ref, payment.refund_evidence_ref, policy_ref
            ),
        )

    if payment.refund_state == "pending":
        return _decision(
            primary_issue="refund_pending",
            case_status="needs_investigation",
            confidence=0.94,
            cause_code="REFUND_PROCESSING_PENDING",
            parties=(("payment_provider", "PAYMENT_PROVIDER"),),
            action="monitor_refund",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(
                payment.payment_evidence_ref, payment.refund_evidence_ref, policy_ref
            ),
        )

    if order.status == "canceled" and payment.payment_total > 0:
        return _decision(
            primary_issue="canceled_order_paid",
            case_status="action_required",
            confidence=0.97,
            cause_code="ORDER_CANCELED_AFTER_PAYMENT",
            parties=(("platform", "OLIST_PLATFORM"),),
            action="issue_full_refund",
            refund=payment.payment_total,
            reason="CANCELED_ORDER_PAYMENT",
            entity_id=context.order_id,
            evidence_refs=_refs(order.order_evidence_ref, payment.payment_evidence_ref, policy_ref),
        )

    if order.status == "unavailable" and payment.payment_total > 0:
        return _decision(
            primary_issue="unavailable_order_paid",
            case_status="action_required",
            confidence=0.97,
            cause_code="ORDER_UNAVAILABLE_AFTER_PAYMENT",
            parties=(("platform", "OLIST_PLATFORM"),),
            action="issue_full_refund",
            refund=payment.payment_total,
            reason="UNAVAILABLE_ORDER_PAYMENT",
            entity_id=context.order_id,
            evidence_refs=_refs(order.order_evidence_ref, payment.payment_evidence_ref, policy_ref),
        )

    expected_total = _money(order.item_total + order.freight_total)
    payment_delta = _money(payment.payment_total - expected_total)
    if payment.duplicate_charge is True:
        duplicate_amount = payment_delta if payment_delta > 0 else payment.payment_total
        return _decision(
            primary_issue="duplicate_charge",
            case_status="action_required",
            confidence=0.95,
            cause_code="DUPLICATE_PAYMENT_DETECTED",
            parties=(("payment_provider", "PAYMENT_PROVIDER"),),
            action="refund_duplicate_charge",
            refund=duplicate_amount,
            reason="DUPLICATE_CHARGE",
            entity_id=(payment.payment_references[-1] if payment.payment_references else None),
            evidence_refs=_refs(
                payment.payment_evidence_ref, payment.timeline_evidence_ref, policy_ref
            ),
        )

    if abs(payment_delta) > policy.payment_tolerance:
        return _decision(
            primary_issue="payment_mismatch",
            case_status="needs_investigation",
            confidence=0.90,
            cause_code="PAYMENT_TOTAL_MISMATCH",
            parties=(("payment_provider", "PAYMENT_PROVIDER"),),
            action="investigate_payment_mismatch",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(order.item_evidence_ref, payment.payment_evidence_ref, policy_ref),
        )

    if shipment.is_late is True and shipment.late_seller_ids:
        return _decision(
            primary_issue="late_delivery_seller",
            case_status="action_required",
            confidence=0.94,
            cause_code="SELLER_HANDOFF_AFTER_LIMIT",
            parties=tuple(("seller", seller_id) for seller_id in shipment.late_seller_ids),
            action="refund_freight",
            refund=order.freight_total,
            reason="LATE_DELIVERY_FREIGHT",
            entity_id=context.order_id,
            evidence_refs=_refs(order.item_evidence_ref, shipment.evidence_refs, policy_ref),
        )

    if shipment.is_late is True:
        return _decision(
            primary_issue="late_delivery_logistics",
            case_status="action_required",
            confidence=0.93,
            cause_code="CARRIER_DELIVERED_AFTER_ESTIMATE",
            parties=(("logistics_provider", "LOGISTICS_PROVIDER"),),
            action="refund_freight",
            refund=order.freight_total,
            reason="LATE_DELIVERY_FREIGHT",
            entity_id=context.order_id,
            evidence_refs=_refs(order.item_evidence_ref, shipment.evidence_ref, policy_ref),
        )

    if payment.payment_count >= 2:
        return _decision(
            primary_issue="valid_split_payment",
            case_status="no_action",
            confidence=0.97,
            cause_code="MULTIPLE_PAYMENTS_RECONCILED",
            parties=(),
            action="explain_valid_split_payment",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(order.item_evidence_ref, payment.payment_evidence_ref, policy_ref),
        )

    if shipment.is_late is None:
        return _decision(
            primary_issue="insufficient_evidence",
            case_status="needs_investigation",
            confidence=0.40,
            cause_code="INSUFFICIENT_SHIPMENT_EVIDENCE",
            parties=(("unknown", None),),
            action="investigate_case",
            refund=Decimal("0"),
            reason=None,
            entity_id=None,
            evidence_refs=_refs(
                order.evidence_refs, payment.payment_evidence_ref, shipment.evidence_ref, policy_ref
            ),
        )

    return _decision(
        primary_issue="unsupported_claim",
        case_status="no_action",
        confidence=0.92,
        cause_code="CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
        parties=(),
        action="reject_claim",
        refund=Decimal("0"),
        reason=None,
        entity_id=None,
        evidence_refs=_refs(
            order.evidence_refs, payment.payment_evidence_ref, shipment.evidence_ref, policy_ref
        ),
    )


def _claim_assessments(context: CaseContext, decision: Decision) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for claim in context.claims:
        topic = claim["topic"]
        if decision.primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            if decision.action == "issue_full_refund":
                verdict = "supported"
            elif decision.refund_amount > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif decision.primary_issue == "unsupported_claim":
            verdict = "unsupported"
        elif topic == decision.primary_issue:
            verdict = "supported"
        else:
            verdict = "unsupported"
        results.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": decision.confidence,
                "evidence_refs": list(decision.evidence_refs),
            }
        )
    return results


def _json_money(value: Decimal) -> float:
    return float(_money(value))


def _build_output(
    context: CaseContext,
    order: OrderReport,
    payment: PaymentReport,
    shipment: ShipmentReport,
    decision: Decision,
) -> dict[str, Any]:
    refund_lines: list[dict[str, Any]] = []
    if decision.refund_amount > 0:
        if decision.refund_reason is None:
            raise ValueError("positive refund is missing a reason code")
        refund_lines.append(
            {
                "reason_code": decision.refund_reason,
                "amount_brl": _json_money(decision.refund_amount),
                "entity_id": decision.refund_entity_id,
            }
        )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": context.case_id,
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": list(order.order_ids[:20]),
            "item_ids": list(order.item_ids[:20]),
            "seller_ids": list(order.seller_ids[:20]),
            "payment_references": list(payment.payment_references[:20]),
            "shipment_ids": list(shipment.shipment_ids[:20]),
        },
        "claim_assessments": _claim_assessments(context, decision),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": decision.cause_code, "rank": 1}],
            "responsible_parties": [
                {"party_type": party_type, "party_id": party_id}
                for party_type, party_id in decision.responsible_parties[:5]
            ],
        },
        "evidence_refs": list(decision.evidence_refs),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _json_money(decision.refund_amount),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision.action],
    }


def _verify_output(context: CaseContext, output: dict[str, Any], consumed_refs: set[str]) -> None:
    if output.get("case_id") != context.case_id:
        raise ValueError("output case_id does not match the active case")

    evidence_refs = output.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(reference, str) for reference in evidence_refs
    ):
        raise ValueError("output evidence_refs must be an array of strings")
    unknown = set(evidence_refs) - consumed_refs
    if unknown:
        raise ValueError(f"unknown evidence references: {sorted(unknown)}")
    if len(evidence_refs) != len(set(evidence_refs)):
        raise ValueError("duplicate evidence reference")

    for claim in output.get("claim_assessments", []):
        claim_refs = claim.get("evidence_refs", []) if isinstance(claim, dict) else []
        if not set(claim_refs) <= set(evidence_refs):
            raise ValueError("claim evidence is outside top-level evidence_refs")

    entities = output.get("affected_entities")
    if not isinstance(entities, dict):
        raise ValueError("affected_entities must be an object")
    for name in (
        "order_ids",
        "item_ids",
        "seller_ids",
        "payment_references",
        "shipment_ids",
    ):
        values = entities.get(name)
        if not isinstance(values, list):
            raise ValueError(f"affected entity {name} must be an array")
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate entity in {name}")

    financial = output.get("financial_resolution")
    if not isinstance(financial, dict):
        raise ValueError("financial_resolution must be an object")
    recommended = _decimal(financial.get("recommended_refund_brl"))
    lines = financial.get("refund_lines")
    if recommended is None or not isinstance(lines, list):
        raise ValueError("refund total or refund lines are invalid")
    line_total = Decimal("0")
    for line in lines:
        amount = _decimal(line.get("amount_brl")) if isinstance(line, dict) else None
        if amount is None:
            raise ValueError("refund line amount is invalid")
        line_total += amount
    if _money(line_total) != _money(recommended):
        raise ValueError("refund total does not equal refund lines")

    assessment = output.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError("assessment must be an object")
    confidence = assessment.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("confidence must be numeric")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    if assessment.get("case_status") == "no_action" and recommended != 0:
        raise ValueError("no-action refund must be zero")
    if recommended < 0:
        raise ValueError("recommended refund must not be negative")

    actions = output.get("resolution_actions")
    if not isinstance(actions, list) or not actions or len(actions) != len(set(actions)):
        raise ValueError("resolution actions must be a non-empty unique array")
    full_refund_issues = {"canceled_order_paid", "unavailable_order_paid"}
    if assessment.get("primary_issue") in full_refund_issues and (
        assessment.get("case_status") != "action_required" or "issue_full_refund" not in actions
    ):
        raise ValueError("full-refund issue has inconsistent status or action")
    if assessment.get("primary_issue") == "valid_split_payment" and recommended != 0:
        raise ValueError("valid split payment cannot recommend a refund")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one L3A case using scoped MCP evidence and observable handoffs."""
    context = _case_context(case)
    order = await _order_agent(gateway, trace, context)
    payment = await _payment_agent(gateway, trace, context)
    shipment = await _shipment_agent(gateway, trace, context, order)
    policy = await _policy_agent(gateway, trace, context)
    decision = _decide(context, order, payment, shipment, policy)
    trace.emit(
        case_id=context.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=decision.primary_issue.upper(),
        evidence_refs=[policy.evidence_ref],
    )
    output = _build_output(context, order, payment, shipment, decision)
    consumed_refs = set(
        _refs(
            order.evidence_refs,
            payment.evidence_refs,
            shipment.evidence_refs,
            policy.evidence_refs,
        )
    )
    _verify_output(context, output, consumed_refs)
    trace.emit(
        case_id=context.case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code=decision.primary_issue.upper(),
        evidence_refs=list(decision.evidence_refs),
        attributes={
            "evidence_count": len(decision.evidence_refs),
            "refund_brl": _json_money(decision.refund_amount),
        },
    )
    return output
