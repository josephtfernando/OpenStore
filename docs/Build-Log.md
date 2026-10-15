# LOGS

> Running record of the MVP build. Formerly root `LOGS.md`. Frozen on 2026-09-23 during the docs reorganization; resumed 2026-10-15 at the operator's request.

Running record of changes and decisions for the OpenStore MVP build. Newest entry at the top. Every entry: what changed, why, and what it means for anyone executing `SPECS/PLAN.md`.

---

## 2026-10-15 · Door 10, and every shop becomes connectable by a pasted URL

Logged late: the work below landed 2026-09-26/27 and sat uncommitted on `main`
until now, when it was reviewed, split into three commits, and committed. It was
one session's answer to "point a stock MCP client at a shop", and it turned into
two features plus a hardening pass.

**The optional tenth door (`requirements.read`).** The trait had nine doors and
a fixed buyer shape: address and contact, nothing else. A Merchant that needs
something more — a delivery slot, gate instructions — had no way to say so, and
an agent had no way to learn it except by being refused. Door 10 is read-only
and optional: a Merchant declares flat string/enum fields (at most 20, keys
`^[a-z][a-z0-9_]{0,63}$`), and the sidecar snapshots the declaration (refreshed
every 5 minutes), publishes it in the shop card's `requires`, and renders it as
the `set-details` tool's JSON Schema. One vocabulary in both places, so a client
cannot trust the card and be refused by the tool. A Merchant without door 10
keeps the fixed shape and nothing breaks — absence is a supported answer, not a
failure. `pii: true` declarations are refused as a whole: there is nowhere to
store dynamic PII honestly until generic receipt commitments exist, so the
Merchant keeps the fixed shape and the refusal is loud in the boot log.

`set-details` is the fifteenth tool. Details live on the basket, ride into
`orders.create` and `quote` (each only when non-empty, so a Merchant whose
door-7/9 schema predates details is not tripped by an always-present key), and
are validated against the declaration the sidecar holds — never a second copy of
the rules. The demo merchant site implements door 10 with `delivery_slot` (an
enum) and `delivery_instructions`; the WooCommerce adapter declares no extras.

**Generic-agent hardening, found alongside it.** Every refusal an agent has to
recover from now carries machine-readable fields — `missing`, `unknown`,
`offered`, `any_of` — so a model retries from data instead of parsing a
sentence. Destination aliases (`address_line1`, `zip`, `pincode`, `town`, …) are
normalized at the agent boundary while the trait contract stays strict. The
nested schemas for `set-destination` / `set-contact` / `set-details` are derived
from the models themselves, and every tool description states its prerequisites
and its next step.

**Public-origin hardening.** Driving the demo behind a tunnel exposed two things
that only bite off `*.localhost`: a WebAuthn ceremony minted for one host cannot
run on another, so the approve page now hides the passkey button and says why
when the public origin and the RP ID disagree (`/readyz` warns too), and
`FakeProvider` links now point at the deploy's public origin rather than the
demo default. Every other authority kind keeps working.

**OAuth 2.1 with Dynamic Client Registration.** A stock MCP client can now
connect with nothing but a pasted URL. `initialize`, `notifications/initialized`
and `tools/list` are public (the same bytes as `GET /agent/tools`), and
anonymous `search` / `read-item` serve the already-public catalogue under IP
limits; everything with a basket stays behind Bearer + scopes. The sidecar
advertises `/.well-known/oauth-authorization-server` and RFC 9728
protected-resource metadata, serves `/agent/oauth/{register,authorize,token}`,
and answers a missing token with a 401 that names the resource metadata so a
generic client discovers sign-in by itself. Registration is open, like
`/agent/register`. Access tokens are the existing opaque Bearer tokens at
`SELF_REGISTERED` tier with the same four scopes: reputation buys throughput
only, `place-order` still returns an approve URL, and the Gate still refuses
spend without fresh human Authority. `agent_id` is the DCR `client_id`, so each
registration gets its own basket and its own rate budget. `docs/Claude-Setup.md`
documents the flow; `scripts/funnel.sh` puts the demo behind Tailscale Funnel
for a real custom-connector test.

**Found while reviewing to commit:** `test_purchase_live.py` fetched
`/receipt/<id>` and called `.json()` on it, but that route serves the HTML
viewer — the JSON is at `/receipt/<id>.json`. It only stayed hidden because the
live suite is skipped when no stack is running.

Committed as `ce90fe4` (door 10 + agent surface + public-origin hardening),
`c0bd935` (OAuth 2.1 + DCR), `15ccc78` (the receipt-test fix). 672 tests pass,
20 skipped; `ruff` and `mypy` clean. Not yet exercised live against a real
Claude connector — the funnel path is written and documented but unrun from
here.

## 2026-09-22 · Mutation testing reaches the passkey ceremony; the two most consequential finds of the pass

Extended mutmut from gate+ledger into `authority/passkey.py` — the WebAuthn ceremony that actually binds an Authority to a cart/amount/merchant/expiry, upstream of everything the Gate's authority check trusts. Test selection also gained `test_durability.py`, since `forget_expired` (the challenge/credential sweep) runs there and not in `test_passkey.py`; without it, all 20 of its mutants read as "no tests" rather than a real verdict.

The two highest-value findings of the whole mutation-testing pass so far, both in `verify_assertion`/`verify_enrollment`: `sign_count=verified.new_sign_count` (and the enrollment equivalent) could be dropped from the stored-credential write with nothing noticing. `StoredCredential.sign_count` defaults to 0, so a silently-dropped update pins every credential's counter at zero forever — the exact thing a WebAuthn sign counter exists to catch (a cloned authenticator replaying an old signature looks identical to a legitimate one once the real counter is never compared against). Added a test doing two successive assertions and checking the stored counter actually advances between them, plus a floor check on enrollment's own write. Also found: `require_user_verification=True` had never been distinguished from `False`/`None` — no test ever produced an assertion missing the UV flag, so a test using the software authenticator's own `user_verified` toggle was added, confirming the ceremony correctly refuses (`AUTHORITY_STALE`) without it.

A third, structural finding in `challenge_for` itself — the function whose whole job is being the cart/amount/merchant/expiry binding. An existing test proved the resulting hash is sensitive to every one of its five inputs, but that test can't tell a renamed preimage key (`cart_hash` → `CART_HASH`) from the real one, since renaming a key changes no before/after pair's difference — all 12 of its survivors were exactly that. Added a test that recomputes the exact preimage independently of the function's own dict literal and compares hashes, which pins the field names too; the preimage format is a wire contract with whatever verifies a receipt offline (SPEC §9), so this isn't pedantry. Rounded out with a `credentials_held()` field round-trip test — its only prior test checked list length, not that any field survived the trip out correctly.

Combined baseline across all three modules: 958 mutants, 762 killed, 196 survived, 0 no-tests. 618 tests passing (was 606 at the start of this mutation-testing arc), guardrails and mypy clean throughout, 12 commits this pass. Sampling the remainder: mostly message-text (refusal wording, WebAuthn options-dict client hints like attestation preference) with no dedicated coverage — real, but not money-path logic, and diminishing returns past this point. Next candidate once these are triaged further: `evidence/bundle.py` (receipt sealing), no mutation coverage yet.

## 2026-09-22 · Mutation testing extended to the Ledger; five real gaps, one dead function

Continued the Phase 3 mutmut pass from the Gate into `ledger/entries.py` — the actual money-movement bookkeeping the Gate's PASS authorizes, and the next-highest-value target per the standing memory note. `only_mutate` now covers both files, test selection `test_gate.py` + `test_ledger.py`.

Same dead-code shape as `decide.py`'s `ctx.binding`/`reserve_or_refuse`: `no_hold_error()` was defined, never called anywhere — the real "no open hold" refusals construct `TraitError` inline both in `trait/fake.py` and inside `ledger/entries.py`'s own guards. Deleted.

Five real boundary gaps, all genuine untested guards rather than message-text noise: `reserve()`'s double-hold check never had a test where a first hold fully closed before a second reserve, so an `and`/`or` swap on the guard survived undetected; the `open_holds_minor == 1` boundary was untested across `reserve()`/`release()`/`capture()` — a hold whittled to one paisa by a partial release needs to still count as open; `refund()`/`reversal()` both refuse an amount of exactly zero (a real guard, distinct from the general negative-amount check in `_append()`) but neither had ever been called with zero; and `assert_invariants(closed=True)` had only ever been asserted to succeed, never to fail — nothing proved it read the order actually being checked rather than a coincidentally-empty one. Added nine tests total, including the first negative case for `assert_invariants`.

Combined gate+ledger baseline: 510 mutants, 389 killed, 121 survived, 0 no-tests (started this session at 514/371/139/4). 614 tests passing (was 606), guardrails and mypy clean throughout. Next candidates once the remaining 121 are triaged: `evidence/bundle.py` (receipt sealing) and `authority/passkey.py` (the WebAuthn ceremony binding) — both real money-path modules with no mutation coverage yet.

## 2026-09-22 · Phase 2 re-verified live: it was already done, not skipped
## 2026-09-22 · Phase 2 re-verified live: it was already done, not skipped

An automated continuation prompt flagged Phase 2 as unimplemented — no generalized chat, no 10 stores, no consumer budget answer — after a context compaction. That earlier work is real and was already committed; the flag was checking a stale view. Re-verified rather than re-built, since re-implementing would have silently duplicated or regressed working code:

- `docker compose ps`: all 22 containers healthy, all 10 sidecar/store pairs up.
- All 10 storefronts and their `agent-commerce.json` cards return 200 live (`spoiledduckie` through `furrow`).
- `make demo` (the live conformance suite against the running stack, not mocks): 19 passed, 1 skipped.
- `demo/buyer-chat`: 191 tests passing, `svelte-check` clean, thinking blocks / model switcher / transcript export / generic-MCP paste-a-link (`initializeGeneric`/`callGeneric`) all present in `+page.svelte` and `lib/mcp/client.ts`.
- The consumer-budget question from the original ask ("how is my budget computed, I never signed a key") is answered directly in the consent modal's copy (`+page.svelte`): the chat's Allow click grants a scope, never an amount; the passkey/UPI-PIN step that actually authorizes spend happens on the shop's own origin because a WebAuthn credential is scoped to the RP ID that registered it; the spend ceiling (`contacts/+page.server.ts`) is an explicitly advisory flag, not a held mandate.
- The four cross-shop overlap SKUs (AA batteries: CircuitYard/IronList, sewn notebook: Dog-Eared/DeskField, tennis balls: Furrow/Playspool) are seeded with genuinely different price and stock per shop, the actual precondition for the multi-merchant reasoning the ask wanted.

One real slip caught in the process: running raw `tsc --noEmit` against the buyer-chat reported 8 errors on SvelteKit action handlers' implicit `request: any`. False alarm — raw `tsc` doesn't resolve SvelteKit's generated `./$types`; the project's own `pnpm run check` (`svelte-check`) is the correct tool and reports 0 errors. Worth remembering next time: check `package.json`'s own `check` script before trusting a bare compiler invocation on a SvelteKit project.

## 2026-09-22 · Mutation testing re-established for the Gate, and a real dead-field bug it found

Phase 3 cheap-verification pass. The `[tool.mutmut]` config had gone stale — it pointed at `src/openstore/core/*`, which no longer exists after the rebuild moved everything under `src/openstore/sidecar/`. Re-pointed at `gate/decide.py` (the single authorizer every money decision passes through) and hit a real mutmut 3.x gotcha along the way: `source_paths` controls what gets *copied* into the isolated `mutants/` working tree, not what gets *mutated* — scoping it to one file leaves that file's own package with no `__init__.py` and every sibling module missing, so collecting `tests/` inside `mutants/` fails on the first import reaching outside that file. Fixed by pointing `source_paths` at the whole `sidecar/` package and using `only_mutate` (a glob) to actually scope the mutation. `tests_dir`/`runner` are also gone in 3.x — replaced with `pytest_add_cli_args_test_selection`, kept narrow to `test_gate.py` alone for the same reason (the full suite reaches outside the copied tree).

First baseline: 299 mutants, 191 killed, 99 survived, 9 no-tests. Three survivors were real, not noise: `_check_authority` computed a `Binding` and assigned it to `ctx.binding` on every synchronous (lands-before-gate) decision, but nothing ever read it — the real receipt-sealing path in `checkout.py` independently re-derives the binding from `authority_kind`/`mechanism` rather than trusting a value computed earlier, and `transcript.binding` is populated only by `settle()`'s separate deferred UPI-PIN path. Every mutation of that dead assignment survived because nothing could observe the difference; deleted the field. Same shape, bigger: `reserve_or_refuse` — a whole module-level function, "the reserve that follows a passing `decide()`" per its own docstring — was never imported or called anywhere in the codebase; `checkout.py`'s actual post-decide() reserve step calls `trait.reserve()` directly. That's why its 9 mutants showed as "no tests" rather than "survived" — nothing could even collect a test for a function nobody calls. Deleted it. Third: the per-order cap check used `total_minor > cap` with no test at the boundary, so a mutant flipping it to `>=` survived silently — added one (a total sitting exactly on the cap must pass, "exceeds" means strictly over). `_first_difference`'s three message branches were also only ever exercised through a loose `X in detail or Y in detail` assertion; added a direct unit test pinning all three.

Final count after both fixes: 290 mutants, 191 killed, 99 survived, 0 no-tests. The remaining 99 are an untriaged backlog — a first read shows most as message-text mutations on FAIL-path detail strings, not obviously more logic bugs, but that scan wasn't exhaustive and two dead-code findings came out of this same pass, so it's worth specifically checking survivors that touch state no later code reads rather than assuming they're all wording. Recorded in memory (`mutmut_survivor_backlog.md`) rather than the old stale one. Next pass: widen test selection to `test_golden_replays.py` + `test_protocols.py` before trusting remaining "survived" verdicts, then triage what's left; after that, the same `source_paths`-covers-the-package pattern extends to `ledger/entries.py`.

## 2026-09-21 · Phase 2 begins — `/agent/mcp` becomes real JSON-RPC MCP (ADR-0026)

Phase 2 scope, set by the operator: a generalized Claude.ai-style chat surface (thinking blocks, model switcher, transcript export, per-tool "always allow" for non-money-path tools, decoupled enough to point at any `agent-commerce.json`/MCP server, not just this repo's), 10 lightweight demo stores with intentional cross-store SKU overlap, and a real answer to "how does my budget get computed and where did I sign anything" — investigated in full before writing code (see below).

First landed: `/agent/mcp` was labeled `"mcp"` in the agent-commerce card and the conformance badge but spoke a proprietary `{"tool", "input"}` envelope, not JSON-RPC 2.0 — a real gap given this project's own ADRs exist specifically to stop conformance claims outrunning what's built. Fixed properly, not shimmed: `initialize` / `tools/list` / `tools/call` per spec, JSON Schema `inputSchema` per tool, and `annotations.moneyPathHint` (`true` only for the `start-checkout`/`confirm` scopes) so a generic client can offer standing "always allow" on everything else without knowing a single tool name. Every direct caller in the test suite updated to the new envelope via one shared `tests/mcp_helpers.py`; 582 tests green, mypy clean across `src/`. No back-compat shim for the old shape — it was never real MCP, so nothing depends on it staying fake.

Investigated and recorded, not yet built: no consumer-owned spending budget exists anywhere in the sidecar (only a flat merchant-side `per_order_cap_minor`); the passkey ceremony (`authority/passkey.py`) is real and correctly binds cart hash + amount + merchant + expiry, but only runs on the merchant's own origin at `/agentic/approve` — never in the chat — because a WebAuthn credential is scoped to the RP ID that registered it and the chat literally cannot perform that ceremony for a merchant it isn't. The chat's "Allow" button today is a scope gate (run this tool or don't), not a money authority; that split is correct, not a bug, but it was never explained to the Consumer. Next: an explicitly-labeled assistant-side advisory spend ceiling (not a rail-held mandate — ADR-0024 keeps that deferred) plus chat copy that states the split instead of leaving it implicit.

## 2026-09-21 · Wire fix, consent widened, a real bug found along the way

Landed and verified live against the running stack (`make up` + `make demo`, both green; a manual search → variant widget → add-line round trip through the actual `openrouter:openai/gpt-oss-20b` driver):

- Fixed the buyer-chat's `mcp/client.ts`, which posted the pre-ADR-0026 `{tool, input}` shape — every call would have failed against the now-real JSON-RPC endpoint. `call()`'s own contract (throws `ShopError`, returns the tool's structured result) is unchanged.
- `tool_calls` never persisted which shop a call went to, even though `SeenCall`/`Seen` (loop.ts) already carry a `shop` field for multi-shop catalogue reasoning — a real gap for the 10-store goal, not just a type error. Added the column, threaded `shop` through every `recordToolCall` site.
- Phase 2 ask #1 (always-allow for non-money-path tools): widened `ALWAYS_ALLOWABLE` to the scope-ladder boundary (everything outside `start-checkout`/`confirm`), matching the sidecar's `moneyPathHint`. That reuse alone would have been wrong — two call sites relied on the old narrow set meaning "safe to run unguided and fan out to every shop." Split into `ALWAYS_ALLOWABLE` (consent) and a new `READS` (unguided/fan-out) so a write can never reach a shop nobody named.
- Found while wiring the above in: the scripted-driver consent gate reimplemented `requiresFreshConsent()` inline with the wrong boolean operator, so any always-allowable tool skipped its consent prompt unconditionally, including the very first call. The tested function existed and was simply never imported. Fixed to call it.
- Consent copy for `start-checkout`/`place-order` now states the split the operator asked about: the chat's own Allow click grants a scope, never an amount; the passkey/UPI-PIN step that actually authorizes spend happens on the merchant's own origin, and the chat cannot perform it because a WebAuthn credential is scoped to the RP ID that registered it.

140 buyer-chat tests green (3 new), 582 Python tests green, 0 type errors both sides. Next: the remaining 8 stores (2 of 10 exist), thinking blocks + model switcher UI, and an explicitly-advisory consumer spend ceiling.

## 2026-09-21 · Ten stores, live and seeded

Phase 2 ask #2. Eight more shops (CircuitYard/electronics, IronList/hardware, PantryLine/grocery, Kettle & Grain/kitchenware, DeskField/stationery, Root & Leaf/plants, Playspool/toys, Furrow/pets), each its own state (GST-path diversity, matching the SpoiledDuckie/Dog-Eared pattern), own DB roles, own sidecar+store pair, own Caddy vhost — additive only, no sidecar code changes, exactly as the earlier survey predicted. Four deliberate cross-shop overlaps (AA batteries, filter coffee, a notebook shared with Dog-Eared's own SKU unchanged, a tennis-ball 3-pack) for the multi-merchant reasoning the operator asked for. SVG placeholders throughout, generator generalized to iterate every registered profile.

Found and fixed along the way: `make seed` only ever seeded the first shop — Dog-Eared had no seed profile wired to `make up` at all since it was added. Genuinely never seeded by the automated path before this. `seed` now loops every store service.

Verified on a fresh volume: `make down && make up && make demo` clean, 22 containers healthy, all ten storefronts + cards return 200, 582 Python tests green, both live suites green.

**A real gap found while trying to verify the overlap SKUs live**: `shopsFor()` (loop.ts) — the function that fans an unaddressed call out across every known shop, or resolves it to the one shop a SKU can only mean — is fully implemented and unit-tested, but is never called anywhere in `+page.server.ts`. The running app only ever operates on `currentShop()`, the single most-recently-added contact. "Serve all 10 simultaneously" is not yet true of the actual chat flow, whatever the data model already supports. Next.

## 2026-09-22 · The chat actually talks to every shop now

Closed the gap logged above. `currentShop()` → `knownShops()` everywhere: `runAgent`'s read batching and single-call dispatch resolve per call through `shopsFor`, `startFresh`/`reset` clear every shop's basket instead of one, the basket panel shows one basket per shop (ADR-0007 — one sidecar per shop means one basket per shop is the only honest thing to render). `awaiting` now carries the domain a call resolved to *at propose time*, so the tap the Consumer answers is the shop they were shown.

`shopsFor` throwing `shop-required` (ambiguous — two-plus shops answer to the same id, or none named among several) is a question, not a crash: caught at every call site the same way `variant-required` already is, and `ToolError` gained a `fields` payload so the candidate shops travel with the refusal into a `choices` widget instead of asking the Consumer to type a domain.

Verified live: 8 shops added as chat contacts, search for "AA batteries" fanned out to all 8 concurrently, returned real matches from exactly CircuitYard (₹249) and IronList (₹229) — the two that actually sell it — and honest not-found from the rest. Every tool card in the transcript and export now names which shop answered. 140 buyer-chat tests green, 0 type errors, 582 Python tests green, `make demo` clean.

Not yet exercised live: the `shop-required` disambiguation widget itself. With this catalogue's SKU-prefixing convention (`CY-`, `IL-`, …), a write's SKU essentially never collides across two shops — only `group` ids do (e.g. both battery shops use `aa-batteries`), and reads fan out before that could matter. The widget is unit-tested and reachable by the empty-catalogue "you have N shops, which one" branch; genuine SKU collision on a write remains a defensive path for a catalogue that doesn't prefix as consistently as this demo's does.

## 2026-09-22 · The budget question, answered and shipped

Closed the operator's original question with a real feature, not just consent copy. A Consumer-set advisory spend ceiling: entered on the Shops page in rupees, stored as a Consumer preference (a new `settings` key-value table, deliberately generic for whatever preference comes next), checked when `place-order` is pending against the same shop's most recent `start-checkout` total. Over the ceiling adds one line to the existing consent modal, explicit that it is a flag, not a block — nothing here claims or attempts enforcement, because nothing here has the authority to (ADR-0008, ADR-0024 — a rail-held mandate stays deliberately unbuilt, this is UX, not a payment-rail feature).

While driving a real checkout live to test it, hit a second real gap the multi-shop dispatch work above left open: `set-destination`, `set-contact`, `start-checkout` and `place-order` carry no sku/group to resolve a shop by, so with 2+ shops known, every step past `add-line` re-asked "which shop?" — including immediately after `add-line` had just resolved one. Fixed: `shopsFor` now falls back to whichever single shop holds basket lines when no id resolves one.

Verified live, start to finish: added SpoiledDuckie, set a ₹100 ceiling, drove tote → destination → contact → fulfillment → checkout (₹948) through the real `openrouter:openai/gpt-oss-20b` driver with 9 other shops known throughout, no shop-required refusals past the first, and the ceiling flag rendered correctly on the pending place-order card. 146 buyer-chat tests green, 582 Python tests green, both live suites green.

Phase 2 scorecard: MCP wire correctness ✓, always-allow widened ✓, ten stores ✓, multi-shop dispatch ✓, budget question ✓ (with a real feature, not just an explanation). Still open: thinking blocks, a real model switcher UI (env-var only today), and true "paste any MCP server, not just this repo's" genericity — the tool set is still the closed 14-name OpenStore list, not fetched from whatever server was pasted.

## 2026-09-22 · A real model switcher

Closed the model-switcher item. Driver selection was `CHAT_MODEL_DRIVER`, read once at process start and cached in a single module-level `held` instance for the process's whole lifetime — shown to the Consumer as read-only text. Replaced with `driverFor(choice)`, cached per choice in a `Map` (not one instance — `ScriptedDriver` keeps a pinned sequence's position *in the instance*, so switching away and back must not rebuild it and lose that position), plus `availableDrivers()` reporting which drivers this process actually has a key or model for.

The failure mode "pick an unconfigured driver, next message breaks" doesn't exist to hit: `driverFor` only ever honours a choice among the configured ones and falls back to the deploy's own default otherwise, and the switcher's own `<option>`s are disabled for the unconfigured ones too — belt and suspenders, deliberately, since a UI control and the function behind it agreeing by accident is how they drift apart later.

Backed by the same `settings` table the spend ceiling introduced (`model_driver_override`) — the second thing confirming that table was worth adding generically rather than as a one-off column.

Verified live: switched the running deploy to `scripted` (`running: scripted`), cleared back to the deploy default (`running: openrouter:openai/gpt-oss-20b`), both took effect on the next request with no rebuild. 152 buyer-chat tests green (6 new), 0 type errors, 582 Python tests green.

Remaining from the original ask: thinking blocks, and true "paste any MCP server, not just this repo's shops" genericity. The latter is architecturally the largest piece left — `OpenRouterDriver` already takes its tool-schema map as a constructor argument rather than hardcoding it internally, which means the driver itself doesn't need to change; what's still closed is `+page.server.ts`'s hard `TOOLS.includes(call.name)` filter (drops any tool name outside the 14-name OpenStore set before the model's call ever reaches dispatch) and the contacts flow requiring an `agent-commerce.json` card rather than accepting a bare MCP endpoint. Sized at a full session's work on its own to do to this codebase's standard, not a bolt-on.

## 2026-09-22 · Thinking blocks, from the model's own reasoning

Closed the last Claude.ai-surface item. OpenRouter's `reasoning` request parameter is a documented no-op for models that don't support it, so no capability probing was needed — just ask, and use whatever comes back. `OpenRouterDriver.step()` now captures `message.reasoning` and carries it on `ToolTurn`, on both the text-turn and calls-turn variants: a turn that ends in tool calls still reasoned its way there, and a calls-turn produces no chat message of its own to carry it on, so it gets its own empty-text row timestamped ahead of the tool cards it led to.

Stored verbatim on a new `messages.thinking` column, rendered as a collapsed disclosure above the message it belongs to — the same convention Claude.ai uses, and the same "never paraphrase a value that could then be wrong with no way to tell" rule everything else on this page already follows. Both transcript export formats carry it too.

Verified live against the real running model (`openai/gpt-oss-20b`): asked "what totes do you have?", got three genuine reasoning blocks across the search → read-item → describe turns, each one actual model reasoning about what to do next — not a placeholder — rendered as three collapsed disclosures.

Phase 2's original seven-item list is now six done; the seventh (true "paste any MCP server" genericity) starts next.

## 2026-09-22 · Phase 2 complete — connects to any MCP server, not just this repo's shops

The last and largest item. Paste a bare MCP server URL on the Shops page and the chat connects the way Claude Desktop's own MCP connector does — no card, no pinned key, just `initialize` + `tools/list` over the same SSRF-hardened fetch every pasted URL already went through (`connectMcp` in contacts.ts, falling back from `fetchCard` only when the failure wasn't a pure SSRF-level refusal). A `kind`/`mcp_endpoint`/cached-`tools` column set tells a `'generic'` shop apart from this repo's own `'openstore'` ones.

The real work was everywhere a hardcoded `TOOLS.includes(...)` gate or the fixed `TOOL_SCHEMAS` map stood between a generic tool and the model actually being able to call it: `reachableTools()`/`schemasFor()` replace both, `shopsFor()` resolves a tool outside the closed set to whichever shop's own `tools/list` named it, `isMoneyPath()` generalises the start-checkout/confirm boundary past the closed set (money-path by default when a tool's server says nothing at all — nothing here can verify silence), and `validate()` stopped hard-refusing any name it didn't recognise.

Verified live, not just in tests: stood up a throwaway toy MCP server (two genuinely non-commerce tools, `roll_dice`/`current_time`, no OpenStore semantics anywhere in it — proof this is a real generic client, not shop-shaped tooling with a costume on), connected it through the actual Shops-page form, and asked the real running model to roll a die. Found and fixed three real bugs doing that, none of them things a mocked test would have caught on its own:

1. **The function-name round-trip was wrong.** OpenRouterDriver blindly reversed every underscore in a model's tool-call name back to a hyphen — correct for the closed set's own naming (`add_line` → `add-line`) and silently wrong for anything else (`roll_dice` → `roll-dice`, a name nothing declares, so the call was dropped with no error). Fixed with a real per-call map built alongside the request instead of a global guess.
2. **`validate()` still hard-refused any name outside the closed set**, even after `shopsFor` had already resolved it to the right shop — one gate generalised, one missed.
3. **The live OpenRouter path never actually checked `standing` for a write at all.** `runAgent`'s write-call branch set `awaiting` unconditionally, every time, for every tool — meaning "Always allow" had never taken effect in the live demo for *any* tool, including this repo's own build-basket ones, despite `requiresFreshConsent` being correctly implemented and passing its own unit tests since the very first "always-allow" pass earlier this session. Only the scripted-driver path had ever actually consulted it. A real, pre-existing gap, closed now — and a second, unplanned confirmation that the earlier always-allow widening is now actually live.

End to end: "roll a 20 sided die for me" → model calls `roll_dice(sides=20)` → resolves to the toy shop → consent prompt → approved → "Your 20-sided die landed on 17." A follow-up with "always" granted skipped the prompt entirely — for the generic tool and, once re-verified, for this repo's own tools too.

188 buyer-chat tests green (31 new), 0 type errors, 582 Python tests green, `make demo` clean. The verification server and its one-line SSRF-allowlist addition were both torn down after — nothing throwaway shipped.

**Phase 2 scorecard, final**: MCP wire correctness ✓, always-allow widened ✓ (and now actually live, not just correct on paper), ten stores ✓, multi-shop dispatch ✓, budget question ✓, model switcher ✓, thinking blocks ✓, generic MCP server support ✓. All seven original items closed. Phase 3 — "find no more improvements" — starts now.

## 2026-09-22 · Phase 3 — an audit finding, not a live-triggered one

First finding that came from deliberately auditing rather than from something breaking live: nothing in the buyer-chat app ever set a fetch timeout. `contacts.ts`'s card fetch always had one (`FETCH_TIMEOUT_MS`/`MAX_CARD_BYTES`); every other outbound call — the generic MCP client's `rpc()` (hit on every tool call to a connected generic server, not just once at add time), the original OpenStore `call()`/`register()` added earlier this session, and all three model drivers including the one actually running (OpenRouter) — had none. A hung server on either side used to hang the whole chat request behind it, indefinitely, with nothing anywhere in the stack that would ever recover.

Fixed in two passes: the MCP client side first (10s generic / 15s this repo's own shops, plus a 256KB response cap mirroring the card fetch's own), then a `grep` for every remaining `fetch(` call site in `src/lib/` turned up the three model drivers too (one shared `fetchWithTimeout`, 30s, used by all three). Both verified live: a stale test contact pointed at a now-unreachable server timed out at 10s instead of hanging; a real chat message still got a real model response through the wrapped fetch afterward.

191 buyer-chat tests green (3 new across both passes), `make demo` clean throughout.

Same audit continued into merchant-site: `callSidecar()` (refund, shop-reject, COD collection, RTO) had the identical gap and got the identical fix (15s timeout, matching this repo's own shops' timeout on the buyer-chat side). Verified live across a rebuild of all ten storefronts.

## 2026-09-22 · Admin login worked for exactly one shop out of ten

The most consequential Phase 3 finding so far, and the kind that only shows up when something is actually clicked rather than assumed to generalise. Tested the admin console live against a new store for the first time — 403, "Cross-site POST form submissions are forbidden," on every login attempt.

Root cause: `svelte.config.js`'s CSRF `trustedOrigins` was `['http://127.0.0.1:3000', 'http://localhost:3000']` — added correctly, with a comment explaining exactly why (no `x-forwarded-proto` over a direct loopback connection makes adapter-node assume `https`, mismatching the browser's real `http` `Origin`), but scoped to the one port that existed when it was written. Tested Dog-Eared (port 3010, predates the ten-store work entirely) to check whether this was new: also 403. Admin login has apparently never actually been exercised with a real form POST against anything but SpoiledDuckie.

Fixed by naming all ten of this repo's own admin ports instead of one — still a fixed list on purpose (these are known demo ports, not something to infer from an incoming request), just the right size now. Rebuilt all ten store images; verified real logins (real credentials, real session cookie) succeed for CircuitYard and for the previously-broken Dog-Eared, with CircuitYard's dashboard rendering its own branding and live data afterward.

While in there: ran the `openstore-conform` CLI's read-only suite (doors 1/2/9 — catalog, stock, quote) against all ten stores' trait implementations directly, not just the two it had ever been pointed at before. 10/10 on eight of them; PantryLine and Playspool score 9/10 with the tenth check (orphan Add-on refused) legitimately skipped — neither store's catalogue has an addon-tagged item, which is a fact about what they sell, not a gap. Full trait conformance confirmed across every store this session added.

---

# Morning report

Written as §0 requires: every phase with its gates, every ladder-step-4
decision, every blocked item, and every cut.

## Phases

| Phase | Gates | Evidence |
|---|---|---|
| **h0** contracts frozen | green | 17 closed sets, 3 `cart_hash` vectors, door + Quote schemas, `.env.example` |
| **A1** skeleton + harness | green | boots behind the proxy split; 18 planted violations caught |
| **A2** trait client + fake | green | conformance suite over real HTTP; `variant-required` at 5 doors |
| **A3** gate + ledger + provider | green | 12 checks in order, both invariants, COD writes nothing at order time — and reached over HTTP since 09-21: `make demo` walks search → signed receipt through it, now via the Provider's own callback rather than around it |
| **A4** authority + admission | green | both admission routes work live since 09-21; `passkey.py` landed 09-21 and a whole passkey purchase runs against the stack — enrollment, the attestation fallback, `cart`/`payer-device` in a receipt the verifier calls VALID |
| **A4c** agent signing kit | green | one fixture asserted by both suites; the chat publishes its Agent Profile and self-registers (09-21) |
| **A5** evidence + verifier | green | exit 0/1/2; the sidecar holds a persisted key and seals a receipt at the end of every real purchase (09-21) |
| **A6** four protocols | green | all four mounted and exercised over HTTP (09-21): one core, four envelopes, one total across all of them, ACP's completion refused with its reason |
| **A7** `/agentic` console | green | every shipped tab renders from live state — keys, agents and receipts were lists nothing populated until 09-21 |
| **A8** mount + install + feed | green | `make up` clean → working shop with photography, zero hand-edited files; the cross-site and font defects were fixed 09-21 |
| **B1** schema, seed, storefront | green | 15 items; no public route creates an order |
| **B2** the nine doors | green | conformance passes against the **real** store; 50-on-5 under Postgres |
| **B3** shop-ops admin | **amber** | refunds route through the sidecar; "auth verified live" was not — every admin form was refused as cross-site until 09-21 |
| **B4** notifications | green | one per (order, kind); erased contact skips with a reason |
| **C1–C5** buyer chat | green | air-gap is a build break; the agent computes nothing and now *does* something — composer, MCP client, consent prompts, and a real model over OpenRouter (09-21) |

**538 Python tests, 36 merchant-site, 75 chat, 8 live purchase.** `ruff`, `mypy --strict`,
`svelte-check` on both roots, and four guardrails clean.

> **Green here once meant only that the phase's tests passed.** Two 09-21
> entries below record what driving the running system found: eleven defects
> under gates already marked green, then a money path with no HTTP entry point
> at all. Both are fixed, and `make demo` now walks a whole purchase — search to
> signed receipt — against the running stack, so a green row above is backed by
> something that actually ran.

## OPEN — decisions taken by ladder step 4

- **h0** — Destination/Contact field shapes; scopes for `order-status`,
  `cancel-order`, `request-refund`; canonical PII bytes. The first is preimage
  bytes and is only reversible by regenerating the vectors.
- **A3** — `aiosqlite` added (an addition, not an upgrade); check 8 `tags`
  passes unconditionally until `/agentic` gives it something to enforce, and
  stays in the order because removing it would change Transcript bytes.
- **A4** — the approve page and `/agent/*` were unit-tested here and mounted
  later; both are now live and asserted end to end.
- **A6** — `fixed_order_salt_hex` and `order_id_hint` on the fake, so a golden
  replay produces comparable bytes. Both are test-only and documented as such.

## BLOCKED

None. No gate required changing a frozen §6 contract.

## OPEN — what is still absent, and honestly so

Closed 09-21: the money path has an HTTP entry point, all four protocols are
mounted over one core, and the chat can talk to a shop with a real model.

**All four remaining gaps closed 09-21** — see the entry below for what each one
turned out to need. Every one is exercised against the running stack, not only
by a suite.

Still open and named rather than forgotten:

- **`prompts` is returned to the page and not written into the Transcript.** The
  ceremony *name* is (`enrollment` vs `assertion`), which is what distinguishes
  one prompt from two, so the count is derivable rather than recorded. Adding it
  would change Transcript bytes, which are frozen.
- ~~**The refund queue is in memory**~~ — closed 09-21. Every store the money
  path depends on is a table now, and a restart mid-purchase is drilled against
  the running stack rather than reasoned about.
- **Razorpay's four network calls are implemented** as of 09-21, against the
  documented API and its documented envelopes. Nothing has still been run
  against Razorpay itself, which is a different and smaller gap than it was.
- **F12's credit-note half** and the Registrar question in ADR-0025 still need
  counsel.

## Cuts

Taken up front, per §12, and none of them reopened: **cut 2** (attribution and
the full health panel), **cut 4a** (admin trimmed to the operating path), **cut
5** (no direct cart or checkout). Each is absent rather than half-built, and
`cut 5` is asserted by a test that walks the route tree.

## What the next person should know

1. **Three implementations of §16.11 agree and share no module** — the
   merchant's in TypeScript, the fake's in Python, and the Gate's check. That is
   the property the money core rests on. Do not refactor them into one.
2. **The golden `cart_hash` vectors and the core Transcript bytes are frozen.**
   If a change makes them fail, the change is wrong.
3. **The guardrails have planted-violation tests.** If one starts failing on
   correct code, fix the guardrail rather than deleting it — that happened
   twice here and both times the rule was directionally right and imprecise.
4. **F12's credit-note half is still open** and needs counsel, as does the
   Registrar question in ADR-0025.
5. **The passkey challenge is the binding.** It is `sha256` over the cart hash,
   amount, currency, domain and expiry — not a nonce with a remembered
   association. If a change makes a ceremony verify against a basket it should
   not, the change is wrong.
6. **Three writers move an order's status**: the tap, the sweeper and the
   Merchant's own actions. They serialize on door 8's `order_id:attempt` key and
   nowhere else, so the Merchant is the arbiter — the sweeper re-reads and
   corrects itself rather than holding a lock.

---

## 2026-09-21 · Four things an MVP needs, and the restart that used to cost money

The morning report's "OPEN — what is still absent" list was about features. This
entry is about the four things between a working demo and something a merchant
who is not us could deploy: durability, an adoption path, a way to check a
second implementation, and a deploy story. Plus Razorpay's four network calls,
which had been `NotImplementedError` since A3.

### The restart, which is the only one that cost money

Six dicts held the sidecar's working state. The expensive one is the checkout:
a Consumer taps, the process is replaced, and the Provider's callback arrives at
a sidecar that has never seen the order. `complete` answered **"No checkout is
waiting on that payment"** — with the money already moved, the stock still held,
and the order sitting `confirmed` until the sweeper expired it.

**Driven against the running stack both ways.** On `22c4785`, a checkout started
before `docker compose restart sidecar` answers 404 at its own approve link. On
`main`, the same drill taps, pays, and reads back a VALID receipt — and the
receipt survives a second restart after sealing.

Two things the port improved rather than merely persisted:

- **Single-use is the UPDATE, not the check above it.** `spend_tap` is now
  `UPDATE ... WHERE spent = false` and the loser sees `rowcount == 0`. The dict
  version marked `spent` after reading it, which two concurrent taps both pass.
- **One open refund ask per order is a partial unique index**, for the same
  reason: two asks racing both read an empty queue.

A bug the port introduced and a test caught before it shipped: folding the
passkey challenge and the completed ceremony into one row made verifying delete
the row a moment before the agreement was written to it, so **every passkey tap
silently fell back to `upi-pin`**. Two tables now, with the two lifetimes they
always had.

`Pending` is no longer a shared mutable object. Callers read the row, so
`result.checkout` is the copy the tap actually used — several tests were
asserting on a snapshot from before the call they were testing.

### WooCommerce, because ten doors is a build and nobody builds

"Makes any Merchant site transactable" had a sample size of one, and it was a
storefront written for this repository. `integrations/woocommerce/` is a plugin:
WooCommerce stays the book of record, an agent's order appears in WooCommerce →
Orders like any other, and the plugin decides nothing.

Three places it refuses rather than accommodates. No SKU, not published. Stock
management off, not published — "in stock" with no number is exactly the null
this spec refuses. A shipping method that needs a cart to price itself is
refused rather than guessed at. All three are counted by name on the admin
screen, because they are invisible in WooCommerce's own.

The reserve goes to `postmeta` directly with `WHERE CAST(meta_value AS SIGNED)
>= qty`, because `wc_update_product_stock` decrements with no floor and would
take stock to -1.

Status is **derived from WooCommerce**, not from our own meta: eight trait
statuses map onto WooCommerce's and `cancelled`/`expired` collide, so meta
breaks that one tie and nothing else. A shopkeeper who completes an order in the
admin has changed the truth.

§16.11 now has a fourth independent implementation and the HMAC preimage a
third, sharing no code on purpose — so `scripts/make_trait_vectors.py` writes
down the inputs and the exact paise, `make guardrails` fails if Python drifts,
and `make woo` fails if PHP does. Nine pricing cases and six signing cases, all
passing first run, odd-paise SGST and three-way apportionment included.

### `openstore-conform`, so nobody has to take our word for it

The conformance suite ran against two implementations that happen to agree. The
CLI points anywhere, reads door 1, and picks its own subjects — no hardcoded
SKU, because a fixed one passes against the store it was written for and fails
against every other.

**It passes 18/18 against the TypeScript storefront unmodified**, which is the
evidence that mattered: it was written against the fake.

It writes to the store and gives every hold back — asserted, because a run that
quietly consumed a shop's inventory would be the last one anybody allowed.
`--read-only` is the subset that writes nothing. And because a conformance suite
that has only seen conforming stores is untested, four real bugs are planted one
at a time and each must produce a named failure.

### Deploy, and a backup somebody has actually restored

`core/db.py` said a real deployment migrates with alembic. There was no alembic
directory. Generating the first migration found that the three passkey tables
were registered on the shared `MetaData` only by whichever import ran first —
`create_all` created them by luck, and the migration would have left them out.

**Demo creates its schema; a deploy that takes money refuses to start against
one it has not migrated.** A process that changes its schema as a side effect of
starting is one whose rollback leaves a table the old code cannot read.

`SIDECAR_KEY_EXPORT_PATH` was a declared, documented setting wired to nothing —
the same bug as the SSRF allowlist and the OAuth credentials before it, and the
worst one to have: a deploy could believe it had a backup of the shop's identity
and have none. `openstore-keys check --against` is the other half, because a
backup nobody has restored is a hope. The failure it exists to find is a backup
taken before a rotation, which looks fine until every receipt sealed since
verifies against nothing.

### Razorpay

Implemented over the Payment Links API. **No money has moved through it** — what
is asserted is that it sends what the API reference documents and reads what its
documented responses contain, with the test envelopes copied from those examples
field for field.

Reading the docs found a defect waiting for the first live deploy: Razorpay
refuses an `expire_by` that is not **more than** fifteen minutes out, and §16.7's
payment window is exactly fifteen minutes. Every link this sidecar asked for
would have been refused at creation.

Four more places a plausible implementation is wrong, each with a test:
`partially_paid` is not paid; `amount_paid` and never `amount`; a refund is
issued against the payment id and keyed by the refund's own id; and a duplicate
`reference_id` is the **recovery path** rather than an error — it means a
previous attempt created the link and crashed before storing it.

The `razorpay` SDK dependency, declared and never imported, is gone: it is
synchronous and would block the event loop for the length of every round trip.

### What this still is not

- **No money has moved through a real rail.** That is the next thing, and it is
  also the first real test of ADR-0021's ECO/TCS boundary.
- **The WooCommerce plugin has not run against a live WooCommerce.** Its PHP
  parses, its arithmetic and its signatures agree with the sidecar's to the byte,
  and that is a different claim from "installed and working".
- **Nothing sweeps the checkouts table.** Bounded by order volume, never deleted.
- **F12's credit-note half** and the Registrar question in ADR-0025 still need
  counsel, unchanged.

---

## 2026-09-21 · The four open gaps, closed — and one bug only the running shop could show

The morning report listed four things absent "rather than half-built". All four
are now built, each with the property that made it hard written into the code
rather than into a comment about the code.

### The expiry sweeper — `expires_at` now means something

`gate/lifecycle.py` had known what to do with an order past its deadline since
A4 and **nothing ever called it**. Pending checkouts lived in memory, the 24h
window was a sentence in the spec, and an abandoned tap held Merchant stock until
the process restarted — an availability hole any self-registered stranger could
open at will, which is precisely what the lifecycle module's own docstring warns
about.

`sidecar/sweeper.py` runs it every 60s (§16.7). Three deadlines, three different
acts: `pending` at 24h expires and returns nothing (nothing was held),
`confirmed` prepaid releases stock through door 5 and closes the Ledger hold
before the status moves, and a late COD parcel alerts in `/agentic` without being
cancelled.

**A test caught a real defect before the commit.** The first version acted on its
in-memory status, and a stale copy applies the *wrong deadline*: a `confirmed`
order swept as `pending` expires **without releasing the stock it is holding**,
which is the exact failure the loop exists to prevent. The pass now confirms
Merchant truth before acting — one extra door-8 read, and only for an order
already past a deadline. `Pending` grew `created_at`, `confirmed_at` and
`link_expires_at`, because one `expiry_utc` string cannot carry three deadlines
measured from two moments, and the hold now takes the Provider's own link
lifetime as its ceiling rather than assuming the default window.

### The Provider's callback — mounted, and the demo now uses it

`provider/webhooks.py` had held the HMAC check, the `event_id` dedupe and the
reconcile since A3 with **nothing mounted in front of them**. The only way money
ever finished moving was the demo's own Approve button calling `complete()`
directly, which is the one shape a real Provider never uses.

`POST /provider/webhook` is public and verified by HMAC on the raw bytes. The
load-bearing sentence is that **the event is a trigger, never an instruction**:
the body yields an event id and a link id and nothing else, and the handler then
asks the Provider what it holds. A signed body claiming `paid` on an unsettled
order *fails* that order rather than capturing — asserted directly, because that
is the whole posture.

The demo's Approve button now builds the body and HMAC the fake rail would POST
and hands them to the same `deliver` the public route calls, so every `make demo`
exercises signature verification, the dedupe and the state re-read. `WebhookEvent`
+ `read_webhook` on the Provider trait is abstract rather than defaulted: a
guessed field name would silently yield an empty link id, which the route would
read as "no checkout is waiting" rather than as the adapter bug it is.

`PROVIDER_WEBHOOK_SECRET` is separate from `RAZORPAY_WEBHOOK_SECRET` because the
latter is issued by Razorpay's own dashboard. **No secret means every webhook is
refused**, demo mode included — there is no unauthenticated callback path.

### `request-refund` — an ask, and a queue to put it in

The tool had been in the closed set since hour 0 and refused as unwired, which
was honest while there was nowhere for a request to go. The reason it could not
simply be implemented is the shape of the product: a refund moves money and
belongs to the Merchant, and **an agent that could refund could move money out of
a shop it holds no credential for**.

So the tool records an ask, and `/agentic` → Refunds shows it. A request carries
no amount (the Merchant decides what to give back), there is one *open* request
per order (a queue an agent can flood is the Merchant's attention spent by
somebody else), and only an order with money in it can be asked about — before
`paid` the instrument is a cancellation.

`/agentic/refund` now closes the request that prompted it, and a new
`/agentic/refund-decline` closes one without paying it and **writes no Ledger
entry** — refusing to refund moves no money, and a `REVERSAL` for a refund that
never happened would be a false entry in an append-only book. Without that half,
an unhonoured request stays open forever and the board stops being work to do.

### The passkey ceremony — a challenge that *is* the binding

`AuthorityKind.PASSKEY` had been in the closed set since hour 0 with no module
behind it, so the strongest claim the system can make was enum-level only.

The challenge is **not a nonce**. It is `sha256` over the `cart_hash`, the exact
amount, the currency, the Merchant domain and the expiry, so a signature over a
swapped basket is a signature over a different challenge and fails. The binding
is a property of the cryptography rather than of a server-side association
nobody re-checks — which is what the ordinary build of this would have been.

Under `fmt: none` nothing in a registration response proves agreement to a
basket, so the server asks for an immediate assertion over the **same** challenge
and records `ceremony = assertion`. Two prompts, named, and the count comes from
the server's own fallback signal rather than from what the client claims. User
verification is required and not preferred: "somebody touched a key" is not the
human permission a spend rests on.

A verified ceremony is consumed by the tap it was taken over — two taps of one
order are two agreements — and changes the *kind*, never the money: `passkey`
prepaid, `confirmed-intent`/`passkey` on cash. On the cash path the credential id
becomes the `consumer_id` handle source, which is the one path with no payer
handle at all, and `Pending` records which derivation was used so a verifier is
never left guessing.

Verification is `py_webauthn`, already a declared dependency — no hand-rolled
CBOR or COSE. The tests drive a software authenticator that really signs.

### Exercised live, and one bug that found

`## OPEN — A3`'s lesson held again. With all four green in the suite, driving the
running shop produced a **500 on `add-line`**: `Basket.add` accumulated
quantities by mutating a frozen `Line`, so a second `add-line` for a SKU already
in the basket raised `frozen_instance`. `Line` is frozen for a good reason — it
is a value in the `cart_hash` preimage — so the fix replaces rather than mutates.
Every suite was green over that.

Against the running stack afterwards: a whole passkey purchase from search to a
sealed receipt (`passkey`, `cart`/`payer-device`, `ceremony: assertion`, verifier
`VALID`), the callback route refusing an unsigned POST `signature-invalid`/401
through the proxy, the refunds board showing a real agent's ask, and the sweeper
running clean. **538 Python tests**, `ruff`, `mypy --strict`, and all four
guardrails green.

## OPEN — refund queue

`RefundRequestState` {`requested`, `approved`, `declined`} was added to
`core/codes.py` and SPECS/PLAN.md §6.4 in one commit under §0's standing rule 1.
It tracks the *ask* and never the money — the movement is `LedgerKind.REFUND` and
the order's own `refunded` status, and conflating the two would let a request look
like money the shop has already sent.

`PROVIDER_WEBHOOK_SECRET` was added as a second named secret rather than reusing
`RAZORPAY_WEBHOOK_SECRET` for every adapter (ladder step 4, conservative): a
deploy that pasted Razorpay's dashboard secret into a generic variable would
verify callbacks with a secret the Provider never agreed to.

---

## 2026-09-21 · The money path gets an HTTP entry point, and the demo becomes a demo

Everything under the money path already worked. The Gate ran its twelve checks
against the real store, the Ledger kept both invariants in real Postgres,
`settle` resolved a deferred Authority, `BundleBuilder` sealed a receipt the
verifier called VALID. **None of it had a caller.** An agent could not buy
anything, the approve page's one button posted to a route that did not exist,
and the chat had no way to send a message.

Exercised first, every component, over HTTP or by driving its real API against
the running containers — not by reading it and not by running its unit tests.
That is how each of the following was found.

### The money path is now reachable

`checkout.py` is the wiring and nothing more: it computes no price, invents no
status, and makes no decision the Gate has not made. Three steps, one HTTP
request each — `start` (door 7 creates the order, door 9 quotes it, the tap
token is minted over that exact hash), `tap` (token spent once, Gate for real,
door 3 holds stock, Ledger takes the hold, Provider issues a link), `complete`
(settle, door 4 commits, receipt sealed).

`order_salt` is never held between requests (§6.3a). Door 7 keys on
`cart_id:attempt`, so each step that needs the salt replays that call and drops
it — verified live that a replay returns the same order and the same salt.

`/agent/mcp` now runs its tools instead of answering `accepted: true`. `search`
reads door 1, `add-line` validates against the real catalogue, every total comes
from door 9. No exact stock reaches an agent: door 2's integers go through
`trait.buckets` first.

**Proof it works**: the §16.11 worked example, reproduced end to end through the
running system — tote + gift-wrap + charm-bar seat to Mumbai, total `259700`,
`IGST 15827 / CGST 11894 / SGST 11894`, `RESERVE` then `CAPTURE`, receipt
VALID. Those are the numbers pinned in the spec, arrived at by the product
rather than by a fixture.

### Four defects the exercising found, three of them mine

- **The Authority bound a different hash than the Gate computed.** `start` built
  the `cart_hash` with a zero placeholder price per line; the Gate uses the
  attested price. `upi-pin` defers before the comparison, so it looked fine —
  and would have failed the moment anyone used a passkey.
- **Door 8 silently replayed.** `orders_set_status` keys on `order_id:attempt`,
  and I used the default attempt for both `confirmed` and `paid`. The order
  stopped at `confirmed` while the code believed it said `paid`. Keyed by target
  status now, so a *retry* still replays and a *different transition* does not.
- **A second checkout reused the first order.** The basket persisted per agent,
  so `start-checkout` twice meant one `cart_id` twice — and door 7 idempotently
  returned the already-paid order.
- **`ProfileFetcher` refused the demo's own chat**, `Admission` had no clients,
  the console's Keys/Agents/Receipts boards read lists nothing wrote to. All the
  same shape: configuration that reached nothing.

### The other three protocols existed only in tests

The card has advertised `["mcp", "ucp", "ap2", "acp"]` since it was written and
only MCP had an endpoint. `ucp.py`, `acp.py` and `ap2.py` produced envelopes for
the golden replays and for nothing else — and those replays build the bundle
directly, which is why A6 was green.

All four are mounted now, as translators over **one** core: `/agent/ucp/checkout`,
`/agent/acp/checkout_sessions` (+ `/complete`, which is the documented refusal —
a delegated credential is exactly the authority this Merchant does not grant),
and `/agent/ap2/checkout` (a Merchant-signed Checkout Mandate; the human tap
still happens, because a mandate is evidence of intent and not a credential).
An ACP-originated checkout completes through the same tap and seals the same
receipt.

### The chat is a chat

It had no form, no action, and `src/lib/mcp/` was an empty directory.

It now has a composer, an MCP client that self-registers with the shop, a
consent prompt that blocks the spend steps from standing approval, product
photographs, the shop's own quote rendered line for line, and the approve
handoff. **And a real model**: `CHAT_MODEL_DRIVER=openrouter` with an
`OPENROUTER_API_KEY` gives native tool-calling over any provider OpenRouter
fronts. The model proposes; deterministic code still validates and the sidecar
still refuses, so a wrong model costs a refusal rather than a wrong charge.

Watched live, `openai/gpt-oss-20b` searched, read the variants, asked for the
address rather than inventing one, called `start-checkout` too early, read the
refusal, called `choose-fulfillment` properly, and completed a purchase ending
in a VALID receipt.

`scripted` is still the default and still what CI runs. A demo that needs
somebody's API key to pass its own tests is one that fails on the next laptop.

### Photographs

`media` had been a column on both catalogue tables since the schema was written,
door 1 carried it, and `core/feed.py` emitted it as `image_link` — every layer
ready, and the seed put nothing in it. 35 photographs scraped from
spoiledduckie.co.in's WooCommerce Store API, resized to web size (62 MB → 1.3 MB),
seeded as Merchant truth. The shop grid, the product gallery, the agent feed and
the chat's result cards all carry them now.

### The gate that would have caught all of this

`tests/test_purchase_live.py`, wired into `make demo`. It imports none of the
money path — it *uses* it, through the URLs an agent and a Consumer use: search
to signed receipt, the twelve checks in the Transcript, `tapped.cart_hash ==
transcript.cart_hash`, the order reaching `paid` at the Merchant, a spent tap
token refused on replay, and one total surviving all four protocol envelopes.

It also buys things, so it looks up an in-stock SKU rather than naming one — a
fixed SKU stops working once the suite has bought enough of it, which happened.
And it takes the allowlisted admission route, because "reputation buys
throughput only" is only true if something uses the throughput.

**470 Python, 36 merchant-site, 75 chat, 7 live purchase.** `make up` clean,
`make demo` green.

### Still not done

- `request-refund` refuses as unwired. A refund moves money and belongs to the
  Merchant; an agent-initiated request needs a queue the console shows, and
  inventing a silent one would be worse than the refusal.
- There is no `passkey.py`. `confirmed-intent` and `upi-pin` are live; the
  passkey ceremony is enum-level only.
- No webhook route, so the Provider's own callback path is unexercised —
  `complete` is driven by the demo rail's page instead.
- Pending checkouts live in memory and never expire. The 24h `pending` window is
  stated and not enforced by a sweep.

---

## 2026-09-21 · Exercising the running system — eleven defects every suite called green

Every phase below was marked green. The demo could not add its own shop, log
into its own admin, register an agent, or render in its own typefaces, and the
sidecar had no signing key at all. Nothing regressed: none of it had ever
worked. The gates were met by unit tests that construct the object under test by
hand, and **every defect here lives in the seam between a component and the
thing meant to call it** — which is exactly where a component test cannot look.

The trigger was a user pasting a shop URL into the chat and getting
`Cross-site POST form submissions are forbidden`.

### Fixed — the seam, seven times

- **CSRF on every form, both node surfaces.** `adapter-node` assumes `https`
  when no `PROTOCOL_HEADER` is named, so it computed `https://<host>` while the
  browser posted from `http://<host>`. Every form on the shop and the chat was
  refused. `PROTOCOL_HEADER: x-forwarded-proto` on both.
- **The admin could not log in.** Same bug, other half: the edge 404s `/admin`,
  so it is reachable only on the loopback port, where there is no forwarded
  header to honour. Its own origins are named in `csrf.trustedOrigins`.
- **The card and the chat disagreed about where keys live.** The chat required
  an inline `jwks`; the card publishes `endpoints.jwks` (SPEC §V1). The chat's
  suite built its own card fixtures, so it passed while agreeing with nothing —
  every real card was refused. The fetcher now reads the two documents, both
  through the same SSRF check, and refuses a card whose keys live on another
  host or that claims a domain it was not served from.
- **The card advertised `https://<domain>` regardless of configuration**,
  handing an agent seven endpoints that do not answer on a plain-http deploy.
  The origin is passed in now, defaulting to `https://` when unset. Same defect
  fixed at the MCP `approve_url` and the feed `base_url`.
- **The sidecar had no signing key.** `Keyring` was implemented, tested, and
  never constructed outside a test, so the live JWKS was `{"keys": []}` and
  nothing could be sealed. Keys now load or enroll at boot from
  `SIDECAR_SIGNING_KEY_PATH` on a volume, encrypted when a passphrase is set,
  refusing rather than guessing on a wrong passphrase, another merchant's
  keyfile, a damaged file, or an unknown version — each of which would otherwise
  mint a second identity over a live one. `/readyz` now reports `signing_key`,
  because an empty JWKS was invisible until an agent refused the shop for
  carrying no keys.
- **The SSRF dev allowlist reached nothing.** It was reported in `/readyz` and
  in the console banner while `ProfileFetcher` held an empty tuple, so the
  sidecar refused the one host the exception exists for and **no agent could
  register at all**. Reporting a policy is not applying it.
- **`OAUTH_CLIENT_ID`/`SECRET` never reached `Admission`**, so the second
  admission route refused every client.

Also: the console's Keys tab read a list nothing populated; the chat published
no Agent Profile though `agentProfile()` was written and documented as the thing
it publishes; and all nine typefaces 404'd on both surfaces because
`design/fonts/` never reached either `static/`. No test fetches a font.

### Still broken — the money path has no HTTP entry point

- **`run_core` has zero production callers.** The one core all four protocols
  must pass through is reached only by tests. Same for `Gate(`, `Ledger(`,
  `.seal(`, `ReceiptStore.put`, `append_moved_entry`, `derive_consumer_id`.
- **`/agent/mcp` is a stub.** Live, as a registered agent, every tool returns
  `{"tool": ..., "accepted": true}` and does nothing — `search` returns nothing
  from a catalogue door 1 serves 12 groups from. Only `place-order` is real.
- **There is no tap endpoint.** `/agentic/approve` is GET-only; POST is 405. No
  spend can complete, so no order can exist, so `/receipt/{id}` is always 404.
- **The chat has no send path** — no form, no action; `src/lib/mcp/` is an empty
  directory.
- **The sidecar never opens its database.** `core/db.py` is imported by nothing.
  `SIDECAR_DATABASE_URL`, `PAYMENT_PROVIDER`, `RAZORPAY_*`, `WEBAUTHN_RP_ID` and
  `SIDECAR_KEY_EXPORT_PATH` (ADR-0014's enrollment export) are read by nothing,
  and there is no `passkey.py`.

### What the gates should have been

Three test files were added for the seam rather than the parts:
`tests/test_boot_wiring.py` runs the real lifespan and asserts what the surface
actually received; `tests/test_keyfile.py` asserts a key survives a restart;
`tests/GOLDEN/card/` is generated from the sidecar's own builders and read by
**both** suites, so the card contract cannot drift on one side alone.

A gate that can be met without the process running is not a gate. `make demo`
should grow a live pass that registers an agent, calls a tool, and asserts the
result is not `accepted: true`.

**469 Python, 36 merchant-site, 75 chat.** `ruff`, `mypy --strict`,
`svelte-check` on both roots, four guardrails, and `make up` clean.

---

## 2026-09-21 · The install gate, and three bugs only a clean run could find

`make down && make up` on an empty machine reaches a working shop with **zero hand-edited files**, and `make demo` passes 12/12 against it. **445 tests.**

### A deadlock in my own install gate

`make up` waited for `/` to return 200 before seeding. `/` cannot return 200 until the catalogue is seeded. So the wait always ran its full sixty iterations and then seeded anyway — the gate "passed" only because the timeout expired. It waits on `/healthz` now, which needs a database connection and nothing more, and it **checks `/` renders after seeding** so a broken shop fails loudly instead of quietly.

Worth naming as a class: a readiness check that waits for a *product* of the step it precedes will always look like a slow success.

### Two configuration bugs the unit tests could not see

- **`TRAIT_BASE_URL` was `http://store:3000/trait`**, and the client appends `/trait/<door>` itself — producing `/trait/trait/catalog.read`. Every door refused, and the shop looked *empty* rather than broken, which is the worst failure mode there is.
- **The HMAC secret was set on the store and not the sidecar**, so the sidecar had nothing to sign with.

Both now **refuse at boot** with an error that says what is wrong and why, and both have tests. A misconfiguration that produces an empty page instead of an error is one somebody debugs for an hour.

### The feed was mounted and never connected

It returned `{"items": []}` and a 200. It reads Merchant truth fresh through doors 1 and 2 now, and the live document shows every property the spec asks for: 15 items, variants grouped by `item_group_id`, `SD-TOTE-BLK-L` absent because it was never made, `low-stock` folded into the reader's `in_stock`, and **no exact count anywhere in the document**.

`on_event("startup")` is deprecated; the wiring is a `lifespan` now and **logs what it wired**, because the failure mode of silent wiring is a shop that looks empty.

---

## 2026-09-21 · The agent surface and the approve page

**442 Python tests.** A6 built four translators and never mounted the request path; this is that path. `/.well-known/*`, `/agent/*` and `/agentic/approve` are live and served through Caddy.

### The card states its refusals up front

A stranger fetching `/.well-known/agent-commerce.json` learns, before it writes a line of integration code, that completion is redirect-only, that delegated payment credentials are **refused** rather than unimplemented, which methods this Merchant actually enabled, and — per protocol — **which completion step is refused and why**, with `ACP 2026-04-17` named.

That is kinder than finding out at the last step, and it is the difference between a deviation and a surprise.

### `place-order` returns a link, never an order

The one route on this surface that could betray the whole posture is handled explicitly and tested explicitly: the response carries an `approve_url`, carries **no** `order_id`, and says in its own note that the agent cannot complete the purchase itself.

### The approve page is the page of record

Every figure on it comes from the Merchant-signed Quote — the tote line with gift-wrap folded in, the charm-bar seat, IGST and CGST/SGST separately, the delivery ETA as a **day count**. The page says in as many words that nothing on it was calculated by the agent that built the basket.

It offers the private-code field (the code never transits the agent), shows only the methods this Merchant enabled, and carries a live countdown — because "this expires" with no number is a sentence nobody acts on.

It is reachable **without the Merchant session**, and a test asserts the request carries no cookie. A Consumer approving a spend is not the Merchant.

### One design note recorded rather than discovered later

`FastAPI` refused the approve route because it returns HTML on the happy path and a JSON refusal envelope otherwise, and it cannot build one response model from both. `response_model=None` is the fix, with a comment saying why — the next person to add a dual-shape route will hit the same thing.

### `make up`

One command: build, start, wait for the shop, seed, then print the four URLs and the `*.localhost` warning from §10.1 — browsers resolve it to loopback and containers do not, which the plan calls the single most likely way to lose an afternoon.

---

## 2026-09-21 · Mount and end-to-end — five services, one origin

**The whole stack runs.** `docker compose up -d` brings up caddy, sidecar, store, buyer-chat and postgres; the merchant seeds through the real flow; and the sidecar's conformance suite passes **12/12 against the containerised store**, including 50 concurrent reserves on 5 units against real Postgres.

### What the edge serves, and what it refuses

| path | answer |
|---|---|
| `/`, `/p/tote`, `/shop`, `/lookup` | store, 200 |
| `/agentic/*`, `/receipt/<id>`, `/.well-known/*` | sidecar, 200 |
| `chat.localhost` | chat, 200 |
| `/trait/*` | **404** |
| `/admin*` | **404** |

**A real finding during integration: the nine doors answered on the public origin.** HMAC refused them with a 401, so nothing was exploitable — but they were *routable*, and §6.1 says private network only. Defence in depth matters exactly here: a door that cannot be reached from the edge stays safe if the HMAC secret ever leaks. The edge refuses both the trait and the shop's admin now, and CI asserts all three paths return 404 while the three public surfaces answer.

### Four integration defects, none visible from unit tests

- **`corepack` is gone from Node 25+.** Both Dockerfiles used it; pnpm is installed explicitly and pinned to the version that wrote the lockfile.
- **The build context was 173 MB** because `node_modules` was being uploaded. A `.dockerignore` was missing entirely.
- **`@import 'tailwindcss'` inside `design/tokens.css` cannot resolve.** The directory sits outside every package, so there is no `node_modules` to resolve against. It worked in local dev and failed at image build — the worst place to find out. The import is lifted into each app's own `app.css`, which is how a Tailwind v4 app is structured anyway, and `design/README.md` records the one change from the portfolio's original.
- **The chat listened on 3000.** `adapter-node` defaults to `$PORT` and the compose file never set one, so both SvelteKit services claimed the same port and Caddy 502'd the chat. Ports are part of §16.1's contract and are now set explicitly rather than inherited from a default.

Also: the images now mirror the repo layout (`/workspace/demo/...` beside `/workspace/design`) so the same relative import works in dev and in the image, and the store image ships `src/lib` because the seed script running inside it imports from there — shipping a schema without the code that applies it is how `docker compose exec store seed` fails at 2am.

### CI now covers all of it

Three jobs became five: guardrails, checks, a **surfaces** matrix running `svelte-check` and `vitest` on both SvelteKit roots, and a compose job that brings the whole stack up, seeds it, asserts the edge's public/private split, and runs the conformance suite against the containerised store.

---

## 2026-09-21 · A4c + C1–C5 — the buyer chat

**68 chat tests, 420 Python, 36 merchant-site.** `svelte-check` clean on both SvelteKit roots, and the import firewall passes with all three roots live.

### A4c: one signature base, two languages, one fixture

`tests/GOLDEN/rfc9421/vectors.json` is generated by the sidecar's signer and asserted by **both** suites — the Python verifier reproduces it, and the TypeScript signer reproduces the base byte-for-byte including a Devanagari body. A drift on either end now fails on both, which is the entire reason the plan put the signer and the verifier in one sitting.

The firewall is untouched: these files live in `demo/buyer-chat/`, import nothing from the sidecar, and reach it over HTTP. A person is not a root.

### The air-gap is a build break, not a sentence

`tests/no-pii-in-session.test.ts` reads the live SQLite schema and fails if any column could hold a Destination, a Contact Point, a payer handle or a total. It also asserts the table list exactly, so a new table has to be a decision. Destination and Contact Point are held in memory for one checkout and posted to the sidecar — an agent that persists them has become a place PII leaks from.

### The agent computes nothing, and that is tested

`assertVerbatim` checks every rendered amount against the set of figures the Merchant actually signed, and throws on any number that is not among them. The total is **taken from the Quote**, never summed from the rows — if the chat summed, a Merchant whose own sums were wrong would be silently corrected and the Consumer would approve a number nobody signed.

### Standing approval never covers a spend

`requiresFreshConsent` returns true for `place-order`, `start-checkout` and `add-line` **even when the Consumer has said "always" to everything**. Only `search`, `read-item` and `order-status` can be standing. The spend modal's copy grants a **scope and never an amount**, because no total exists at that point and a modal that reads like an amount approval teaches the Consumer to click through the tap that is one.

### Two real defects the tests found

- **An allowlist entry with a default port never matched.** `new URL()` strips `:80`, so `OPENSTORE_DEV_PROFILE_HOSTS=host:80` silently failed to match `http://host:80` — a miss that reads as "the allowlist is broken". Both sides are normalised through `URL` now.
- **`fetchCard` could not take an injected resolver**, so its own tests hit real DNS. Fixed at the signature rather than by mocking the network.

### TOFU that survives a legitimate rotation

An unknown `kid` is a **rotation to be confirmed**, not an immediate hijack: the chat refetches the pinned domain's JWKS exactly once, and only a key absent from *that* is treated as a hijack. Calling every new key a hijack would break ADR-0014's additive rotation and every Merchant who rotates after an incident; calling every new key a rotation would be no pinning at all.

### `code-invalid` says only that the code did not apply

A test asserts the copy contains no "expired", "used", "unknown" or "exists". A chat that explains why a code failed is a code oracle with a friendly face — and the sidecar went to the trouble of making every wrong code refuse identically, with no timing tell, which the chat could undo in one helpful sentence.

### C5: the smoothness law as assertions

One tap per spend, one Allow-once per spend, no re-search after the tap, no second sign-in, and resumed state exact **on both sides**. Each has a test that plants the violation. Context loss is a red build.

---

## 2026-09-21 · B3 + B4 — shop-ops admin, notifications, merchant money actions

**418 Python tests, 36 vitest, svelte-check clean.** Admin auth verified live: unauthenticated `/admin` → 303 to login, signed in → 200 on every page, wrong password mints **no session cookie**, and an unknown user gets the same message as a wrong password.

### Access control first, because of what this surface holds

The admin holds the **Refund button**, which moves real money through the sidecar. An unauthenticated admin is a refund endpoint for anyone who can reach the container, so the session check is a `+layout.server.ts` guard that every route under `/admin` inherits — a new page cannot be added without it, which is the difference between access control and a habit.

Login hashes even when the user does not exist, so a missing account and a wrong password take the same time. Otherwise the form enumerates operators.

### The four money actions go through the sidecar, never the order row

Refund, shop-reject, COD collection and RTO are HMAC calls to `/agentic/*`. The sidecar holds the `RESERVE`, so a Merchant-side write would strand a Ledger hold, and routing through door 8 serialises shop-reject against a Consumer tap and the expiry sweep on one key. Seven tests cover them, including that an RTO writes **zero** Ledger entries and that rejecting a `paid` order refuses `cancel-not-allowed` — past that point a refund is the instrument, not a cancellation.

### The invoice number, and the boundary that gets it wrong

`assignInvoiceNumber` uses one `INSERT … ON CONFLICT DO UPDATE … RETURNING`, so two concurrent dispatch calls cannot read the same value. It refuses on a terminal-negative status and returns the existing number on a second call — never re-derived, never reused.

The financial year is computed in **IST**, and the test names the case a UTC implementation gets wrong: 00:15 IST on 1 April is 18:45 UTC on 31 March, and a UTC bucket files the new year's first invoice into the year that just closed. The whole five-and-a-half-hour window is asserted, in both directions, around the exact 18:30 UTC boundary.

### Two test-scope corrections, both real distinctions

My "no second money path" assertions were too broad and the test caught it twice:

- **The admin legitimately sees exact stock counts.** The exposure rule is about agents and the public, not about the Merchant looking at their own inventory — they are the ones who have to reorder it. The rule now excludes `/admin` and separately asserts the admin sits behind a session.
- **The admin legitimately writes stock.** That is Merchant truth being edited by its owner. The rule now excludes `/admin` for writes and adds a stronger one in its place: **every admin stock write must insert a `stock_moves` row**, because an adjustment nobody can trace is an adjustment nobody can dispute.

Both are cases where the first version of a guardrail was directionally right and imprecise, and the imprecision would have fired on correct code until somebody deleted the check.

### Notifications

One row per (order, kind), so a retry writes nothing — which is what "exactly one notification per status change" actually requires. **Dispatch is not a status change**, so the shipped notice needs that guard rather than inheriting one. An erased Contact Point skips the send with a named reason instead of throwing: erasure must never break the system that honoured it.

---

## 2026-09-21 · B1 + B2 — schema, seed, storefront, the nine doors

**B2's DONE WHEN met against the real store, not the fake.** The sidecar's conformance suite runs unmodified against the SvelteKit merchant site over HTTP: **12 live tests pass**, including the 50-concurrent-reserves gate against real Postgres. `svelte-check` clean, 21 vitest tests pass.

### Three implementations of §16.11 now agree

The Merchant's own arithmetic is in TypeScript, the conformance fake's is in Python, and the Gate's `quote-consistent` check is a third. **None of them share a module.** All three reach §16.11's worked example — subtotal 249800, shipping apportioned 3955/5945, IGST 15827, CGST/SGST 11894 each, total 259700 — and the live test asserts it through the real HTTP door.

That is the property the whole design rests on: two implementations that round differently fire `quote-inconsistent` on a *correct* quote, and the only way to know they do not is to build them separately and compare.

### The concurrency gate is a different claim from A3's

A3 ran 50-on-5 against the conformance fake, which proves the sidecar's logic. This runs it against Postgres, which proves `UPDATE stock SET available = available - qty WHERE available >= qty` actually holds under the real isolation level. Exactly 5 succeed and stock lands on zero. Both were required and neither substitutes for the other.

### Cut 5, asserted rather than remembered

No direct cart, no direct checkout, no `site_carts` table. A test walks the route tree and fails if any non-`/trait` file contains `INSERT INTO orders` or `UPDATE stock SET` — so a second money path cannot reappear by accident in six weeks. Another asserts every storefront loader that touches `available` passes it through a bucket function before it reaches the browser.

### Four dependency decisions worth recording

- **argon2 → `scrypt` from the standard library.** argon2 is a native module whose build script pnpm blocks pending interactive approval, which breaks unattended installs. scrypt is memory-hard, needs no build, and is one fewer thing that fails on somebody else's machine.
- **TypeScript pinned to 6.** `svelte-check` refuses TS 7 unless both majors are installed with a `--tsgo` flag; pinning is the honest fix rather than carrying two compilers.
- **Tailwind v4 installed**, because the imported token layer *is* a Tailwind `@theme` block. §4 says the design system is taken whole, so the plugin comes with it rather than the tokens being transcribed.
- **`vitest` config split out of `vite.config.ts`**, where `test` is not a valid key.

### Two Postgres details

`postgres.js` owns transaction boundaries and refuses a literal `BEGIN` inside `unsafe()`, so the schema has none and every statement is `IF NOT EXISTS`. And bigint columns come back as strings by default — without the explicit `types.bigint`, every paise comparison would silently become a string comparison.

### OPEN — B1

- **The seed's `SD-TOTE-BLK-L` guard is a hard error, not a comment.** `validate()` refuses to seed it at all, refuses a non-integer stock or price, and refuses a missing HSN or GST rate on a non-Add-on — a catalogue that loads with a missing GST rate produces a first agent order that cannot be invoiced, and the failure surfaces hours later as an untraceable quote refusal.
- **Postgres is now published on `127.0.0.1:5432`** so the seed script and a human with `psql` can reach it. The services still talk over the compose network.

---

## 2026-09-21 · A7 + A8 — console, feed, logs, install

**411 tests pass**; ruff, mypy --strict and all four guardrails clean. Verified against the live stack through Caddy, not just in-process.

### The design system was imported, not approximated

`design/tokens.css` and eleven font files copied whole from `~/projects/portfolío/frontend` per §4, with `design/README.md` carrying the role rules **and the licensing flag**: two of the four faces ship no licence file, Open Sauce Sans is believed OFL but unverified, and PP Kyoto is a commercial Pangram Pangram face. Fine on this machine, a question the moment anything is recorded or hosted. Written down now rather than discovered later.

The console is the instrument-panel weighting: Iosevka throughout, tabular figures, hairline rules, `--bg-sunken` panels, **no animation**, `prefers-reduced-motion` honoured anyway. A test parses the stylesheet and asserts **no literal colour appears outside a token fallback**, so the dark theme is inherited rather than re-implemented.

### What the cuts actually mean in the UI

Attribution and the full health panel were cut up front to fund A6's four protocols. They are **absent rather than empty** — a tab rendering a blank panel looks broken, an absent one is a decision — and a test asserts `/agentic/attribution` 404s. Health keeps only the two things that could not be dropped: overdue holds and the dev-allowlist banner, both of which appear on **every** tab because the whole point is that somebody sees them.

### Two real bugs, both found by running it rather than reasoning about it

- **A route collision.** The console's `/agentic/{tab}` catch-all was registered before `/agentic/codes`, so the reason-code endpoint answered "no console tab 'codes'". FastAPI matches in registration order; the route now sits above the catch-all with a comment saying why, because the next person to add a console route will hit the same edge.
- **`design/` was not in the Docker image.** The console rendered correctly in tests and served an **unstyled page in the container** — `tokens.css` 404'd through Caddy. Exactly the class of thing that only shows up once it is deployed. The Dockerfile copies it now and the live check passes.

### The feed maps to the reader's enum, not ours

`low-stock` is our bucket and not Merchant Center's, so it folds into `in_stock` through a function with a test rather than a hopeful string. A feed carrying an invented value is rejected by the only reader that matters. Two rules hold without exception and both are asserted: only exposed items appear, and **no exact count ever does**.

### Logs redact by name, not by guess

A regex over values would miss `line1` and flag a SKU, so redaction is a deny list of key names applied at any depth. Transcripts are forensic; logs are operational — a Gate that refuses in 3ms and one that refuses in 3s after a slow Merchant look identical in a Transcript, which is why the log line carries the duration.

### The install gate

`openstore up <domain>` reads `.env.example` for its variable names rather than keeping a second list that would drift — a test asserts the two agree exactly. Every secret comes from `secrets`, the file is written `0600`, the dev allowlist is empty out of the box, and it **refuses to overwrite an existing `.env`**, because regenerating secrets over a live deployment orphans every signature it has made.

`scripts/` became a package so the install gate could be tested rather than only run.

---

## 2026-09-20 · A6 — four protocols, one core

**DONE WHEN met.** **379 tests pass**; ruff, mypy --strict and all four guardrails clean.

### The assertion this phase exists for

`test_the_core_transcript_is_byte_identical_across_all_four` runs the full flow — search → allow → cart → tap → fake-UPI → receipt → verify — four times through four envelopes and byte-compares the core Transcripts. Only `order_id` differs by construction and is normalised out. A companion test asserts the **envelopes actually differ**, because if they were all identical the byte-identity claim would be trivially true and prove nothing. A third asserts byte-identity holds **on the refusal path too**, which is the case where a translator drifting would matter most.

### The spec was vendored, not remembered

`vendor/acp/2026-04-17/openapi.agentic_checkout.yaml` is fetched from the repository and committed. A test parses it and asserts the four paths, the five `operationId`s and the seven top-level schemas §16.12 names are all present. That is what makes "a quarterly revision is noticed rather than silently drifted past" a build failure rather than a hope. The vendored document confirmed §16.12's pinned facts exactly.

### ACP: four operations work, one is refused

`completeCheckoutSession` refuses with `method-not-supported`, names the refused step, returns the approve URL, and **lists the credential fields it ignored** — so an integrator can see their Shared Payment Token was received and deliberately not used, rather than wondering if it was dropped. ADR-0008 and ADR-0013 at the envelope boundary, not an unimplemented gap.

Headers are per-operation: `getCheckoutSession` requires no `Idempotency-Key` because it mutates nothing, and inventing a requirement the spec does not have would be its own kind of non-conformance. A request with no `API-Version` is refused rather than defaulted — guessing against a quarterly spec is how a silent incompatibility ships.

### AP2 rides on UCP

It renders UCP's checkout because it *is* a layer over UCP, and the toggle reads `[MCP | UCP | UCP+AP2 | ACP]`. The `checkout_hash` comparison is the load-bearing check: without it a mandate proves only that *some* checkout was approved. Autonomous mode refuses `authority-kind-not-enabled` — it is the `mandate` kind, defined and refused in v1.

`alg: none` is refused at the door, and a test greps `ap2.py` to assert it does **no curve maths of its own** — there is one ES256 verifier in this codebase and a second would be a second thing to get wrong.

### Two bugs in my own replay, both instructive

- I gave each protocol a **different `agent_id`**, which made the Transcripts differ for a reason that had nothing to do with envelopes. The test was measuring my test harness.
- I called `orders.set-status` twice on **the same idempotency attempt**, so the second transition replayed the first result and the order sat at `confirmed` while the Ledger said `CAPTURE`. That is same-key-same-result working exactly as specified, and it is a trap worth naming: two distinct transitions need two keys.

### OPEN — A6

- **`fixed_order_salt_hex` added to the conformance fake**, so a golden replay produces comparable bytes — the same reason hour 0's `cart_hash` vectors pin a salt. Empty outside tests; a real Merchant always generates one.
- **`order_id_hint` added to door 7's request.** It lets a replay name its own order so Transcripts are comparable. A Merchant is free to ignore it and the real one will; it is documented as test-only at the call site.
- **Scope boundary held:** ACP and AP2 are request-path live and golden-replay proven, with no client integration for either, exactly as the phase text scoped. Only MCP has a live client, and that client is C's job.

---

## 2026-09-20 · A5 — evidence and verifier

**DONE WHEN met.** **337 tests pass**; ruff, mypy --strict and all four guardrails clean.

Five sections, hash-chained, ES256-signed, carrying their own JWKS snapshot. Exit codes 0/1/2 preserved from `main`, because a script piping receipts through the verifier needs to tell valid from tampered from untrusted without parsing prose.

### The verifier reports rather than summarises

A bundle never comes back "verified". It comes back with the Authority kind, the mechanism where there is one, and the Binding printed — and a `strength` line in the plan's own words. Under `upi-pin` that line says explicitly that the basket is bound *by reference through the Quote, not signed by the buyer*; under `passkey` it says buyer-attested over the exact basket. A COD `moved` section says the **Merchant asserts** the capture and no rail attested it.

**A claim-ordering bug, caught by its own test.** The COD branch fired on `merchant_asserted` before checking whether anything had moved, so a bundle sealed at `confirmed` with an empty `moved` section announced "the Merchant asserts this CAPTURE" about a capture that had not happened. Emptiness is checked first now. Precisely the flattering-claim failure the section exists to avoid, produced by my own code.

### Three details worth keeping

- **The chain link covers the section's name, not just its bytes.** Without that, two sections with identical payloads would be interchangeable and a `told` could be presented as a `moved`.
- **The JWKS snapshot is inside the signed payload.** A bundle whose key list could be swapped after signing would verify against whatever key an attacker supplied; a test swaps in a hostile keyring and expects `TAMPERED`.
- **Erased verifies, altered does not.** Without the salt both PII sections report `unopened` and the bundle is `VALID`, because erasure must never break verification. With the salt and an altered row it is `TAMPERED`. Those are different states and the verifier says which.

### COD sealing, and why it needed no new machinery

Sealed at `confirmed` with an empty `moved` section — every other section is already final — and collection appends an entry as v2 through **exactly the path a refund uses**. The same function serves both because from the bundle's point of view they are the same act: a new money event on an order whose other four sections were settled. The Consumer has a verifiable receipt from the moment they commit rather than only after they pay.

### `format_rupees`

Written first as `f"₹{minor / 100:.2f}"` with a money-lint marker, which was the wrong instinct: the marker exists for exact decimal arithmetic, not for making the lint quiet. This is the number a Consumer reads off a receipt, so it is `divmod` — integer arithmetic with no rounding mode to get wrong.

### The receipt viewer

`/receipt/<id>` is mounted **outside** the console's auth boundary. `/agentic` is session-authenticated for the Merchant, and a receipt that opens by unguessable id *with no login* cannot live there — putting it there would mean no Consumer could ever open their own. A test asserts the request carries no session. A missing receipt and a wrong id answer identically.

---

## 2026-09-20 · A4 — Authority, admission, order lifecycle

**299 tests pass**; ruff, mypy --strict and all four guardrails clean.

### Authority as data, not as an `if`

`authority/kinds.py` holds §6.5's table: each kind declares what it binds, by whom, and **when it lands**. That last part is why the table is data — a caller that could say "my authority landed early" could say it about `upi-pin`, and then a spend would pass check 1 on a ceremony that has not happened yet.

`mandate` is present with `live=False` rather than absent. Defined, registered and refused is a different claim from "we never thought about it", and a reader of the registry can see which one this is.

An OTP is refused **at the enum**: `IntentMechanism("otp")` raises. It cannot be configured, which is stronger than being validated away, and the reason is in the docstring — it proves control of a phone number, which is exactly what RTO fraud already defeats.

### The `consumer_id` gap the plan flagged, closed

A COD order authorized by the `passkey` mechanism never touches a payment rail, so there is no VPA to derive a pseudonym from. The derivation now takes a `HandleSource` (`payer-handle` or `credential-id`) and **folds it into the HMAC**, so the two sources cannot collide and a verifier can tell which was used. An empty handle raises rather than producing a null id — on exactly the path where attribution matters most.

### Tokens

A tap token is what makes "a hold cannot be created without spending an approve token" true. It is single-use, 5 minutes, bound to one `cart_hash`, and carries a `rendered_digest` of what the approve page actually showed — so a **Destination edit after render invalidates it even though the total did not move**. That is the gap hashes alone leave, closed on display rather than on hashes.

A resume token is session-bound and resolves both ids server-side. A stranger presenting someone else's token gets `not-found` and not "that belongs to another session" — the second answer is the oracle the token exists to close.

### The SSRF sink

Refusals happen **before any packet leaves the box**: scheme, then the metadata address, then resolve-and-check. The metadata check runs on what the host *resolves to*, not what it is called, and it is consulted before the allowlist, so no configuration can reach it.

§10.1's scheme carve-out is implemented and tested: `http://buyer-chat:3001` is admitted when named, and a *different* host in the same private range is refused on both dimensions — scheme first, address second. The allowlist is names, never a CIDR, because a range is not an exception.

### RFC 9421

The signature base is written once, here, with a Python signer alongside the verifier. A4c's real signer is the chat's in TypeScript; having both ends against one set of vectors before the chat exists is what stops the signature base being debugged twice by two people who each think the other side is wrong.

One design point worth keeping: **a failed verification does not burn the nonce.** Remembering a nonce from an unverified request would let anyone burn nonces they never signed, which is a denial of service dressed as replay protection. The test asserts the real request still succeeds afterwards.

### Lifecycle

One `expires_at` column, three deadlines, and the COD one **alerts rather than acting**: a parcel that is late is not a parcel that is lost, and only the Merchant knows which. The payment window is `min(15 min, provider link lifetime)` — a hold outliving the link it was taken for is stock nobody can pay for.

Transitions encode that `cancelled` is pre-money only and `refunded` post-money only, which is what keeps "status flip alone never moves money" true. An RTO is `cancelled` + `rto`, not a ninth status.

### OPEN — A4

- **The approve page and `/agent/*` routes are not wired yet.** A4's building blocks (tokens, admission, rate limits, lifecycle) are complete and tested as units; mounting them as HTTP routes lands with A5's console and A6's protocol surface, which is where the request path actually gets built. The DONE WHEN items that require an end-to-end HTTP flow — a stranger transacting with no prior Merchant action, an abandoned `confirmed` releasing at link expiry — are asserted at the unit level here and re-asserted end to end there.

---

## 2026-09-20 · A3 — Gate, Ledger, provider

**DONE WHEN met.** `uv run pytest -q` → **222 passed**; ruff, mypy --strict and all four guardrails clean. Every gate in the phase text has a test named after it.

### The Ledger

Single entry per money event, and `main`'s double-entry account legs deliberately not carried over: the invariants here are stated per order, and mixing the representations makes the arithmetic disagree without failing anything loudly.

**The `main` trap is closed and tested.** `create_refund_entry` keyed idempotency on the checkout id alone, so a second partial refund returned the first entry and wrote nothing. The key now carries the refund's own id, and `test_two_successive_partial_refunds_both_write` asserts two rows — the test that would have caught it doing nothing.

**A design bug of my own, found by the COD test.** `open_holds` was computed as `reserved − captured − released`, which made a COD capture look like it had closed a hold that never existed: the order read as "closed more than it held" and the invariant refused a correct cash sale. Whether an entry closes a hold is now **recorded on the row** rather than inferred from its kind. Escrow-zero then quantifies over `RESERVE` entries exactly as ADR-0018 says it should, and COD needs no exception at all.

Also: SQLite only autoincrements `INTEGER PRIMARY KEY`, never `BIGINT`, so the first version had every insert arrive with a NULL id and look like a duplicate-key collision. The column carries a `with_variant` now. And the duplicate-key path uses a SAVEPOINT rather than rolling back the session — a duplicate key is the *expected* outcome of a retry, and rolling back would discard every legitimate write before it in the same unit of work.

### The Gate

Twelve checks in §6.5's fixed order, byte-stable Transcript, dry-run with zero side effects. `decide()` / `settle()` / `collect()` are three entry points rather than two, and `collect()` is not a branch inside `settle()`: it verifies no Provider record because there is no rail, records nothing about Authority because `confirmed-intent` resolved days earlier at `decide()`, writes one `CAPTURE` with no preceding `RESERVE`, and runs no Gate check because the goods are already on the doorstep.

`upi-pin` **defers** at check 1 rather than passing, because the payer authenticates in their own PSP app and the Authority arrives with the money. Recording it as `pass` would put a false statement into signed evidence; `settle()` resolves it, and `assert_complete()` refuses to capture while anything is still deferred.

`quote-consistent` is implemented independently of the fake's §16.11 arithmetic, as A2 promised. It catches a tampered total, a tampered line fold, a quote that disagrees with the pinned Attestation, a non-zero `round_off`, and tax-inclusive lines that are not marked informational.

### Two things the tests pin that prose alone would not

- **A `sold-out` failure writes no `RELEASE`.** No hold was taken, so there is nothing to close, and the trait refuses one if attempted. An `amount-mismatch` arrives *after* a successful reserve and does release exactly its own hold — two causes, different ledger consequences, and they must not be written as one.
- **50 concurrent taps on 5 units yield exactly 5**, stock never negative, against the conformance fake. B2 repeats this against real Postgres; they test different things and both are required.

### Provider

Four methods, plus `block / capture-block / release-block` declared by nothing and consumed by nothing — the seam ADR-0024 wants, and explicitly not a Reserve Pay COD hold, which ADR-0018 retired. A test asserts no adapter declares it, so the seam cannot drift open unnoticed.

Webhooks are HMAC-on-raw-bytes with `event_id` dedupe. The raw-bytes point is tested by signing a body with irregular whitespace and showing that its own re-serialization fails verification — a signature checked against a round-trip verifies nothing.

Crash-mid-link adopts via `check-status`: the fake can crash after creating a link and before answering, and the recovery finds the orphan rather than creating a second link, which is how a Consumer gets charged twice.

### OPEN — A3 (ladder step 4)

- **`aiosqlite` added as a dependency.** §16.1 says pin at hour 0 and never upgrade mid-build; this is an addition rather than an upgrade, needed because the Ledger is async and SQLAlchemy's async engine needs an async driver for the test database. Postgres uses `psycopg`, already pinned.
- **`tags` (check 8) currently passes unconditionally.** It is separate from `blocked` (check 7) on purpose — blocked is "never", tags is the Merchant's own per-tag refusal list — but no Policy field feeds it yet. It needs the `/agentic` console (A5) to have anything to enforce, and it stays in the check order because removing it would change Transcript bytes.

---

## 2026-09-20 · A2 — trait client and conformance fake

**DONE WHEN met.** Conformance suite green: retry-safety, external-sale visibility, byte-for-byte quote determinism, a flat-price Merchant's zero-value lines passing unchanged, and `variant-required` on a group id at every door taking a SKU. `uv run pytest -q` → **159 passed**, everything else clean.

### The fake is a Merchant, not a mock

It is an ASGI app and the client reaches it over real HTTP through `httpx.ASGITransport`, so every test exercises HMAC verification, idempotency replay and reason-code → status mapping rather than stepping around them. It also misbehaves on purpose, because that is what the Gate is for: `quote_drift_paise` moves the price between calls so `quote-fresh` has something real to catch, and `external_sale()` lowers stock outside the agent path so "the next Gate re-reads fresh" is a test rather than a claim.

**§16.11's arithmetic is implemented in the fake, deliberately, and A3 will re-implement the check independently.** If the Gate and the Merchant shared a pricing module, `quote-consistent` would prove only that the sidecar agrees with itself. The fake computes the §16.11 worked example from its own seed and lands on 249800 / 9900 / 15827 / 11894 / 11894 / 259700 — the same numbers hour 0's golden vector pins, reached by a different route.

### The client is async, and that was a design decision rather than a test convenience

`httpx.ASGITransport` is async-only, which forced the question early. The answer would have been the same regardless: a synchronous HTTP call inside a FastAPI handler blocks the event loop for the length of the Merchant's round trip, and the Gate makes several per decision (`quote`, `reserve`, `orders.set-status`). Under any concurrency that is the sidecar serialising itself behind the slowest Merchant response. Better found at A2 than at A3 with the Gate written against a blocking client.

### Three decisions worth the ink

**The signature covers the path, not only the body.** `release` and `commit` both carry `{order_id}` and nothing else, so a signature over the body alone makes a signed release a signed commit. An attacker who can redirect a request gets a free close of somebody's hold.

**`reserve` checks every line before it moves any stock.** Decrementing as it goes leaves a basket half-reserved when line three is sold out, and the caller then holds stock it has no order to release.

**A refusal replays as a refusal.** Same idempotency key, same result, including the error — the Merchant caches the refusal too. A retry that succeeds where the first attempt refused is a second hold with extra steps, and the test plants a restock between the two attempts to prove it.

### Two guardrail findings from A2's own code

- The money lint flagged `timeout: float = 10.0` in the client. A timeout is a duration, not money — but rather than teach the lint an exception, the field is now `int` seconds. "No float anywhere in the sidecar" is enforceable; "no float except where it is fine" is not.
- `ruff format` wrapped the tax extraction and moved `# money-lint: decimal` onto the closing-paren line, so the line-anchored marker missed and the lint failed on correct code. The marker now matches anywhere in the **enclosing statement's** line span. A guardrail that fails whenever the formatter rewraps is a guardrail somebody deletes.

---

## 2026-09-20 · A1 — skeleton and guardrail harness

**DONE WHEN met.** App boots behind the proxy split; the harness goes red on planted violations and green on the tree. `uv run pytest -q` → **105 passed**. `ruff`, `mypy --strict`, firewall, money lint, time lint, `CODES.md` diff and the prose scan all clean.

### The gate, run rather than asserted

`docker compose up -d --build` → caddy, sidecar and postgres healthy. Against the live stack:

- `curl -H 'Host: spoiledduckie.localhost' http://127.0.0.1/agentic/codes` → the reason-code set, served by the sidecar through Caddy on the Merchant domain.
- `/readyz` → `ready`, and it carries the `dev-profile-allowlist-active` warning, because compose sets `OPENSTORE_DEV_PROFILE_HOSTS=buyer-chat:3001` and SPEC §14 requires the exception to be visible in health output.
- `GET /` → **502**, which is the correct answer and the point of the check: `/` belongs to the store, the store is not built yet, and a 200 would mean the split had leaked. `chat.localhost` likewise — the site block exists per §10 so the third surface has somewhere to arrive.

### What landed

- `app.py` (FastAPI, `/healthz`, `/readyz`, `/agentic/codes`), `core/settings.py`, `Dockerfile` (non-root — the container holds the signing key), `Caddyfile` with both site blocks, `docker-compose.yml`, `docker/postgres-init.sql` (two databases, two roles, and an explicit `REVOKE CONNECT ... FROM PUBLIC`, since PUBLIC gets CONNECT on every new database by default and "no cross-grant" would otherwise quietly mean "can connect and read the public schema").
- `scripts/lint_money.py`, `scripts/lint_time.py`, `scripts/lint_firewall.py`, and `.github/workflows/ci.yml` wiring all of them plus hour 0's `registry_diff` into every push.
- `tests/test_harness_planted_violations.py` — 18 cases. Every lint is run against a file written to fail it *and* against the real tree, because a guardrail nobody has watched fail is a guardrail nobody knows works.

### Two decisions worth the ink

**Money lint bans `/` outright, with an escape.** `int / int` silently yields a float and AST cannot see types, so true division is refused and the tax extraction (§16.11 step 5) carries `# money-lint: decimal`. The marker is not a weakening: it makes every place in the system where money is divided greppable, which is a list worth being able to read. Also refused: float literals, `float()`, and `round()` — banker's rounding disagrees with §16.11's ROUND_HALF_UP on exactly the .5 cases money hits.

**`extra="forbid"` on Settings was wrong and is now `ignore`.** Written as a typo guard, it was caught by its own test suite reading the repo's real `.env`. The compose demo shares one `.env` across three services (§10), so a variable the sidecar does not declare is usually the chat's — and forbidding it would mean the sidecar dies at boot because another surface added a variable. Typo detection did not go away; it moved to `test_env_example_and_settings_agree`, which diffs `.env.example` against the fields **in both directions**. A field with no template entry is one an operator cannot set; a template entry with no field is a typo that reads as an empty secret. CI is the right place for that, boot is not.

### Note for A4c

§10.1's scheme carve-out is now in `.env.example` and in the `Settings` field description: the dev allowlist permits plain `http` for its named entries, because the hardened fetcher is HTTPS-only and public-IP-only, and both of those refuse `http://buyer-chat:3001`. Without it the demo's own self-registration is refused by its own hardening.

---

## 2026-09-20 · Hour 0 — contracts frozen

All seven steps of §12's hour-0 checklist, in order. `uv run pytest -q` → **74 passed**; `ruff check`, `ruff format --check` and `mypy --strict` clean.

### What landed

1. **Skeleton.** `src/openstore/sidecar/{core,trait,gate,ledger,authority,admission,provider,evidence,protocols,console,verify}/`, plus `tests/`, `scripts/`, `design/`, `lists/`. `pyproject.toml` adapted from `main`: dropped `discord.py`, `langgraph`, `typer`/`click` and `rich` (no surface in the rebuild uses them), dropped `[project.scripts]` (an entry point naming a module that does not exist is a broken install — `openstore up` arrives with A7), dropped `main`'s `[tool.mutmut]` block (it lists money-path modules that do not exist yet). Added `jsonschema` for step 5. `uv.lock` committed; nothing upgrades mid-build.
2. **`core/codes.py`** — 17 closed sets, 32 reason codes, and the reason-code → HTTP-status table. §6.4 listed eleven sets; the other six were named in prose and registered nowhere, which is precisely the drift the registry exists to stop.
3. **`scripts/registry_diff.py`** — generates `docs/CODES.md` from the enums, `--check` diffs it. Direction reversed from `main`, which diffed source against a hand-maintained `REGISTRY.json`: there is no hand-maintained file left to drift. `--prose` additionally scans the normative files for backticked identifiers in no closed set.
4. **`tests/GOLDEN/cart_hash/vectors.json`** — three vectors, hashes pinned. `mixed-basket` reproduces §16.11's worked example to the paise (shipping 3955/5945, IGST 15827, CGST/SGST 11894/11894, total 259700) and the test recomputes it rather than trusting the literal.
5. **Door and Quote JSON Schemas** in `trait/schema/`. Shapes only.
6. **`.env.example`** per §16.1, every variable named, no values.
7. **The D1 amendment** — Next.js → SvelteKit in `SPEC.md §1`, `PLAN-merchant-site.md` and `PLAN.md`.

### Added to §6.4 under standing rule 1 (registry and spec in the same commit)

- **`cancel-order` + `cancel-not-allowed`** — closes `docs/REVIEW-findings.md` R4, decided by the operator before step 2 rather than laddered. Scope `start-checkout`, own orders only, pre-money only, `RELEASE` where a hold exists, refusing at or past `paid`. Without it an agent that built a basket could only abandon it, holding a Quote for 24h and held stock until the payment window lapsed — an inventory-denial path open to any self-registered stranger, which is the same reasoning that time-boxed `confirmed` in the first place.
- **`dispatch-not-allowed`** — ADR-0020 bounds dispatch to `confirmed` onward.
- **`untrusted-key`** — PLAN-sidecar S8's DONE WHEN already refused a post-revocation signature with it, and it was in no set. Found by the prose scanner on its first run, which is the whole argument for having one.
- **Six unregistered closed sets** promoted out of prose: trait doors, gate checks, provider operations, availability buckets, ceremony, discount visibility, protocols, tool names. The gate-check names are written into the Transcript, so they were frozen bytes living in a bulleted list.

### OPEN — h0 (ladder step 4)

- **Destination and Contact field shapes.** §16.5 pins the two demo addresses as prose and never names the object. Frozen as `{line1, line2?, city, state, postal_code, country}` and `{email?, phone?}` — these feed `destination_hash` / `contact_hash`, so they are preimage bytes now. Reversible only by regenerating the vectors, which means before A3.
- **Scopes for `order-status`, `cancel-order`, `request-refund`.** Both plans group them as "reads, and scoped" without naming a scope. Conservative reading taken: the read takes `search`, the two that change an order's fate take `start-checkout`.
- **Canonical PII bytes.** §6.3 says "canonical PII bytes" without defining them. Taken as the same canonical JSON as everything else (sorted keys, no whitespace, UTF-8 unescaped) so there is one canonicalization in the system rather than two.

### Two prose fixes the tooling forced

- `set-status` → `orders.set-status` across SPEC §5/§6 and two plans. It was shorthand for door 8, and shorthand is how a closed set stops being closed.
- `F12` (tax-invoice series) marked **half closed** in `REVIEW-findings.md`: ADR-0020 supplies the series, credit notes for partial refunds remain open and still need counsel.

---

## 2026-09-20 · Verification pass on the fetched specs — all claims hold, four refinements

The step-0 findings were committed on the strength of page summaries. Re-fetched against raw sources asking for **verbatim quotes** rather than paraphrase, because those claims are now pinned authority that translators will be built from.

### Verified verbatim — every load-bearing claim holds

AP2, quoted from `docs/ap2/specification.md`:

> "AP2 operates as a security feature within a Commerce Protocol."
> "AP2 is designed explicitly to be compatible with the Universal Commerce Protocol (UCP) and integrates seamlessly."
> "AP2 defines two Mandate types: Checkout Mandate and Payment Mandate."
> "Verify that the hash of the Checkout JWT sent for approval matches the value included for the `checkout_hash` claim."
> "The `vct` value includes a numeric suffix that acts as a schema version number (e.g. `mandate.payment.1`, `mandate.checkout.open.1`)."
> "The Trusted Surface role is a UI surface that is trusted to get informed user consent for an Intent before creating a user-signed Mandate."
> "Human Present (Direct): The User directly sees the closed Checkout and approves it and its payment explicitly."
> "Human Not Present (Autonomous): The User sees and approves a set of constraints over what closed Checkout and Payment would meet their intent."

**So the AP2-as-a-layer decision is not an interpretation — it is the specification's own sentence**, and it is now quoted in §16.12 so it can be defended in the room rather than asserted.

ACP, from the raw `openapi.agentic_checkout.yaml`: all four path keys, all five operationIds and the header sets confirmed as recorded.

### Four refinements

1. **"Five endpoints" was loose.** It is **four paths, five operations** — `/checkout_sessions/{checkout_session_id}` carries both POST (`updateCheckoutSession`) and GET (`getCheckoutSession`). Corrected everywhere, because a pinned reference that miscounts its own surface invites a wrong implementation.
2. **Headers are per-operation, not blanket.** `Authorization` and `API-Version` on all five; `Idempotency-Key` on the four POSTs only — **`getCheckoutSession` has none**; `Content-Type` only where there is a body. The plan had listed all four as universally required.
3. **Authorship conflicts between sources and is now recorded as such.** The repository says the spec is *maintained by OpenAI and Stripe*; Stripe's documentation says *created by Stripe, OpenAI and Meta*. The plan previously asserted the three-party version flatly. Guidance added: **say "OpenAI and Stripe" on stage** and you cannot be contradicted by the repo.
4. **ACP revises quarterly, which the plan had not accounted for.** `spec/` holds `2025-09-29`, `2025-12-12`, `2026-01-16`, `2026-01-30`, `2026-04-17` and `unreleased` — five dated releases in under a year. Two consequences now written in: **pin `2026-04-17`, never build against `unreleased`**, and **the conformance badge must name the spec version it targets** (`ACP 2026-04-17`), because an unqualified "ACP conformant" ages badly against a spec that moves every quarter. A6's DONE WHEN now asserts the vendored OpenAPI matches that version, so a revision is noticed rather than drifted past.

Point 4 is the one with legs beyond this build: the badge already names payment-instrument and completion deviations, and spec version belongs in the same honest sentence.

---

## 2026-09-20 · A6 STEP 0 DONE — specs fetched, and AP2 was not what we assumed

Pulled the ACP and AP2 specifications before hour 0 rather than at hour 17.5. **It immediately overturned two things this plan had asserted from memory.** This is the step-0 gate working exactly as intended, one day early and at zero cost.

### Versions pinned (the §0 requirement)

| Protocol | Source | Version read | Licence |
|---|---|---|---|
| ACP | `github.com/agentic-commerce-protocol/agentic-commerce-protocol`, `agenticcommerce.dev` | spec **`2026-04-17`** | Apache 2.0 |
| AP2 | `github.com/google-agentic-commerce/AP2`, `docs/ap2/specification.md` | `main` @ 2026-09-20 | open, Google-led |

### Correction 1 — AP2 is a security layer, not an envelope

The plan had AP2 as a fourth sibling translator beside MCP, UCP and ACP. The specification says otherwise, plainly: **"AP2 operates as a security feature within a Commerce Protocol"**, catalogue and checkout APIs are **outside its scope**, and it is **"designed explicitly to be compatible with the Universal Commerce Protocol (UCP)."**

So `ap2.py` is now a **mandate layer over `ucp.py`**, and the header toggle reads `[MCP | UCP | UCP+AP2 | ACP]`. A fourth peer tab would have been a nicer-looking lie. Building it as a sibling would have meant discovering at hour 19 that we had written a standalone envelope for a thing that explicitly is not one.

### Correction 2 — two mandates, not three

The plan said Intent / Cart / Payment, which is the September 2025 launch-announcement description. The current spec defines **two**: **Checkout Mandate** (`vct` e.g. `mandate.checkout.open.1`, carrying a `checkout_hash` claim) and **Payment Mandate** (`vct` e.g. `mandate.payment.1`, carrying `checkout_hash`, `transaction_id`, and a `cnf` claim holding the agent public key in autonomous mode). Both are JWTs bound to a merchant-signed Checkout JWT.

Direct (human-present) is the mode we support — the agent presents mandates to a **Trusted Surface** for the user to review and sign, which is our approve page. Autonomous (human-not-present) pre-approves constraints, which is our `mandate` Authority kind: registered, recorded, refused in v1.

### ACP fits better than budgeted

Maintained by **Stripe, OpenAI and Meta** — the plan said OpenAI + Stripe and was out of date. Five checkout-session endpoints, all mapping close to 1:1 onto our core, and crucially it ships **machine-readable OpenAPI and JSON Schema**, so validation is generated rather than hand-transcribed.

`POST /checkout_sessions/{id}/complete` is the single refusal point — it carries the delegated credential (Stripe Shared Payment Token, or a `vt_…` vault token under OpenAI's Delegate Payment spec). The other four operations work normally. Two pleasant surprises: ACP has **native `Idempotency-Key` headers** mapping straight onto our per-attempt discipline, and its *Delegate authentication* building block is **OAuth 2.0**, which is already our allowlisted-agent admission route from ADR-0012.

### The finding worth putting on a slide

AP2's `checkout_hash` — a signed mandate bound to the hash of a merchant-signed Checkout JWT — is **structurally the same primitive as our `cart_hash`**, a signed Authority bound to a merchant-signed Quote (§6.3). We arrived at it independently, and their Trusted Surface is our approve page.

Written into the plan as "structurally the same primitive," deliberately not "identical" — the field sets differ and the claim has to survive someone opening both specs in front of you.

### Budget unchanged

A6 stays at 4h. ACP's published JSON Schema pays for the five endpoints; AP2-as-a-layer is cheaper than AP2-as-an-envelope would have been, and it reuses A4/A4c's ES256 verification rather than adding a second JWT verifier. Track P remains at −1.0h.

---

## 2026-09-20 · D3 REVISED — all four protocols live (ACP and AP2 promoted)

Operator asked for the ACP and AP2 translators. D3 previously shipped them declared-and-refused. They are now real translators alongside MCP and UCP.

### Why this is worth real hours

Two live tabs plus two greyed ones makes the "one core, many envelopes" claim at half strength. More to the point, **the deviations are the argument rather than an embarrassment**: each of these protocols wants something at the completion step that this product refuses on purpose, and the badge now names it per protocol.

- **ACP** is built around a delegated payment credential handed to the agent — exactly the authority ADR-0008 and ADR-0013 removed. The translator accepts the whole flow and refuses that one step with a named code, returning the approve URL. Refused on purpose, not unimplemented.
- **AP2** is mandate-based. Human-present maps cleanly onto the approve ceremony and the `cart_hash` binding; human-not-present rests on a pre-signed intent mandate, which is the `mandate` Authority kind — registered, recorded, refused in v1 (ADR-0017).

Two protocols where the refusal *is* the product beat beats two greyed tabs, and the conformance badge now does real work across four envelopes. New demo beat 7b at 60s, flagged as the architecture moment.

### Cost, and the correction to my own number

A6 2h → 4h. Funded by taking **cut #2 up front** (`/agentic` Attribution and Health tabs, −1.0) and making **Exposure read-only** over seeded values (−0.5). A7 drops 2.5h → 1.0h.

I first wrote this as "net −0.5h, unchanged." **That was wrong.** +2.0 against −1.5 is net +0.5, so **Track P moves from −0.5h to −1.0h** against the day. Corrected in the arithmetic table and in D3 rather than smoothed over. Track S is untouched at −0.5h.

Where the hour comes from: the rehearsal block compresses 1.5h → 0.5h, and A6 carries an automatic escape (below) that returns 2h without anyone deciding. Noted explicitly that **the rehearsal is not the buffer** — if Track P is behind at Integration 3, take cut #1 and reverse ACP/AP2 rather than skipping the rehearsal.

### The prerequisite that makes this safe

ACP and AP2 are young specifications and **nobody on this build has their wire formats memorised, including whoever wrote this plan.** A translator built from memory is a false conformance claim, which is the one failure this product cannot survive — the whole pitch rests on the badge being honest.

So A6 now opens with a blocking **step 0**: fetch the published specs, record URL and version in `LOGS.md`, map against the document. §16.12 repeats it and states the boundary precisely — the *shape* of each deviation (ACP's delegated credential, AP2's two modes) is safe to rely on from this plan; **every field name, envelope key and endpoint path comes from the fetched document.**

**If a spec cannot be retrieved, that protocol ships declared-and-refused with the badge saying exactly why, and A6 reclaims its time.** That is a designed escape, not a failure, and it is what makes this addition self-limiting rather than an open-ended risk on the track with no margin.

### Cut list renumbered

Cut #1 is now "ACP and AP2 back to declared-and-refused" (−2h, a clean reversal), with #1b "UCP too" (−1h more) only if A6 is genuinely drowning. Cut #2 is spent. Cuts 4a and 5 remain spent from the Option B decision.

### Also changed

`protocols/registry.py` is now one place naming all four protocols, their live/refused capabilities and their deviation text, with the header toggle and the badge both reading from it — so a protocol cannot be live in one and stale in the other. A6's DONE WHEN requires a golden replay per protocol with **the core Transcript byte-identical across all four**, which is the single most valuable assertion in the phase. Day 2 schedule rebuilt: A6 occupies 17.5–21, Integration 2 moves to 21–21.5.

---

## 2026-09-20 · Pre-hour-0 validation — four defects found and fixed

Last pass before the build starts. Cross-checked §16 against §6, §8 and §13, and checked whether hour 0 is executable as written. Four things were wrong.

### 1 — The item count was wrong, and the way it was wrong was dangerous

Claimed "exactly 16 Catalogue Items." The §8 table sums to **15** (tote 3 + cap 2 + ten singles), and §16.3 has 16 *rows* because one of them is `SD-TOTE-BLK-L`, the deliberately-absent combination.

An agent building the seeder from a 16-row table told to produce 16 items would have seeded `SD-TOTE-BLK-L` — with zero stock or a null price — and broken **two** gates at once: "seed loads with zero null-stock rows" and "the unavailable combination renders as unavailable." The whole point of that row is that it does not exist.

Corrected to **15** everywhere. The row is now struck through and labelled **DO NOT SEED**, with a sentence above the table saying it has 16 rows of which you seed 15, and why. Also fixed a broken markdown row in §8 that had swallowed the following paragraph into a table cell.

### 2 — `core/codes.py` was assigned to two different hours

Hour 0 said to commit it; A1 said to write it. Worse, hour 0 would have been writing into `src/openstore/sidecar/core/` before A1 created that layout.

Hour 0 is now an **ordered seven-step checklist** where each step needs the one before: repo skeleton and `pyproject` first, then `codes.py` (**here, explicitly not in A1**), then `registry_diff.py` and the generated `docs/CODES.md`, then the `cart_hash` golden vectors, then the door and Quote JSON Schemas, then `.env.example`, then the D1 amendment. A1 now *wires codes.py into CI* rather than creating it.

Added: **Track S does not wait for hour 0.** The `design/` import and app scaffolds depend on none of it, so an overrunning hour 0 does not idle the track that has no margin.

### 3 — Inclusive-price arithmetic was unpinned, and it is the one place the money core eats itself

§16 said prices are tax-inclusive but never said whether **shipping and discounts** are, never gave the inclusive-tax extraction formula, and — the real trap — never said how to split an **odd-paise tax into CGST and SGST**.

The Merchant computes the Quote (B2) and the Gate re-checks it (A3). If they round differently by one paise, `quote-consistent` fires on a *correct* quote, and that presents as a bug in the money core rather than as a missing paragraph here. It would have been a genuinely miserable thing to debug at 3am.

New **§16.11** pins the whole order of operations: fold Add-ons, apply discounts to inclusive amounts, apportion shipping by line inclusive total, largest-remainder with ties broken by SKU ascending, extract tax as `ROUND_HALF_UP(inclusive × rate_bp / (10000 + rate_bp))` in exact decimal, and split odd paise as `CGST = tax // 2, SGST = tax − CGST` — **SGST takes the odd paise**, stated once, never re-decided. `round_off_minor` is always `0` in v1, which removes a whole class of ambiguity.

It closes with a **worked example**, computed and verified rather than illustrative: the demo basket to destination B gives subtotal 249800, shipping apportioned 3955 / 5945 (the leftover paise going to the larger fraction), IGST 15827 on the tote line, CGST 11894 / SGST 11894 on the service line, **total 259700 = ₹2,597.00**, with both GST splits in one Quote. That is now the first fixture, the Integration-1 expected output, and the demo's beat-4 number.

### 4 — Shipping and discount inclusivity, marked where the values live

`incl. GST` now appears on both shipping rows and `off the inclusive total` on both discount codes, because a value read in §16.5 is read without §16.11 open beside it.

### Verified correct, not assumed

- Demo basket clears every seeded policy limit: ₹2,597 against a ₹25,000 cap, 3 lines against a 10-line limit.
- `SD-PLUSH-MINI` cap of 2 matches the §13 eighth-minute refusal beat; stock 10 supports it.
- `SD-RECALLED` carries the `recalled` tag and §16.4 blocks that tag — the blocked check has something to refuse.
- Both demo destinations avoid the `19xxxx` unserviceable range; 560038 matches the KA zone, 400028 the rest-of-India zone.
- `SD-CHARMBAR-SEAT` at POS Karnataka against a Karnataka-registered Merchant yields CGST/SGST while the tote to Maharashtra yields IGST — the two-split basket works as claimed.
- Three distinct GST rates (3 / 12 / 18%) are present in the seed, so apportionment is exercised rather than trivially correct.

---

## 2026-09-20 · Unattended-run hardening — §0 rewritten, §16 added

Requirement: the build runs overnight with nobody awake, so **no hour may contain a question**. Two problems, both fixed.

### Problem 1 — §0 literally instructed the executor to stop and ask

The old protocol said "stop and ask" under rule 1 and "3. Ask." under *When blocked*. That is exactly the 3am wake-up, written into the plan as policy.

§0 now opens with **"never ask, decide by the ladder"**, a four-step resolution order that always terminates in action:

1. §16 Pinned values → 2. `SPEC.md` + its ADR → 3. §6 + phase text → 4. **take the most conservative option that keeps the gates reachable, log it under `## OPEN — <phase>`, continue.**

Conservative is defined rather than left to taste: fail loud over coerce, refuse over allow, narrower over wider, existing identifier over new one.

**"Blocked" is now narrowed to one case only** — a gate that cannot go green without changing a frozen §6 contract. Everything else is a decision, not a block. Even then the instruction is: log it, build the rest of the phase, commit, **move to the next phase**. Never wait.

Rule 1 also softened in the right direction: adding a genuinely missing code **to the registry and §6 in one commit** is now explicitly allowed and logged, because the old wording ("stop and ask") turned a five-minute registry edit into a stalled track. Inventing one at a call site is still a red build.

Added alongside: **overrun rule** (a phase more than 50% over budget takes the next §12 cut rather than grinding), **commit at every phase boundary even if cut short** (a run that dies at hour 19 should leave 18 committed hours, not an uncommitted tree), and a **morning report** spec for `LOGS.md` — phases with gate output pasted, every `## OPEN —`, every `## BLOCKED —`, every cut with its hour. Gate output is now required every time, not "if in doubt": an unattended run's claim that something passed is worth exactly what the pasted output under it is worth.

### Problem 2 — the real blocker was unpinned values, not the ask rule

Sweeping for open decisions turned up the larger issue: **the seed named 16 items and priced none of them.** No GST rates, no HSN codes, no stock counts, no thresholds, no GSTIN, no registered state, no addresses, no discount codes, no rate-limit numbers, no COD delivery window, no DB names or roles, no admin credentials, no scripted-driver transcript. An agent at hour 2.5 would have invented all of it — and then the golden vectors, the two-GST-split test and the demo script would all have rested on values nobody chose.

New **§16 Pinned values**, 11 subsections: infrastructure and ports, merchant tax identity, the full 16-item catalogue with prices/HSN/GST/stock/thresholds, seeded policy, shipping zones and the two demo destinations, discount codes, every timing, every rate limit, the `scripted` driver's exact tool-call sequence, fake-provider behaviour, and an explicit list of what is deliberately *not* pinned (copy, images, component structure).

Choices worth recording:

- **Three GST rates** (3% / 12% / 18%) rather than one, so apportionment and largest-remainder rounding are actually exercised instead of trivially correct.
- **Karnataka (29) as the registered state**, destinations Bengaluru 560038 (intra) and Mumbai 400028 (inter). `SD-CHARMBAR-SEAT` is a service fixed at Place of Supply Karnataka regardless of destination — that single row is what makes a two-place-of-supply basket possible, and both the Integration-1 smoke test and demo beat 4 depend on it.
- **Tax-inclusive pricing** (Indian MRP), so tax lines are `informational: true` and must not be added again. The inverted case is still seeded and tested, because getting it backwards double-charges every order.
- **GSTIN `29AABCS1429B1ZQ` is fabricated** — correct in shape, belonging to nobody, and marked as such in place so it is never presented as real.
- **No invented version numbers.** Dependencies are resolved by `pnpm add` / `uv add` at hour 0 and pinned by committed lockfiles. This covers `http-message-signatures` and the B3 table primitive.
- All 128-bit values come from `secrets.token_bytes(16)` / `crypto.randomBytes(16)` — stated because "unguessable" without a named source is how a demo ends up seeding from a timestamp.

Cross-references added at every point where a value was previously left open: B1's seed points at §16.3 as authoritative, A7's console tabs name their seeded values, A4's rate limits point at §16.8, A1's `.env.example` at §16.1, D7 at §16.9, and the demo script and Integration-1 command now name the exact SKUs, destinations and codes they use.

---

## 2026-09-20 · CONFIGURATION DECIDED — Option B, two builders, no third

The §12 hour-0 decision is made and closed. **Option B: two builders, cuts 5 and 4a in force from hour 0.** Anyone or anything executing the plan takes this as given and does not re-open it.

### What is now out of scope

- **Cut 5 — no direct storefront cart or checkout.** The store browses and hands off to an agent; it never creates an order itself. The `site_carts` table and its 30-minute-hold sweeper are **not built** — they existed only to hold stock for that cart. A **Buy via agent** panel carrying the sidecar card URL replaces it on every group page, and that is the surface the demo actually uses.
- **Cut 4a — admin trimmed, not gutted.** Out: CSV export, bulk archive, and the pricing-setup CRUD. Shipping zones, GST identity and discount codes are **seeded in B1 and changed by re-seeding**. In, unchanged: dashboard, catalogue create/edit, variant matrix, stock, price, inventory moves, orders + Timeline, dispatch + invoice, refund dialog, Record collection, Record RTO.

The line held deliberately: everything on the *operating* path stays, only the *setup-once* screens go, so Surface 2's claim — "a Merchant can operate it" — survives intact. The blunter read-only-admin cut would have bought the same ninety minutes and taken that claim with it.

### Budgets after the decision

| | before | after |
|---|---|---|
| B1 schema + seed + storefront | 6.0h | **4.5h** |
| B3 shop-ops admin | 3.0h | **1.5h** |
| Track S phase subtotal | 26.5h | **23.5h** |
| Track S total (incl. 3 integration windows + polish) | 29.5h | **26.5h** vs 26.0 available |

Both tracks now sit at 26.5h against 26.0. The −0.5h is absorbed by the polish pass, budgeted 1.5h and scheduled 0.5h.

### The change that mattered most for an agent-executed build

Recording the decision in §12 alone would not have been enough. The phase bodies in §8 still described **full** scope, so an agent building B1 would have built the direct checkout and an agent building B3 would have built the pricing CRUD — the schedule says one thing, the instructions another, and the instructions are what gets read at the point of work.

So both cuts are now stated **as blockquote banners at the top of B1 and B3**, the removed scope is struck through in place with its full text kept for a later phase, and the DONE WHEN gates are rewritten to match what is actually being built. §1's deliverable and §2's D9 trim list were corrected to match.

One contradiction surfaced while doing this and is fixed: B1's new gate first read "no route anywhere in the store creates an order," which would have forbidden **door 7 `orders.create`** — implemented by the store in B2 and called by the sidecar over the private network. Corrected to "no **public storefront** route creates an order; door 7 is the only path that does."

### Option A kept on the record

Option A (a third builder takes Surface C — 11.5h, lifting out cleanly since B and C share no state, code or database) is retained in §12 as the path **not** taken, so that if a third pair of hands appears mid-build the restoration is mechanical rather than a redesign. Under it, cuts 5 and 4a would both be restored.

---

## 2026-09-20 · Track S re-budget — three changes applied

Prompted by a question about Track S, which turned into a second look at the schedule. Audit #1 fixed the schedule's *arithmetic*; this fixes its *estimates*, which were a separate and larger error.

### The finding behind it: the reuse asymmetry

Checked and confirmed: **`main` has no `package.json` and no `node_modules`.** Its entire UI is Jinja templates plus three vanilla JS files served from FastAPI. So Track P inherits ~7,000 lines of debugged Python *and* can crib `/agentic` from main's existing studio templates, while **Track S inherits nothing from `main` at all** — both SvelteKit apps are greenfield, and its only reuse is the portfolio design system.

Equal hours in the table, unequal risk in reality. Track S also gates all three integration points. Neither fact was written down; both are now.

### Two phases were under-budgeted — correcting my own numbers

- **C1 was 3h and could not have been.** It contained the whole chat UI *plus* ES256 key generation, Agent Profile publishing, RFC 9421 request signing, a three-driver model seam, the scoped action set and a bounded tool loop. Realistically ~5h.
- **C3 was 3h and was light.** Eleven distinct UI states, a verbatim signed-quote renderer with a byte-equality assertion, a pre-display signature check, and named copy for eleven reason codes.

Corrected: **C1 → 3.5h** (after the signing slice moves out, below), **C3 → 4.5h**.

### Change 1 — the agent signing kit moves to Track P as new phase A4c (1.5h)

The chat *signs* RFC 9421 requests; the sidecar *verifies* them. Same specification from two ends, and A4 is already writing the verifier. Split across two builders in two languages, the signature base gets debugged twice by two people who each assume the other side is wrong.

A4c gives both to one person in one sitting, against one shared fixture set asserted in **both** test suites. The files live in `demo/buyer-chat/src/lib/identity/` and are scheduled **first on day 2**, because C1 consumes them and because A4's verifier is still fresh from the previous evening.

**This does not touch the import firewall.** The firewall forbids imports across roots, not authorship — a person is not a root. The files import nothing from the sidecar and reach it only over HTTP.

### Change 2 — reuse two libraries instead of hand-rolling

- `http-message-signatures` (npm) for the RFC 9421 signature base, rather than writing it. It is the fiddly part and it is someone else's solved problem. ES256 itself needs no dependency — Node's built-in WebCrypto covers P-256/SHA-256.
- A headless table/form primitive for B3's variant matrix; the design system supplies the styling either way. **B3 3.5h → 3.0h.**

### Change 3 — the configuration is now decided at hour 0, not discovered at hour 22

New block in §12 with two options, one of which must be chosen and written here before any code:

- **Option A — a third builder takes Surface C.** B and C share no state, no code and no database. Surface C is 11.5h and lifts out cleanly; Track S then runs Surface B alone at 16.5h against 26.0, and **cuts 4a and 5 are not needed**. Costs the demo nothing. Take this if any third person exists.
- **Option B — two builders, cuts 5 and 4a taken up front.** Cut 5 drops the direct storefront checkout (−1.5h, cheapest cut in the plan — that path takes no money anyway). Cut 4a **trims** the admin rather than gutting it (−1.5h): keep create/edit, variant matrix, stock, price, dispatch, refund; drop CSV, bulk archive and pricing-setup CRUD. Explicitly **not** the older blunt "read-only admin" cut, which trades away Surface 2's entire reason to exist for the same ninety minutes.

### A second arithmetic error, found while applying the above

Audit #1's table omitted the three integration windows — 1.5h where neither track is building. Adding them: **both tracks now total 26.5h against 26.0 available, −0.5h each.** The earlier "+1.0h margin" on Track P was wrong.

Without cuts 4a and 5, Track S is **29.5h against 26.0 — three and a half hours over**, which is the number that makes the hour-0 decision non-optional.

The −0.5h is absorbed by the polish pass, which is budgeted 1.5h and scheduled 0.5h. Recorded in the plan as a deliberate choice: **polish is the buffer; the rehearsal is not.** An unrehearsed demo wastes the whole plan; plain empty states go unnoticed.

### Also changed

- Day 1 and Day 2 tables rebuilt against the corrected budgets, with B2 explicitly finishing by hour 10 because Integration 1 depends on it, and C2 by 17.5 because Integration 2 does.
- Cut list renumbered: 4a and 5 are spent under Option B and no longer available. The remaining list now notes that **cuts 1–3 are all Track P, the track with margin** — so their real value is freeing a Track P builder to move onto C3, not the hours themselves.
- §5 layout shows `lib/identity/` with a note on who authors it.

---

## 2026-09-20 · Audit #1 of SPECS/PLAN.md

Full read-through of the plan against `SPEC.md`, `CONTEXT.md`, `docs/adr/0017`, `0018`, and the actual code on the `main` branch. Every claim about `main` was checked by reading the file, not by memory.

**Verdict: the plan was directionally right and had 8 things in it that would have cost real hours or broken the demo.** All 26 findings below are fixed in the same commit. Severity is about consequence, not about how wrong the sentence was.

### Blockers — would have broken the build or the demo

| # | Finding | Fix |
|---|---|---|
| B-1 | **`.localhost` does not resolve inside containers.** The chat container fetching `spoiledduckie.localhost` hits its own loopback, not the proxy. Worse, the SSRF rule is "HTTPS only" and the compose fetch is HTTP — the dev exception covered hostnames but not the scheme, so the demo's own self-registration would have been refused by its own hardening. | New §10.1 "Two addresses for one shop": browser origin vs container origin, named explicitly. Dev exception now covers scheme **and** host, both fenced. |
| B-2 | **Receipt viewer was at `/agentic/receipt/<id>`** — inside the session-authenticated Merchant console. The spec requires a receipt to open by unguessable ID with **no login**. As written, no Consumer could ever open their own receipt. | Moved to public `/receipt/<id>`, added to the mount table, and the auth boundary is now stated per route. |
| B-3 | **Door 3 `reserve` took `cart_id` but keyed idempotency on `order_id:attempt`.** Contradiction: `reserve` runs after `orders.create`, so it has an `order_id`. | Door 3 input corrected to `order_id`. A call-sequence diagram added so the ordering cannot be misread. |
| B-4 | **`order_salt` custody was undefined.** The sidecar must have it to compute `destination_hash` / `contact_hash`, but the spec says it "lives only in the Merchant order row." Who generates it, how it reaches the sidecar, and why the sidecar does not persist it were all unwritten — and getting this wrong silently breaks ADR-0011's erasure guarantee. | New §6.6 "Where `order_salt` lives": Merchant generates at `orders.create`, returns it in that response only, sidecar holds it in request memory and never writes it. A test asserts it appears in no sidecar table and no log. |
| B-5 | **COD receipt sealing time was undefined.** Money moves days after `confirmed`, so "the bundle is sealed at payment" has no meaning on the cash path. | Stated: sealed at `confirmed` with an empty `moved`, and collection appends a v2 `moved` entry through the same versioning path refunds already use. No new machinery. |
| B-6 | **Schedule arithmetic was wrong and Track S was over-subscribed.** C3 (3h) + C4 (2h) were slotted into one 3h block; B1 was labelled 4h but occupied 6h of slots; Track S totalled ~25h against ~23h of slots. The plan also never said how many people it assumes. | §12 rebuilt: every phase hour re-summed, C5 moved to Track P, staffing stated in the first line (**two builders, 13h days**), and per-track totals printed against available hours so the margin is visible instead of implied. |
| B-7 | **`main`'s `create_refund_entry` keys idempotency on `checkout_id` alone.** A second partial refund returns the first entry and **writes nothing, silently**. The plan lists partial refunds as in-scope and lists this file as near-verbatim reuse — copying it would have shipped that bug. | Flagged in the reuse map as a named trap with the required fix (key on `refund_id`), and an explicit test added to A3's DONE WHEN. |
| B-8 | **`chat.localhost` had no Caddy site block.** Nothing served it. | Added to the compose/proxy section. |

### Logic errors — wrong, but recoverable

| # | Finding | Fix |
|---|---|---|
| L-1 | §6.5 check 1 named only `passkey` and `upi-pin`; a reader of the numbered list alone would mishandle `confirmed-intent`. | Check 1 now points at the Binding table as normative. |
| L-2 | `quote-fresh` is check 11, described as "immediately before `RESERVE`", while `method-enabled` is check 12 — implying a check runs after the reserve. The actual call sequence was never written down. | Explicit call sequence added to §6.5. SPEC's frozen check order is unchanged; what is now stated is that **all twelve checks are inside `decide()`, and `RESERVE` follows `decide()` returning**. |
| L-3 | Transcript check results were implicitly pass/fail, but `upi-pin` defers check 1 — so the Transcript would record a **pass for an authority that has not arrived**, which is a false statement in signed evidence. | Third result state `deferred` added to the closed set, with `settle()` required to resolve every deferred check before capture. |
| L-4 | `settle()` on COD was described as running at order time *and* capturing days later — two moments in one function. | Third entry point `collect()` named explicitly, with what it may and may not do. |
| L-5 | `consumer_id = HMAC(key, payer_handle)` — but a COD order authorized by the `passkey` mechanism has **no payer handle at all**. | Fallback stated: the passkey credential ID is the handle source on that path, recorded in the Transcript so the derivation is never ambiguous. |
| L-6 | `design/` firewall permitted only `.css/.woff2/.svg/LICENSE-*`, and §4 then put a `README.md` in it. The firewall test would have failed on the design system's own documentation. | `.md` added to the permitted list. |
| L-7 | B1 said `expires_at` carries "two deadlines"; A4 said three. | Reconciled to three. |
| L-8 | A5 said the verifier "prints both" after listing three things. | Corrected. |
| L-9 | Seed claimed 12 groups → "~20" Catalogue Items; the enumeration yields 15. | Exact counts written out per group so the seeder is a checklist, not an estimate. |
| L-10 | The seeded COD order was merchant-side only, so it could not have a sidecar Ledger or appear in `/agentic` — "an empty Ledger" was vacuously true for the wrong reason. | Seeding rule added: any order that must be visible on both sides is created by **driving the real flow** in a script, never by inserting rows. |
| L-11 | The storefront's 30-minute direct-cart stock hold had no table and no sweeper. | `site_carts` table and its sweeper added to B1. |
| L-12 | HTTP status per refusal was ambiguous — `variant-required` could reasonably be 400 or 409, and two implementers would have chosen differently. | Status is now derived from the reason code by **one table in `core/codes.py`**, never chosen at the call site. |
| L-13 | §5 layout omitted `confirmed_intent.py`. | Added. |
| L-14 | A3 and B2 both claimed the 50-concurrent-on-5-units test without distinguishing the fake from the real store. | Disambiguated: A3 runs it against the conformance fake, B2 against Postgres. Both are required; they test different things. |
| L-15 | **Ledger shape was unstated.** `main` is double-entry with named accounts (`merchant_revenue`, `customer`); the plan's invariant formulas (`sum(REFUND) ≤ captured`) assume one row per money event. An implementer copying `main` would have produced a ledger whose arithmetic does not match the plan's. | Stated explicitly: **single entry per money event**, `main`'s account legs are deliberately not carried over, and the reason is written down. |

### Unverified claims — softened rather than asserted

| # | Finding | Fix |
|---|---|---|
| U-1 | "Open Sauce Sans is OFL" was asserted from memory. The fonts folder ships `LICENSE-EBGaramond.txt` and `LICENSE-Iosevka.txt` and **no licence for Open Sauce Sans or PP Kyoto**. | Downgraded to "believed OFL, licence file absent — verify before anything public", alongside the existing PP Kyoto flag. |
| U-2 | "`localhost` is a secure context, so WebAuthn works" — true in Chrome and Firefox for `*.localhost`; **Safari does not resolve `*.localhost` at all**. | Browser support stated, with the `/etc/hosts` fallback written out. |
| U-3 | The Quote example carried `"amount_minor": -0`, which is not a meaningful JSON literal and could be copied as one. | Replaced with a real negative value, and the whole block labelled shape-not-literal. |

### Verified as correct (checked, not assumed)

- Line counts in the reuse map: `compiler.py` 322, `ledger.py` 374, `poai.py` 518, `webauthn_rp.py` 621, `oauth.py` 501, `idempotency.py` 156, `verify/checks.py` 660, `verify/cli.py` 181, `mcp_server.py` 1859, `wellknown.py` 296, `webhooks.py` 286, `inventory.py` 792. `virtual_authenticator.py` is **204** (plan said ~180 — corrected) and `core/health.py` is **227**.
- `core/health.py`, `core/database.py`, `core/audit.py`, `config.py` all exist on `main` as cited.
- The `rebuild` branch genuinely has no `src/` — this is a greenfield build on top of `main`, as the plan says.
- ADR-0017's Binding table, ADR-0018's COD ledger rules, and the `confirmed-intent` two-mechanism closed set are quoted accurately.
- The portfolio design system is at `~/projects/portfolío/frontend` (accented `í`), SvelteKit 2 / Svelte 5 / Tailwind v4, with 9 woff2 files and 2 licences — counted, not estimated.

### Added for execution safety

The plan was written to be *read*. It is now written to be *executed*, including by someone or something that will not push back when a step is ambiguous:

- **§0 Execution protocol** — the order phases run in, the commit discipline, and three standing rules: never invent an identifier, never widen scope to make a test pass, stop and ask rather than guess.
- **Per-phase file checklists** — each phase now names the files it creates, so "done" is countable.
- **Integration points as runnable commands** — the three ★ gates in §12 are now commands with expected output, not descriptions.

---

## 2026-09-20 · SPECS/PLAN.md — D1 and D4 settled

- **D1 (merchant site stack)** resolved to **SvelteKit**, on the operator's "whichever is easiest." It is the easier path: the portfolio design system is already SvelteKit 2 / Svelte 5 / Tailwind v4, so it becomes a dependency rather than something to re-implement in Next.js. Costs a one-line amendment to `SPEC.md §1` and `PLAN-merchant-site.md`, which lands in the hour-0 contracts commit.
- **D4 (COD)** flipped from **cut** to **in**, on the operator's "let COD in, it's easiest." Verified against ADR-0018 and ADR-0017 before flipping: COD is the cheapest money path in the product — no provider call, no payment link, no webhook, no link-expiry sweep, no reconcile. Cost is four specific additions (`confirmed-intent` Authority, `CAPTURE` with no preceding `RESERVE`, a third `expires_at` deadline, RTO as `cancelled`+`rto`), budgeted at 2h and ranked **cut #6 — last**, because by the time it is built the Ledger work it demonstrates is already done.
- Propagated through 14 sections: closed sets, the Binding table, Ledger rules, order lifecycle, receipt honesty, schema, admin actions, chat copy, four new test gates, the schedule, and a 7th demo beat.

## 2026-09-20 · SPECS/PLAN.md created

Comprehensive 48-hour MVP build plan covering all three surfaces, written against `SPEC.md` with departures named in §2. Grounded in a survey of the `main` branch (~7,000 reusable lines identified, ~7,000 deliberately left behind) and the portfolio design system.
