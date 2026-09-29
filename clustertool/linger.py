"""Keeping a login node's processes alive across a disconnect.

There are two ways a login node kills your work, they are not the same, and
defending against one of them alone does nothing. On a FASRC node:

    /user.slice/user-12345.slice/session-809.scope     <- one per connection
    /user.slice/user-12345.slice/user@12345.service    <- one per node

1. **The connection ends.** logind runs ``KillUserProcesses=yes``, so it
   terminates that connection's *session scope* and everything inside it.
   Linger has no bearing on this whatsoever.
2. **The last connection ends.** ``user@$UID.service`` is stopped, taking
   what it holds. *This* is what linger (``loginctl enable-linger``) prevents.

A ``tmux new-session`` sent over SSH runs in the connection's shell and so
lands in the session scope, where linger cannot help it. :data:`SCOPE` is the
other half: starting the server under ``systemd-run --scope --user`` puts it
in ``user@$UID.service`` instead, out of reach of the first death and squarely
behind linger for the second. FASRC ships both halves itself in
/etc/profile.d/linger.sh — ``loginctl enable-linger`` and an ``alias tmux=``
— but an alias reaches interactive shells only, and nothing this tool sends is
interactive.

Measured on holylogin07, 2026-09-17, linger enabled throughout, one clean
`close` and reconnect between each:

    tmux new-session -d -s a            -> session-809.scope        -> GONE
    systemd-run --scope --user tmux ... -> user@12345.service/run-  -> SURVIVED

Dropping every *client* connection for a few minutes proves nothing either
way: FASRC's sshd sets no ``ClientAliveInterval``, so the node does not end
those sessions at all, and a session found alive afterwards met neither death.
A clean close is what a reboot actually does, and that is what the table above
measures.

Three further facts shape the linger half. On holylogin06, 2026-09-17:

* **Only one instant matters** — the one when the last session ends. A client
  reboot ends every session at once, so linger has to be in place *already*;
  an assertion a second late is an assertion about nothing.
* **Linger is node-local and not durable.** ``/var/lib/systemd/linger`` does not
  survive a node reboot, and something on the node clears it even while a
  connection is up: a file created at 14:32:49 was gone by 14:42:32 with the
  master alive throughout. Enabling it once, at setup, is worth nothing a day
  later.
* **Nothing removes it when a session closes.** An extra session was opened and
  closed against a node whose file was being watched, and the file stayed. So
  re-asserting from here is not a race the node will simply undo; between the
  sweeps the state holds.

Which is why linger is asserted continuously rather than configured once: on
every new connection, on every session created (in the same round trip), on
``close``, and on a timer in the watcher.

That leaves the gap between two assertions, and a reboot landing in it after a
sweep still loses the work. For an *orderly* shutdown the gap is
closed rather than narrowed: ``extras/cluster-linger-system.service`` asserts
on the way down, ordered so it runs while the network is still up — which is
the one thing the watcher's own SIGTERM cannot promise, being a detached
process that systemd kills in its final sweep, after the network has gone.
What is left uncovered is power loss, and nothing on this side reaches that.

Releasing is the other half, and it has one rule: **linger this tool did not
turn on is never turned off by it.** Somebody who enabled it themselves (or
whose site's login script did) has their own work depending on it, and a
``disable-linger`` on their behalf kills that work the moment their last
session ends. So every enable that actually happens leaves a node-side note
of what it created (:data:`OWNED`), and the release goes ahead only while the
linger file is still exactly that one. The note lives on the node, not on this
machine, because the keeper enables linger from the node's crontab with no
client present, and because a second client machine must see it too.
"""

from __future__ import annotations

import shlex

from . import config

#: A wedged logind must not hold up the work it exists to protect. Measured on
#: holylogin05, 2026-09-17: under load 15 every ``loginctl`` call answered
#: neither way, so a piggybacked assertion sat until the ssh command's own
#: timeout and turned a single session create into two minutes. The bound is
#: node-side because the caller's timeout is the wrong lever — it would fail
#: the create along with the assertion.
TIMEOUT = 5

#: logind decides whether an account lingers by whether this file exists
#: (``user_check_linger_file``), so the file is the state rather than a cache of
#: it. That makes a stat the honest question, and it is the only form that
#: answers on a node whose logind will not: holylogin05 timed out every
#: ``loginctl`` call while its linger file sat there, enabled.
LINGER_DIR = "/var/lib/systemd/linger"

#: What the node's linger file is, precisely enough to tell it apart from a
#: later one: inode and nanosecond mtime. logind touches the file on every
#: ``enable-linger``, so the owner re-enabling it after us changes this too.
#: Pinned locale and zone so a cron shell and an ssh shell print it alike, and
#: ``-n`` so no name lookup can make it vary. Deliberately free of ``%``,
#: which crontab(5) would read as a newline (it ends up in :func:`keeper_line`).
#: Where ``ls`` cannot say, it prints nothing, and nothing is never a match.
STAMP = (f'LC_ALL=C TZ=UTC0 ls -nid --full-time {LINGER_DIR}/"$(id -un)" '
         "2>/dev/null")

#: The note that *this tool* turned linger on here, holding the :data:`STAMP`
#: of what it created. Per node, because home is shared across a site's login
#: nodes while linger is not.
OWNED_DIR = '"$HOME/.cluster/linger"'
OWNED = '"$HOME/.cluster/linger/$(uname -n)"'

#: Only root can create that file, so asserting still has to go through logind
#: — but only when there is something to change. Reading first is normally the
#: wrong shape (two round trips to learn what the write guarantees); here the
#: read is a stat in the same shell, and it keeps the steady state off a bus
#: that a busy login node cannot always serve.
#:
#: When logind does get asked and agrees, the result is noted in :data:`OWNED`:
#: that is the only case in which :data:`SETTLE` may later take it away again.
#: An account that already lingered never reaches the note. The exit status is
#: logind's, whether or not the note could be written.
ENABLE = (f'test -e {LINGER_DIR}/"$(id -un)" '
          f"|| {{ timeout {TIMEOUT} loginctl enable-linger; _lg=$?; "
          f"if [ $_lg = 0 ]; then mkdir -p {OWNED_DIR} 2>/dev/null; "
          f"{STAMP} > {OWNED} 2>/dev/null; fi; [ $_lg = 0 ]; }}")

#: Marks the crontab line as ours. A hand-edited crontab is never guessed at:
#: only lines carrying this are touched, and it is how the line is found again
#: to take it out.
KEEPER_MARKER = "cluster-linger-keeper"

#: Reads the node's crontab into ``$c``, once, or leaves the subshell it runs
#: in. Nothing printed counts as an empty crontab only when crontab(1) says
#: there is none: a `crontab -l` that failed any other way (a spool on NFS
#: that did not answer, say) prints nothing too, and writing back what it
#: printed would wipe every line the user has there.
CRONTAB_READ = ("c=$(crontab -l 2>/dev/null) || "
                "{ crontab -l 2>&1 | grep -q 'no crontab for' || exit 1; c=; }")

_MARK = shlex.quote(KEEPER_MARKER)

#: Takes the keeper's line out of the node's crontab, keeping every other
#: line. The crontab is read once and rewritten only if that read worked and
#: found the line.
KEEPER_REMOVE = (f"( {CRONTAB_READ}; case $c in *{_MARK}*) ;; *) exit 0 ;; esac; "
                 f"printf '%s\\n' \"$c\" | grep -vF {_MARK} | crontab - )")

#: Which of the two cgroups the node's tmux server is actually in — the thing
#: that decides whether it survives the connection ending, and the one piece
#: of this that cannot be fixed after the fact. A server started outside a
#: user scope cannot be moved into one: cgroup v2 wants write access to the
#: *source* cgroup as well as the destination, and the session scope belongs
#: to root. Measured on holylogin06: creating the destination is permitted,
#: the write to cgroup.procs is denied. So the only repair is to recreate the
#: sessions, which makes reporting it the whole of the job.
#: The *server* pid, asked of tmux itself and only then guessed at from ps.
#: Matching /tmux/ in a process list would also match an attached client, and
#: a client always sits in the session scope of whatever connection is
#: attached to it — so a perfectly protected server would be reported as
#: doomed whenever somebody had it open. ps is kept as the fallback for a
#: server with no session to answer through, and matches the server's own
#: comm (``tmux: server``) rather than the word tmux.
SERVER_PID = (
    "p=$(tmux display-message -p '#{pid}' 2>/dev/null); "
    'case "$p" in \'\'|*[!0-9]*) '
    'p=$(ps -u "$(id -un)" -o pid=,comm= '
    '| awk \'/tmux: server/{print $1; exit}\') ;; esac; ')


def scope_probe(key="scope"):
    """Shell printing ``<key>=user|session|none|unknown`` for the tmux server.

    Parameterised by key so the same probe can be a line of a state read and a
    marker appended to a session create, without two versions of it drifting
    apart.
    """
    return (f'{SERVER_PID}'
            f'if [ -z "$p" ]; then echo {key}=none; else '
            f'case "$(cat /proc/$p/cgroup 2>/dev/null)" in '
            f'*user@*) echo {key}=user ;; '
            f'*session-*) echo {key}=session ;; '
            f'*) echo {key}=unknown ;; esac; fi')


SCOPE_READ = scope_probe()

#: Distinct from the plain ``scope=`` key so it cannot be confused with a line
#: of ordinary command output when it rides along with a session create.
SCOPE_MARKER = "cluster-scope"


def scope_report(logins):
    """Shell that says which cgroup a just-started server actually landed in.

    The whole point of :data:`SCOPE_SETUP` is that it degrades quietly — a
    node with no user manager still gets its session rather than an error. But
    quiet is exactly wrong afterwards: a silent downgrade means the tool goes
    on making sessions that will not survive, and nothing says so until
    somebody thinks to run `doctor`. This rides the create's own round trip so
    the downgrade is reported the moment it happens.
    """
    return f"{scope_probe(SCOPE_MARKER)}; " if required(logins) else ""


def scope_of(output):
    """The scope a :func:`scope_report` reported, or ``""`` if it did not."""
    for line in (output or "").splitlines():
        key, _, value = line.strip().partition("=")
        if key == SCOPE_MARKER:
            return value
    return ""


#: Exits 0 either way for every answer: "no" is an answer, and only a failed
#: *question* should come back empty.
READ = (f'test -e {LINGER_DIR}/"$(id -un)" && echo linger=yes || echo linger=no; '
        f"crontab -l 2>/dev/null | grep -qF '{KEEPER_MARKER}' "
        f"&& echo keeper=yes || echo keeper=no; "
        f"{SCOPE_READ}")


#: The whole decision, as one command. Whether a node should go on keeping
#: processes is a question about the node, and never about the flag a caller
#: happened to use: `repin` passes keep_tmux only to avoid killing sessions
#: twice, `close --keep-tmux` means work is being left behind, a full close
#: means none is, and a sweep may have spared somebody else's. All of them want
#: the same answer — assert if tmux is still running there, release if nothing
#: is — and only the node can give it.
#:
#: One command, not a check followed by an action: between those two there is a
#: window in which a session appears on the node and is killed by the release
#: that follows. `if`/`else` rather than ``&&``/``||`` for the same reason —
#: with ``a && b || c``, a failing *assert* falls through to the release.
#:
#: The release itself happens only where this tool turned linger on, and only
#: while the linger file is still the one it created (see :data:`OWNED`). A
#: file that has changed since — the owner re-enabled it, or the node cleared
#: it and something else made a new one — is somebody else's, so the note is
#: dropped and linger left alone. A failed release keeps the note, so the next
#: settle tries again.
SETTLE = (
    f"if tmux ls >/dev/null 2>&1; then {ENABLE} >/dev/null 2>&1; else "
    f"{KEEPER_REMOVE} >/dev/null 2>&1; "
    f"if [ -s {OWNED} ]; then "
    f'if [ "$({STAMP})" != "$(cat {OWNED})" ]; then rm -f {OWNED}; '
    f"elif timeout {TIMEOUT} loginctl disable-linger >/dev/null 2>&1; then "
    f"rm -f {OWNED}; fi; fi; "
    f"fi; true"
)


#: Where long-lived work has to live if it is to outlive the connection that
#: started it: ``user@$UID.service`` rather than the connection's session
#: scope, which `tmux new-session` inherits from the shell that ran it. The
#: module docstring has the two deaths and the measurement that tells them
#: apart. FASRC ships exactly this in /etc/profile.d/linger.sh, as
#: ``alias tmux="systemd-run --scope --user tmux"`` — but an alias reaches
#: interactive shells only, and every command this tool sends is
#: non-interactive, so the tool has to do it itself.
SCOPE = "systemd-run --scope --user --quiet"

#: Sets ``$S`` to :data:`SCOPE` where that actually works and to nothing where
#: it does not, by trying it rather than by looking for the binary: a node can
#: have `systemd-run` and still refuse a user scope when there is no user
#: manager to put it in. Unset expands to nothing, so ``$S tmux …`` degrades
#: to plain ``tmux …`` — including on a backend where this whole module is
#: switched off and the prefix is never emitted at all.
SCOPE_SETUP = (f'S=; timeout {TIMEOUT} {SCOPE} true >/dev/null 2>&1 '
               f'&& S="{SCOPE}"; ')


def scope_setup(logins):
    """Shell that makes ``$S`` usable in front of a server-starting command."""
    return SCOPE_SETUP if required(logins) else ""


def bound(logins):
    """The seconds :func:`prefix` and :func:`scope_setup` can add to a
    command on the node: each asks systemd once, bounded there by
    :data:`TIMEOUT`, so a wedged logind or user manager costs that and no
    more."""
    return 2 * TIMEOUT if required(logins) else 0


def keeper_line():
    """The node-side crontab line that re-asserts linger once a minute.

    This is the only mechanism that still works when the client is not there to
    assert anything: a closed laptop, a dropped wifi, a machine that lost power
    rather than shutting down. Everything else in this module runs on the
    client, and stops the moment the client does — while the instant that
    decides whether the work lives can come long afterwards. FASRC's sshd sets
    no ClientAliveInterval, so a vanished client's session is held open on the
    node until TCP keepalive gives up, hours later; *that* is when logind looks
    at the linger file. Something has to be asserting through that window, and
    by definition it cannot be the machine that went away.

    One line, node-local (crontab is per node, as tmux is), and doing nothing at
    all in the normal case: the stat finds the file and stops.
    """
    return f"* * * * * {ENABLE} >/dev/null 2>&1 # {KEEPER_MARKER}"


def keeper_wanted(logins):
    """Whether the node-side keeper should be installed for this backend."""
    return required(logins) and logins.settings.flag("LINGER_KEEPER")


def keeper_command(wanted):
    """One round trip that makes the node's crontab agree with *wanted*.

    Both directions read the existing crontab once (see :data:`CRONTAB_READ`)
    and rewrite it at most once, and neither ever touches a line that is not
    ours: installing appends, removing is a `grep -vF` of the marker. A
    crontab on a login node belongs to everything else you run there too.
    Each runs in a subshell, so giving up leaves only the reconciliation.
    """
    if not wanted:
        return KEEPER_REMOVE
    line = shlex.quote(keeper_line())
    return (f"( {CRONTAB_READ}; case $c in *{_MARK}*) exit 0 ;; esac; "
            f"{{ [ -z \"$c\" ] || printf '%s\\n' \"$c\"; printf '%s\\n' {line}; }} "
            "| crontab - )")


def apply_command(wanted):
    """Assert linger *and* settle the keeper question, in one round trip.

    The two always travel together: every path with a reason to assert linger
    has the same reason to make the keeper match the setting, and the node
    that most needs asking is the one too loaded to be asked twice.

    The exit status is linger's alone. A crontab that could not be rewritten
    is a reconciliation that did not happen, not an unprotected login, and
    reporting it as the latter would send `doctor` and the shutdown hook
    shouting about the wrong thing.
    """
    return (f"if {ENABLE} >/dev/null 2>&1; then ok=0; else ok=1; fi; "
            f"{keeper_command(wanted)} >/dev/null 2>&1; exit $ok")


def apply(logins, name, timeout=config.COMMAND_TIMEOUT):
    """Assert linger on *name*'s node and bring its keeper in line, or not.

    This is what every client-side path that wants a node protected calls:
    connecting, `cluster linger`, and the watcher's tick. Because the keeper
    is reconciled rather than merely installed, turning ``LINGER_KEEPER`` off
    takes it back out on its own, wherever the setting is next acted on —
    there is no separate cleanup step to remember, which is the only way a
    node-side leftover ever actually goes away.
    """
    if not required(logins):
        return True
    command = apply_command(keeper_wanted(logins))
    return _run(logins, name, command, timeout).returncode == 0


def _run(logins, name, command, timeout=config.COMMAND_TIMEOUT):
    """*command* on *name*'s node, within *timeout*: REMOTE_COMMAND_TIMEOUT
    unless one is given, and none at all for None (Logins.run_remote)."""
    return logins.run_remote(name, command, timeout=timeout)


def remove_keeper(logins, name):
    """Take the keeper out of *name*'s node crontab, leaving everything else.

    Deliberately unconditional, where :data:`SETTLE` removes it only on a node
    with no tmux server left at all. The difference is who is asking. Settling
    happens on the way out of a node whose contents the caller does not know,
    so it must not quietly strip protection from sessions it spared; this is
    somebody typing ``--remove-keeper``, or a ``LINGER_KEEPER`` they have just
    turned off, and refusing an instruction that explicit would mean the
    setting never takes effect on the nodes actually in use.

    Removing the line stops re-assertion; it never disables linger and so can
    never kill anything on its own.
    """
    return _run(logins, name, keeper_command(False)).returncode == 0


def required(logins):
    """Whether this backend's nodes need linger to keep work alive."""
    return bool(logins.backend.reaps_on_logout) and logins.settings.flag("LINGER")


def prefix(logins):
    """Shell prefix asserting linger ahead of whatever runs next.

    For callers already paying for a round trip — creating a session is the
    moment the protection starts mattering, and this way it costs nothing
    extra. Its status is swallowed deliberately: the caller's command decides
    the exit code, and a node without ``loginctl``, or with a logind that will
    not answer, must not turn every session create into a failure or make the
    caller wait out its own timeout. See :data:`TIMEOUT`.
    """
    return f"{ENABLE} >/dev/null 2>&1 || true; " if required(logins) else ""


def assert_enabled(logins, name, timeout=config.COMMAND_TIMEOUT):
    """Re-assert linger on *name*'s node; True when the node accepted it.

    True without asking for backends that do not reap, so callers can log a
    failure without also having to know which sites the question applies to.

    The timeout (REMOTE_COMMAND_TIMEOUT) is generous because what it has to
    cover is not this command — which is a stat — but sshd session setup on a
    login node under load. Measured on holylogin05 at load 15: the site's own
    login hook blocks on the same wedged logind, and a 20s bound reports a node
    as unprotected while its linger file sits there, enabled. A false alarm about lost work is worse than
    a slow answer, and callers that cannot wait run their logins concurrently.
    """
    if not required(logins):
        return True
    return _run(logins, name, ENABLE, timeout).returncode == 0


def settle(logins, name):
    """Make *name*'s node keep, or stop keeping, processes — as its state says.

    Every ending calls this: closing a login, moving one, sweeping a node.
    Asserting when work is still there is what the rest of this module is for;
    releasing when none is left matters just as much, and is easier to forget.
    Without it every node ever connected to keeps a user manager alive for
    good, and a keeper asserting once a minute for work that no longer exists —
    a leak, and on a shared login node somebody else's memory.

    "Nothing left" means no tmux server at all, not merely none of ours.
    `close` and `clean` both spare foreign and untagged sessions deliberately,
    and ``disable-linger`` with those running kills them on the spot, logind
    having no reason to keep a user with no sessions. That is the exact
    accident this module exists to prevent, so the test and the act are one
    command on the node. See :data:`SETTLE`.

    And "release" means undoing what this tool did, never more: linger that
    was on before it arrived, or that its owner turned on again since, stays
    on. See :data:`OWNED`.
    """
    if not required(logins):
        return True
    return _run(logins, name, SETTLE).returncode == 0


def read(logins, name):
    """``{"linger": …, "keeper": …, "scope": …}``, ``""`` where unanswered.

    All three in one round trip because they are one question — "will this
    node keep my work" — and because the node that most needs asking is the
    one too busy to be asked twice. ``scope`` is ``user`` (survives),
    ``session`` (does not), ``none`` (no server) or ``unknown``.
    """
    found = {"linger": "", "keeper": "", "scope": ""}
    allowed = {"linger": ("yes", "no"), "keeper": ("yes", "no"),
               "scope": ("user", "session", "none", "unknown")}
    said = logins.remote_value(name, READ)
    for line in said.splitlines():
        key, _, value = line.strip().partition("=")
        if key in found and value in allowed[key]:
            found[key] = value
    return found
