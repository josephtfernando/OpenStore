"""The closed sets, and the one table that maps a refusal to an HTTP status.

This file is the registry. `docs/CODES.md` is generated from it by
`scripts/registry_diff.py` and diffed in CI, so a code that exists in prose but
not here is a red build (SPEC §12). Nothing downstream may define a reason
code, status, scope, or ledger kind of its own.

Frozen at hour 0 (SPECS/PLAN.md §12) — everything downstream pins these bytes.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class LedgerKind(StrEnum):
    """Money entries only. Stock is held by trait door 3, never by the Ledger."""

    RESERVE = "RESERVE"
    CAPTURE = "CAPTURE"
    RELEASE = "RELEASE"
    REFUND = "REFUND"
    REVERSAL = "REVERSAL"


@unique
class OrderStatus(StrEnum):
    """Eight, and there is never a ninth. Full vs partial refund is derived from
    `refunded_minor`; dispatch is a Merchant-recorded event, not a status."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    REFUNDED = "refunded"
    COMPLETED = "completed"


#: Dispatch may be recorded from `confirmed` onward and never on a
#: terminal-negative status: goods that never left cannot carry a tax invoice,
#: and a sequence number burned on one is a gap somebody has to explain
#: (ADR-0020, SPEC §6).
DISPATCHABLE_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.PAID,
        OrderStatus.REFUNDED,
        OrderStatus.COMPLETED,
    }
)


@unique
class Scope(StrEnum):
    """Four, identical for both admission routes. No tier unlocks money:
    `CONFIRM` without a fresh accepted Authority is refused regardless."""

    SEARCH = "search"
    BUILD_BASKET = "build-basket"
    START_CHECKOUT = "start-checkout"
    CONFIRM = "confirm"


@unique
class AuthorityKind(StrEnum):
    """ADR-0017. The first three are live; `MANDATE` is registered, recorded,
    and refused in v1."""

    UPI_PIN = "upi-pin"
    PASSKEY = "passkey"
    CONFIRMED_INTENT = "confirmed-intent"
    MANDATE = "mandate"


@unique
class IntentMechanism(StrEnum):
    """Closed at two. An OTP to a Contact Point is **not** a member and never
    becomes one — it binds no funding instrument."""

    UPI_VERIFY = "upi-verify"
    PASSKEY = "passkey"


@unique
class BindingWhat(StrEnum):
    CART = "cart"
    AMOUNT = "amount"
    NONE = "none"


@unique
class BindingBy(StrEnum):
    PAYER_DEVICE = "payer-device"
    PAYER_BANK = "payer-bank"
    MERCHANT = "merchant"


@unique
class Ceremony(StrEnum):
    """Which WebAuthn ceremony carried a `passkey` Authority — distinct from the
    Authority kind itself, so "one prompt" is a claim the golden replays can
    check rather than a hope (SPEC §7)."""

    ENROLLMENT = "enrollment"
    ASSERTION = "assertion"


@unique
class CheckResult(StrEnum):
    """`DEFERRED` exists because `upi-pin`'s Authority arrives with the money.
    Recording it as `PASS` at `decide()` would put a false statement into signed
    evidence. `settle()` must resolve every `DEFERRED` check to `PASS` before it
    captures, and a bundle carrying an unresolved one fails verification."""

    PASS = "pass"
    FAIL = "fail"
    DEFERRED = "deferred"


@unique
class PaymentMethod(StrEnum):
    """`UPI` and `CASH_ON_DELIVERY` are live. `CARD` and `NETBANKING` are
    declarable by an adapter and enableable without touching the money core
    (ADR-0013)."""

    UPI = "upi"
    CASH_ON_DELIVERY = "cash-on-delivery"
    CARD = "card"
    NETBANKING = "netbanking"


@unique
class RefundRequestState(StrEnum):
    """An agent's refund *request*, which is not a refund.

    Three, and there is never a fourth: a request is open, the Merchant paid it,
    or the Merchant refused it. The money itself is `LedgerKind.REFUND` and the
    order's own `REFUNDED` status — this set tracks the ask, not the movement,
    and conflating the two would let an agent's request look like money the shop
    has already sent.

    Registered here rather than at the call site per §0's first standing rule;
    the decision is logged in `LOGS.md`.
    """

    REQUESTED = "requested"
    APPROVED = "approved"
    DECLINED = "declined"


@unique
class CancellationReason(StrEnum):
    CONSUMER_WALKAWAY = "consumer-walkaway"
    CONSUMER_DECLINED = "consumer-declined"
    SHOP_REJECT = "shop-reject"
    RTO = "rto"


@unique
class StockMoveChannel(StrEnum):
    SITE_DIRECT = "site-direct"
    AGENT_RESERVE = "agent-reserve"
    AGENT_COMMIT = "agent-commit"
    AGENT_RELEASE = "agent-release"
    REFUND = "refund"
    RESTOCK = "restock"
    ADMIN_ADJUST = "admin-adjust"
    RTO = "rto"


@unique
class AvailabilityBucket(StrEnum):
    """What a Buyer Agent is told about stock. Exact counts stay on the private
    network (trait door 2) — a count handed to a self-registered stranger is both
    competitive intelligence and an inventory-probing oracle (SPEC §5)."""

    IN_STOCK = "in-stock"
    LOW_STOCK = "low-stock"
    SOLD_OUT = "sold-out"


@unique
class DiscountVisibility(StrEnum):
    """ADR-0015. A `PRIVATE` code never transits the Buyer Agent — it is entered
    on the approve page, which re-quotes and rebinds before anything is signed."""

    PUBLIC = "public"
    PRIVATE = "private"


@unique
class Protocol(StrEnum):
    """Envelopes over one money core. The core Transcript bytes are identical
    across all four; only the envelope differs (SPEC §9)."""

    MCP = "mcp"
    UCP = "ucp"
    ACP = "acp"
    AP2 = "ap2"


@unique
class Door(StrEnum):
    """The Merchant-truth trait (§6.1). Nine doors are required; door 10
    `REQUIREMENTS_READ` is optional — a Merchant that does not implement it
    gets the fixed buyer-shape (Destination + Contact) and nothing breaks.
    Door 9 `QUOTE` is read-only and side-effect-free; every mutating door takes
    an idempotency key, and `ORDERS_CREATE` keys on `cart_id:attempt` because
    it is the door that produces the `order_id`."""

    CATALOG_READ = "catalog.read"
    STOCK_READ = "stock.read"
    RESERVE = "reserve"
    COMMIT = "commit"
    RELEASE = "release"
    RESTOCK = "restock"
    ORDERS_CREATE = "orders.create"
    ORDERS_READ = "orders.read"
    ORDERS_SET_STATUS = "orders.set-status"
    QUOTE = "quote"
    REQUIREMENTS_READ = "requirements.read"


@unique
class GateCheck(StrEnum):
    """`decide()`'s twelve checks, in the order it runs them, stopping at the
    first failure. These names are written into the byte-stable Transcript, so
    renaming or reordering one changes evidence bytes (§6.5, SPEC §4)."""

    AUTHORITY_PRESENT_AND_ACCEPTED = "authority-present-and-accepted"
    CURRENCY = "currency"
    MERCHANT = "merchant"
    WINDOW = "window"
    COUNT = "count"
    QTY = "qty"
    BLOCKED = "blocked"
    TAGS = "tags"
    CAPS = "caps"
    QUOTE_CONSISTENT = "quote-consistent"
    QUOTE_FRESH = "quote-fresh"
    METHOD_ENABLED = "method-enabled"


#: The fixed order `decide()` runs them in. A tuple, not the enum's definition
#: order by accident: the Transcript's bytes depend on it.
GATE_CHECK_ORDER: tuple[GateCheck, ...] = (
    GateCheck.AUTHORITY_PRESENT_AND_ACCEPTED,
    GateCheck.CURRENCY,
    GateCheck.MERCHANT,
    GateCheck.WINDOW,
    GateCheck.COUNT,
    GateCheck.QTY,
    GateCheck.BLOCKED,
    GateCheck.TAGS,
    GateCheck.CAPS,
    GateCheck.QUOTE_CONSISTENT,
    GateCheck.QUOTE_FRESH,
    GateCheck.METHOD_ENABLED,
)


@unique
class ProviderOp(StrEnum):
    """The Provider trait. The three `*_BLOCK` ops are a declarable capability
    that no adapter declares and no path consumes in v1 — a seam left open for
    ADR-0024's repeat-purchase work, not a COD hold (ADR-0018 retired that)."""

    MAKE_LINK = "make-link"
    CHECK_STATUS = "check-status"
    CANCEL = "cancel"
    REFUND = "refund"
    BLOCK = "block"
    CAPTURE_BLOCK = "capture-block"
    RELEASE_BLOCK = "release-block"


@unique
class ToolName(StrEnum):
    """The closed agent-facing action set (SPECS/PLAN.md §16.9). Every name here
    is proposal-only: `PLACE_ORDER` returns an approve URL, never an order."""

    SEARCH = "search"
    READ_ITEM = "read-item"
    ADD_LINE = "add-line"
    REMOVE_LINE = "remove-line"
    CLEAR_BASKET = "clear-basket"
    SET_DESTINATION = "set-destination"
    SET_CONTACT = "set-contact"
    SET_DETAILS = "set-details"
    CHOOSE_FULFILLMENT = "choose-fulfillment"
    APPLY_PUBLIC_CODE = "apply-public-code"
    START_CHECKOUT = "start-checkout"
    PLACE_ORDER = "place-order"
    ORDER_STATUS = "order-status"
    CANCEL_ORDER = "cancel-order"
    REQUEST_REFUND = "request-refund"


#: Which scope each action requires. `PLACE_ORDER` holding `CONFIRM` still buys
#: nothing on its own — the Gate refuses without a fresh accepted Authority.
#:
#: `ORDER_STATUS`, `CANCEL_ORDER` and `REQUEST_REFUND` are scoped by ladder step 4
#: (SPECS/PLAN.md §0): the plans group them as scoped reads without naming a
#: scope, so the read takes `SEARCH` and the two that change an order's fate take
#: the narrower `START_CHECKOUT`. Logged under `## OPEN — h0` in LOGS.md.
TOOL_SCOPES: dict[ToolName, Scope] = {
    ToolName.SEARCH: Scope.SEARCH,
    ToolName.READ_ITEM: Scope.SEARCH,
    ToolName.ADD_LINE: Scope.BUILD_BASKET,
    ToolName.REMOVE_LINE: Scope.BUILD_BASKET,
    ToolName.CLEAR_BASKET: Scope.BUILD_BASKET,
    ToolName.SET_DESTINATION: Scope.BUILD_BASKET,
    ToolName.SET_CONTACT: Scope.BUILD_BASKET,
    ToolName.SET_DETAILS: Scope.BUILD_BASKET,
    ToolName.CHOOSE_FULFILLMENT: Scope.BUILD_BASKET,
    ToolName.APPLY_PUBLIC_CODE: Scope.BUILD_BASKET,
    ToolName.START_CHECKOUT: Scope.START_CHECKOUT,
    ToolName.PLACE_ORDER: Scope.CONFIRM,
    ToolName.ORDER_STATUS: Scope.SEARCH,
    ToolName.CANCEL_ORDER: Scope.START_CHECKOUT,
    ToolName.REQUEST_REFUND: Scope.START_CHECKOUT,
}


@unique
class ReasonCode(StrEnum):
    """Every refusal in the system. The HTTP status is derived from the code by
    `HTTP_STATUS` below and is never chosen at the call site."""

    SOLD_OUT = "sold-out"
    VARIANT_REQUIRED = "variant-required"
    ADDON_WITHOUT_PARENT = "addon-without-parent"
    PRICE_CHANGED = "price-changed"
    QUOTE_INCONSISTENT = "quote-inconsistent"
    CODE_INVALID = "code-invalid"
    DESTINATION_UNSERVICEABLE = "destination-unserviceable"
    METHOD_NOT_SUPPORTED = "method-not-supported"
    AMOUNT_MISMATCH = "amount-mismatch"
    TIME_LIMIT_REACHED = "time-limit-reached"
    PAYMENT_WINDOW_ELAPSED = "payment-window-elapsed"
    DELIVERY_WINDOW_ELAPSED = "delivery-window-elapsed"
    AUTHORITY_MISSING = "authority-missing"
    AUTHORITY_KIND_NOT_ENABLED = "authority-kind-not-enabled"
    AUTHORITY_STALE = "authority-stale"
    INTENT_MECHANISM_NOT_ENABLED = "intent-mechanism-not-enabled"
    CAP_EXCEEDED = "cap-exceeded"
    QTY_EXCEEDED = "qty-exceeded"
    COUNT_EXCEEDED = "count-exceeded"
    WINDOW_CLOSED = "window-closed"
    BLOCKED_ITEM = "blocked-item"
    TAG_REFUSED = "tag-refused"
    CURRENCY_MISMATCH = "currency-mismatch"
    MERCHANT_MISMATCH = "merchant-mismatch"
    SIGNATURE_INVALID = "signature-invalid"
    NO_HOLD = "no-hold"
    RATE_LIMITED = "rate-limited"
    IDEMPOTENCY_CONFLICT = "idempotency-conflict"
    """One key, two different questions. Never returned for a genuine replay —
    a repeat of the same call with the same key is a success carrying the first
    answer, because that is the whole point of the key."""
    PROFILE_REFUSED = "profile-refused"
    AGENT_BLOCKED = "agent-blocked"
    NOT_FOUND = "not-found"
    UNTRUSTED_KEY = "untrusted-key"
    DISPATCH_NOT_ALLOWED = "dispatch-not-allowed"
    CANCEL_NOT_ALLOWED = "cancel-not-allowed"


#: Reason code → HTTP status. **One table, and the call site never chooses.**
#: Two implementers picking statuses independently is how one client retries
#: what the other treats as fatal (§6.1).
#:
#: 409 — a business refusal against a well-formed request: the caller asked for
#:       something real that the Merchant's present state will not give.
#: 400 — the request itself is malformed or wrongly shaped.
#: 401 — the caller is not authenticated as who it claims to be.
#: 403 — authenticated, and not allowed.
#: 404 — `not-found`, which an agent also gets for somebody else's order.
#: 429 — over a published limit.
HTTP_STATUS: dict[ReasonCode, int] = {
    # Business refusals against well-formed requests.
    ReasonCode.SOLD_OUT: 409,
    ReasonCode.PRICE_CHANGED: 409,
    ReasonCode.CODE_INVALID: 409,
    ReasonCode.DESTINATION_UNSERVICEABLE: 409,
    ReasonCode.AMOUNT_MISMATCH: 409,
    ReasonCode.TIME_LIMIT_REACHED: 409,
    ReasonCode.PAYMENT_WINDOW_ELAPSED: 409,
    ReasonCode.DELIVERY_WINDOW_ELAPSED: 409,
    ReasonCode.CAP_EXCEEDED: 409,
    ReasonCode.QTY_EXCEEDED: 409,
    ReasonCode.COUNT_EXCEEDED: 409,
    ReasonCode.WINDOW_CLOSED: 409,
    ReasonCode.BLOCKED_ITEM: 409,
    ReasonCode.TAG_REFUSED: 409,
    ReasonCode.NO_HOLD: 409,
    ReasonCode.DISPATCH_NOT_ALLOWED: 409,
    ReasonCode.CANCEL_NOT_ALLOWED: 409,
    # Malformed or wrongly-shaped requests.
    ReasonCode.VARIANT_REQUIRED: 400,
    ReasonCode.ADDON_WITHOUT_PARENT: 400,
    ReasonCode.QUOTE_INCONSISTENT: 400,
    ReasonCode.CURRENCY_MISMATCH: 400,
    ReasonCode.MERCHANT_MISMATCH: 400,
    # Authentication and authorization.
    ReasonCode.SIGNATURE_INVALID: 401,
    ReasonCode.UNTRUSTED_KEY: 401,
    ReasonCode.AUTHORITY_MISSING: 403,
    ReasonCode.AUTHORITY_KIND_NOT_ENABLED: 403,
    ReasonCode.AUTHORITY_STALE: 403,
    ReasonCode.INTENT_MECHANISM_NOT_ENABLED: 403,
    ReasonCode.METHOD_NOT_SUPPORTED: 403,
    ReasonCode.PROFILE_REFUSED: 403,
    ReasonCode.AGENT_BLOCKED: 403,
    # Lookup and limits.
    ReasonCode.NOT_FOUND: 404,
    ReasonCode.RATE_LIMITED: 429,
    # 409: the request is well formed and the shop will not answer it, which is
    # exactly the "business refusal" bucket above. The caller fixes it by
    # choosing a new key, not by retrying this one.
    ReasonCode.IDEMPOTENCY_CONFLICT: 409,
}

#: Every closed set in this module, by the name `docs/CODES.md` prints. The
#: generator reads this rather than scanning the module, so adding an enum
#: without registering it here is visible in review.
CLOSED_SETS: dict[str, type[StrEnum]] = {
    "Ledger kinds": LedgerKind,
    "Order statuses": OrderStatus,
    "Scopes": Scope,
    "Authority kinds": AuthorityKind,
    "Confirmed-intent mechanisms": IntentMechanism,
    "Binding — what": BindingWhat,
    "Binding — by": BindingBy,
    "Ceremony": Ceremony,
    "Transcript check results": CheckResult,
    "Payment methods": PaymentMethod,
    "Cancellation reasons": CancellationReason,
    "Refund request states": RefundRequestState,
    "Stock-move channels": StockMoveChannel,
    "Availability buckets": AvailabilityBucket,
    "Discount visibility": DiscountVisibility,
    "Protocols": Protocol,
    "Trait doors": Door,
    "Gate checks": GateCheck,
    "Provider operations": ProviderOp,
    "Tool names": ToolName,
    "Reason codes": ReasonCode,
}


def http_status(code: ReasonCode) -> int:
    """The HTTP status for a refusal. Raises rather than defaulting: a code with
    no mapping is a bug in this file, and a silent 500 is how it would hide."""
    try:
        return HTTP_STATUS[code]
    except KeyError:  # pragma: no cover - guarded by test_codes.py
        raise AssertionError(f"reason code {code.value!r} has no HTTP status mapping") from None
