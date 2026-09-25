from __future__ import annotations

from decimal import Decimal
from typing import Any

from student_agent.evidence import EvidenceLedger, EvidenceRecord
from student_agent.policy import analyze_case


def case_with_claim(topic: str) -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "o-1",
            "claims": [{"claim_id": "claim-1", "topic": topic}],
        },
        "policy_version": "EC_POLICY_V1",
    }


def ledger_for(**domains: Any) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for index, (domain, data) in enumerate(domains.items(), start=1):
        ledger.add(
            EvidenceRecord(
                tool_name=f"get_{domain}",
                actor=f"{domain}-agent",
                evidence_ref=f"ev_{index:020d}",
                domain=domain,
                data=data,
                warnings=(),
            )
        )
    return ledger


def test_canceled_paid_order_recommends_full_order_refund() -> None:
    decision = analyze_case(
        case_with_claim("canceled_order_paid"),
        ledger_for(
            order={
                "order_status": "canceled",
                "order_id": "o-1",
                "customer_id": "c-1",
                "order_amount": "42.50",
            },
            payment={
                "payments": [
                    {"payment_reference": "p-1", "payment_value": "42.50", "payment_status": "paid"}
                ]
            },
            item={"items": [{"order_item_id": "1", "seller_id": "s-1", "price": "42.50"}]},
        ),
    )

    assert decision.primary_issue == "canceled_order_paid"
    assert decision.refund_total == Decimal("42.50")
    assert decision.responsible_parties == (("seller", "s-1"),)
    assert decision.refund_lines == (
        {"reason_code": "canceled_order_paid", "amount_brl": Decimal("42.50"), "entity_id": "o-1"},
    )


def test_shipment_delay_after_handoff_assigns_logistics() -> None:
    decision = analyze_case(
        case_with_claim("late_delivery_logistics"),
        ledger_for(
            order={"order_id": "o-1", "order_status": "delivered"},
            shipment={
                "shipment_id": "sh-1",
                "shipping_limit_date": "2018-01-05T00:00:00-03:00",
                "order_delivered_carrier_date": "2018-01-05T00:00:00-03:00",
                "order_estimated_delivery_date": "2018-01-10T00:00:00-03:00",
                "order_delivered_customer_date": "2018-01-12T00:00:00-03:00",
            },
        ),
    )

    assert decision.primary_issue == "late_delivery_logistics"
    assert decision.responsible_parties == (("logistics_provider", None),)
    assert decision.refund_total == Decimal("0")


def test_shipment_delay_before_handoff_assigns_seller() -> None:
    decision = analyze_case(
        case_with_claim("late_delivery_seller"),
        ledger_for(
            order={"order_id": "o-1", "order_status": "delivered"},
            shipment={
                "shipping_limit_date": "2018-01-05T00:00:00-03:00",
                "order_delivered_carrier_date": "2018-01-07T12:00:00-03:00",
                "order_estimated_delivery_date": "2018-01-10T00:00:00-03:00",
                "order_delivered_customer_date": "2018-01-12T00:00:00-03:00",
            },
            item={"items": [{"order_item_id": "1", "seller_id": "s-1"}]},
        ),
    )

    assert decision.primary_issue == "late_delivery_seller"
    assert decision.responsible_parties == (("seller", "s-1"),)


def test_payment_mismatch_creates_conflict_and_reduces_confidence() -> None:
    decision = analyze_case(
        case_with_claim("payment_mismatch"),
        ledger_for(
            order={"order_id": "o-1", "order_total": "40.00"},
            payment={
                "payments": [
                    {"payment_reference": "p-1", "payment_value": "60.00", "payment_status": "paid"}
                ]
            },
        ),
    )

    assert decision.primary_issue == "payment_mismatch"
    assert decision.confidence < 0.9
    assert decision.data_conflicts == (
        {
            "field": "payment_total_vs_order_total",
            "sources": ["order", "payment"],
            "selected_source": None,
            "resolution_code": "payment_total_mismatch",
        },
    )
    assert decision.refund_total == Decimal("20.00")


def test_unsupported_claim_has_no_invented_refund() -> None:
    decision = analyze_case(
        case_with_claim("unsupported_claim"),
        ledger_for(
            order={"order_id": "o-1", "order_status": "delivered", "order_total": "40.00"},
            payment={
                "payments": [
                    {"payment_reference": "p-1", "payment_value": "40.00", "payment_status": "paid"}
                ]
            },
        ),
    )

    assert decision.primary_issue == "unsupported_claim"
    assert decision.case_status == "needs_investigation"
    assert decision.refund_total == Decimal("0")
    assert decision.refund_lines == ()
    assert decision.claim_assessments[0]["verdict"] == "unsupported"


def test_nested_records_numeric_strings_and_exact_refund_lines() -> None:
    decision = analyze_case(
        case_with_claim("unavailable_order_paid"),
        ledger_for(
            order={
                "result": {
                    "order_id": "o-1",
                    "order_status": "unavailable",
                    "total_amount": "12.30",
                }
            },
            payment={"rows": [{"transaction_id": "p-1", "amount": "12.30", "status": "captured"}]},
            item={"rows": [{"item_id": "i-1", "seller_id": "s-1", "price": "12.30"}]},
        ),
    )

    assert decision.primary_issue == "unavailable_order_paid"
    assert decision.order_ids == ("o-1",)
    assert decision.item_ids == ("i-1",)
    assert decision.payment_references == ("p-1",)
    assert sum(line["amount_brl"] for line in decision.refund_lines) == decision.refund_total


def test_valid_split_payment_is_not_reported_as_mismatch() -> None:
    decision = analyze_case(
        case_with_claim("valid_split_payment"),
        ledger_for(
            order={"order_id": "o-1", "order_total": "100.00", "order_status": "delivered"},
            payment={
                "payments": [
                    {
                        "payment_reference": "p-1",
                        "payment_value": "40.00",
                        "payment_status": "paid",
                    },
                    {
                        "payment_reference": "p-2",
                        "payment_value": "60.00",
                        "payment_status": "paid",
                    },
                ]
            },
        ),
    )

    assert decision.primary_issue == "valid_split_payment"
    assert decision.case_status == "no_action"
    assert decision.refund_total == Decimal("0")
