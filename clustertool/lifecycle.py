"""Session disposition when a managed login leaves its node.

Closing, repinning and unpinning all face the same data-loss boundary: a tmux
session can outlive its SSH connection, but once its login points at another
node the session needs an explicit abandoned record or it becomes hidden.
Keeping that rule in one module prevents the three commands from drifting.
"""

from __future__ import annotations

from . import ui


def retired_owner(login, node_short):
    """An ownership tag no valid login can hold, so a sweep sees an orphan."""
    return f"{login}@{node_short}"


def mark_abandoned(ctx, name, short, sessions):
    """Record left sessions locally and retag shared-home breadcrumbs."""
    unique = sorted(set(sessions))
    for session in unique:
        ctx.state.abandon_record(short, session, name)
    tag = retired_owner(name, short)
    executor = next(iter(ctx.logins.active_names()), "")
    if executor:
        ctx.tmux.crumb_retag_node(executor, short, name, tag)
    return unique


def known_owned_on_checked(ctx, name, node):
    """``(catalogue_complete, owned_sessions)`` for *name* on *node*.

    "Complete" is deliberately separate from an empty list.  Session
    disposition changes pins and ownership records, so an SSH timeout must not
    be allowed to masquerade as a safely empty tmux server.
    """
    short = ctx.backend.short(node)
    if ctx.logins.is_active(name):
        return ctx.tmux.owned_sessions_checked(name)
    # Breadcrumbs are on shared home. Any active login on this backend can read
    # them even when the old node itself is gone.
    for other in ctx.logins.active_names():
        complete, crumbs = ctx.tmux.crumbs_checked(other)
        if not complete:
            continue
        return True, sorted(
            session for (crumb_node, session), owner in crumbs.items()
            if crumb_node == short and owner == name
        )
    return False, []


def release_sessions(ctx, name, old, opts, verb):
    """Tear down or record sessions a repin/unpin would leave behind."""
    short = ctx.backend.short(old)
    complete, known = known_owned_on_checked(ctx, name, old)
    if not complete:
        ui.die(f"cannot catalogue sessions on {short}",
               "the pin was not changed, so possible work remains findable",
               f"reconnect '{name}' and try again")

    # Without this login's live master we can read shared breadcrumbs, but we
    # cannot safely issue node-local tmux commands.  Preserve what is known and
    # let the caller complete the requested move with an explicit warning.
    if not ctx.logins.is_active(name):
        mark_abandoned(ctx, name, short, known)
        ui.warn(f"login '{name}' is disconnected, so sessions on {short} "
                "could not be closed")
        if known:
            ui.note(f"left running (recorded): {', '.join(known)}")
        ui.note("reap them once it is back: cluster clean")
        return 1

    stranded = known
    if not stranded:
        return 0

    if opts.abandon:
        mark_abandoned(ctx, name, short, stranded)
        ui.warn(f"left {len(stranded)} session(s) running on {short}: "
                f"{', '.join(stranded)}")
        ui.note("they are recorded as abandoned; reap them with: cluster clean")
        return 0

    ui.warn(f"{verb} will close {len(stranded)} session(s) on {short}: "
            f"{', '.join(stranded)}")
    if opts.migrate:
        ui.note("their names, windows and cwds are rebuilt on the new node, "
                "but running commands are not restarted")
    else:
        ui.note("anything running in them is lost; --abandon leaves them instead")
    if not ui.confirm(f"close {len(stranded)} session(s) on {short}?", opts.yes,
                      "pass -y to close them unattended"):
        ui.die("aborted; the pin is unchanged")

    if opts.migrate and not ctx.tmux.layout_save(name):
        ui.die(f"could not snapshot the layout on {short}", "nothing was moved")

    killed, failed = ctx.tmux.kill_sessions(name, stranded)
    for session in killed:
        ctx.state.abandon_forget(short, session)
        ui.info(f"closed session '{session}' on {short}")
    if failed:
        mark_abandoned(ctx, name, short, failed)
        ui.warn(f"could not confirm {', '.join(failed)} died on {short}; "
                "recorded as abandoned")
        return 1
    return 0


def close_logins(targets, keep_tmux=False, abandon_tmux=False):
    """Close ``(Context, login_name)`` pairs with fail-safe disposition.

    Argument parsing and backend scoping belong to the CLI; the data-loss
    boundary belongs here beside repin/unpin.  This also leaves one place to
    enforce the rule that a failed catalogue read retains the pin.
    """
    failures = 0
    for ctx, name in targets:
        active = ctx.logins.is_active(name)
        old_node = ((ctx.logins.node_of(name) or ctx.logins.live_node(name))
                    if active else ctx.state.pin_read(name))
        keep_pin = keep_tmux
        leave_running = keep_tmux or abandon_tmux

        if abandon_tmux:
            complete, sessions = (
                known_owned_on_checked(ctx, name, old_node)
                if old_node else (True, [])
            )
            if old_node and complete:
                short = ctx.backend.short(old_node)
                recorded = mark_abandoned(ctx, name, short, sessions)
                if recorded:
                    ui.warn(f"left {len(recorded)} session(s) running on {short}: "
                            f"{', '.join(recorded)}")
                    ui.note("recorded as abandoned; reap later with: cluster clean")
                else:
                    ui.info(f"no owned tmux sessions to record on {short}")
                keep_pin = False
            elif old_node:
                ui.warn(f"cannot catalogue sessions on {ctx.backend.short(old_node)}; "
                        "keeping the pin rather than hiding possible work")
                keep_pin = True
                failures += 1
            else:
                keep_pin = False
        elif not keep_tmux:
            if active:
                complete, killed, failed = ctx.tmux.kill_owned_checked(name)
                if not complete:
                    ui.warn(f"cannot catalogue tmux sessions for '{name}'; "
                            "keeping the pin rather than hiding possible work")
                    keep_pin = True
                    failures += 1
                else:
                    for session in killed:
                        ui.info(f"killed session '{session}'")
                if complete and failed:
                    ui.warn(f"could not confirm {', '.join(failed)} died on "
                            f"{ctx.backend.short(ctx.logins.node_of(name))}; "
                            "keeping the pin")
                    keep_pin = True
                    failures += 1
            elif ctx.state.pin_read(name):
                ui.warn(f"login '{name}' is not connected; keeping its pin "
                        f"({ctx.backend.short(ctx.state.pin_read(name))}) and tmux")
                keep_pin = True

        ctx.mounts.stop_watcher(name, quiet=True)
        ctx.mounts.unmount(name, quiet=True)
        ctx.logins.close(name, keep_tmux=leave_running, keep_pin=keep_pin)
        if not keep_pin:
            ctx.state.drop_meta(name)
    return 1 if failures else 0


def _restore_mount_setup(ctx, name, was_mounted, was_watched):
    """Put a login's mount and watcher back after it changed node."""
    if was_mounted and not ctx.mounts.try_mount(name):
        ui.warn(f"could not remount '{name}'; mount it by hand: cluster mount {name}")
    if was_watched:
        ctx.mounts.start_watcher(name)


def refresh_login(ctx, name, keep_tmux=False):
    """Move a login while preserving proof of anything left on its old node."""
    old_node = ctx.state.pin_read(name)
    active = ctx.logins.is_active(name)
    left_running = []
    preserve_old_evidence = False
    disposition_rc = 0
    if active:
        complete, owned = ctx.tmux.owned_sessions_checked(name)
        if not complete:
            ui.die(f"cannot catalogue tmux sessions for '{name}'",
                   "refresh did not close the connection or change its pin")
        if keep_tmux:
            left_running = mark_abandoned(
                ctx, name, ctx.backend.short(old_node), owned) if old_node else []
            if left_running:
                ui.warn(f"left {len(left_running)} session(s) running on "
                        f"{ctx.backend.short(old_node)}: {', '.join(left_running)}")
                ui.note("recorded as abandoned; reap later with: cluster clean")
        else:
            killed, failed = ctx.tmux.kill_sessions(name, owned)
            for session in killed:
                ui.info(f"killed session '{session}'")
            if failed:
                left_running = mark_abandoned(
                    ctx, name, ctx.backend.short(old_node), failed) if old_node else []
                ui.warn(f"could not confirm {', '.join(failed)} died; "
                        "recorded them as abandoned before moving")
                disposition_rc = 1
        if old_node and not left_running:
            ctx.tmux.layout_forget(name, old_node)
    elif old_node:
        if keep_tmux:
            complete, owned = known_owned_on_checked(ctx, name, old_node)
            if complete:
                left_running = mark_abandoned(
                    ctx, name, ctx.backend.short(old_node), owned)
                if left_running:
                    ui.warn(f"left running (recorded): {', '.join(left_running)}")
            else:
                preserve_old_evidence = True
                disposition_rc = 1
                ui.warn(f"could not catalogue tmux on {ctx.backend.short(old_node)}; "
                        "keeping its ledger/breadcrumb evidence for rescue")
        else:
            complete, owned = ctx.tmux.owned_sessions_direct_checked(old_node, name)
            if complete:
                # The login is not connected, so the close below has no
                # channel to settle the old node over: the kill settles it,
                # in the connection it pays for anyway.
                killed, failed = ctx.tmux.kill_sessions_direct(
                    old_node, owned, settle=True)
                for session in killed:
                    ui.info(f"killed session '{session}' on "
                            f"{ctx.backend.short(old_node)}")
                if failed:
                    left_running = mark_abandoned(
                        ctx, name, ctx.backend.short(old_node), failed)
                    ui.warn(f"could not confirm {', '.join(failed)} died; "
                            "recorded them as abandoned before moving")
                    disposition_rc = 1
            else:
                preserve_old_evidence = True
                disposition_rc = 1
                ui.warn(f"could not catalogue tmux directly on "
                        f"{ctx.backend.short(old_node)}; moving the login but "
                        "keeping the old node's evidence")
                ui.note(f"inspect it later with: cluster rescue "
                        f"{ctx.backend.short(old_node)}")

    was_mounted = ctx.mounts.is_mounted(name)
    was_watched = ctx.mounts.watcher_running(name)
    ctx.mounts.stop_watcher(name, quiet=True)
    ctx.mounts.unmount(name, quiet=True)
    ctx.logins.close(name, keep_pin=False, quiet=True)

    avoid = [old_node] if old_node else []
    if old_node and not (left_running or preserve_old_evidence):
        ctx.state.ledger_remove(old_node)

    tries = ctx.settings.int("REFRESH_TRIES")
    for attempt in range(1, tries + 1):
        candidates = ctx.backend.node_candidates_for(name, avoid=avoid)
        try:
            ctx.logins.ensure(name, preferred=candidates)
        except SystemExit:
            # The login is down now, and its watcher is what brings it back
            # (and remounts it) once the cluster lets it; left stopped, nothing
            # would.
            if was_watched:
                ctx.mounts.start_watcher(name)
            raise
        landed = ctx.logins.node_of(name)
        if not old_node or ctx.backend.short(landed) != ctx.backend.short(old_node):
            ui.info(f"login '{name}' is now on {ctx.backend.short(landed)}")
            _restore_mount_setup(ctx, name, was_mounted, was_watched)
            return disposition_rc
        ui.warn(f"landed on {ctx.backend.short(landed)} again "
                f"({attempt}/{tries}); retrying")
        ctx.logins.close(name, keep_pin=False, quiet=True)
    _restore_mount_setup(ctx, name, was_mounted, was_watched)
    ui.die(f"could not move '{name}' off {ctx.backend.short(old_node)}")
