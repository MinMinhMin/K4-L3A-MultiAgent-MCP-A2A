"""Discovery-based resolution of MCP tools by business domain.

The competition rules forbid guessing tool names. Every lookup here starts from
`EvidenceGateway.list_tool_specs()` (the server's own tool + input-schema listing)
and matches on the tool's name/description words, never on a hardcoded tool name.

The live L3A gateway names tools like `get_order_items`, `get_shipment_summary`,
`get_payment_timeline`: a "get_<parent>_<child...>" convention where the domain is
carried by a *word* in the name, not a standalone keyword substring (a naive
substring match on "order" would wrongly claim `get_order_items` for the "order"
domain instead of "item"). `_domain_for_tool` resolves this by scanning the
name's underscore-separated words from right to left and returning the first
one that matches a known domain, so the most specific word wins.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

# Keyword -> domain. Order matters only in that `_domain_for_tool` matches the
# *last* word of a tool name that resolves to a keyword here.
DOMAIN_WORDS: dict[str, str] = {
    "order": "order",
    "orders": "order",
    "item": "item",
    "items": "item",
    "payment": "payment",
    "payments": "payment",
    "timeline": "timeline",  # disambiguated below using the preceding word
    "shipment": "shipment",
    "shipments": "shipment",
    "seller": "seller",
    "sellers": "seller",
    "product": "product",
    "products": "product",
    "refund": "refund",
    "refunds": "refund",
    "customer": "customer",
    "customers": "customer",
    "policy": "policy",
}

ID_KEY_HINTS: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "order_ids"),
    "item_id": ("item_id", "item_ids", "order_item_id", "order_item_ids"),
    "seller_id": ("seller_id", "seller_ids"),
    "payment_id": ("payment_id", "payment_ids", "payment_reference", "payment_references"),
    "shipment_id": ("shipment_id", "shipment_ids"),
    "product_id": ("product_id", "product_ids"),
    "customer_id": ("customer_id", "customer_ids", "customer_unique_id"),
}


def _domain_for_tool(name: str, description: str) -> str | None:
    words = name.lower().removeprefix("get_").split("_")
    for index in range(len(words) - 1, -1, -1):
        word = words[index]
        if word == "timeline" and index > 0:
            # "payment_timeline" / "refund_timeline" are their own domains,
            # distinct from the flat "payment" / "refund" record lookups.
            preceding = DOMAIN_WORDS.get(words[index - 1])
            if preceding:
                return f"{preceding}_timeline"
            continue
        domain = DOMAIN_WORDS.get(word)
        if domain:
            return domain
    for word, domain in DOMAIN_WORDS.items():
        if word in description.lower():
            return domain
    return None


class ToolCatalog:
    """Resolves MCP tools by business domain from a discovered tool listing."""

    def __init__(self, specs: list[Tool]) -> None:
        self._by_domain: dict[str, list[Tool]] = {}
        for spec in specs:
            domain = _domain_for_tool(spec.name, spec.description or "")
            if domain is not None:
                self._by_domain.setdefault(domain, []).append(spec)

    def tools_for(self, domain: str) -> list[Tool]:
        return list(self._by_domain.get(domain, []))

    def best_tool(self, domain: str, available_fields: set[str]) -> Tool | None:
        """Pick the discovered tool for `domain` whose required inputs we can supply."""
        candidates = self.tools_for(domain)
        if not candidates:
            return None
        satisfiable = [
            spec
            for spec in candidates
            if set((spec.input_schema or {}).get("required", [])) - {"case_id"} <= available_fields
        ]
        pool = satisfiable or candidates
        pool.sort(key=lambda spec: len((spec.input_schema or {}).get("required", [])))
        return pool[0]


def primary_id_kwarg(tool: Tool) -> str | None:
    """The argument name a discovered tool expects for its main identifier.

    Every specialist tool on the live gateway is keyed by `order_id` (the same
    order the customer named); a handful (customer history) use a different
    `*_id` field instead. Prefer the literal `order_id` when present, then fall
    back to whatever other `*_id` property the tool's own schema declares.
    """
    properties = (tool.input_schema or {}).get("properties", {})
    if "order_id" in properties:
        return "order_id"
    candidates = [name for name in properties if name != "case_id" and name.endswith("_id")]
    return candidates[0] if candidates else None


def extract_ids(data: Any, collected: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Recursively harvest known id-shaped fields from an MCP evidence payload."""
    if collected is None:
        collected = {key: set() for key in ID_KEY_HINTS}
    if isinstance(data, dict):
        for key, value in data.items():
            lowered = key.lower()
            for id_name, hints in ID_KEY_HINTS.items():
                if lowered not in hints:
                    continue
                if isinstance(value, str):
                    collected[id_name].add(value)
                elif isinstance(value, list):
                    collected[id_name].update(item for item in value if isinstance(item, str))
            extract_ids(value, collected)
    elif isinstance(data, list):
        for item in data:
            extract_ids(item, collected)
    return collected
