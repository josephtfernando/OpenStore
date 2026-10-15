"""The schemas are valid, cover all doors, and accept the golden Quotes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from openstore.sidecar.core.codes import Door
from referencing import Registry, Resource

SCHEMA_DIR = Path("src/openstore/sidecar/trait/schema")
QUOTE_SCHEMA = json.loads((SCHEMA_DIR / "quote.schema.json").read_text(encoding="utf-8"))
DOORS_SCHEMA = json.loads((SCHEMA_DIR / "doors.schema.json").read_text(encoding="utf-8"))
VECTORS = json.loads(Path("tests/GOLDEN/cart_hash/vectors.json").read_text(encoding="utf-8"))[
    "vectors"
]


_REGISTRY = Registry().with_resources(
    [
        (QUOTE_SCHEMA["$id"], Resource.from_contents(QUOTE_SCHEMA)),
        (DOORS_SCHEMA["$id"], Resource.from_contents(DOORS_SCHEMA)),
    ]
)


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=_REGISTRY)


def _property_names(node: Any) -> set[str]:
    """Every declared property name anywhere in a subschema — descriptions are
    prose and must not be matched against, or a comment explaining that a field
    is absent reads as the field being present."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            if key != "description":
                found |= _property_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _property_names(item)
    return found


def test_both_schemas_are_themselves_valid() -> None:
    Draft202012Validator.check_schema(QUOTE_SCHEMA)
    Draft202012Validator.check_schema(DOORS_SCHEMA)


def test_every_door_has_a_request_and_response_shape() -> None:
    """Nine required doors plus the optional tenth, and the registry is the
    list — not this file."""
    defs = DOORS_SCHEMA["$defs"]
    for door in Door:
        assert door.value in defs, f"door {door.value} has no shape"
        assert "request" in defs[door.value], door.value
        assert "response" in defs[door.value], door.value


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_golden_quotes_validate(vector: dict[str, Any]) -> None:
    _validator(QUOTE_SCHEMA).validate(vector["inputs"]["quote"])


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_golden_destinations_and_contacts_validate(vector: dict[str, Any]) -> None:
    defs = DOORS_SCHEMA["$defs"]
    _validator(defs["destination"]).validate(vector["inputs"]["destination"])
    _validator(defs["contact"]).validate(vector["inputs"]["contact"])


def test_a_float_price_is_refused() -> None:
    """Money is paise. A float here would round silently downstream."""
    quote = json.loads(json.dumps(VECTORS[0]["inputs"]["quote"]))
    quote["lines"][0]["unit_price_minor"] = 199.5
    assert not _validator(QUOTE_SCHEMA).is_valid(quote)


def test_a_positive_discount_is_refused() -> None:
    """The sign is intrinsic — a discount that adds money is not a discount."""
    quote = json.loads(json.dumps(VECTORS[2]["inputs"]["quote"]))
    quote["discount_lines"][0]["amount_minor"] = 10000
    assert not _validator(QUOTE_SCHEMA).is_valid(quote)


def test_a_dated_eta_is_refused() -> None:
    """A date in a Quote is a clock, and midnight would turn every re-quote into
    a spurious price-changed."""
    quote = json.loads(json.dumps(VECTORS[0]["inputs"]["quote"]))
    quote["fulfillment_options"][0]["eta_days"] = "2026-09-23"
    assert not _validator(QUOTE_SCHEMA).is_valid(quote)


def test_a_nonzero_round_off_is_refused() -> None:
    quote = json.loads(json.dumps(VECTORS[0]["inputs"]["quote"]))
    quote["round_off_minor"] = 50
    assert not _validator(QUOTE_SCHEMA).is_valid(quote)


def test_an_unknown_quote_field_is_refused() -> None:
    """Fail loud: a field nobody declared is a field nobody validates."""
    quote = json.loads(json.dumps(VECTORS[0]["inputs"]["quote"]))
    quote["convenience_fee_minor"] = 500
    assert not _validator(QUOTE_SCHEMA).is_valid(quote)


def test_orders_create_returns_the_salt_and_orders_read_never_does() -> None:
    """§6.3a's custody rule, asserted on the shapes rather than trusted to prose."""
    defs = DOORS_SCHEMA["$defs"]
    assert "order_salt_hex" in defs["orders.create"]["response"]["properties"]
    read_fields = _property_names(defs["orders.read"])
    assert not any(f.startswith("order_salt") for f in read_fields), read_fields


def test_orders_create_keys_on_cart_id_not_order_id() -> None:
    """It is the door that produces the order_id, so it cannot key on one — and a
    crash between request and response is exactly where duplicate orders are born."""
    create = DOORS_SCHEMA["$defs"]["orders.create"]["request"]
    assert "cart_id" in create["required"]
    assert "order_id" not in create["properties"]


def test_stock_read_is_the_only_door_exposing_an_exact_count() -> None:
    defs = DOORS_SCHEMA["$defs"]
    assert "stock" in defs["stock.read"]["response"]["properties"]
    for door in Door:
        if door is Door.STOCK_READ:
            continue
        assert "available" not in _property_names(defs[door.value])


def test_error_envelope_requires_a_code_and_a_detail() -> None:
    validator = _validator(DOORS_SCHEMA["$defs"]["error"])
    validator.validate({"error": {"code": "sold-out", "detail": "2 left; try 2 or fewer."}})
    assert not validator.is_valid({"error": {"detail": "something went wrong"}})


def test_models_and_schema_agree_on_the_quote() -> None:
    """`models.py` says the JSON Schema is the contract a Merchant implements
    against and these are what the sidecar holds. A field in one and not the
    other is a divergence that shows up as a validation error on a correct
    Merchant."""
    from openstore.sidecar.trait.models import Quote as QuoteModel

    schema_fields = set(QUOTE_SCHEMA["properties"])
    model_fields = set(QuoteModel.model_fields)
    assert schema_fields == model_fields, {
        "schema only": sorted(schema_fields - model_fields),
        "model only": sorted(model_fields - schema_fields),
    }


def test_models_and_schema_agree_on_the_quote_line() -> None:
    from openstore.sidecar.trait.models import QuoteLine

    schema_fields = set(QUOTE_SCHEMA["$defs"]["quoteLine"]["properties"])
    assert schema_fields == set(QuoteLine.model_fields)


def test_models_and_schema_agree_on_requirements() -> None:
    """Door 10, same rule as every other door: one shape in two places is a
    divergence waiting to refuse a correct Merchant."""
    from openstore.sidecar.trait.models import RequirementField, Requirements

    response = DOORS_SCHEMA["$defs"]["requirements.read"]["response"]
    assert set(response["required"]) == {"fields"}
    item_props = set(response["properties"]["fields"]["items"]["properties"])
    assert item_props == set(RequirementField.model_fields)
    assert set(Requirements.model_fields) == {"fields"}
    # Declaration caps match on both sides.
    assert response["properties"]["fields"]["maxItems"] == 20


def test_the_fake_quotes_something_the_schema_accepts() -> None:
    """The conformance fake is a Merchant. If its Quote does not validate
    against the published schema, the schema is describing nobody."""
    import asyncio

    import httpx
    from openstore.sidecar.trait.client import TraitClient
    from openstore.sidecar.trait.fake import make_app
    from openstore.sidecar.trait.models import Destination, Line
    from openstore.sidecar.trait.seed import seeded

    async def run() -> dict[str, object]:
        transport = httpx.ASGITransport(app=make_app(seeded(), "s"))
        async with TraitClient(
            "http://m",
            "s",
            client=httpx.AsyncClient(transport=transport, base_url="http://m"),
        ) as client:
            quote, _ = await client.quote(
                [Line(sku="SD-TOTE-BLK-M", qty=1), Line(sku="SD-CHARMBAR-SEAT", qty=1)],
                Destination(line1="Dadar West", city="Mumbai", state="MH", postal_code="400028"),
            )
            return quote.model_dump()

    _validator(QUOTE_SCHEMA).validate(asyncio.run(run()))
