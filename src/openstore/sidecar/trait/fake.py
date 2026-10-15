"""An in-memory Merchant implementing all nine doors, served over real HTTP.

This is the conformance fake (A2). It is not a mock: the sidecar reaches it
through the same signed HTTP the real Merchant answers, so the conformance suite
exercises HMAC verification, idempotency replay and status mapping rather than
stepping around them.

**The pricing arithmetic below is the Merchant's, deliberately.** §16.11 is
implemented here and re-checked independently by the Gate's `quote-consistent`
(A3). If the two shared a module the check would be a tautology — it would prove
the sidecar agrees with itself. Two implementations of one pinned specification
is the entire point, and where they disagree, §16.11 is right and both are
wrong until they agree with it.

It can also behave badly on purpose, because the Gate's job is catching that:

- `quote_drift_paise` moves the price between calls, so `quote-fresh` has
  something real to catch.
- `external_sale()` lowers stock outside the agent path, so "the next Gate
  re-reads fresh" is a test rather than a claim.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from openstore.sidecar.core.codes import Door, OrderStatus, ReasonCode
from openstore.sidecar.trait.errors import TraitError
from openstore.sidecar.trait.signing import (
    IDEMPOTENCY_HEADER,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ReplayGuard,
    verify,
)

# ── §16.11, the arithmetic, pinned to the paise ──────────────────────────────


def extract_tax(inclusive_minor: int, rate_bp: int) -> int:
    """Step 5: tax already inside an inclusive amount.

    Exact decimal, never binary float: `0.1 + 0.2` is the reason this whole
    system counts in paise.
    """
    if rate_bp == 0:
        return 0
    value = (
        Decimal(inclusive_minor) * Decimal(rate_bp) / Decimal(10000 + rate_bp)
    )  # money-lint: decimal
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apportion(amount: int, weights: list[tuple[str, int]]) -> dict[str, int]:
    """Step 4: largest remainder, stated once so it is never re-derived.

    Floor each raw share; the leftover paise go one each to the largest
    fractional parts, descending, ties broken by SKU ascending. The shares sum
    **exactly** to the amount — an apportionment that loses a paise is a total
    that does not add up, which the Gate refuses as `quote-inconsistent`.
    """
    total_weight = sum(w for _, w in weights)
    if total_weight == 0:
        return {sku: 0 for sku, _ in weights}

    raw = {
        sku: Decimal(amount) * Decimal(w) / Decimal(total_weight)  # money-lint: decimal
        for sku, w in weights
    }
    shares = {sku: int(value) for sku, value in raw.items()}
    leftover = amount - sum(shares.values())

    ranked = sorted(raw, key=lambda sku: (-(raw[sku] - shares[sku]), sku))
    for sku in ranked[:leftover]:
        shares[sku] += 1

    assert sum(shares.values()) == amount, "apportionment must be exact"
    return shares


def split_cgst_sgst(tax: int) -> tuple[int, int]:
    """Step 6, the trap this section exists for. SGST takes the odd paise.
    Deterministic, stated once, never re-decided."""
    cgst = tax // 2
    return cgst, tax - cgst


# ── Merchant state ───────────────────────────────────────────────────────────


@dataclass
class FakeItem:
    sku: str
    group_id: str
    name: str
    price_minor: int
    hsn_sac: str
    gst_rate_bp: int
    low_stock_threshold: int = 3
    options: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class FakeGroup:
    id: str
    slug: str
    name: str
    option_axes: dict[str, list[str]] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class FakeZone:
    id: str
    label: str
    cost_minor: int
    eta_days: int
    states: tuple[str, ...] | None = None
    """None means 'every state this zone is not excluded from' — the catch-all."""


@dataclass
class FakeDiscount:
    code: str
    amount_minor: int
    label: str
    max_uses: int = 1
    uses: int = 0


@dataclass
class FakeOrder:
    order_id: str
    cart_id: str
    status: OrderStatus
    lines: list[dict[str, Any]]
    total_minor: int
    order_salt_hex: str
    refunded_minor: int = 0
    tracking_number: str | None = None
    carrier: str | None = None
    dispatched_at: str | None = None
    invoice_number: str | None = None
    expires_at: str | None = None


@dataclass
class FakeMerchant:
    """Merchant truth, in memory. Seeded from §16.3 by `seeded()`."""

    groups: dict[str, FakeGroup] = field(default_factory=dict)
    items: dict[str, FakeItem] = field(default_factory=dict)
    stock: dict[str, int] = field(default_factory=dict)
    zones: list[FakeZone] = field(default_factory=list)
    discounts: dict[str, FakeDiscount] = field(default_factory=dict)
    orders: dict[str, FakeOrder] = field(default_factory=dict)
    holds: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    held_codes: dict[str, str] = field(default_factory=dict)
    idempotency: dict[str, Any] = field(default_factory=dict)

    home_state: str = "KA"
    tax_inclusive: bool = True
    unserviceable_prefix: str = "19"

    #: Move the price by this many paise on every quote after the first, so
    #: `quote-fresh` has something real to catch.
    quote_drift_paise: int = 0

    #: Pin the order salt so a golden replay produces comparable bytes, exactly
    #: as hour 0's cart_hash vectors do. A real Merchant always generates one
    #: (§16.1: secrets.token_bytes(16)) and this stays empty outside tests.
    fixed_order_salt_hex: str = ""
    _quote_calls: int = 0

    # ── helpers ──────────────────────────────────────────────────────────────

    def external_sale(self, sku: str, qty: int = 1) -> None:
        """A walk-in bought one. Not an agent path, no hold, no order — the next
        Gate must simply see a lower number (SPEC §5)."""
        if self.stock[sku] < qty:
            raise ValueError(f"cannot sell {qty} of {sku}: only {self.stock[sku]} on hand")
        self.stock[sku] -= qty

    def _item(self, sku: str) -> FakeItem:
        """Resolve a SKU, refusing a Product Group id rather than guessing.

        A group id anywhere a SKU belongs refuses `variant-required` — the
        alternative is picking a size on the Consumer's behalf, which is a guess
        wearing a helpful expression.
        """
        if sku in self.groups:
            group = self.groups[sku]
            raise TraitError(
                ReasonCode.VARIANT_REQUIRED,
                f"{sku!r} is a Product Group; choose one of its Catalogue Items",
                {"group_id": sku, "axes": group.option_axes},
            )
        if sku not in self.items:
            raise TraitError(ReasonCode.NOT_FOUND, f"no Catalogue Item {sku!r}", {"sku": sku})
        return self.items[sku]

    def _zone_for(self, state: str) -> FakeZone:
        for zone in self.zones:
            if zone.states and state in zone.states:
                return zone
        for zone in self.zones:
            if zone.states is None:
                return zone
        raise TraitError(
            ReasonCode.DESTINATION_UNSERVICEABLE, f"no fulfillment zone covers {state}"
        )

    def _place_of_supply(self, item: FakeItem, destination_state: str) -> str:
        """Per line: the Destination state for goods, the place of performance
        for a service sold at the premises. This is what lets one basket carry
        IGST on one line and CGST/SGST on another."""
        return self.home_state if "service" in item.tags else destination_state

    # ── the nine doors ───────────────────────────────────────────────────────

    def catalog_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "groups": [
                {
                    "id": g.id,
                    "slug": g.slug,
                    "name": g.name,
                    "description": "",
                    "media": [],
                    "option_axes": g.option_axes,
                    "tags": g.tags,
                    "status": g.status,
                }
                for g in self.groups.values()
            ],
            "items": [
                {
                    "sku": i.sku,
                    "group_id": i.group_id,
                    "options": i.options,
                    "name": i.name,
                    "price_minor": i.price_minor,
                    "tags": i.tags,
                    "media": [],
                    "status": i.status,
                    "low_stock_threshold": i.low_stock_threshold,
                    "hsn_sac": i.hsn_sac,
                    "gst_rate_bp": i.gst_rate_bp,
                }
                for i in self.items.values()
            ],
        }

    def stock_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Exact integers, private network only."""
        out: dict[str, int] = {}
        for sku in payload["skus"]:
            self._item(sku)
            out[sku] = self.stock[sku]
        return {"stock": out}

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Atomic compare-and-set across every line, all or nothing.

        Checked first, applied second. Decrementing as it goes would leave a
        basket half-reserved when line three is sold out, and the caller has no
        hold to release for the two that succeeded.
        """
        order_id = payload["order_id"]
        lines = payload["lines"]

        wanted: dict[str, int] = {}
        for line in lines:
            item = self._item(line["sku"])
            wanted[item.sku] = wanted.get(item.sku, 0) + line["qty"]

        for sku, qty in wanted.items():
            available = self.stock[sku]
            if available < qty:
                raise TraitError(
                    ReasonCode.SOLD_OUT,
                    # The one place a count is named to an agent, because "try
                    # fewer" without a number is not a fix (SPEC §5). Rate-limited
                    # and counted as the oracle it is.
                    f"Only {available} left; try {available} or fewer."
                    if available
                    else "Sold out.",
                    {"sku": sku, "requested": qty, "available": available},
                )

        code = payload.get("discount_code")
        if code:
            discount = self.discounts.get(code)
            if discount is None or discount.uses >= discount.max_uses:
                raise TraitError(ReasonCode.CODE_INVALID, "That code is not valid.")
            # Held under the same key as the stock, so a single-use code cannot
            # be spent twice by two carts racing.
            self.held_codes[order_id] = code
            discount.uses += 1

        for sku, qty in wanted.items():
            self.stock[sku] -= qty
            assert self.stock[sku] >= 0, "stock went negative; the CAS above is wrong"

        self.holds[order_id] = lines
        return {"reserved": True}

    def commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = payload["order_id"]
        if order_id not in self.holds:
            raise TraitError(ReasonCode.NO_HOLD, f"no hold to commit for {order_id}")
        del self.holds[order_id]
        return {"committed": True}

    def release(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Releasing a hold that was never taken refuses `no-hold`. A `sold-out`
        failure closes no hold because none was opened — and the trait must say
        so rather than accept a release that balances nothing."""
        order_id = payload["order_id"]
        if order_id not in self.holds:
            raise TraitError(ReasonCode.NO_HOLD, f"no hold to release for {order_id}")
        for line in self.holds.pop(order_id):
            self.stock[line["sku"]] += line["qty"]
        code = self.held_codes.pop(order_id, None)
        if code:
            self.discounts[code].uses -= 1
        return {"released": True}

    def restock(self, payload: dict[str, Any]) -> dict[str, Any]:
        for line in payload["lines"]:
            self._item(line["sku"])
            self.stock[line["sku"]] += line["qty"]
        return {"restocked": True}

    def orders_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The door that produces the `order_id`, and the only response that
        ever carries the `order_salt` (§6.3a)."""
        order_id = payload.get("order_id_hint") or f"ord_{secrets.token_hex(8)}"
        salt = self.fixed_order_salt_hex or secrets.token_hex(16)
        quote = self._quote(
            payload["lines"],
            payload["destination"],
            payload.get("fulfillment_option_id"),
            None,
            advance=False,
        )
        self.orders[order_id] = FakeOrder(
            order_id=order_id,
            cart_id=payload["cart_id"],
            status=OrderStatus.PENDING,
            lines=payload["lines"],
            total_minor=quote["total_minor"],
            order_salt_hex=salt,
        )
        return {"order_id": order_id, "order_salt_hex": salt, "status": "pending"}

    def orders_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        order = self.orders.get(payload["order_id"])
        if order is None:
            raise TraitError(ReasonCode.NOT_FOUND, "no such order")
        return {
            "order_id": order.order_id,
            "status": order.status.value,
            "lines": order.lines,
            "total_minor": order.total_minor,
            "refunded_minor": order.refunded_minor,
            "tracking_number": order.tracking_number,
            "carrier": order.carrier,
            "dispatched_at": order.dispatched_at,
            "invoice_number": order.invoice_number,
            "expires_at": order.expires_at,
        }

    def orders_set_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The serialization point for tap vs expiry vs shop-reject. The Merchant
        never self-expires; the sidecar owns both clocks and calls this."""
        order = self.orders.get(payload["order_id"])
        if order is None:
            raise TraitError(ReasonCode.NOT_FOUND, "no such order")
        order.status = OrderStatus(payload["status"])
        return self.orders_read({"order_id": order.order_id})

    def quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._quote(
            payload["lines"],
            payload["destination"],
            payload.get("fulfillment_option_id"),
            payload.get("discount_code"),
            advance=True,
        )

    # ── door 9's arithmetic ──────────────────────────────────────────────────

    def _quote(
        self,
        lines: list[dict[str, Any]],
        destination: dict[str, Any],
        fulfillment_option_id: str | None,
        discount_code: str | None,
        *,
        advance: bool,
    ) -> dict[str, Any]:
        """Read-only and side-effect-free, and **carrying no clock**: an ETA is a
        day count, never a date, or midnight turns every re-quote into a spurious
        `price-changed`."""
        if destination["postal_code"].startswith(self.unserviceable_prefix):
            raise TraitError(
                ReasonCode.DESTINATION_UNSERVICEABLE,
                f"We do not deliver to {destination['postal_code']} yet.",
                {"postal_code": destination["postal_code"]},
            )

        drift = 0
        if advance:
            self._quote_calls += 1
            if self.quote_drift_paise and self._quote_calls > 1:
                drift = self.quote_drift_paise

        # 1. Fold Add-ons into their parents. An Add-on is a cart line with its
        #    own SKU and Attestation, and never a Quote Line of its own.
        parents: dict[str, dict[str, Any]] = {}
        addons: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            item = self._item(line["sku"])
            if "addon" in item.tags:
                parent = line.get("parent")
                if not parent:
                    raise TraitError(
                        ReasonCode.ADDON_WITHOUT_PARENT,
                        f"{item.sku} attaches to a line; it is never sold alone",
                        {"sku": item.sku},
                    )
                addons.setdefault(parent, []).append(line)
            else:
                parents[line["sku"]] = line

        for parent_sku in addons:
            if parent_sku not in parents:
                raise TraitError(
                    ReasonCode.ADDON_WITHOUT_PARENT,
                    f"no line {parent_sku!r} for its Add-on to attach to",
                    {"parent": parent_sku},
                )

        quote_lines: list[dict[str, Any]] = []
        for sku, line in parents.items():
            item = self._item(sku)
            unit = item.price_minor + drift
            folded = [
                {"sku": a["sku"], "amount_minor": self._item(a["sku"]).price_minor * a["qty"]}
                for a in sorted(addons.get(sku, []), key=lambda a: a["sku"])
            ]
            quote_lines.append(
                {
                    "sku": sku,
                    "qty": line["qty"],
                    "unit_price_minor": unit,
                    "line_total_minor": unit * line["qty"] + sum(a["amount_minor"] for a in folded),
                    "hsn_sac": item.hsn_sac,
                    "gst_rate_bp": item.gst_rate_bp,
                    "place_of_supply": self._place_of_supply(item, destination["state"]),
                    "addons": folded,
                }
            )
        quote_lines.sort(key=lambda ql: ql["sku"])

        subtotal = sum(ql["line_total_minor"] for ql in quote_lines)
        weights = [(ql["sku"], ql["line_total_minor"]) for ql in quote_lines]

        # 2–3. Discount, then fulfillment, both apportioned on the same basis:
        #      the line inclusive total from step 1.
        discount_lines: list[dict[str, Any]] = []
        discount_shares: dict[str, int] = dict.fromkeys((ql["sku"] for ql in quote_lines), 0)
        if discount_code:
            discount = self.discounts.get(discount_code)
            if discount is None:
                # Every wrong code refuses the same way, with no message and no
                # timing tell — otherwise door 9 answers "is this a code?" all day.
                raise TraitError(ReasonCode.CODE_INVALID, "That code is not valid.")
            discount_lines.append(
                {
                    "label": discount.label,
                    "code": discount.code,
                    "amount_minor": discount.amount_minor,
                }
            )
            discount_shares = apportion(abs(discount.amount_minor), weights)

        zone = self._zone_for(destination["state"])
        options = [
            {"id": z.id, "label": z.label, "cost_minor": z.cost_minor, "eta_days": z.eta_days}
            for z in self.zones
            if z is zone
        ]
        chosen_id = fulfillment_option_id or zone.id
        if chosen_id != zone.id:
            raise TraitError(
                ReasonCode.DESTINATION_UNSERVICEABLE,
                f"fulfillment option {chosen_id!r} does not serve {destination['state']}",
                {"available": [o["id"] for o in options]},
            )
        shipping_shares = apportion(zone.cost_minor, weights)

        # 5–6. Extract the tax from each line's post-discount, post-shipping
        #      inclusive amount, then split by Place of Supply.
        taxes: dict[tuple[str, int], int] = {}
        for ql in quote_lines:
            sku = ql["sku"]
            inclusive = ql["line_total_minor"] - discount_shares[sku] + shipping_shares[sku]
            tax = extract_tax(inclusive, ql["gst_rate_bp"])
            if ql["place_of_supply"] == self.home_state:
                cgst, sgst = split_cgst_sgst(tax)
                half = ql["gst_rate_bp"] // 2
                taxes[("CGST", half)] = taxes.get(("CGST", half), 0) + cgst
                taxes[("SGST", half)] = taxes.get(("SGST", half), 0) + sgst
            else:
                rate = ql["gst_rate_bp"]
                taxes[("IGST", rate)] = taxes.get(("IGST", rate), 0) + tax

        tax_lines = [
            {
                "kind": kind,
                "label": f"{kind} {_rate_label(rate_bp)}",
                "rate_bp": rate_bp,
                "amount_minor": amount,
                # Everything seeded is tax-inclusive, so tax is reported and
                # never added. Getting this backwards double-charges every order.
                "informational": self.tax_inclusive,
            }
            for (kind, rate_bp), amount in sorted(taxes.items())
        ]

        discount_total = sum(d["amount_minor"] for d in discount_lines)
        return {
            "currency": "INR",
            "subtotal_minor": subtotal,
            "lines": quote_lines,
            "discount_lines": discount_lines,
            "fulfillment_options": options,
            "fulfillment_chosen": {"id": zone.id, "cost_minor": zone.cost_minor},
            "tax_lines": tax_lines,
            "round_off_minor": 0,  # always 0 in v1; a non-zero value is a bug
            "total_minor": subtotal + zone.cost_minor + discount_total,
            "tax_inclusive": self.tax_inclusive,
        }


def _rate_label(rate_bp: int) -> str:
    """`1800` → `18%`, `150` → `1.5%`. Integer arithmetic: a percentage printed
    on an invoice is not a place to start using floats."""
    whole, frac = divmod(rate_bp, 100)
    return f"{whole}%" if frac == 0 else f"{whole}.{frac // 10}%"


# ── the ASGI app: real HTTP, real signatures ─────────────────────────────────

_DOOR_METHODS = {
    Door.CATALOG_READ: "catalog_read",
    Door.STOCK_READ: "stock_read",
    Door.RESERVE: "reserve",
    Door.COMMIT: "commit",
    Door.RELEASE: "release",
    Door.RESTOCK: "restock",
    Door.ORDERS_CREATE: "orders_create",
    Door.ORDERS_READ: "orders_read",
    Door.ORDERS_SET_STATUS: "orders_set_status",
    Door.QUOTE: "quote",
}

_MUTATING = {
    Door.RESERVE,
    Door.COMMIT,
    Door.RELEASE,
    Door.RESTOCK,
    Door.ORDERS_CREATE,
    Door.ORDERS_SET_STATUS,
}


def make_app(merchant: FakeMerchant, hmac_secret: str) -> FastAPI:
    """Mount the nine doors behind HMAC verification."""
    app = FastAPI(docs_url=None, redoc_url=None)
    guard = ReplayGuard()

    @app.post("/trait/{door_path:path}")
    async def door(door_path: str, request: Request) -> JSONResponse:
        try:
            which = Door(door_path)
        except ValueError:
            return JSONResponse(
                status_code=404,
                content=TraitError(ReasonCode.NOT_FOUND, f"no door {door_path!r}").to_payload(),
            )

        body = await request.body()
        try:
            verify(
                hmac_secret,
                body=body,
                path=f"/trait/{door_path}",
                signature=request.headers.get(SIGNATURE_HEADER, ""),
                timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
                nonce=request.headers.get(NONCE_HEADER, ""),
                guard=guard,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=401,
                content=TraitError(ReasonCode.SIGNATURE_INVALID, str(exc)).to_payload(),
            )

        key = request.headers.get(IDEMPOTENCY_HEADER)
        if which in _MUTATING:
            if not key:
                return JSONResponse(
                    status_code=400,
                    content=TraitError(
                        ReasonCode.SIGNATURE_INVALID,
                        f"{which.value} mutates and requires an {IDEMPOTENCY_HEADER}",
                    ).to_payload(),
                )
            cached = merchant.idempotency.get(f"{which.value}:{key}")
            if cached is not None:
                # Same key, same result — including a refusal. A retry that
                # succeeds where the first attempt refused is a second hold.
                return JSONResponse(status_code=cached["status"], content=cached["body"])

        payload = json.loads(body) if body else {}
        handler = _DOOR_METHODS.get(which)
        if handler is None:
            # An unimplemented optional door (door 10 on an older Merchant):
            # absence is a supported answer, so this is 404 rather than a
            # crash — the client's fallback turns it into the fixed shape.
            return JSONResponse(
                status_code=404,
                content=TraitError(ReasonCode.NOT_FOUND, f"no door {which.value!r}").to_payload(),
            )
        method = getattr(merchant, handler)
        try:
            result = method(payload)
            status, content = 200, result
        except TraitError as exc:
            status, content = exc.status_code, exc.to_payload()

        if which in _MUTATING and key:
            merchant.idempotency[f"{which.value}:{key}"] = {"status": status, "body": content}

        return JSONResponse(status_code=status, content=content)

    return app
