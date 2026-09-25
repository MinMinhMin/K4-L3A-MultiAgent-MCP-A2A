from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .evidence import EvidenceLedger

ZERO = Decimal("0")
CENT = Decimal("0.01")
MONEY_ALIASES = {
    "amount",
    "amountbrl",
    "orderamount",
    "ordertotal",
    "paidamount",
    "paymentamount",
    "paymentvalue",
    "pricetotal",
    "refundamount",
    "totalamount",
    "totalvalue",
}


@dataclass(frozen=True)
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    order_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    payment_references: tuple[str, ...]
    shipment_ids: tuple[str, ...]
    claim_assessments: tuple[dict[str, Any], ...]
    ranked_causes: tuple[dict[str, Any], ...]
    responsible_parties: tuple[tuple[str, str | None], ...]
    evidence_refs: tuple[str, ...]
    data_conflicts: tuple[dict[str, Any], ...]
    refund_total: Decimal
    refund_lines: tuple[dict[str, Any], ...]
    resolution_actions: tuple[str, ...]


def _key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _mappings(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            result.extend(_mappings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_mappings(child))
    return result


def _values(value: Any, aliases: set[str]) -> list[Any]:
    wanted = {_key(alias) for alias in aliases}
    result: list[Any] = []
    for mapping in _mappings(value):
        for name, item in mapping.items():
            if _key(name) in wanted:
                result.append(item)
    return result


def _unique_strings(values: list[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if isinstance(value, (str, int)) and str(value).strip():
            item = str(value).strip()
            if item not in result:
                result.append(item)
    return tuple(result)


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed.quantize(CENT, rounding=ROUND_HALF_UP)


def _first_money(value: Any, aliases: set[str]) -> Decimal | None:
    for item in _values(value, aliases):
        parsed = _money(item)
        if parsed is not None:
            return parsed
    return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_timestamp(value: Any, aliases: set[str]) -> datetime | None:
    for item in _values(value, aliases):
        parsed = _timestamp(item)
        if parsed is not None:
            return parsed
    return None


def _rows(value: Any, aliases: set[str]) -> list[dict[str, Any]]:
    wanted = {_key(alias) for alias in aliases}
    return [
        mapping for mapping in _mappings(value) if any(_key(name) in wanted for name in mapping)
    ]


def _status(value: Any) -> str:
    return _key(value).replace("status", "")


def _record_data(ledger: EvidenceLedger, domains: set[str]) -> list[Any]:
    return [record.data for record in ledger.all() if record.domain in domains]


def _evidence_refs(ledger: EvidenceLedger, domains: set[str]) -> tuple[str, ...]:
    refs = [record.evidence_ref for record in ledger.all() if record.domain in domains]
    return tuple(dict.fromkeys(refs))


def _entity_ids(
    ledger: EvidenceLedger, aliases: set[str], domains: set[str] | None = None
) -> tuple[str, ...]:
    records = (
        ledger.all()
        if domains is None
        else tuple(record for record in ledger.all() if record.domain in domains)
    )
    values = [value for record in records for value in _values(record.data, aliases)]
    return _unique_strings(values)


def _payment_rows(ledger: EvidenceLedger) -> list[dict[str, Any]]:
    data = _record_data(ledger, {"payment"})
    return _rows(
        data,
        {
            "payment_value",
            "payment_amount",
            "amount",
            "paid_amount",
            "payment_reference",
            "payment_id",
            "transaction_id",
            "payment_status",
        },
    )


def _dedupe_payment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        references = _values(
            row, {"payment_reference", "payment_id", "transaction_id", "transactionid"}
        )
        if references:
            identity = ("reference", str(references[0]))
        else:
            identity = ("row", json.dumps(row, sort_keys=True, default=str))
        if identity not in seen:
            seen.add(identity)
            unique.append(row)
    return unique


def _refund_rows(ledger: EvidenceLedger) -> list[dict[str, Any]]:
    return _rows(
        _record_data(ledger, {"refund"}),
        {"refund_status", "refund_amount", "amount", "status"},
    )


def _issue_assessment(
    claims: list[dict[str, Any]],
    primary_issue: str,
    confidence: float,
    evidence_refs: tuple[str, ...],
    refund_total: Decimal,
) -> tuple[dict[str, Any], ...]:
    assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        claim_id = str(claim.get("claim_id", "claim"))
        topic = str(claim.get("topic", "")).strip()
        if primary_issue == "unsupported_claim":
            verdict = "unsupported"
            claim_confidence = min(confidence, 0.35)
        elif primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            claim_confidence = min(confidence, 0.35)
        elif topic == primary_issue or (topic == "requested_full_refund" and refund_total > ZERO):
            verdict = "supported"
            claim_confidence = confidence
        else:
            verdict = "unsupported"
            claim_confidence = min(confidence, 0.45)
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(claim_confidence, 4),
                "evidence_refs": list(evidence_refs),
            }
        )
    return tuple(assessments)


def _round_money(value: Decimal) -> Decimal:
    return max(value, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def analyze_case(case: dict[str, Any], ledger: EvidenceLedger) -> Decision:
    order_data = _record_data(ledger, {"order"})
    shipment_data = _record_data(ledger, {"shipment"})

    order_ids = _entity_ids(ledger, {"order_id", "orderid"})
    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")
    if isinstance(claimed_order_id, str) and claimed_order_id and claimed_order_id not in order_ids:
        order_ids = (claimed_order_id, *order_ids)
    item_ids = _entity_ids(ledger, {"order_item_id", "item_id", "itemid"})
    seller_ids = _entity_ids(ledger, {"seller_id", "sellerid"})
    payment_references = _entity_ids(
        ledger,
        {"payment_reference", "payment_id", "transaction_id", "transactionid"},
        {"payment"},
    )
    shipment_ids = _entity_ids(
        ledger,
        {"shipment_id", "tracking_code", "tracking_id"},
        {"shipment"},
    )

    order_status = (
        _status(_values(order_data, {"order_status", "status"})[0])
        if _values(order_data, {"order_status", "status"})
        else ""
    )
    order_total = _first_money(
        order_data,
        {"order_total", "order_amount", "total_amount", "total_value", "price_total"},
    )
    raw_payment_rows = _payment_rows(ledger)
    payment_rows = _dedupe_payment_rows(raw_payment_rows)
    payment_amounts = [
        parsed
        for row in payment_rows
        for value in _values(row, {"payment_value", "payment_amount", "amount", "paid_amount"})
        if (parsed := _money(value)) is not None
    ]
    payment_total = _round_money(sum(payment_amounts, ZERO))
    payment_statuses = {
        _status(value)
        for row in payment_rows
        for value in _values(row, {"payment_status", "status", "state"})
    }
    paid_statuses = {"paid", "approved", "captured", "charged", "authorized", "completed"}
    paid_confirmed = payment_total > ZERO or bool(payment_statuses & paid_statuses)

    duplicate_markers = _values(
        raw_payment_rows, {"is_duplicate", "duplicate_charge", "charge_type"}
    )
    explicit_duplicate = any(
        value is True or _key(value) in {"duplicate", "duplicatecharge"}
        for value in duplicate_markers
    )
    duplicate_charge = explicit_duplicate or (
        len(payment_rows) > 1
        and order_total is not None
        and payment_total > order_total
    )
    payment_mismatch = (
        order_total is not None and payment_total > ZERO and abs(payment_total - order_total) > CENT
    )
    valid_split = (
        len(payment_amounts) > 1
        and order_total is not None
        and payment_total == order_total
        and all(status in paid_statuses for status in payment_statuses or {"paid"})
    )

    refund_rows = _refund_rows(ledger)
    refund_statuses = {
        _status(value)
        for row in refund_rows
        for value in _values(row, {"refund_status", "status", "state"})
    }
    refund_amounts = [
        parsed
        for row in refund_rows
        for value in _values(row, {"refund_amount", "amount", "refund_value"})
        if (parsed := _money(value)) is not None
    ]
    refund_requested = _round_money(sum(refund_amounts, ZERO))

    shipment_data_all = shipment_data
    seller_handoff_limit = _first_timestamp(
        shipment_data_all,
        {"shipping_limit_date", "seller_handoff_deadline", "seller_handoff_limit"},
    )
    carrier_handoff = _first_timestamp(
        shipment_data_all,
        {
            "order_delivered_carrier_date",
            "carrier_handoff_at",
            "handed_to_carrier_at",
            "seller_handoff_at",
        },
    )
    estimated_delivery = _first_timestamp(
        shipment_data_all,
        {"order_estimated_delivery_date", "estimated_delivery_at", "delivery_deadline"},
    )
    delivered_at = _first_timestamp(
        shipment_data_all,
        {"order_delivered_customer_date", "delivered_at", "actual_delivery_at"},
    )
    seller_late = bool(
        seller_handoff_limit and carrier_handoff and carrier_handoff > seller_handoff_limit
    )
    logistics_late = bool(
        delivered_at
        and estimated_delivery
        and delivered_at > estimated_delivery
        and not seller_late
    )

    conflict_list: list[dict[str, Any]] = []
    if payment_mismatch:
        conflict_list.append(
            {
                "field": "payment_total_vs_order_total",
                "sources": ["order", "payment"],
                "selected_source": None,
                "resolution_code": "payment_total_mismatch",
            }
        )

    unavailable = order_status in {"unavailable", "notavailable", "itemunavailable"}
    canceled = order_status in {"canceled", "cancelled"}
    if canceled and paid_confirmed:
        primary_issue = "canceled_order_paid"
    elif unavailable and paid_confirmed:
        primary_issue = "unavailable_order_paid"
    elif duplicate_charge:
        primary_issue = "duplicate_charge"
    elif payment_mismatch:
        primary_issue = "payment_mismatch"
    elif "failed" in refund_statuses:
        primary_issue = "refund_failed"
    elif "pending" in refund_statuses or "processing" in refund_statuses:
        primary_issue = "refund_pending"
    elif seller_late:
        primary_issue = "late_delivery_seller"
    elif logistics_late:
        primary_issue = "late_delivery_logistics"
    elif valid_split:
        primary_issue = "valid_split_payment"
    elif any(
        claim.get("topic") == "unsupported_claim"
        for claim in case.get("customer_request", {}).get("claims", [])
    ):
        primary_issue = "unsupported_claim"
    else:
        primary_issue = "insufficient_evidence"

    required_domains = {
        "canceled_order_paid": {"order", "payment"},
        "unavailable_order_paid": {"order", "payment"},
        "duplicate_charge": {"payment"},
        "payment_mismatch": {"order", "payment"},
        "refund_failed": {"refund"},
        "refund_pending": {"refund"},
        "late_delivery_seller": {"shipment"},
        "late_delivery_logistics": {"shipment"},
        "valid_split_payment": {"payment", "order"},
    }.get(primary_issue, set())
    missing_count = sum(not ledger.by_domain(domain) for domain in required_domains)
    warning_count = sum(len(record.warnings) for record in ledger.all())
    if primary_issue == "unsupported_claim":
        confidence = 0.25
    elif primary_issue == "insufficient_evidence":
        confidence = 0.20
    else:
        confidence = 0.90 - 0.15 * missing_count - 0.10 * (len(conflict_list) + warning_count)
        confidence = max(0.05, min(0.99, confidence))

    if primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
        refund_total = _round_money(order_total or payment_total)
        refund_reason = primary_issue
    elif primary_issue in {"duplicate_charge", "payment_mismatch"}:
        refund_total = _round_money(max(payment_total - (order_total or ZERO), ZERO))
        refund_reason = (
            "duplicate_charge" if primary_issue == "duplicate_charge" else "payment_mismatch"
        )
    elif primary_issue in {"refund_failed", "refund_pending"}:
        refund_total = refund_requested
        refund_reason = primary_issue
    else:
        refund_total = ZERO
        refund_reason = primary_issue

    refund_lines: tuple[dict[str, Any], ...] = ()
    if refund_total > ZERO:
        refund_lines = (
            {
                "reason_code": refund_reason,
                "amount_brl": refund_total,
                "entity_id": order_ids[0] if order_ids else None,
            },
        )

    responsible_map: dict[str, tuple[tuple[str, str | None], ...]] = {
        "canceled_order_paid": (("seller", seller_ids[0] if seller_ids else None),),
        "unavailable_order_paid": (("seller", seller_ids[0] if seller_ids else None),),
        "duplicate_charge": (("payment_provider", None),),
        "payment_mismatch": (("payment_provider", None),),
        "refund_failed": (("payment_provider", None),),
        "refund_pending": (("payment_provider", None),),
        "late_delivery_seller": (("seller", seller_ids[0] if seller_ids else None),),
        "late_delivery_logistics": (("logistics_provider", None),),
        "valid_split_payment": (),
        "unsupported_claim": (("unknown", None),),
        "insufficient_evidence": (("unknown", None),),
    }
    actions_map = {
        "canceled_order_paid": ("approve_full_refund", "notify_customer", "review_seller"),
        "unavailable_order_paid": ("approve_full_refund", "notify_customer", "review_seller"),
        "duplicate_charge": ("refund_duplicate_amount", "reconcile_payment"),
        "payment_mismatch": ("reconcile_payment", "hold_refund_until_verified"),
        "refund_failed": ("retry_refund", "notify_customer"),
        "refund_pending": ("monitor_refund", "notify_customer"),
        "late_delivery_seller": ("review_seller", "notify_customer"),
        "late_delivery_logistics": ("review_logistics_provider", "notify_customer"),
        "valid_split_payment": ("close_case_no_action",),
        "unsupported_claim": ("request_supporting_evidence",),
        "insufficient_evidence": ("investigate_missing_evidence",),
    }
    cause = {"cause_code": primary_issue.upper(), "rank": 1}
    relevant_domains = required_domains or {record.domain for record in ledger.all()}
    refs = _evidence_refs(ledger, relevant_domains)
    claims = case.get("customer_request", {}).get("claims", [])
    if not isinstance(claims, list):
        claims = []

    return Decision(
        primary_issue=primary_issue,
        case_status=(
            "no_action"
            if primary_issue == "valid_split_payment"
            else "needs_investigation"
            if primary_issue in {"unsupported_claim", "insufficient_evidence"}
            else "action_required"
        ),
        confidence=round(confidence, 4),
        order_ids=order_ids,
        item_ids=item_ids,
        seller_ids=seller_ids,
        payment_references=payment_references,
        shipment_ids=shipment_ids,
        claim_assessments=_issue_assessment(claims, primary_issue, confidence, refs, refund_total),
        ranked_causes=(cause,),
        responsible_parties=responsible_map[primary_issue],
        evidence_refs=refs,
        data_conflicts=tuple(conflict_list),
        refund_total=refund_total,
        refund_lines=refund_lines,
        resolution_actions=actions_map[primary_issue],
    )
