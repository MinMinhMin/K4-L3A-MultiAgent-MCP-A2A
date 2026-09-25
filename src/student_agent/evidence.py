from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import Contracts
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class TransientEvidenceError(RuntimeError):
    """Marker for a gateway failure that may be retried once."""


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    actor: str
    evidence_ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...]


class EvidenceLedger:
    """Case-local store of validated MCP evidence envelopes."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def add(self, record: EvidenceRecord) -> None:
        self._records.setdefault(record.evidence_ref, record)

    def add_record(
        self,
        *,
        tool_name: str,
        actor: str,
        evidence: dict[str, Any],
        contracts: Contracts,
    ) -> EvidenceRecord:
        contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        record = EvidenceRecord(
            tool_name=tool_name,
            actor=actor,
            evidence_ref=evidence["evidence_ref"],
            domain=evidence["domain"],
            data=evidence["data"],
            warnings=tuple(evidence.get("warnings", ())),
        )
        self.add(record)
        return record

    def all(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())

    def refs(self) -> tuple[str, ...]:
        return tuple(self._records)

    def by_domain(self, domain: str) -> tuple[EvidenceRecord, ...]:
        return tuple(record for record in self._records.values() if record.domain == domain)

    def get(self, evidence_ref: str) -> EvidenceRecord | None:
        return self._records.get(evidence_ref)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, OSError, TransientEvidenceError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in ("timeout", "temporar", "unavailable", "429", "503"))


async def collect_tool(
    gateway: EvidenceGateway,
    ledger: EvidenceLedger,
    *,
    tool_name: str,
    actor: str,
    case_id: str,
    arguments: dict[str, str],
    trace: TraceWriter,
) -> EvidenceRecord | None:
    """Call one scoped tool, retry a transient failure once, and audit success."""

    for attempt in range(2):
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
        except Exception as exc:
            if attempt == 0 and _retryable(exc):
                continue
            return None

        record = EvidenceRecord(
            tool_name=tool_name,
            actor=actor,
            evidence_ref=evidence["evidence_ref"],
            domain=evidence["domain"],
            data=evidence["data"],
            warnings=tuple(evidence.get("warnings", ())),
        )
        ledger.add(record)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[record.evidence_ref],
            attributes={"retry_count": attempt},
        )
        return record

    return None
