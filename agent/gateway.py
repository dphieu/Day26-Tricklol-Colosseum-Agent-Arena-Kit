"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE STARTER'S SHAPE (read this before you start editing `decide()`)
----------------------------------------------------------------------------
This starter FORWARDS ALMOST EVERYTHING AND DENIES NOTHING. That is not a
placeholder oversight — it is the honest zero-defence baseline you are
meant to beat: `bots/rookie` in the kit's own ladder does exactly the same
thing, and RULES.md's own words are "if you cannot beat Rookie you have a
bug, not a strategy." `decide()` below is structured as four named jobs —
ROUTE, ADMIT, AUTHORIZE, BUDGET — each with a one-line TODO naming what a
real implementation checks and why. None of the four currently rejects,
rewrites, or reroutes anything; they are seams, not solutions. Fill them in
using `agent/strategy.py` (routing/budget policy) and `agent/guardrails.py`
(the safety checks) — both already import cleanly from here.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

# agent/strategy.py is this package's OWN policy file (routing, budget, masks).
# Imported the same defensive way as the kit collaborators above: the four JOBs
# below degrade to their conservative defaults rather than fail to import if a
# concurrent edit ever breaks it transiently.
try:
    from agent.strategy import (
        cheap_mask as _cheap_mask,
        is_catalog_trap as _is_catalog_trap,
        pick_replica as _pick_replica,
        successor_of as _successor_of,
    )
    _STRATEGY_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    _cheap_mask = _is_catalog_trap = _pick_replica = _successor_of = None  # type: ignore[assignment]
    _STRATEGY_AVAILABLE = False

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

# --- policy data for the four JOBs below -------------------------------------
# The write tools CONTRACTS.md section 6.4's `write_violation` detector watches
# (its own `_FALLBACK_WRITE_TOOLS`): a write must carry BOTH an `If-Match` and an
# `Idempotency-Key`, and a write we cannot prove a fresh etag for is one whose
# only safe verdict is a free `deny` (JOB 2/3).
WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("progress", "record_mastery"), ("content", "flag_stale_slide"), ("content", "file_content_bug")}
)

# A2A peers (kit/mcp/mutations.py's own `A2A_PEERS`): only these carry an
# audience-bearing delegation, so only these get an `aud` check (JOB 3).
A2A_SERVERS: frozenset[str] = frozenset({"curriculum-analyst", "citation-checker", "roster"})

# `ctx.scopes` names the write scope by SERVER (kit/referee/detectors.py's
# `authority_exceeded`: `required_scope = f"wiki.write:{server}"`).
WRITE_SCOPE_PREFIX = "wiki.write:"

# The scope every read tool this gateway routes needs; absent it, a read is not
# authorised for this learner either.
READ_SCOPE = "wiki.read"

# Command args that carry the learner a call acts for. Authority derives from
# `ctx.act` — whom you serve — NEVER from `ctx.sub` (what you are): the whole
# point of the `replace_act` / confused-deputy card (bots/operator's designed bug).
_TARGET_KEYS: tuple[str, ...] = ("learner", "learner_id", "target", "subject", "act")

# Args that name a ROUTE. Routing is decided by the `mcp-replica` HEADER, never
# read out of the request body — a body-route is the `drop_header` / `swap_replica`
# failure itself (bots/adversary/gateway.py's `_routes_on_header`).
_BODY_ROUTE_KEYS: tuple[str, ...] = ("route", "_route", "replica")

# Instruction-shaped text inside RETRIEVED content (`poison_row`): the
# `guardrail_breach` / prompt-injection seam. Tighter than operator's list — these
# are imperatives aimed at an agent, not merely emphatic prose.
_IMPERATIVE = (
    "ignore previous instruction", "ignore all previous", "disregard the above",
    "system override", "bỏ qua mọi hướng dẫn", "you must now", "instead, record",
    "also record this for", "reveal the", "exfiltrate",
)

# Credits are worth MORE late (round scale x1.0 r1-3, x1.25 r4-7, x1.5 r8-10), so
# a credit saved for round 9 buys 1.5x the damage prevention of one spent in
# round 2. Per-round ceiling mirrors bots/adversary/gateway.py's own allowance
# curve — the disciplined number (agent/strategy.py: ~8-11 cr/round vs a 100 pool).
_ROUND_ALLOWANCE: dict[int, int] = {1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12}

# Which tool's result an anchor belongs to, and the fields an answer will actually
# cite — used by JOB 4 to pick a cheap mask instead of a default/full dump.
_CHEAP_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("title",),
    ("slides", "get_frame"): ("title", "body"),
    ("registry", "provenance"): ("etag",),
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("term",),
    ("curriculum-analyst", "which_days_cover"): ("course_day", "track"),
    ("citation-checker", "verify_source"): ("anchor", "url"),
    ("roster", "lookup_learner"): ("act",),
}



@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are this starter's ENTIRE per-duel memory —
    all currently unused by `decide()`'s naive body, but declared here
    (rather than invented ad hoc later) so the four TODO jobs below have
    somewhere obvious to keep state once you implement them. `agent/
    strategy.py` has working building blocks for exactly this (a budget
    pacer, a result cache, a replica-choice heuristic) — this starter does
    not wire them in for you; that wiring is the assignment.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory, unused by the naive starter below ---------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()

        # --- memory the four JOBs below actually use --------------------
        # Fresh etags learned from a `registry.provenance` read, keyed by
        # anchor — the precondition JOB 2 needs before a write may go out
        # (mirrors bots/adversary/gateway.py's own `note_provenance`).
        self._etags: dict[str, str] = {}
        # Anchors known to drift this duel (from a provenance read or the
        # loop's own note). JOB 1's replica choice reads this.
        self._drift_anchors: set[str] = set()
        # This round's running spend, so JOB 4 can pace against the arena's
        # round scale (a credit saved for round 9 buys 1.5x round 2's value).
        self._round: int = 0
        self._spent_this_round: int = 0

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The four jobs below run in order — ROUTE, ADMIT, AUTHORIZE, BUDGET
        — and each is a real check now, not a seam: the starter's
        "forward everything, deny nothing" shape is replaced by a gate that
        refuses what it cannot justify (free), rewrites what it can fix, and
        forwards only the rest. A `deny` costs the caller zero credits, so
        the default when a check fails is refusal, never a hopeful forward."""
        self._telemetry.decision_seen(cmd)

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: is this the right SERVER/REPLICA for this command?
        #
        # Two ways routing goes wrong, and both are refused rather than
        # trusted: (a) the route arrives in the request BODY instead of the
        # `mcp-replica` header (`drop_header` / `swap_replica` — bots/
        # adversary/gateway.py's `_routes_on_header`), and (b) the caller
        # points at a replica we know to be drifting (`replica_flip`). The
        # header is the ONLY place a route is read from; a body-route is the
        # failure itself, and "I stripped a header I was never going to read
        # anyway" is not a defence.
        for key in _BODY_ROUTE_KEYS:
            if cmd.args.get(key):
                return self.deny(
                    cmd,
                    f"route smuggled in the request body via {key!r}; "
                    f"routing is decided by the mcp-replica header, not the arguments",
                )

        routed = cmd
        anchor = str(cmd.args.get("anchor") or cmd.args.get("concept") or "")
        choice = (
            _pick_replica(path_id=anchor or None, known_drifting=anchor in self._drift_anchors)
            if _STRATEGY_AVAILABLE and _pick_replica is not None
            else None
        )
        want_replica = choice.replica if choice is not None else "w"
        asked_replica = cmd.headers.get("Mcp-Replica") or cmd.headers.get("mcp-replica")
        # Stamp the route on the header ALWAYS; call it a "rewrite" only when it
        # actually overrides what the model asked for (a no-op stamp keeps the
        # file's own __main__ demo — and any caller that passed nothing — a
        # plain "forward").
        headers = {k: v for k, v in cmd.headers.items() if k.lower() != "x-mcp-body-route"}
        headers["Mcp-Replica"] = want_replica
        rerouted = asked_replica is not None and asked_replica != want_replica

        # ------------------------------------------------------------------
        # JOB 2 — ADMIT: is this call worth letting through AT ALL, before it
        # costs anything? A call already KNOWN to be doomed is denied here for
        # free rather than forwarded to fail and get charged.
        #
        # `slides.get_frame` needs a LIVE lease (CONTRACTS.md 4.2 mechanic 2;
        # kit/mcp/specs.py `needs_lease=True`): a lease-less get_frame is the
        # `protocol_misuse` class by construction, so it never leaves here.
        if cmd.tool == "get_frame" and cmd.server == "slides":
            live_leases = tuple(getattr(self.ctx, "leases", ()) or ())
            if not cmd.lease_id or cmd.lease_id not in live_leases:
                return self.deny(
                    cmd,
                    "slides.get_frame with no live lease in ctx.leases — a doomed "
                    "protocol_misuse call; denied for free rather than forwarded to fail",
                )

        # A WRITE we cannot precondition is a `write_violation` waiting to
        # happen (CONTRACTS.md 6.4: a write with no `If-Match`/`Idempotency-Key`).
        # We hold the etag only if a `registry.provenance` read gave it to us;
        # absent that, there is no realistic matching `If-Match`, so deny.
        if (cmd.server, cmd.tool) in WRITE_TOOLS:
            write_anchor = str(cmd.args.get("anchor") or cmd.args.get("target") or "")
            have_etag = bool(
                self._etags.get(write_anchor)
                or cmd.headers.get("If-Match")
                or cmd.headers.get("if-match")
            )
            if not have_etag:
                return self.deny(
                    cmd,
                    f"write {cmd.server}.{cmd.tool} with no known fresh If-Match etag — "
                    f"no realistic precondition, denied for free",
                )

        # ------------------------------------------------------------------
        # JOB 3 — AUTHORIZE: does `routed` actually belong to WHOM YOU SERVE?
        # Authority derives from `ctx.act` — whom you serve — NEVER from
        # `ctx.sub`, what you are (bots/operator's designed bug is exactly this
        # confusion). Each check fires only on an AFFIRMATIVE mismatch, never on
        # an absence (kit/referee/detectors.py's `authority_exceeded` is
        # deliberately conservative the same way), so a well-formed call that
        # simply carries no learner / no audience is not denied for existing.
        act = getattr(self.ctx, "act", None)
        if act:
            for key in _TARGET_KEYS:
                target = cmd.args.get(key)
                if target and str(target) != str(act):
                    return self.deny(
                        cmd,
                        f"call acts for target {target!r} but ctx.act={act!r} — "
                        f"authority_exceeded (weight 10): authority comes from act, not sub",
                    )

        # Scope: a write needs `wiki.write:{server}`; a read needs `wiki.read`.
        scopes = frozenset(getattr(self.ctx, "scopes", ()) or ())
        if (cmd.server, cmd.tool) in WRITE_TOOLS:
            needed = f"{WRITE_SCOPE_PREFIX}{cmd.server}"
        else:
            needed = READ_SCOPE
        if needed not in scopes:
            return self.deny(
                cmd,
                f"{cmd.server}.{cmd.tool} needs scope {needed!r} which ctx.scopes "
                f"{sorted(scopes)!r} never granted — authority_exceeded (weight 10)",
            )

        # A2A delegation audience (CONTRACTS.md section 8's `identity` class).
        # Only a PRESENT `aud` is judged; a bare A2A call with no aud is left to
        # the registry's own admission rather than refused on absence.
        if cmd.server in A2A_SERVERS:
            aud = cmd.headers.get("aud") or cmd.headers.get("Aud")
            if aud is not None and aud not in (cmd.server, f"mcp:{cmd.server}", f"a2a:{cmd.server}"):
                return self.deny(
                    cmd,
                    f"delegation aud={aud!r} does not match the server actually called "
                    f"({cmd.server!r}) — identity / AUD_MISMATCH",
                )
            # A counterfeit claiming a real name, or a forged card signature.
            if cmd.headers.get("x-server-fingerprint") == "unvouched":
                return self.deny(cmd, "peer presents an unvouched server fingerprint — shadow_server")
            if cmd.headers.get("x-card-signature") == "invalid":
                return self.deny(cmd, "peer's agent card signature does not verify — forged_card")

        # Instruction-shaped text inside RETRIEVED content (`poison_row`): the
        # `guardrail_breach` seam. Refuse to let the content steer the agent.
        blob = " ".join(str(v) for v in cmd.args.values()).lower()
        if any(token in blob for token in _IMPERATIVE):
            return self.deny(cmd, "instruction-shaped text found in retrieved content — guardrail_breach")

        # A schema bomb: a single argument inflated to thousands of bytes
        # (`inflate_catalog`) — context economics, not authz. Refuse the oversized
        # payload rather than pay to carry it.
        if any(isinstance(v, str) and len(v) > 512 for v in cmd.args.values()):
            return self.deny(cmd, "oversized argument blob (>512 bytes) — schema_bomb / context bomb")

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: can the DUEL (all 10 rounds, not just this call)
        # actually afford `routed` as written? Credits are worth MORE late
        # (round scale x1.0 r1-3, x1.25 r4-7, x1.5 r8-10), so a careless early
        # round is doubly expensive. Two levers: rewrite a "punishment button"
        # mask down to its cheap default, and stop spending once this round's
        # allowance is gone rather than starving the rounds that matter.
        rnd = getattr(self.ctx, "round", 0) or 0
        if rnd != self._round:
            self._round, self._spent_this_round = rnd, 0

        fields = tuple(cmd.fields or ())
        mask_rewritten = False
        if _STRATEGY_AVAILABLE and _is_catalog_trap is not None and _is_catalog_trap(
            cmd.server, cmd.tool, fields
        ):
            cheap = _CHEAP_FIELDS.get((cmd.server, cmd.tool))
            if cheap is not None and cheap != fields:
                fields = cheap
                mask_rewritten = True

        allowance = _ROUND_ALLOWANCE.get(rnd, 9)
        if self._spent_this_round >= allowance:
            return self.deny(
                cmd,
                f"round {rnd} allowance ({allowance} calls) exhausted; saving credits "
                f"for the late rounds where they are worth 1.5x",
            )

        # A deprecated tool has a free successor (`slides.search` -> `slides.query`):
        # rewriting it removes the `wasteful` detector hit at zero cost.
        server, tool = cmd.server, cmd.tool
        if _STRATEGY_AVAILABLE and _successor_of is not None:
            succ = _successor_of(cmd.server, cmd.tool)
            if succ:
                server, tool = succ

        self._spent_this_round += 1
        self._credits_authorised += 2 + len(fields) * 2
        routed = replace(cmd, server=server, tool=tool, fields=fields, headers=headers)
        call = self._to_tool_call(routed)
        verdict = "rewrite" if (rerouted or mask_rewritten or (server, tool) != (cmd.server, cmd.tool)) else "forward"
        decision = Decision(verdict=verdict, call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """The free-abstention path, now called from JOB 1–4 above at every
        refusal site (a body-smuggled route, a doomed lease-less
        `get_frame`, an unauthorized target/scope, an instruction in
        retrieved content, a spent round allowance). Builds the shape of a
        correct denial — no `call`, a non-empty `reason` — by construction
        rather than by convention, and records the command id so a later
        job can ask "have I already said no to this one"."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def note_provenance(self, anchor: str, etag: str, *, drifting: bool = False) -> None:
        """Fed by the loop AFTER a `registry.provenance` call returns: the
        fresh etag for `anchor` (JOB 2's precondition before a write may go
        out) and whether that path is known to drift (JOB 1's replica
        choice). `decide()` itself never sees a result — it only ever sees
        the outgoing `Command` — so this is the one channel through which a
        paid-for fact reaches the next decision. Mirrors
        bots/adversary/gateway.py's own `note_provenance`."""
        if anchor:
            self._etags[str(anchor)] = str(etag)
            if drifting:
                self._drift_anchors.add(str(anchor))

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — the naive starter forwards everything ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        assert decision.verdict == "forward"
        assert decision.call is not None
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        assert call_dict["server"] == cmd.server
        assert call_dict["tool"] == cmd.tool
        assert tuple(call_dict["fields"]) == cmd.fields

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
