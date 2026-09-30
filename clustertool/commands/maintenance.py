"""Health and upkeep: checkups, reboot safety, renaming and tidying up."""

from __future__ import annotations

import argparse

from .. import (backends, platform as plat, registry, strays as straylib,
                tmuxlayer, ui)
from ..auth import is_rejection
from ..command import command


@command("linger", help="Make connected nodes keep tmux when this machine goes")
def cmd_linger(ctx, args):
    """Reboot safety in one verb, for a machine on its way down or just curious.

    FASRC login nodes kill an account's leftover processes when its last session
    on the node ends, and a reboot here is exactly that. Linger is the
    exemption; this asserts it on every connected login, and says which nodes
    would not take it.

    It rides masters that are already open and never authenticates, which is
    what makes it safe to call from a shutdown hook, where nothing can type a
    code and the network has seconds left. See clustertool.linger, and
    extras/cluster-linger.service for running it at the right moment.
    """
    parser = argparse.ArgumentParser(prog="cluster linger", add_help=False)
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print nothing unless a node refuses")
    parser.add_argument("--remove-keeper", action="store_true",
                        help="take the node-side crontab line back out")
    parser.add_argument("--install-hook", action="store_true",
                        help="install and enable the shutdown hook, then exit")
    opts = parser.parse_args(args)
    from .. import linger as lingerlib

    if opts.install_hook:
        from ..diagnostics import (NO_SYSTEMD, NO_SYSTEMD_HINT, has_systemd,
                                   install_hook)

        if not has_systemd():
            ui.die(NO_SYSTEMD, *([NO_SYSTEMD_HINT] if plat.IS_MAC else []))
        kind, detail = install_hook()
        (ui.info if kind else ui.warn)(
            f"shutdown hook: {detail}" if kind else
            f"could not install the shutdown hook: {detail}")
        if kind == "user":
            ui.note("re-run with sudo available for the ordered system unit")
        return 0 if kind else 1

    def protect(logins, name):
        """Everything one node needs, in as few round trips as it takes."""
        if opts.remove_keeper:
            return lingerlib.remove_keeper(logins, name)
        return lingerlib.apply(logins, name)

    targets = []
    for backend_name in ctx.scope():
        sibling = ctx.sibling(backend_name)
        if not lingerlib.required(sibling.logins):
            continue
        for name in sorted(sibling.logins.active_names()):
            node = sibling.backend.short(sibling.logins.node_of(name))
            targets.append((sibling, name, node or backend_name))

    # Concurrently, because one loaded node must not decide how long a shutdown
    # takes: the wall time is the slowest login, not their sum.
    asserted, refused, considered = [], [], len(targets)
    if targets:
        from concurrent import futures

        with futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            done = [(label, pool.submit(protect, sibling.logins, name))
                    for sibling, name, label in
                    ((s, n, f"{n} ({where})") for s, n, where in targets)]
        for label, task in done:
            (asserted if task.result() else refused).append(label)

    verb = "remove the keeper from" if opts.remove_keeper else "enable linger on"
    for label in refused:
        ui.warn(f"{label}: could not {verb}")
    if refused and not opts.remove_keeper:
        ui.note("work on that node will not survive losing every connection")
    if not opts.quiet:
        if asserted:
            ui.say(("keeper removed: " if opts.remove_keeper
                    else "linger asserted: ") + ", ".join(asserted))
        elif not refused:
            ui.say("nothing connected that needs linger"
                   if not considered else "no login in scope needs linger")
    return 1 if refused else 0


@command("status", "st", help="One-glance health: logins, mounts, tmux, connections")
def cmd_status(ctx, args):
    from ..diagnostics import status
    from ..listing import say_nothing_set_up

    if ctx.nothing_set_up():
        return say_nothing_set_up()
    # Same scope rule as `ls`: every backend unless one was named.
    rc = 0
    for backend_name in ctx.scope():
        rc |= status(ctx.sibling(backend_name))
    return rc


@command("doctor", needs_context=False,
         help="Check prerequisites and per-login state")
def cmd_doctor(invocation, args):
    """This machine first, then every backend: set up, reachable, healthy.

    The machine-wide checks run whether or not any backend is set up, since
    "what does this machine still need" is the first question on a new one.
    With a backend named, only that backend is checked; otherwise each one
    the tool knows is, and one not set up says the command that sets it up.
    """
    from .. import diagnostics
    from ..context import Context

    report = diagnostics.Report()
    # The machine-wide checks are about this workstation, so they are asked
    # once: repeating them per backend would bury the per-backend answers.
    diagnostics.machine_checks(report)
    if invocation.explicit_backend:
        names = [backends.resolve_name(invocation.backend_name)
                 or backends.default_name()]
    else:
        names = sorted(backends.BACKENDS)
    set_up = 0
    for name in names:
        if not backends.configured(name):
            _not_set_up(report, name, invocation.explicit_backend)
            continue
        ctx = Context(name, explicit=True)
        try:
            diagnostics.backend_checks(ctx, report)
        except backends.BackendUnavailable as exc:
            report.check(name, False, f"{exc.reason}; {exc.fix}")
            continue
        set_up += 1
    if not set_up and not invocation.explicit_backend:
        report.section("")
        report.check("clusters", False, "none is set up on this machine yet: "
                     f"run {backends.setup_commands()}")
    # A duplicated name breaks the one-name-one-connection rule that every
    # other command relies on, so it is a health problem, not a cosmetic one.
    duplicated = registry.collisions()
    if duplicated:
        report.section("\nlogin names")
        for login, owners in sorted(duplicated.items()):
            report.check(f"login '{login}' exists on {' and '.join(owners)}",
                         False, f"rename one: cluster --backend {owners[-1]} "
                                f"rename {login} NEWNAME")
    return report.finish()


def _not_set_up(report, name, explicit):
    """One backend without a username here: a finding only if it matters."""
    report.section(f"\ncredentials ({name})")
    fix = f"set it up with: {backends.BACKENDS[name].setup_command()}"
    recorded = registry.logins_of(name)
    if explicit:
        report.check(name, False, f"not set up; {fix}")
    elif recorded:
        report.check(name, False,
                     f"not set up, but logins are recorded for it "
                     f"({', '.join(recorded)}); {fix}", fatal=False)
    else:
        report.line("off", name, f"not set up; {fix}")


@command("forget", "reset-state",
         help="Drop local state for a backend without touching the cluster")
def cmd_forget(ctx, args):
    """Clear this machine's record of a backend's logins. Purely local.

    For after a cluster maintenance restart: the nodes came back with no tmux,
    so every pin, mountpoint and socket here refers to something that is
    gone. This drops all of it *without sending anything to the cluster* — no
    tmux is killed, no breadcrumb removed, nothing authenticated. If sessions did
    survive, `clean` is the command that reasons about them; this one only
    forgets.

    Scoped to one backend on purpose, since that is what a maintenance window
    is. --all-backends does every one.
    """
    parser = argparse.ArgumentParser(prog="cluster forget", add_help=False)
    parser.add_argument("names", nargs="*", help="logins to forget (default: all)")
    parser.add_argument("--all-backends", action="store_true",
                        help="every backend, not just this one")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="say what would happen, change nothing")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="do not ask for confirmation")
    opts = parser.parse_args(args)

    if opts.all_backends:
        rc = 0
        for backend_name in ctx.every_backend():
            ui.say(ui.bold(f"\n== {backend_name} =="))
            rc |= _forget_one(ctx.sibling(backend_name), opts, opts.names)
        return rc
    return _forget_one(ctx, opts, opts.names)


def _forget_one(ctx, opts, names):
    backend_name = ctx.backend.name
    targets = list(names) if names else registry.logins_of(backend_name)
    if not targets:
        ui.say(f"no local state for {backend_name}")
        return 0

    ui.say(f"{'would forget' if opts.dry_run else 'forgetting'} local state on "
           f"{backend_name}: {', '.join(targets)}")
    ui.note("remote tmux sessions are left running and untouched")
    if opts.dry_run:
        return 0
    if not ui.confirm(f"drop local state for {backend_name}?", opts.yes,
                      "pass -y to forget it unattended"):
        ui.die("aborted")

    for name in targets:
        # Stop watching first, or the watcher would recreate what we remove.
        ctx.mounts.stop_watcher(name, quiet=True)
        # Unmounting and dropping the master are local acts: the far side keeps
        # running, which is exactly the point.
        ctx.mounts.unmount(name, quiet=True)
        ctx.mounts.close_mount_master(name)
        ctx.logins.close(name, keep_tmux=True, keep_pin=False, quiet=True)
        ctx.state.forget_login_files(name)
        ctx.state.pin_clear(name)
        ctx.state.drop_meta(name)
        # Leave no directory named after a login that is gone. rmdir
        # refuses while anything is still mounted or stored there, which is the
        # safety check we want.
        try:
            ctx.state.default_mountpoint(name).rmdir()
        except OSError:
            pass
        ui.info(f"forgot '{name}'")

    # The ledger is a local cache of nodes seen; it is stale for the same reason.
    # So is the abandonment record: after a maintenance window the sessions it
    # names are gone with everything else.
    for leftover in ("nodes.seen", "abandoned.tsv"):
        (ctx.state.dir / leftover).unlink(missing_ok=True)
    return 0


@command("rename", help="Rename a login (its tmux sessions follow)")
def cmd_rename(ctx, args):
    """Rename a login, keeping its connection and its sessions.

    Login names are global, so this is how a collision between backends is
    resolved. The control sockets are renamed rather than reopened — a unix
    socket listener is bound to its inode, so the running master survives and no
    reauthentication is needed.

    What rides the login while it is renamed keeps the channels it has, but
    it reaches the login by its old name: its next connection fails, and an
    attach that reconnects opens a new login under the old name. So a login
    other commands are using is renamed only with --force.
    """
    parser = argparse.ArgumentParser(prog="cluster rename", add_help=False)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--force", action="store_true",
                        help="rename even while other commands use the login")
    opts = parser.parse_args(args)
    if len(opts.names) != 2:
        ui.die("need an old and a new name", "usage: cluster rename OLD NEW [--force]")
    old_raw, new = opts.names
    tmuxlayer.require_name(new, "login name")
    backends.refuse_backend_name(new)
    old = ctx.login(old_raw)                      # binds ctx to the owning backend
    # ctx is now bound to the backend that owns `old`; asking the registry again
    # would re-resolve the name and, during a collision, name the wrong backend.
    owner = ctx.backend.name
    if old not in registry.logins_of(owner):
        ui.die(f"no login named '{old}'")
    taken = registry.backends_claiming(new)
    if taken:
        ui.die(f"'{new}' is already a login on {', '.join(taken)}",
               "login names are global: one name is one connection")
    if old == new:
        ui.say("nothing to do")
        return 0
    # The same checks a new login's name gets: it names sockets, and a name
    # equal to another but for case shares its files on a case-insensitive disk.
    ctx.logins.check_new_login(new)

    active = ctx.logins.is_active(old)
    # The mount is taken down and put back by the rename itself.
    riders = [(pid, kind) for pid, kind in ctx.logins.channel_clients(old)
              if kind != "sshfs mount"] if active else []
    if riders:
        named = ", ".join(f"pid {pid} ({kind})" for pid, kind in riders)
        consequence = (f"they reach it as '{old}', so their next connection "
                       "fails, and an attach that reconnects opens a new login "
                       f"called '{old}'")
        if not opts.force:
            ui.die(f"login '{old}' is in use by {len(riders)} command(s): {named}",
                   consequence,
                   "finish or close them first, or rename anyway with --force")
        ui.warn(f"renaming '{old}' while {named} still use(s) it")
        ui.note(consequence)
    was_mounted = ctx.mounts.is_mounted(old)
    watching = ctx.mounts.watcher_running(old)

    # Retag the far side while the old name is still the one recorded there.
    if active:
        if not ctx.tmux.retag_owner(old, old, new):
            ui.warn("could not retag remote sessions; continuing with the local rename")
        else:
            ui.info(f"remote sessions and breadcrumbs now owned by '{new}'")

    ctx.mounts.stop_watcher(old, quiet=True)
    if was_mounted:
        ctx.mounts.unmount(old, quiet=True)

    moved = ctx.state.rename(old, new)
    ui.info(f"moved {moved} local file(s)/socket(s) to '{new}'")

    if active and not ctx.logins.is_active(new):
        ui.warn(f"the master did not survive the rename; reconnect with: "
                f"cluster login {new}")
    if was_mounted:
        try:
            ctx.mounts.mount(new, quiet=True)
            ui.info(f"remounted at {ctx.mounts.mountpoint(new)}")
        except ui.Die as exc:
            ui.warn(f"could not remount as '{new}': {exc}")
    if watching:
        ctx.mounts.start_watcher(new)
    ui.info(f"login '{old}' is now '{new}' on {owner}")
    return 0


@command("fixterm", needs_context=False,
         help="Repair a local terminal left in a broken drawing mode")
def cmd_fixterm(invocation, args):
    """Clear terminal state that a crashed full-screen program left behind.

    This is the repair every managed session already performs on its way in and
    out, available on its own for when the program that wedged the terminal was
    not one of ours — a local editor or agent killed before it could restore the
    modes it set. It touches no cluster, no connection and no stored state, so
    it needs neither a backend nor a login.

    It covers the symptoms that do not look like leftover escape state: text
    piling onto the last column instead of wrapping, typing that appears to
    overwrite a ghost character, output confined to a band of the screen, an
    invisible cursor, everything stuck in one colour — and the loud ones too,
    scrolling and clicks arriving in the shell as literal "0;48;27M".

    The scrollback is deliberately left alone. `reset` and `tput reset` fix the
    same modes by clearing the screen, which throws away the output you were
    probably trying to read when you noticed the problem.
    """
    parser = argparse.ArgumentParser(prog="cluster fixterm", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="show this help")
    parser.parse_args(args)
    flags = plat.sane_tty()
    if not plat.restore_tty():
        ui.die("no controlling terminal to repair",
               "run this in the terminal that is misbehaving, not through a pipe")
    ui.info("terminal drawing modes restored"
            + ("" if flags else "; tty flags left alone (no stty)"))
    reported = plat.terminal_report_size()
    if not reported:
        ui.note("terminal did not report its size; left the window size alone")
    else:
        fixed = plat.resync_terminal_size(reported=reported)
        ui.info(f"window size corrected to {fixed[0]}x{fixed[1]}" if fixed
                else f"window size already correct at {reported[0]}x{reported[1]}")
    return 0


@command("clean", help="Reap tmux sessions that provably belong to no live login")
def cmd_clean(ctx, args):
    """Sweep orphaned sessions.

    The bar for killing is deliberately high: only a session that carries *this
    tool's* ownership tag naming a login that is gone is provably junk.
    Anything else is reported and left alone, because from here a stranger's live
    work is indistinguishable from litter, and getting it wrong destroys real
    work. Sessions tagged by another tool, and sessions with no tag at all, need
    an explicit flag.
    """
    parser = argparse.ArgumentParser(prog="cluster clean", add_help=False)
    parser.add_argument("--all", action="store_true",
                        help="sweep every node in the pool, not just known ones")
    parser.add_argument("--all-backends", action="store_true",
                        help="sweep each backend in turn (one at a time)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be reaped, reap nothing")
    parser.add_argument("--include-untagged", action="store_true",
                        help="also reap sessions carrying no ownership tag")
    parser.add_argument("--force", action="store_true",
                        help="reap even foreign-tagged sessions, and sweep while "
                             "a known login is disconnected (untagged sessions "
                             "still need --include-untagged)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="answer the stale-record prompt without asking")
    opts = parser.parse_args(args)

    # Sweeping is per backend by default: it visits nodes, and on FASRC every
    # node visited costs a TOTP window. --all-backends does them in sequence
    # rather than pretending it is one fleet-wide operation.
    if opts.all_backends:
        rc = 0
        for backend_name in ctx.every_backend():
            ui.say(ui.bold(f"\n== {backend_name} =="))
            rc |= clean_backend(ctx.sibling(backend_name), opts)
        return rc
    return clean_backend(ctx, opts)


def clean_backend(ctx, opts):
    """One backend's sweep; *opts* carries `clean`'s parsed flags."""
    ctx.by_hand()
    known = set(ctx.state.known_logins())
    inactive = [n for n in known
                if ctx.state.pin_read(n) and not ctx.logins.is_active(n)]
    if inactive and not opts.force:
        ui.die(
            f"login(s) {', '.join(sorted(inactive))} are pinned but not connected",
            "their sessions cannot be told apart from orphans right now",
            "reconnect them, or pass --force to sweep anyway "
            "(which also reaps sessions tagged by other tools)",
        )

    # Every session this machine knows about, on any node, is spared.
    protected = set()
    stray = set()
    occupied = straylib.occupied_nodes(ctx)
    catalogued_crumbs = {}
    live = {}
    for name in ctx.logins.active_names():
        node = ctx.backend.short(ctx.logins.node_of(name))
        complete, sessions, crumbs = ctx.tmux.sessions_and_crumbs_checked(name)
        if not complete:
            ui.die(f"cannot catalogue tmux sessions through login '{name}'",
                   "clean did not touch any sessions or breadcrumbs")
        for row in sessions:
            if row.owner == name and not row.foreign:
                protected.add((node, row.name))
        if node:
            live[node] = {row.name for row in sessions}
        catalogued_crumbs.update(crumbs)

    # The breadcrumb catalogue lives in the shared home, so it is the same for
    # every login and is classified once, after all of them have been read —
    # `live` has to be complete before any crumb can be called lost.
    lost = set()
    for (crumb_node, session), owner in catalogued_crumbs.items():
        if owner not in known:
            continue
        if crumb_node in occupied:
            seen = live.get(crumb_node)
            if seen is not None and session not in seen:
                # A record on a node we just listed, naming a session that
                # node is not running: the work is already gone, and the
                # record is all that is left saying it existed. Reaping it is
                # the only thing here that destroys no work, because there is
                # none left to destroy.
                lost.add((crumb_node, session, owner))
            else:
                protected.add((crumb_node, session))
        else:
            # The owner login exists but does not live on that node, so
            # nothing is looking at this session. Protecting it like real work
            # would make it unreapable forever, and `clean` only reaps what it
            # can prove is junk, so it is not killed here: it is named as a
            # stray.
            stray.add((crumb_node, session))

    if lost:
        ui.warn(f"{len(lost)} recorded session(s) are not running on the node "
                f"that holds their record; the processes in them are gone")
        for node, session, owner in sorted(lost):
            ui.say(f"  {node}:{session} (recorded to '{owner}')")
        for owner, node in sorted({(o, n) for n, _s, o in lost}):
            ui.note(f"rebuild windows and cwds: cluster restore-layout "
                    f"{owner} {node}")
        executor = next(iter(ctx.logins.active_names()), "")
        if opts.dry_run or not executor:
            ui.note("the records would be dropped; this is a dry run"
                    if opts.dry_run else "no login open to drop the records")
        elif not (opts.yes or plat.terminal_attached()):
            # Skipped, not fatal. Dropping stale records is a side concern of
            # a sweep, and an unattended `clean` that aborted over a prompt it
            # could not ask would stop doing the job it was run for.
            ui.note(f"{len(lost)} stale record(s) kept; nothing here can "
                    f"answer a prompt — pass -y to drop them")
        elif ui.confirm(f"drop {len(lost)} stale record(s)?", opts.yes,
                        "pass -y to drop them, or leave them and restore first"):
            by_node = {}
            for node, session, _owner in sorted(lost):
                by_node.setdefault(node, []).append(session)
            dropped = sum(
                len(sessions) for node, sessions in by_node.items()
                if ctx.tmux.crumbs_remove(executor, sessions,
                                          node=ctx.backend.fqdn(node)))
            ui.info(f"dropped {dropped}/{len(lost)} stale record(s)")
        else:
            # Kept deliberately: the record is the only thing left naming the
            # session, and `restore-layout` reads it. Dropping it before the
            # restore is how the name goes for good.
            ui.note("records kept; they are what restore-layout reads")

    targets = sweep_targets(ctx, everything=opts.all, crumbs=catalogued_crumbs)
    if not targets:
        ui.say("nothing to sweep")
        return 0
    ui.info(f"sweeping {len(targets)} node(s): "
            f"{', '.join(ctx.backend.short(n) for n in targets)}")

    killed, failed, kept = [], [], {"protected": [], "foreign": [], "untagged": [],
                                    "stray": []}
    unreachable = []
    # A credential refused on one node is refused on every other, and each
    # try counts towards locking the account: after the first, the nodes a
    # login is on are still swept over it, and the rest are left.
    refused, unvisited = "", []
    # Whatever this sweep kills or spares, what a node should do for us may
    # have changed, so each node visited is settled — in the command that
    # lists it, and again in the one that kills, never in a round trip (on
    # FASRC, an authentication) of its own. A dry run changes nothing,
    # including this.
    settle = not opts.dry_run
    for node in targets:
        short = ctx.backend.short(node)
        runner = _login_on(ctx, node)
        if refused and not runner:
            unvisited.append(short)
            continue
        complete, rows = (
            ctx.tmux.list_sessions_checked(runner, settle=settle) if runner
            else ctx.tmux.list_sessions_direct_checked(node, settle=settle))
        if not complete:
            unreachable.append(short)
            if not runner and is_rejection(ctx.logins.last_failure):
                refused = ctx.logins.last_failure
            continue

        abandoned_here = set(ctx.state.abandoned_on(short))
        doomed = _reapable(ctx, opts, short, rows, abandoned_here, protected,
                           stray, known, kept)
        if opts.dry_run:
            killed += list(doomed.values())
            continue
        if not doomed:
            continue
        # One command kills every session this node is losing and confirms it.
        if runner:
            gone, lost_kills = ctx.tmux.kill_sessions(runner, list(doomed),
                                                      settle=True)
        else:
            gone, lost_kills = ctx.tmux.kill_sessions_direct(node, list(doomed),
                                                             settle=True)
            if lost_kills and is_rejection(ctx.logins.last_failure):
                refused = ctx.logins.last_failure
            # Drop the records as well. crumb_sync only reconciles a login's
            # *own* node, so a crumb for a node we merely visited would linger
            # and keep sending later sweeps back to a dead node. The home is
            # shared, so any live login can remove them.
            executor = next(iter(ctx.logins.active_names()), "")
            if executor and gone:
                ctx.tmux.crumbs_remove(executor, gone, node=node)
        for session in gone:
            killed.append(doomed[session])
            if session in abandoned_here:
                ctx.state.abandon_forget(short, session)
        failed += [f"{short}:{session}" for session in lost_kills]

    if kept["protected"]:
        ui.say(f"kept (yours): {', '.join(sorted(kept['protected']))}")
    if kept["stray"]:
        ui.say(ui.yellow("kept (stray — recorded to a login that lives "
                         "elsewhere): " + ", ".join(sorted(kept["stray"]))))
        ui.note("decide what happens to them: cluster strays")
    if kept["foreign"]:
        ui.say(ui.yellow("kept (owned by another tool): "
                         + ", ".join(sorted(kept['foreign']))))
    if kept["untagged"]:
        ui.say(ui.yellow("kept (no ownership tag, cannot prove they are orphans): "
                         + ", ".join(sorted(kept['untagged']))))
        ui.note("pass --include-untagged to reap these too")
    if unreachable:
        ui.warn("could not catalogue node(s), left untouched: "
                + ", ".join(sorted(set(unreachable))))
    if unvisited:
        ui.warn(f"the credential was refused ({refused}); not tried on the other "
                f"node(s), which would refuse it too: {', '.join(unvisited)}")
    if failed:
        ui.warn("could not confirm dead, records left in place: "
                + ", ".join(sorted(failed)))
    if killed:
        ui.say(f"{'would kill' if opts.dry_run else 'killed'}: "
               f"{', '.join(sorted(killed))}")
    else:
        ui.say("no reapable sessions found")
    for name in ctx.logins.active_names():
        ctx.tmux.crumb_sync(name)
    return 1 if failed or unreachable or unvisited else 0


def sweep_targets(ctx, everything=False, crumbs=None):
    """Nodes worth visiting: everywhere we have evidence of our own sessions."""
    if everything:
        candidates = list(ctx.backend.pool_nodes())
    else:
        candidates = []
        crumbs = crumbs or {}
        for name in ctx.logins.active_names():
            node = ctx.logins.node_of(name)
            if node:
                candidates.append(node)
        for crumb_node, _session in crumbs:
            candidates.append(ctx.backend.fqdn(crumb_node))
        candidates += ctx.state.ledger_nodes()
        candidates += [ctx.backend.fqdn(n) for (n, _s, _f) in ctx.state.abandoned()]
    seen, ordered = set(), []
    for node in candidates:
        short = ctx.backend.short(node)
        if short not in seen:
            seen.add(short)
            ordered.append(node)
    return ordered


def _reapable(ctx, opts, short, rows, abandoned_here, protected, stray, known,
              kept):
    """``{session: label}`` of what `clean` reaps on one node.

    Everything spared is added to *kept* under the reason it was spared.
    """
    doomed = {}
    for row in rows:
        label = f"{short}:{row.name}"
        # A recorded abandonment outranks every protection below: this
        # machine wrote down that the session's login moved off this node,
        # so its still-present ownership tag is stale by construction.
        if row.name in abandoned_here:
            doomed[row.name] = f"{label} [abandoned]"
        elif (short, row.name) in stray:
            kept["stray"].append(label)
        elif (short, row.name) in protected:
            kept["protected"].append(label)
        elif row.foreign and not opts.force:
            kept["foreign"].append(f"{label} [{row.foreign}]")
        # Only its own flag reaps these: a session nobody tagged may be
        # somebody's hand-made work, and --force is for other questions.
        elif not row.tagged and not opts.include_untagged:
            kept["untagged"].append(label)
        elif row.tagged and row.owner in known and ctx.logins.is_active(row.owner):
            kept["protected"].append(label)
        else:
            doomed[row.name] = label
    return doomed


def _login_on(ctx, node):
    for name in ctx.logins.active_names():
        if ctx.backend.short(ctx.logins.node_of(name)) == ctx.backend.short(node):
            return name
    return None
