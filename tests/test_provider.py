"""Providers and webhooks. The Provider decides that money moved, never that
anyone authorized it."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from openstore.sidecar.core.codes import PaymentMethod, ProviderOp
from openstore.sidecar.core.settings import BootRefused
from openstore.sidecar.provider.fake import FakeProvider
from openstore.sidecar.provider.razorpay import RazorpayProvider
from openstore.sidecar.provider.webhooks import (
    WebhookRejected,
    WebhookVerifier,
    reconcile,
)

SECRET = "webhook-secret"  # noqa: S105 - a test secret


def _signed(payload: dict[str, object], secret: str = SECRET) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# ── The trait ────────────────────────────────────────────────────────────────


def test_the_fake_declares_every_method_and_razorpay_declares_upi() -> None:
    assert FakeProvider().declared_methods() == frozenset(PaymentMethod)
    assert RazorpayProvider("rzp_test_x", "s").declared_methods() == frozenset({PaymentMethod.UPI})


def test_no_adapter_declares_the_block_capability_in_v1() -> None:
    """The seam ADR-0024 wants, consumed by nothing. It is explicitly not a
    Reserve Pay COD hold — ADR-0018 retired that."""
    for provider in (FakeProvider(), RazorpayProvider("rzp_test_x", "s")):
        ops = provider.declared_ops()
        assert ProviderOp.BLOCK not in ops
        assert ops == frozenset(
            {
                ProviderOp.MAKE_LINK,
                ProviderOp.CHECK_STATUS,
                ProviderOp.CANCEL,
                ProviderOp.REFUND,
            }
        )


def test_razorpay_refuses_a_live_key_in_demo_mode() -> None:
    """Every demo receipt is marked demo, and a demo that can move real money is
    not a demo."""
    with pytest.raises(BootRefused, match="not a rzp_test_"):
        RazorpayProvider("rzp_live_realmoney", "s", demo_mode=True)


def test_razorpay_accepts_a_live_key_when_demo_mode_is_off() -> None:
    assert RazorpayProvider("rzp_live_realmoney", "s", demo_mode=False).name == "razorpay"


# ── Links and adoption ───────────────────────────────────────────────────────


async def test_make_link_then_approve_then_check_status() -> None:
    provider = FakeProvider()
    link = await provider.make_link("ord_1", 259700, "INR", expires_in_seconds=900)
    assert link.expires_in_seconds == 900  # §16.7: matches the confirmed window

    before = await provider.check_status(link.link_id)
    assert before.paid is False

    provider.approve(link.link_id)
    after = await provider.check_status(link.link_id)
    assert after.paid is True
    assert after.amount_minor == 259700
    assert after.reference


async def test_crash_mid_link_adopts_instead_of_double_creating() -> None:
    """The crash is between creating the link and hearing about it. The recovery
    is to ask the Provider what happened — never to create a second link, which
    is how a Consumer gets charged twice."""
    provider = FakeProvider(crash_after_create=True)
    with pytest.raises(ConnectionError):
        await provider.make_link("ord_crash", 1000, "INR", expires_in_seconds=900)

    orphan = provider.find_by_order("ord_crash")
    assert orphan is not None, "the link exists even though the caller never saw it"

    provider.crash_after_create = False
    adopted = await provider.check_status(orphan.link_id)
    assert adopted.order_id == "ord_crash"
    assert len(provider.links) == 1, "adoption created a second link"


async def test_cancel_marks_the_link_dead() -> None:
    provider = FakeProvider()
    link = await provider.make_link("ord_2", 1000, "INR", expires_in_seconds=900)
    await provider.cancel(link.link_id)
    assert (await provider.check_status(link.link_id)).cancelled is True


async def test_two_partial_refunds_reach_the_provider_separately() -> None:
    """Same reason the Ledger key carries the refund's own id: a second partial
    keyed on the order alone silently does nothing."""
    provider = FakeProvider()
    await provider.refund("pay_1", 3000, "INR", refund_id="r1")
    await provider.refund("pay_1", 2000, "INR", refund_id="r2")
    assert len(provider.refunds) == 2
    assert sum(r.amount_minor for r in provider.refunds.values()) == 5000


# ── Webhooks ─────────────────────────────────────────────────────────────────


def test_a_forged_signature_is_rejected() -> None:
    verifier = WebhookVerifier(SECRET)
    raw, _ = _signed({"event_id": "evt_1"})
    with pytest.raises(WebhookRejected, match="signature"):
        verifier.verify(raw_body=raw, signature="deadbeef")


def test_a_signature_from_the_wrong_secret_is_rejected() -> None:
    verifier = WebhookVerifier(SECRET)
    raw, signature = _signed({"event_id": "evt_1"}, secret="not-the-secret")
    with pytest.raises(WebhookRejected):
        verifier.verify(raw_body=raw, signature=signature)


def test_the_signature_covers_the_raw_bytes_not_a_reserialization() -> None:
    """Two JSON encoders disagree about key order and whitespace, so a signature
    verified against a round-trip verifies nothing."""
    verifier = WebhookVerifier(SECRET)
    raw = b'{"event_id":"evt_1",  "amount":100}'
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert verifier.verify(raw_body=raw, signature=signature)["event_id"] == "evt_1"

    reserialized = json.dumps(json.loads(raw)).encode()
    assert reserialized != raw
    with pytest.raises(WebhookRejected):
        verifier.verify(raw_body=reserialized, signature=signature)


def test_replay_is_caught_by_event_id() -> None:
    """Providers retry, correctly. A webhook delivered twice must capture once."""
    verifier = WebhookVerifier(SECRET)
    assert verifier.is_duplicate("evt_1") is False
    assert verifier.is_duplicate("evt_1") is True


def test_a_webhook_without_an_event_id_is_rejected() -> None:
    verifier = WebhookVerifier(SECRET)
    with pytest.raises(WebhookRejected, match="dedupe is not optional"):
        verifier.is_duplicate("")


def test_out_of_order_delivery_is_safe_because_events_are_triggers() -> None:
    """Providers do not promise ordering. Distinct event ids both pass dedupe;
    what keeps a late `paid` from resurrecting a `failed` order is that the
    handler re-reads state rather than obeying the event."""
    verifier = WebhookVerifier(SECRET)
    assert verifier.is_duplicate("evt_paid") is False
    assert verifier.is_duplicate("evt_failed") is False
    assert verifier.is_duplicate("evt_paid") is True


def test_a_verified_body_that_is_not_json_is_rejected() -> None:
    verifier = WebhookVerifier(SECRET)
    raw = b"not json at all"
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(WebhookRejected, match="not JSON"):
        verifier.verify(raw_body=raw, signature=signature)


# ── Reconcile ────────────────────────────────────────────────────────────────


def test_reconcile_matches_on_amount_and_currency() -> None:
    assert reconcile(
        provider_amount_minor=259700,
        provider_currency="INR",
        pinned_amount_minor=259700,
        pinned_currency="INR",
    ).matches


def test_reconcile_catches_a_short_payment() -> None:
    result = reconcile(
        provider_amount_minor=259600,
        provider_currency="INR",
        pinned_amount_minor=259700,
        pinned_currency="INR",
    )
    assert not result.matches
    assert "259600" in result.detail


def test_reconcile_catches_a_currency_swap() -> None:
    result = reconcile(
        provider_amount_minor=259700,
        provider_currency="USD",
        pinned_amount_minor=259700,
        pinned_currency="INR",
    )
    assert not result.matches
    assert "USD" in result.detail


def test_fake_links_point_at_the_deploys_origin() -> None:
    """The tap redirect must land on the host the Consumer looks at, not the
    demo default — otherwise a public deploy approves into localhost."""
    from openstore.sidecar.provider.fake import FakeProvider

    async def run() -> str:
        link = await FakeProvider(public_origin="https://shop.example").make_link(
            "ord_1", 100, "INR", expires_in_seconds=900
        )
        return link.url

    import asyncio

    assert asyncio.run(run()).startswith("https://shop.example/agentic/fake-pay/")
