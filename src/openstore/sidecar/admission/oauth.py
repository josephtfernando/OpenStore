"""Two admission routes, one authority.

An allowlisted agent uses Merchant-issued OAuth client credentials; a stranger
self-registers by publishing an Agent Profile and signing every request against
it (ADR-0012). Both get the same four scopes and the same short expiry.

**No tier unlocks money.** `confirm` without a fresh accepted Authority is
refused by the Gate regardless of which door the agent came through. Reputation
buys throughput only — an allowlisted agent gets a higher rate limit and not one
capability more.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from openstore.sidecar.admission.ratelimit import Tier
from openstore.sidecar.core.codes import ReasonCode, Scope
from openstore.sidecar.trait.errors import TraitError

#: §16.7. Short, because an agent token is a bearer credential and the agent is
#: a stranger by design.
TOKEN_TTL = timedelta(minutes=15)

ALL_SCOPES: frozenset[Scope] = frozenset(Scope)


@dataclass(frozen=True)
class AgentToken:
    token: str
    agent_id: str
    tier: Tier
    scopes: frozenset[Scope]
    expires_at: datetime

    def allows(self, scope: Scope, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return scope in self.scopes and moment <= self.expires_at


@dataclass
class AgentRecord:
    """What the Merchant can see about an agent that showed up.

    Admission is open by design (ADR-0012), which makes the record of **who
    took it** the Merchant's only view of their own front door. The console
    listed agents from an empty list nothing wrote to, so the board said "no
    agents have registered yet" however many had.
    """

    agent_id: str
    tier: Tier
    name: str = ""
    profile_url: str = ""
    callback_url: str = ""
    """Where this agent asked for order events. Kept on the record rather than
    on the token, because a token expires every hour and a callback should not
    have to be re-declared to keep working."""
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    calls: int = 0

    @property
    def blocked(self) -> bool:
        return self._blocked

    _blocked: bool = False


@dataclass
class Admission:
    """Issues tokens by either route, and refuses blocklisted agents."""

    clients: dict[str, str] = field(default_factory=dict)
    """client_id -> client_secret, Merchant-issued in `/agentic`."""
    blocklist: set[str] = field(default_factory=set)
    tokens: dict[str, AgentToken] = field(default_factory=dict)
    seen: dict[str, AgentRecord] = field(default_factory=dict)
    """Every agent that has ever been admitted here, for the console's board."""

    def issue_for_client(self, client_id: str, client_secret: str) -> AgentToken:
        expected = self.clients.get(client_id)
        # compare_digest so a wrong secret cannot be found a character at a time.
        import hmac

        if expected is None or not hmac.compare_digest(expected, client_secret):
            raise TraitError(ReasonCode.SIGNATURE_INVALID, "client credentials are not valid")
        self._refuse_if_blocked(client_id)
        return self._issue(client_id, Tier.ALLOWLISTED)

    def issue_for_stranger(
        self, agent_id: str, *, name: str = "", profile_url: str = "", callback_url: str = ""
    ) -> AgentToken:
        """Issued on the spot, with no prior Merchant action. That is the
        product: admission is open by design, and the Gate is what makes it
        safe."""
        self._refuse_if_blocked(agent_id)
        return self._issue(
            agent_id,
            Tier.SELF_REGISTERED,
            name=name,
            profile_url=profile_url,
            callback_url=callback_url,
        )

    def issue_for_oauth(
        self, client_id: str, *, name: str = "", profile_url: str = ""
    ) -> AgentToken:
        """Access token for an OAuth DCR client.

        Same four scopes, `SELF_REGISTERED` tier — reputation buys throughput
        only, and the Gate still refuses spend without fresh human Authority.
        `agent_id` is the DCR `client_id`, so each registration gets its own
        basket and rate budget.
        """
        self._refuse_if_blocked(client_id)
        return self._issue(client_id, Tier.SELF_REGISTERED, name=name, profile_url=profile_url)

    def _refuse_if_blocked(self, agent_id: str) -> None:
        if agent_id in self.blocklist:
            raise TraitError(ReasonCode.AGENT_BLOCKED, "this agent is blocked by the Merchant")

    def _issue(
        self,
        agent_id: str,
        tier: Tier,
        *,
        name: str = "",
        profile_url: str = "",
        callback_url: str = "",
    ) -> AgentToken:
        token = AgentToken(
            token=secrets.token_urlsafe(24),
            agent_id=agent_id,
            tier=tier,
            # Identical scopes on both routes. A tier that carried extra scopes
            # would be a tier that unlocks money.
            scopes=ALL_SCOPES,
            expires_at=datetime.now(UTC) + TOKEN_TTL,
        )
        self.tokens[token.token] = token
        self._note(agent_id, tier, name=name, profile_url=profile_url, callback_url=callback_url)
        return token

    def _note(
        self, agent_id: str, tier: Tier, *, name: str, profile_url: str, callback_url: str = ""
    ) -> None:
        record = self.seen.get(agent_id)
        if record is None:
            self.seen[agent_id] = AgentRecord(
                agent_id=agent_id,
                tier=tier,
                name=name,
                profile_url=profile_url,
                callback_url=callback_url,
            )
            return
        record.last_seen = datetime.now(UTC)
        record.tier = tier
        # A re-registration may carry a better name; it never replaces one with
        # nothing.
        record.name = name or record.name
        record.profile_url = profile_url or record.profile_url
        # A re-registration that declares no callback is an agent that has not
        # changed its mind, not one withdrawing. Withdrawal is publishing a
        # Profile without the field and is therefore indistinguishable — so the
        # documented way to stop events is to answer them with a 410, which
        # parks the queue after MAX_ATTEMPTS.
        record.callback_url = callback_url or record.callback_url

    def note_call(self, agent_id: str) -> None:
        """One tool call by this agent. The board's "last seen" is about use,
        not about registration — an agent that registered once and never came
        back should look different from one that is working."""
        record = self.seen.get(agent_id)
        if record is not None:
            record.calls += 1
            record.last_seen = datetime.now(UTC)

    def board(self) -> list[dict[str, object]]:
        """The console's rows, newest activity first."""
        return [
            {
                "agent_id": r.agent_id,
                "name": r.name,
                "tier": r.tier.value,
                "calls": r.calls,
                "last_seen": r.last_seen.isoformat(timespec="seconds"),
                "blocked": r.agent_id in self.blocklist,
            }
            for r in sorted(self.seen.values(), key=lambda r: r.last_seen, reverse=True)
        ]

    def resolve(self, token: str, *, now: datetime | None = None) -> AgentToken:
        record = self.tokens.get(token)
        if record is None:
            raise TraitError(ReasonCode.SIGNATURE_INVALID, "unknown token")
        if (now or datetime.now(UTC)) > record.expires_at:
            raise TraitError(ReasonCode.SIGNATURE_INVALID, "token expired")
        if record.agent_id in self.blocklist:
            # Revocation invalidates the future, not the past: an already-issued
            # token stops working the moment the Merchant blocks the agent.
            raise TraitError(ReasonCode.AGENT_BLOCKED, "this agent is blocked by the Merchant")
        return record

    def revoke_agent(self, agent_id: str) -> None:
        self.blocklist.add(agent_id)
