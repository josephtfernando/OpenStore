"""The basket, which lives here and not in the agent.

An agent that keeps its own basket has two baskets and shows the wrong one. The
lines, the Destination, the Contact Point and the chosen fulfillment all live in
the sidecar; a chat's cart operations are calls, not local state (SPEC §11).

**Nothing here prices anything.** A total comes from door 9 or it does not
exist — the Merchant is truth, and a basket that could add up its own lines
would eventually disagree with the shop about what a Consumer owes.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Column as Col
from sqlalchemy import DateTime, String, Table, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openstore.sidecar.core.db import session_scope
from openstore.sidecar.core.tables import metadata
from openstore.sidecar.trait.models import Destination, Line

#: One row per agent. The whole basket, written whole.
baskets = Table(
    "baskets",
    metadata,
    Col("agent_id", String(128), primary_key=True),
    Col("cart_id", String(64), nullable=False),
    Col("lines", Text, nullable=False),
    Col("destination", Text, nullable=False),
    Col("contact", Text, nullable=False),
    Col("fulfillment_option_id", String(64), nullable=False),
    Col("discount_code", String(64), nullable=True),
    # Door-10 buyer details (flat string map, merchant-declared keys). Nullable
    # so pre-A1 rows read back as empty; never part of tax/address semantics.
    Col("details", Text, nullable=True),
    Col("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass
class Basket:
    """One agent's working basket, before any checkout exists."""

    agent_id: str
    cart_id: str = field(default_factory=lambda: f"cart_{secrets.token_hex(8)}")
    lines: list[Line] = field(default_factory=list)
    destination: Destination | None = None
    contact: dict[str, str] = field(default_factory=dict)
    fulfillment_option_id: str = ""
    discount_code: str | None = None
    details: dict[str, str] = field(default_factory=dict)
    """Door-10 values, keyed by merchant-declared keys. Kept out of
    Destination/Contact on purpose: these never feed tax or address logic."""

    def add(self, sku: str, qty: int, parent: str | None = None) -> None:
        """Quantities accumulate per SKU: two `add-line` calls for one SKU are
        one line of two, not two lines the Merchant would quote separately."""
        for index, line in enumerate(self.lines):
            if line.sku == sku and line.parent == parent:
                # **Replaced, not mutated.** `Line` is frozen — it is a value in
                # the `cart_hash` preimage, and one that could be edited in place
                # is one that can change after it has been hashed. Incrementing
                # it raised a pydantic `frozen_instance` error, so a second
                # `add-line` for a SKU already in the basket answered HTTP 500
                # rather than adding anything.
                self.lines[index] = Line(sku=sku, qty=line.qty + qty, parent=parent)
                self._unchoose_fulfillment()
                return
        self.lines.append(Line(sku=sku, qty=qty, parent=parent))
        self._unchoose_fulfillment()

    def remove(self, sku: str) -> bool:
        before = len(self.lines)
        # Add-ons hang off a parent line, so removing the parent removes them —
        # leaving an orphaned gift-wrap on a basket with nothing to wrap is how
        # a Consumer is charged for something they cannot receive.
        self.lines = [ln for ln in self.lines if ln.sku != sku and ln.parent != sku]
        if len(self.lines) != before:
            self._unchoose_fulfillment()
            return True
        return False

    def _unchoose_fulfillment(self) -> None:
        """A changed basket un-chooses the delivery option, exactly as a changed
        Destination does.

        The option was offered and priced for the lines that were in the basket
        when the shop offered it. Keeping it across an `add-line` meant a
        fulfillment could be chosen before the basket was finished and still
        reach the quote — so the Consumer approved delivery for a cart that no
        longer existed. Cleared here rather than in the tools, so no caller can
        forget."""
        self.fulfillment_option_id = ""

    def quotable(self) -> bool:
        return bool(self.lines and self.destination and self.fulfillment_option_id)


@dataclass
class BasketStore:
    """Baskets in the sidecar's own database, one row per agent.

    **Durable since 09-21.** A dict here meant a deploy in the middle of a
    conversation dropped the Consumer's basket without telling either of them:
    the agent's next call started an empty cart and the shop's side of the
    conversation had simply forgotten. Nothing here is money, but a basket is
    what the Consumer has spent their attention on.

    Read and written whole. A basket is small, it is touched once per tool call,
    and the alternative — a line table the agent edits row by row — buys
    nothing and invents a second place the cart can disagree with itself.
    """

    sessionmaker: async_sessionmaker[AsyncSession] | None = None

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        if self.sessionmaker is None:
            raise RuntimeError(
                "This BasketStore has no database, so a basket cannot be kept between "
                "tool calls. Set SIDECAR_DATABASE_URL."
            )
        return self.sessionmaker

    async def for_agent(self, agent_id: str) -> Basket:
        """This agent's basket, or a fresh one. Never another agent's: the row
        is keyed by the admitted `agent_id` and nothing else is consulted."""
        async with session_scope(self._maker()) as session:
            row = (
                await session.execute(select(baskets).where(baskets.c.agent_id == agent_id))
            ).first()
        if row is None:
            return Basket(agent_id=agent_id)
        destination = json.loads(row.destination) if row.destination else None
        details_raw = getattr(row, "details", None)
        return Basket(
            agent_id=row.agent_id,
            cart_id=row.cart_id,
            lines=[Line(**line) for line in json.loads(row.lines)],
            destination=Destination(**destination) if destination else None,
            contact=json.loads(row.contact),
            fulfillment_option_id=row.fulfillment_option_id,
            discount_code=row.discount_code,
            details=dict(json.loads(details_raw)) if details_raw else {},
        )

    async def save(self, basket: Basket) -> None:
        """Write this agent's basket back, replacing what was there."""
        values = {
            "agent_id": basket.agent_id,
            "cart_id": basket.cart_id,
            "lines": json.dumps([line.model_dump(mode="json") for line in basket.lines]),
            "destination": (
                json.dumps(basket.destination.model_dump(mode="json")) if basket.destination else ""
            ),
            "contact": json.dumps(basket.contact),
            "fulfillment_option_id": basket.fulfillment_option_id,
            "discount_code": basket.discount_code,
            "details": json.dumps(basket.details),
            "updated_at": datetime.now(UTC),
        }
        async with session_scope(self._maker()) as session:
            existing = (
                await session.execute(
                    select(baskets.c.agent_id).where(baskets.c.agent_id == basket.agent_id)
                )
            ).first()
            if existing is None:
                await session.execute(baskets.insert().values(**values))
            else:
                await session.execute(
                    baskets.update().where(baskets.c.agent_id == basket.agent_id).values(**values)
                )

    async def clear(self, agent_id: str) -> None:
        async with session_scope(self._maker()) as session:
            await session.execute(baskets.delete().where(baskets.c.agent_id == agent_id))
