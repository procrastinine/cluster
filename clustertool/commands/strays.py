"""Reconciling sessions recorded where no login is looking.

A stray is a breadcrumb (or an abandonment record) on a node no login of that
backend occupies — see :mod:`clustertool.strays` for why they happen and why
neither deleting nor ignoring them is acceptable. This module is the only place
that acts on one, and every verb prints the command that follows it.

Cost is stated before it is spent: listing reads local state and, at most, an
already-open master. Anything that must find out what is really on a node opens
a connection to that node, which on a TOTP-paced backend is one window.
"""

from __future__ import annotations

import argparse

from .. import backends, registry, strays as straylib, tmuxlayer, ui
from ..auth import is_rejection
from ..command import command
from ..listing import read_crumbs

VERBS = ("list", "check", "adopt", "rename", "clear", "kill")


def _parser():
    parser = argparse.ArgumentParser(prog="cluster strays", add_help=False)
    parser.add_argument("-y", "--yes", action="store_true",
                        help="do not ask for confirmation")
    parser.add_argument("--as", dest="as_login",
                        help="login name to adopt a node under")
    parser.add_argument("words", nargs="*")
    return parser


def _confirm(question, yes):
    return ui.confirm(question, yes)


def _gather(ctx):
    """``[(context, Stray)]`` across every backend in scope. Never authenticates."""
    out = []
    for backend_name in ctx.scope():
        sub = ctx.sibling(backend_name)
        # with_live so a record naming a session its own node is not running
        # shows up here too, and not only in `ls`: the same loss reported by
        # one surface and silently skipped by another is how it gets missed.
        crumbs, _source, live = read_crumbs(sub, with_live=True)
        out += [(sub, stray)
                for stray in straylib.collect(sub, crumbs, live=live)]
    return out


def _pick(ctx, token):
    """``(context, [Stray])`` for *token*, or die explaining what does exist.

    Selection runs over the whole list, not stray by stray: `select` resolves a
    bare token as a node *first* and only falls back to session names when no
    node matches, and asking it one row at a time would lose that precedence.
    """
    found = _gather(ctx)
    if not found:
        ui.die("no stray records", "nothing is recorded on a node without a login")
    owner = {(s.backend, s.node, s.session): sub for sub, s in found}
    matched = straylib.select([s for _sub, s in found], token)
    if not matched:
        ui.warn(f"no stray matches '{token}'")
        straylib.report([s for _sub, s in found])
        raise SystemExit(1)
    hit = {s.backend for s in matched}
    if len(hit) > 1:
        ui.die(f"'{token}' matches strays on {' and '.join(sorted(hit))}",
               "name the backend: cluster --fasrc strays ...")
    first = matched[0]
    sub = owner[(first.backend, first.node, first.session)]
    sub.by_hand()
    return sub, matched


def _executor(sub):
    """A live login of this backend, for writing to the shared home."""
    return next(iter(sub.logins.active_names()), "")


def _forget(sub, chosen, reason):
    """Drop the records for *chosen* — breadcrumb and abandonment row alike.

    Both or neither: a local record without its breadcrumb goes stale on the
    next machine, and a breadcrumb without the local record is exactly the leak
    this whole feature exists to make visible.
    """
    executor = _executor(sub)
    if not executor:
        ui.die("no connection is open on this backend, so the shared-home "
               "records cannot be removed",
               f"open one first: cluster login {sub.settings.str('DEFAULT_LOGIN')}")
    dropped, stuck = [], []
    for stray in chosen:
        if sub.tmux.crumb_remove(executor, stray.session, node=stray.node):
            sub.state.abandon_forget(stray.node, stray.session)
            dropped.append(f"{stray.node}:{stray.session}")
        else:
            stuck.append(f"{stray.node}:{stray.session}")
    if dropped:
        ui.info(f"dropped {len(dropped)} record(s) ({reason}): "
                + ", ".join(dropped))
    if stuck:
        ui.warn("could not remove: " + ", ".join(stuck))
    return 1 if stuck else 0


def _visit(sub, node):
    """``(reachable, {live sessions}, reason)`` for one node, via a connection.

    A login already on that node would be cheaper, but a stray is by definition
    on a node no login occupies, so there is never one to borrow.
    """
    complete, rows, why = sub.tmux.list_sessions_direct_explained(
        sub.backend.fqdn(node))
    return complete, {row.name for row in rows}, why


def _refused(sub, left):
    """Whether the last visit's credential was refused, then said once, with
    the nodes *left*: every node refuses it the same way, and each try
    counts towards locking the account."""
    if not is_rejection(sub.logins.last_failure):
        return False
    if left:
        ui.warn(f"the credential was refused ({sub.logins.last_failure}); not "
                f"tried on {', '.join(left)}, whose records are unchanged")
    return True


def _note_cost(sub, nodes):
    """State what visiting costs. Each verb asks its own single question."""
    price = ("one authentication each" if sub.backend.paces_totp
             else "a new connection each")
    ui.note(f"visiting {len(nodes)} node(s) — {price}: {', '.join(nodes)}")


# --- verbs -----------------------------------------------------------------
def _cmd_list(ctx, token, _opts):
    found = straylib.select([s for _sub, s in _gather(ctx)], token)
    if not found:
        ui.say("no stray records")
        ui.note("every recorded session is on a node some login is looking at")
        return 0
    straylib.report(found)
    ui.note("then: cluster strays adopt NODE --as NAME   (take them back)")
    ui.note("      cluster strays rename NODE:SESSION NEW  (free the name)")
    ui.note("      cluster strays kill NODE               (kill and forget)")
    ui.note("      cluster strays clear NODE              (forget the record only)")
    return 0


def _cmd_check(ctx, token, opts):
    sub, chosen = _pick(ctx, token)
    nodes = straylib.nodes_of(chosen)
    _note_cost(sub, nodes)
    if not _confirm("go ahead?", opts.yes):
        ui.die("aborted; nothing was visited")
    gone, unreachable = [], []
    for index, node in enumerate(nodes):
        here = [s for s in chosen if s.node == node]
        reachable, live, why = _visit(sub, node)
        if not reachable:
            unreachable.append(node)
            ui.warn(f"{node}: {why}; its records are unchanged")
            if _refused(sub, nodes[index + 1:]):
                unreachable += nodes[index + 1:]
                break
            continue
        for stray in here:
            if stray.session in live:
                ui.say(f"{node}:{stray.session} — still running")
            else:
                ui.say(ui.dim(f"{node}:{stray.session} — gone"))
                gone.append(stray)
    if unreachable:
        ui.note("a node that refuses connections keeps its records: they are "
                "the only evidence the work exists")
    if not gone:
        if not unreachable:
            ui.say("every stray session is still running")
            ui.note("take them back with: cluster strays adopt "
                    f"{nodes[0]} --as NAME")
        return 1 if unreachable else 0
    if not _confirm(f"drop {len(gone)} record(s) for sessions that are gone?",
                    opts.yes):
        ui.say("left as they are")
        return 0
    return _forget(sub, gone, "proven gone") or (1 if unreachable else 0)


def _cmd_clear(ctx, token, opts):
    sub, chosen = _pick(ctx, token)
    straylib.report(chosen)
    ui.warn("this removes the record only — nothing on the cluster is touched")
    ui.note("a session that is still running becomes invisible to this tool; "
            f"`cluster strays check {chosen[0].node}` finds out first")
    if not _confirm(f"forget {len(chosen)} record(s)?", opts.yes):
        ui.die("aborted; nothing was changed")
    return _forget(sub, chosen, "cleared")


def _cmd_kill(ctx, token, opts):
    sub, chosen = _pick(ctx, token)
    nodes = straylib.nodes_of(chosen)
    straylib.report(chosen)
    ui.warn(f"this kills {len(chosen)} tmux session(s); anything running in "
            f"them is lost")
    _note_cost(sub, nodes)
    if not _confirm(f"kill {len(chosen)} session(s)?", opts.yes):
        ui.die("aborted; nothing was killed")
    rc = 0
    for index, node in enumerate(nodes):
        here = [s for s in chosen if s.node == node]
        # Same ending as every other sweep: the node is settled in the command
        # that kills, so it does not linger for records deleted here.
        killed, failed = sub.tmux.kill_sessions_direct(
            sub.backend.fqdn(node), [s.session for s in here], settle=True)
        if killed:
            ui.info(f"killed on {node}: {', '.join(killed)}")
            rc |= _forget(sub, [s for s in here if s.session in killed], "killed")
        if failed:
            # Unconfirmed dead means the record stays: that is the rule
            # everywhere else in this tool and the reason work stays findable.
            ui.warn(f"could not confirm dead on {node}: {', '.join(failed)}")
            ui.note("their records are kept")
            rc = 1
            if _refused(sub, nodes[index + 1:]):
                break
    return rc


def _cmd_adopt(ctx, token, opts):
    sub, chosen = _pick(ctx, token)
    nodes = straylib.nodes_of(chosen)
    if len(nodes) != 1:
        ui.die(f"adopt takes one node; '{token}' spans {', '.join(nodes)}",
               "a login is one connection to one node")
    node = nodes[0]
    owners = {s.owner for s in chosen}
    name = opts.as_login
    if not name:
        # The recorded owner is the natural name, but only when nothing else
        # answers to it: a stranded session's owner is a live login elsewhere.
        free = [o for o in owners if o != "-" and "@" not in o
                and o not in set(sub.state.known_logins())]
        if len(free) == 1:
            name = free[0]
        else:
            ui.die(f"name the login to adopt {node} under",
                   f"use: cluster strays adopt {node} --as NAME")
    tmuxlayer.require_name(name, "login name")
    if name not in sub.state.known_logins():
        backends.refuse_backend_name(name)
    if registry.is_taken_elsewhere(name, sub.backend.name):
        ui.die(f"login '{name}' already exists on "
               f"{registry.find(name)}", "login names are global")
    fqdn = sub.backend.fqdn(node)
    if sub.state.pin_read(name) and sub.backend.short(sub.state.pin_read(name)) != node:
        ui.die(f"login '{name}' is already pinned to "
               f"{sub.backend.short(sub.state.pin_read(name))}",
               "pick another name, or move it: "
               f"cluster repin {name} {node}")
    holder = sub.state.login_pinned_to(fqdn, exclude=name, short=sub.backend.short)
    if holder and sub.settings.flag("ONE_LOGIN_PER_NODE"):
        ui.die(f"{node} is already login '{holder}'s node",
               "sessions are node-local: two logins on one node list each "
               "other's tmux sessions",
               f"attach there instead: cluster attach {holder} SESSION")

    _note_cost(sub, [node])
    # The pin is written before connecting, so the connection dials the node
    # itself rather than the pool address — and if it fails, the intent is
    # recorded rather than lost.
    sub.state.pin_write(name, fqdn)
    ui.info(f"login '{name}' pinned to {node}")
    sub.logins.ensure(name)
    taken, missed = [], []
    for stray in chosen:
        ok = sub.tmux.tag_owner(name, stray.session, owner=name, force=True)
        ok = sub.tmux.crumb_add(name, stray.session, node=fqdn, owner=name) and ok
        sub.state.abandon_forget(stray.node, stray.session)
        (taken if ok else missed).append(stray.session)
    if taken:
        ui.info(f"adopted on {node}: {', '.join(taken)}")
        ui.note(f"attach to one: cluster attach {name} {taken[0]}")
    if missed:
        ui.warn(f"could not fully re-tag: {', '.join(missed)}")
        ui.note(f"check what is there: cluster sessions {name}")
    return 1 if missed else 0


def _cmd_rename(ctx, words, opts):
    if len(words) < 2:
        ui.die("rename needs a stray and a new name",
               "usage: cluster strays rename NODE:SESSION NEWNAME")
    token, new = words[0], words[1]
    sub, chosen = _pick(ctx, token)
    if len(chosen) != 1:
        ui.die(f"'{token}' matches {len(chosen)} strays; name one exactly",
               "usage: cluster strays rename NODE:SESSION NEWNAME")
    stray = chosen[0]
    crumbs, _source = read_crumbs(sub)
    clash = [node for (node, session) in crumbs if session == new]
    if clash:
        ui.die(f"a session named '{new}' is already recorded on "
               f"{', '.join(sorted(clash))}",
               "one name is one thing: pick another")
    _note_cost(sub, [stray.node])
    if not _confirm(f"rename '{stray.session}' on {stray.node} to '{new}'?",
                    opts.yes):
        ui.die("aborted; nothing was renamed")
    if not sub.tmux.rename_session_direct(sub.backend.fqdn(stray.node),
                                          stray.session, new):
        ui.die(f"could not rename '{stray.session}' on {stray.node}",
               "the record is unchanged")
    executor = _executor(sub)
    if executor:
        sub.tmux.crumb_add(executor, new, node=stray.node, owner=stray.owner)
        sub.tmux.crumb_remove(executor, stray.session, node=stray.node)
    else:
        ui.warn("renamed, but no connection was open to move the breadcrumb")
    sub.state.abandon_forget(stray.node, stray.session)
    ui.info(f"{stray.node}: '{stray.session}' is now '{new}'")
    ui.note(f"the name '{stray.session}' is free again")
    return 0


@command("strays", "stray",
         help="Sessions recorded on nodes no login is looking at")
def cmd_strays(ctx, args):
    """Usage: cluster strays [check|adopt|rename|clear|kill] [TARGET]

A stray is a session recorded on a node no login of that backend occupies —
left behind when a login moved node, was reset, or lost one. Nothing looks
there any more, so the record is the only evidence the work exists.

  cluster strays                     what is recorded, and where  (free)
  cluster strays check NODE          do those tmux sessions still exist?
  cluster strays adopt NODE --as X   put them back under a login called X
  cluster strays rename NODE:S NEW   free the name S, keeping the session
  cluster strays clear TARGET        forget the record, touch nothing remote
  cluster strays kill TARGET         kill the sessions, then forget them

TARGET is a node, a session name, or NODE:SESSION. Listing never authenticates;
anything that visits a node says so, and asks, before it does.
"""
    opts = _parser().parse_args(args)
    words = opts.words
    verb = words[0] if words and words[0] in VERBS else "list"
    rest = words[1:] if words and words[0] in VERBS else words
    token = rest[0] if rest else ""
    if verb == "list":
        return _cmd_list(ctx, token, opts)
    if verb == "rename":
        return _cmd_rename(ctx, rest, opts)
    if not token:
        ui.die(f"'{verb}' needs a target",
               f"usage: cluster strays {verb} NODE|SESSION|NODE:SESSION",
               "see what there is: cluster strays")
    return {"check": _cmd_check, "adopt": _cmd_adopt,
            "clear": _cmd_clear, "kill": _cmd_kill}[verb](ctx, token, opts)
