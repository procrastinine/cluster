"""Moving data between two clusters.

The thing to understand first: **rclone and rsync have no server-to-server mode
over ssh.** ``rclone copy sftpA: sftpB:`` is a legitimate command, but every byte
is pulled to wherever rclone runs and pushed back out. So the question is not
"which tool" but "which machine runs it", and there are only two answers:

``direct``
    Run the transfer *on one of the clusters* — the executor — which dials the
    other over ssh. Bytes go cluster to cluster at cluster bandwidth, and this
    machine only supervises. Requires the executor to reach the peer and to be
    able to authenticate there, which is what agentscope arranges.

``relay``
    Run it here, with two sftp remotes. Every byte crosses this machine twice, so
    it is bounded by the slowest hop of a home connection — but it needs nothing
    from the clusters beyond the connections that already exist. The honest
    fallback when ``direct`` is impossible.

``globus``
    Hand the job to Globus and let the endpoints move it. Right for very large,
    restartable, unattended moves; see globuslayer.

Which cluster can be the executor is not a preference, it is a property of the
credentials. The executor authenticates to the peer as itself, so the *peer's*
credential must be lendable — a key file, loadable into an agent. FASRC
authenticates with a password and a TOTP code typed into a pty, which cannot be
lent to anything; NERSC's sshproxy certificate is a file, which can. So for this
pair the executor is always FASRC and the peer always NERSC, in both directions,
and that falls out of each backend's ``lends_credential`` rather than being
written down here.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import socket

from . import agentscope, backends, config, registry, ui
from . import platform as plat
from .remote_sh import sftp_path
from .riding import LINK_POLL, SILENCED, Link, Ride
from .sshmux import Logins, rclone_ssh_value, rider_argv
from .state import State
from .transfer import (MIN_RCLONE, HomeUnknown, RcloneOps, ShellHome, TransferSpec,
                       Transfers, channel_ssh, concurrency, expectation,
                       parse_rclone_version, probe_limits, rclone_flags, sftp_arg,
                       verify, version_text)

ENGINES = ("auto", "direct", "relay", "globus")

#: How the executor runs its rclone: in the background of a bash that reads
#: the heartbeat this machine sends down the channel (riding.run_riding),
#: and stops it once none has come for $1 seconds or the channel has closed.
#: Otherwise an rclone whose connection to this machine was lost would go on
#: copying there, unseen, while the resumed transfer copies the same files,
#: and Ctrl-C here would stop only this end. The stop is a SIGTERM, then a
#: SIGKILL if rclone is still there $2 seconds later, and the watchdog then
#: exits 7: from then on it ignores the TERM that otherwise ends it once
#: rclone has, so that status is what it ends with. The script's status is
#: rclone's, or riding.SILENCED when the watchdog stopped a run that
#: failed; the shell's own notice of a killed job is not printed.
TIED = ('t=$1; g=$2; shift 2; exec 3<&0; "$@" </dev/null & r=$!; '
        '( while IFS= read -r -t "$t" _ <&3; do :; done; trap "" TERM; '
        'kill "$r" 2>/dev/null; i=0; '
        'while kill -0 "$r" 2>/dev/null && [ "$i" -lt "$g" ]; do '
        'sleep 1; i=$((i + 1)); done; '
        'kill -0 "$r" 2>/dev/null && kill -9 "$r" 2>/dev/null; exit 7 ) & w=$!; '
        'wait "$r" 2>/dev/null; s=$?; kill "$w" 2>/dev/null; wait "$w"; '
        f'[ $? -eq 7 ] && [ "$s" -ne 0 ] && s={SILENCED}; exit "$s"')


def tied(argv, silence, grace, name="cluster-transfer"):
    """The shell command that runs *argv* on the executor under TIED, as
    *name*: the name its processes there go by (see still_there)."""
    return shlex.join(["bash", "-c", TIED, name, str(int(silence)),
                       str(int(grace)), *argv])


def still_there(name):
    """The shell command that fails with status 1 when none of this user's
    processes goes by *name* there, and only then. The pattern is bracketed,
    so the shell that runs it is not one of them."""
    pattern = f"[{name[0]}]{name[1:]}"
    return f'pgrep -u "$(id -u)" -f {shlex.quote(pattern)} >/dev/null'


def far_silence(settings):
    """Seconds the executor's rclone goes on without a heartbeat before it is
    stopped: as long as the master carrying the heartbeat itself rides out
    silence from its server (ServerAliveInterval x ServerAliveCountMax), and
    one look (LINK_POLL) more. A stall the connection survives is then one
    the rclone survives too; one it does not is a lost link, which this
    machine sees for itself."""
    return (settings.int("SSH_SERVER_ALIVE_INTERVAL")
            * settings.int("SSH_SERVER_ALIVE_COUNT_MAX") + LINK_POLL)


class DirectUnavailable(Exception):
    """The direct route cannot be set up — but no bytes have moved yet.

    Raised only for failures relay would not share: the executor cannot reach or
    authenticate to the peer, has no usable rclone, or cannot hold a forwarded
    agent. Deliberately *not* raised once rclone is running: a transfer that
    fails halfway has already moved data, and quietly restarting it down a much
    slower path is not a decision to make on someone's behalf. Failures relay
    would hit too (no credential, no connection to the executor) stay fatal.
    """

    def __init__(self, message, *hints):
        super().__init__(message)
        self.hints = hints


class _Closer:
    """Adapts a plain callable to contextlib.closing."""

    def __init__(self, close):
        self.close = close


class Endpoint:
    """One side of a cross-cluster transfer: a backend, and a path on it."""

    def __init__(self, backend_name, path):
        self.backend = backends.load(backend_name)
        self.state = State(self.backend)
        self.logins = Logins(self.backend, self.state)
        self.path = path

    @property
    def name(self):
        return self.backend.name

    @property
    def settings(self):
        return self.backend.settings

    def __repr__(self):
        return f"{self.name}:{self.path}"


def split_endpoint(token):
    """``fasrc:~/x`` -> ("fasrc", "~/x"); anything unqualified -> (None, token).

    A built-in backend's name, one of its aliases, or a *login* name all name
    a cluster — login names are a global namespace, so ``main:~/x`` is as
    unambiguous as ``fasrc:~/x``, and it is how a backend of your own is
    named here. ``local:`` and ``remote:`` keep their existing meaning, and a
    plain ``host:path`` or a drive letter is left alone rather than half-parsed.
    """
    if ":" not in token:
        return None, token
    head, rest = token.split(":", 1)
    if head in ("local", "remote"):
        return None, token
    key = backends.as_shorthand(head)
    if key:
        return key, rest
    owner = registry.find(head)  # local state only: no network, no auth
    if owner:
        return owner, rest
    return None, token


def is_cross(source, dest):
    """True when both sides name a cluster and they are different clusters."""
    src, _ = split_endpoint(source)
    dst, _ = split_endpoint(dest)
    return bool(src and dst and src != dst)


class CrossTransfer:
    """A transfer whose two sides are different clusters."""

    def __init__(self, source, dest, *, engine="auto", executor=None,
                 operation="copy", contents=False, symlinks="follow",
                 dry_run=False, progress=False, quiet=False, keep=False,
                 transfers=0, checkers=0, extra=(), allow_agent=True,
                 peer_node=None, executor_node=None, via=None):
        src_name, src_path = split_endpoint(source)
        dst_name, dst_path = split_endpoint(dest)
        self.source = Endpoint(src_name, src_path)
        self.dest = Endpoint(dst_name, dst_path)
        self.engine = engine
        self.want_executor = backends.as_backend(executor) if executor else None
        if executor and not self.want_executor:
            ui.die(f"unknown cluster for --executor: {executor}")
        self.operation = operation
        self.contents = contents or src_path.rstrip().endswith("/")
        self.symlinks = symlinks
        self.dry_run = dry_run
        self.progress = progress
        self.quiet = quiet
        self.keep = keep
        self.transfers = transfers
        self.checkers = checkers
        self.extra = list(extra)
        self.allow_agent = allow_agent
        self.peer_node = peer_node
        self.executor_node = executor_node
        self.via = via
        #: Set when direct gives up *after* opening its executor connection. That
        #: connection is a perfectly good plain transport, and on FASRC it cost a
        #: TOTP window — so relay inherits it rather than paying again, in the
        #: exact situation where something is already going wrong.
        self._inherited = None

    # --- planning -----------------------------------------------------------
    @staticmethod
    def _lendable(endpoint):
        """Can this side's credential be lent to ssh on another host?

        A declared capability, so planning never fetches anything: deciding to
        relay must not cost a certificate renewal.
        """
        return bool(endpoint.backend.lends_credential)

    def choose_executor(self):
        """(executor, peer), or (None, None) when no direct route exists.

        The executor runs the transfer and dials the peer, so it is the *peer's*
        credential that has to travel. That, not preference, decides.
        """
        if self.want_executor:
            if self.want_executor not in (self.source.name, self.dest.name):
                ui.die(f"--executor {self.want_executor} is not one side of this "
                       f"transfer ({self.source.name} -> {self.dest.name})")
            pair = ((self.source, self.dest) if self.want_executor == self.source.name
                    else (self.dest, self.source))
            if not self._lendable(pair[1]):
                ui.die(
                    f"{pair[0].backend.label} cannot authenticate to "
                    f"{pair[1].backend.label}",
                    f"{pair[1].backend.label} has no credential that can be lent "
                    "to another host (it is typed in, not stored as a key)",
                    f"try --executor {pair[1].name}, or --engine relay",
                )
            return pair

        viable = [(runner, peer)
                  for runner, peer in ((self.source, self.dest),
                                       (self.dest, self.source))
                  if self._lendable(peer)]
        if not viable:
            return None, None
        # Given a real choice, run it where the data is read: --move then deletes
        # on the side doing the reading, and filters apply there too.
        for runner, peer in viable:
            if runner is self.source:
                return runner, peer
        return viable[0]

    def plan(self):
        """Pick the engine without opening anything. (engine, executor, peer)."""
        if self.engine in ("relay", "globus"):
            return self.engine, None, None
        executor, peer = self.choose_executor()
        if executor is None:
            if self.engine == "direct":
                ui.die("no direct route between these clusters",
                       "neither side has a credential that can be lent to the "
                       "other, so neither can authenticate to its peer",
                       "use --engine relay (data flows through this machine)")
            return "relay", None, None
        return "direct", executor, peer

    def describe_plan(self):
        engine, executor, peer = self.plan()
        if engine == "direct":
            return (f"direct: {executor.backend.label} runs the transfer and "
                    f"dials {peer.backend.label}")
        if engine == "relay":
            return "relay: this machine moves the bytes between both clusters"
        return "globus: the Globus service moves the bytes between collections"

    # --- shared rclone flags -------------------------------------------------
    def _rclone_flags(self, settings, shared=False):
        """The TRANSFER_* flags, as for a transfer from this machine.

        A cross-cluster transfer is long and often unattended, so without
        --progress it logs one-line stats, which read well in a log file.
        """
        transfers, checkers = concurrency(settings, self.transfers, self.checkers,
                                          shared=shared)
        return rclone_flags(settings, transfers, checkers, dry_run=self.dry_run,
                            progress=self.progress, quiet=self.quiet,
                            symlinks=self.symlinks, extra=self.extra,
                            log_stats=True)

    # --- entry point --------------------------------------------------------
    def run(self):
        engine, executor, peer = self.plan()
        if not self.quiet:
            ui.note(self.describe_plan())
        if engine == "globus":
            from . import globuslayer

            return globuslayer.run_cross(self)
        if engine == "direct":
            try:
                return self._run_direct(executor, peer)
            except DirectUnavailable as unavailable:
                if self.engine == "direct":
                    # Explicitly asked for. Relay is often orders of magnitude
                    # slower, so substituting it silently would be a worse
                    # surprise than failing.
                    ui.die(str(unavailable), *unavailable.hints,
                           "or use --engine relay to move it through this machine")
                ui.warn(f"direct transfer unavailable: {unavailable}")
                for hint in unavailable.hints:
                    ui.note(hint)
                ui.warn("falling back to relay — every byte will cross this "
                        "machine, which is much slower over a home connection")
        return self._run_relay()

    # --- direct -------------------------------------------------------------
    def _run_direct(self, executor, peer):
        """rclone on *executor*, reaching *peer* with a forwarded credential."""
        if not self.allow_agent:
            ui.die("a direct transfer lends the peer's credential to the "
                   "executor, which --no-agent-forward forbids",
                   "use --engine relay instead")
        if self.via:
            ui.warn("--via is ignored for a direct transfer: agent forwarding is "
                    "fixed when a master is created, and a login's master has "
                    "none")

        # Short node names ("login01", "dtn01") are only expandable once the
        # plan says which cluster each one belongs to.
        if self.executor_node:
            self.executor_node = executor.backend.fqdn(self.executor_node)
        if self.peer_node:
            self.peer_node = peer.backend.fqdn(self.peer_node)

        agent = agentscope.borrow(peer.backend)
        if agent is None:
            ui.die(f"{peer.backend.label} has no lendable credential")

        try:
            agent.start()
        except ui.Die as exc:
            # relay authenticates from here and needs no agent at all.
            raise DirectUnavailable(
                "could not hold the peer credential in an ssh-agent",
                "is ssh-agent installed and runnable?") from exc

        xfer = Transfers(executor.logins)
        with contextlib.closing(_Closer(agent.close)):
            if not self.quiet:
                ui.info(f"lending the {peer.backend.label} credential to "
                        f"{executor.backend.label} over a forwarded agent "
                        "(the key itself is never written there)")
            tag, sock = self._open_forwarding(xfer, executor, agent)
            handed_over = False
            node = self.executor_node

            def restore(lost):
                return xfer.reopen(tag, lost, node, self.quiet, forward_agent=True,
                                   agent_env=agent.env())

            link = Link(f"transfer connection {tag} to {executor.backend.label}",
                        sock, restore, executor.logins, node)
            try:
                return self._direct_body(executor, peer, link, agent)
            except DirectUnavailable:
                # Hand the connection to relay only if relay is actually going to
                # run. With an explicitly chosen engine there is no fallback, so
                # holding it open would strand a connection nothing will close.
                if self.engine != "direct":
                    self._inherited = (executor.name, tag)
                    handed_over = True
                raise
            finally:
                xfer.lease_drop(tag)
                if not self.keep and not handed_over:
                    xfer.close_connection(tag)

    def _open_forwarding(self, xfer, executor, agent):
        """A master whose forwarded agent is not known to be gone.

        A forwarded agent belongs to the process that opened the master, so a
        master left behind by ``--keep`` outlives the agent that fed it: the
        socket on the far side is still there and answers nothing. So a master
        that was already up is asked whether its agent answers, which is the
        question itself; whether the peer lets us in is a different one, which
        a peer host being down would answer "no" as well.

        One whose agent is gone is replaced, but only when no other run holds
        a lease on it: another run may be riding it as a plain transport, and
        closing it under that run would cut its transfer off.
        """
        base_tag = (executor.backend.short(self.executor_node)
                    if self.executor_node else "pool") + "-fwd"
        reused = xfer.is_active(base_tag)
        tag = xfer.open_connection(node=self.executor_node, quiet=self.quiet,
                                   forward_agent=True, agent_env=agent.env())
        sock = executor.state.xfer_socket(tag)
        if not reused or self._agent_answers(executor, sock, agent) is not False:
            return tag, sock
        if not self.quiet:
            ui.note(f"transfer connection {tag} forwards an agent that is gone "
                    "(it belonged to the run that opened the connection)")
        if not xfer.close_connection(tag):
            xfer.lease_drop(tag)
            raise DirectUnavailable(
                f"transfer connection {tag} forwards an agent that is gone, and "
                "another run still uses the connection",
                "its forwarding cannot be replaced while it is open",
                f"once that run is done: cluster {executor.backend.cli_flag()} transfer "
                f"--close {tag}")
        tag = xfer.open_connection(node=self.executor_node, quiet=self.quiet,
                                   forward_agent=True, agent_env=agent.env())
        return tag, executor.state.xfer_socket(tag)

    def _agent_answers(self, executor, sock, agent):
        """Does the agent the master at *sock* forwards answer on the executor?

        ssh-add says: 0 is an agent holding a key, 1 one that holds none or
        cannot be reached through the master, 2 no agent at all. None when
        ssh-add could not be run there (ssh's 255, a timeout, no ssh-add),
        which says nothing about the agent either way.
        """
        run = self._on_executor(executor, sock, self.executor_node, agent)
        got = run("ssh-add -l", timeout=executor.settings.int("REMOTE_COMMAND_TIMEOUT"))
        if got.returncode == 0:
            return True
        if got.returncode in (1, 2):
            return False
        return None

    def _on_executor(self, executor, sock, node, agent):
        """A callable that runs a shell command on the executor node, within
        the timeout it is given: every caller knows what its command does,
        and REMOTE_COMMAND_TIMEOUT is only for the small ones."""
        target = executor.backend.target(node)
        prefix = rider_argv(sock, target, ["-o", "ForwardAgent=yes",
                                           "-o", "BatchMode=yes"])

        def run(command, timeout, **watch):
            return plat.run(prefix + [command], timeout=timeout, env=agent.env(),
                            **watch)

        run.prefix = prefix
        return run

    def _peer_ssh(self, peer, host=None):
        """The ssh command the executor uses to reach the peer at *host*.

        No ControlPath: this connection is made from the executor, where none of
        our sockets exist. accept-new because there is nobody there to answer a
        host-key prompt, which makes the first connection trust-on-first-use:
        the peer's key is recorded in the executor's known_hosts and a later
        change is refused. The lent certificate authenticates *us* to the peer;
        it says nothing about whether the peer is who it claims to be, so it is
        no substitute for host-key checking.
        """
        host = host or self.peer_node or peer.backend.inbound_transfer_host()
        settings = peer.settings
        return ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={settings.int('PEER_CONNECT_TIMEOUT')}",
                "-o", f"ServerAliveInterval={settings.int('SSH_SERVER_ALIVE_INTERVAL')}",
                "-o", f"ServerAliveCountMax={settings.int('SSH_SERVER_ALIVE_COUNT_MAX')}",
                f"{peer.backend.user}@{host}" if peer.backend.user else host]

    def _peer_candidates(self, peer):
        """Hosts on the peer worth trying, best first.

        An explicit --peer-node is an instruction, not a suggestion, so it is
        used alone; otherwise every routable transfer node is a candidate and one
        being down costs a probe instead of the transfer.
        """
        if self.peer_node:
            return [self.peer_node]
        return list(peer.backend.inbound_transfer_hosts())

    def _pick_peer_host(self, run, peer):
        """The first peer host that is both reachable and authenticating.

        Reachability and authentication are checked separately so a drained data
        transfer node and a dead credential cannot arrive as the same symptom —
        the first is worth trying the next host for, the second never is.
        """
        candidates = self._peer_candidates(peer)
        # Each connect gets its own deadline: a host behind a filtering
        # firewall never refuses, it just never answers.
        wait = peer.settings.int("PEER_CONNECT_TIMEOUT")
        # The ssh to the peer is a connection of its own, let in before it
        # runs anything (REMOTE_COMMAND_TIMEOUT).
        command = peer.settings.int("REMOTE_COMMAND_TIMEOUT")
        unreachable, refused = [], []
        for host in candidates:
            if run(self._port_check(host, wait),
                   timeout=wait + command).returncode != 0:
                unreachable.append(host)
                continue
            got = run(shlex.join(self._peer_ssh(peer, host) + ["true"]),
                      timeout=2 * command)
            if got.returncode == 0:
                if unreachable and not self.quiet:
                    ui.note(f"skipped {', '.join(unreachable)} (no answer on 22); "
                            f"using {host}")
                return host, None
            refused.append((host, (got.stderr or "").strip()[-200:]))
        if refused:
            # Reachable but rejecting us: the credential is the problem, and no
            # other host on the same cluster will feel differently.
            host, detail = refused[0]
            return None, DirectUnavailable(
                f"{peer.backend.label} refused the forwarded credential at {host}",
                detail or "no detail from ssh")
        return None, DirectUnavailable(
            f"no {peer.backend.label} transfer host answered on port 22",
            f"tried: {', '.join(candidates)}",
            "an outbound firewall on the executing cluster would look like this")

    @staticmethod
    def _port_check(host, wait):
        """Shell that succeeds when *host* accepts a connection on port 22.

        bash's /dev/tcp, since nc is not everywhere; the host is an argument,
        never part of the script, and ``timeout`` bounds the connect.
        """
        return (f"timeout {int(wait)} bash -c "
                f"{shlex.quote('exec 3<>/dev/tcp/$1/22')} _ {shlex.quote(host)}")

    def _direct_body(self, executor, peer, link, agent):
        node = link.node
        run = self._on_executor(executor, link.sock, node, agent)

        peer_host, problem = self._pick_peer_host(run, peer)
        if problem is not None:
            problem.hints += (f"the agent holds: {agent.loaded() or '(nothing)'}",)
            raise problem
        peer_ssh = rclone_ssh_value(self._peer_ssh(peer, peer_host))

        # Asked now, so every path built from it is real before anything moves.
        small = executor.settings.int("REMOTE_COMMAND_TIMEOUT")
        home = ShellHome(lambda command: run(command, timeout=small))
        try:
            home.home()
        except HomeUnknown as exc:
            raise DirectUnavailable(
                f"could not find the home directory on {executor.backend.label}",
                str(exc)) from exc
        rclone = self._remote_rclone(run, executor, home)

        # From the executor's point of view this is an ordinary transfer: its own
        # filesystem on one side, an sftp remote on the other. The only
        # difference is that every path question is asked over there, of the
        # rclone that will run the transfer.
        def on_executor(argv, timeout, **watch):
            return run(shlex.join(argv), timeout=timeout, **watch)

        limits = probe_limits(executor.settings)
        far = RcloneOps([rclone, "--config", "/dev/null", "--sftp-ssh", peer_ssh],
                        sftp_arg, run=on_executor, where=f"on {peer.backend.label}",
                        **limits)
        # --copy-links: a stat of a symlink is otherwise an error, not an answer.
        near = RcloneOps([rclone, "--config", "/dev/null", "--copy-links"],
                         home.expand, run=on_executor,
                         where=f"on {executor.backend.label}", **limits)
        up = self.source is executor
        here = (self.source if up else self.dest).path
        there = (self.dest if up else self.source).path
        spec = TransferSpec(
            executor.backend,
            here if up else there, there if up else here,
            up=up, contents=self.contents, operation=self.operation,
            symlinks=self.symlinks, dry_run=self.dry_run,
            progress=self.progress, quiet=self.quiet,
            transfers=self.transfers, checkers=self.checkers, extra=self.extra,
            ops=near,
        )
        spec.resolve(far.is_dir, where=far.where)

        argv = [rclone, spec.operation, "--config", "/dev/null",
                "--sftp-ssh", peer_ssh]
        argv += self._rclone_flags(executor.settings)
        argv += [spec.source_arg(), spec.dest_arg()]

        if not self.quiet:
            where = executor.backend.short(node) if node else "a pool node"
            ui.info(f"direct via {executor.backend.label} ({where}): "
                    f"{spec.describe()}")
            ui.note(f"peer: {peer_host}")
        expect = expectation(spec, near, far)
        # The rclone there hears from this machine every LINK_POLL seconds,
        # and stops itself once it has not for far_silence(); a run resumed
        # after a lost connection waits that out, and the stop's own grace,
        # from the last beat it may have heard (Ride's settle). Its processes
        # there go by a name of their own, so once a master is back on the
        # node it ran on, that node can say it has gone, and the wait ends.
        silence = far_silence(executor.settings)
        grace = executor.settings.int("STOP_TIMEOUT")
        name = f"cluster-transfer-{os.urandom(4).hex()}"
        ran_on = None

        def command():
            nonlocal ran_on
            ran_on = link.node
            return (self._on_executor(executor, link.sock, link.node, agent).prefix
                    + [tied(argv, silence, grace, name)])

        def settled():
            if link.node is None or link.node != ran_on:
                return False
            there = self._on_executor(executor, link.sock, link.node, agent)
            return there(still_there(name), timeout=small).returncode == 1

        ride = Ride([link], executor.settings, env=agent.env(), heartbeat=True,
                    settle=silence + grace + LINK_POLL, silenced=SILENCED,
                    quiet=self.quiet, settled=settled)
        rc = ride.run(command)
        if rc == 0 and not self.dry_run:
            verify(spec, near, far, expect)
        return rc

    def _remote_rclone(self, run, executor, home):
        """An rclone new enough for --sftp-ssh, on the executor."""
        override = executor.settings.str("REMOTE_RCLONE")
        candidates = ([home.expand(override)] if override else
                      ["rclone", "/usr/local/bin/rclone", "/usr/bin/rclone"])
        seen = []
        for candidate in candidates:
            got = run(f"{shlex.quote(candidate)} --config /dev/null version "
                      "2>/dev/null | head -1",
                      timeout=executor.settings.int("REMOTE_COMMAND_TIMEOUT"))
            line = (got.stdout or "").strip()
            version = parse_rclone_version(line)
            if version is None:
                continue
            seen.append(f"{candidate} = {line}")
            if version >= MIN_RCLONE:
                return candidate
        raise DirectUnavailable(
            f"no rclone {version_text(MIN_RCLONE)} or newer on "
            f"{executor.backend.label}",
            "; ".join(seen) or "no rclone found there at all",
            "load a module providing one, then: cluster "
            f"{executor.backend.cli_flag()} config set REMOTE_RCLONE /path/to/rclone",
        )

    # --- relay --------------------------------------------------------------
    def _run_relay(self):
        """rclone here, with one sftp remote per cluster."""
        rclone = Transfers(self.source.logins).rclone_bin()
        opened = []
        config_path = None
        try:
            src_link = self._relay_link(self.source, opened)
            dst_link = self._relay_link(self.dest, opened)
            config_path = _relay_config(
                [("src", self.source, src_link.sock, src_link.node),
                 ("dst", self.dest, dst_link.sock, dst_link.node)])

            # The source cluster's TRANSFER_* settings pace the whole transfer.
            settings = self.source.settings
            limits = probe_limits(settings)
            base = [rclone, "--config", str(config_path)]

            def side(name, endpoint):
                return RcloneOps(base, lambda path: f"{name}:{sftp_path(path)}",
                                 where=f"on {endpoint.backend.label}", **limits)

            src, dst = side("src", self.source), side("dst", self.dest)
            spec = TransferSpec(
                self.source.backend, self.source.path, self.dest.path,
                up=True, contents=self.contents, operation=self.operation,
                symlinks=self.symlinks, dry_run=self.dry_run,
                progress=self.progress, quiet=self.quiet,
                transfers=self.transfers, checkers=self.checkers,
                extra=self.extra, ops=src, remote_fmt=dst.arg,
            )
            spec.resolve(dst.is_dir, where=dst.where)

            argv = base[:1] + [spec.operation] + base[1:]
            argv += self._rclone_flags(settings, shared=bool(self.via))
            argv += [spec.source_arg(), spec.dest_arg()]

            if not self.quiet:
                ui.warn("relay: every byte crosses this machine, twice over the "
                        "wire")
                ui.info(f"relay {self.source} -> {self.dest}"
                        f"{' (dry run)' if self.dry_run else ''}")
            expect = expectation(spec, src, dst)
            rc = Ride([src_link, dst_link], settings).run(lambda: argv)
            if rc == 0 and not self.dry_run:
                verify(spec, src, dst, expect)
            return rc
        finally:
            if config_path is not None:
                config_path.unlink(missing_ok=True)
            used = {tag for _xfer, tag in opened}
            for xfer, tag in opened:
                xfer.lease_drop(tag)
                if not self.keep:
                    xfer.close_connection(tag)
            # An inherited connection relay did not end up using (--via took a
            # different route) would otherwise stay open with nothing tracking it.
            if self._inherited and not self.keep:
                name, tag = self._inherited
                if tag not in used:
                    side = self.source if self.source.name == name else self.dest
                    orphan = Transfers(side.logins)
                    if orphan.is_active(tag):
                        orphan.close_connection(tag)

    def _relay_link(self, endpoint, opened):
        """A live connection to *endpoint*, as a Link, without paying for one
        twice. Restoring it keeps its node, which the relay's rclone config
        names."""
        if self.via and endpoint.name in registry.backends_claiming(self.via):
            via, logins = self.via, endpoint.logins
            logins.ensure(via, quiet=self.quiet)
            node = logins.node_of(via)

            def restore(_lost):
                logins.restore(via)
                return node

            return Link(f"login '{via}' on {endpoint.backend.label}",
                        endpoint.state.socket(via), restore, logins, node)
        xfer = Transfers(endpoint.logins)
        # A connection direct opened before giving up, or any transfer master
        # already up for this cluster. An agent-forwarding master is an ordinary
        # transport as well — forwarding is merely permitted, never required.
        inherited = (self._inherited[1]
                     if self._inherited and self._inherited[0] == endpoint.name
                     else None)
        for tag in ([inherited] if inherited else []) + ["pool-fwd", "pool"]:
            if xfer.reuse(tag):
                opened.append((xfer, tag))
                if not self.quiet and tag == inherited:
                    ui.note(f"reusing the {endpoint.backend.label} connection "
                            "already open, rather than authenticating again")
                return self._transfer_link(xfer, endpoint, tag)
        tag = xfer.open_connection(quiet=self.quiet)
        opened.append((xfer, tag))
        return self._transfer_link(xfer, endpoint, tag)

    def _transfer_link(self, xfer, endpoint, tag):
        # A "-fwd" tag names a master that forwards an agent, which is what
        # a direct transfer reusing it counts on (Transfers.open_connection),
        # so one reopened here forwards too. Nothing, though: the agent it
        # forwarded belonged to the direct run that gave up, and the user's
        # own agent is never forwarded (agentscope). A direct run finds that
        # agent gone and replaces the master, as for any left behind.
        forwarding = tag.endswith("-fwd")
        no_agent = {key: value for key, value in os.environ.items()
                    if key not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}

        def restore(lost):
            xfer.reopen(tag, lost, quiet=self.quiet, forward_agent=forwarding,
                        agent_env=no_agent if forwarding else None)

        return Link(f"transfer connection {tag} to {endpoint.backend.label}",
                    endpoint.state.xfer_socket(tag), restore, endpoint.logins)


def _relay_config(remotes):
    """A throwaway rclone config with one sftp remote per cluster.

    Two remotes need two different ssh commands, and ``--sftp-ssh`` is a single
    global flag, so a config file is the only way to say it. The user's own
    config is untouched: it may be password-encrypted and would then prompt.
    It names two masters, so it is private from the moment it exists.
    """
    here = socket.gethostname() or "localhost"
    _sweep_relay_configs(here)
    path = config.STATE_ROOT / f"relay-{here}-{os.getpid()}.conf"
    lines = []
    for name, endpoint, sock, node in remotes:
        argv = channel_ssh(endpoint.backend, endpoint.settings, sock, node)
        lines += [f"[{name}]", "type = sftp", "shell_type = unix",
                  "ssh = " + rclone_ssh_value(argv), ""]
    plat.atomic_write_text(path, "\n".join(lines), mode=0o600)
    return path


def _sweep_relay_configs(host):
    """Remove the configs of relays on *host* that ended without removing theirs.

    A relay removes its config on the way out, but a terminal closing under it
    (SIGHUP) or a kill (SIGTERM) ends it before it can. Each config names the
    process that wrote it, so one whose process is gone is a leftover. Another
    host's are left alone: its process table is not this one.
    """
    prefix = f"relay-{host}-"
    for old in config.STATE_ROOT.glob(f"{prefix}*.conf"):
        pid = old.name[len(prefix):-len(".conf")]
        if pid.isdigit() and not plat.pid_alive(pid):
            old.unlink(missing_ok=True)
