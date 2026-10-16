"""What the tools actually do.

`/agent/mcp` answered every tool `{"accepted": true}` and ran nothing: `search`
returned no results from a catalogue door 1 serves twelve groups from, and
`add-line` added to nothing. This is the dispatch behind the tool set.

Two rules hold for every tool here:

- **No exact stock ever reaches an agent.** Door 2 returns integers because the
  Gate needs them; everything agent-facing goes through `trait.buckets` first.
  The single exception SPEC §16.9 allows is a refusal on a waited-on quantity,
  which names the number the Consumer can actually have.
- **No arithmetic.** Every total comes from door 9. A tool that could add up a
  basket would eventually disagree with the shop.
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import ValidationError

from openstore.sidecar.basket import Basket
from openstore.sidecar.core.codes import (
    AvailabilityBucket,
    PaymentMethod,
    ReasonCode,
    ToolName,
)
from openstore.sidecar.trait.buckets import bucket_for, bucket_for_group, quantity_refusal_detail
from openstore.sidecar.trait.client import TraitClient
from openstore.sidecar.trait.errors import TraitError
from openstore.sidecar.trait.models import (
    MAX_REQUIREMENT_VALUE_LENGTH,
    Destination,
    Requirements,
)


class ToolRefused(Exception):
    """A refusal, optionally carrying the facts needed to fix it.

    `fields` is the difference between an agent retrying blindly and retrying
    correctly. `mcp.call_error` has always accepted extra keys and nothing ever
    passed any, so every refusal arrived as a code plus a sentence and an agent
    wanting the number in it had to parse English — which it will do badly, and
    which makes the wording of a message load-bearing in a way nobody intended.

    **Only facts the agent could already have.** These are computed from what it
    sent and what this shop publishes; a refusal is not a side channel for
    anything the tool surface would not have answered anyway. That is why a
    blocked tag reports that a rule matched and not which rule, and why an
    unserviceable address does not come back with the serviceable list.
    """

    def __init__(self, code: ReasonCode, detail: str, fields: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.fields = fields or {}


async def _buckets(trait: TraitClient, skus: list[str], low: dict[str, int]) -> dict[str, str]:
    """Counts in, buckets out — the conversion that keeps door 2 private."""
    stock = await trait.stock_read(skus)
    return {sku: bucket_for(count, low.get(sku, 3)).value for sku, count in stock.items()}


#: The most groups one `search` will return. A shop with fifty thousand SKUs
#: answering every query with all of them is not a catalogue endpoint, it is an
#: amplification surface on a public route — and the agent on the other end has
#: to hold the whole thing in a context window to use any of it.
#:
#: Fifty rather than a round ten: a shopper asking for "notebook" wants the
#: shortlist in one call, and a page size that forces a second round trip for an
#: ordinary query has moved the cost rather than removed it.
SEARCH_PAGE_SIZE = 50

#: The ceiling on what a caller may ask for. A `limit` an agent chooses is a
#: number an agent can get wrong, and a shop is entitled to a bound on the work
#: one call makes it do.
SEARCH_MAX_PAGE_SIZE = 200


async def search(
    trait: TraitClient, query: str, *, limit: int | None = None, cursor: str = ""
) -> dict[str, Any]:
    """Match on any word, across the group and the variants under it.

    A whole-phrase match against the group name alone finds nothing for "black
    tote", because the group is "Tote" and `black` is a value on one of its
    option axes — so the shop looked empty for the most obvious search a
    Consumer could type.
    """
    catalogue = await trait.catalog_read()
    words = [w for w in query.strip().lower().split() if w]

    def haystack(group_id: str, name: str, description: str) -> str:
        variants = [i for i in catalogue.items if i.group_id == group_id]
        return " ".join(
            [name, description or ""]
            + [i.name for i in variants]
            + [i.sku for i in variants]
            + [v for i in variants for v in i.options.values()]
            + [t for i in variants for t in i.tags]
        ).lower()

    groups = [
        g
        for g in catalogue.groups
        if not words or any(w in haystack(g.id, g.name, g.description or "") for w in words)
    ]
    low = {i.sku: i.low_stock_threshold for i in catalogue.items}
    buckets = await _buckets(trait, [i.sku for i in catalogue.items], low)

    results = []
    for group in groups:
        items = [i for i in catalogue.items if i.group_id == group.id]
        if not items:
            continue
        results.append(
            {
                "group": group.id,
                "name": group.name,
                "from_minor": min(i.price_minor for i in items),
                "currency": "INR",
                "availability": bucket_for_group(
                    [AvailabilityBucket(buckets[i.sku]) for i in items]
                ).value,
                "variants": len(items),
                # The axes, not the variants: a group with one size needs no
                # question, and one with two can be asked about straight from
                # the search result instead of costing a `read-item` round trip
                # to learn that a question exists at all.
                "option_axes": group.option_axes,
                "image": group.media[0] if group.media else None,
            }
        )
    # Ordered, always: an agent that re-reads a shop must see the same order —
    # and a cursor is only meaningful against a stable one. Ties on name are
    # broken by group id so two groups sharing a name cannot swap places between
    # pages and hide a row.
    results.sort(key=lambda r: (str(r["name"]), str(r["group"])))

    size = SEARCH_PAGE_SIZE if limit is None else max(1, min(int(limit), SEARCH_MAX_PAGE_SIZE))
    start = 0
    if cursor:
        # The cursor is the last group id of the previous page, not an offset:
        # an offset shifts under an insert and silently skips a row. Resolved by
        # lookup, so a cursor from a stale ordering restarts rather than
        # truncating — an agent that pages through a catalogue being edited
        # under it should see too much, never too little.
        ids = [str(r["group"]) for r in results]
        start = ids.index(cursor) + 1 if cursor in ids else 0

    page = results[start : start + size]
    next_cursor = str(page[-1]["group"]) if page and start + size < len(results) else ""
    body: dict[str, Any] = {"results": page, "query": query, "total": len(results)}
    if next_cursor:
        body["next_cursor"] = next_cursor
    return body


async def read_item(trait: TraitClient, group_id: str) -> dict[str, Any]:
    catalogue = await trait.catalog_read()
    group = next((g for g in catalogue.groups if g.id == group_id or g.slug == group_id), None)
    if group is None:
        raise ToolRefused(ReasonCode.NOT_FOUND, f"{group_id!r} is not a product group here.")
    items = [i for i in catalogue.items if i.group_id == group.id]
    low = {i.sku: i.low_stock_threshold for i in items}
    buckets = await _buckets(trait, [i.sku for i in items], low)
    return {
        "group": group.id,
        "name": group.name,
        "description": group.description,
        "option_axes": group.option_axes,
        "images": group.media,
        "variants": [
            {
                "sku": i.sku,
                "name": i.name,
                "options": i.options,
                "price_minor": i.price_minor,
                "currency": "INR",
                "availability": buckets.get(i.sku, "sold-out"),
                "images": i.media,
                "tags": i.tags,
            }
            for i in items
        ],
    }


async def add_line(
    trait: TraitClient,
    basket: Basket,
    sku: str,
    qty: int,
    parent: str | None,
    requirements: Requirements | None = None,
) -> dict[str, Any]:
    if qty < 1:
        raise ToolRefused(ReasonCode.NOT_FOUND, "A line needs a quantity of at least one.")
    catalogue = await trait.catalog_read()
    item = next((i for i in catalogue.items if i.sku == sku), None)
    if item is None:
        group = next((g for g in catalogue.groups if g.id == sku or g.slug == sku), None)
        if group is not None:
            variants = [i.sku for i in catalogue.items if i.group_id == group.id]
            raise ToolRefused(
                ReasonCode.VARIANT_REQUIRED,
                f"{sku!r} is a product group, not something that can be bought. Choose one "
                f"of its variants: {', '.join(variants)}",
                # The same list the sentence names, as data. An agent that has
                # to regex a comma-separated tail out of a message is an agent
                # this shop can break by rewording an error.
                {"group": group.id, "variants": variants, "option_axes": group.option_axes},
            )
        raise ToolRefused(ReasonCode.NOT_FOUND, f"{sku!r} is not a SKU this shop sells.")

    # Availability is checked here so the agent hears "two left" now rather than
    # at the tap, but the hold still happens only at the tap.
    stock = await trait.stock_read([sku])
    available = stock.get(sku, 0)
    wanted = qty + sum(ln.qty for ln in basket.lines if ln.sku == sku)
    if available < wanted:
        raise ToolRefused(
            ReasonCode.SOLD_OUT,
            quantity_refusal_detail(available, wanted),
            # `available` is already public through the availability bucket; the
            # exact count is what turns "sold out" into "ask for two instead of
            # three" without another round trip.
            {"sku": sku, "available": available, "requested": wanted},
        )

    basket.add(sku, qty, parent)
    return await summary(trait, basket, requirements)


async def summary(
    trait: TraitClient, basket: Basket, requirements: Requirements | None = None
) -> dict[str, Any]:
    """The basket as the Consumer would see it — priced by the Merchant, or not
    priced at all."""
    missing_details = details_missing(requirements, basket.details)
    body: dict[str, Any] = {
        "cart_id": basket.cart_id,
        "lines": [
            {"sku": ln.sku, "qty": ln.qty, **({"parent": ln.parent} if ln.parent else {})}
            for ln in basket.lines
        ],
        "destination_set": basket.destination is not None,
        "contact_set": bool(basket.contact),
        "fulfillment_option_id": basket.fulfillment_option_id or None,
        "discount_code": basket.discount_code,
        "details": dict(basket.details),
    }
    if missing_details:
        body["details_missing"] = missing_details
    if basket.quotable():
        assert basket.destination is not None
        quote, _ = await trait.quote(
            basket.lines,
            basket.destination,
            fulfillment_option_id=basket.fulfillment_option_id,
            discount_code=basket.discount_code,
            details=dict(basket.details) or None,
        )
        body["quote"] = quote.model_dump(mode="json")
        body["total_minor"] = quote.total_minor
        body["currency"] = quote.currency
        if missing_details:
            # Priced but not checkout-ready: required merchant details still
            # missing. `needs` is how an agent learns that without guessing.
            body["needs"] = ["details"]
    else:
        body["quote"] = None
        body["needs"] = [
            name
            for name, missing in (
                ("lines", not basket.lines),
                ("destination", basket.destination is None),
                ("fulfillment", not basket.fulfillment_option_id),
            )
            if missing
        ]
        if missing_details:
            body["needs"].append("details")
    return body


async def fulfillment_options(trait: TraitClient, basket: Basket) -> dict[str, Any]:
    if basket.destination is None or not basket.lines:
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            "The shop quotes delivery against a Destination and a basket; set both first.",
        )
    quote, _ = await trait.quote(basket.lines, basket.destination)
    return {
        "options": [
            {
                "id": o.id,
                "label": o.label,
                "cost_minor": o.cost_minor,
                "eta_days": o.eta_days,
            }
            for o in quote.fulfillment_options
        ]
    }


#: What the Consumer must fix for each Destination field, in the order the
#: fields are asked for. A pydantic dump names patterns and doc URLs; the agent
#: — and the shopper behind it — needs the fix, not the schema.
_DESTINATION_FIXES: tuple[tuple[str, str], ...] = (
    ("line1", "a street address in line1"),
    ("city", "a city"),
    ("state", "a 2-letter state code (e.g. TN for Tamil Nadu)"),
    ("postal_code", "a 6-digit postal code"),
)


def _destination_fix(exc: ValidationError) -> str:
    bad = [
        fix
        for field, fix in _DESTINATION_FIXES
        if any(err["loc"][:1] == (field,) for err in exc.errors())
    ]
    unknown = sorted(
        {
            str(err["loc"][-1])
            for err in exc.errors()
            if err.get("type") == "extra_forbidden" and err.get("loc")
        }
    )
    if not bad:
        base = (
            "That address needs a street address, a city, a 2-letter state code "
            "and a 6-digit postal code."
        )
    elif len(bad) == 1:
        base = f"That address needs {bad[0]}."
    elif len(bad) == 2:
        base = f"That address needs {bad[0]} and {bad[1]}."
    else:
        base = f"That address needs {', '.join(bad[:-1])} and {bad[-1]}."
    if unknown:
        base += (
            f" Unrecognized keys: {', '.join(unknown)} — use line1/line2/city/state/postal_code."
        )
    return base


#: Aliases generic agent clients send for Destination fields (Merchant Center /
#: UCP-style names). Normalized at the agent input boundary only — the Merchant
#: trait contract (`Destination`, door schema) stays strict. First name wins;
#: anything else (locality, phone, …) is dropped, not refused: it is data the
#: shop does not quote on.
_DESTINATION_ALIASES: dict[str, tuple[str, ...]] = {
    "line1": ("address_line1", "address1", "street", "street_address", "address"),
    "line2": (
        "address_line2",
        "address2",
    ),
    "city": ("town",),
    "state": ("region", "province"),
    "postal_code": ("zip", "zip_code", "zipcode", "postalcode", "pincode", "pin"),
    "country": (),
}


def _normalize_destination(payload: dict[str, Any]) -> dict[str, Any]:
    """Map aliases to canonical keys, drop unknowns, keep canonical first."""
    lowered = {str(k).strip().lower(): v for k, v in payload.items()}
    out: dict[str, Any] = {}
    for canonical, aliases in _DESTINATION_ALIASES.items():
        if canonical in lowered and lowered[canonical] not in (None, ""):
            out[canonical] = lowered[canonical]
            continue
        for alias in aliases:
            if alias in lowered and lowered[alias] not in (None, ""):
                out[canonical] = lowered[alias]
                break
    # `country` defaults on the model; only pass it when actually sent.
    return out


def set_destination(basket: Basket, payload: dict[str, Any]) -> None:
    """Refusals carry `missing` / `unknown` as data, not just prose: a generic
    agent retries from fields, never by parsing the sentence."""
    normalized = _normalize_destination(payload)
    lowered_keys = {str(k).strip().lower() for k in payload}
    known_keys = set(_DESTINATION_ALIASES) | {
        alias for aliases in _DESTINATION_ALIASES.values() for alias in aliases
    }
    unknown = sorted(lowered_keys - known_keys - {""})
    try:
        basket.destination = Destination.model_validate(normalized)
    except ValidationError as exc:
        missing = [
            field
            for field, _ in _DESTINATION_FIXES
            if any(err["loc"][:1] == (field,) for err in exc.errors())
        ]
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            _destination_fix(exc),
            {"missing": missing, "unknown": unknown},
        ) from None
    except Exception:  # noqa: BLE001 — anything else is still a shape problem
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            "That address needs a street address, a city, a 2-letter state code "
            "and a 6-digit postal code.",
        ) from None
    # A Destination change re-prices and re-hashes, so a chosen option no longer
    # applies to it.
    basket.fulfillment_option_id = ""


def set_contact(basket: Basket, payload: dict[str, Any]) -> None:
    contact = {k: str(v) for k, v in payload.items() if k in {"email", "phone"} and v}
    if not contact:
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            "A Contact Point is an email, a phone, or both — and one is needed.",
            {"missing": ["contact"], "any_of": ["email", "phone"]},
        )
    basket.contact = contact


def details_missing(requirements: Requirements | None, details: dict[str, str]) -> list[str]:
    """Required merchant keys with no value. Empty when the shop declares
    nothing — no declaration means nothing to miss."""
    if requirements is None:
        return []
    return [f.key for f in requirements.fields if f.required and not details.get(f.key)]


def set_details(
    basket: Basket, requirements: Requirements | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Merge one shop's own extras into the basket, validated against the
    merchant's declaration. Partial sets allowed — requiredness is enforced at
    `summary`/`start-checkout`, not here, so an agent can build piece by piece.
    An empty-string value clears a key. Unknown keys refuse with names rather
    than disappearing: silently dropping a field the agent thought it set is
    how a delivery goes to the wrong slot."""
    if requirements is None or not requirements.fields:
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            "This shop declares no extra details — address and contact are all it asks.",
        )
    declared = {f.key: f for f in requirements.fields}
    unknown = sorted(str(k) for k in payload if str(k) not in declared)
    if unknown:
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            f"Unrecognized detail keys: {', '.join(unknown)} — this shop asks for: "
            f"{', '.join(f.key for f in requirements.fields)}.",
            {"unknown": unknown, "declared": [f.key for f in requirements.fields]},
        )
    invalid: dict[str, str] = {}
    merged = dict(basket.details)
    for key, field in declared.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            merged.pop(key, None)
            continue
        if not isinstance(value, str):
            invalid[key] = f"must be a string, not {type(value).__name__}"
            continue
        if len(value) > MAX_REQUIREMENT_VALUE_LENGTH:
            invalid[key] = f"at most {MAX_REQUIREMENT_VALUE_LENGTH} characters"
            continue
        if field.type == "enum" and value not in field.values:
            invalid[key] = f"must be one of: {', '.join(field.values)}"
            continue
        if field.pattern:
            import re as _re

            if not _re.fullmatch(field.pattern, value):
                invalid[key] = "does not match this shop's pattern"
                continue
        merged[key] = value
    if invalid:
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            f"These details do not fit what the shop asks: {', '.join(sorted(invalid))}.",
            {"invalid": invalid},
        )
    basket.details = merged
    # Details ride into the quote, so a change re-prices: same rule as lines
    # and Destination — the chosen option no longer applies to it.
    basket.fulfillment_option_id = ""
    return {"details": dict(merged), "missing": details_missing(requirements, merged)}


def clear_basket(basket: Basket) -> dict[str, Any]:
    """Empty the basket back to a fresh state: lines, Destination, Contact
    Point, chosen fulfillment and code all go, and the cart id rotates so a
    later checkout cannot idempotently return an order from the cleared cart.

    In place, so the one save path in `_run_tool` persists exactly this: a
    delete-then-save here would race the save that follows dispatch and
    resurrect the cleared basket.

    Used when the shopper starts over. A basket that outlives its conversation
    puts old lines into the next quote, and every one of those lines breaks
    the invariant that a cart line traces to something this conversation asked
    for or accepted.
    """
    removed = [ln.sku for ln in basket.lines]
    basket.lines = []
    basket.destination = None
    basket.contact = {}
    basket.fulfillment_option_id = ""
    basket.discount_code = None
    basket.details = {}
    basket.cart_id = f"cart_{secrets.token_hex(8)}"
    return {
        "cleared": True,
        "removed": removed,
        "quote": None,
        "needs": ["lines", "destination", "fulfillment"],
    }


async def choose_fulfillment(
    trait: TraitClient,
    basket: Basket,
    option_id: str,
    requirements: Requirements | None = None,
) -> dict[str, Any]:
    options = await fulfillment_options(trait, basket)
    offered = [str(o["id"]) for o in options["options"]]
    if option_id not in set(offered):
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            f"{option_id!r} is not offered for that Destination. Offered: " f"{', '.join(offered)}",
            {"offered": offered},
        )
    basket.fulfillment_option_id = option_id
    return await summary(trait, basket, requirements)


async def apply_public_code(
    trait: TraitClient,
    basket: Basket,
    code: str,
    requirements: Requirements | None = None,
) -> dict[str, Any]:
    """Public codes only. A private code is entered by the Consumer on the
    approve page and never travels through an agent (ADR-0015)."""
    if not basket.quotable():
        raise ToolRefused(
            ReasonCode.NOT_FOUND,
            "A code re-prices a basket, so the basket has to be quotable first.",
        )
    assert basket.destination is not None
    previous = basket.discount_code
    basket.discount_code = code
    try:
        return await summary(trait, basket, requirements)
    except TraitError as exc:
        basket.discount_code = previous
        raise ToolRefused(exc.code, exc.detail) from None


def unsupported(tool: ToolName) -> ToolRefused:
    return ToolRefused(
        ReasonCode.NOT_FOUND,
        f"{tool.value} is listed but not wired in this build. It refuses rather than "
        f"pretending to have worked.",
    )


def method_for(name: str) -> PaymentMethod:
    try:
        return PaymentMethod(name)
    except ValueError:
        return PaymentMethod.UPI
