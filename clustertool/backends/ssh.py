"""The ``ssh`` type: any host ssh reaches, authenticated however ssh does it.

Where the fasrc and nersc types each know one site, this one knows nothing
about the far side beyond what its profile says::

    [lab]
    TYPE = ssh
    HOST = lab-login

HOST is anything ssh takes as a destination: a Host of your own ssh
configuration, which this type reads (SSH_CONFIG, empty by default: ssh's
own), a hostname, or user@host. Authentication is ssh's too: keys, an agent,
certificates, a jump host, whatever the configuration says. The tool types
nothing. A connection someone makes at a terminal may ask them for a
passphrase, a password or a new host key. One made with nobody there (a
reconnect, the watcher, a sweep) runs in BatchMode and fails rather than wait
on a prompt no one can see. A refusal is recorded, and the unattended ones stop
trying until someone connects by hand (state.Refusals).

A login is pinned to the name its node reports (``hostname -f``), which need
not be HOST, and reconnects go through HOST. A HOST that can land on more than
one machine needs a way to each of them, or a reconnect that lands elsewhere is
refused rather than adopted: NODE_HOSTS, as ``NODE=DESTINATION`` pairs. A
command sent to a node over a connection of its own first checks that it is
on that node.

Nothing is taken to be shared between nodes: every login mounts its own home,
and a mount is not moved to another node to repair it.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .. import platform as plat
from ..config import Setting
from ..nodes import LOGIN, MOUNT, TRANSFER, NodeClass
from .base import CRED_OK, Backend, BackendUnavailable, _file_marks


def _destination(text):
    """*text* as an ssh destination; ValueError saying what is wrong."""
    if text.startswith("-"):
        raise ValueError("a destination cannot begin with '-'")
    if any(c.isspace() for c in text):
        raise ValueError("a destination has no spaces")
    return text


def _routes(text):
    """``NODE=DESTINATION ...`` as ``{short node: destination}``; ValueError
    saying which pair is wrong."""
    routes = {}
    for pair in text.split():
        node, sep, destination = pair.partition("=")
        node = node.strip().split(".", 1)[0]
        if not sep or not node or not destination:
            raise ValueError(f"'{pair}' is not NODE=DESTINATION")
        routes[node] = _destination(destination)
    return routes


def _routes_value(text):
    _routes(text)
    return " ".join(text.split())


def _node_guard(node):
    """Shell that ends a command at once, as ssh's own 255, unless it runs on
    *node*: what a destination reached this time is only known once there."""
    short = (node or "").split(".", 1)[0]
    return (f'h=$(hostname -f 2>/dev/null || hostname); '
            f'if [ "${{h%%.*}}" != {shlex.quote(short)} ]; then '
            f'echo "cluster: this is $h, not {shlex.quote(short)}" >&2; '
            f'exit 255; fi; ')


def _hop(text):
    """One ProxyJump hop, ``[ssh://][user@]host[:port]``, as (host, port or
    None)."""
    text = text.strip()
    if text.startswith("ssh://"):
        text = text[len("ssh://"):]
    text = text.rpartition("@")[2]
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, ""
    return host, (int(port) if port.isdigit() else None)


class SshBackend(Backend):
    type_name = "ssh"
    label = "SSH host"

    SETTINGS = dict(
        Backend.SETTINGS,
        HOST=Setting("", "where ssh connects: a Host of your ssh config, a "
                         "hostname, or user@host", check=_destination),
        NODE_HOSTS=Setting("", "NODE=DESTINATION pairs, for a HOST that can "
                               "land on more than one node", check=_routes_value),
        # The type's defaults for shared settings (config.lookup): ssh's own
        # configuration, and nothing assumed shared between nodes.
        SSH_CONFIG=Setting("", "OpenSSH config file (empty: ssh's own, "
                               "~/.ssh/config)"),
        AUTO_MOUNT=Setting(0, "automatically mount before interactive work",
                           flag=True),
        ONE_MOUNT_PER_BACKEND=Setting(0, "share one home mount across backend "
                                         "logins", flag=True),
        MOUNT_FAILOVER=Setting(0, "move only the mount after repeated repair "
                                  "failures", flag=True),
        REAPS_ON_LOGOUT=Setting(0, "the host ends your processes when your last "
                                   "session on it ends, so tmux needs linger",
                                flag=True),
    )

    #: ssh asks what it asks, on the terminal when there is one (a person
    #: connecting by hand), and in BatchMode nobody is asked.
    interactive_auth = False
    records_refusals = True
    CREDENTIALS = ()
    enroll_settings = ("HOST",)
    enroll_hint = (
        "HOST is where ssh connects: a Host of your ssh config, a hostname, "
        "or user@host. ssh authenticates as your ssh configuration says, so "
        "nothing secret is saved here.",
    )
    login_cost = "and may ask what ssh asks: a passphrase, a password or a new host key"

    def __init__(self, settings):
        super().__init__(settings)
        host = settings.str("HOST")
        if not host:
            raise BackendUnavailable(
                self.name, f"backend '{self.name}' has no HOST",
                f"set it with: {self.setup_command()}")
        user, _, bare = host.rpartition("@")
        self.user, self.pool_host = user, bare
        self.label = settings.str("LABEL") or host
        self.routes = _routes(settings.str("NODE_HOSTS"))
        # A type built on this one may know its host reaps; the setting can
        # only say so.
        if settings.flag("REAPS_ON_LOGOUT"):
            self.reaps_on_logout = True
        self._resolved = {}

    # --- configuration --------------------------------------------------------
    @classmethod
    def is_configured(cls, settings):
        return bool(settings.str("HOST"))

    @classmethod
    def local_username(cls, settings):
        return settings.str("HOST").rpartition("@")[0]

    @classmethod
    def setup_command(cls):
        return f"cluster {cls.cli_flag()} config set HOST DESTINATION"

    def credentials_command(self):
        return f"ssh {self.target()}"

    @classmethod
    def configured_node_classes(cls, settings):
        """One class of the nodes NODES or NODE_HOSTS names; none when
        neither does, since only a connection tells where HOST lands."""
        try:
            routes = _routes(settings.str("NODE_HOSTS"))
        except ValueError:
            routes = {}
        names = (settings._raw("NODES") or "").split() or list(routes)
        if not names:
            return ()
        return (NodeClass(
            name="host", routable=True,
            purposes=frozenset({LOGIN, TRANSFER, MOUNT}), hosts=tuple(names),
            note="reached through NODE_HOSTS" if routes else "set by NODES"),)

    def credential_state(self):
        return CRED_OK, "ssh's own authentication"

    def credential_marks(self):
        """What a refusal on record is a refusal of: where ssh connects, and
        the configuration file that says how (a stat, never a read). A change
        to either lets the unattended connections try again. Only a regular
        file counts: /dev/null's times say nothing."""
        path = Path(self.settings.str("SSH_CONFIG") or "~/.ssh/config").expanduser()
        marks = [["HOST", self.settings.str("HOST")]]
        if path.is_file():
            marks += _file_marks(path.parent, [path.name]) or []
        return marks

    # --- ssh ------------------------------------------------------------------
    def _config_file(self):
        """["-F", SSH_CONFIG], or [] for ssh's own. ssh does not expand a
        ``~`` in -F, so it is expanded here."""
        path = self.settings.str("SSH_CONFIG")
        return ["-F", os.path.expanduser(path)] if path else []

    def common_opts(self, forward_agent=False):
        # Everything the tool does not need to say is left to ssh's own
        # configuration: the username, the host key policy, the identity.
        # What it does say comes first, so it wins (ssh takes the first value).
        opts = self._config_file()
        if self.user:
            opts += ["-o", f"User={self.user}"]
        if not self.by_hand:
            opts += ["-o", "BatchMode=yes"]
        return opts + [
            "-o", f"ForwardAgent={'yes' if forward_agent else 'no'}",
            "-o", "ForwardX11=no",
            "-o", f"ServerAliveInterval={self.settings.int('SSH_SERVER_ALIVE_INTERVAL')}",
            "-o", f"ServerAliveCountMax={self.settings.int('SSH_SERVER_ALIVE_COUNT_MAX')}",
            "-o", f"ConnectTimeout={self.settings.int('CONNECT_TIMEOUT')}",
        ]

    def host_for(self, node=None):
        """NODE_HOSTS's way to *node*, else HOST. A node's own name is never
        dialled: it is what the node calls itself, not something ssh's
        configuration knows (a Host alias, a jump, a port)."""
        if node:
            route = self.routes.get(self.short(node))
            if route:
                return route
        return self.pool_host

    def ssh_argv(self, node=None, sock=None, master=False, extra=(), remote=None,
                 persist=None, forward_agent=False):
        if node and remote is not None:
            remote = _node_guard(node) + remote
        return super().ssh_argv(node=node, sock=sock, master=master, extra=extra,
                                remote=remote, persist=persist,
                                forward_agent=forward_agent)

    def pin_hint(self, pinned, landed):
        short = self.short(pinned)
        if short in self.routes:
            return [f"{self.routes[short]} (NODE_HOSTS' way to {short}) reached "
                    f"{self.short(landed)}: check that entry",
                    "or repin deliberately if the sessions really moved"]
        return [f"{self.pool_host} is more than one machine; say how to reach "
                f"{short} itself: cluster {self.cli_flag()} config set "
                f"NODE_HOSTS '{short}=DESTINATION'",
                "or repin deliberately if the sessions really moved"]

    # --- reachability ---------------------------------------------------------
    def _config_of(self, destination):
        """What ssh's configuration makes of *destination* (`ssh -G`, which
        connects nowhere): ``{option: value}``, or {} when ssh cannot say."""
        if destination not in self._resolved:
            argv = ["ssh", *self._config_file(), "-G", destination]
            proc = plat.run(argv, timeout=10, capture=True)
            options = {}
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    option, _, value = line.partition(" ")
                    options.setdefault(option.lower(), value.strip())
            self._resolved[destination] = options
        return self._resolved[destination]

    def _dialled(self, destination, through_jumps=False, depth=0):
        """(host, port) ssh dials first for *destination*, or None when only
        a connection can tell: a ProxyCommand, or with *through_jumps* false,
        a ProxyJump. Through jumps it is the first jump host's."""
        options = self._config_of(destination)
        if options.get("proxycommand", "none").lower() != "none":
            return None
        jump = options.get("proxyjump", "none")
        if jump.lower() != "none":
            if not through_jumps or depth >= 8:
                return None
            name, port = _hop(jump.split(",")[0])
            first = self._dialled(name, through_jumps, depth + 1)
            return (first[0], port) if first and port else first
        try:
            port = int(options.get("port", 22))
        except ValueError:
            port = 22
        return options.get("hostname") or destination, port

    def node_probe_host(self, node):
        return self._dialled(self.host_for(node))

    def reach_host(self):
        return self._dialled(self.pool_host, through_jumps=True)

    def describe(self):
        return f"{self.label} ({self.name}) via {self.target()}"
