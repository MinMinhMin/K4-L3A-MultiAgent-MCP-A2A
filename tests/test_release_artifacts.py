from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts

CASE_IDS = ("CASE_001", "CASE_002")


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def output(case_id: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [evidence_ref],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["investigate_missing_evidence"],
    }


def event(case_id: str, event_id: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-trace-event-v1",
        "event_id": event_id,
        "case_id": case_id,
        "event_type": "tool_result_consumed",
        "occurred_at": "2026-09-25T10:00:00Z",
        "actor": "order-agent",
        "tool_name": "get_order",
        "evidence_refs": [evidence_ref],
    }


def write_fixture(
    root: Path,
    outputs: dict[str, dict[str, Any]],
    trace_events: list[dict[str, Any]],
) -> CaseSet:
    (root / "outputs").mkdir(parents=True)
    (root / "traces").mkdir()
    for case_id, value in outputs.items():
        (root / "outputs" / f"{case_id}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )
    (root / "traces" / "trace.jsonl").write_text(
        "\n".join(json.dumps(value) for value in trace_events) + "\n", encoding="utf-8"
    )
    return CaseSet("test-v1", "l3a", CASE_IDS, {})


def test_validate_artifacts_rejects_unknown_output_evidence_ref(tmp_path: Path) -> None:
    known = "ev_abcdefghijklmnopqrst"
    unknown = "ev_zzzzzzzzzzzzzzzzzzzz"
    case_set = write_fixture(
        tmp_path,
        {
            case_id: output(case_id, known if case_id == "CASE_001" else unknown)
            for case_id in CASE_IDS
        },
        [
            event("CASE_001", "evt_abcdefghijkl", known),
            event("CASE_002", "evt_abcdefghijkm", "ev_yyyyyyyyyyyyyyyyyyyy"),
        ],
    )

    with pytest.raises(ValueError, match="unknown evidence_ref"):
        validate_artifacts(tmp_path, case_set, contracts())


def test_validate_artifacts_rejects_duplicate_trace_event_id(tmp_path: Path) -> None:
    evidence_ref = "ev_abcdefghijklmnopqrst"
    case_set = write_fixture(
        tmp_path,
        {case_id: output(case_id, evidence_ref) for case_id in CASE_IDS},
        [event(case_id, "evt_abcdefghijkl", evidence_ref) for case_id in CASE_IDS],
    )

    with pytest.raises(ValueError, match="duplicate event_id"):
        validate_artifacts(tmp_path, case_set, contracts())


def test_validate_artifacts_accepts_case_scoped_evidence(tmp_path: Path) -> None:
    refs = {"CASE_001": "ev_abcdefghijklmnopqrst", "CASE_002": "ev_uvwxyzabcdefghijklmn"}
    case_set = write_fixture(
        tmp_path,
        {case_id: output(case_id, refs[case_id]) for case_id in CASE_IDS},
        [
            event("CASE_001", "evt_abcdefghijkl", refs["CASE_001"]),
            event("CASE_002", "evt_abcdefghijkm", refs["CASE_002"]),
        ],
    )

    outputs, trace_lines = validate_artifacts(tmp_path, case_set, contracts())

    assert set(outputs) == set(CASE_IDS)
    assert len(trace_lines) == 2
