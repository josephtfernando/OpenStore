"""The money path, and the one HTTP entry point into it.

Everything below already existed and was exercised only by tests: the Gate ran
its twelve checks, the Ledger kept its two invariants, `settle` resolved a
deferred Authority, `BundleBuilder` sealed a receipt and the verifier checked
it. Nothing in `src/` ever called any of it, so the sidecar could not take an
order at all — the agent surface answered every tool `accepted: true` and the
approve page posted to a route that did not exist.

This module is the wiring, and nothing more. It computes no price, invents no
status, and makes no decision the Gate has not already made.

**The sequence** (SPEC §6.5's call order, one HTTP request per step):

1. `start` — door 7 creates the order (`pending`, no stock held), door 9 quotes
   it, and the two together fix the `cart_hash`. A tap token is minted over that
   exact hash and total. Nothing has moved.
2. `tap` — the Consumer approves on the Merchant's own origin. The token is
   spent once, the Gate runs for real, door 3 holds the stock, the Ledger takes
   the hold, and the Provider issues a link.
3. `complete` — the money arrives. `settle` reconciles it, door 4 commits the
   stock, the order becomes `paid`, and the receipt is sealed and stored.

**`order_salt` is never held between requests** (§6.3a). Door 7 is idempotent on
`cart_id:attempt`, so each step that needs the salt re-reads it from the
Merchant by replaying that call, uses it in request memory, and drops it. Caching
it here would be the quiet way to break ADR-0011's erasure guarantee: erasing an
order would stop making its PII commitments unopenable, which is the whole point
of them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openstore.sidecar.authority.kinds import HandleSource, derive_consumer_id
from openstore.sidecar.authority.tokens import TokenStore
from openstore.sidecar.checkout_store import CheckoutStore
from openstore.sidecar.core.canonical import cart_hash as compute_cart_hash
from openstore.sidecar.core.canonical import pii_commit, quote_hash
from openstore.sidecar.core.codes import (
    AuthorityKind,
    IntentMechanism,
    OrderStatus,
    PaymentMethod,
    ReasonCode,
)
from openstore.sidecar.evidence.bundle import BundleBuilder, attestation_digest
from openstore.sidecar.evidence.keys import Keyring, sign
from openstore.sidecar.gate.decide import Authority, Decision, DecisionInput, Gate
from openstore.sidecar.gate.policy import Policy, current_policy
from openstore.sidecar.gate.settle import ProviderRecord, collect, settle
from openstore.sidecar.gate.transcript import binding_for
from openstore.sidecar.ledger.entries import Ledger
from openstore.sidecar.protocols.core import run_core
from openstore.sidecar.protocols.events import event_body
from openstore.sidecar.trait.client import TraitClient
from openstore.sidecar.trait.errors import TraitError
from openstore.sidecar.trait.models import Destination, Line, Quote

#: §16.7. The Provider's link lifetime, and the ceiling on how long a `confirmed`
#: order may hold stock — the sweeper releases at whichever comes first.
PAYMENT_LINK_SECONDS = 900

#: How long a Consumer has to approve before the quote is re-read. The Quote's
#: own validity, not an inventory hold — no stock is held until the tap.
CHECKOUT_TTL = timedelta(hours=24)


class CheckoutRefused(Exception):
    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Pending:
    """One checkout between start and completion.

    Holds no `order_salt` and no payment credential — only what the Consumer was
    shown and what the Authority will be taken over.
    """

    cart_id: str
    order_id: str
    lines: list[Line]
    destination: Destination
    contact: dict[str, str]
    fulfillment_option_id: str
    expiry_utc: str
    agent_id: str
    method: PaymentMethod
    quote: Quote
    quote_bytes: bytes
    cart_hash: str
    total_minor: int
    discount_code: str | None = None
    tap_token: str = ""
    link_id: str = ""
    payer_handle: str = ""
    status: OrderStatus = OrderStatus.PENDING
    receipt_id: str = ""
    #: The three moments `expires_at` is measured from (§16.7). Held here rather
    #: than re-derived from `expiry_utc`, because the payment window is measured
    #: from the tap and the Quote's validity from creation — one string cannot
    #: carry both, and guessing one from the other is how a 15-minute hold
    #: becomes a 24-hour one.
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    confirmed_at: datetime | None = None
    link_expires_at: datetime | None = None
    handle_source: HandleSource = HandleSource.PAYER_HANDLE
    """Where `consumer_id` is derived from. A COD order authorized by the
    `passkey` mechanism never touches a payment rail, so there is no VPA and the
    credential id is the source instead — recorded rather than inferred, so a
    verifier is never left guessing which derivation produced a pseudonym."""


@dataclass
class CheckoutContext:
    """Everything the money path needs, injected so it can be driven directly."""

    trait: TraitClient | None = None

    @property
    def policy(self) -> Policy:
        """The live Policy, read per decision. Never a field: a copy taken when
        this context was built is a copy that stops tracking the Merchant's
        edits the moment they make one."""
        return current_policy()

    tokens: TokenStore = field(default_factory=TokenStore)
    provider: Any = None
    keyring: Keyring | None = None
    sessionmaker: Any = None
    receipts: Any = None
    store: CheckoutStore = field(default_factory=CheckoutStore)
    """Checkouts in flight and the Decision each is waiting to settle against,
    in the sidecar's own database. Three dicts until 09-21 — `pending`,
    `by_token` and `by_link` — which is why a restart between the tap and the
    Provider's callback lost the order the money belonged to."""
    passkey_rp: Any = None
    """A `PasskeyRP`. `None` means this deploy runs no passkey ceremony, and the
    approve page offers none rather than offering one that cannot complete."""
    merchant_domain: str = "spoiledduckie.localhost"
    public_origin: str = ""
    deploy_pseudonym_key: bytes = b""
    demo: bool = True

    @property
    def origin(self) -> str:
        return self.public_origin or f"https://{self.merchant_domain}"

    def ready(self) -> bool:
        return all((self.trait, self.provider, self.keyring, self.sessionmaker))


_context = CheckoutContext()


def configure(context: CheckoutContext) -> None:
    global _context
    _context = context


def get_context() -> CheckoutContext:
    return _context


async def _catalogue_facts(
    ctx: CheckoutContext,
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, int]]:
    """Group, tags and attested price per SKU, read fresh from door 1.

    Fresh every decision because the Merchant is truth (ADR-0001): a price the
    sidecar remembered is a price the Merchant may have changed, and the Gate
    exists to catch exactly that.
    """
    assert ctx.trait is not None
    catalogue = await ctx.trait.catalog_read()
    group_of = {item.sku: item.group_id for item in catalogue.items}
    tags_of = {item.sku: list(item.tags) for item in catalogue.items}
    prices = {item.sku: item.price_minor for item in catalogue.items}
    return group_of, tags_of, prices


def _authority_for(
    method: PaymentMethod, *, passkey: bool = False
) -> tuple[AuthorityKind, IntentMechanism | None]:
    """Which Authority a tap terminates in (§6.5's table, not a choice made here).

    A completed passkey ceremony changes the *kind*, never the money: on the
    prepaid path it is `passkey` itself, binding the cart to the payer's device
    where `upi-pin` would have bound only the amount; on cash it is the
    `passkey` mechanism of `confirmed-intent`. The payment still moves the same
    way afterwards — a passkey is evidence of agreement, not a credential.

    Without one, cash has no rail to authenticate against and takes
    `confirmed-intent` / `upi-verify`, and everything else terminates in the
    payer's own PSP, which is `upi-pin` — landing with the money rather than
    before it.
    """
    if method is PaymentMethod.CASH_ON_DELIVERY:
        return AuthorityKind.CONFIRMED_INTENT, (
            IntentMechanism.PASSKEY if passkey else IntentMechanism.UPI_VERIFY
        )
    if passkey:
        return AuthorityKind.PASSKEY, None
    return AuthorityKind.UPI_PIN, None


async def _request_for(
    ctx: CheckoutContext,
    checkout: Pending,
    *,
    authority: Authority,
    order_salt: bytes,
) -> DecisionInput:
    group_of, tags_of, prices = await _catalogue_facts(ctx)
    return DecisionInput(
        order_id=checkout.order_id,
        cart_id=checkout.cart_id,
        lines=checkout.lines,
        destination=checkout.destination,
        contact=checkout.contact,
        fulfillment_option_id=checkout.fulfillment_option_id,
        order_salt=order_salt,
        expiry_utc=checkout.expiry_utc,
        merchant_domain=ctx.merchant_domain,
        agent_id=checkout.agent_id,
        consumer_id=_consumer_id(ctx, checkout),
        authority=authority,
        method=checkout.method,
        pinned_quote=checkout.quote,
        pinned_quote_bytes=checkout.quote_bytes,
        group_of=group_of,
        tags_of=tags_of,
        attested_prices=prices,
        discount_code=checkout.discount_code,
    )


def _consumer_id(ctx: CheckoutContext, checkout: Pending) -> str:
    """Pseudonymous and stable per deploy (ADR-0011). Empty until a handle
    exists — a fabricated id would be worse than an absent one."""
    if not (ctx.deploy_pseudonym_key and checkout.payer_handle):
        return ""
    return derive_consumer_id(
        ctx.deploy_pseudonym_key, checkout.payer_handle, source=checkout.handle_source
    )


async def _salt_for(ctx: CheckoutContext, checkout: Pending) -> bytes:
    """Re-read the salt from the Merchant, in request memory only (§6.3a).

    Door 7 keys on `cart_id:attempt`, so this replays the same order rather than
    creating a second one.
    """
    assert ctx.trait is not None
    created = await ctx.trait.orders_create(
        checkout.cart_id,
        checkout.lines,
        checkout.destination,
        checkout.contact,
        checkout.fulfillment_option_id,
        agent_id=checkout.agent_id,
    )
    return bytes.fromhex(created.order_salt_hex)


#: Door 8's idempotency key is `order_id:attempt`, so every distinct transition
#: needs its own attempt number. Using the default for all of them makes the
#: second transition a silent replay of the first — an order reaches `confirmed`
#: and then stays there while the caller believes it said `paid`, which is
#: exactly what happened. Keyed by target status rather than by a counter, so a
#: **retry of the same transition** still replays, which is what the key is for.
_STATUS_ATTEMPT: dict[OrderStatus, int] = {
    OrderStatus.CONFIRMED: 1,
    OrderStatus.PAID: 2,
    OrderStatus.CANCELLED: 3,
    OrderStatus.EXPIRED: 4,
    OrderStatus.FAILED: 5,
    OrderStatus.REFUNDED: 6,
    OrderStatus.COMPLETED: 7,
}


async def set_status(
    ctx: CheckoutContext,
    order_id: str,
    status: OrderStatus,
    reason: str = "",
    *,
    agent_id: str = "",
    receipt_id: str = "",
) -> None:
    """The one place an order changes state, and therefore the one place an
    event is raised.

    Emitting from here rather than from each caller is what makes "an agent
    hears about every transition" true instead of intended: a new call site that
    forgot to publish would be a status the agent only learns by polling, which
    is the thing events exist to remove.
    """
    assert ctx.trait is not None
    order = await ctx.trait.orders_set_status(
        order_id, status.value, reason, attempt=_STATUS_ATTEMPT[status]
    )
    if order.status is not status:
        # The Merchant is truth: if it did not take the transition, saying it did
        # would put a false status in front of the Consumer and in the receipt.
        raise CheckoutRefused(
            ReasonCode.NOT_FOUND,
            f"The shop kept {order_id} at {order.status.value} rather than moving it to "
            f"{status.value}.",
        )
    # Only after the Merchant took the transition. An event published on the
    # attempt would tell an agent an order moved when it had not — and the
    # Merchant is truth here, which is the whole reason the check above exists.
    await publish_order_event(
        ctx, order_id=order_id, status=status, agent_id=agent_id, receipt_id=receipt_id
    )


# ── 1. start ─────────────────────────────────────────────────────────────────


async def start(
    ctx: CheckoutContext,
    *,
    cart_id: str,
    lines: list[Line],
    destination: Destination,
    contact: dict[str, str],
    fulfillment_option_id: str,
    method: PaymentMethod = PaymentMethod.UPI,
    agent_id: str = "",
    discount_code: str | None = None,
    details: dict[str, str] | None = None,
) -> Pending:
    """Create the order, quote it, and mint the tap token over that exact total.

    No stock is held and no money moves here. The order is `pending`, which is
    the Quote's validity window and not an inventory hold.

    `details` are door-10 buyer metadata (delivery slot, instructions). They
    ride to the Merchant's order row and into its quote validation, never into
    the Gate: `quote-fresh` byte-compares the Merchant's quote response, which
    carries no details echo, so details are pinned by the cleared basket and
    the merchant order row rather than by the byte comparison.
    """
    if not ctx.ready():
        raise CheckoutRefused(
            ReasonCode.NOT_FOUND,
            "This sidecar has no trait, provider, signing key or database wired, so it "
            "cannot take an order. Check /readyz.",
        )
    assert ctx.trait is not None

    created = await ctx.trait.orders_create(
        cart_id,
        lines,
        destination,
        contact,
        fulfillment_option_id,
        agent_id=agent_id or None,
        details=details,
    )
    salt = bytes.fromhex(created.order_salt_hex)  # request memory only (§6.3a)

    quote, quote_bytes = await ctx.trait.quote(
        lines,
        destination,
        fulfillment_option_id=fulfillment_option_id,
        discount_code=discount_code,
        details=details,
    )
    created_at = datetime.now(UTC)
    expiry = (created_at + CHECKOUT_TTL).isoformat().replace("+00:00", "Z")

    # The **attested** price per line, exactly as the Gate will use it. The
    # Authority is taken over this hash and the Gate recomputes it at the tap:
    # if the two are built from different line prices the binding compares a
    # hash to a different hash and means nothing. `upi-pin` hides that, because
    # it defers before the comparison — so it must be right here.
    _, _, attested = await _catalogue_facts(ctx)

    digest = compute_cart_hash(
        lines=[
            {"sku": ln.sku, "qty": ln.qty, "price_minor": attested.get(ln.sku, 0)} for ln in lines
        ],
        quote_hash_hex=quote_hash(quote.model_dump(mode="json")),
        destination_hash=pii_commit(salt, destination.model_dump()),
        contact_hash=pii_commit(salt, dict(sorted(contact.items()))),
        fulfillment_option_id=fulfillment_option_id,
        total_minor=quote.total_minor,
        currency=quote.currency,
        merchant_domain=ctx.merchant_domain,
        expiry_utc=expiry,
    )

    checkout = Pending(
        cart_id=cart_id,
        order_id=created.order_id,
        lines=list(lines),
        destination=destination,
        contact=dict(contact),
        fulfillment_option_id=fulfillment_option_id,
        expiry_utc=expiry,
        agent_id=agent_id,
        method=method,
        quote=quote,
        quote_bytes=quote_bytes,
        cart_hash=digest,
        total_minor=quote.total_minor,
        discount_code=discount_code,
        created_at=created_at,
    )
    token = await ctx.tokens.issue_tap(checkout.order_id, digest, quote.total_minor)
    checkout.tap_token = token.token
    # Written once, with its token already on it: a row saved before the token
    # existed would be a checkout no approve link could ever find.
    await ctx.store.save(checkout)
    return checkout


def approve_url(ctx: CheckoutContext, checkout: Pending) -> str:
    return f"{ctx.origin}/agentic/approve?t={checkout.tap_token}"


# ── 2. tap ───────────────────────────────────────────────────────────────────


@dataclass
class TapResult:
    checkout: Pending
    decision: Decision | None
    pay_url: str = ""
    receipt_id: str = ""
    reason_code: ReasonCode | None = None
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.reason_code is not None


async def tap(
    ctx: CheckoutContext, token: str, *, method: PaymentMethod | None = None
) -> TapResult:
    """The Consumer approved. This is the only place a spend is permitted.

    The token is spent **before** the Gate runs, single-use, and bound to the
    cart_hash it was minted over — so a basket edited after render invalidates
    it even when the total did not move.
    """
    checkout = await ctx.store.by_token(token)
    if checkout is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "That approval link is not valid.")
    if method is not None:
        checkout.method = method

    try:
        await ctx.tokens.spend_tap(token, cart_hash=checkout.cart_hash)
    except TraitError as exc:
        raise CheckoutRefused(exc.code, exc.detail) from None

    # Consumed, not read: a ceremony answers one tap. Taken after the token is
    # spent so a refused tap cannot leave an agreement lying about for the next.
    verified = await ctx.passkey_rp.take(token) if ctx.passkey_rp is not None else None
    if verified is not None and verified.cart_hash != checkout.cart_hash:
        # Belt and braces over the challenge itself, which already covers the
        # cart. This catches the ceremony being carried to a different checkout,
        # which the challenge cannot see.
        raise CheckoutRefused(
            ReasonCode.AUTHORITY_STALE,
            "that passkey was used to agree to a different basket.",
        )

    kind, mechanism = _authority_for(checkout.method, passkey=verified is not None)
    if verified is not None:
        # There is no payment rail on the cash path, so the credential id is the
        # only handle a `consumer_id` can come from (A4, ADR-0011).
        checkout.handle_source = HandleSource.CREDENTIAL_ID
        checkout.payer_handle = verified.credential_id
    authority = Authority(
        kind=kind,
        mechanism=mechanism,
        # `upi-pin` defers: it arrives with the money and `settle` resolves it.
        # Saying it is present here would put a false statement into signed
        # evidence.
        present=kind is not AuthorityKind.UPI_PIN,
        ceremony=(
            verified.ceremony.value
            if verified is not None
            else ("assertion" if kind is not AuthorityKind.UPI_PIN else None)
        ),
        bound_cart_hash=checkout.cart_hash,
        bound_amount_minor=checkout.total_minor,
    )
    salt = await _salt_for(ctx, checkout)
    request = await _request_for(ctx, checkout, authority=authority, order_salt=salt)

    assert ctx.trait is not None
    gate = Gate(ctx.trait, ctx.policy)
    result = await run_core(gate, request)
    if result.refused:
        assert result.reason_code is not None
        return TapResult(
            checkout=checkout,
            decision=None,
            reason_code=result.reason_code,
            detail=result.detail,
        )

    decision = result.decision
    assert decision is not None

    # Stock is held here and nowhere earlier: `pending` holds none, and an agent
    # that never reaches a human tap must not be able to reserve inventory.
    await ctx.trait.reserve(checkout.order_id, checkout.lines, discount_code=checkout.discount_code)
    await set_status(ctx, checkout.order_id, OrderStatus.CONFIRMED, agent_id=checkout.agent_id)
    checkout.status = OrderStatus.CONFIRMED
    checkout.confirmed_at = datetime.now(UTC)

    if checkout.method is PaymentMethod.CASH_ON_DELIVERY:
        # ADR-0018: no Ledger entry at order time and no Provider at all. The
        # money event is collection, which is a Merchant action later.
        checkout.receipt_id = await _seal(ctx, checkout, decision, entries=[])
        await ctx.store.save(checkout, decision)
        return TapResult(checkout=checkout, decision=decision, receipt_id=checkout.receipt_id)

    from openstore.sidecar.core.db import session_scope

    async with session_scope(ctx.sessionmaker) as session:
        await Ledger(session).reserve(
            checkout.order_id, decision.total_minor, decision.quote.currency
        )

    link = await ctx.provider.make_link(
        checkout.order_id,
        decision.total_minor,
        decision.quote.currency,
        expires_in_seconds=PAYMENT_LINK_SECONDS,
    )
    checkout.link_id = link.link_id
    # The hold must never outlive the link it was taken for, so the sweeper is
    # given the Provider's own deadline rather than assuming the default window.
    checkout.link_expires_at = checkout.confirmed_at + timedelta(seconds=link.expires_in_seconds)
    # The link id and the Decision land in the same write. They are what the
    # Provider's callback arrives looking for, and a callback can arrive before
    # the Consumer's browser has finished redirecting.
    await ctx.store.save(checkout, decision)
    return TapResult(checkout=checkout, decision=decision, pay_url=link.url)


# ── 3. complete ──────────────────────────────────────────────────────────────


async def complete(ctx: CheckoutContext, link_id: str, *, payer_handle: str = "") -> Pending:
    """The money arrived. Reconcile it, commit the stock, seal the receipt."""
    checkout = await ctx.store.by_link(link_id)
    if checkout is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "No checkout is waiting on that payment.")
    decision = await ctx.store.decision_for(checkout.order_id)
    if decision is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "That checkout has no decision to settle.")
    if payer_handle and checkout.handle_source is HandleSource.PAYER_HANDLE:
        # A passkey order already derived its pseudonym from the credential id,
        # and letting the rail's handle overwrite it would silently change which
        # derivation the Transcript claims.
        checkout.payer_handle = payer_handle
    assert ctx.trait is not None

    status = await ctx.provider.check_status(link_id)
    record = ProviderRecord(
        order_id=checkout.order_id,
        amount_minor=status.amount_minor,
        currency=status.currency,
        reference=status.reference,
        succeeded=status.paid,
    )

    from openstore.sidecar.core.db import session_scope

    async with session_scope(ctx.sessionmaker) as session:
        ledger = Ledger(session)
        settlement = await settle(decision, record, ledger)
        entries = [
            {"kind": e.kind.value, "amount_minor": e.amount_minor, "currency": e.currency}
            for e in await ledger.entries(checkout.order_id)
        ]

    if settlement.status is not OrderStatus.PAID:
        checkout.status = settlement.status
        await ctx.store.save(checkout)
        await set_status(
            ctx,
            checkout.order_id,
            settlement.status,
            settlement.reason_code.value if settlement.reason_code else "",
            agent_id=checkout.agent_id,
        )
        raise CheckoutRefused(
            settlement.reason_code or ReasonCode.NOT_FOUND,
            f"The payment did not settle: the order is {settlement.status.value}.",
        )

    await ctx.trait.commit(checkout.order_id)
    await set_status(ctx, checkout.order_id, OrderStatus.PAID, agent_id=checkout.agent_id)
    checkout.status = OrderStatus.PAID
    checkout.receipt_id = await _seal(ctx, checkout, decision, entries=entries)
    await ctx.store.save(checkout)
    return checkout


async def _seal(
    ctx: CheckoutContext, checkout: Pending, decision: Decision, *, entries: list[dict[str, Any]]
) -> str:
    """Five sections, hash-chained, signed by the key the card publishes."""
    import json

    assert ctx.keyring is not None
    salt = await _salt_for(ctx, checkout)  # request memory only (§6.3a)
    receipt_id = f"rcpt_{checkout.order_id.removeprefix('ord_')}"
    _, _, prices = await _catalogue_facts(ctx)
    # Declared by the kind (§6.5's table), never chosen here: a receipt that
    # names a stronger binding than the ceremony produced is a false claim in
    # signed evidence.
    kind = decision.transcript.authority_kind or AuthorityKind.UPI_PIN
    binding, _ = binding_for(kind, decision.transcript.authority_mechanism)

    builder = (
        BundleBuilder(receipt_id, ctx.merchant_domain, demo=ctx.demo)
        .bought(
            quote=json.loads(decision.quote_bytes),
            lines=[{"sku": ln.sku, "qty": ln.qty} for ln in checkout.lines],
            attestation_digests={
                ln.sku: attestation_digest(ln.sku, prices.get(ln.sku, 0), [], {})
                for ln in checkout.lines
            },
            destination_hash=pii_commit(salt, checkout.destination.model_dump()),
            contact_hash=pii_commit(salt, dict(sorted(checkout.contact.items()))),
        )
        .tapped(
            authority_kind=kind,
            mechanism=decision.transcript.authority_mechanism,
            binding_what=binding.what.value,
            binding_by=binding.by.value,
            ceremony=decision.transcript.ceremony,
            cart_hash=checkout.cart_hash,
        )
        .decided(transcript=json.loads(decision.transcript.to_bytes()))
        .told(notifications=[])
        .moved(entries=entries, method=checkout.method)
    )
    bundle = builder.seal(jwks_snapshot=ctx.keyring.jwks())
    bundle.signing_kid = ctx.keyring.current.kid
    bundle.signature = sign(ctx.keyring.current, bundle.signing_payload())
    if ctx.receipts is not None:
        await ctx.receipts.put(bundle)
    return receipt_id


async def cancel(ctx: CheckoutContext, order_id: str, *, reason: str = "") -> Pending:
    """Abandon a checkout before any money has moved.

    Pre-money only, and the ordering matters: stock goes back **before** the
    status moves, so a crash between the two leaves an order that still looks
    live rather than a cancelled one holding inventory nobody can buy.

    A `paid` order is not cancelled here. That is a refund, which moves money
    and belongs to the Merchant, not to an agent.
    """
    checkout = await ctx.store.get(order_id)
    if checkout is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "No such checkout.")
    if checkout.status is OrderStatus.PAID:
        raise CheckoutRefused(
            ReasonCode.METHOD_NOT_SUPPORTED,
            "That order is paid. Cancelling it would move money, which is a refund and "
            "the Merchant's to make.",
        )
    assert ctx.trait is not None

    if checkout.status is OrderStatus.CONFIRMED:
        # Only a confirmed order ever took a hold (door 3 runs at the tap), and
        # releasing one that was never taken refuses `no-hold`.
        await ctx.trait.release(checkout.order_id)
        if ctx.sessionmaker is not None:
            from openstore.sidecar.core.db import session_scope

            async with session_scope(ctx.sessionmaker) as session:
                ledger = Ledger(session)
                held = (await ledger.position(checkout.order_id)).open_holds_minor
                if held > 0:
                    # Exactly its own hold: releasing a different amount would
                    # close escrow-zero on an entry that balances nothing.
                    await ledger.release(checkout.order_id, held, checkout.quote.currency)

    await set_status(
        ctx,
        checkout.order_id,
        OrderStatus.CANCELLED,
        reason or "cancelled",
        agent_id=checkout.agent_id,
    )
    checkout.status = OrderStatus.CANCELLED
    if checkout.link_id:
        await ctx.provider.cancel(checkout.link_id)
    # The tap token dies with the checkout: an approve link for a cancelled
    # order must not still open. Cleared off the row rather than deleted from
    # the token table, so the token itself stays readable as history while
    # resolving to no checkout — which is exactly what the page should say.
    checkout.tap_token = ""
    await ctx.store.save(checkout)
    return checkout


async def collect_cash(ctx: CheckoutContext, order_id: str) -> Pending:
    """COD collection: one CAPTURE with no preceding RESERVE (ADR-0018)."""
    checkout = await ctx.store.get(order_id)
    if checkout is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "No such checkout.")
    decision = await ctx.store.decision_for(order_id)
    if decision is None:
        raise CheckoutRefused(ReasonCode.NOT_FOUND, "That checkout has no decision to collect on.")

    from openstore.sidecar.core.db import session_scope

    assert ctx.trait is not None
    async with session_scope(ctx.sessionmaker) as session:
        await collect(decision, Ledger(session))
    await set_status(ctx, order_id, OrderStatus.PAID, agent_id=checkout.agent_id)
    checkout.status = OrderStatus.PAID
    await ctx.store.save(checkout)
    return checkout


async def publish_order_event(
    ctx: CheckoutContext,
    *,
    order_id: str,
    status: OrderStatus,
    agent_id: str = "",
    receipt_id: str = "",
) -> None:
    """Queue an order event for the agent that placed the order, if it wants one.

    Every reason to skip is a quiet one, so each is checked rather than caught:
    no event store (a sidecar with no database), no agent (a Consumer-initiated
    change), no record, or an agent that declared no callback. A `try/except`
    around this would turn all four into the same shrug.
    """
    from openstore.sidecar.protocols.agent_routes import get_surface

    surface = get_surface()
    if not surface.events.enabled or not agent_id:
        return
    record = surface.admission.seen.get(agent_id)
    if record is None or not record.callback_url:
        return

    event_id = f"evt_{secrets.token_hex(8)}"
    body = event_body(
        event_id=event_id,
        merchant_domain=ctx.merchant_domain,
        order_id=order_id,
        kind=f"order.{status.value}",
        at=datetime.now(UTC).isoformat(),
        receipt_id=receipt_id,
    )
    await surface.events.enqueue(
        event_id=event_id,
        agent_id=agent_id,
        order_id=order_id,
        kind=body["kind"],
        body=body,
        callback_url=record.callback_url,
    )
