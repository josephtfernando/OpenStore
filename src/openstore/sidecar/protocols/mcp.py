"""MCP — real JSON-RPC 2.0 (ADR-0026), not a lookalike envelope.

`tools/list` and the tool schemas come from the closed action set in
`core/codes.py`, so a tool that exists here and not in the registry is a red
build. Least privilege per tool: each one declares the scope it needs, and the
scope check happens before the body runs rather than inside it.

Three methods, because this sidecar has never exposed anything a fourth would
serve: `initialize` (capability handshake), `tools/list` (JSON Schema +
annotations a generic client can act on without knowing any tool by name), and
`tools/call` (dispatches into the same core every other envelope shares). No
resources, no prompts, no sampling.
"""

from __future__ import annotations

import json
from typing import Any

from openstore.sidecar.admission.oauth import AgentToken
from openstore.sidecar.core.codes import TOOL_SCOPES, Protocol, ReasonCode, Scope, ToolName
from openstore.sidecar.core.correlation import request_id
from openstore.sidecar.protocols.core import refusal_envelope

PROTOCOL_VERSION = "2025-06-18"

#: Human-facing descriptions. The shapes come from the registry; these are the
#: only free text, and they say what the tool does for a Consumer rather than
#: what it does to the system.
_DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.SEARCH: (
        "Search this shop's catalogue. An empty query lists everything, a page at a "
        "time: pass the `next_cursor` from a result back as `cursor` for the next page, "
        "and `total` says how many matched in all."
    ),
    ToolName.READ_ITEM: (
        "Read one product group, its options and availability. Then add-line "
        "with one variant SKU from it — never the group id."
    ),
    ToolName.ADD_LINE: (
        "Add one resolved variant SKU to the basket, qty 1 or more (a group id "
        "refuses with the variant list — pick one). `parent` only for add-ons "
        "hanging off a line."
    ),
    ToolName.REMOVE_LINE: "Remove a line from the basket.",
    ToolName.CLEAR_BASKET: "Empty the basket back to a fresh state: lines, destination, contact, fulfillment and code all go.",
    ToolName.SET_DESTINATION: (
        "Set where the order is delivered. Needs street, city, 2-letter state "
        "and 6-digit PIN; changing it un-chooses fulfillment, so pick delivery "
        "after the last address or line change."
    ),
    ToolName.SET_CONTACT: (
        "Set how the shop notifies (email, phone, or both — at least one). "
        "Needed before checkout."
    ),
    ToolName.SET_DETAILS: (
        "Set this shop's own extra details (delivery slot, instructions, …). "
        "Only the keys this shop declares — read them from this tool's schema "
        "or the shop card — anything else is refused. May be set in pieces."
    ),
    ToolName.CHOOSE_FULFILLMENT: (
        "Choose a delivery option by id. Call with no id to list what the "
        "current basket and address offer."
    ),
    ToolName.APPLY_PUBLIC_CODE: (
        "Apply an advertised discount code. Private codes never travel through "
        "an agent — the Consumer enters them on the approve page."
    ),
    ToolName.START_CHECKOUT: (
        "Price the basket and prepare it for approval. Needs lines, address, "
        "a chosen delivery option and contact first — the refusal names what "
        "is missing. Then place-order for the approve link."
    ),
    ToolName.PLACE_ORDER: (
        "Hand the Consumer a link to approve the purchase. Returns an approve URL, "
        "never an order — the shop cannot place an order on the Consumer's behalf."
    ),
    ToolName.ORDER_STATUS: "Check the status of an order this agent placed.",
    ToolName.CANCEL_ORDER: (
        "Cancel an order this agent placed, before any money moves. "
        "After money moves, request-refund instead — this will refuse."
    ),
    ToolName.REQUEST_REFUND: (
        "Ask the shop to refund an order this agent placed. Queues a request "
        "for the Merchant; moves no money and promises none."
    ),
}

#: field -> loose type shorthand ("x?" is optional). Kept next to the
#: descriptions because both are hand-authored per tool; `_input_schema`
#: below is what actually leaves the process.
_FIELDS: dict[ToolName, dict[str, str]] = {
    ToolName.SEARCH: {"query": "string", "limit": "integer?", "cursor": "string?"},
    ToolName.READ_ITEM: {"group": "string"},
    ToolName.ADD_LINE: {"sku": "string", "qty": "integer", "parent": "string?"},
    ToolName.REMOVE_LINE: {"sku": "string"},
    ToolName.CLEAR_BASKET: {},
    ToolName.SET_DESTINATION: {"destination": "object"},
    ToolName.SET_CONTACT: {"contact": "object"},
    ToolName.SET_DETAILS: {"details": "object"},
    ToolName.CHOOSE_FULFILLMENT: {"id": "string?"},
    ToolName.APPLY_PUBLIC_CODE: {"code": "string"},
    ToolName.START_CHECKOUT: {},
    ToolName.PLACE_ORDER: {},
    ToolName.ORDER_STATUS: {"order_id": "string"},
    ToolName.CANCEL_ORDER: {"order_id": "string", "reason": "string?"},
    ToolName.REQUEST_REFUND: {"order_id": "string", "reason": "string?"},
}

_JSON_TYPE = {"string": "string", "integer": "integer", "object": "object", "boolean": "boolean"}


def _destination_subschema() -> dict[str, Any]:
    """Nested address shape, derived from the model the trait validates
    against — never retyped, so the published shape and the enforced shape
    cannot disagree (same rule as the card's `requires`)."""
    from openstore.sidecar.trait.models import Destination

    schema = Destination.model_json_schema()
    properties: dict[str, Any] = dict(schema.get("properties", {}))
    alias_notes = {
        "line1": " Also accepts address_line1.",
        "line2": " Also accepts address_line2.",
        "postal_code": " Also accepts zip / pincode.",
    }
    for key, note in alias_notes.items():
        if isinstance(properties.get(key), dict):
            prior = str(properties[key].get("description", ""))
            properties[key] = {**properties[key], "description": (prior + note).strip()}
    return {
        "type": "object",
        "properties": properties,
        "required": list(schema.get("required", ["line1", "city", "state", "postal_code"])),
        "description": (
            "Extra keys (locality, phone, …) are ignored, not refused. "
            "Phone belongs in set-contact."
        ),
    }


def _contact_subschema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "phone": {"type": "string"},
        },
        "description": "At least one of email / phone — and one is needed before checkout.",
    }


#: The two scopes that move a basket toward spend. Everything else — reads and
#: basket-building — is safe for a client to offer standing "always allow" on;
#: these two never are, regardless of what a client's UI wants to do.
_MONEY_PATH_SCOPES = {Scope.START_CHECKOUT, Scope.CONFIRM}


def _details_subschema(requirements: Any | None) -> dict[str, Any]:
    """This shop's own extras, rendered from its door-10 declaration — the same
    vocabulary its refusals speak. `None` means the fixed shape: the tool then
    refuses everything, loudly, rather than accepting keys nobody declared."""
    if requirements is None or not getattr(requirements, "fields", ()):
        return {
            "type": "object",
            "description": (
                "This shop declares no extra details — address and contact are "
                "all it asks. Any call refuses."
            ),
        }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in requirements.fields:
        fragment: dict[str, Any] = {"type": "string"}
        if getattr(field, "label", ""):
            fragment["title"] = field.label
        if getattr(field, "description", ""):
            fragment["description"] = field.description
        if getattr(field, "type", "string") == "enum":
            fragment["enum"] = list(getattr(field, "values", []))
        elif getattr(field, "pattern", ""):
            fragment["pattern"] = field.pattern
        properties[field.key] = fragment
        if getattr(field, "required", False):
            required.append(field.key)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "description": "Set in pieces; required keys must all hold before checkout.",
    }
    if required:
        schema["required"] = required
    return schema


def _input_schema(tool: ToolName, requirements: Any | None = None) -> dict[str, Any]:
    fields = _FIELDS[tool]
    nested: dict[str, dict[str, Any]] = {}
    if tool is ToolName.SET_DESTINATION:
        nested = {"destination": _destination_subschema()}
    elif tool is ToolName.SET_CONTACT:
        nested = {"contact": _contact_subschema()}
    elif tool is ToolName.SET_DETAILS:
        nested = {"details": _details_subschema(requirements)}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, kind in fields.items():
        if name in nested:
            properties[name] = nested[name]
            required.append(name)
            continue
        optional = kind.endswith("?")
        json_type = _JSON_TYPE[kind[:-1] if optional else kind]
        properties[name] = {"type": json_type}
        if not optional:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def tools_list(requirements: Any | None = None) -> list[dict[str, Any]]:
    """`tools/list`. Annotations carry the safety-relevant facts — read-only,
    destructive, money-path — as booleans a generic client can branch on
    without a single hardcoded tool name. `requirements` is this Merchant's
    door-10 snapshot (or `None` for the fixed shape); only `set-details`
    reads it, so every other tool's bytes are identical either way."""
    out = []
    for tool in ToolName:
        scope = TOOL_SCOPES[tool]
        out.append(
            {
                "name": tool.value,
                "description": _DESCRIPTIONS[tool],
                "inputSchema": _input_schema(tool, requirements),
                "annotations": {
                    "title": tool.value.replace("-", " ").capitalize(),
                    "readOnlyHint": scope is Scope.SEARCH,
                    "destructiveHint": tool
                    in (ToolName.CANCEL_ORDER, ToolName.REQUEST_REFUND, ToolName.CLEAR_BASKET),
                    "idempotentHint": scope is Scope.SEARCH,
                    "openWorldHint": False,
                    # Not a standard MCP annotation — this sidecar's own scope
                    # ladder, exposed so a client's permission UI can offer
                    # standing approval for everything except the two scopes
                    # that ever move toward a spend (ADR-0008, ADR-0017).
                    "moneyPathHint": scope in _MONEY_PATH_SCOPES,
                    "scope": scope.value,
                },
            }
        )
    return out


def check_scope(token: AgentToken, tool: ToolName) -> dict[str, Any] | None:
    """Refuse before the body runs.

    Holding `confirm` still buys nothing: `place-order` returns an approve URL
    and the Gate refuses a spend without a fresh Authority regardless.
    """
    required = TOOL_SCOPES[tool]
    if not token.allows(required):
        return refusal_envelope(
            Protocol.MCP,
            ReasonCode.AUTHORITY_MISSING,
            f"{tool.value} needs the {required.value} scope",
        )
    return None


def call_result(result: dict[str, Any]) -> dict[str, Any]:
    """A successful `tools/call` result: text for a client that renders
    nothing tool-specific, `structuredContent` for one that does."""
    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "structuredContent": result,
        "isError": False,
    }


def call_error(code: ReasonCode, detail: str, **fields: Any) -> dict[str, Any]:
    """A refused `tools/call`: still a successful JSON-RPC response (the
    *transport* worked), `isError: true` per the MCP spec, so a generic client
    doesn't need this repo's `ReasonCode` enum to know something went wrong —
    it only needs one it already does need it to show why."""
    error: dict[str, Any] = {"code": code.value, "detail": detail, **fields}
    # Same id the console row carries. An agent that reports a refusal to a
    # Merchant can now name the attempt instead of the minute it happened.
    current = request_id()
    if current:
        error["request_id"] = current
    return {
        "content": [{"type": "text", "text": f"{code.value}: {detail}"}],
        "structuredContent": {"protocol": Protocol.MCP.value, "error": error},
        "isError": True,
    }
