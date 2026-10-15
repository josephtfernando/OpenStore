"""The checks, and what each one is actually claiming.

**Nothing here hardcodes a SKU.** The suite reads door 1 and picks its own
subjects: the first item with stock to spare, a Product Group to offer where a
SKU belongs, an Add-on if the store has one. A fixed SKU would pass against the
store it was written for and fail against every other, which is the opposite of
a conformance suite.

**The store is written to, and says so.** Orders are created, stock is held and
released. Every check cleans up after itself — a hold this suite takes is a hold
it gives back, because a conformance run that quietly consumed a shop's
inventory would be the last one anybody let near a real store. `--read-only`
runs the subset that writes nothing, which is the right first pass against
production.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from openstore.sidecar.core.codes import OrderStatus, ReasonCode
from openstore.sidecar.trait.client import TraitClient
from openstore.sidecar.trait.errors import TraitError
from openstore.sidecar.trait.models import Catalog, Destination, Line

#: A Destination every Indian store should be able to price. Two of them,
#: because one basket shipped to two states is how CGST/SGST is told from IGST.
HOME_ISH = Destination(
    line1="4th Cross, Indiranagar", city="Bengaluru", state="KA", postal_code="560038"
)
AWAY = Destination(line1="Dadar West", city="Mumbai", state="MH", postal_code="400028")

CONTACT = {"email": "conformance@openstore.test", "phone": "+919000000001"}


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    #: The store has nothing to check this against — no Add-on, no second
    #: shipping zone. **Not a failure**: a shop without Add-ons is a shop, and
    #: reporting it as a failure would teach implementers to ignore the report.
    SKIP = "skip"


@dataclass
class Check:
    door: str
    name: str
    outcome: Outcome
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    #: Something went wrong that stopped the run rather than failing one check —
    #: an unreachable store, a secret that verifies nothing, a catalogue with
    #: nothing sellable in it.
    fatal: str = ""

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def exit_code(self) -> int:
        """`0` conforms, `1` does not, `2` could not be run.

        Three codes because a script needs to tell "this store is wrong" from "I
        could not reach it" without parsing prose.
        """
        if self.fatal:
            return 2
        return 1 if self.failures else 0

    def render(self, *, verbose: bool = False) -> str:
        lines: list[str] = []
        if self.fatal:
            lines.append(f"COULD NOT RUN: {self.fatal}")
            return "\n".join(lines)

        door = ""
        for check in self.checks:
            if check.door != door:
                door = check.door
                lines.append(f"\n  {door}")
            mark = {Outcome.PASS: "ok  ", Outcome.FAIL: "FAIL", Outcome.SKIP: "skip"}[check.outcome]
            lines.append(f"    {mark}  {check.name}")
            if check.detail and (verbose or check.failed):
                lines.append(f"          {check.detail}")

        passed = sum(1 for c in self.checks if c.outcome is Outcome.PASS)
        skipped = sum(1 for c in self.checks if c.outcome is Outcome.SKIP)
        lines.append("")
        lines.append(
            f"  {passed} passed, {len(self.failures)} failed, {skipped} skipped"
            if self.failures or skipped
            else f"  {passed} passed"
        )
        if self.failures:
            lines.append("")
            lines.append("  This store does not implement the trait yet. Each FAIL above")
            lines.append("  names the door and the property; SPECS/PLAN.md §6.1 is the contract.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conforms": self.exit_code == 0,
            "fatal": self.fatal or None,
            "checks": [
                {"door": c.door, "name": c.name, "outcome": c.outcome.value, "detail": c.detail}
                for c in self.checks
            ],
        }


@dataclass
class _Subjects:
    """What this particular store gives the suite to work with."""

    catalog: Catalog
    sellable: str
    """A SKU with stock to spare. Everything that holds inventory uses this."""
    stock: int
    group_id: str = ""
    addon: str = ""
    addon_parent: str = ""


class _Runner:
    def __init__(self, client: TraitClient, *, read_only: bool) -> None:
        self._client = client
        self._read_only = read_only
        self.report = Report()

    # ── recording ────────────────────────────────────────────────────────────

    async def check(self, door: str, name: str, body: Callable[[], Awaitable[str]]) -> None:
        """Run one check, and let it fail without taking the run with it.

        A store that refuses one door is exactly the store this is for; the
        other nine still need reporting.
        """
        try:
            detail = await body()
            self.report.checks.append(Check(door, name, Outcome.PASS, detail))
        except _Skipped as skip:
            self.report.checks.append(Check(door, name, Outcome.SKIP, str(skip)))
        except AssertionError as failure:
            self.report.checks.append(Check(door, name, Outcome.FAIL, str(failure)))
        except TraitError as refusal:
            self.report.checks.append(
                Check(door, name, Outcome.FAIL, f"refused {refusal.code.value}: {refusal.detail}")
            )
        except Exception as error:  # noqa: BLE001 - a broken door is a failed check
            self.report.checks.append(
                Check(door, name, Outcome.FAIL, f"{type(error).__name__}: {error}")
            )


class _Skipped(Exception):
    """This store has nothing to check this property against."""


def _order_id(suffix: str) -> str:
    """Unique per run. A live store keeps its idempotency cache between runs,
    and a reused id would replay a previous run's answer instead of doing
    anything."""
    return f"ord_conform_{suffix}_{secrets.token_hex(4)}"


async def _subjects(client: TraitClient) -> _Subjects:
    """Read the catalogue and pick what to test with."""
    catalog = await client.catalog_read()
    if not catalog.items:
        raise _Fatal("door 1 returned no Catalogue Items, so there is nothing to check against")

    skus = [item.sku for item in catalog.items]
    stock = await client.stock_read(skus)

    sellable = next((sku for sku in skus if stock.get(sku, 0) >= 2), "")
    if not sellable:
        raise _Fatal(
            "no Catalogue Item has 2 or more in stock, so the hold checks cannot run "
            "without risking the shop's last unit"
        )

    addon = next((item.sku for item in catalog.items if "addon" in item.tags), "")
    addon_parent = next(
        (item.sku for item in catalog.items if "addon" not in item.tags and item.sku != addon), ""
    )

    return _Subjects(
        catalog=catalog,
        sellable=sellable,
        stock=stock[sellable],
        group_id=catalog.groups[0].id if catalog.groups else "",
        addon=addon,
        addon_parent=addon_parent,
    )


class _Fatal(Exception):
    """The run cannot proceed at all."""


async def run(base_url: str, secret: str, *, read_only: bool = False) -> Report:
    """Every check, against one store."""
    async with TraitClient(base_url, secret) as client:
        runner = _Runner(client, read_only=read_only)
        try:
            subjects = await _subjects(client)
        except _Fatal as fatal:
            runner.report.fatal = str(fatal)
            return runner.report
        except TraitError as refusal:
            runner.report.fatal = (
                f"door 1 refused {refusal.code.value}: {refusal.detail}. "
                f"If this is `signature-invalid`, the secret does not match the store's."
            )
            return runner.report
        except Exception as error:  # noqa: BLE001 - the address or the store is wrong
            runner.report.fatal = f"could not read the catalogue: {type(error).__name__}: {error}"
            return runner.report

        await _catalogue_checks(runner, client, subjects)
        await _quote_checks(runner, client, subjects)
        await _requirements_checks(runner, client)
        if not read_only:
            await _order_checks(runner, client, subjects)
            await _hold_checks(runner, client, subjects)
            await _idempotency_checks(runner, client, subjects)
            await _concurrency_check(runner, client, subjects)
        return runner.report


# ── doors 1 and 2 ────────────────────────────────────────────────────────────


async def _catalogue_checks(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    async def every_item_belongs_to_a_group() -> str:
        group_ids = {g.id for g in s.catalog.groups}
        orphans = [i.sku for i in s.catalog.items if i.group_id not in group_ids]
        assert not orphans, f"items whose group_id names no group: {orphans[:5]}"
        return f"{len(s.catalog.items)} items in {len(s.catalog.groups)} groups"

    async def no_group_id_is_also_a_sku() -> str:
        clash = {g.id for g in s.catalog.groups} & {i.sku for i in s.catalog.items}
        assert not clash, (
            f"{sorted(clash)[:3]} are both a Product Group and a Catalogue Item. "
            f"A Group is never sellable, so the two namespaces must not overlap."
        )
        return "the two namespaces do not overlap"

    async def stock_is_exact_integers() -> str:
        stock = await client.stock_read([i.sku for i in s.catalog.items][:25])
        bad = {k: v for k, v in stock.items() if not isinstance(v, int) or v < 0}
        assert not bad, f"stock must be int >= 0, never null: {bad}"
        return f"{len(stock)} counts, all int >= 0"

    async def an_unknown_sku_is_not_found() -> str:
        try:
            await client.stock_read([f"NO-SUCH-SKU-{secrets.token_hex(4)}"])
        except TraitError as refusal:
            assert (
                refusal.code is ReasonCode.NOT_FOUND
            ), f"an unknown SKU must refuse `not-found`, not `{refusal.code.value}`"
            return "refuses `not-found`"
        raise AssertionError("an unknown SKU was answered rather than refused")

    await runner.check(
        "door 1 — catalog.read", "every item belongs to a group", every_item_belongs_to_a_group
    )
    await runner.check(
        "door 1 — catalog.read", "no group id is also a SKU", no_group_id_is_also_a_sku
    )
    await runner.check(
        "door 2 — stock.read", "stock is exact integers, never null", stock_is_exact_integers
    )
    await runner.check(
        "door 2 — stock.read", "an unknown SKU refuses not-found", an_unknown_sku_is_not_found
    )


# ── door 9 ───────────────────────────────────────────────────────────────────


async def _quote_checks(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    lines = [Line(sku=s.sellable, qty=1)]

    async def quote_adds_up() -> str:
        quote, _ = await client.quote(lines, AWAY)
        subtotal = sum(line.line_total_minor for line in quote.lines)
        discounts = sum(d.amount_minor for d in quote.discount_lines)
        shipping = quote.fulfillment_chosen.cost_minor

        assert (
            quote.subtotal_minor == subtotal
        ), f"subtotal_minor is {quote.subtotal_minor}, but the lines sum to {subtotal}"
        assert quote.total_minor == subtotal + shipping + discounts, (
            f"total_minor is {quote.total_minor}, but subtotal + fulfillment + discounts "
            f"is {subtotal + shipping + discounts} (§16.11 step 8)"
        )
        assert quote.round_off_minor == 0, "round_off_minor is 0 in v1 (§16.11 step 7)"
        return f"total {quote.total_minor} = {subtotal} + {shipping} + {discounts}"

    async def quote_is_byte_identical_when_nothing_changed() -> str:
        _, first = await client.quote(lines, AWAY)
        for _ in range(3):
            _, again = await client.quote(lines, AWAY)
            assert again == first, (
                "two quotes for one basket differ byte for byte. Door 9 is read-only and "
                "side-effect-free; something in it is carrying a clock or a counter."
            )
        return "four calls, identical bytes"

    async def quote_carries_no_clock() -> str:
        """A date anywhere in a Quote turns every re-quote after midnight into a
        spurious `price-changed`, which is a Consumer being told the price moved
        when it did not."""
        quote, raw = await client.quote(lines, AWAY)
        body = raw.decode("utf-8")

        stamps = re.findall(r"\d{4}-\d{2}-\d{2}", body)
        assert not stamps, (
            f"the Quote carries a date ({stamps[0]}). It owns no validity of its own: "
            f"the sidecar holds both expiry clocks, and an ETA is a day count."
        )
        for option in quote.fulfillment_options:
            assert isinstance(
                option.eta_days, int
            ), f"fulfillment option {option.id} has a non-integer eta_days"
        return f"{len(quote.fulfillment_options)} option(s), all day counts"

    async def tax_splits_by_place_of_supply() -> str:
        quote, _ = await client.quote(lines, AWAY)
        kinds = {t.kind for t in quote.tax_lines}
        if not kinds:
            raise _Skipped("this store quotes no tax lines")
        assert not (
            {"IGST"} & kinds and {"CGST", "SGST"} & kinds and len(quote.lines) == 1
        ), "one line cannot be both inter-state and intra-state"
        return f"{sorted(kinds)}"

    async def a_group_id_where_a_sku_belongs_refuses() -> str:
        if not s.group_id:
            raise _Skipped("this store publishes no Product Groups")
        try:
            await client.quote([Line(sku=s.group_id, qty=1)], AWAY)
        except TraitError as refusal:
            assert refusal.code is ReasonCode.VARIANT_REQUIRED, (
                f"a Product Group offered where a SKU belongs must refuse "
                f"`variant-required`, not `{refusal.code.value}` — the alternative is "
                f"picking a size on the Consumer's behalf"
            )
            return "refuses `variant-required`"
        raise AssertionError("a Product Group was priced as if it were sellable")

    async def an_orphan_addon_refuses() -> str:
        if not s.addon:
            raise _Skipped("this store has no Add-on items")
        try:
            await client.quote([Line(sku=s.addon, qty=1)], AWAY)
        except TraitError as refusal:
            assert refusal.code is ReasonCode.ADDON_WITHOUT_PARENT, (
                f"an Add-on with no parent must refuse `addon-without-parent`, not "
                f"`{refusal.code.value}`"
            )
            return "refuses `addon-without-parent`"
        raise AssertionError("an Add-on was sold on its own")

    await runner.check("door 9 — quote", "the quote adds up (§16.11 step 8)", quote_adds_up)
    await runner.check(
        "door 9 — quote",
        "identical inputs give identical bytes",
        quote_is_byte_identical_when_nothing_changed,
    )
    await runner.check(
        "door 9 — quote", "an ETA is a day count, never a date", quote_carries_no_clock
    )
    await runner.check(
        "door 9 — quote", "tax splits by Place of Supply", tax_splits_by_place_of_supply
    )
    await runner.check(
        "door 9 — quote",
        "a Product Group refuses variant-required",
        a_group_id_where_a_sku_belongs_refuses,
    )
    await runner.check("door 9 — quote", "an orphan Add-on is refused", an_orphan_addon_refuses)


# ── door 10 (optional) ───────────────────────────────────────────────────────


async def _requirements_checks(runner: _Runner, client: TraitClient) -> None:
    """Absence passes: a Merchant without door 10 serves the fixed shape.
    Presence must be a valid declaration the sidecar can publish verbatim."""

    async def declares_a_valid_shape_or_nothing() -> str:
        declared = await client.requirements_read()
        if declared is None:
            raise _Skipped("no door 10 — the fixed buyer shape applies")
        from openstore.sidecar.trait.models import MAX_REQUIREMENT_FIELDS

        assert len(declared.fields) <= MAX_REQUIREMENT_FIELDS
        assert len({f.key for f in declared.fields}) == len(declared.fields), (
            "requirement keys must be unique"
        )
        pii = [f.key for f in declared.fields if f.pii]
        assert not pii, (
            f"declares pii:true ({', '.join(pii)}): the sidecar refuses such "
            f"declarations until generic receipt commitments land"
        )
        return f"{len(declared.fields)} declared extras"

    await runner.check(
        "door 10 — requirements.read",
        "declares a valid shape, or nothing at all",
        declares_a_valid_shape_or_nothing,
    )


# ── door 7 ───────────────────────────────────────────────────────────────────


async def _order_checks(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    lines = [Line(sku=s.sellable, qty=1)]

    async def create_returns_a_salt_once() -> str:
        created = await client.orders_create(
            f"cart_{secrets.token_hex(4)}", lines, AWAY, CONTACT, "", agent_id="conformance"
        )
        assert len(created.order_salt_hex) == 32, (
            f"order_salt_hex must be 32 hex characters (128 bits), got "
            f"{len(created.order_salt_hex)}"
        )
        assert (
            created.status is OrderStatus.PENDING
        ), f"a new order is `pending`, not `{created.status.value}` — no stock is held yet"

        read = await client.orders_read(created.order_id)
        dumped = read.model_dump_json()
        assert created.order_salt_hex not in dumped, (
            "the order salt came back from orders.read. It is returned by "
            "orders.create and in no other response ever (§6.3a) — erasure stops "
            "meaning anything if a second door hands it out."
        )
        return f"{created.order_id}, salt returned once"

    async def two_orders_have_two_salts() -> str:
        first = await client.orders_create(
            f"cart_{secrets.token_hex(4)}", lines, AWAY, CONTACT, "", agent_id="conformance"
        )
        second = await client.orders_create(
            f"cart_{secrets.token_hex(4)}", lines, AWAY, CONTACT, "", agent_id="conformance"
        )
        assert first.order_salt_hex != second.order_salt_hex, (
            "two orders share one salt. The salt is what makes a PII commitment "
            "unopenable; a shared one opens both."
        )
        assert first.order_id != second.order_id, "two orders share one id"
        return "distinct ids, distinct salts"

    async def status_round_trips() -> str:
        created = await client.orders_create(
            f"cart_{secrets.token_hex(4)}", lines, AWAY, CONTACT, "", agent_id="conformance"
        )
        moved = await client.orders_set_status(
            created.order_id, OrderStatus.CANCELLED.value, "conformance", attempt=3
        )
        assert moved.status is OrderStatus.CANCELLED, (
            f"set-status said `{moved.status.value}` after being asked for `cancelled`. "
            f"The Merchant is truth, so a transition it did not take must not be reported "
            f"as one it did."
        )
        read_back = await client.orders_read(created.order_id)
        assert read_back.status is OrderStatus.CANCELLED, (
            f"orders.read says `{read_back.status.value}` for an order set-status just "
            f"moved to `cancelled`"
        )
        return "set-status and read agree"

    await runner.check(
        "door 7 — orders.create", "the salt is 128 bits, returned once", create_returns_a_salt_once
    )
    await runner.check("door 7 — orders.create", "two orders, two salts", two_orders_have_two_salts)
    await runner.check("door 8 — orders.set-status", "a transition round-trips", status_round_trips)


# ── doors 3, 4, 5 ────────────────────────────────────────────────────────────


async def _hold_checks(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    async def reserve_then_release_returns_the_stock() -> str:
        before = (await client.stock_read([s.sellable]))[s.sellable]
        order_id = _order_id("hold")
        await client.reserve(order_id, [Line(sku=s.sellable, qty=1)])
        held = (await client.stock_read([s.sellable]))[s.sellable]
        assert held == before - 1, (
            f"reserve of 1 moved stock from {before} to {held}; it must move by exactly "
            f"the quantity held"
        )
        await client.release(order_id)
        after = (await client.stock_read([s.sellable]))[s.sellable]
        assert after == before, (
            f"release left stock at {after}, not the {before} it started from. A hold "
            f"that does not come back is inventory nobody can sell."
        )
        return f"{before} → {held} → {after}"

    async def releasing_nothing_refuses_no_hold() -> str:
        try:
            await client.release(_order_id("never"))
        except TraitError as refusal:
            assert refusal.code is ReasonCode.NO_HOLD, (
                f"releasing a hold that was never taken must refuse `no-hold`, not "
                f"`{refusal.code.value}` — a sold-out failure closes no hold, and "
                f"accepting a release for it would balance an entry against nothing"
            )
            return "refuses `no-hold`"
        raise AssertionError("a hold that was never taken was released anyway")

    async def over_reserving_refuses_sold_out() -> str:
        available = (await client.stock_read([s.sellable]))[s.sellable]
        order_id = _order_id("oversell")
        try:
            await client.reserve(order_id, [Line(sku=s.sellable, qty=available + 5)])
        except TraitError as refusal:
            assert refusal.code is ReasonCode.SOLD_OUT, (
                f"reserving more than exists must refuse `sold-out`, not " f"`{refusal.code.value}`"
            )
            left = (await client.stock_read([s.sellable]))[s.sellable]
            assert left == available, (
                f"a refused reserve still moved stock, from {available} to {left}. "
                f"The compare-and-set must be all or nothing."
            )
            return "refuses `sold-out`, and moves nothing"
        # Clean up a hold that should never have been granted.
        await client.release(order_id)
        raise AssertionError(f"reserved {available + 5} of an item with {available} in stock")

    await runner.check(
        "doors 3+5 — reserve/release",
        "a hold moves stock by exactly its quantity",
        reserve_then_release_returns_the_stock,
    )
    await runner.check(
        "door 5 — release", "releasing nothing refuses no-hold", releasing_nothing_refuses_no_hold
    )
    await runner.check(
        "door 3 — reserve",
        "over-reserving refuses sold-out and moves nothing",
        over_reserving_refuses_sold_out,
    )


# ── idempotency ──────────────────────────────────────────────────────────────


async def _idempotency_checks(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    async def a_repeated_key_moves_stock_once() -> str:
        order_id = _order_id("idem")
        before = (await client.stock_read([s.sellable]))[s.sellable]
        await client.reserve(order_id, [Line(sku=s.sellable, qty=1)])
        await client.reserve(order_id, [Line(sku=s.sellable, qty=1)])
        held = (await client.stock_read([s.sellable]))[s.sellable]
        await client.release(order_id)
        assert held == before - 1, (
            f"two reserves under one idempotency key moved stock from {before} to {held}. "
            f"A retry is how a mutation survives a dropped connection; taking a second "
            f"hold for it is an oversell with extra steps."
        )
        return f"two calls, one hold ({before} → {held})"

    await runner.check(
        "idempotency", "a repeated key holds stock once", a_repeated_key_moves_stock_once
    )


# ── concurrency ──────────────────────────────────────────────────────────────


async def _concurrency_check(runner: _Runner, client: TraitClient, s: _Subjects) -> None:
    async def concurrent_reserves_never_oversell() -> str:
        """The check a single-threaded implementation passes by accident.

        `WHERE available >= qty` inside one transaction is the whole mechanism.
        An implementation that reads the count and then decides passes every
        sequential test in this file and oversells the moment two agents arrive
        together.
        """
        available = (await client.stock_read([s.sellable]))[s.sellable]
        contenders = min(available + 4, 24)
        order_ids = [_order_id(f"race{n}") for n in range(contenders)]

        async def attempt(order_id: str) -> bool:
            try:
                await client.reserve(order_id, [Line(sku=s.sellable, qty=1)])
                return True
            except TraitError:
                return False

        results = await asyncio.gather(*(attempt(o) for o in order_ids), return_exceptions=True)
        granted = [order_ids[i] for i, ok in enumerate(results) if ok is True]
        left = (await client.stock_read([s.sellable]))[s.sellable]

        # Give everything back before asserting, so a failure does not also cost
        # the shop its stock.
        for order_id in granted:
            try:
                await client.release(order_id)
            except TraitError:
                pass

        assert len(granted) <= available, (
            f"{len(granted)} of {contenders} concurrent reserves were granted against "
            f"{available} in stock. The reserve must be an atomic compare-and-set."
        )
        assert left >= 0, f"concurrent reserves drove stock to {left}"
        return f"{contenders} at once against {available}: {len(granted)} granted"

    await runner.check(
        "concurrency", "concurrent reserves never oversell", concurrent_reserves_never_oversell
    )
