"""`/agentic` routes, and the auth boundary that is part of the contract.

**Two paths inside this prefix are public on purpose** (A8's table):

- `/agentic/approve` is authenticated by the **one-time tap token**, not by the
  Merchant session. A Consumer approving a spend is not the Merchant and must
  never need the Merchant's login.
- `/receipt/<id>` (mounted in `app.py`) is public entirely.

That is exactly the kind of thing a proxy config gets wrong once and then serves
wrong forever, so it is asserted by tests here rather than left to the
`Caddyfile`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

from openstore.sidecar.console.refunds import get_refund_queue
from openstore.sidecar.console.render import TABS, ConsoleState, page
from openstore.sidecar.core.codes import (
    AuthorityKind,
    IntentMechanism,
    PaymentMethod,
    ReasonCode,
)
from openstore.sidecar.core.settings import get_settings
from openstore.sidecar.gate.policy import Policy, current_policy, set_current_policy
from openstore.sidecar.protocols import mcp
from openstore.sidecar.protocols.agent_routes import card, get_surface, jwks, ucp_card

router = APIRouter(prefix="/agentic")

#: Paths inside `/agentic` that are NOT Merchant-session authenticated.
PUBLIC_AGENTIC_PATHS = frozenset(
    {
        "/agentic/approve",
        # The passkey ceremony is the Consumer agreeing, so it is authenticated
        # by the same one-time tap token as the page it runs on and by nothing
        # else. Behind the Merchant session it could never run at all.
        "/agentic/approve/passkey/begin",
        "/agentic/approve/passkey/finish",
        "/agentic/static/tokens.css",
    }
)


@dataclass
class ConsoleStore:
    """Live console state. Policy edits land here and the Gate reads them, so an
    edit changes Gate outcomes without a restart."""

    @property
    def policy(self) -> Policy:
        """The live Policy — the same one the Gate evaluates and the card
        publishes, read through the holder rather than kept here."""
        return current_policy()

    keys: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    overdue_holds: list[dict[str, Any]] = field(default_factory=list)
    export_acknowledged: bool = True
    provider_adapter: str = "fake"
    webhook_status: str = "no events yet"


@lru_cache(maxsize=1)
def get_console_store() -> ConsoleStore:
    return ConsoleStore()


async def _sealed_receipts() -> list[dict[str, Any]]:
    """Receipts this sidecar has sealed, newest first.

    Empty when there is no database — checked rather than caught. A sidecar with
    no database has sealed nothing, and the console must still render: the
    health tab is where an operator finds out *why* the boards are empty, and a
    console that refused to draw would hide the one screen that says so.
    """
    from openstore.sidecar.evidence.store import get_receipt_store

    store = get_receipt_store()
    if store.sessionmaker is None:
        return []
    rows: list[dict[str, Any]] = []
    for bundle in await store.all():
        tapped = next((s for s in bundle.sections if s.name == "tapped"), None)
        bought = next((s for s in bundle.sections if s.name == "bought"), None)
        rows.append(
            {
                "receipt_id": bundle.receipt_id,
                "total_minor": (bought.payload.get("quote", {}) if bought else {}).get(
                    "total_minor", 0
                ),
                "authority": (tapped.payload if tapped else {}).get("authority_kind", "—"),
                "version": bundle.version,
                # Carried so the overview can plot sealings over time. The
                # console does not parse it: the date is the first 10 characters
                # of an ISO-8601 instant, and a bundle whose seal time is blank
                # simply falls out of the series rather than crashing it.
                "sealed_at": bundle.sealed_at,
            }
        )
    return list(reversed(rows))


async def _refund_rows() -> list[dict[str, object]]:
    """The live queue, or nothing when there is no database to hold one — the
    same reason `_sealed_receipts` checks rather than catches."""
    queue = get_refund_queue()
    return await queue.rows() if queue.sessionmaker is not None else []


def _published_keys() -> list[dict[str, Any]]:
    """The keys this sidecar actually publishes, in the console's row shape."""
    return [
        {
            "kid": key.get("kid", ""),
            "created_at": key.get("created_at", ""),
            "revoked_at": key.get("revoked_at"),
        }
        for key in get_surface().jwks.get("keys", [])
    ]


async def build_state(store: ConsoleStore) -> ConsoleState:
    settings = get_settings()
    policy = store.policy
    return ConsoleState(
        merchant_domain=settings.openstore_merchant_domain or "unset",
        # Read from the live keyring, not a list the console keeps: an operator
        # looking at an empty Keys table has no way to tell "no keys enrolled"
        # from "the table is not wired up", and for a while it was the latter.
        keys=store.keys or _published_keys(),
        policy={
            "per_order_cap_minor": policy.per_order_cap_minor,
            "per_order_line_count": policy.per_order_line_count,
            "per_group_qty": policy.per_group_qty,
            "blocked_tags": sorted(policy.blocked_tags),
            "window_open": policy.window_open,
        },
        provider={
            "adapter": store.provider_adapter,
            "declared_methods": sorted(m.value for m in PaymentMethod),
            "enabled_methods": sorted(m.value for m in policy.enabled_methods),
            "link_lifetime_minutes": 15,
            "webhook_status": store.webhook_status,
        },
        authority={
            "kinds": sorted(k.value for k in policy.enabled_authority_kinds),
            "mechanisms": sorted(m.value for m in policy.enabled_intent_mechanisms),
        },
        exposure={
            "per-order cap": "yes",
            "per-order line count": "yes",
            "per-group quantity": "yes",
            "blocked tags": "yes",
            "enabled payment methods": "yes",
            "enabled authority kinds": "yes",
        },
        # Live, like the keys above. Both of these were lists the console kept
        # and nothing ever appended to, so the boards read "none yet" no matter
        # what the sidecar had actually admitted or sealed.
        agents=store.agents or get_surface().admission.board(),
        receipts=store.receipts or await _sealed_receipts(),
        # Live, not a list the console keeps: the queue the tool writes to is
        # the queue the Merchant reads, or the board is decoration.
        refund_requests=await _refund_rows(),
        overdue_holds=store.overdue_holds,
        dev_profile_hosts=settings.dev_profile_hosts,
        export_acknowledged=store.export_acknowledged,
        # Read from the handlers that serve these paths, not rebuilt here. A
        # console that assembled its own copy of the card would be a second
        # source of truth for the one document strangers integrate against.
        discovery={
            "/.well-known/agent-commerce.json": card(),
            "/.well-known/ucp.json": ucp_card(),
            "/.well-known/jwks.json": jwks(),
        },
        tools=mcp.tools_list(get_surface().requirements),
    )


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def console_root() -> HTMLResponse:
    return HTMLResponse(page(await build_state(get_console_store()), "overview"))


@router.get("/static/tokens.css")
def tokens_css() -> Response:
    """The imported token layer. Public: it is a stylesheet, and gating it behind
    the session would leave the approve page unstyled for every Consumer."""
    from pathlib import Path

    path = Path("design/tokens.css")
    if not path.exists():
        # Loud, because the failure is not. The page still renders with every
        # custom property undefined, which looks like a font bug rather than a
        # missing file — the console falls back to the browser's default serif
        # and nothing in the logs says why.
        logging.getLogger("openstore").warning(
            "NO TOKEN LAYER: %s is missing, so the console and the receipt viewer "
            "render with every design token undefined.",
            path.resolve(),
        )
        return Response(status_code=404, content="")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/css")


@router.get("/codes")
def codes() -> dict[str, list[str]]:
    """The closed reason-code set, so an operator can check what a refusal they
    were handed actually means. Generated from the enum — there is no second
    list.

    **Declared above `/{tab}` on purpose.** FastAPI matches in registration
    order, so a catch-all registered first would swallow this and answer "no
    console tab 'codes'" — which is exactly what happened once.
    """
    return {"reason_codes": [c.value for c in ReasonCode]}


@router.get("/{tab}", response_class=HTMLResponse)
async def console_tab(tab: str) -> Response:
    if tab not in dict(TABS):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not-found", "detail": f"no console tab {tab!r}"}},
        )
    return HTMLResponse(page(await build_state(get_console_store()), tab))


def record_overdue_hold(order_id: str, status: str, deadline: datetime, amount_minor: int) -> None:
    """Called by the expiry sweeper when a hold passes its deadline without
    closing. Visible within one sweep interval (60s, §16.7)."""
    get_console_store().overdue_holds.append(
        {
            "order_id": order_id,
            "status": status,
            "deadline": deadline.astimezone(UTC).isoformat(),
            "amount_minor": amount_minor,
        }
    )


def set_policy(policy: Policy) -> None:
    """A policy edit, applied to the one live Policy.

    Rebinding a local copy was the bug. `ConsoleStore`, `CheckoutContext` and
    `AgentSurface` each held their own `Policy`, and this function replaced only
    the console's — so an edit changed what `/agentic` displayed and nothing the
    Gate enforced. The cap, the window switch, the blocked tags: all rendered
    correctly, all applied to nothing.

    The test meant to catch it read `get_console_store().policy` back, which was
    the object this function had just written. It asserted this function against
    itself and passed while its own name was false.
    """
    set_current_policy(policy)


__all__ = [
    "PUBLIC_AGENTIC_PATHS",
    "AuthorityKind",
    "ConsoleStore",
    "IntentMechanism",
    "build_state",
    "get_console_store",
    "record_overdue_hold",
    "router",
    "set_policy",
]
