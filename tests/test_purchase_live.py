"""A whole purchase against the **running demo**, over HTTP, like an agent.

Everything here was green as unit tests while the running system could not take
an order at all: the tool surface answered `accepted: true`, the approve page
posted to a route that did not exist, and nothing ever called the Gate. So this
suite does not import the money path — it *uses* it, through the same URLs an
agent and a Consumer would.

Skipped unless `OPENSTORE_LIVE_ORIGIN` points at a running stack, so the default
suite stays hermetic:

    make up
    OPENSTORE_LIVE_ORIGIN=http://127.0.0.1 uv run pytest tests/test_purchase_live.py
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp_helpers import mcp_error, mcp_request, mcp_result

ORIGIN = os.environ.get("OPENSTORE_LIVE_ORIGIN")
SHOP = os.environ.get("OPENSTORE_LIVE_SHOP", "spoiledduckie.localhost")
PROFILE = os.environ.get(
    "OPENSTORE_LIVE_AGENT_PROFILE", "http://buyer-chat:3001/.well-known/agent-profile.json"
)

pytestmark = pytest.mark.skipif(
    not ORIGIN, reason="set OPENSTORE_LIVE_ORIGIN to run against a running demo"
)

DESTINATION = {
    "line1": "4th Cross, Indiranagar",
    "city": "Bengaluru",
    "state": "KA",
    "postal_code": "560038",
}
CONTACT = {"email": "demo@spoiledduckie.test", "phone": "+919000000001"}


@pytest.fixture(scope="module")
def shop() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=str(ORIGIN), headers={"host": SHOP}, timeout=30) as client:
        yield client


@pytest.fixture(scope="module")
def token(shop: httpx.Client) -> str:
    """A token for the suite, by whichever admission route this deploy offers.

    **The allowlisted route when credentials exist**, and not to dodge a limit:
    a suite that walks several whole purchases is exactly the traffic the
    allowlisted tier is for, and "reputation buys throughput only" is only true
    if something actually uses the throughput. Self-registration is the fallback
    and is what `test_a_stranger_is_admitted_with_no_merchant_action` covers.

    Module-scoped because registration is rate-limited to five an hour — that
    limit is the door working, and registering per test would be routing around
    the product.
    """
    client_id = os.environ.get("OPENSTORE_LIVE_CLIENT_ID", "demo-agent")
    secret = os.environ.get("OPENSTORE_LIVE_CLIENT_SECRET", "demo-agent-secret")
    issued = shop.post("/agent/token", json={"client_id": client_id, "client_secret": secret})
    if issued.status_code == 200:
        assert issued.json()["tier"] == "allowlisted"
        return str(issued.json()["access_token"])

    response = shop.post("/agent/register", json={"name": "live-suite", "profile_url": PROFILE})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def test_a_stranger_is_admitted_with_no_merchant_action(shop: httpx.Client) -> None:
    """The other admission route, exercised once: no Merchant action, and the
    scopes stop short of spending."""
    response = shop.post("/agent/register", json={"name": "stranger", "profile_url": PROFILE})
    if response.status_code == 429:
        # Five registrations an hour, per SPEC §5. Hitting it is the door
        # working; skipping says so rather than reporting a broken product.
        pytest.skip("registration rate limit reached — the limiter is doing its job")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "self-registered"
    assert sorted(body["scopes"]) == ["build-basket", "confirm", "search", "start-checkout"]
    assert "let you spend" in body["note"]


@pytest.fixture(autouse=True, scope="module")
def clean_slate(shop: httpx.Client, token: str) -> None:
    """Abandon anything this agent left behind.

    The sidecar is long-lived and the agent_id is a key thumbprint, so a second
    run of this suite meets its own leftovers: a basket with lines already in it
    and a checkout still waiting on a tap. Cancelling them is the product's own
    pre-money path, not a back door — the same `cancel-order` a Consumer's agent
    would call.
    """
    while True:
        response = shop.post(
            "/agent/mcp",
            json=mcp_request("place-order"),
            headers={"authorization": f"Bearer {token}"},
        )
        result = response.json()["result"]
        if result.get("isError"):
            break
        order_id = result["structuredContent"]["order_id"]
        shop.post(
            "/agent/mcp",
            json=mcp_request("cancel-order", order_id=order_id, reason="test setup"),
            headers={"authorization": f"Bearer {token}"},
        )
    for line in ("SD-CAP-S", "SD-KEYCHAIN", "SD-STICKERS", "SD-PINSET", "SD-TOTE-BLK-M"):
        shop.post(
            "/agent/mcp",
            json=mcp_request("remove-line", sku=line),
            headers={"authorization": f"Bearer {token}"},
        )


@pytest.fixture(scope="module")
def buyable(shop: httpx.Client, token: str) -> str:
    """Any variant the shop currently has, looked up **once**.

    Never a hardcoded SKU: this suite buys things, and a fixed SKU stops working
    once the suite has bought enough of it — which is what happened. Looked up
    once because walking every group per test is dozens of calls, and the shop
    rate-limits per IP for good reason.
    """
    for group in tool(shop, token, "search", query="")["results"]:
        for variant in tool(shop, token, "read-item", group=group["group"])["variants"]:
            if variant["availability"] == "in-stock" and "addon" not in variant.get("tags", []):
                return str(variant["sku"])
    raise AssertionError("the shop has nothing in stock to buy")


def tool(shop: httpx.Client, token: str, name: str, **args: Any) -> dict[str, Any]:
    response = shop.post(
        "/agent/mcp",
        json=mcp_request(name, **args),
        headers={"authorization": f"Bearer {token}"},
    )
    if response.status_code == 429:
        # A whole purchase is a few dozen calls, so running this suite twice
        # inside its window trips the shop's own per-IP limit. That is the
        # limiter working; saying so beats reporting a broken product.
        pytest.skip(f"the shop is rate-limiting: {response.json()['error']['detail']}")
    assert response.status_code == 200, response.text
    return mcp_result(response.json())


def test_the_tools_do_something(shop: httpx.Client, token: str) -> None:
    """`search` answered `{"accepted": true}` for the whole of this project's
    life. The assertion that would have caught it is this one."""
    found = tool(shop, token, "search", query="cap")
    assert found["results"], "search returned nothing from a seeded catalogue"
    assert "accepted" not in found
    group = found["results"][0]["group"]

    item = tool(shop, token, "read-item", group=group)
    assert item["variants"], "a group with no variants cannot be bought"
    # Buckets, never counts (SPEC §16.9).
    assert all(v["availability"] in {"in-stock", "low-stock", "sold-out"} for v in item["variants"])
    assert not any("available" in v or "stock" in v for v in item["variants"])


def test_a_group_cannot_be_added_only_a_variant(shop: httpx.Client, token: str) -> None:
    response = shop.post(
        "/agent/mcp",
        json=mcp_request("add-line", sku="cap", qty=1),
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert mcp_error(response.json())["error"]["code"] == "variant-required"


def test_place_order_refuses_before_a_checkout_exists(shop: httpx.Client, token: str) -> None:
    response = shop.post(
        "/agent/mcp",
        json=mcp_request("place-order"),
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True


def test_a_whole_purchase_ends_in_a_verified_receipt(
    shop: httpx.Client, token: str, buyable: str
) -> None:
    """Search to signed receipt, through the real Gate, Ledger and Provider."""
    tool(shop, token, "add-line", sku=buyable, qty=1)
    tool(shop, token, "set-destination", destination=DESTINATION)
    tool(shop, token, "set-contact", contact=CONTACT)
    basket = tool(shop, token, "choose-fulfillment", id="karnataka")
    quoted = basket["total_minor"]
    assert quoted > 0

    started = tool(shop, token, "start-checkout", method="upi")
    assert started["total_minor"] == quoted, "the checkout re-priced what the basket showed"

    handoff = tool(shop, token, "place-order")
    assert handoff["approve_url"].startswith(f"http://{SHOP}/agentic/approve?t=")
    # The agent gets a link and never a purchase.
    assert "status" not in handoff

    # The order exists and has NOT moved: no stock held, no money, until a human
    # taps. This is the load-bearing claim of the whole design.
    before = tool(shop, token, "order-status", order_id=started["order_id"])
    assert before["status"] == "pending"
    assert before["receipt_id"] is None

    tap_token = handoff["approve_url"].split("t=")[1]
    page = shop.get("/agentic/approve", params={"t": tap_token})
    assert page.status_code == 200
    assert "Approve" in page.text

    tapped = shop.post(
        "/agentic/approve",
        data={"t": tap_token, "method": "upi"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert tapped.status_code == 303, tapped.text
    link = tapped.headers["location"].rsplit("/", 1)[1]

    paid = shop.post(f"/agentic/fake-pay/{link}")
    assert paid.status_code == 303, paid.text
    receipt_id = paid.headers["location"].rsplit("/", 1)[1]

    receipt = shop.get(f"/receipt/{receipt_id}.json").json()
    assert receipt["verification"]["status"] == "VALID"
    body = receipt["receipt"]
    assert [s["name"] for s in body["sections"]] == [
        "bought",
        "tapped",
        "decided",
        "told",
        "moved",
    ]
    assert body["signature"] and body["kid"]

    transcript = next(s for s in body["sections"] if s["name"] == "decided")["payload"][
        "transcript"
    ]
    assert len(transcript["checks"]) == 12
    assert transcript["total_minor"] == quoted

    # The Authority is bound to the exact basket the Gate decided on. These were
    # two different hashes once, and `upi-pin` defers before the comparison that
    # would have caught it.
    tapped_section = next(s for s in body["sections"] if s["name"] == "tapped")["payload"]
    assert tapped_section["cart_hash"] == transcript["cart_hash"]

    moved = next(s for s in body["sections"] if s["name"] == "moved")["payload"]
    assert [e["kind"] for e in moved["entries"]] == ["RESERVE", "CAPTURE"]
    assert all(e["amount_minor"] == quoted for e in moved["entries"])

    # And the Merchant agrees: the order reached `paid`, not merely `confirmed`.
    # Door 8 keys on `order_id:attempt`, so this failed silently when both
    # transitions shared an attempt number.
    after = tool(shop, token, "order-status", order_id=started["order_id"])
    assert after["status"] == "paid"


def test_a_spent_tap_token_cannot_be_spent_twice(
    shop: httpx.Client, token: str, buyable: str
) -> None:
    tool(shop, token, "add-line", sku=buyable, qty=1)
    tool(shop, token, "set-destination", destination=DESTINATION)
    tool(shop, token, "set-contact", contact=CONTACT)
    tool(shop, token, "choose-fulfillment", id="karnataka")
    tool(shop, token, "start-checkout", method="upi")
    tap_token = tool(shop, token, "place-order")["approve_url"].split("t=")[1]

    form = {"content-type": "application/x-www-form-urlencoded"}
    first = shop.post("/agentic/approve", data={"t": tap_token, "method": "upi"}, headers=form)
    assert first.status_code == 303
    replay = shop.post("/agentic/approve", data={"t": tap_token, "method": "upi"}, headers=form)
    assert replay.status_code == 403, "a single-use tap token was accepted twice"


def test_every_protocol_reaches_the_same_core(shop: httpx.Client, token: str, buyable: str) -> None:
    """One core, four envelopes — checked over HTTP rather than in a replay that
    builds the bundle itself."""
    body = {
        "lines": [{"sku": buyable, "qty": 1}],
        "destination": DESTINATION,
        "contact": CONTACT,
        "fulfillment_option_id": "karnataka",
    }
    auth = {"authorization": f"Bearer {token}"}

    ucp = shop.post("/agent/ucp/checkout", json=body, headers=auth).json()
    assert ucp["protocol"] == "ucp"
    assert ucp["completion"]

    acp = shop.post(
        "/agent/acp/checkout_sessions",
        json=body,
        headers={**auth, "api-version": "2026-04-17", "idempotency-key": "live-1"},
    ).json()
    assert acp["protocol"] == "acp"

    ap2 = shop.post("/agent/ap2/checkout", json=body, headers=auth).json()
    assert ap2["checkout_mandate"].count(".") == 2, "a mandate is a signed JWT"

    # Same shop, same basket, same Quote — so one total has to survive all three
    # translations. An envelope that re-priced on its way out would be computing,
    # and none of them is allowed to compute.
    claims_b64 = ap2["checkout_mandate"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=" * (-len(claims_b64) % 4)))
    acp_total = next(t["amount"] for t in acp["totals"] if t["type"] == "total")
    assert ucp["totals"]["total"] == acp_total == claims["amount_minor"], (
        f"three envelopes disagree about one basket: ucp={ucp['totals']['total']} "
        f"acp={acp_total} ap2={claims['amount_minor']}"
    )


def test_acp_completion_is_refused_with_its_reason(
    shop: httpx.Client, token: str, buyable: str
) -> None:
    """The refusal IS the product: an agent-held credential is the authority
    this Merchant does not grant."""
    body = {
        "lines": [{"sku": buyable, "qty": 1}],
        "destination": DESTINATION,
        "contact": CONTACT,
        "fulfillment_option_id": "karnataka",
    }
    auth = {"authorization": f"Bearer {token}"}
    session = shop.post(
        "/agent/acp/checkout_sessions",
        json=body,
        headers={**auth, "api-version": "2026-04-17", "idempotency-key": "live-2"},
    ).json()

    refused = shop.post(
        f"/agent/acp/checkout_sessions/{session['id']}/complete",
        json={"payment_data": {"token": "tok_delegated"}},
        headers={
            **auth,
            "api-version": "2026-04-17",
            "idempotency-key": "live-3",
            "signature": "x",
            "timestamp": "1",
        },
    )
    assert refused.status_code == 400
    payload = refused.json()
    assert payload["code"] == "method-not-supported"
    assert payload["approve_url"]
    assert payload["credential_fields_ignored"] == ["token"]
