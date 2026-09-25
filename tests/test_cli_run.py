"""`day09 run` during a gateway outage: every tool answers with a generic
server error. The run must not write confident-looking insufficient_evidence
outputs for cases it never actually investigated, and must stop hammering the
gateway instead of spending two failed calls on every remaining case."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent import VARIANT_ID, cli
from student_agent.agents import EvidenceUnavailableError
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str, key: str) -> SimpleNamespace:
    keys = ["case_id", key]
    return SimpleNamespace(
        name=name,
        description="",
        input_schema={"required": keys, "properties": dict.fromkeys(keys, {})},
    )


class OutageGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self) -> list[str]:
        return ["get_order", "get_policy"]

    async def list_tool_specs(self) -> list[Any]:
        return [_tool("get_order", "order_id"), _tool("get_policy", "policy_version")]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")


def _case(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "customer_request": {
            "claimed_order_id": "ORD-1",
            "claims": [{"claim_id": f"{case_id}-a", "topic": "canceled_order_paid"}],
        },
        "policy_version": "EC_POLICY_V1",
    }


async def _no_sleep(_seconds: float) -> None:
    return None


def test_solve_case_raises_instead_of_guessing_during_outage(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    with pytest.raises(EvidenceUnavailableError):
        asyncio.run(solve_case(_case("L3A_CASE_001"), OutageGateway(), trace))  # type: ignore[arg-type]


def test_run_writes_nothing_and_trips_breaker_during_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case_ids = [f"L3A_CASE_{n:03d}" for n in range(1, 101)]
    (tmp_path / "case-set.json").write_text(
        json.dumps({"case_set_version": "t", "variant_id": VARIANT_ID, "case_ids": case_ids}),
        encoding="utf-8",
    )
    (tmp_path / "inputs").mkdir()
    for case_id in case_ids:
        (tmp_path / "inputs" / f"{case_id}.json").write_text(
            json.dumps(_case(case_id)), encoding="utf-8"
        )
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")

    gateway = OutageGateway()

    @asynccontextmanager
    async def fake_connect(*_args: Any, **_kwargs: Any):
        yield gateway

    monkeypatch.setattr(
        cli.Settings,
        "load",
        classmethod(lambda _cls, _root=None: SimpleNamespace(mcp_endpoint="x", team_api_key="y")),
    )
    monkeypatch.setattr(cli, "get_policy_llm", lambda: None)
    monkeypatch.setattr(cli, "connect_gateway", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="100 case\\(s\\) not completed"):
        asyncio.run(cli._run(tmp_path, concurrency=3, fresh=True))

    assert list((tmp_path / "outputs").glob("*.json")) == []
    # Without the breaker: 100 cases x 2 attempts x 5 passes = 1000 failed calls.
    assert gateway.calls < 150
