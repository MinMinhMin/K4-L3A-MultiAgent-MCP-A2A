# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
inputs/<case_id>.json
        │
        ▼
   Coordinator (workflow.solve_case)
        │ task_assigned / handoff
        ▼
Order/Item Agent ──┬── Payment Agent ──┬── Shipment Agent
        │           │                  │
        └────────── MCP Evidence Gateway (tool discovery, per-domain calls) ──┘
        │
        ▼
   Policy Agent (OpenAI-backed reasoning over collected evidence, src/student_agent/llm.py)
        │ policy_decided
        ▼
   Verifier Agent (agents.verify_and_finalize — reconciles the draft against real evidence)
        │ verification_completed
        ▼
   Output (outputs/<case_id>.json)         Trace (traces/trace.jsonl, emitted at every step above)
```

Implementation split:

- `workflow.solve_case` — Coordinator. Orchestrates handoffs and calls the functions below in order.
- `tool_catalog.py` — discovers MCP tools (`gateway.list_tool_specs()`) and resolves which tool/argument
  name to use per domain from the server's own schema, instead of hardcoding tool names.
- `agents.py` — Order/Item, Payment, Shipment and Policy evidence-gathering, plus the Verifier
  (including deterministic application of the MCP `get_policy` rule table — see §2 and §6).
- `llm.py` — the Policy Agent's OpenAI (`OPENAI_API_KEY`/`OPENAI_MODEL`) reasoning call and its
  strict JSON-schema response contract (per-issue `hypotheses` come first in the schema, so every
  issue is assessed before `primary_issue` is committed to).
- `facts.py` — deterministic facts computed from the collected evidence (payment/item totals, date
  gaps, repeated records, sellers who handed over late, event summaries) so the LLM weighs the whole
  picture instead of doing arithmetic over raw JSON.

**Live gateway shape** (confirmed by discovery against the running MCP endpoint, not assumed):
every specialist tool — `get_order_items`, `get_order_payments`, `get_payment_timeline`,
`get_shipment_summary`, `get_sellers`, `get_product_context`, `get_refund_timeline` — is keyed by
the *same* `order_id` the customer named, not by a separate child id chained from the order payload.
Only `get_customer_history` (`customer_unique_id`) and `get_policy` (`policy_version`) differ.
`agents.gather_order_scoped_evidence` reflects this: one call per domain per case, all against the
one verified `order_id`. `get_policy` itself returns a rule table keyed by `primary_issue`
(`case_status`, `recommended_action`, `refund_brl`, `responsible_parties`), which the Verifier
applies deterministically — see §6.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case` dict from `inputs/<case_id>.json` | Discover MCP tools once per case; sequence specialist handoffs; assemble evidence map; call Policy then Verifier; return final output | `task_assigned`/`handoff` trace events; final validated output dict |
| Order/Item | `claimed_order_id` from `customer_request` (not trusted as ground truth) | Resolve the order via the discovered "order" tool; once verified, fetch `item`/`seller`/`product` evidence for that same `order_id`. `customer_history` is fetched only when the order record carries a genuine `customer_unique_id` (its `customer_id` is a different identifier, and passing it made the tool fail on every case) | `DomainEvidence` list for `order`, `item`, `seller`, `product`, `customer`; `tool_result_consumed` events |
| Payment | The same verified `order_id` (never a guessed id) | Fetch `payment`, `payment_timeline` and `refund_timeline` evidence for that order | `DomainEvidence` list for `payment`, `payment_timeline`, `refund_timeline` |
| Shipment | The same verified `order_id` | Fetch shipment/logistics summary evidence | `DomainEvidence` list for `shipment` |
| Policy | Aggregated evidence (order/item/seller/product/payment/payment_timeline/shipment/refund_timeline/customer) + `policy_version` + `get_policy` evidence + deterministic `facts` (`facts.build_case_facts`: totals, date gaps, repeated records, late-handover sellers, event summaries) | Call OpenAI to assess **every** catalogue issue as a hypothesis (`confirmed` / `contradicted` / `insufficient_data` / `not_applicable`, with the deciding domains) *before* naming `primary_issue`; treat the customer's claimed issue as a hypothesis to verify, not a fact; also draft claim verdicts, root causes and data conflicts — pointing at evidence **by domain only**, never by raw ID or `evidence_ref` | Draft assessment dict; `policy_decided` trace event |
| Verifier | Policy Agent's draft + the same evidence map + facts + `get_policy`'s rule table | **Reconcile `primary_issue` with the Policy Agent's own hypotheses** (`agents.reconcile_primary_issue`, §6); look up `get_policy`'s rule for the final issue and use it — not the LLM's guess — for `case_status`, `resolution_actions`, `responsible_parties` and `financial_resolution`; align claim verdicts with the decision; cite the evidence groups the decision rests on; set a calibrated confidence; dedupe/cap arrays to schema limits | Final `l3a-output-v2` object; `verification_completed` trace event |

Tool permissions: each specialist only calls tools discovered for **its own domain keyword** (`ToolCatalog.tools_for(domain)` in `tool_catalog.py`) — Order/Item never calls the payment/shipment tool and vice versa. The Policy Agent's LLM call has no MCP tool access at all; it only reasons over evidence already fetched and validated by the specialists.

## 3. A2A protocol

- **Envelope**: an in-process async call chain (no network hop between agents); every hop is made
  observable by emitting a `trace-event-v1` record via `TraceWriter.emit`, never by passing free-text
  reasoning between agents.
- **Correlation**: every trace event carries the same `case_id` as the input case; `solve_case` never
  reuses evidence or IDs across cases (evidence is gathered fresh per call, per `case_id`).
- **Handoff conditions**: Coordinator → Order/Item is unconditional (it is the entry point that
  resolves entities). Coordinator → Payment/Shipment/the rest of Order/Item's own domains only fire
  their MCP calls when the order actually resolved (`order_evidence is not None`); if the claimed
  order doesn't exist, no downstream specialist call is made at all — nothing to chain from, so
  nothing is guessed. Coordinator → Policy always runs, but the Policy Agent immediately returns "no
  draft" if no order evidence exists at all. Coordinator → Verifier always runs, even when the Policy
  Agent produced no draft (deterministic `insufficient_evidence` fallback).
- **Timeout / loop avoidance**: each MCP call goes through `agents._call_with_retry`, which bounds
  every tool call — 3 attempts for transport errors, 2 for an error the tool itself returned — with
  fixed backoff (`0.5s`, `1.5s`). There is no unbounded loop and no agent calls itself or another
  specialist back (`handoff` is a straight line Coordinator → specialist → Coordinator, never
  specialist → specialist).
- **Observability boundary**: only decision codes, tool names and evidence refs are traced
  (`event_type`, `actor`, `target`, `decision_code`, `tool_name`, `evidence_refs`); the LLM prompt/
  response content is never written to the trace, per the no-chain-of-thought rule.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` invokes the MCP tool and validates the response against
   `mcp-evidence-response-v1.schema.json` before returning it (`contracts.validate_evidence`) — a
   malformed envelope raises instead of being used.
2. On success, the calling agent immediately emits `tool_result_consumed` with `evidence_refs`
   containing exactly the `evidence_ref` from that response — never a modified or synthesized ref.
3. Each fetched payload becomes a `DomainEvidence(domain, evidence_ref, data, entity_id)` kept only
   in that case's local `evidence_by_domain` dict — nothing is cached or shared across cases/case
   runs, so evidence can't leak between cases.
4. The Verifier cites evidence by **issue**, not by whatever one or two domains the LLM named:
   `ISSUE_EVIDENCE_DOMAINS` maps each issue to the evidence groups that establish or refute it
   (e.g. payment-family issues → order + payment + payment_timeline; delivery issues → order +
   shipment + item [+ seller]; refund issues → order + payment_timeline + refund_timeline). The case
   cites the groups for its primary issue, for every issue the customer claimed (the evidence that
   confirmed or refuted it), plus `policy` when a policy rule decided the resolution. A claim cites
   its own topic's groups; `requested_full_refund` cites payment + payment_timeline (+ policy).
   Domains unrelated to any of these (customer history, product catalogue) are never cited. A group
   with no `DomainEvidence` for this case contributes nothing, and a claim left with no refs is
   downgraded to `insufficient_evidence` rather than left pointing at nothing.
5. The final `evidence_refs` (top level and per-claim) are always a subset of refs actually returned
   by MCP calls made *during this case's run* — the Verifier never adds a ref that wasn't produced by
   `_call_with_retry` for this `case_id`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / transport error | Yes, up to 2 retries (3 attempts total), fixed backoff 0.5s/1.5s | Domain evidence treated as absent | `tool_result_consumed` with `decision_code="EVIDENCE_UNAVAILABLE"`, `attributes.error` |
| Tool returned an error | Once (2 attempts): such errors are usually deterministic (e.g. no refund history for this order), so more attempts only add latency and audited calls | Domain evidence treated as absent | `tool_result_consumed` with `decision_code="EVIDENCE_UNAVAILABLE"` |
| Not found | No (idempotent negative answer, retrying won't change it) | Same as above — absent evidence, never guessed | `tool_result_consumed` with `decision_code="EVIDENCE_UNAVAILABLE"` |
| Whole MCP connection drops mid-run | Yes: `cli._run` reconnects (up to 5 times, backoff 2–30s) and resumes only cases without an output | Cases already written are kept; on a later `day09 run` (resume mode), outputs that are invalid or `insufficient_evidence` are recomputed | stderr `WARN`, no trace event (no case-level decision was made) |
| Source conflict (two evidence payloads disagree on a field) | N/A (not a call failure) | Policy Agent reports it as a `data_conflict`; Verifier keeps it only if ≥2 real source labels are present, else drops the conflict entry | Recorded in the case output's `data_conflicts`, not in trace (trace holds events, not data) |
| Invalid/empty specialist result (e.g. no order resolved at all) | N/A | Policy Agent is skipped entirely (`draft_policy_assessment` returns `None`); Verifier emits the deterministic `insufficient_evidence` / `needs_investigation` output | `policy_decided` with `decision_code="insufficient_evidence"`, `verification_completed` with the same code |
| Policy LLM error/timeout | No (single attempt; a bad/partial LLM call must not be retried into inconsistent state) | Same deterministic fallback as above | `policy_decided` with `decision_code="insufficient_evidence"` |

Retries are capped, use fixed (idempotent) backoff, and never turn a missing MCP answer into invented
data — the only two outcomes are "verified evidence" or "explicitly absent evidence".

## 6. Verification invariants

Checked in `agents.verify_and_finalize` before Coordinator returns the output (and re-checked by
`contracts.validate_output` in `cli.py` before it is written to disk):

- **Primary-issue reconciliation** (`agents.reconcile_primary_issue`): in the first scored run the
  LLM often *confirmed* the customer's claimed issue and still named a secondary anomaly (an extra
  payment row, a stray event) as `primary_issue`, or hedged to `insufficient_evidence` with the
  evidence in hand. The Verifier therefore decides from the LLM's per-issue hypotheses, in order:
  (1) a claimed issue the evidence confirms is the primary issue; (2) otherwise the LLM's pick if it
  is confirmed, else the first confirmed issue; (3) a claimed *fault* issue the evidence contradicts,
  with nothing confirmed, is `unsupported_claim` (a contradicted *no-fault* label such as
  `unsupported_claim` means the opposite and never lands here); (4) otherwise the LLM's pick when
  core evidence exists; (5) `insufficient_evidence` only when the evidence needed is genuinely
  missing. The primary issue's own cause code is always ranked first.
- **Claim ↔ decision consistency**: a claim whose topic is the primary issue is `supported` (fault)
  or `unsupported` (no-fault outcomes: the complaint is not upheld); a claimed issue the evidence
  contradicted is `unsupported`; `requested_full_refund` follows the policy action applied
  (`issue_refund` → supported, freight/duplicate refunds → partially_supported,
  `document_no_action` → unsupported).
- **Schema**: every field is normalized into the exact shape/enum/pattern `l3a-output-v2.schema.json`
  requires (enum fallbacks, `cause_code` sanitized to `^[A-Z][A-Z0-9_]{2,79}$`, string length caps,
  array size caps) so a malformed LLM draft can never produce an invalid output.
- **Entity scope**: `affected_entities` is built from `agents._aggregate_known_ids`, which scans every
  evidence payload actually collected this case for id-shaped fields (`order_id`, `item_id`,
  `seller_id`, ...) — never from the customer's claimed IDs directly and never from the LLM.
- **Evidence ownership**: every `evidence_ref` in the output traces back to a `DomainEvidence`
  collected for this `case_id` in this run (see §4); the LLM cannot introduce a ref because it never
  sees or emits one.
- **Claim linkage**: a claim keeps a `supported`/`partially_supported`/`unsupported` verdict only if
  it resolves to a non-empty evidence-ref list; otherwise it is downgraded to
  `insufficient_evidence` with confidence `0.0`.
- **Money totals**: `financial_resolution.recommended_refund_brl` is always recomputed as
  `round(sum(refund_lines[*].amount_brl), 2)` — it is never taken as-is from the LLM, so it cannot
  drift from the line items.
- **Responsibility/action consistency**: `party_type` and each `resolution_action` are validated
  against the schema enum/format before inclusion; unknown values fall back to `"unknown"` /
  are dropped rather than passed through.
- **Policy-driven financial resolution (phase 4)**: `agents._apply_policy_rule` looks up
  `get_policy`'s rule table for the finalized `primary_issue`. When a rule exists, its
  `case_status`/`recommended_action`/`refund_brl`/`responsible_parties` are used verbatim for
  everything **except entity identity**. The rule table is shared by every case on that
  `policy_version` (its non-null `party_id`s match specific other cases' orders, e.g. L3A_CASE_092
  and 093), so: a null `party_id` in the rule stays null (the rule names no specific entity); a
  non-null one is re-resolved from *this* case's own evidence — for `late_delivery_seller`, the
  seller whose item was handed to the carrier after its shipping limit. Refund lines are keyed by the
  verified order id. The policy path is strictly preferred over the LLM's drafted
  responsibility/money/actions, which are used only when no policy rule covers the issue;
  `insufficient_evidence` gets a fixed resolution (unknown party, no refund,
  `escalate_manual_review`).
- **Primary-issue ↔ responsible-party consistency (LLM-fallback path only)**: when no policy rule
  applies, `PRIMARY_ISSUE_PARTY_RULES` in `agents.py` lists which party types can plausibly cause each
  `primary_issue` (e.g. `late_delivery_logistics` → `logistics_provider` only, never `seller`). A
  party outside that set is forced to `{"party_type": "unknown", "party_id": null}` and the override
  is recorded as a `data_conflict` (`PARTY_TYPE_INCONSISTENT_WITH_PRIMARY_ISSUE`) so it stays
  auditable instead of silently dropped.
- **Primary-issue ↔ case-status ↔ financial-resolution consistency**: `unsupported_claim` always
  forces `case_status = "no_action"`; `insufficient_evidence` always forces
  `case_status = "needs_investigation"`. Either of those, or an LLM-chosen `case_status = "no_action"`,
  forces `refund_lines = []` and `recommended_refund_brl = 0` — an unresolved or no-action case can
  never carry a refund recommendation.
- **Confidence calibration**: `assessment.confidence` is the probability `primary_issue` is right,
  so it is set by *how* the issue was established (`BASIS_CONFIDENCE`) rather than by the LLM's
  self-report: claimed issue confirmed by evidence 0.9; claimed fault contradicted →
  `unsupported_claim` 0.6; a different confirmed issue 0.55; `insufficient_evidence` 0.25; the LLM's
  own value only when it gave no usable hypotheses. It can then only drop: ≤0.6 with data conflicts
  (except for a confirmed claimed issue, whose conflicts are the incidental anomalies already looked
  past), ≤0.5 when evidence came from at most one domain or a party had to be overridden. A claim on
  the primary issue carries the case confidence; an `insufficient_evidence` claim is capped at 0.3.

## 7. Reproducibility

- **Model/config**: Policy Agent uses OpenAI Chat Completions with `temperature=0` and a `strict`
  JSON-schema response format (`llm.RESPONSE_SCHEMA`), model selected via `OPENAI_MODEL` env var
  (default `gpt-4.1-mini`); this keeps the reasoning step deterministic modulo provider-side changes.
- **Dependencies**: pinned ranges in `pyproject.toml` (`httpx2`, `mcp`, `jsonschema`, `python-dotenv`,
  `openai`); exact versions locked by the environment's installed wheels at submission time.
- **Concurrency**: cases are processed sequentially in `cli._run` (one `solve_case` at a time, so MCP
  audit load stays predictable and every trace event is attributable to one case's run). Within a
  case, `get_order` is resolved first (everything else depends on its verified `order_id`), then the
  7 independent order-scoped calls (item/seller/product/payment/payment_timeline/shipment/
  refund_timeline) plus `get_customer_history` run concurrently via `asyncio.gather` in
  `workflow.solve_case`, since none of them depends on another's result — only on the order already
  being verified. `get_policy` runs after that fan-out (it does not depend on it, but doesn't need to
  race it either). Measured against the live gateway, this cut per-case MCP time roughly in half.
- **Randomness**: no random seed is used; the only stochastic element (the LLM) is pinned to
  `temperature=0`.
- **Run commands**: `day09 run` (executes all cases), `day09 validate` (schema/consistency check),
  `day09 package --output dist/submission.zip` (build submission).
- **Resource limits**: each MCP call is capped at 3 attempts with fixed backoff (§5); no unbounded
  retry loops or background tasks are spawned. No API key or secret is recorded anywhere in this file,
  the trace, or the output.
