"""Phase 4: cross-field consistency and confidence calibration in the Verifier."""

from __future__ import annotations

from pathlib import Path

from student_agent.agents import DomainEvidence, verify_and_finalize
from student_agent.contracts import Contracts

ROOT = Path(__file__).resolve().parents[1]


def _contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


def _evidence(**domains: str) -> dict[str, list[DomainEvidence]]:
    return {
        domain: [DomainEvidence(domain, f"ev_{'a' * 20}_{domain}", {}, entity_id=entity_id)]
        for domain, entity_id in domains.items()
    }


def test_logistics_issue_rejects_seller_as_responsible_party() -> None:
    evidence = _evidence(order="ORD1", shipment="SHP1", seller="SEL1")
    draft = {
        "primary_issue": "late_delivery_logistics",
        "case_status": "action_required",
        "confidence": 0.95,
        "claim_assessments": [],
        "ranked_causes": [],
        "responsible_parties": [{"party_type": "seller", "party_id_domain": "seller"}],
        "data_conflicts": [],
        "financial_resolution": {"recommended_refund_brl": 0, "refund_lines": []},
        "resolution_actions": [],
    }
    output = verify_and_finalize("L3A_CASE_901", [], evidence, draft)
    _contracts().validate_output(output, "logistics case")

    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "unknown", "party_id": None}]
    assert any(
        c["resolution_code"] == "PARTY_TYPE_INCONSISTENT_WITH_PRIMARY_ISSUE"
        for c in output["data_conflicts"]
    )
    # A logically-inconsistent draft must not keep its original high confidence.
    assert output["assessment"]["confidence"] <= 0.5


def test_unsupported_claim_forces_no_action_and_zero_refund() -> None:
    evidence = _evidence(order="ORD1")
    draft = {
        "primary_issue": "unsupported_claim",
        "case_status": "action_required",
        "confidence": 0.8,
        "claim_assessments": [],
        "ranked_causes": [],
        "responsible_parties": [],
        "data_conflicts": [],
        "financial_resolution": {
            "recommended_refund_brl": 200,
            "refund_lines": [
                {"reason_code": "full_refund", "amount_brl": 200, "entity_domain": "order"}
            ],
        },
        "resolution_actions": [],
    }
    output = verify_and_finalize("L3A_CASE_902", [], evidence, draft)
    _contracts().validate_output(output, "unsupported claim case")

    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["refund_lines"] == []
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_confidence_capped_when_evidence_conflicts() -> None:
    evidence = _evidence(order="ORD1", payment="PAY1")
    draft = {
        "primary_issue": "payment_mismatch",
        "case_status": "action_required",
        "confidence": 0.99,
        "claim_assessments": [],
        "ranked_causes": [],
        "responsible_parties": [],
        "data_conflicts": [
            {
                "field": "amount",
                "sources": ["order.total", "payment.amount"],
                "selected_source": "payment.amount",
                "resolution_code": "PREFER_PAYMENT",
            }
        ],
        "financial_resolution": {"recommended_refund_brl": 0, "refund_lines": []},
        "resolution_actions": [],
    }
    output = verify_and_finalize("L3A_CASE_903", [], evidence, draft)
    _contracts().validate_output(output, "conflicting evidence case")
    assert output["assessment"]["confidence"] <= 0.6
