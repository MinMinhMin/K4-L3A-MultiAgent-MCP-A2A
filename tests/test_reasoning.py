"""Regression tests for the classification failures seen in the first scored
submission: the Policy Agent confirming the customer's claimed issue and still
naming a secondary anomaly as primary_issue, hedging to insufficient_evidence
with evidence in hand, and citing too narrow a slice of the evidence."""

from __future__ import annotations

from pathlib import Path

from student_agent.agents import DomainEvidence, reconcile_primary_issue, verify_and_finalize
from student_agent.contracts import Contracts
from student_agent.facts import build_case_facts, late_handover_sellers

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"
LATE_SELLER = "seller-late00000001"
ON_TIME_SELLER = "seller-ontime000002"

POLICY_RULES = {
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "refund_brl": 18.0,
        "responsible_parties": [{"party_id": "seller-template", "party_type": "seller"}],
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "refund_brl": 35.0,
        "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_id": None, "party_type": "customer"}],
    },
}


def _ev(domain: str, data: object, suffix: str) -> DomainEvidence:
    return DomainEvidence(domain, f"ev_{domain}_{suffix}_{'x' * 20}", data, entity_id=ORDER_ID)


def _evidence() -> dict[str, list[DomainEvidence]]:
    order = {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-1",
        "order_status": "delivered",
        "order_delivered_carrier_date": "2018-01-10T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-20T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-15T09:00:00-03:00",
    }
    items = [
        {
            "order_id": ORDER_ID,
            "order_item_id": "item-1",
            "seller_id": LATE_SELLER,
            "shipping_limit_date": "2018-01-05T09:00:00-03:00",
            "price": "79.00",
            "freight_value": "18.00",
        },
        {
            "order_id": ORDER_ID,
            "order_item_id": "item-2",
            "seller_id": ON_TIME_SELLER,
            "shipping_limit_date": "2018-01-12T09:00:00-03:00",
            "price": "20.00",
            "freight_value": "5.00",
        },
    ]
    payments = [
        {
            "order_id": ORDER_ID,
            "payment_sequential": "1",
            "payment_type": "credit_card",
            "payment_value": "97.00",
        },
        # an incidental extra row — the kind of secondary anomaly that pulled
        # the LLM toward payment_mismatch in the scored run
        {
            "order_id": ORDER_ID,
            "payment_sequential": "2",
            "payment_type": "voucher",
            "payment_value": "35.00",
        },
    ]
    return {
        "order": [_ev("order", order, "o")],
        "item": [_ev("item", items, "i")],
        "seller": [_ev("seller", [{"seller_id": LATE_SELLER}, {"seller_id": ON_TIME_SELLER}], "s")],
        "product": [_ev("product", [{"product_id": "p-1"}], "p")],
        "payment": [_ev("payment", payments, "pay")],
        "payment_timeline": [
            _ev(
                "payment_timeline",
                {
                    "events": [
                        {
                            "event_at": "2018-01-02T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "97.00",
                            "status": "confirmed",
                        },
                    ]
                },
                "pt",
            )
        ],
        "shipment": [
            _ev(
                "shipment",
                {
                    "delivered_customer_at": "2018-01-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-01-15T09:00:00-03:00",
                    "events": [
                        {"event_type": "delivered_late", "actor": "seller", "status": "confirmed"}
                    ],
                },
                "sh",
            )
        ],
        "refund_timeline": [],
        "customer": [],
        "policy": [_ev("policy", {"rules": POLICY_RULES}, "pol")],
    }


CLAIMS = [
    {"claim_id": "claim-a", "topic": "late_delivery_seller"},
    {"claim_id": "claim-b", "topic": "requested_full_refund"},
]


def _draft(primary: str, hypotheses: list[dict], **overrides: object) -> dict:
    draft = {
        "hypotheses": hypotheses,
        "primary_issue": primary,
        "case_status": "action_required",
        "confidence": 0.7,
        "claim_assessments": [
            {
                "claim_id": "claim-a",
                "verdict": "supported",
                "confidence": 0.9,
                "supporting_domains": ["shipment"],
            },
            {
                "claim_id": "claim-b",
                "verdict": "partially_supported",
                "confidence": 0.6,
                "supporting_domains": ["payment"],
            },
        ],
        "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}],
        "responsible_parties": [],
        "data_conflicts": [],
        "financial_resolution": {"recommended_refund_brl": 0, "refund_lines": []},
        "resolution_actions": [],
    }
    draft.update(overrides)
    return draft


def _hyp(issue: str, assessment: str, *domains: str) -> dict:
    return {"issue": issue, "assessment": assessment, "supporting_domains": list(domains)}


def test_facts_compute_totals_lateness_and_late_seller() -> None:
    facts = build_case_facts(_evidence())
    assert facts["payments"]["payment_total"] == 132.0
    assert facts["items"]["items_plus_freight_total"] == 122.0
    assert facts["payments"]["paid_minus_items_plus_freight"] == 10.0
    assert facts["order"]["delivered_days_after_estimate"] == 5.0
    assert late_handover_sellers(facts) == [LATE_SELLER]
    assert facts["evidence_available"]["refund_timeline"] is False


def test_confirmed_claim_beats_secondary_anomaly() -> None:
    """Case 003 pattern: claim confirmed, but LLM named payment_mismatch."""
    evidence = _evidence()
    draft = _draft(
        "payment_mismatch",
        [
            _hyp("late_delivery_seller", "confirmed", "shipment", "item"),
            _hyp("payment_mismatch", "confirmed", "payment"),
        ],
        ranked_causes=[
            {"cause_code": "PAYMENT_MISMATCH", "rank": 1},
            {"cause_code": "LATE_DELIVERY_SELLER", "rank": 2},
        ],
    )
    output = verify_and_finalize("L3A_CASE_003", CLAIMS, evidence, draft)
    Contracts(ROOT / "contracts" / "schemas").validate_output(output, "case 003 pattern")

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["assessment"]["confidence"] == 0.9
    assert output["root_cause_analysis"]["ranked_causes"][0]["cause_code"] == "LATE_DELIVERY_SELLER"
    # the template party id from another case is never copied; the seller who
    # actually handed over late is resolved from this case's own evidence
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": LATE_SELLER}
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0
    assert output["resolution_actions"] == ["refund_freight"]

    claim_a, claim_b = output["claim_assessments"]
    assert claim_a["verdict"] == "supported"
    # only the freight is refunded, so the full-refund request is partial
    assert claim_b["verdict"] == "partially_supported"
    cited = set(output["evidence_refs"])
    for domain in ("order", "shipment", "item", "seller", "policy"):
        assert evidence[domain][0].evidence_ref in cited
    # unrelated domains are never cited
    assert evidence["product"][0].evidence_ref not in cited


def test_contradicted_claim_is_unsupported_not_insufficient() -> None:
    """Case 010 pattern: the LLM refuted the complaint but hedged."""
    draft = _draft(
        "insufficient_evidence",
        [_hyp("late_delivery_seller", "contradicted", "shipment", "order")],
    )
    output = verify_and_finalize("L3A_CASE_010", CLAIMS, _evidence(), draft)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_valid_split_payment_claim_resolution() -> None:
    claims = [
        {"claim_id": "claim-a", "topic": "valid_split_payment"},
        {"claim_id": "claim-b", "topic": "requested_full_refund"},
    ]
    draft = _draft(
        "payment_mismatch",
        [
            _hyp("valid_split_payment", "confirmed", "payment", "payment_timeline"),
            _hyp("payment_mismatch", "contradicted", "payment"),
        ],
    )
    output = verify_and_finalize("L3A_CASE_005", claims, _evidence(), draft)
    Contracts(ROOT / "contracts" / "schemas").validate_output(output, "split payment")

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["refund_lines"] == []
    # the customer's complaint is not upheld
    assert [c["verdict"] for c in output["claim_assessments"]] == ["unsupported", "unsupported"]
    # a null party id in the policy rule stays null (no entity to name)
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "customer", "party_id": None}
    ]


def test_reconcile_rules() -> None:
    core = True
    assert reconcile_primary_issue(
        "payment_mismatch", ["duplicate_charge"], {"duplicate_charge": "confirmed"}, core
    ) == ("duplicate_charge", "claim_confirmed")
    # a contradicted no-fault label means something *did* go wrong: never
    # turned into unsupported_claim
    assert reconcile_primary_issue(
        "late_delivery_logistics",
        ["unsupported_claim"],
        {"unsupported_claim": "contradicted"},
        core,
    ) == ("late_delivery_logistics", "llm")
    # insufficient_evidence only when the evidence genuinely is not there
    assert reconcile_primary_issue(
        "insufficient_evidence", ["refund_failed"], {"refund_failed": "insufficient_data"}, core
    ) == ("insufficient_evidence", "insufficient")
    assert reconcile_primary_issue("canceled_order_paid", ["canceled_order_paid"], {}, False) == (
        "canceled_order_paid",
        "llm",
    )
