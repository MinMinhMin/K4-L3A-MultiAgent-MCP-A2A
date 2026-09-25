# L3A Deterministic Multi-Agent Workflow Design

## Goal

Implement a deterministic, evidence-first L3A case solver that processes all 100
competition cases through the existing MCP Evidence Gateway, emits an observable
multi-agent trace, and produces only outputs accepted by the published L3A V2
schema.

## Context and constraints

- The public schemas in `contracts/schemas/` are immutable sources of truth.
- Every MCP request must carry the input `case_id`; every submitted evidence
  reference must be copied from a validated MCP envelope.
- The current input contract contains `case_id`, `customer_request.claimed_order_id`,
  two claims, and `policy_version`.
- The discovered L3A tools are `get_order`, `get_order_items`,
  `get_order_payments`, `get_shipment_summary`, `get_sellers`, `get_policy`,
  `get_customer_history`, `get_product_context`, `get_payment_timeline`, and
  `get_refund_timeline`.
- The L3A score emphasizes semantic correctness, evidence relevance and
  provenance. L3A efficiency has zero weight, so the design favors complete,
  relevant evidence coverage over aggressive call minimization.
- No LLM service is required. A deterministic policy engine is easier to test,
  reproducible across the public/private split, and avoids unsupported claims
  caused by model variability.

## Architecture

The implementation uses a small async state machine inside `workflow.py` with
explicit role boundaries:

1. **Coordinator** validates the case identifiers, emits `task_assigned`, and
   constructs the scoped work order.
2. **Order/item agent** calls order, item and seller/product tools for the
   claimed order.
3. **Payment agent** calls payment, payment-timeline and refund-timeline tools.
4. **Shipment agent** calls shipment evidence and relates delivery timestamps to
   the policy deadlines.
5. **Policy agent** consumes normalized evidence and applies deterministic issue
   precedence and refund rules.
6. **Verifier agent** checks evidence linkage, entity sets, money totals,
   responsibility/action consistency and confidence calibration before returning
   the schema-shaped output.

Specialists run independent MCP calls concurrently. Each call is made through
`EvidenceGateway.call`, which validates the evidence envelope. A successful
response is immediately recorded in an in-memory evidence ledger and a
`tool_result_consumed` trace event. The ledger is local to one case and is never
reused across cases.

## Evidence and failure handling

The coordinator obtains the available tool list once per case and only calls
discovered tools. Order-scoped tools receive the claimed order ID; the customer
history tool is called only if the input contains a customer identity. The policy
tool receives the case policy version.

Transient gateway failures are retried once with the same arguments. Invalid
envelopes, authorization failures, not-found responses and a second failure are
recorded as unavailable specialist evidence and do not produce fabricated
references or data. The final result degrades to `insufficient_evidence` when
the facts needed for a reliable issue are unavailable.

Evidence selection is relevance-first: the output cites only references from the
ledger whose domain/facts support a selected claim or resolution. All consumed
references may remain in the trace, but unrelated references are omitted from the
output evidence list where the verifier can determine that they are irrelevant.

## Policy and normalization

The policy engine normalizes MCP payloads defensively because the envelope's
`data` field is intentionally unconstrained by the public schema. It recognizes
the published Olist-style field names and equivalent nested/list forms for:

- order status and order lifecycle timestamps;
- item IDs, seller IDs, product IDs and item/freight values;
- payment values, payment references, payment status and lifecycle events;
- shipment delivery, carrier and estimate timestamps;
- refund status and refund amounts.

Money is normalized to `Decimal` internally and rounded to two BRL decimals only
at the output boundary. Refund lines always sum exactly to
`recommended_refund_brl`; unsupported or contradictory facts result in a zero
refund or `needs_investigation`, never an invented amount.

Issue precedence is explicit and deterministic:

1. cancellation/unavailability with a confirmed payment;
2. duplicate or mismatched payment/refund totals;
3. failed or pending refund lifecycle;
4. late delivery attributable to seller handoff;
5. late delivery attributable to logistics after handoff;
6. valid split payment;
7. unsupported or insufficient evidence.

The first matching rule becomes `assessment.primary_issue`; additional supported
claim topics are represented in `claim_assessments`. Responsibility is derived
from the same rule that selects the issue so that refund and actions cannot drift
from the root cause.

## Trace protocol

Each case emits the following ordered lifecycle:

```text
case_received -> task_assigned ->
tool_result_consumed (one per consumed envelope) ->
handoff (specialists -> policy) -> policy_decided ->
verification_completed -> case_finalized
```

Trace attributes contain only bounded, observable metadata such as call outcome,
retry count, specialist name and selected issue. Prompts, raw customer messages
and private reasoning are never written to trace.

## Verification invariants

Before returning an output, the verifier enforces:

- exact case ID and schema version;
- all output evidence references came from this case's validated ledger;
- every evidence reference is unique and every claim reference is a subset of
  the top-level references;
- entity arrays contain only IDs observed in evidence or the scoped input;
- refund line amounts are non-negative and sum to the recommended refund;
- a refund is not recommended for an unresolved/unsupported claim;
- responsibility and resolution actions agree with the selected issue;
- confidence is bounded in `[0, 1]` and reduced for missing, conflicting or
  warning-bearing evidence;
- the final output passes `Contracts.validate_output` before the CLI persists it.

## Testing strategy

Tests will use a real in-memory fake gateway with deterministic evidence
envelopes, not mocks of the production policy functions. Coverage will include:

- all public issue classes and claim assessment behavior;
- order-scoped MCP arguments and evidence reference preservation;
- transient retry and no-fabrication failure behavior;
- duplicate/mismatched payment arithmetic and exact refund totals;
- shipment responsibility split by seller handoff versus logistics delay;
- trace lifecycle ordering and tool-result linkage;
- output and package validation against the unchanged public schemas.

## Reproducibility

The workflow uses Python 3.11+, the dependencies already pinned by
`pyproject.toml`, deterministic rule ordering, no random decision-making, and
bounded async concurrency. The existing random trace event IDs remain audit
identifiers only and never affect decisions.
