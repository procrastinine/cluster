"""Read-only, concurrent fleet inventory for ``cluster ls``.

Keeping this separate from command dispatch makes its important constraint
obvious: it may inspect existing local masters and their nodes, but must never
authenticate or create a connection merely to answer where work is.
"""

from __future__ import annotations

import sys

from . import backends, registry, strays as straylib, ui, workstation

LIST_HEADERS = ["BACKEND", "LOGIN", "STATE", "NODE", "PINNED", "MOUNT", "SESSIONS"]


def _cached_session_label(names):
    """Render names whose remote tmux server could not be queried this time."""
    return "cached: " + ", ".join(names)


def _crumb_rows(crumbs):
    """Breadcrumbs as JSON-friendly rows for the saved evidence file; the
    fourth field is the workstation that wrote each, "" when none did."""
    return [[node, session, owner, getattr(owner, "workstation", "")]
            for (node, session), owner in sorted((crumbs or {}).items())]


def _crumb_map(rows):
    """The inverse of :func:`_crumb_rows`, tolerant of a hand-edited file."""
    out = {}
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0] and row[1]:
            out[(row[0], row[1])] = workstation.Owner(
                row[2] if len(row) > 2 else "",
                row[3] if len(row) > 3 and isinstance(row[3], str) else "")
    return out


def cached_crumbs(c):
    """The last breadcrumb catalogue saved for this backend by `ls`."""
    for item in c.state.read_list_evidence().get("evidence", []):
        if isinstance(item, dict) and item.get("crumbs"):
            return _crumb_map(item["crumbs"])
    return {}


def read_crumbs(c, with_live=False):
    """``(crumbs, source)`` for one backend without ever authenticating.

    Over an existing master when there is one — a crumb read is a listing of
    the shared home, so it costs one channel and no credential — and otherwise
    from what `ls` last saved. `ls`, `strays` and `doctor` all have to be able
    to say where work is recorded without spending a TOTP window.

    *with_live* returns ``(crumbs, source, live)`` instead, where *live* is
    ``{node: {sessions}}`` for the nodes actually read. It rides the same round
    trip, so it is free, and it is what lets a caller tell a record describing
    living work from one describing work that is gone.
    """
    live = {}
    crumbs = None
    for name in c.logins.active_names():
        node, sessions, found = c.tmux.node_sessions_and_crumbs(
            name, with_sessions=with_live)
        if with_live and node:
            live[c.backend.short(node)] = {row.name for row in sessions}
        if found is not None and crumbs is None:
            crumbs = found
            if not with_live:
                break
    if crumbs is not None:
        return (crumbs, "live", live) if with_live else (crumbs, "live")
    cached = cached_crumbs(c)
    return (cached, "cached", live) if with_live else (cached, "cached")


def list_evidence(c, name, quiet=False, cached_sessions=()):
    """One inventory row, costing at most one round trip to a live node."""
    active = c.logins.is_active(name)
    pinned = c.state.pin_read(name)
    node = ""
    sessions = "-"
    session_names = []
    sessions_loaded = False
    crumbs = None
    if active:
        # The breadcrumb catalogue rides the same channel as the node and the
        # session list: it is what tells `ls` about sessions recorded on nodes
        # nothing is connected to, and asking for it separately would double
        # the cost of the one round trip this function is allowed.
        live, found, crumbs = c.tmux.node_sessions_and_crumbs(
            name, with_sessions=not quiet)
        sessions_loaded = bool(live)
        node = c.backend.short(live)
        if not quiet:
            session_names = [row.name for row in found]
            if sessions_loaded:
                sessions = ", ".join(
                    row.name + ("*" if row.attached else "") for row in found
                ) or "none"
            else:
                sessions = "?"
    if not quiet and not sessions_loaded and cached_sessions:
        # A dead ControlMaster says nothing about node-local tmux. Hiding the
        # last successful catalogue behind '-' would make a routine reconnect
        # look as though it had recovered orphaned work. Names are useful
        # evidence, but attached state is deliberately omitted because it is
        # not live evidence.
        session_names = list(cached_sessions)
        sessions = _cached_session_label(session_names)
    pin_short = c.backend.short(pinned) or "-"
    if node and pin_short != "-" and node != pin_short:
        node += "!"
    state = "active" if active else ("down" if pinned else "stale")

    mount = ""
    if c.mounts.is_mounted(name):
        via = c.state.mountnode_read(name)
        mount = f"mounted{f' via {c.backend.short(via)}' if via else ''}"
    else:
        holder = c.mounts.mounted_elsewhere(name)
        if holder:
            mount = f"shares {holder}"
    evidence = {
        "login": name,
        "row": [c.backend.name, name, state, node or "-", pin_short,
                mount or "-", sessions],
        "sessions": session_names,
        "sessions_loaded": sessions_loaded,
        # The node as read, without the '!' the row may carry, and whether the
        # session list is a real answer about it. In quiet mode the node is
        # read and the sessions are not, so `sessions_loaded` alone would say
        # "this node is running nothing" about a node nobody asked.
        "node": node,
        "sessions_listed": bool(sessions_loaded and not quiet),
    }
    if crumbs is not None:
        evidence["crumbs"] = _crumb_rows(crumbs)
    return evidence


def _cached(ctx):
    saved = []
    for backend_name in ctx.scope():
        allowed = set(registry.logins_of(backend_name))
        if not allowed:
            continue
        snapshot = ctx.sibling(backend_name).state.read_list_evidence()
        by_login = {
            item.get("login"): item
            for item in snapshot.get("evidence", [])
            if isinstance(item, dict) and item.get("login") in allowed
        }
        for login in registry.logins_of(backend_name):
            item = by_login.get(login)
            if item and len(item.get("row", [])) == len(LIST_HEADERS):
                saved.append(item)
    return saved


def _render(evidence, quiet=False):
    rows = [item["row"] for item in evidence]
    headers = LIST_HEADERS
    if quiet:
        rows = [row[:-1] for row in rows]
        headers = headers[:-1]
    return ui.render_table(rows, headers)


class _Optimistic:
    """A first paint of saved evidence that can be taken back — or given up on.

    Saving the cursor with ``\\033[s`` and restoring it with ``\\033[u`` would
    not do: that position is absolute, so anything that moves the screen
    underneath invalidates it — the block scrolling, or the terminal being
    resized during the second or two the live probe takes — and the restore
    would land somewhere else and draw the live table over the cached one.

    Counting our own rows and moving up by that many is relative, so scrolling
    cannot break it, and the geometry is measured again before the erase: if
    the terminal changed size under us, the cached table is left where it is
    and the live one is printed below. Two tables in the scrollback is a poor
    result; two tables written over each other is an unreadable one.
    """

    def __init__(self, enabled, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = enabled
        self.rows = 0
        self.size = None

    def paint(self, text):
        """Show `text` now if it can be taken back later, and say whether it was.

        A block taller than the screen is not painted at all. It would scroll,
        which is precisely when the erase has to be given up on, and the result
        would be the saved evidence and the live evidence both in full — twice
        the scrolling, for a preview that had already scrolled out of sight.
        Narrow terminals reach that size easily, because a table too narrow to
        tabulate becomes one stacked record per login.
        """
        if not self.enabled:
            return False
        size = ui.terminal_size(self.stream)
        rows = ui.visual_lines(text, size[0])
        if size[1] and rows >= size[1]:
            return False
        self.size, self.rows = size, rows
        self.stream.write(text + "\n")
        self.stream.flush()
        return True

    def erase(self):
        """Take the paint back, or keep it and separate it from what follows."""
        painted, self.rows = self.rows, 0
        if not painted:
            return False
        size = ui.terminal_size(self.stream)
        height = size[1]
        if size != self.size or (height and painted >= height):
            self.stream.write("\n")
            self.stream.flush()
            return False
        self.stream.write(f"\r\033[{painted}A\033[J")
        self.stream.flush()
        return True


def _evidence_live(evidence, backend_name):
    """``{node: {sessions}}`` for nodes this run actually listed."""
    return {item["node"]: set(item.get("sessions", ()))
            for item in evidence
            if item["row"][0] == backend_name and item.get("sessions_listed")
            and item.get("node")}


def _evidence_crumbs(evidence, backend_name):
    """Crumbs read live this run for *backend_name*, or None if none were."""
    for item in evidence:
        if item["row"][0] == backend_name and "crumbs" in item:
            return _crumb_map(item["crumbs"])
    return None


def _save(ctx, evidence):
    grouped = {}
    for item in evidence:
        grouped.setdefault(item["row"][0], []).append(item)
    for backend_name, items in grouped.items():
        ctx.sibling(backend_name).state.write_list_evidence(items)


def _in_parallel(function, items, workers):
    """``[function(item) for item in items]``, on up to *workers* threads.

    concurrent.futures is imported here, when there is more than one login to
    ask, because importing it costs every command several milliseconds.
    """
    from concurrent import futures

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, items))


def say_nothing_set_up():
    """What a fleet view says on a machine with no backend set up: how to start.

    Not an error: nothing is wrong, and a script asking "what is running"
    gets a true answer, which is nothing.
    """
    ui.say("no cluster is set up on this machine yet")
    ui.note(f"set one up with {backends.setup_commands()}")
    return 0


def run(ctx, quiet=False):
    interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
    # Saved session names are useful even in redirected output: the final live
    # row must distinguish "could not ask" from "the server has no sessions".
    # Only the optimistic first paint itself is terminal-specific.
    cached = _cached(ctx)
    cached_sessions = {
        (item["row"][0], item["login"]): tuple(item.get("sessions", ()))
        for item in cached
        if len(item.get("row", ())) == len(LIST_HEADERS)
    }
    painter = _Optimistic(interactive and bool(cached))
    painter.paint(_render(cached, quiet) +
                  "\n(saved evidence; loading live evidence...)")

    targets = ctx.scope_all()
    # Instantiate lazy Mounts before worker threads can race to create it.
    for candidate, _name in targets:
        candidate.mounts
    if len(targets) > 1:
        limit = ctx.settings.int("LIST_WORKERS")
        evidence = _in_parallel(
            lambda target: list_evidence(
                target[0], target[1], quiet,
                cached_sessions.get((target[0].backend.name, target[1]), ())),
            targets, min(limit, len(targets)))
    else:
        evidence = [list_evidence(
            candidate, name, quiet,
            cached_sessions.get((candidate.backend.name, name), ()))
                    for candidate, name in targets]

    if not evidence:
        painter.erase()
        where = "on " + ctx.backend.label if ctx.explicit_backend else "anywhere"
        ui.say(f"no connections {where}")
        ui.note("open one with: cluster new NAME")
        if interactive:
            ui.say("(live evidence loaded)")
        return 0
    if not quiet:
        _save(ctx, evidence)
    painter.erase()
    # Re-rendered, not reused: the terminal may be a different width than it
    # was when the cached table was painted.
    ui.say(_render(evidence, quiet))

    found, elsewhere, stale_evidence = [], [], False
    for backend_name in ctx.scope():
        sub = ctx.sibling(backend_name)
        crumbs = _evidence_crumbs(evidence, backend_name)
        if crumbs is None:
            crumbs, stale_evidence = cached_crumbs(sub), True
        found += straylib.collect(sub, crumbs,
                                  live=_evidence_live(evidence, backend_name))
        elsewhere += straylib.elsewhere(sub, crumbs)
        # Abandonments on a node a login still occupies are not strays —
        # something is looking at them, and `clean` can reap them there.
        occupied = straylib.occupied_nodes(sub)
        for node, session, former in sub.state.abandoned():
            if node in occupied:
                ui.warn(f"{backend_name}: session '{session}' left on {node} "
                        f"when '{former}' moved off it")
                ui.note("reap it with: cluster clean")
    if elsewhere:
        ui.say("")
        straylib.report_elsewhere(elsewhere)
    if found:
        ui.say("")
        straylib.report(found)
        if stale_evidence:
            ui.note("(from saved evidence; no connection was open to re-read it)")

    for login, owners in sorted(registry.collisions().items()):
        ui.warn(f"login '{login}' exists on {' and '.join(owners)}; "
                f"rename one: cluster rename {login} NEWNAME")
    if interactive:
        ui.say("(live evidence loaded)")
    return 0
