"""SSHFS mount lifecycle and watcher commands."""

from __future__ import annotations

import argparse
import time

from .. import config, mounts as mountstate, platform as plat, ui
from ..command import command


def auto_mount(ctx, login, skip=False):
    """Mount the backend's home for *login* after connecting, if configured.

    Declines only to *create* a duplicate. A login that already has a mount of
    its own still goes through try_mount, because that is what notices a
    wedged one and heals it. A machine without sshfs has mounts off, which
    is said once here rather than failed on every connection.
    """
    if skip or not ctx.settings.flag("AUTO_MOUNT"):
        return False
    missing = plat.mount_tools_missing()
    if missing:
        ui.note(f"{' and '.join(missing)} is not installed, so mounts are off; "
                "install it, or turn mounts off: cluster config set AUTO_MOUNT 0")
        return False
    if not ctx.mounts.is_mounted(login):
        holder = ctx.mounts.mounted_elsewhere(login)
        if holder:
            ui.note(
                f"{ctx.backend.label} is already mounted for login "
                f"'{holder}' at "
                f"{mountstate.short_path(ctx.mounts.mountpoint(holder))}; "
                f"not mounting it a second time for '{login}'"
            )
            return False
    return ctx.mounts.try_mount(login)


@command("mount", "m", help="Mount the cluster over a login's connection")
def cmd_mount(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster mount", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("remote", nargs="?")
    parser.add_argument("mountpoint", nargs="?")
    opts = parser.parse_args(args)
    name = ctx.login(opts.name)
    holder = ctx.mounts.mounted_elsewhere(name)
    if holder and not ctx.mounts.is_mounted(name):
        # An explicit verb is honoured — asking for a mount is the whole point of
        # typing this — but say what it costs, because a second copy of one
        # filesystem spends a channel and the channel budget runs out first.
        ui.warn(f"{ctx.backend.label} is already mounted for login '{holder}' at "
                f"{mountstate.short_path(ctx.mounts.mountpoint(holder))}; "
                f"a second mount spends one of '{name}'s channels")
    ctx.logins.ensure(name)
    ctx.mounts.mount(name, remote=opts.remote, mountpoint=opts.mountpoint)
    return 0


@command("mounts", help="List managed SSHFS mounts")
def cmd_mounts(ctx, args):
    rows = []
    for name in ctx.state.known_logins():
        mp = ctx.mounts.mountpoint(name)
        if not plat.mount_table_has(mp) and not ctx.state.mountnode_read(name):
            continue
        via_node = ctx.state.mountnode_read(name)
        via = f"{ctx.backend.short(via_node)}*" if via_node else "login"
        status, detail = ctx.mounts.probe(name)
        rows.append([name, str(mp).replace(str(config.HOME), "~"), via,
                     mountstate.STATUS_LABEL[status],
                     "" if status == mountstate.ANSWERED else detail])
    if not rows:
        ui.say("no managed mounts")
        return 0
    ui.table(rows, ["LOGIN", "MOUNTPOINT", "VIA", "STATE", "DETAIL"])
    return 0


@command("umount", "unmount", help="Unmount a managed SSHFS mount")
def cmd_umount(ctx, args):
    name = ctx.login(args[0] if args else None)
    return 0 if ctx.mounts.unmount(name) else 1


@command("repair", help="Run the mount repair ladder once, by hand")
def cmd_repair(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster repair", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--attempt", type=int, default=1,
                        help="which rung to start at (higher escalates sooner)")
    opts = parser.parse_args(args)
    name = ctx.login(opts.name)
    status, detail = ctx.mounts.probe(name)
    ui.info(f"mount state: {mountstate.STATUS_LABEL[status]} — {detail}")
    if status in mountstate.STATUS_OK:
        # A busy mount is working, just slowly; repairing it would abandon the
        # in-flight requests that prove it is alive.
        return 0
    if ctx.mounts.repair(name, attempt=opts.attempt, quiet=False, log=ui.info):
        ui.info("mount repaired")
        return 0
    ui.warn("repair did not succeed")
    return 1


@command("watch", help="Start the login/mount watcher")
def cmd_watch(ctx, args):
    name = ctx.login(args[0] if args else None)
    ctx.logins.ensure(name)
    return 0 if ctx.mounts.start_watcher(name) else 1


@command("unwatch", help="Stop the login/mount watcher")
def cmd_unwatch(ctx, args):
    name = ctx.login(args[0] if args else None)
    ctx.mounts.stop_watcher(name)
    return 0


@command("monitor", help="Run the watcher loop in the foreground (used by watch)")
def cmd_monitor(ctx, args):
    from ..watcher import Watcher

    name = ctx.login(args[0] if args else None)
    return Watcher(ctx.logins, ctx.mounts, ctx.tmux, name).run()


def _ensure_shutdown_hook(ctx):
    """Re-arm the reboot hook if this boot finds it missing or broken.

    `boot` is the one thing guaranteed to run after every restart, which makes
    it the place to notice that the hook protecting the *next* restart is not
    there: a working tree that moved, a machine that was rebuilt, a unit that
    was never installed. Checking costs nothing when it is armed, which is the
    normal case.

    Non-interactive by necessity — nothing can type a sudo password at boot —
    so this lands on the `--user` unit unless sudo is already passwordless,
    and says which it got rather than failing the boot over it.
    """
    from .. import linger
    from ..diagnostics import has_systemd, hook_report, install_hook

    if not has_systemd():
        return
    if not linger.required(ctx.logins) or hook_report()[0]:
        return
    kind, detail = install_hook(interactive=False)
    if kind:
        ui.info(f"shutdown hook re-armed ({kind} unit)")
    else:
        ui.warn("no shutdown hook: tmux on the login nodes may not survive "
                f"the next reboot of this machine ({detail})")
        ui.note("install it with: cluster linger --install-hook")


def _leave_to_watcher(ctx, name, why, then="the background watcher will keep retrying"):
    """Start *name*'s watcher to finish what boot could not; boot's status.

    The watcher reconnects and remounts on its own, backing off while it
    cannot, so a boot that ran out of patience still ends with the login on
    its way back rather than forgotten until someone notices. *then* says
    what it does next.
    """
    if ctx.mounts.start_watcher(name):
        ui.warn(f"{why}; {then}")
    else:
        ui.warn(f"{why}, and its watcher could not be started")
    return 1


@command("boot", help="Restore login, mount and watcher unattended")
def cmd_boot(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster boot", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--wait", type=int, default=None,
                        help="seconds to wait for the network (default: config BOOT_WAIT)")
    parser.add_argument("--tries", type=int, default=None,
                        help="connection attempts (default: config BOOT_TRIES)")
    opts = parser.parse_args(args)
    name = ctx.login(opts.name)
    wait = opts.wait if opts.wait is not None else ctx.settings.int("BOOT_WAIT")
    tries = opts.tries if opts.tries is not None else ctx.settings.int("BOOT_TRIES")
    if wait < 0 or tries < 1:
        ui.die("--wait must be non-negative and --tries must be at least 1")

    # Poll for the network rather than sleeping a fixed time: at boot it is
    # usually up within a second or two, and there is no reason to wait longer.
    # A network that is not up by then is left to the watcher, as a login that
    # will not open is: it goes on trying, with backoff, until it comes.
    deadline = time.monotonic() + wait
    announced = False
    # What must resolve first: the pool address, or the first host a jump
    # goes through. None: a proxy command, which only connecting can test.
    reach = ctx.backend.reach_host()
    while reach and not plat.dns_ok(reach[0]):
        if time.monotonic() >= deadline:
            return _leave_to_watcher(
                ctx, name, f"no route to {reach[0]} after {wait}s")
        if not announced:
            ui.info(f"waiting for the network (up to {wait}s)")
            announced = True
        time.sleep(ctx.settings.int("BOOT_NETWORK_POLL_INTERVAL"))

    from ..auth import failure_text, refused_by
    from ..sshmux import connection_failures

    restored = False
    for attempt in range(1, tries + 1):
        ctx.logins.last_failure = ""
        try:
            ctx.logins.ensure(name)
            restored = True
            break
        except connection_failures() as exc:
            reason = failure_text(exc, ctx.logins.last_failure)
            if refused_by(exc, ctx.logins.last_failure):
                # Trying again would only be refused again, and spend a TOTP
                # window: the refusal is on record, and the watcher confirms
                # it once, as every unattended process does (state.Refusals).
                return _leave_to_watcher(
                    ctx, name, f"could not restore login '{name}': {reason}",
                    "the background watcher tries a refused credential once "
                    "more at most, and then waits for it to change")
            if attempt == tries:
                break
            # Backing off matters here: on FASRC a retry needs a fresh TOTP
            # window anyway, and totp_pace waits for exactly that.
            delay = min(ctx.settings.int("BOOT_RETRY_DELAY_MAX"),
                        ctx.settings.int("BOOT_RETRY_DELAY") * attempt)
            ui.warn(f"login attempt {attempt}/{tries} failed; retrying in {delay}s")
            time.sleep(delay)
    if not restored:
        return _leave_to_watcher(
            ctx, name, f"could not restore login '{name}' after {tries} attempts")
    auto_mount(ctx, name)
    if not ctx.mounts.start_watcher(name):
        return 1
    _ensure_shutdown_hook(ctx)
    ui.info(f"login '{name}' restored on {ctx.backend.short(ctx.logins.node_of(name))}")
    return 0
