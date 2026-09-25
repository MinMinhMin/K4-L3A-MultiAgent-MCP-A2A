from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


def valid_evidence(evidence_ref: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": evidence_ref,
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
    }


@dataclass
class SnakeCaseResult:
    is_error: bool = False
    structured_content: dict[str, object] | None = None
    content: list[object] = field(default_factory=list)


class RecordingSession:
    def __init__(self, result: SnakeCaseResult) -> None:
        self.result = result
        self.arguments: dict[str, str] | None = None
        self.tool_name: str | None = None

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> SnakeCaseResult:
        self.tool_name = tool_name
        self.arguments = arguments
        return self.result


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_gateway_accepts_mcp_sdk_snake_case_result(contracts: Contracts) -> None:
    evidence = valid_evidence("ev_abcdefghijklmnopqrst", "order", {"order_id": "o-1"})
    session = RecordingSession(SnakeCaseResult(structured_content=evidence))
    gateway = EvidenceGateway(session, contracts)

    result = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="o-1"))

    assert result == evidence
    assert session.tool_name == "get_order"
    assert session.arguments == {"case_id": "CASE_001", "order_id": "o-1"}


def test_gateway_reports_snake_case_tool_error(contracts: Contracts) -> None:
    result = SnakeCaseResult(
        is_error=True,
        content=[SimpleNamespace(text="server refused the request")],
    )
    gateway = EvidenceGateway(RecordingSession(result), contracts)

    with pytest.raises(RuntimeError, match="server refused the request"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="o-1"))
