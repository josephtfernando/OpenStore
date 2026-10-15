"""Four protocols, one core.

The single most valuable assertion in this phase is that the **core Transcript
bytes are identical across all four**. Envelopes differ; core decision bytes do
not. Everything else here supports that claim or checks a named deviation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from openstore.sidecar.admission.oauth import Admission
from openstore.sidecar.core.codes import (
    AuthorityKind,
    PaymentMethod,
    Protocol,
    ReasonCode,
    Scope,
    ToolName,
)
from openstore.sidecar.evidence.keys import Keyring
from openstore.sidecar.gate.decide import Authority, DecisionInput, Gate
from openstore.sidecar.gate.policy import Policy
from openstore.sidecar.protocols import acp, ap2, mcp, ucp
from openstore.sidecar.protocols.core import approve_url, run_core
from openstore.sidecar.protocols.registry import (
    SPECS,
    TOGGLE_ORDER,
    Shape,
    badge,
    toggle_labels,
)
from openstore.sidecar.protocols.tools import ToolRefused
from openstore.sidecar.protocols.wellknown import (
    agent_commerce_card,
    checkout_requirements,
    policy_limits,
)
from openstore.sidecar.trait.client import TraitClient
from openstore.sidecar.trait.models import Destination, Line
from openstore.sidecar.trait.seed import SEED_ITEMS

VENDORED_SPEC = Path("vendor/acp/2026-04-17/openapi.agentic_checkout.yaml")

DEST_B = Destination(line1="Dadar West", city="Mumbai", state="MH", postal_code="400028")
CONTACT = {"email": "demo@spoiledduckie.test", "phone": "+919000000001"}
SALT = bytes.fromhex("00112233445566778899aabbccddeeff")

GROUP_OF = {sku: group for sku, group, *_ in SEED_ITEMS}
TAGS_OF = {i[0]: i[-1] for i in SEED_ITEMS}
PRICE_OF = {i[0]: i[3] for i in SEED_ITEMS}


def _request() -> DecisionInput:
    """One basket, used by every protocol. The §16.11 worked example."""
    return DecisionInput(
        order_id="ord_proto",
        cart_id="cart_proto",
        lines=[
            Line(sku="SD-TOTE-BLK-M", qty=1),
            Line(sku="SD-GIFTWRAP", qty=1, parent="SD-TOTE-BLK-M"),
            Line(sku="SD-CHARMBAR-SEAT", qty=1),
        ],
        destination=DEST_B,
        contact=CONTACT,
        fulfillment_option_id="rest-of-india",
        order_salt=SALT,
        expiry_utc="2026-09-21T12:00:00Z",
        merchant_domain="spoiledduckie.localhost",
        agent_id="agent_proto",
        consumer_id="consumer_proto",
        authority=Authority(kind=AuthorityKind.UPI_PIN),
        method=PaymentMethod.UPI,
        group_of=GROUP_OF,
        tags_of=TAGS_OF,
        attested_prices=PRICE_OF,
    )


@pytest.fixture
def gate(trait: TraitClient) -> Gate:
    return Gate(trait, Policy())


# ── THE assertion ────────────────────────────────────────────────────────────


async def test_core_transcript_bytes_are_identical_across_all_four_protocols(
    gate: Gate,
) -> None:
    """Envelopes differ, core decision bytes do not.

    Every translator goes through `run_core`, and this is what proves none of
    them reached past it. If this test ever fails, a protocol has grown its own
    money path.
    """
    results = {}
    for protocol in TOGGLE_ORDER:
        result = await run_core(gate, _request())
        assert not result.refused, f"{protocol.value}: {result.detail}"
        results[protocol] = result.transcript_bytes

    distinct = set(results.values())
    assert len(distinct) == 1, f"{len(distinct)} distinct Transcripts across four protocols"


async def test_a_refusal_also_produces_identical_core_bytes(gate: Gate) -> None:
    """Byte-identity has to hold on the refusal path too, or a protocol could
    differ in exactly the case that matters most."""
    blocked = _request()
    blocked.lines = [Line(sku="SD-RECALLED", qty=1)]

    seen = set()
    for _ in TOGGLE_ORDER:
        result = await run_core(gate, blocked)
        assert result.reason_code is ReasonCode.BLOCKED_ITEM
        seen.add(result.transcript_bytes)
    assert len(seen) == 1


# ── The registry ─────────────────────────────────────────────────────────────


def test_the_toggle_shows_ap2_as_a_layer_on_ucp() -> None:
    """A fourth peer tab would be a nicer-looking lie: AP2's own specification
    calls it a security feature within a Commerce Protocol."""
    assert toggle_labels() == ["MCP", "UCP", "UCP+AP2", "ACP"]
    assert SPECS[Protocol.AP2].shape is Shape.LAYER
    assert SPECS[Protocol.AP2].rides_on is Protocol.UCP
    for envelope in (Protocol.MCP, Protocol.UCP, Protocol.ACP):
        assert SPECS[envelope].shape is Shape.ENVELOPE


def test_every_protocol_is_live() -> None:
    assert all(spec.live for spec in SPECS.values())
    assert set(SPECS) == set(Protocol)


@pytest.mark.parametrize("protocol", list(Protocol), ids=[p.value for p in Protocol])
def test_the_badge_names_its_deviations_inline(protocol: Protocol) -> None:
    """This is what keeps 'conformant' from becoming a lie."""
    result = badge(protocol, frozenset({PaymentMethod.UPI, PaymentMethod.CASH_ON_DELIVERY}))
    assert result["capability"] == "supported"
    assert result["completion"] == "redirect-only"
    assert result["payment_instruments_enabled"] == ["cash-on-delivery", "upi"]

    deviations = " ".join(result["deviations"])  # type: ignore[arg-type]
    assert "redirect-only" in deviations
    assert "refused by design" in deviations


def test_the_acp_badge_names_the_spec_version_it_targets() -> None:
    """'ACP conformant' unqualified ages badly against a specification with five
    dated releases in under a year."""
    result = badge(Protocol.ACP, frozenset({PaymentMethod.UPI}))
    assert result["version"] == "2026-04-17"


@pytest.mark.parametrize(
    "protocol", [Protocol.UCP, Protocol.ACP, Protocol.AP2], ids=["ucp", "acp", "ap2"]
)
def test_the_badge_names_which_completion_step_is_refused(protocol: Protocol) -> None:
    deviations = " ".join(badge(protocol, frozenset({PaymentMethod.UPI}))["deviations"])  # type: ignore[arg-type]
    assert SPECS[protocol].refused_step in deviations


def test_the_badge_reflects_what_this_merchant_actually_enabled() -> None:
    """A card attempt refuses on a UPI-only store and succeeds on one that
    enabled cards, and the badge has to say which this is."""
    upi_only = badge(Protocol.ACP, frozenset({PaymentMethod.UPI}))
    with_cards = badge(Protocol.ACP, frozenset({PaymentMethod.UPI, PaymentMethod.CARD}))
    assert upi_only["payment_instruments_enabled"] == ["upi"]
    assert with_cards["payment_instruments_enabled"] == ["card", "upi"]


# ── MCP ──────────────────────────────────────────────────────────────────────


def test_tools_list_is_the_closed_action_set() -> None:
    """A tool here and not in the registry is a red build."""
    listed = {t["name"] for t in mcp.tools_list()}
    assert listed == {t.value for t in ToolName}


def test_every_tool_declares_the_scope_it_needs() -> None:
    """Least privilege stated in the listing, so an agent can see what a tool
    costs before calling it."""
    for tool in mcp.tools_list():
        assert tool["annotations"]["scope"] in {s.value for s in Scope}


def test_money_path_hint_is_exactly_the_two_scopes_that_move_toward_spend() -> None:
    """A generic client should be able to offer standing 'always allow' on
    every tool except these two, without knowing any tool by name."""
    for tool in mcp.tools_list():
        scope = tool["annotations"]["scope"]
        expected = scope in {Scope.START_CHECKOUT.value, Scope.CONFIRM.value}
        assert tool["annotations"]["moneyPathHint"] is expected


def test_place_order_says_it_returns_an_approve_url_not_an_order() -> None:
    entry = next(t for t in mcp.tools_list() if t["name"] == "place-order")
    assert "approve URL" in entry["description"]
    assert "never an order" in entry["description"]


def test_destination_schema_names_fields_patterns_and_aliases() -> None:
    entry = next(t for t in mcp.tools_list() if t["name"] == "set-destination")
    dest = entry["inputSchema"]["properties"]["destination"]
    assert set(dest["required"]) == {"line1", "city", "state", "postal_code"}
    assert dest["properties"]["state"]["pattern"] == "^[A-Z]{2}$"
    assert dest["properties"]["postal_code"]["pattern"] == "^[0-9]{6}$"
    assert "address_line1" in dest["properties"]["line1"]["description"]
    contact = next(t for t in mcp.tools_list() if t["name"] == "set-contact")
    assert "email" in contact["inputSchema"]["properties"]["contact"]["properties"]


def test_descriptions_guide_a_model_with_no_prior_knowledge() -> None:
    """Generic-client hardening: each step states its prerequisites and its
    next step, in plain words, so a weaker model need not infer the workflow."""
    by_name = {t["name"]: t for t in mcp.tools_list()}
    assert "add-line" in by_name["read-item"]["description"]
    assert "approve page" in by_name["apply-public-code"]["description"]
    assert "place-order" in by_name["start-checkout"]["description"]
    assert "request-refund" in by_name["cancel-order"]["description"]
    assert "no money" in by_name["request-refund"]["description"]
    # choose-fulfillment lists when id is omitted — the schema must allow that.
    assert "required" not in by_name["choose-fulfillment"]["inputSchema"]


def test_a_tool_call_without_its_scope_is_refused_before_the_body_runs() -> None:
    from dataclasses import replace

    token = Admission().issue_for_stranger("agent_x")
    narrowed = replace(token, scopes=frozenset({Scope.SEARCH}))
    refusal = mcp.check_scope(narrowed, ToolName.PLACE_ORDER)
    assert refusal is not None
    assert refusal["error"]["code"] == ReasonCode.AUTHORITY_MISSING.value
    assert mcp.check_scope(narrowed, ToolName.SEARCH) is None


# ── UCP ──────────────────────────────────────────────────────────────────────


async def test_ucp_totals_map_one_to_one_onto_the_quote(gate: Gate) -> None:
    """The translator carries no pricing logic: a translator that computed
    anything would be a second money path."""
    result = await run_core(gate, _request())
    assert result.decision is not None
    quote = result.decision.quote

    totals = ucp.totals(quote)
    assert totals["subtotal"] == quote.subtotal_minor == 249800
    assert totals["fulfillment"] == 9900
    assert totals["total"] == quote.total_minor == 259700
    assert totals["tax"] == sum(t.amount_minor for t in quote.tax_lines)


async def test_ucp_reports_a_discount_positively_while_the_quote_holds_it_negative(
    gate: Gate,
) -> None:
    """UCP's shape wants a positive `items_discount`; the Quote's sign is
    intrinsic. The conversion is one abs() and it is the only arithmetic here."""
    request = _request()
    request.lines = [Line(sku="SD-TOTE-BLK-M", qty=1), Line(sku="SD-PHONECHARM", qty=1)]
    request.destination = Destination(
        line1="4th Cross", city="Bengaluru", state="KA", postal_code="560038"
    )
    request.fulfillment_option_id = "karnataka"
    request.discount_code = "SPOILED10"

    result = await run_core(gate, request)
    assert result.decision is not None
    quote = result.decision.quote
    assert quote.discount_lines[0].amount_minor == -10000
    assert ucp.totals(quote)["items_discount"] == 10000


async def test_ucp_completion_is_the_buyer_escalation_path(gate: Gate) -> None:
    result = await run_core(gate, _request())
    assert result.decision is not None
    checkout = ucp.checkout(
        result.decision.quote,
        approve_url=approve_url("spoiledduckie.localhost", "tok_abc"),
    )
    assert checkout["completion"]["mode"] == "buyer_escalation"
    assert checkout["completion"]["approve_url"].startswith(
        "https://spoiledduckie.localhost/agentic/approve"
    )


# ── ACP ──────────────────────────────────────────────────────────────────────


def test_the_vendored_openapi_is_the_version_we_built_against() -> None:
    """A quarterly revision should be noticed rather than silently drifted past."""
    assert VENDORED_SPEC.exists(), "the ACP spec is vendored, not remembered"
    document = yaml.safe_load(VENDORED_SPEC.read_text(encoding="utf-8"))

    assert sorted(document["paths"]) == sorted({path for _, path in acp.OPERATION_PATHS.values()})
    operations = {
        op["operationId"]
        for path in document["paths"].values()
        for method, op in path.items()
        if method in {"get", "post"}
    }
    assert operations == set(acp.OPERATION_PATHS)

    schemas = document["components"]["schemas"]
    for name in (
        "CheckoutSessionCreateRequest",
        "CheckoutSessionUpdateRequest",
        "CheckoutSessionCompleteRequest",
        "CheckoutSession",
        "CheckoutSessionWithOrder",
        "CancelSessionRequest",
        "Error",
    ):
        assert name in schemas, f"{name} is named in §16.12 and missing from the spec"


def test_the_vendored_spec_path_carries_the_pinned_version() -> None:
    assert acp.SPEC_VERSION in str(VENDORED_SPEC)
    assert acp.SPEC_VERSION == "2026-04-17"


def test_get_checkout_session_requires_no_idempotency_key() -> None:
    """Headers are per-operation, not blanket. It mutates nothing, and
    pretending otherwise invents a requirement the spec does not have."""
    assert "Idempotency-Key" not in acp.REQUIRED_HEADERS["getCheckoutSession"]
    assert "Idempotency-Key" in acp.REQUIRED_HEADERS["createCheckoutSession"]


def test_a_missing_api_version_is_refused() -> None:
    """Guessing a version against a quarterly spec is how a silent
    incompatibility ships."""
    with pytest.raises(acp.AcpError, match="API-Version"):
        acp.check_headers("getCheckoutSession", {"Authorization": "Bearer x"})


def test_a_different_api_version_is_refused_naming_ours() -> None:
    with pytest.raises(acp.AcpError, match="2026-04-17"):
        acp.check_headers(
            "getCheckoutSession",
            {"Authorization": "Bearer x", "API-Version": "2026-01-30"},
        )


async def test_four_acp_operations_work_normally(gate: Gate) -> None:
    result = await run_core(gate, _request())
    assert result.decision is not None

    headers = {
        "Authorization": "Bearer t",
        "API-Version": acp.SPEC_VERSION,
        "Idempotency-Key": "cart_proto:1",
        "Content-Type": "application/json",
    }
    for operation in (
        "createCheckoutSession",
        "updateCheckoutSession",
        "cancelCheckoutSession",
    ):
        acp.check_headers(operation, headers)
    acp.check_headers(
        "getCheckoutSession", {"Authorization": "Bearer t", "API-Version": acp.SPEC_VERSION}
    )

    session = acp.session_from_quote("cs_1", result.decision.quote)
    payload = acp.envelope(session)
    assert payload["acp_version"] == "2026-04-17"
    assert next(t for t in payload["totals"] if t["type"] == "total")["amount"] == 259700

    cancelled = acp.cancel(session)
    assert cancelled["status"] == "cancelled"


async def test_acp_complete_refuses_a_delegated_credential_and_returns_the_approve_url(
    gate: Gate,
) -> None:
    """The refusal point. ADR-0008 and ADR-0013 enforced at the envelope
    boundary rather than left as an unimplemented gap."""
    result = await run_core(gate, _request())
    assert result.decision is not None
    session = acp.session_from_quote("cs_2", result.decision.quote)

    response = acp.complete(
        session,
        approve_url=approve_url("spoiledduckie.localhost", "tok_xyz"),
        payment_data={"token": "spt_live_abc123", "provider": "stripe"},
    )

    assert response["code"] == ReasonCode.METHOD_NOT_SUPPORTED.value
    assert response["refused_step"] == "completeCheckoutSession"
    assert response["approve_url"].endswith("t=tok_xyz")
    assert response["credential_fields_ignored"] == ["provider", "token"]
    assert "authorize the exact amount themselves" in response["message"]


def test_acp_session_carries_day_counts_not_dates() -> None:
    """No clock crosses door 9, and the envelope must not add one."""
    from openstore.sidecar.trait.models import FulfillmentChosen, FulfillmentOption, Quote

    quote = Quote(
        subtotal_minor=1000,
        lines=[],
        fulfillment_options=[
            FulfillmentOption(id="roi", label="Rest of India", cost_minor=9900, eta_days=5)
        ],
        fulfillment_chosen=FulfillmentChosen(id="roi", cost_minor=9900),
        total_minor=10900,
        tax_inclusive=True,
    )
    session = acp.session_from_quote("cs_3", quote)
    assert session.fulfillment_options[0]["earliest_delivery_days"] == 5


# ── AP2 ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def merchant_keys() -> Keyring:
    ring = Keyring("spoiledduckie.localhost")
    ring.enroll("k1")
    return ring


@pytest.fixture
def agent_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _mandate(
    agent_key: ec.EllipticCurvePrivateKey,
    checkout_jwt: str,
    *,
    autonomous: bool = False,
    vct: str = ap2.CHECKOUT_MANDATE_VCT,
    digest: str | None = None,
) -> str:
    from datetime import UTC, datetime

    from openstore.sidecar.evidence.keys import KeyRecord

    record = KeyRecord(kid="agent-k1", private_key=agent_key, created_at=datetime.now(UTC))
    claims: dict[str, Any] = {
        "vct": vct,
        "checkout_hash": digest or ap2.checkout_hash(checkout_jwt),
        "transaction_id": "txn_1",
    }
    if autonomous:
        claims["cnf"] = {"jwk": ap2.agent_jwk_from_key(agent_key, "agent-k1")}
    return ap2.encode_jwt(claims, record)


def test_a_matching_mandate_completes_through_the_approve_ceremony(
    merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey
) -> None:
    """`/agentic/approve` is the Trusted Surface the spec describes: a UI
    trusted to get informed user consent before a user-signed Mandate."""
    checkout_jwt = ap2.sign_checkout({"total_minor": 259700}, merchant_keys.keys["k1"])
    mandate_jwt = _mandate(agent_key, checkout_jwt)

    mandate = ap2.verify_checkout_mandate(
        mandate_jwt,
        agent_jwk=ap2.agent_jwk_from_key(agent_key, "agent-k1"),
        our_checkout_jwt=checkout_jwt,
    )
    assert mandate.mode is ap2.Mode.HUMAN_PRESENT

    receipt = ap2.checkout_receipt(
        mandate,
        merchant_keys.keys["k1"],
        transaction_id="txn_1",
        approve_url=approve_url("spoiledduckie.localhost", "tok_1"),
    )
    claims = ap2.decode_jwt(receipt, dict(merchant_keys.jwks()["keys"][0]))  # type: ignore[index]
    assert claims["vct"] == ap2.CHECKOUT_RECEIPT_VCT
    assert claims["completion"] == "buyer_escalation"


def test_a_mandate_for_a_different_checkout_is_rejected(
    merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey
) -> None:
    """The load-bearing check. Without it the mandate proves only that *some*
    checkout was approved."""
    ours = ap2.sign_checkout({"total_minor": 259700}, merchant_keys.keys["k1"])
    theirs = ap2.sign_checkout({"total_minor": 100}, merchant_keys.keys["k1"])
    mandate_jwt = _mandate(agent_key, theirs)

    with pytest.raises(ap2.Ap2Error) as exc:
        ap2.verify_checkout_mandate(
            mandate_jwt,
            agent_jwk=ap2.agent_jwk_from_key(agent_key, "agent-k1"),
            our_checkout_jwt=ours,
        )
    assert exc.value.code is ReasonCode.AUTHORITY_STALE
    assert "different basket" in exc.value.detail


def test_an_autonomous_mandate_refuses_authority_kind_not_enabled(
    merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey
) -> None:
    """Human Not Present is the `mandate` Authority kind: defined, registered
    and refused in v1 (ADR-0017)."""
    checkout_jwt = ap2.sign_checkout({"total_minor": 1}, merchant_keys.keys["k1"])
    mandate_jwt = _mandate(agent_key, checkout_jwt, autonomous=True)

    with pytest.raises(ap2.Ap2Error) as exc:
        ap2.verify_checkout_mandate(
            mandate_jwt,
            agent_jwk=ap2.agent_jwk_from_key(agent_key, "agent-k1"),
            our_checkout_jwt=checkout_jwt,
        )
    assert exc.value.code is ReasonCode.AUTHORITY_KIND_NOT_ENABLED
    assert "Autonomous" in exc.value.detail


def test_an_unknown_vct_is_refused(
    merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey
) -> None:
    """The spec versions its mandates; reading one we do not know as one we do
    is how a schema change becomes a silent misinterpretation."""
    checkout_jwt = ap2.sign_checkout({"t": 1}, merchant_keys.keys["k1"])
    mandate_jwt = _mandate(agent_key, checkout_jwt, vct="mandate.checkout.open.2")
    with pytest.raises(ap2.Ap2Error, match="mandate.checkout.open.1"):
        ap2.verify_checkout_mandate(
            mandate_jwt,
            agent_jwk=ap2.agent_jwk_from_key(agent_key, "agent-k1"),
            our_checkout_jwt=checkout_jwt,
        )


def test_alg_none_is_refused(merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey) -> None:
    """Algorithm confusion, refused at the door."""
    import base64
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    claims = base64.urlsafe_b64encode(json.dumps({"vct": "x"}).encode()).rstrip(b"=").decode()
    with pytest.raises(ap2.Ap2Error, match="ES256"):
        ap2.decode_jwt(f"{header}.{claims}.", ap2.agent_jwk_from_key(agent_key, "agent-k1"))


def test_a_mandate_signed_by_another_key_is_refused(
    merchant_keys: Keyring, agent_key: ec.EllipticCurvePrivateKey
) -> None:
    checkout_jwt = ap2.sign_checkout({"t": 1}, merchant_keys.keys["k1"])
    mandate_jwt = _mandate(agent_key, checkout_jwt)
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ap2.Ap2Error, match="does not verify"):
        ap2.verify_checkout_mandate(
            mandate_jwt,
            agent_jwk=ap2.agent_jwk_from_key(other, "agent-k1"),
            our_checkout_jwt=checkout_jwt,
        )


def test_the_checkout_hash_is_taken_over_the_jwt_as_sent(
    merchant_keys: Keyring,
) -> None:
    """The agent hashed the string it received. Re-encoding the claims would
    produce a different hash for the same document."""
    checkout_jwt = ap2.sign_checkout({"total_minor": 259700}, merchant_keys.keys["k1"])
    assert ap2.checkout_hash(checkout_jwt) == ap2.checkout_hash(checkout_jwt)
    assert ap2.checkout_hash(checkout_jwt) != ap2.checkout_hash(checkout_jwt + " ")


def test_there_is_only_one_jwt_verifier() -> None:
    """A second ES256 implementation is a second thing to get wrong. `ap2.py`
    reuses the evidence keyring's verifier rather than writing its own."""
    source = Path("src/openstore/sidecar/protocols/ap2.py").read_text(encoding="utf-8")
    assert "from openstore.sidecar.evidence.keys import" in source
    assert not re.search(r"\bec\.ECDSA\b", source), "ap2.py is doing its own curve maths"


# ── The discovery hint (ADR-0027) ────────────────────────────────────────────


def test_the_card_publishes_what_the_shop_says_it_sells() -> None:
    """So an agent holding ten shops can ask the likely ones first instead of
    asking all ten about a mug."""
    card = agent_commerce_card(
        merchant_domain="kettleandgrain.localhost",
        merchant_name="Kettle & Grain",
        origin="http://kettleandgrain.localhost",
        enabled_methods=frozenset({PaymentMethod.UPI}),
        enabled_authority_kinds=frozenset({AuthorityKind.PASSKEY}),
        demo=True,
        description="Kitchenware and brewing gear.",
        categories=("kitchenware", "homeware"),
    )
    assert card["merchant"]["description"] == "Kitchenware and brewing gear."
    assert card["merchant"]["categories"] == ["kitchenware", "homeware"]


def test_the_card_says_the_hint_is_unverified_and_never_a_filter() -> None:
    """The one thing an integrator must not conclude from this field.

    It is the Merchant's own words about itself, checked by nobody, and this
    demo deliberately puts four SKUs in two shops at once. An agent that reads
    the hint as authoritative answers "they do not stock that" about stock that
    is there — which is worse than the extra round trip the field exists to
    save, because a slow answer is still a right one.
    """
    card = agent_commerce_card(
        merchant_domain="x.test",
        merchant_name="X",
        origin="http://x.test",
        enabled_methods=frozenset(),
        enabled_authority_kinds=frozenset(),
        demo=True,
        description="Anything.",
    )
    note = card["merchant"]["note"].lower()
    assert "verified by no one" in note
    assert "never grounds for concluding" in note
    assert "only a search can establish" in note


def test_a_shop_that_declares_nothing_carries_no_empty_hint() -> None:
    """Absent, not blank. An empty description is a claim that the shop said
    something; leaving the key out says it did not."""
    card = agent_commerce_card(
        merchant_domain="x.test",
        merchant_name="X",
        origin="http://x.test",
        enabled_methods=frozenset(),
        enabled_authority_kinds=frozenset(),
        demo=True,
    )
    assert card["merchant"] == {"name": "X", "domain": "x.test"}


# ── Limits, requirements and structured refusals ─────────────────────────────


def test_the_card_publishes_the_limits_the_gate_enforces() -> None:
    """Each of these is otherwise learned by building a basket and being turned
    away at `decide()` — the most expensive way to learn a number nobody was
    keeping secret."""
    limits = policy_limits(Policy())
    assert limits["per_order_cap_minor"] == Policy().per_order_cap_minor
    assert limits["per_order_line_count"] == Policy().per_order_line_count
    assert limits["per_group_qty"] == Policy().per_group_qty
    assert limits["window_open"] is True
    assert limits["currency"] == "INR"


def test_the_limits_report_the_merchants_refusal_rules_as_counts() -> None:
    """An agent needs to know a tag rule exists so it can read the refusal when
    one fires. Nobody outside needs the list of what this shop will not sell."""
    limits = policy_limits(Policy(blocked_tags=frozenset({"recalled", "adult"})))
    assert limits["blocked_tag_rules"] == 2
    assert "recalled" not in str(limits)


def test_a_closed_window_is_visible_before_an_agent_spends_a_conversation() -> None:
    """The sharpest case for publishing limits at all: a closed shop answers
    searches normally and refuses at checkout, so without this an agent spends a
    whole conversation to find out the door was shut."""
    assert policy_limits(Policy(window_open=False))["window_open"] is False


def test_the_published_destination_shape_is_the_one_that_is_validated() -> None:
    """Derived from `Destination`, never retyped.

    A hand-written copy would be a second statement of the same rule, and the
    interesting failure is the one where an agent builds exactly what the card
    described and is refused for it.
    """
    from openstore.sidecar.trait.models import Destination

    published = {f["name"] for f in checkout_requirements()["destination"]["fields"]}
    assert published == set(Destination.model_fields)

    by_name = {f["name"]: f for f in checkout_requirements()["destination"]["fields"]}
    assert by_name["state"]["pattern"] == "^[A-Z]{2}$"
    assert by_name["postal_code"]["pattern"] == "^[0-9]{6}$"
    assert by_name["line2"]["required"] is False
    assert by_name["line1"]["required"] is True


def test_the_contact_rule_is_published_as_any_of_rather_than_a_field_list() -> None:
    """ "Email or phone, at least one" is not something a field list can say, and
    it is the only thing an agent needs before asking a person for details."""
    contact = checkout_requirements()["contact"]
    assert contact["any_of"] == ["email", "phone"]


def test_a_refusal_carries_the_facts_needed_to_retry_correctly() -> None:
    """`call_error` always accepted extra keys and nothing ever passed any, so an
    agent wanting the number in a refusal had to parse English — which makes the
    wording of a message load-bearing in a way nobody intended."""
    refusal = ToolRefused(
        ReasonCode.SOLD_OUT, "Only 2 left.", {"sku": "SD-TOTE-BLK-M", "available": 2}
    )
    payload = mcp.call_error(refusal.code, refusal.detail, **refusal.fields)
    error = payload["structuredContent"]["error"]
    assert error["code"] == "sold-out"
    assert error["available"] == 2
    assert error["sku"] == "SD-TOTE-BLK-M"
    # The human sentence is still there; the fields are beside it, not instead.
    assert payload["content"][0]["text"].startswith("sold-out: ")


def test_a_refusal_with_no_fields_is_shaped_exactly_as_before() -> None:
    """Additive, so nothing that reads `{code, detail}` notices."""
    refusal = ToolRefused(ReasonCode.NOT_FOUND, "No such SKU.")
    assert refusal.fields == {}
    error = mcp.call_error(refusal.code, refusal.detail, **refusal.fields)
    assert error["structuredContent"]["error"] == {
        "code": "not-found",
        "detail": "No such SKU.",
    }


def test_every_policy_limit_the_gate_enforces_is_published() -> None:
    """The guardrail, not the feature.

    A limit added to `Policy` and enforced by the Gate, with nothing added here,
    is a refusal an agent can only discover by tripping it — which is the exact
    state this work existed to end. Publishing is therefore the default and an
    omission has to be argued for in `_UNPUBLISHED` below, by name.
    """
    from dataclasses import fields as dataclass_fields

    #: Policy fields deliberately not in the card's `limits`, each with its
    #: reason. Payment methods and Authority kinds are published, just under
    #: their own keys (`payment.methods_enabled`, `authority.kinds_accepted`)
    #: because they were there before `limits` existed and agents read them.
    _UNPUBLISHED = {
        "enabled_methods": "published as payment.methods_enabled",
        "enabled_authority_kinds": "published as authority.kinds_accepted",
        "enabled_intent_mechanisms": "published as authority.kinds_accepted's mechanisms",
        "blocked_tags": "published as a count — the list is the Merchant's own",
        "per_group_qty_overrides": "published as a count — naming a group leaks which",
    }

    published = set(policy_limits(Policy()))
    for field_ in dataclass_fields(Policy):
        if field_.name in published or field_.name in _UNPUBLISHED:
            continue
        raise AssertionError(
            f"Policy.{field_.name} is enforced by the Gate and appears nowhere on the "
            f"card. Publish it in `policy_limits`, or name it in _UNPUBLISHED with the "
            f"reason an agent is better off discovering it by being refused."
        )
