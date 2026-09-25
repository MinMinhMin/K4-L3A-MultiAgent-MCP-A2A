from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .llm import get_policy_llm
from .mcp_gateway import EvidenceGateway, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

DEFAULT_CASE_CONCURRENCY = 3
MAX_CASE_CONCURRENCY = 20


def _root(value: str) -> Path:
    return Path(value).resolve()


def _case_concurrency(override: int | None) -> int:
    if override is not None:
        value = override
    else:
        raw = os.getenv("CASE_CONCURRENCY", "").strip()
        value = int(raw) if raw.isdigit() else DEFAULT_CASE_CONCURRENCY
    return max(1, min(MAX_CASE_CONCURRENCY, value))


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


MAX_GATEWAY_ATTEMPTS = 5
CIRCUIT_BREAKER_FAILURES = 6


def _is_valid_existing_output(contracts: Contracts, path: Path, case_id: str) -> bool:
    """A previously written output only counts as "done" if it still parses,
    still passes the schema, and actually reached a determination — never trust
    a possibly-corrupt leftover file as complete, and give an
    `insufficient_evidence` result (typically the product of evidence that was
    unavailable during a server outage) another attempt on resume.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("case_id") != case_id:
        return False
    if (data.get("assessment") or {}).get("primary_issue") == "insufficient_evidence":
        return False
    try:
        contracts.validate_output(data, f"outputs/{case_id}.json")
    except ValueError:
        return False
    return True


@dataclass
class _PassState:
    """Outcome of one pass over the pending cases. After enough consecutive
    case failures the gateway is treated as down for the rest of the pass
    (a circuit breaker), so an outage costs a handful of failed calls per pass
    instead of two failed calls for every remaining case."""

    completed: int = 0
    consecutive_failures: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def tripped(self) -> bool:
        return self.consecutive_failures >= CIRCUIT_BREAKER_FAILURES


async def _solve_and_write_case(
    case_id: str,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    contracts: Contracts,
    output_root: Path,
    semaphore: asyncio.Semaphore,
    state: _PassState,
) -> None:
    """Solve one case and write its output. A per-case failure (the gateway
    failing on required evidence, a bad draft, ...) is recorded and skipped
    rather than raised: with several cases running concurrently, one bad case
    must not cancel every other in-flight case and lose their work (an
    `asyncio.TaskGroup` cancels all siblings the instant any task raises). The
    case simply stays without an output file and is retried on the next pass.
    """
    async with semaphore:
        if state.tripped:
            return
        try:
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        except (OSError, RuntimeError, ValueError, httpx2.HTTPError) as exc:
            state.consecutive_failures += 1
            state.failed[case_id] = str(exc)
        else:
            state.consecutive_failures = 0
            state.completed += 1


async def _run(root: Path, concurrency: int | None = None, fresh: bool = False) -> None:
    settings = Settings.load(root)
    get_policy_llm()  # fail fast on a missing/invalid OPENAI_API_KEY before spending MCP calls
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    if fresh:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    else:
        # Resume: keep whatever already-valid outputs exist (e.g. from a run
        # interrupted by a dropped MCP connection) and only drop leftovers that
        # don't belong to this case-set or fail the schema — a case is only
        # ever considered "done" if it is genuinely done. The trace file is
        # appended to (TraceWriter opens in "a" mode), never truncated here.
        expected_ids = set(case_set.case_ids)
        for existing in output_root.glob("*.json"):
            case_id = existing.stem
            if case_id not in expected_ids or not _is_valid_existing_output(
                contracts, existing, case_id
            ):
                existing.unlink()

    trace = TraceWriter(trace_path, contracts)

    limit = _case_concurrency(concurrency)
    semaphore = asyncio.Semaphore(limit)

    def pending_case_ids() -> list[str]:
        return [
            case_id
            for case_id in case_set.case_ids
            if not (output_root / f"{case_id}.json").exists()
        ]

    already_done = len(case_set.case_ids) - len(pending_case_ids())
    if already_done:
        print(f"Resuming: {already_done}/{len(case_set.case_ids)} case(s) already done")

    # A dropped MCP connection kills the *shared* session's background stream,
    # not just the one request in flight — that can surface outside any single
    # case's own error handling. Rather than trust every transient failure mode
    # is catchable per-case, reconnect from scratch and resume only the cases
    # that don't have an output yet (checked on disk, not tracked in memory).
    last_error = ""
    for attempt in range(1, MAX_GATEWAY_ATTEMPTS + 1):
        pending = pending_case_ids()
        if not pending:
            break
        if attempt > 1:
            await asyncio.sleep(min(2**attempt, 30))
        state = _PassState()
        connection_dropped = False
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                async with asyncio.TaskGroup() as group:
                    for case_id in pending:
                        group.create_task(
                            _solve_and_write_case(
                                case_id,
                                case_set.cases[case_id],
                                gateway,
                                trace,
                                contracts,
                                output_root,
                                semaphore,
                                state,
                            )
                        )
        except* (OSError, RuntimeError, ValueError, httpx2.HTTPError) as eg:
            connection_dropped = True
            last_error = str(eg.exceptions[0])
            print(
                f"WARN: pass {attempt}/{MAX_GATEWAY_ATTEMPTS}: MCP connection dropped: "
                f"{last_error[:160]}",
                file=sys.stderr,
            )
        if not connection_dropped and state.failed:
            last_error = next(iter(state.failed.values()))
            status = "gateway looks down, pausing" if state.tripped else "will retry"
            print(
                f"WARN: pass {attempt}/{MAX_GATEWAY_ATTEMPTS}: {state.completed} case(s) done, "
                f"{len(state.failed)} failed ({status}), e.g. {last_error[:160]}",
                file=sys.stderr,
            )

    remaining = pending_case_ids()
    if remaining:
        preview = ", ".join(remaining[:5]) + (" ..." if len(remaining) > 5 else "")
        raise RuntimeError(
            f"{len(remaining)} case(s) not completed after {MAX_GATEWAY_ATTEMPTS} passes "
            f"({preview}); no output was written for them. Last error: {last_error[:200]}"
        )
    print(f"OK: {len(case_set.case_ids)} cases solved")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"cases to solve concurrently (default: {DEFAULT_CASE_CONCURRENCY}, "
        f"env CASE_CONCURRENCY, max {MAX_CASE_CONCURRENCY})",
    )
    run.add_argument(
        "--fresh",
        action="store_true",
        help="discard any existing outputs/trace and start over (default: resume, "
        "keeping already-completed cases)",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.concurrency, args.fresh))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError, httpx2.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
