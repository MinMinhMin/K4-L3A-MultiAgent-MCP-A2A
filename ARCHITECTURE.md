# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý một case:

~~~text
inputs/<case_id>.json
        │
        ▼
Coordinator ── task_assigned ──┬── Order/item agent ── get_order
                               │                    ├── get_order_items
                               │                    ├── get_sellers
                               │                    └── get_product_context
                               ├── Payment agent ─── get_order_payments
                               │                    ├── get_payment_timeline
                               │                    └── get_refund_timeline
                               └── Shipment agent ── get_shipment_summary
                                        │
                                        └── Policy agent ── get_policy
                                                             │
MCP Evidence Gateway ── validated evidence ledger ── handoff
                                                             │
                                                     Verifier agent
                                                             │
                                      outputs/<case_id>.json + trace event
~~~

cli._run emits case_received, invokes solve_case, validates the returned output
against the public schema, atomically replaces outputs/<case_id>.json, then emits
case_finalized. Thus an output is never finalized before schema validation and
durable output replacement.

## 2. Agent ownership and tool permissions

| Actor | Input | Allowed tools | Responsibility | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case object, discovered tool names | None | Validate case/order/policy scope, assign specialists and retain case-local ledger | task_assigned, specialist work orders |
| Order/item agent | case_id, claimed order_id | get_order, get_order_items, get_sellers, get_product_context | Establish order state, item IDs, seller IDs and product context | Validated evidence records |
| Payment agent | case_id, claimed order_id | get_order_payments, get_payment_timeline, get_refund_timeline | Reconcile charges, split payments and refund lifecycle | Validated evidence records |
| Shipment agent | case_id, claimed order_id | get_shipment_summary | Compare seller handoff, carrier and delivery timestamps | Validated evidence record |
| Policy agent | Case claims, policy version and ledger | get_policy | Apply deterministic issue precedence and derive responsibility/refund/actions | policy_decided and Decision |
| Verifier agent | Decision and case-local ledger | None | Check evidence linkage, entity scope, refund arithmetic, confidence and output shape | verification_completed, final output |

The runtime filters each work order against the discovered tool list. It never calls
get_customer_history without an explicit customer identity in the input, and it
never broadens an order-scoped call to another order.

## 3. A2A protocol

The observable handoff envelope is represented by trace fields:

- case_id: correlation key and mandatory MCP scope;
- actor and target: sender and receiving role;
- event_type: lifecycle transition;
- tool_name: tool invoked for a tool-result event;
- evidence_refs: references copied from validated MCP envelopes;
- attributes: bounded metadata such as scope, retry count, evidence count and confidence.

The in-memory specialist work order additionally carries tool_name and exact
string arguments. Independent specialists run concurrently with asyncio.gather.
There is no recursive handoff: specialists return once, the coordinator hands the
ledger to policy, and policy returns once to the verifier. The only retry is one
idempotent retry for timeout, temporary-unavailable, HTTP 429/503 or equivalent
transport failures.

Required order in traces/trace.jsonl is:

~~~text
case_received
  → task_assigned (order-agent, payment-agent, shipment-agent)
  → tool_result_consumed (one per successful validated envelope)
  → handoff (coordinator → policy-agent)
  → policy_decided
  → verification_completed
  → case_finalized
~~~

## 4. Evidence lifecycle

1. EvidenceGateway sends {"case_id": case_id, ...arguments} and validates the
   MCP envelope against mcp-evidence-response-v1.schema.json.
2. collect_tool copies evidence_ref, domain, data and warnings into a case-local
   EvidenceLedger; it never creates or edits an evidence reference.
3. A successful copy emits tool_result_consumed with the original ref. Failed
   calls produce no ref and no fabricated record.
4. The policy engine normalizes only ledger data. Its output evidence refs are
   selected from domains supporting the chosen issue.
5. The workflow verifier checks that output and claim refs are subsets of this
   ledger and that refund lines sum exactly to the total.
6. Submission validation cross-checks every output ref against the same case scope
   tool_result_consumed trace events and rejects cross-case ownership.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout, temporary unavailable, 429 or 503 | Once with identical arguments | Keep no record if retry fails | No tool_result_consumed; verifier may produce degraded output |
| MCP not found or authorization/tool error | No | Keep no record; do not infer the missing row | No fabricated event |
| Invalid MCP envelope | No | Reject the response through gateway validation | No fabricated event |
| Source conflict | No MCP retry | Preserve data_conflicts, lower confidence, use deterministic rule precedence | policy_decided with selected issue |
| Missing required evidence | No fabricated fallback | insufficient_evidence, needs_investigation, zero refund | verification_completed code degraded |
| Invalid specialist result | No recursive handoff | Drop the result and continue with remaining case-local evidence | verification_completed code degraded |

## 6. Verification invariants

Before verification_completed, the workflow checks:

- output case_id and schema version are exact;
- every output evidence ref was consumed for this case and is unique;
- claim evidence refs are subsets of top-level refs;
- entity lists contain only scoped input IDs or IDs observed in ledger data;
- financial resolution is BRL, non-negative and line totals equal the recommended refund;
- root cause, responsible party and actions come from the same deterministic issue rule;
- confidence is within [0, 1] and is reduced for missing/conflicting/warning evidence;
- the CLI validates the complete object against l3a-output-v2.schema.json.

## 7. Reproducibility

- Python 3.11+; dependencies are constrained in pyproject.toml.
- Policy decisions use fixed rule order, Decimal BRL arithmetic and no random
  model output.
- Specialist calls are bounded by the discovered tool set and one retry.
- Trace event IDs/timestamps are audit metadata only and do not affect decisions.
- Run commands:

~~~bash
source .venv/bin/activate
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
~~~

The submission ZIP contains only manifest.json, trace.jsonl and outputs/*.json;
source, raw inputs, .env, API keys and debug logs are excluded.
