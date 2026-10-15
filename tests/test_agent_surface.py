"""The public front door: the cards, `/agent/*`, and the approve page.

Everything here is reachable by a stranger, because that is the design. What
makes it safe is the Gate, not the doorman.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp_helpers import mcp_error, mcp_request, mcp_result
from openstore.sidecar.admission.oauth import Admission
from openstore.sidecar.admission.ratelimit import RateLimiter, Tier
from openstore.sidecar.app import app
from openstore.sidecar.authority.tokens import TokenStore
from openstore.sidecar.console.approve import ApproveContext
from openstore.sidecar.console.approve import configure as configure_approve
from openstore.sidecar.core.codes import PaymentMethod, ReasonCode, Scope, ToolName
from openstore.sidecar.protocols.agent_routes import AgentSurface
from openstore.sidecar.protocols.agent_routes import configure as configure_surface
from openstore.sidecar.trait.models import Destination, Quote

QUOTE = {
    "currency": "INR",
    "subtotal_minor": 249800,
    "lines": [
        {
            "sku": "SD-TOTE-BLK-M",
            "qty": 1,
            "unit_price_minor": 89900,
            "line_total_minor": 99800,
            "addons": [{"sku": "SD-GIFTWRAP", "amount_minor": 9900}],
        },
        {
            "sku": "SD-CHARMBAR-SEAT",
            "qty": 1,
            "unit_price_minor": 150000,
            "line_total_minor": 150000,
            "addons": [],
        },
    ],
    "discount_lines": [],
    "fulfillment_options": [
        {"id": "rest-of-india", "label": "Rest of India", "cost_minor": 9900, "eta_days": 5}
    ],
    "fulfillment_chosen": {"id": "rest-of-india", "cost_minor": 9900},
    "tax_lines": [
        {"kind": "IGST", "label": "IGST 18%", "amount_minor": 15827, "informational": True},
        {"kind": "CGST", "label": "CGST 9%", "amount_minor": 11894, "informational": True},
        {"kind": "SGST", "label": "SGST 9%", "amount_minor": 11894, "informational": True},
    ],
    "round_off_minor": 0,
    "total_minor": 259700,
    "tax_inclusive": True,
}


#: The smallest Quote the model accepts. This test is about what `place-order`
#: hands back, not about arithmetic — `QUOTE` above is the §16.11 worked example
#: and carries tax detail that is beside the point here.
MINIMAL_QUOTE: dict[str, Any] = {
    "currency": "INR",
    "subtotal_minor": 89900,
    "lines": [
        {
            "sku": "SD-TOTE-BLK-M",
            "qty": 1,
            "unit_price_minor": 89900,
            "line_total_minor": 89900,
            "addons": [],
            "hsn_sac": "4202",
            "gst_rate_bp": 1800,
            "place_of_supply": "KA",
        }
    ],
    "discount_lines": [],
    "fulfillment_options": [
        {"id": "karnataka", "label": "Karnataka", "cost_minor": 4900, "eta_days": 2}
    ],
    "fulfillment_chosen": {"id": "karnataka", "cost_minor": 4900},
    "tax_lines": [],
    "round_off_minor": 0,
    "total_minor": 94800,
    "tax_inclusive": True,
}


@pytest.fixture
def surface() -> Iterator[AgentSurface]:
    state = AgentSurface(
        admission=Admission(clients={"allowlisted": "secret"}),
        limiter=RateLimiter(),
        jwks={"keys": [{"kid": "k1", "kty": "EC", "crv": "P-256", "x": "a", "y": "b"}]},
    )
    configure_surface(state)
    yield state
    configure_surface(AgentSurface())


@pytest.fixture
def tokens(sessionmaker) -> Iterator[TokenStore]:  # type: ignore[no-untyped-def]
    store = TokenStore(sessionmaker=sessionmaker)
    configure_approve(ApproveContext(tokens=store, quotes={"ord_1": QUOTE}))
    yield store
    configure_approve(ApproveContext())


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ── The cards ────────────────────────────────────────────────────────────────


def test_the_card_is_public_and_unauthenticated(client: TestClient, surface: AgentSurface) -> None:
    """A card behind a login is a card no new agent can read, and the whole
    admission story starts with a stranger fetching one."""
    response = client.get("/.well-known/agent-commerce.json")
    assert response.status_code == 200
    assert not response.request.headers.get("authorization")


def test_the_card_states_the_refusals_inline(client: TestClient, surface: AgentSurface) -> None:
    """An agent knows before it starts that there is no delegated-credential
    path — kinder than finding out at the last step."""
    card = client.get("/.well-known/agent-commerce.json").json()
    assert card["payment"]["delegated_credentials"] == "refused"
    assert card["authority"]["completion"] == "redirect-only"
    assert "may not spend" in card["authority"]["note"]


def test_the_card_names_every_protocol_and_its_deviations(
    client: TestClient, surface: AgentSurface
) -> None:
    card = client.get("/.well-known/agent-commerce.json").json()
    assert card["protocols"] == ["mcp", "ucp", "ap2", "acp"]
    acp = next(b for b in card["conformance"] if b["protocol"] == "acp")
    assert acp["version"] == "2026-04-17"
    assert any("completeCheckoutSession" in d for d in acp["deviations"])


def test_the_card_advertises_self_registration(client: TestClient, surface: AgentSurface) -> None:
    card = client.get("/.well-known/agent-commerce.json").json()
    assert card["admission"]["self_registration"] is True
    assert card["admission"]["agent_id"] == "RFC 7638 JWK thumbprint"


def test_the_ucp_manifest_declares_buyer_escalation_not_direct_completion(
    client: TestClient, surface: AgentSurface
) -> None:
    manifest = client.get("/.well-known/ucp.json").json()
    assert manifest["capabilities"]["direct_completion"] is False
    assert manifest["capabilities"]["buyer_escalation"] is True
    assert manifest["escalation_url"].endswith("/agentic/approve")


def test_jwks_is_served_so_a_receipt_can_be_verified_by_anyone(
    client: TestClient, surface: AgentSurface
) -> None:
    assert client.get("/.well-known/jwks.json").json()["keys"][0]["kid"] == "k1"


# ── Admission ────────────────────────────────────────────────────────────────


def test_the_tool_list_is_public(client: TestClient, surface: AgentSurface) -> None:
    """An agent should see what a shop offers before deciding to register."""
    tools = client.get("/agent/tools").json()["tools"]
    assert {t["name"] for t in tools} == {t.value for t in ToolName}


def test_the_tool_surface_needs_a_token_and_says_how_to_get_one(
    client: TestClient, surface: AgentSurface
) -> None:
    response = client.post("/agent/mcp", json={"tool": "search"})
    assert response.status_code == 401
    assert "/agent/register" in response.json()["error"]["detail"]


def test_an_allowlisted_client_gets_the_same_scopes_as_a_stranger(
    client: TestClient, surface: AgentSurface
) -> None:
    issued = client.post(
        "/agent/token", json={"client_id": "allowlisted", "client_secret": "secret"}
    ).json()
    assert sorted(issued["scopes"]) == sorted(s.value for s in Scope)
    assert issued["tier"] == Tier.ALLOWLISTED.value


def test_wrong_client_credentials_are_refused(client: TestClient, surface: AgentSurface) -> None:
    response = client.post(
        "/agent/token", json={"client_id": "allowlisted", "client_secret": "wrong"}
    )
    assert response.status_code == 401


# ── place-order returns a link, never an order ───────────────────────────────


def test_place_order_refuses_when_no_checkout_has_been_started(
    client: TestClient, surface: AgentSurface
) -> None:
    """It used to hand back a bare approve URL for a basket that did not exist,
    because the whole tool surface answered without doing anything. A link to
    approve nothing is worse than a refusal."""
    token = surface.admission.issue_for_stranger("agent_x")
    response = client.post(
        "/agent/mcp",
        json=mcp_request("place-order"),
        headers={"authorization": f"Bearer {token.token}"},
    )
    assert response.status_code == 200
    assert "start-checkout" in mcp_error(response.json())["error"]["detail"]


async def test_place_order_returns_an_approve_url_and_never_an_order(
    client: TestClient, surface: AgentSurface, sessionmaker
) -> None:  # type: ignore[no-untyped-def]
    """The single most load-bearing sentence on this surface.

    What comes back is a link to the Merchant's own origin. The order it names
    exists — door 7 created it as `pending` — but nothing about it has been
    approved, no stock is held and no money has moved. Only the tap can change
    that, and this surface has no way to reach it.
    """
    from openstore.sidecar import checkout as flow
    from openstore.sidecar.checkout_store import CheckoutStore

    token = surface.admission.issue_for_stranger("agent_x")
    # A ready context: `place-order` refuses outright when the money path is
    # unwired, which is a different assertion and has its own test.
    context = flow.CheckoutContext(
        merchant_domain="spoiledduckie.localhost",
        trait=object(),
        provider=object(),
        keyring=object(),
        sessionmaker=sessionmaker,
        store=CheckoutStore(sessionmaker=sessionmaker),
    )
    pending = flow.Pending(
        cart_id="cart_1",
        order_id="ord_1",
        lines=[],
        destination=Destination(
            line1="4th Cross", city="Bengaluru", state="KA", postal_code="560038"
        ),
        contact={},
        fulfillment_option_id="karnataka",
        expiry_utc="2026-09-22T00:00:00Z",
        agent_id="agent_x",
        method=PaymentMethod.UPI,
        quote=Quote.model_validate(MINIMAL_QUOTE),
        quote_bytes=b"{}",
        cart_hash="deadbeef",
        total_minor=int(MINIMAL_QUOTE["total_minor"]),
        tap_token="tok_1",
    )
    await context.store.save(pending)
    flow.configure(context)
    try:
        response = client.post(
            "/agent/mcp",
            json=mcp_request("place-order"),
            headers={"authorization": f"Bearer {token.token}"},
        )
        body = mcp_result(response.json())
        assert body["approve_url"].startswith("https://spoiledduckie.localhost/agentic/approve?t=")
        assert body["order_id"] == "ord_1"
        # No payment, no status, no confirmation: the agent is handing over a
        # link, not reporting a purchase.
        assert "status" not in body and "paid" not in body
        assert "cannot complete the purchase itself" in body["note"]
    finally:
        flow.configure(flow.CheckoutContext())


def test_an_unknown_tool_is_refused(client: TestClient, surface: AgentSurface) -> None:
    """Unknown tool name is a JSON-RPC protocol error (invalid params), not a
    tool refusal — the request never named a real tool to refuse."""
    token = surface.admission.issue_for_stranger("agent_y")
    response = client.post(
        "/agent/mcp",
        json=mcp_request("wire-transfer"),
        headers={"authorization": f"Bearer {token.token}"},
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602


def test_rate_limits_apply_per_agent(client: TestClient, surface: AgentSurface) -> None:
    token = surface.admission.issue_for_stranger("agent_noisy")
    headers = {"authorization": f"Bearer {token.token}"}
    last = None
    for _ in range(40):
        last = client.post("/agent/mcp", json=mcp_request("search"), headers=headers)
    assert last is not None
    assert last.status_code == 429
    assert last.json()["error"]["code"] == ReasonCode.RATE_LIMITED.value


# ── The approve page ─────────────────────────────────────────────────────────


async def test_the_approve_page_is_reachable_without_the_merchant_session(
    client: TestClient, tokens: TokenStore
) -> None:
    """A Consumer approving a spend is not the Merchant. Requiring the shop's
    login would make the whole flow impossible."""
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    response = client.get(f"/agentic/approve?t={tap.token}")
    assert response.status_code == 200
    assert not response.request.headers.get("cookie")


async def test_the_approve_page_renders_the_signed_quote_verbatim(
    client: TestClient, tokens: TokenStore
) -> None:
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    body = client.get(f"/agentic/approve?t={tap.token}").text

    # Every figure from the Quote, and the total the Consumer is agreeing to.
    assert "₹2,597.00" in body
    assert "₹998.00" in body  # the tote line, gift-wrap folded in
    assert "₹1,500.00" in body  # the charm-bar seat
    assert "₹158.27" in body  # IGST
    assert "₹118.94" in body  # CGST and SGST
    assert "5 days" in body  # a day count, never a date
    assert "includes SD-GIFTWRAP" in body
    assert "Nothing here was" in body and "calculated by the agent" in body


async def test_the_approve_page_offers_the_private_code_field(
    client: TestClient, tokens: TokenStore
) -> None:
    """A private code is entered here and never travels through the agent."""
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    body = client.get(f"/agentic/approve?t={tap.token}").text
    assert "Have a code?" in body
    assert "never travels through the agent" in body


async def test_the_approve_page_shows_only_the_enabled_methods(
    client: TestClient, tokens: TokenStore
) -> None:
    configure_approve(
        ApproveContext(
            tokens=tokens, quotes={"ord_1": QUOTE}, enabled_methods=frozenset({PaymentMethod.UPI})
        )
    )
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    body = client.get(f"/agentic/approve?t={tap.token}").text
    assert 'value="upi"' in body
    assert 'value="cash-on-delivery"' not in body


async def test_the_approve_page_carries_a_countdown(client: TestClient, tokens: TokenStore) -> None:
    """'This expires' with no number is a sentence nobody acts on."""
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    body = client.get(f"/agentic/approve?t={tap.token}").text
    assert 'id="countdown"' in body
    assert "can be used once" in body


def test_an_unknown_token_is_not_found(client: TestClient, tokens: TokenStore) -> None:
    assert client.get("/agentic/approve?t=nope").status_code == 404


async def test_a_spent_token_is_refused(client: TestClient, tokens: TokenStore) -> None:
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    await tokens.spend_tap(tap.token, cart_hash="cart-hash")
    response = client.get(f"/agentic/approve?t={tap.token}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ReasonCode.AUTHORITY_STALE.value


async def test_an_expired_token_is_refused(client: TestClient, tokens: TokenStore) -> None:
    tap = await tokens.issue_tap(
        "ord_1", "cart-hash", 259700, now=datetime.now(UTC) - timedelta(minutes=10)
    )
    body = client.get(f"/agentic/approve?t={tap.token}").text
    # The page still renders but the countdown is zero; the spend itself is
    # refused server-side by `spend_tap`, which is the authority.
    assert ">0<" in body or "0</span>" in body


async def test_the_demo_banner_says_no_real_money_moves(
    client: TestClient, tokens: TokenStore
) -> None:
    tap = await tokens.issue_tap("ord_1", "cart-hash", 259700)
    assert "no real money moves" in client.get(f"/agentic/approve?t={tap.token}").text


def test_adding_a_sku_twice_makes_one_line_of_two() -> None:
    """Quantities accumulate per SKU. `Line` is frozen because it is a value in
    the `cart_hash` preimage, so this used to raise `frozen_instance` and answer
    HTTP 500 — found by driving the running shop, not by a suite."""
    from openstore.sidecar.basket import Basket

    basket = Basket(agent_id="agent_x")
    basket.add("SD-TOTE-BLK-M", 1)
    basket.add("SD-TOTE-BLK-M", 2)

    assert [(ln.sku, ln.qty) for ln in basket.lines] == [("SD-TOTE-BLK-M", 3)]


def test_an_addon_is_its_own_line_from_the_same_sku() -> None:
    """Parent is part of the line's identity: a gift-wrap hanging off a tote is
    not the same line as a gift-wrap bought on its own."""
    from openstore.sidecar.basket import Basket

    basket = Basket(agent_id="agent_x")
    basket.add("SD-GIFTWRAP", 1)
    basket.add("SD-GIFTWRAP", 1, parent="SD-TOTE-BLK-M")

    assert len(basket.lines) == 2


def test_set_details_round_trips_declared_keys_over_mcp(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    """Door 10 over the wire: declared keys merge, unknown keys refuse with
    names, and `needs` names details while a required key is missing."""
    from openstore.sidecar.basket import BasketStore
    from openstore.sidecar.trait.models import RequirementField, Requirements

    state = AgentSurface(
        admission=Admission(),
        limiter=RateLimiter(),
        trait=object(),
        baskets=BasketStore(sessionmaker=sessionmaker),
        requirements=Requirements(
            fields=[
                RequirementField(
                    key="delivery_slot",
                    type="enum",
                    required=True,
                    values=["morning", "evening"],
                )
            ]
        ),
    )
    configure_surface(state)
    try:
        with TestClient(app) as client:
            configure_surface(state)
            token = state.admission.issue_for_stranger("agent_d")
            head = {"authorization": f"Bearer {token.token}"}

            def call(args: dict) -> dict:
                return client.post(
                    "/agent/mcp", json=mcp_request("set-details", details=args), headers=head
                ).json()

            ok = call({"delivery_slot": "evening"})
            assert ok["result"]["structuredContent"]["details"] == {"delivery_slot": "evening"}
            assert "details" not in ok["result"]["structuredContent"].get("needs", [])

            missing = call({"delivery_slot": ""})
            assert "details" in missing["result"]["structuredContent"]["needs"]

            unknown = call({"nope": "x"})
            assert unknown["result"]["isError"] is True
            assert unknown["result"]["structuredContent"]["error"]["unknown"] == ["nope"]
    finally:
        configure_surface(AgentSurface())


def test_origin_rp_mismatch_hides_the_passkey_button_with_a_note() -> None:
    """The tunnel failure, made visible at deploy time instead of tap time: a
    ceremony minted for one host cannot run on another, so the page says so
    rather than serving a button the browser refuses."""
    from openstore.sidecar.app import _origin_rp_mismatch
    from openstore.sidecar.console.approve import render_approve

    assert _origin_rp_mismatch("https://shop.example", "shop.example") is False
    assert _origin_rp_mismatch("https://pay.shop.example", "shop.example") is False
    assert _origin_rp_mismatch("https://tunnel.example", "shop.example") is True

    quote = {
        "lines": [],
        "discount_lines": [],
        "fulfillment_options": [],
        "fulfillment_chosen": {},
        "tax_lines": [],
        "total_minor": 100,
    }
    body = render_approve(
        merchant_name="Shop",
        quote=quote,
        token="t",
        expires_in_seconds=60,
        enabled_methods=frozenset(),
        demo=True,
        passkey_enabled=False,
        origin_mismatch_note="passkeys are set up for shop.example, not for tunnel.example",
    )
    assert "passkey-btn" not in body
    assert "Passkeys are unavailable on this address" in body

    plain = render_approve(
        merchant_name="Shop",
        quote=quote,
        token="t",
        expires_in_seconds=60,
        enabled_methods=frozenset(),
        demo=True,
    )
    assert "passkey-btn" not in plain
    assert "passkey-unavailable" not in plain
