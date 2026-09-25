"""OpenAI-backed reasoning for the Policy Agent.

The LLM never sees or invents `evidence_ref` values and never invents entity IDs:
it only classifies the case and points at which *domains* of already-collected
MCP evidence support each conclusion. `agents.py` resolves those domain pointers
back to real evidence refs / entity IDs before anything is written to the output.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-4.1-mini"

# Every issue the policy table can decide; `insufficient_evidence` is the one
# primary_issue value that is not a policy outcome.
POLICY_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
)
PRIMARY_ISSUES = (*POLICY_ISSUES, "insufficient_evidence")
HYPOTHESIS_ASSESSMENTS = ("confirmed", "contradicted", "insufficient_data", "not_applicable")

_DOMAIN_ENUM = [
    "order",
    "item",
    "payment",
    "payment_timeline",
    "shipment",
    "seller",
    "policy",
    "refund_timeline",
    "product",
    "customer",
]

# Property order matters: structured output is generated in schema order, so
# the model assesses every hypothesis *before* it commits to a primary_issue.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "hypotheses",
        "primary_issue",
        "case_status",
        "confidence",
        "claim_assessments",
        "ranked_causes",
        "responsible_parties",
        "data_conflicts",
        "financial_resolution",
        "resolution_actions",
    ],
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["issue", "assessment", "supporting_domains"],
                "properties": {
                    "issue": {"type": "string", "enum": list(POLICY_ISSUES)},
                    "assessment": {"type": "string", "enum": list(HYPOTHESIS_ASSESSMENTS)},
                    "supporting_domains": {
                        "type": "array",
                        "items": {"type": "string", "enum": _DOMAIN_ENUM},
                    },
                },
            },
        },
        "primary_issue": {"type": "string", "enum": list(PRIMARY_ISSUES)},
        "case_status": {
            "type": "string",
            "enum": ["action_required", "no_action", "needs_investigation"],
        },
        "confidence": {"type": "number"},
        "claim_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "verdict", "confidence", "supporting_domains"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": [
                            "supported",
                            "unsupported",
                            "partially_supported",
                            "insufficient_evidence",
                        ],
                    },
                    "confidence": {"type": "number"},
                    "supporting_domains": {
                        "type": "array",
                        "items": {"type": "string", "enum": _DOMAIN_ENUM},
                    },
                },
            },
        },
        "ranked_causes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["cause_code", "rank"],
                "properties": {
                    "cause_code": {"type": "string"},
                    "rank": {"type": "integer"},
                },
            },
        },
        "responsible_parties": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["party_type", "party_id_domain"],
                "properties": {
                    "party_type": {
                        "type": "string",
                        "enum": [
                            "seller",
                            "platform",
                            "logistics_provider",
                            "payment_provider",
                            "customer",
                            "unknown",
                        ],
                    },
                    "party_id_domain": {"type": ["string", "null"], "enum": [*_DOMAIN_ENUM, None]},
                },
            },
        },
        "data_conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "sources", "selected_source", "resolution_code"],
                "properties": {
                    "field": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "selected_source": {"type": ["string", "null"]},
                    "resolution_code": {"type": "string"},
                },
            },
        },
        "financial_resolution": {
            "type": "object",
            "additionalProperties": False,
            "required": ["recommended_refund_brl", "refund_lines"],
            "properties": {
                "recommended_refund_brl": {"type": "number"},
                "refund_lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["reason_code", "amount_brl", "entity_domain"],
                        "properties": {
                            "reason_code": {"type": "string"},
                            "amount_brl": {"type": "number"},
                            "entity_domain": {
                                "type": ["string", "null"],
                                "enum": [*_DOMAIN_ENUM, None],
                            },
                        },
                    },
                },
            },
        },
        "resolution_actions": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = """You are the Policy Agent in a multi-agent e-commerce dispute investigation
system.

You receive:
- customer_request: the customer's message and claims. Claims are HYPOTHESES to verify, not facts.
  `claimed_issues` lists the claim topics that name an issue from the catalogue below.
- evidence: verified MCP evidence grouped by domain (order, item, seller, product, payment,
  payment_timeline, shipment, refund_timeline, customer, policy). An empty list means that
  evidence was unavailable.
- facts: numbers pre-computed deterministically from that same evidence (totals, date gaps,
  repeated records, event summaries). Trust these numbers over your own arithmetic.

Look at the WHOLE picture before deciding — every domain, every event, every timestamp — and do
not stop at the first anomaly you notice. Case data can contain secondary, incidental records
(an extra payment row, a stray event) that are not what the dispute is about.

Procedure:
1. hypotheses: assess EVERY issue in the catalogue, one entry each:
   - confirmed: the evidence establishes this issue happened in this order.
   - contradicted: the evidence shows it did not happen.
   - insufficient_data: the evidence needed to decide is missing.
   - not_applicable: unrelated to this order's situation.
   List the evidence domains that decide each assessment.
2. primary_issue:
   - If an issue the customer claims is confirmed, it IS the primary issue, even when other
     anomalies also exist. Report those other anomalies as lower-ranked causes or data conflicts.
   - Otherwise pick the confirmed issue that best explains the customer's situation.
   - If the evidence contradicts the customer's complaint and no fault is confirmed, choose
     unsupported_claim (or valid_split_payment when the payments questioned are a legitimate split).
   - Use insufficient_evidence ONLY when the evidence needed to decide is genuinely unavailable,
     never merely because the case looks ambiguous.
3. Weigh sources: authoritative order/shipment timestamps and events with status "confirmed"
   outweigh events with any other status; one stray record does not override consistent evidence.

Issue catalogue:
- canceled_order_paid: order status is canceled, yet a payment was captured and not refunded.
- unavailable_order_paid: order status is unavailable (could not be fulfilled), yet it was paid.
- late_delivery_seller: delivered after the estimated date, and the delay is on the seller's side
  (handed to the carrier after the item's shipping limit, or late events attributed to the seller).
- late_delivery_logistics: delivered after the estimated date although the seller handed over on
  time; the delay is on the carrier / logistics side.
- duplicate_charge: the same charge was captured more than once (a repeated identical payment or
  capture), so the customer paid extra by exactly that duplicated amount.
- valid_split_payment: several payment records (different sequentials and/or types, e.g. voucher +
  credit card) that legitimately add up to what was owed; there is no overcharge.
- payment_mismatch: the amount paid differs from the amount owed in a way that is neither a
  duplicate charge nor a legitimate split.
- refund_pending: a refund was initiated and is still pending.
- refund_failed: a refund was attempted and failed or was rejected.
- unsupported_claim: the customer's complaint is contradicted by the evidence (e.g. delivered on
  time, charges correct, order not canceled).

Claim verdicts say whether the evidence upholds the customer's complaint:
- A claim whose topic is a catalogue issue: supported when that issue is confirmed as a fault,
  unsupported when the evidence contradicts it. If the case turns out to be unsupported_claim or
  valid_split_payment, the customer's complaint is not upheld, so that claim is unsupported.
- requested_full_refund: supported only if a full refund of what was paid is warranted;
  partially_supported if only part of it is refundable (e.g. freight or a duplicated charge);
  otherwise unsupported.

Rules:
- Never invent an evidence reference or any id. Reference evidence only by domain name.
- Money amounts must come from the evidence, facts or policy, never guessed.
- Use the policy's recommended_action names for resolution_actions when a policy rule applies.
- confidence is your probability that primary_issue is correct.
"""


class PolicyLLM:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def draft_assessment(self, case_context: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(case_context, ensure_ascii=False)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "policy_draft", "schema": RESPONSE_SCHEMA, "strict": True},
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("policy LLM returned no content")
        return json.loads(content)


_llm: PolicyLLM | None = None


def get_policy_llm() -> PolicyLLM:
    global _llm
    if _llm is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set; the Policy Agent requires it")
        model = os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
        _llm = PolicyLLM(api_key, model)
    return _llm
