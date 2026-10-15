"""The agent-facing tools behind `/agent/mcp`, one tool at a time.

Covers what the envelopes cannot: the behaviour of a single tool rather than
the wiring around it. First case: `search` with an empty query is the
whole-catalogue browse — the call an ideas, gifts, or occasion question starts
from, and the reason no separate browse tool exists.
"""

from __future__ import annotations

from openstore.sidecar.basket import Basket
from openstore.sidecar.protocols import tools
from openstore.sidecar.trait.client import TraitClient
from openstore.sidecar.trait.models import Destination


async def test_search_with_an_empty_query_lists_the_whole_catalogue(
    trait: TraitClient,
) -> None:
    catalog = await trait.catalog_read()
    result = await tools.search(trait, "")
    assert {row["group"] for row in result["results"]} == {group.id for group in catalog.groups}


async def test_search_with_words_still_filters(trait: TraitClient) -> None:
    result = await tools.search(trait, "tote")
    assert result["results"], "the seed catalogue has a tote group"
    assert len(result["results"]) < len((await trait.catalog_read()).groups)


def test_set_destination_accepts_generic_client_aliases() -> None:
    basket = Basket(agent_id="a1")
    tools.set_destination(
        basket,
        {
            "address_line1": "147, A/16, Paneer Nagar Main Road",
            "address_line2": "DABC Madhuram Apartments",
            "city": "Chennai",
            "locality": "Mogappair",
            "country": "IN",
            "phone": "12345678901",
            "postal_code": "600037",
            "state": "TN",
        },
    )
    assert basket.destination is not None
    assert basket.destination.city == "Chennai"
    assert basket.destination.postal_code == "600037"


def test_clear_basket_empties_everything_and_names_what_went() -> None:
    from openstore.sidecar.basket import Basket
    from openstore.sidecar.trait.models import Destination

    basket = Basket(agent_id="a1")
    basket.add("SD-TOTE-BLK-M", 1)
    basket.add("SD-GIFTWRAP", 1, parent="SD-TOTE-BLK-M")
    basket.destination = Destination(line1="x", city="Mumbai", state="MH", postal_code="400028")
    basket.contact = {"phone": "+919000000001"}
    basket.fulfillment_option_id = "rest-of-india"
    basket.discount_code = "SPOILED10"
    before = basket.cart_id

    result = tools.clear_basket(basket)

    assert result["cleared"] is True
    assert sorted(result["removed"]) == ["SD-GIFTWRAP", "SD-TOTE-BLK-M"]
    assert basket.lines == []
    assert basket.destination is None
    assert basket.contact == {}
    assert basket.fulfillment_option_id == ""
    assert basket.discount_code is None
    # A later checkout must not idempotently return an order from the cleared cart.
    assert basket.cart_id != before


def test_clear_basket_on_an_empty_basket_is_still_fresh() -> None:
    from openstore.sidecar.basket import Basket

    basket = Basket(agent_id="a1")
    result = tools.clear_basket(basket)
    assert result == {
        "cleared": True,
        "removed": [],
        "quote": None,
        "needs": ["lines", "destination", "fulfillment"],
    }


BENGALURU = Destination(
    line1="12 Church Street", city="Bengaluru", state="KA", postal_code="560001"
)


def _basket() -> Basket:
    return Basket(agent_id="agent_shopper")


async def test_adding_a_line_un_chooses_the_delivery_option(trait: TraitClient) -> None:
    """The transcript that started this: a delivery option was chosen while the
    basket was still being built, another item was added, and the quote came
    back priced against the option the shop had offered for the *smaller*
    basket. A cart change invalidates the choice, exactly as an address change
    does."""
    basket = _basket()
    basket.destination = BENGALURU
    await tools.add_line(trait, basket, "SD-TOTE-RED-L", 1, None)
    await tools.choose_fulfillment(trait, basket, "karnataka")
    assert basket.quotable()

    summary = await tools.add_line(trait, basket, "SD-PHONECHARM", 1, None)

    assert basket.fulfillment_option_id == ""
    assert summary["fulfillment_option_id"] is None
    assert summary["quote"] is None
    assert summary["needs"] == ["fulfillment"]


async def test_removing_a_line_un_chooses_the_delivery_option(trait: TraitClient) -> None:
    basket = _basket()
    basket.destination = BENGALURU
    await tools.add_line(trait, basket, "SD-TOTE-RED-L", 1, None)
    await tools.add_line(trait, basket, "SD-PHONECHARM", 1, None)
    await tools.choose_fulfillment(trait, basket, "karnataka")

    assert basket.remove("SD-PHONECHARM") is True
    assert basket.fulfillment_option_id == ""


async def test_a_removal_that_removes_nothing_leaves_the_choice_alone(trait: TraitClient) -> None:
    basket = _basket()
    basket.destination = BENGALURU
    await tools.add_line(trait, basket, "SD-TOTE-RED-L", 1, None)
    await tools.choose_fulfillment(trait, basket, "karnataka")

    assert basket.remove("SD-CAP-M") is False
    assert basket.fulfillment_option_id == "karnataka"


async def test_clear_basket_leaves_nothing_of_the_last_conversation(trait: TraitClient) -> None:
    """Lines, address, contact, delivery and code — all of it. A basket that
    kept any of them would put something in the next conversation's quote that
    nobody in it had asked for."""
    basket = _basket()
    basket.destination = BENGALURU
    basket.contact = {"email": "someone@example.test"}
    await tools.add_line(trait, basket, "SD-CHARMBAR-SEAT", 1, None)
    await tools.choose_fulfillment(trait, basket, "karnataka")
    was = basket.cart_id

    result = tools.clear_basket(basket)

    assert result["removed"] == ["SD-CHARMBAR-SEAT"]
    assert basket.lines == []
    assert basket.destination is None
    assert basket.contact == {}
    assert basket.fulfillment_option_id == ""
    assert basket.discount_code is None
    # A new cart id too: door 7 keys idempotency on `cart_id:attempt`, so a
    # cleared basket that kept its id could return the old cart's order.
    assert basket.cart_id != was


async def test_choose_fulfillment_with_no_id_says_it_chose_nothing(trait: TraitClient) -> None:
    """`choose-fulfillment {}` lists what is offered. It answered with a bare
    list, an agent read that as success and told the shopper "Done!" over a
    basket with no delivery set."""
    from openstore.sidecar.core.codes import ToolName
    from openstore.sidecar.protocols import agent_routes

    basket = _basket()
    basket.destination = BENGALURU
    await tools.add_line(trait, basket, "SD-TOTE-RED-L", 1, None)

    surface = agent_routes.get_surface()
    was = surface.trait
    surface.trait = trait
    try:
        offered = await agent_routes._dispatch(
            ToolName.CHOOSE_FULFILLMENT, "agent_shopper", {}, basket
        )
    finally:
        surface.trait = was

    assert offered["chosen"] is None
    assert "choose-fulfillment" in offered["next"]
    assert [o["id"] for o in offered["options"]] == ["karnataka"]
    assert basket.fulfillment_option_id == ""


async def test_search_carries_the_option_axes_of_each_group(trait: TraitClient) -> None:
    """A group with axes needs a question before anything can be added, and the
    caller should not have to spend a `read-item` to find that out."""
    result = await tools.search(trait, "tote")
    row = next(r for r in result["results"] if r["group"] == "tote")
    catalog = await trait.catalog_read()
    group = next(g for g in catalog.groups if g.id == "tote")
    assert row["option_axes"] == group.option_axes
    assert row["option_axes"], "the seed tote has option axes"


def test_refusals_carry_missing_and_offered_as_data() -> None:
    """B-phase convention: every refusal an agent must recover from carries
    machine-readable fields — missing keys, unknown keys, offered ids — so a
    model retries from data, never by parsing the sentence."""
    from openstore.sidecar.basket import Basket
    from openstore.sidecar.protocols.tools import ToolRefused

    basket = Basket(agent_id="a1")
    try:
        tools.set_destination(basket, {"address_line1": "", "city": "Chennai"})
        raise AssertionError("should have refused")
    except ToolRefused as refusal:
        assert "state" in refusal.fields["missing"]
        assert refusal.fields["unknown"] == []
    try:
        tools.set_contact(basket, {})
        raise AssertionError("should have refused")
    except ToolRefused as refusal:
        assert refusal.fields["any_of"] == ["email", "phone"]


def _slot_requirements(required: bool = False):
    from openstore.sidecar.trait.models import RequirementField, Requirements

    return Requirements(
        fields=[
            RequirementField(
                key="delivery_slot",
                type="enum",
                required=required,
                label="Delivery slot",
                values=["morning", "evening"],
            ),
            RequirementField(key="delivery_instructions", type="string", required=False),
        ]
    )


def test_set_details_merges_validates_and_clears() -> None:
    from openstore.sidecar.basket import Basket
    from openstore.sidecar.protocols.tools import ToolRefused, set_details

    basket = Basket(agent_id="a1")
    req = _slot_requirements()
    out = set_details(basket, req, {"delivery_slot": "morning"})
    assert out["details"] == {"delivery_slot": "morning"}
    assert out["missing"] == []
    # Unknown keys refuse with names, never disappear.
    try:
        set_details(basket, req, {"nope": "x"})
        raise AssertionError("should have refused")
    except ToolRefused as refusal:
        assert refusal.fields["unknown"] == ["nope"]
    # Invalid enum refuses with the valid set.
    try:
        set_details(basket, req, {"delivery_slot": "midnight"})
        raise AssertionError("should have refused")
    except ToolRefused as refusal:
        assert "invalid" in refusal.fields
    # Empty string clears.
    set_details(basket, req, {"delivery_instructions": "call me"})
    out = set_details(basket, req, {"delivery_instructions": ""})
    assert out["details"] == {"delivery_slot": "morning"}


def test_set_details_refuses_everything_without_a_declaration() -> None:
    from openstore.sidecar.basket import Basket
    from openstore.sidecar.protocols.tools import ToolRefused, set_details

    try:
        set_details(Basket(agent_id="a1"), None, {"anything": "x"})
        raise AssertionError("should have refused")
    except ToolRefused as refusal:
        assert "no extra details" in refusal.detail
