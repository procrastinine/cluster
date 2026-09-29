"""Sessions recorded on nodes no login of this backend occupies.

A breadcrumb in the shared home says "session S is on node N, owned by login
L". A login is exactly one SSH connection to exactly one node, so the moment L
stops sitting on N that record describes something nothing looks for any more —
while still being the only evidence the work exists at all.

Deleting such a record silently is how work goes missing; blocking on it
silently would make `cluster new work api` refuse a session name on a node
that does not have it. So they get a name — **stray** — a place in `cluster ls`,
and one command that reconciles them.

Everything here is derived from local state plus breadcrumbs the caller has
already read: no credentials, no network, no backend construction. That is what
lets `ls` show strays without ever authenticating.
"""

from __future__ import annotations

from collections import namedtuple

from . import ui

#: One recorded session sitting where no login is looking.
Stray = namedtuple("Stray", "backend node session owner state")

#: The recorded owner is a login that still exists, but lives on another node.
#: The common case, and always a leak: something moved the login and left the
#: record behind (a repin off a dead node, a pool redraw, forget + repin).
STRANDED = "stranded"
#: Deliberately left behind: a `retired_owner` tag or a row in abandoned.tsv.
ABANDONED = "abandoned"
#: The recorded owner is gone, so nothing can ever claim it.
ORPHAN = "orphan"
#: Recorded on a node a live login occupies, and provably not running there.
#:
#: This is what both ways of losing work look like afterwards: skipping a
#: crumb on an occupied node would leave full evidence in the shared home that
#: nothing ever mentions. Only a node whose live session list was actually read
#: this run can be judged — "did not ask" must never be reported as "not there".
LOST = "lost"

STRAY_HEADERS = ["BACKEND", "NODE", "SESSION", "RECORDED OWNER", "STATE"]


def occupied_nodes(ctx):
    """``{short node: login}`` for every login of this backend with a node.

    A *disconnected* login still occupies its node: it is pinned there, `ls`
    shows it, and reconnecting goes back to it — so its sessions are not
    strays. Only a node no login names at all has nothing looking at it.
    """
    out = {}
    for name in ctx.state.known_logins():
        node = ctx.state.pin_read(name) or ctx.state.read_meta(name).get("node", "")
        if node:
            out.setdefault(ctx.backend.short(node), name)
    return out


def classify(owner, known_logins):
    """Which kind of stray a crumb's recorded owner makes it."""
    if not owner:
        return ORPHAN
    if owner in known_logins:
        return STRANDED
    # lifecycle.retired_owner() writes 'login@node' when a move deliberately
    # leaves work behind, precisely so a sweep can tell it from a live owner.
    if "@" in owner:
        return ABANDONED
    return ORPHAN


def collect(ctx, crumbs, live=None):
    """``[Stray]`` for *crumbs* that no login is looking after.

    Recorded abandonments are folded in even when their breadcrumb is gone: the
    local record is a statement that work was left running, and it should not
    become invisible just because the shared home was tidied.

    *live* is ``{short node: {session names}}`` for nodes whose session list
    was genuinely read this run. A crumb on an occupied node is normally not a
    stray — something is looking at that node — but when the node was read and
    the session was not in it, the session is gone, and that is worth more than
    silence. Nodes absent from *live* are not judged at all.
    """
    occupied = occupied_nodes(ctx)
    known = set(ctx.state.known_logins())
    live = live or {}
    abandoned = {(node, session): former
                 for node, session, former in ctx.state.abandoned()}
    found = {}
    for (node, session), owner in (crumbs or {}).items():
        if node in occupied:
            seen = live.get(node)
            if seen is None or session in seen:
                continue
            found[(node, session)] = Stray(ctx.backend.name, node, session,
                                           owner or "-", LOST)
            continue
        state = (ABANDONED if (node, session) in abandoned
                 else classify(owner, known))
        found[(node, session)] = Stray(ctx.backend.name, node, session,
                                       owner or "-", state)
    for (node, session), former in abandoned.items():
        if node in occupied or (node, session) in found:
            continue
        found[(node, session)] = Stray(ctx.backend.name, node, session,
                                       former or "-", ABANDONED)
    return [found[key] for key in sorted(found)]


def select(strays, token):
    """Strays matching ``NODE``, ``SESSION`` or ``NODE:SESSION``.

    A bare token is read as a node first, because that is the useful unit: a
    node is what one visit can answer for. ``NODE:SESSION`` disambiguates.
    """
    if not token:
        return list(strays)
    if ":" in token:
        node, _, session = token.partition(":")
        return [s for s in strays
                if s.node == node.strip() and s.session == session.strip()]
    hits = [s for s in strays if s.node == token]
    return hits or [s for s in strays if s.session == token]


def nodes_of(strays):
    """The node names covered by *strays*, in stable order."""
    seen = []
    for stray in strays:
        if stray.node not in seen:
            seen.append(stray.node)
    return seen


def render(strays):
    """The stray table, or '' when there are none."""
    if not strays:
        return ""
    return ui.render_table(
        [[s.backend, s.node, s.session, s.owner, s.state] for s in strays],
        STRAY_HEADERS)


def report(strays, prefix="cluster strays"):
    """Print the table and the one command that reconciles each kind.

    Lost records are separated out because they are a different statement and
    need a different answer. A stray says "work may be running where nothing
    is looking" — the question is whether it still exists. A lost record says
    "work that was here is gone", which is not a question, and the only useful
    reply is what can be rebuilt from the layout snapshot.
    """
    if not strays:
        return
    lost = [s for s in strays if s.state == LOST]
    rest = [s for s in strays if s.state != LOST]
    if rest:
        ui.say(ui.yellow(render(rest)))
        ui.note(f"{len(rest)} stray record(s) — nothing is looking at "
                f"{'these nodes' if len(nodes_of(rest)) > 1 else 'this node'}: "
                + ", ".join(nodes_of(rest)))
        ui.note(f"see if they still exist: {prefix} check {nodes_of(rest)[0]}")
    if lost:
        ui.say(ui.red(render(lost)))
        ui.warn(f"{len(lost)} session(s) recorded on a node that is not "
                f"running them — the processes in them are gone")
        for owner, node in sorted({(s.owner, s.node) for s in lost}):
            ui.note(f"windows and cwds can be rebuilt: "
                    f"cluster restore-layout {owner} {node}")
        ui.note("then drop the records: cluster clean")
        ui.note("why it happened: cluster doctor (linger, and tmux scope)")
