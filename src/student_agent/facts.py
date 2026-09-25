"""Deterministic facts derived from a case's collected MCP evidence.

The Policy Agent's LLM is good at weighing evidence but unreliable at arithmetic
over nested JSON (summing payments, comparing timestamps across domains). These
facts do that work up front so the LLM reasons over the whole picture — totals,
date gaps, duplicate records, who an event is attributed to — instead of
latching onto whichever anomaly it happens to read first. Every fact is computed
only from evidence actually returned by MCP for this case; nothing is guessed,
and a fact whose inputs are missing is simply left out.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any


def _records(evidence_by_domain: dict[str, list[Any]], domain: str) -> list[dict[str, Any]]:
    """Flatten a domain's evidence payloads into a list of dict records."""
    records: list[dict[str, Any]] = []
    for item in evidence_by_domain.get(domain, []):
        data = item.data
        if isinstance(data, dict):
            records.append(data)
        elif isinstance(data, list):
            records.extend(row for row in data if isinstance(row, dict))
    return records


def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_between(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    try:
        return round((later - earlier).total_seconds() / 86400, 1)
    except TypeError:  # naive vs aware timestamps
        return None


def _events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in records:
        nested = record.get("events")
        if isinstance(nested, list):
            events.extend(event for event in nested if isinstance(event, dict))
        elif "event_type" in record:
            events.append(record)
    return events


def _event_summary(events: list[dict[str, Any]], *, with_actor: bool) -> list[dict[str, Any]]:
    summary = []
    for event in events:
        entry: dict[str, Any] = {
            "at": event.get("event_at"),
            "type": event.get("event_type"),
            "status": event.get("status"),
        }
        amount = _money(event.get("amount_brl"))
        if amount is not None:
            entry["amount_brl"] = amount
        if with_actor:
            entry["actor"] = event.get("actor")
        summary.append(entry)
    return summary


def _order_facts(order: dict[str, Any]) -> dict[str, Any]:
    estimated = _when(order.get("order_estimated_delivery_date"))
    delivered = _when(order.get("order_delivered_customer_date"))
    facts: dict[str, Any] = {
        "order_status": order.get("order_status"),
        "delivered_to_customer": delivered is not None,
    }
    days_late = _days_between(delivered, estimated)
    if days_late is not None:
        facts["delivered_days_after_estimate"] = days_late
    return facts


def _item_facts(items: list[dict[str, Any]], carrier_handover: datetime | None) -> dict[str, Any]:
    prices = [_money(item.get("price")) for item in items]
    freights = [_money(item.get("freight_value")) for item in items]
    item_ids = Counter(item.get("order_item_id") for item in items if item.get("order_item_id"))
    facts: dict[str, Any] = {
        "item_rows": len(items),
        "item_price_total": round(sum(p for p in prices if p is not None), 2),
        "freight_total": round(sum(f for f in freights if f is not None), 2),
        "sellers": sorted({item["seller_id"] for item in items if item.get("seller_id")}),
    }
    facts["items_plus_freight_total"] = round(facts["item_price_total"] + facts["freight_total"], 2)
    repeated = sorted(item_id for item_id, count in item_ids.items() if count > 1)
    if repeated:
        facts["repeated_order_item_ids"] = repeated
    if carrier_handover is not None:
        late_handover = []
        for item in items:
            limit = _when(item.get("shipping_limit_date"))
            gap = _days_between(carrier_handover, limit)
            if gap is not None and gap > 0:
                late_handover.append(
                    {"seller_id": item.get("seller_id"), "days_after_shipping_limit": gap}
                )
        facts["seller_handover_after_shipping_limit"] = late_handover
    return facts


def _payment_facts(payments: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_money(payment.get("payment_value")) for payment in payments]
    records = [
        (
            str(payment.get("payment_sequential")),
            str(payment.get("payment_type")),
            _money(payment.get("payment_value")),
        )
        for payment in payments
    ]
    same_charge = Counter((ptype, value) for _, ptype, value in records)
    same_sequential = Counter(seq for seq, _, _ in records)
    return {
        "payment_records": len(payments),
        "payment_total": round(sum(v for v in values if v is not None), 2),
        "payment_types": sorted({ptype for _, ptype, _ in records}),
        "distinct_payment_sequentials": len(same_sequential),
        "identical_type_and_amount_records": [
            {"payment_type": ptype, "amount_brl": value, "count": count}
            for (ptype, value), count in same_charge.items()
            if count > 1
        ],
    }


def _payment_event_facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    captured_amounts: Counter[float] = Counter()
    for event in events:
        key = f"{event.get('event_type')}:{event.get('status')}"
        amount = _money(event.get("amount_brl")) or 0.0
        slot = by_type.setdefault(key, {"count": 0, "amount_brl": 0.0})
        slot["count"] += 1
        slot["amount_brl"] = round(slot["amount_brl"] + amount, 2)
        if event.get("event_type") == "captured":
            captured_amounts[amount] += 1
    return {
        "payment_events_by_type_status": by_type,
        "repeated_capture_amounts": sorted(
            amount for amount, count in captured_amounts.items() if count > 1
        ),
    }


def build_case_facts(evidence_by_domain: dict[str, list[Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "evidence_available": {domain: bool(items) for domain, items in evidence_by_domain.items()}
    }

    orders = _records(evidence_by_domain, "order")
    shipments = _records(evidence_by_domain, "shipment")
    carrier_handover = None
    if orders:
        facts["order"] = _order_facts(orders[0])
        carrier_handover = _when(orders[0].get("order_delivered_carrier_date"))
    if carrier_handover is None and shipments:
        carrier_handover = _when(shipments[0].get("delivered_carrier_at"))

    items = _records(evidence_by_domain, "item")
    if items:
        facts["items"] = _item_facts(items, carrier_handover)

    payments = _records(evidence_by_domain, "payment")
    if payments:
        facts["payments"] = _payment_facts(payments)
        items_total = facts.get("items", {}).get("items_plus_freight_total")
        if items_total is not None:
            facts["payments"]["paid_minus_items_plus_freight"] = round(
                facts["payments"]["payment_total"] - items_total, 2
            )

    payment_events = _events(_records(evidence_by_domain, "payment_timeline"))
    if payment_events:
        facts["payment_timeline"] = {
            "events": _event_summary(payment_events, with_actor=False),
            **_payment_event_facts(payment_events),
        }

    if shipments:
        shipment = shipments[0]
        delivered = _when(shipment.get("delivered_customer_at"))
        estimated = _when(shipment.get("estimated_delivery_at"))
        shipment_facts: dict[str, Any] = {
            "events": _event_summary(_events(shipments), with_actor=True),
        }
        days_late = _days_between(delivered, estimated)
        if days_late is not None:
            shipment_facts["delivered_days_after_estimate"] = days_late
        facts["shipment"] = shipment_facts

    refund_events = _events(_records(evidence_by_domain, "refund_timeline"))
    if refund_events:
        facts["refund_timeline"] = {"events": _event_summary(refund_events, with_actor=False)}

    return facts


def late_handover_sellers(facts: dict[str, Any]) -> list[str]:
    """Sellers who handed an item to the carrier after its shipping limit."""
    late = facts.get("items", {}).get("seller_handover_after_shipping_limit", [])
    return sorted({entry["seller_id"] for entry in late if entry.get("seller_id")})
