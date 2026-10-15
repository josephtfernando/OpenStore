"""The trait client. HTTP only, HMAC-signed, idempotent where it mutates.

There is no other way for the sidecar to reach Merchant truth: no shared
database, no shared module, no import. The Merchant owns Product Groups,
Catalogue Items, stock and order rows (ADR-0001), and this file is the whole of
how the sidecar asks.

Idempotency keys are `order_id:attempt` on every mutating door **except**
`orders.create`, which is the door that produces the `order_id` and therefore
keys on `cart_id:attempt` — the Pending Cart being the thing an order is made
from. A crash between request and response is exactly where duplicate orders
are born.

**Async, because the sidecar is.** A synchronous HTTP call inside a FastAPI
request handler blocks the whole event loop for the length of the Merchant's
round trip, and the Gate makes several per decision — `quote`, `reserve`,
`orders.set-status`. Under any concurrency that is the sidecar serialising
itself behind the slowest Merchant response.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from openstore.sidecar.core.codes import Door, ReasonCode
from openstore.sidecar.trait.errors import TraitError, TraitProtocolError
from openstore.sidecar.trait.models import (
    Catalog,
    Destination,
    Line,
    Order,
    OrderCreated,
    Quote,
    Requirements,
)
from openstore.sidecar.trait.signing import (
    IDEMPOTENCY_HEADER,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    new_nonce,
    sign,
)

#: Doors that change something and therefore must carry an idempotency key.
MUTATING_DOORS = frozenset(
    {
        Door.RESERVE,
        Door.COMMIT,
        Door.RELEASE,
        Door.RESTOCK,
        Door.ORDERS_CREATE,
        Door.ORDERS_SET_STATUS,
    }
)


def idempotency_key(order_id: str, attempt: int) -> str:
    return f"{order_id}:{attempt}"


def cart_idempotency_key(cart_id: str, attempt: int) -> str:
    """`orders.create` only. It produces the `order_id`, so it cannot key on one."""
    return f"{cart_id}:{attempt}"


class TraitClient:
    """A Merchant, as the sidecar sees it: nine required doors plus the
    optional `requirements.read` (door 10), and nothing else."""

    def __init__(
        self,
        base_url: str,
        hmac_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: int = 10,  # seconds; an int keeps 'no float in the sidecar' absolute
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = hmac_secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TraitClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ── transport ────────────────────────────────────────────────────────────

    async def _call(
        self,
        door: Door,
        payload: dict[str, Any],
        *,
        idempotency: str | None = None,
    ) -> Any:
        if door in MUTATING_DOORS and not idempotency:
            raise TraitProtocolError(
                f"{door.value} mutates and was called with no idempotency key. A retry "
                f"without one is how a hold is taken twice."
            )

        path = f"/trait/{door.value}"
        # Canonical bytes, signed as sent. Re-serializing before signing would
        # sign a different document than the one on the wire.
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        timestamp = int(time.time())
        nonce = new_nonce()

        headers = {
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: str(timestamp),
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: sign(
                self._secret, body=body, timestamp=timestamp, nonce=nonce, path=path
            ),
        }
        if idempotency:
            headers[IDEMPOTENCY_HEADER] = idempotency

        response = await self._client.post(f"{self._base_url}{path}", content=body, headers=headers)

        try:
            parsed = response.json()
        except ValueError:
            raise TraitProtocolError(
                f"{door.value} answered HTTP {response.status_code} with a body that is not "
                f"JSON: {response.text[:200]!r}"
            ) from None

        if response.status_code >= 400:
            raise TraitError.from_payload(parsed, response.status_code)

        return parsed

    # ── the nine required doors + the optional tenth ─────────────────────────

    async def catalog_read(self, since: str | None = None) -> Catalog:
        """Door 1."""
        payload: dict[str, Any] = {"since": since} if since else {}
        return Catalog.model_validate(await self._call(Door.CATALOG_READ, payload))

    async def stock_read(self, skus: list[str]) -> dict[str, int]:
        """Door 2 — **exact integers, private network only**.

        The caller is responsible for bucketing before any of this reaches an
        agent (`trait.buckets`). Returning the dict raw is deliberate: the Gate
        needs the number, and pretending otherwise here would push the count
        somewhere less visible.
        """
        result = await self._call(Door.STOCK_READ, {"skus": skus})
        stock = result["stock"]
        for sku, count in stock.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise TraitProtocolError(
                    f"stock for {sku!r} is {count!r}; stock is int >= 0, never null, never "
                    f"a float, and a Merchant sending otherwise must fail loud"
                )
        return dict(stock)

    async def reserve(
        self,
        order_id: str,
        lines: list[Line],
        *,
        attempt: int = 1,
        discount_code: str | None = None,
    ) -> None:
        """Door 3. Atomic compare-and-set, never negative-proof by test. Also
        holds the Discount Code under the same key, so a single-use code cannot
        be spent twice."""
        payload: dict[str, Any] = {
            "order_id": order_id,
            "lines": [ln.model_dump(exclude_none=True) for ln in lines],
        }
        if discount_code:
            payload["discount_code"] = discount_code
        result = await self._call(
            Door.RESERVE, payload, idempotency=idempotency_key(order_id, attempt)
        )
        if result.get("reserved") is not True:
            raise TraitProtocolError(f"reserve answered {result!r} rather than reserved:true")

    async def commit(self, order_id: str, *, attempt: int = 1) -> None:
        """Door 4."""
        result = await self._call(
            Door.COMMIT, {"order_id": order_id}, idempotency=idempotency_key(order_id, attempt)
        )
        if result.get("committed") is not True:
            raise TraitProtocolError(f"commit answered {result!r} rather than committed:true")

    async def release(self, order_id: str, *, attempt: int = 1) -> None:
        """Door 5. Releasing a hold that was never taken refuses `no-hold` — a
        `sold-out` failure closes no hold, because none was ever opened."""
        result = await self._call(
            Door.RELEASE, {"order_id": order_id}, idempotency=idempotency_key(order_id, attempt)
        )
        if result.get("released") is not True:
            raise TraitProtocolError(f"release answered {result!r} rather than released:true")

    async def restock(self, order_id: str, lines: list[Line], *, attempt: int = 1) -> None:
        """Door 6."""
        result = await self._call(
            Door.RESTOCK,
            {
                "order_id": order_id,
                "lines": [ln.model_dump(exclude_none=True) for ln in lines],
            },
            idempotency=idempotency_key(order_id, attempt),
        )
        if result.get("restocked") is not True:
            raise TraitProtocolError(f"restock answered {result!r} rather than restocked:true")

    async def orders_create(
        self,
        cart_id: str,
        lines: list[Line],
        destination: Destination,
        contact: dict[str, str],
        fulfillment_option_id: str,
        *,
        attempt: int = 1,
        agent_id: str | None = None,
        consumer_id: str | None = None,
        order_id_hint: str | None = None,
        details: dict[str, str] | None = None,
    ) -> OrderCreated:
        """Door 7 (create). Keys on `cart_id:attempt`, and returns the
        `order_salt` — the only response that ever does (§6.3a)."""
        payload: dict[str, Any] = {
            "cart_id": cart_id,
            "lines": [ln.model_dump(exclude_none=True) for ln in lines],
            "destination": destination.model_dump(),
            "contact": contact,
            "fulfillment_option_id": fulfillment_option_id,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        if consumer_id:
            payload["consumer_id"] = consumer_id
        if details:
            # Only when non-empty: an always-present key would trip Merchants
            # whose door-7 schema predates details.
            payload["details"] = details
        if order_id_hint:
            # Tests and golden replays only: lets a replay name its own order so
            # the Transcript bytes are comparable. A Merchant is free to ignore
            # it, and the real one does.
            payload["order_id_hint"] = order_id_hint
        return OrderCreated.model_validate(
            await self._call(
                Door.ORDERS_CREATE, payload, idempotency=cart_idempotency_key(cart_id, attempt)
            )
        )

    async def orders_read(self, order_id: str) -> Order:
        """Door 7 (read). The salt is never here."""
        return Order.model_validate(await self._call(Door.ORDERS_READ, {"order_id": order_id}))

    async def orders_set_status(
        self, order_id: str, status: str, reason: str = "", *, attempt: int = 1
    ) -> Order:
        """Door 8 — the serialization point for tap vs expiry vs shop-reject.
        Same key, same result."""
        payload: dict[str, Any] = {"order_id": order_id, "status": status}
        if reason:
            payload["reason"] = reason
        return Order.model_validate(
            await self._call(
                Door.ORDERS_SET_STATUS, payload, idempotency=idempotency_key(order_id, attempt)
            )
        )

    async def quote(
        self,
        lines: list[Line],
        destination: Destination,
        *,
        fulfillment_option_id: str | None = None,
        discount_code: str | None = None,
        details: dict[str, str] | None = None,
    ) -> tuple[Quote, bytes]:
        """Door 9. Read-only, side-effect-free, byte-identical for identical
        inputs against unchanged state.

        Returns the parsed Quote **and the raw bytes**, because `quote-fresh`
        byte-compares against what was pinned. Comparing re-serialized models
        would compare our encoder against itself and pass a Merchant whose bytes
        moved.
        """
        payload: dict[str, Any] = {
            "lines": [ln.model_dump(exclude_none=True) for ln in lines],
            "destination": destination.model_dump(),
        }
        if fulfillment_option_id:
            payload["fulfillment_option_id"] = fulfillment_option_id
        if discount_code:
            payload["discount_code"] = discount_code
        if details:
            # Only when non-empty: an always-present key would trip Merchants
            # whose door-9 schema predates details.
            payload["details"] = details

        raw = await self._call(Door.QUOTE, payload)
        quote = Quote.model_validate(raw)
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return quote, canonical

    async def requirements_read(self) -> Requirements | None:
        """Door 10 (optional). `None` means the Merchant does not implement it
        and the sidecar serves the fixed buyer shape — absence is a supported
        answer here, not a failure, so `not-found` from this door falls back
        instead of refusing."""
        try:
            raw = await self._call(Door.REQUIREMENTS_READ, {})
        except TraitError as exc:
            if exc.code is ReasonCode.NOT_FOUND:
                return None
            raise
        return Requirements.model_validate(raw)


__all__ = [
    "MUTATING_DOORS",
    "ReasonCode",
    "TraitClient",
    "cart_idempotency_key",
    "idempotency_key",
]
