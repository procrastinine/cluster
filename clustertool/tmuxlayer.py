"""Remote tmux sessions.

tmux sessions are **node-local**, which is the reason logins are pinned at all.
Three mechanisms keep the catalogue honest:

*Ownership* — a session created here is tagged with the login that owns it in the
tmux user option ``@cluster_login``, so a sweep can tell your work from someone
else's. Untagged sessions fall back to owner = session name.

*Breadcrumbs* — every session is also recorded as a file on the cluster's shared
home (``~/.cluster/sessions/<node>/<session>``). Home is shared across the whole
pool on both clusters, so one connection can discover sessions on nodes it is not
connected to. Breadcrumbs are written on create, removed on kill, and reconciled
on every sweep, so the catalogue self-heals in both directions.

*Layout snapshots* — the watcher periodically records each node's
sessions/windows/cwds under ``~/.cluster/layout/<node>``, which is what lets
``restore-layout`` rebuild a workspace after a node reboot.
"""

from __future__ import annotations

import collections
import re
import shlex

from . import config, linger, strays, ui
from .sshmux import COMMAND_TIMEOUT
from .remote_sh import (EACH_TARGET, for_each_session, home_path, quote_opt,
                        split_target, tmux_target)

CRUMB_ROOT = "~/.cluster/sessions"
LAYOUT_ROOT = "~/.cluster/layout"
OWNER_OPTION = "@cluster_login"

#: tmux user options set by *other* tools that own sessions the same way. A
#: session carrying one of these is somebody else's live work and is never
#: swept (short of `clean --force`). None by default; list them, space
#: separated: cluster config set FOREIGN_OWNER_OPTIONS '@owner @agent'.
FOREIGN_OWNER_OPTIONS = tuple(
    config.global_value("FOREIGN_OWNER_OPTIONS",
                        config.GLOBAL_DEFAULTS["FOREIGN_OWNER_OPTIONS"]).split()
)
_warned_options = set()


class Session(collections.namedtuple(
        "Session", "name windows attached owner tagged foreign created",
        defaults=(False, "", ""))):
    """One tmux session as listed.

    *tagged* is True when an explicit owner tag was found (as opposed to
    *owner* defaulting to the session name); *foreign* is non-empty when
    another tool claims the session.
    """

    __slots__ = ()


Window = collections.namedtuple("Window", "session index name panes active")
Client = collections.namedtuple("Client", "name session tty")
#: Lists of Session, Window and Client, and the node's ssh client lines.
RemoteDetails = collections.namedtuple("RemoteDetails",
                                       "sessions windows clients ssh_clients")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9._-]+)?(\.[0-9]+)?$")


def valid_name(name):
    return bool(name) and bool(NAME_RE.match(name))


def require_name(name, what="name"):
    # A leading dash must never become a name: a flag typed in the wrong place
    # would otherwise create a login that close() cannot parse.
    if not valid_name(name):
        ui.die(f"invalid {what}: {name!r}",
               "names must start alphanumeric and hold only letters, digits, . _ -")
    return name


def require_target(target):
    if not target or not TARGET_RE.match(target):
        ui.die(f"invalid tmux target: {target!r}",
               "expected SESSION, SESSION:WINDOW or SESSION:WINDOW.PANE")
    return target


def _foreign_options():
    """FOREIGN_OWNER_OPTIONS that tmux could hold, each said once if not."""
    names = []
    for option in FOREIGN_OWNER_OPTIONS:
        if option == OWNER_OPTION:
            continue
        try:
            names.append(quote_opt(option))
        except ValueError:
            if option not in _warned_options:
                _warned_options.add(option)
                ui.warn(f"FOREIGN_OWNER_OPTIONS: {option!r} is not a tmux "
                        "option name, so no session can carry it; ignored")
    return names


def list_sessions_snippet():
    """Remote shell that lists sessions with their ownership tags.

    Ownership is read with ``show-options`` rather than a ``#{@option}`` format
    string: user options in formats need tmux 3.0, and FASRC runs 2.7, where the
    format silently expands to nothing and makes every session look unowned —
    which would turn a sweep into data loss.

    Shared by the over-a-login path and the direct-to-a-node path so both see
    identical ownership information.
    """
    foreign_reads = "".join(
        f'f="$f$(tmux show-options -qv -t {EACH_TARGET} {option} 2>/dev/null)"; '
        for option in _foreign_options())
    return (
        "tmux list-sessions -F "
        "'#{session_name}\t#{session_windows}\t#{?session_attached,1,0}' "
        "2>/dev/null | while IFS='\t' read -r s w a; do "
        f"o=$(tmux show-options -qv -t {EACH_TARGET} {OWNER_OPTION} 2>/dev/null); "
        'f=""; ' + foreign_reads +
        f'c=$(tmux display-message -p -t {EACH_TARGET} "#{{t:session_created}}" '
        "2>/dev/null); "
        'printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n" "$s" "$w" "$a" "$o" "$f" "$c"; '
        "done 2>/dev/null || true"
    )


#: Marks where our own output starts. A shell rc file may print before the
#: command runs — FASRC's prints a Slurm banner — so reading the *last* line
#: stops working as soon as the reply has more than one line. Everything before
#: the first marker is noise by construction, because it was written before our
#: command existed.
LS_MARKER = "__cluster_ls__"
#: Section and step markers for the batched `attach` round trips. Distinct
#: strings, so a reply that arrives truncated or interleaved cannot have one
#: section read as another.
SESSIONS_MARKER = "__cluster_sessions__"
CRUMBS_MARKER = "__cluster_crumbs__"
STEP_MARKER = "__cluster_step__"
WINDOWS_MARKER = "__cluster_windows__"
CLIENTS_MARKER = "__cluster_clients__"
WHO_MARKER = "__cluster_who__"
LAYOUTS_MARKER = "__cluster_layouts__"


def _after_marker(text, marker):
    """The lines after the first one that is *marker*, or None if none is."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == marker:
            return lines[index + 1:]
    return None


def details_snippet(include_who=True):
    """One remote read for sessions, windows, tmux clients and SSH clients."""
    parts = [
        f'printf "%s\\n" {SESSIONS_MARKER}',
        list_sessions_snippet(),
        f'printf "%s\\n" {WINDOWS_MARKER}',
        "tmux list-windows -a -F "
        "'#{session_name}\t#{window_index}\t#{window_name}\t#{window_panes}\t#{window_active}' "
        "2>/dev/null || true",
        f'printf "%s\\n" {CLIENTS_MARKER}',
        "tmux list-clients -F "
        "'#{client_name}\t#{client_session}\t#{client_tty}' "
        "2>/dev/null || true",
    ]
    if include_who:
        parts += [
            f'printf "%s\\n" {WHO_MARKER}',
            "who -u 2>/dev/null | awk -v u=\"$USER\" '$1 == u' || true",
        ]
    return "; ".join(parts)


def _detail_sections(text):
    markers = (SESSIONS_MARKER, WINDOWS_MARKER, CLIENTS_MARKER, WHO_MARKER)
    sections = {marker: [] for marker in markers}
    current = None
    for line in (text or "").splitlines():
        if line.strip() in sections:
            current = line.strip()
        elif current:
            sections[current].append(line)
    return sections


def parse_details(text):
    """Parse :func:`details_snippet`, treating missing sections as empty."""
    sections = _detail_sections(text)
    windows = []
    for line in sections[WINDOWS_MARKER]:
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0]:
            windows.append(Window(parts[0], parts[1], parts[2], parts[3],
                                  parts[4].strip() == "1"))
    clients = []
    for line in sections[CLIENTS_MARKER]:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            clients.append(Client(parts[0], parts[1], parts[2]))
    return RemoteDetails(
        sessions=parse_sessions("\n".join(sections[SESSIONS_MARKER])),
        windows=windows,
        clients=clients,
        ssh_clients=[line for line in sections[WHO_MARKER] if line.strip()],
    )


def explain_remote_failure(proc):
    """One line saying why a fresh connection produced no catalogue.

    Deliberately pattern-matched on what ssh actually prints rather than on
    exit status alone: ssh reports almost everything as 255, and the useful
    distinction (refused to connect / refused the credential / refused to start
    a session) lives in the text.
    """
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
    for needle, reason in (
            ("permission denied", "the credential was refused"),
            ("connection refused", "nothing is listening on port 22"),
            ("connection closed", "it closed the connection during setup"),
            ("connection reset", "it reset the connection"),
            ("no route to host", "it is not routable from here"),
            ("name or service not known", "its name does not resolve"),
            ("could not resolve", "its name does not resolve"),
            ("timed out", "the connection timed out"),
    ):
        if needle in text:
            return reason
    if proc.returncode == 0:
        return "the reply did not contain our marker, so it was not our output"
    return (f"it accepted the login and then refused to start a session "
            f"(exit {proc.returncode}) — anything already running there is "
            f"unaffected, but nothing new can be started")


def node_and_sessions_snippet(with_sessions=True, with_crumbs=False):
    """One remote command answering both questions `ls` asks of a node.

    An SSH channel costs the same whatever it carries: measured on both
    clusters, a bare ``true`` and this whole listing each take ~0.85s, because
    the time goes to sshd's per-session setup and not to the work. Asking for
    the hostname and the sessions separately would double the cost of `ls` for
    nothing.

    The hostname is always exactly one line, even when ``hostname`` fails, so
    the reply splits without having to guess where the sessions begin.
    ``with_sessions=False`` (``ls -q``) leaves tmux out of it entirely rather
    than asking and discarding: it costs the same, but it also cannot block on a
    sick tmux server.

    ``with_crumbs`` adds the breadcrumb catalogue to the same reply, which is
    what lets `ls` report strays without a second channel or a credential. It
    stays on under ``-q``: crumbs are a read of the shared home, not a question
    for tmux, so `-q`'s "cannot block on a sick tmux server" property survives.
    """
    parts = [f'printf "%s\\n%s\\n" {LS_MARKER} "$(hostname -f 2>/dev/null)"']
    if with_sessions:
        parts.append(list_sessions_snippet())
    if with_crumbs:
        parts.append(f'printf "%s\\n" {CRUMBS_MARKER}')
        parts.append(crumbs_snippet())
    return "; ".join(parts)


def parse_node_sessions_and_crumbs(text):
    """``(node, [Session], {(node, session): owner})`` from that reply.

    A reply with no marker means the command never ran, so the node comes back
    unknown rather than guessed from whatever text did arrive. A crumbs section
    that never arrived is likewise absent rather than empty: `ls` must not
    report every recorded session as vanished because one read failed.
    """
    lines = _after_marker(text, LS_MARKER)
    if lines is None:
        return "", [], None
    node = lines[0].strip() if lines else ""
    rest = lines[1:]
    for index, line in enumerate(rest):
        if line.strip() == CRUMBS_MARKER:
            return (node, parse_sessions("\n".join(rest[:index])),
                    parse_crumbs("\n".join(rest[index + 1:])))
    return node, parse_sessions("\n".join(rest)), None


def parse_node_and_sessions(text):
    """``(node, [Session])`` from :func:`node_and_sessions_snippet`."""
    node, sessions, _crumbs = parse_node_sessions_and_crumbs(text)
    return node, sessions


def layout_names_snippet():
    """Remote shell naming the nodes with a layout snapshot in the shared home."""
    return (f"for f in {LAYOUT_ROOT}/*; do [ -f \"$f\" ] || continue; "
            'basename "$f"; done 2>/dev/null || true')


def status_snippet():
    """:func:`node_and_sessions_snippet` with the crumbs, then the layout names.

    Everything `status` asks of its first live login: that login's node and
    sessions, and what the shared home holds, which any login would answer
    the same.
    """
    return (node_and_sessions_snippet(with_crumbs=True)
            + f'; printf "%s\\n" {LAYOUTS_MARKER}; ' + layout_names_snippet())


def parse_status(text):
    """``(node, [Session], crumbs_or_None, [layout node])`` from that reply.

    The part before the layout marker reads as
    :func:`parse_node_sessions_and_crumbs` reads it. Layout names that never
    arrived are none.
    """
    lines = (text or "").splitlines()
    marks = [index for index, line in enumerate(lines)
             if line.strip() == LAYOUTS_MARKER]
    split = marks[-1] if marks else len(lines)
    node, sessions, crumbs = parse_node_sessions_and_crumbs(
        "\n".join(lines[:split]))
    return node, sessions, crumbs, sorted(set(lines[split + 1:]))


def ensure_server_snippet(session):
    """Remote shell that guarantees *session* exists, reporting whether it does.

    ``new-session -A -d`` must be the first tmux command to touch a server-less
    node: on tmux 2.7, which FASRC runs, a *query* first starts a server that then
    exits, and the follow-up new-session attaches to the corpse ("lost server").

    The ``has-session`` fallback is what makes the exit status mean "the session
    exists" rather than "the session was new". On tmux 2.7 the ``-A`` path for an
    *already existing* session tries to attach even under ``-d``, and so fails
    with `open terminal failed: not a terminal` and status 1 — measured on
    boslogin08. That is the common case, so without the fallback the command
    would report failure almost every time it succeeds. The fallback runs only
    after new-session, so a server-less node still gets the create first and the
    ordering above is preserved.
    """
    # $S is set by clustertool.linger.SCOPE_SETUP, which every caller of this
    # snippet prepends; unset it expands to nothing and this is plain tmux.
    # Only the command that may *start* the server needs it — a session
    # created on a server that already exists is the server's child and
    # inherits whichever scope the server itself was started in.
    return (f"$S tmux new-session -A -d -s {shlex.quote(session)} >/dev/null 2>&1 || "
            f"tmux has-session -t {tmux_target(session)} >/dev/null 2>&1")


def crumbs_snippet():
    """Remote shell listing every breadcrumb in the shared home.

    Module level and shared, so the batched reader and :meth:`Tmux.crumbs`
    cannot drift apart.
    """
    return (
        f"for d in {CRUMB_ROOT}/*/; do "
        '[ -d "$d" ] || continue; n=$(basename "$d"); '
        'for f in "$d"*; do [ -f "$f" ] || continue; '
        'printf "%s\\t%s\\t%s\\n" "$n" "$(basename "$f")" "$(cat "$f" 2>/dev/null)"; '
        "done; done 2>/dev/null || true"
    )


def parse_crumbs(text):
    """``{(node_short, session): owning_login}`` from :func:`crumbs_snippet`."""
    result = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[1]:
            result[(parts[0], parts[1])] = parts[2] if len(parts) > 2 else ""
    return result


def sessions_and_crumbs_snippet():
    """One remote command answering both of `attach`'s questions.

    Two markers rather than one, because both payloads are tab-separated line
    lists and neither has a fixed length, so there is nothing else to split on.
    A section whose marker never arrives is reported as absent rather than empty
    — see :func:`parse_sessions_and_crumbs`.
    """
    return (
        f'printf "%s\\n" {SESSIONS_MARKER}; '
        + list_sessions_snippet() + "; "
        + f'printf "%s\\n" {CRUMBS_MARKER}; '
        + crumbs_snippet()
    )


def parse_sessions_and_crumbs(text):
    """``([Session], {(node, session): owner})`` from the batched read.

    A missing marker means that half never ran, and it comes back empty, as in
    :func:`parse_node_and_sessions`. Callers that must tell "none" from "could
    not ask" — for sessions, "none" makes `attach` create one — use
    :meth:`Tmux.sessions_and_crumbs_checked`, which requires both markers.
    """
    lines = _after_marker(text, SESSIONS_MARKER)
    if lines is None:
        return [], {}
    try:
        split = [line.strip() for line in lines].index(CRUMBS_MARKER)
    except ValueError:
        return parse_sessions("\n".join(lines)), {}
    return (parse_sessions("\n".join(lines[:split])),
            parse_crumbs("\n".join(lines[split + 1:])))


def register_session_snippet(login, session, owner=None, node=None):
    """One remote command that creates a session, tags it, and records it.

    ORDER IS LOAD-BEARING AND MUST NOT BE REARRANGED. ``new-session -A -d`` has
    to be the first tmux command to touch a server-less node: on tmux 2.7, which
    FASRC runs, a *query* first starts a server that then exits, and the
    follow-up new-session attaches to the corpse ("lost server"). Batching these
    three is safe only while the create stays in front — which is exactly the
    kind of constraint a single blob invites someone to break, so it is written
    here beside the code rather than only in the method's docstring.

    Each step prints its own exit status, so one failure among three is still
    identifiable; see :func:`record_session_snippet` for the last two.
    """
    return (
        f"{ensure_server_snippet(session)}; "
        f'printf "%s\\t%s\\t%s\\n" {STEP_MARKER} server $?; '
        + record_session_snippet(login, session, owner=owner, node=node)
    )


def record_session_snippet(login, session, owner=None, node=None):
    """Remote shell that tags an existing session's owner and records its crumb.

    Each step prints its own exit status (``owner``, then ``crumb``). The owner
    tag deliberately re-reads before writing and never overwrites an existing
    one; a skipped write is success, not failure, because a shell ``if`` with
    no ``else`` exits 0 when its condition is false. *node* names the node for
    the breadcrumb path; left None, the remote works it out from its hostname.
    """
    owner = owner or login
    sess = shlex.quote(session)
    target = tmux_target(session)
    root = CRUMB_ROOT
    if node:
        where = f"n={shlex.quote(node)}"
    else:
        # ${n%%.*} is backend.short() in shell: the first DNS label.
        where = 'n=$(hostname -f 2>/dev/null); n=${n%%.*}'
    return (
        f"cur=$(tmux show-options -qv -t {target} {OWNER_OPTION} 2>/dev/null); "
        f'if [ -z "$cur" ]; then '
        f"tmux set-option -t {target} {OWNER_OPTION} {shlex.quote(owner)} >/dev/null 2>&1; "
        f"fi; "
        f'printf "%s\\t%s\\t%s\\n" {STEP_MARKER} owner $?; '
        f"{where}; "
        f'if [ -n "$n" ]; then '
        f'mkdir -p {root}/"$n" && printf "%s\\n" {shlex.quote(login)} '
        f'> {root}/"$n"/{sess}; '
        f"else false; fi; "
        f'printf "%s\\t%s\\t%s\\n" {STEP_MARKER} crumb $?'
    )


#: What each step of registering a session means when it fails.
STEP_COMPLAINTS = {"server": "could not create or reach the tmux session",
                   "owner": "could not tag session ownership",
                   "crumb": "could not record the session breadcrumb"}

#: Exit statuses that say the answer was lost, not what the command did: a
#: timeout (plat.run's 124), or ssh's own failure (255).
ANSWER_LOST = (124, 255)


def parse_register_steps(text):
    """``{step: ok}`` from :func:`register_session_snippet`.

    A step whose line never arrived is reported failed rather than missing: the
    caller's question is "did this get recorded", and no news is not good news.
    """
    steps = {"server": False, "owner": False, "crumb": False}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == STEP_MARKER and parts[1] in steps:
            steps[parts[1]] = parts[2].strip() == "0"
    return steps


def parse_sessions(text):
    """Parse the output of :func:`list_sessions_snippet`."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        owner = parts[3].strip() if len(parts) > 3 else ""
        foreign = parts[4].strip() if len(parts) > 4 else ""
        rows.append(Session(
            name=parts[0],
            windows=parts[1],
            attached=parts[2] == "1",
            # An untagged session defaults its owner to its own name, but
            # `tagged` records that no tag was actually found — the difference
            # decides whether a sweep may touch it.
            owner=owner or parts[0],
            tagged=bool(owner),
            foreign=foreign,
            created=parts[5].strip() if len(parts) > 5 else "",
        ))
    return rows


class Tmux:
    def __init__(self, logins):
        self.logins = logins
        self.backend = logins.backend
        self.state = logins.state
        #: Said once per process: a server in the wrong cgroup is a fact
        #: about the node, and `restore-layout` creates in a loop.
        self._warned_unscoped = False
        #: Strays seen by the last crumb_sync, for callers that already paid
        #: for that read — the watcher logs them when the set changes.
        self.last_strays = []

    # --- low level ----------------------------------------------------------
    def _linger(self):
        """Prefix asserting that this node keeps processes past the last client.

        Creating a session is the moment the protection starts mattering, and
        these calls are already paying for a round trip, so it rides along free
        rather than being a call of its own. See :mod:`clustertool.linger`.
        """
        return linger.prefix(self.logins) + linger.scope_setup(self.logins)

    def _create_timeout(self):
        """A create's budget: a small command, and the node-side steps
        _linger() adds, each bounded there by timeout(1) (linger.bound)."""
        return self.logins.command_timeout() + linger.bound(self.logins)

    def _warn_if_unscoped(self, output):
        """Say so the moment a server lands where it will not survive.

        Once per process, not once per session: `restore-layout` creates in a
        loop and the fact is about the node, not the session. Not fatal — the
        session exists and is perfectly usable, it just will not outlive the
        connection, and saying that is more use than refusing to make it.
        """
        if linger.scope_of(output) != "session" or self._warned_unscoped:
            return
        self._warned_unscoped = True
        ui.warn("this node's tmux server is in a session scope, so its "
                "sessions will not survive losing this connection")
        ui.note("it was started outside a user scope (by a plain tmux over "
                "ssh, for example) or without a usable user manager; cluster "
                "doctor explains, and recreating the sessions is the repair")

    def settle_node(self, node, runner=None, timeout=COMMAND_TIMEOUT):
        """Leave *node* keeping processes, or not, according to what is on it.

        The counterpart to :meth:`_linger`. :data:`clustertool.linger.SETTLE`
        is the single meaning of "we are done with this node": close, repin,
        unpin, refresh, `clean` and `strays` all end with it, so none of them
        has to work out for itself what finishing with a node entails.

        Safe to run unconditionally, which is the point — the decision is made
        on the node, in the same command that acts on it. A node still running
        tmux keeps everything, whether that tmux is ours, another tool's, or a
        session this sweep deliberately spared.

        Over *runner*'s master when a login is already on the node, and
        directly otherwise. A caller that is already sending the node a command
        appends :meth:`_then_settle` to it instead, which costs nothing extra;
        this is for the one that is not.
        """
        if not linger.required(self.logins):
            return True
        if runner:
            return self._run(runner, linger.SETTLE,
                             timeout=timeout).returncode == 0
        proc = self._direct_run(node, linger.SETTLE, timeout=timeout)
        return bool(proc) and proc.returncode == 0

    def _then_settle(self, settle=True):
        """Shell that settles the node after the command it follows, or "".

        Silent, so the marker-framed reply in front of it is left alone, and
        always successful, so it cannot turn that reply into a failure.
        """
        if not settle or not linger.required(self.logins):
            return ""
        return f"; {{ {linger.SETTLE}; }} >/dev/null 2>&1"

    # COMMAND_TIMEOUT is left for Logins to resolve (REMOTE_COMMAND_TIMEOUT).
    def _sh(self, login, snippet, timeout=COMMAND_TIMEOUT):
        if timeout is COMMAND_TIMEOUT:
            return self.logins.remote_value(login, snippet)
        return self.logins.remote_value(login, snippet, timeout=timeout)

    def _run(self, login, snippet, timeout=COMMAND_TIMEOUT, capture=True):
        if timeout is COMMAND_TIMEOUT:
            return self.logins.run_remote(login, snippet, capture=capture)
        return self.logins.run_remote(login, snippet, timeout=timeout, capture=capture)

    def _run_printing(self, login, snippet):
        """*snippet*, whose work grows with the sessions and breadcrumbs it
        finds and which prints as it goes: stopped only after
        REMOTE_COMMAND_TIMEOUT of silence, never for how long it runs."""
        return self.logins.run_remote(login, snippet, timeout=None,
                                      idle=self.logins.command_timeout())

    def _direct_run(self, node, snippet, timeout=COMMAND_TIMEOUT):
        """Run on a node without a managed master, authenticating normally
        (Logins.authenticate_directly), within REMOTE_COMMAND_TIMEOUT plus
        CONNECT_TIMEOUT for being let in.

        None when nothing could be sent: no TOTP window, or a refusal on
        record holding this process, as logins.last_failure says."""
        if timeout is COMMAND_TIMEOUT:
            timeout = self.logins.command_timeout(own_connection=True)
        self.backend.ensure_credential(quiet=True)
        extra = ["-o", "ControlMaster=no", "-o", "ControlPath=none",
                 "-o", "ControlPersist=no"]
        if not self.backend.interactive_auth:
            extra += ["-o", "BatchMode=yes"]
        argv = self.backend.ssh_argv(node=node, extra=extra, remote=snippet)
        return self.logins.authenticate_directly(
            self.backend.short(node),
            lambda: self.backend.run_ssh(argv, timeout=timeout, capture=True,
                                         quiet=True))

    def ensure_server(self, login, session):
        """Make sure *session* exists, as the first tmux command on a node.

        True means the session exists now, whether this call created it or found
        it. See :func:`ensure_server_snippet` for the ordering constraint and for
        why "already there" needs an explicit fallback to count as success.
        """
        require_name(session, "session name")
        if self._run(login, self._linger() + ensure_server_snippet(session)).returncode != 0:
            return False
        self.state.note_sessions(login, add=[session])
        return True

    def list_sessions(self, login, timeout=COMMAND_TIMEOUT):
        """[Session(...)] on the login's node."""
        return parse_sessions(self._sh(login, list_sessions_snippet(), timeout=timeout))

    @staticmethod
    def _checked_payload(proc, *markers):
        """``(complete, stdout)`` for a marker-framed remote catalogue read.

        An empty catalogue is a valid and important answer, and a failed SSH
        command produces the same empty string, which would let close/clean
        confuse "could not ask" with "nothing exists". Requiring both a zero
        transport status and every expected marker keeps those paths fail-safe.
        """
        text = proc.stdout or ""
        lines = {line.strip() for line in text.splitlines()}
        return proc.returncode == 0 and all(marker in lines for marker in markers), text

    @staticmethod
    def _listing_snippet(before=""):
        """*before*, then the marker-framed session list that confirms it."""
        return (f"{before}; " if before else "") + (
            f'printf "%s\\n" {LS_MARKER}; ' + list_sessions_snippet())

    def _listed(self, proc):
        """``(complete, sessions)`` from a reply to :meth:`_listing_snippet`."""
        complete, text = self._checked_payload(proc, LS_MARKER)
        if not complete:
            return False, []
        return True, parse_sessions("\n".join(_after_marker(text, LS_MARKER)))

    def list_sessions_checked(self, login, timeout=COMMAND_TIMEOUT, settle=False):
        """``(catalogue_complete, sessions)`` on a managed login's node.

        *settle* settles the node in the same command, after the read.
        """
        snippet = self._listing_snippet() + self._then_settle(settle)
        return self._listed(self._run(login, snippet, timeout=timeout))

    def list_sessions_direct_checked(self, node, timeout=COMMAND_TIMEOUT, settle=False):
        """A marker-checked catalogue using a fresh direct node connection."""
        complete, rows, _why = self.list_sessions_direct_explained(
            node, timeout, settle=settle)
        return complete, rows

    def list_sessions_direct_explained(self, node, timeout=COMMAND_TIMEOUT, settle=False):
        """``(complete, sessions, reason)`` — and *why*, when it did not answer.

        "could not be reached" covers three very different facts: the node is
        gone, the credential was refused, or (observed on boslogin08) it
        authenticates fine and then refuses to start a session, while every
        already-open channel on it keeps working. Only the third means "leave
        the records alone and repin instead", so the caller has to be able to
        tell them apart.
        """
        snippet = self._listing_snippet() + self._then_settle(settle)
        proc = self._direct_run(node, snippet, timeout=timeout)
        if proc is None:
            return False, [], self.logins.last_failure
        complete, rows = self._listed(proc)
        if not complete:
            return False, [], explain_remote_failure(proc)
        return True, rows, ""

    def owned_sessions_direct_checked(self, node, owner, timeout=COMMAND_TIMEOUT):
        complete, rows = self.list_sessions_direct_checked(node, timeout=timeout)
        return complete, [row.name for row in rows
                          if row.owner == owner and not row.foreign]

    def details_checked(self, login, include_who=True, timeout=COMMAND_TIMEOUT):
        """``(snapshot_complete, details)`` for user-facing diagnostics."""
        proc = self._run(login, details_snippet(include_who=include_who),
                         timeout=timeout)
        markers = [SESSIONS_MARKER, WINDOWS_MARKER, CLIENTS_MARKER]
        if include_who:
            markers.append(WHO_MARKER)
        complete, text = self._checked_payload(proc, *markers)
        return complete, parse_details(text) if complete else RemoteDetails([], [], [], [])

    def node_and_details_checked(self, login, timeout=COMMAND_TIMEOUT):
        """``(node, snapshot_complete, details)``: what `where` shows, in one
        round trip.

        The node's name comes first, on the line after a marker as in
        :func:`node_and_sessions_snippet`, so it is read even from a reply
        whose details are incomplete; it is "" when it did not arrive.
        """
        proc = self._run(
            login, f'printf "%s\\n%s\\n" {LS_MARKER} "$(hostname -f 2>/dev/null)"; '
            + details_snippet(include_who=True), timeout=timeout)
        after = _after_marker(proc.stdout if proc.returncode == 0 else "", LS_MARKER)
        node = after[0].strip() if after else ""
        complete, text = self._checked_payload(
            proc, SESSIONS_MARKER, WINDOWS_MARKER, CLIENTS_MARKER, WHO_MARKER)
        return (node, complete,
                parse_details(text) if complete else RemoteDetails([], [], [], []))

    def node_and_sessions(self, login, with_sessions=True, timeout=COMMAND_TIMEOUT):
        """``(node, [Session])`` on the login's node, in one round trip.

        What `ls` uses instead of :meth:`list_sessions` plus
        :meth:`Logins.live_node`; see :func:`node_and_sessions_snippet` for why
        the two questions travel together.
        """
        node, sessions, _crumbs = self.node_sessions_and_crumbs(
            login, with_sessions=with_sessions, with_crumbs=False,
            timeout=timeout)
        return node, sessions

    def node_sessions_and_crumbs(self, login, with_sessions=True,
                                 with_crumbs=True, timeout=COMMAND_TIMEOUT):
        """``(node, [Session], crumbs_or_None)`` in one round trip.

        The crumbs ride along free — same channel, same sshd session setup,
        which is where the time actually goes — so `ls` can name the sessions
        recorded on nodes it is not visiting without costing anything. ``None``
        means the catalogue was not read, which is not the same as no crumbs.
        """
        return parse_node_sessions_and_crumbs(self._sh(
            login, node_and_sessions_snippet(with_sessions, with_crumbs),
            timeout=timeout))

    def status_read(self, login, timeout=COMMAND_TIMEOUT):
        """``(node, [Session], crumbs_or_None, [layout node])`` in one round
        trip: see :func:`status_snippet`."""
        return parse_status(self._sh(login, status_snippet(), timeout=timeout))

    def session_exists(self, login, session, sessions=None):
        """Whether *session* is on *login*'s node; *sessions* is a listing
        the caller has already read, from :meth:`sessions_and_crumbs`."""
        if sessions is None:
            sessions = self.list_sessions(login)
        if not any(row.name == session for row in sessions):
            # False can also mean the listing failed, so nothing is recorded:
            # only a positive answer is evidence.
            return False
        self.state.note_sessions(login, add=[session])
        return True

    def tag_owner(self, login, session, owner=None, force=False):
        """Record which login owns a session.

        An existing tag is left alone unless *force*: overwriting one silently
        is how a live session changes hands behind its owner's back. `adopt`
        passes force, because taking ownership of a stranded session is exactly
        what the user asked for there.
        """
        owner = owner or login
        target = tmux_target(session)
        set_option = (f"tmux set-option -t {target} {OWNER_OPTION} "
                      f"{shlex.quote(owner)} >/dev/null 2>&1")
        if force:
            snippet = set_option
        else:
            snippet = (
                f"cur=$(tmux show-options -qv -t {target} {OWNER_OPTION} 2>/dev/null); "
                f'if [ -z "$cur" ]; then ' + set_option + "; fi"
            )
        return self._run(login, snippet).returncode == 0

    def rename_session_direct(self, node, old, new, timeout=COMMAND_TIMEOUT):
        """Rename a session on a node with no login there, and confirm it.

        Used by `strays rename`, whose whole point is freeing a name held by a
        session nothing is connected to. The result is read back from tmux
        rather than trusted: an unconfirmed rename must not be followed by a
        breadcrumb move, or the record would point at a name that never existed.
        """
        require_name(new, "session name")
        snippet = self._listing_snippet(
            f"tmux rename-session -t {tmux_target(old)} {shlex.quote(new)} "
            "2>/dev/null")
        proc = self._direct_run(node, snippet, timeout=timeout)
        if proc is None:
            return False
        complete, rows = self._listed(proc)
        names = {row.name for row in rows}
        return complete and new in names and old not in names

    # --- breadcrumbs --------------------------------------------------------
    def crumb_add(self, login, session, node=None, owner=None):
        """Record *session* on *node* as owned by *owner* (default *login*).

        *login* is only the connection the write travels over — the home is
        shared, so any live login can record a session on any node. *owner* is
        what the record says, which is what `adopt` and `rename` need to set.
        """
        node = node or self.logins.node_of(login) or self.logins.live_node(login)
        if not node:
            return False
        short = self.backend.short(node)
        snippet = (
            f"mkdir -p {CRUMB_ROOT}/{shlex.quote(short)} && "
            f"printf '%s\\n' {shlex.quote(owner or login)} > "
            f"{CRUMB_ROOT}/{shlex.quote(short)}/{shlex.quote(session)}"
        )
        return self._run(login, snippet).returncode == 0

    def crumb_remove(self, login, session, node=None):
        return self.crumbs_remove(login, [session], node=node)

    def crumbs_remove(self, login, sessions, node=None):
        """Drop the breadcrumbs for *sessions* on *node*, in one round trip."""
        node = node or self.logins.node_of(login)
        if not node or not sessions:
            return bool(node)
        where = f"{CRUMB_ROOT}/{shlex.quote(self.backend.short(node))}"
        # The node's directory goes too once it is empty, so a listing of the
        # shared home stops implying there is state on nodes there is not.
        # `rmdir` refuses a non-empty directory, which is the whole safety
        # check, and `crumb_add` mkdir -p's before writing, so a record that
        # races this simply recreates it.
        files = " ".join(f"{where}/{shlex.quote(session)}" for session in sessions)
        snippet = f"rm -f {files} && {{ rmdir {where} 2>/dev/null || true; }}"
        return self._run(login, snippet).returncode == 0

    def retag_owner(self, login, old_owner, new_owner):
        """Move ownership of every session and breadcrumb from one login to another.

        Used by `rename`. Both halves must move together: the tmux option is what
        a sweep reads to decide a session is ours, and the breadcrumb is what
        finds sessions on nodes we are not connected to. Leaving either behind
        would orphan live work under a name nothing looks for. Its work grows
        with the sessions and breadcrumbs there, so it prints a line for each
        and runs for as long as it keeps printing (_run_printing).
        """
        old_q, new_q = shlex.quote(old_owner), shlex.quote(new_owner)
        retag = (
            f"o=$(tmux show-options -qv -t {EACH_TARGET} {OWNER_OPTION} 2>/dev/null); "
            # An untagged session is still ours if the breadcrumb says so: the
            # tag is set after the session exists, and a failed tag write leaves
            # the breadcrumb as the only ownership record.
            f'if [ "$o" = {old_q} ] || {{ [ -z "$o" ] && '
            f'[ "$(cat {CRUMB_ROOT}/"$h"/"$s" 2>/dev/null)" = {old_q} ]; }}; then '
            f"tmux set-option -t {EACH_TARGET} {OWNER_OPTION} {new_q} "
            ">/dev/null 2>&1; fi; echo ."
        )
        snippet = (
            "h=$(hostname -s); " + for_each_session(retag) + "; "
            # breadcrumbs: the file name is the session, the contents the owner
            f"for d in {CRUMB_ROOT}/*/; do [ -d \"$d\" ] || continue; "
            'for f in "$d"*; do [ -f "$f" ] || continue; '
            f'if [ "$(cat "$f" 2>/dev/null)" = {old_q} ]; then '
            f"printf '%s\\n' {new_q} > \"$f\"; fi; echo .; "
            "done; done; printf done"
        )
        proc = self._run_printing(login, snippet)
        said = (proc.stdout or "").split()
        return proc.returncode == 0 and said[-1:] == ["done"]

    def crumb_retag_node(self, login, node_short, old_owner, new_owner):
        """Retag breadcrumbs for one node only, run through *any* live login.

        The crumb root is on the shared home, so this reaches a node that is
        down — which is the point: a login repinned away from a dead node cannot
        touch the tmux options there, but it can still mark the records so a
        later sweep knows whose they were.
        """
        old_q, new_q = shlex.quote(old_owner), shlex.quote(new_owner)
        d = f"{CRUMB_ROOT}/{shlex.quote(node_short)}"
        snippet = (
            f'for f in {d}/*; do [ -f "$f" ] || continue; '
            f'if [ "$(cat "$f" 2>/dev/null)" = {old_q} ]; then '
            f"printf '%s\\n' {new_q} > \"$f\"; fi; done; printf done"
        )
        return self._sh(login, snippet) == "done"

    def crumbs(self, login, timeout=COMMAND_TIMEOUT):
        """{(node_short, session): owning_login} from the shared home."""
        return parse_crumbs(self._sh(login, crumbs_snippet(), timeout=timeout))

    def crumbs_checked(self, login, timeout=COMMAND_TIMEOUT):
        """``(catalogue_complete, crumbs)`` from the shared home."""
        snippet = f'printf "%s\\n" {CRUMBS_MARKER}; ' + crumbs_snippet()
        proc = self._run(login, snippet, timeout=timeout)
        complete, text = self._checked_payload(proc, CRUMBS_MARKER)
        if not complete:
            return False, {}
        return True, parse_crumbs("\n".join(_after_marker(text, CRUMBS_MARKER)))

    def sessions_and_crumbs(self, login, timeout=COMMAND_TIMEOUT):
        """:meth:`list_sessions` and :meth:`crumbs` in one round trip.

        Unchecked as they are: a half that could not be read comes back empty.
        """
        return parse_sessions_and_crumbs(
            self._sh(login, sessions_and_crumbs_snippet(), timeout=timeout))

    def sessions_and_crumbs_checked(self, login, timeout=COMMAND_TIMEOUT):
        """``(complete, [Session], {(node, session): owner})`` in one round trip.

        The two questions `attach` asks before it can act: which sessions exist
        here, and is this session name already registered on another node. They
        are independent reads, and a separate call would add a whole channel
        setup and nothing else — the same trade :func:`node_and_sessions_snippet`
        makes for `ls`.
        """
        proc = self._run(login, sessions_and_crumbs_snippet(), timeout=timeout)
        complete, text = self._checked_payload(
            proc, SESSIONS_MARKER, CRUMBS_MARKER)
        if not complete:
            return False, [], {}
        sessions, crumbs = parse_sessions_and_crumbs(text)
        return True, sessions, crumbs

    def register_session(self, login, session, owner=None, node=None):
        """Create the session and record who owns it, in one round trip.

        The three writes `attach` makes once it knows the session name:
        create-or-attach, tag the owner, drop the breadcrumb. None depends on
        another's *result*, only on their order.

        Returns ``{step: ok}`` rather than one boolean, because a single exit
        status for three actions would make a failure unattributable, and saying
        exactly what broke is worth more than the line it costs.

        *node* names the node for the breadcrumb path. Left None, the remote
        works it out from its own hostname — which is the point: resolving it
        here would mean a `live_node()` round trip whenever the login has no
        pin, quietly making this two trips.
        """
        require_name(session, "session name")
        require_name(login, "login name")
        text = self._sh(
            login,
            self._linger()
            + register_session_snippet(login, session, owner=owner or login,
                                       node=node),
            timeout=self._create_timeout())
        steps = parse_register_steps(text)
        if steps.get("server"):
            self.state.note_sessions(login, add=[session])
        return steps

    def crumb_sync(self, login):
        """Make the breadcrumbs match reality on this login's node.

        Adds crumbs for live sessions this login owns but that were never
        recorded, and removes crumbs for sessions that are gone. Runs on
        every sweep and on each watcher tick, so the catalogue converges from
        both directions.
        """
        node = self.logins.node_of(login)
        if not node:
            return (0, 0)
        short = self.backend.short(node)
        complete, sessions, crumbs = self.sessions_and_crumbs_checked(login)
        if not complete:
            # Most importantly, do not interpret a failed tmux read as an empty
            # server and delete every breadcrumb on this node.
            ui.warn(f"could not catalogue tmux on '{login}'; breadcrumbs unchanged")
            return (0, 0)
        live = {row.name: row.owner for row in sessions}
        recorded = {s for (n, s) in crumbs if n == short}
        # Deliberately *not* pruned here: crumbs for other nodes. Deleting a
        # record for a node this login cannot see is how live work becomes
        # unfindable. They are published instead — `ls`, `status` and the
        # watcher all name them, and `cluster strays` decides what happens.
        self.last_strays = strays.collect(self, crumbs)
        added = removed = 0
        for session, owner in live.items():
            if session not in recorded and owner == login:
                if self.crumb_add(login, session, node):
                    added += 1
        gone = sorted(recorded - set(live))
        if gone and self.crumbs_remove(login, gone, node):
            removed = len(gone)
        return (added, removed)

    # --- layout snapshots ---------------------------------------------------
    def layout_save(self, login):
        node = self.logins.node_of(login)
        if not node:
            return False
        short = self.backend.short(node)
        fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_current_path}"
        snippet = (
            f"mkdir -p {LAYOUT_ROOT} && "
            f"tmux list-panes -a -F {shlex.quote(fmt)} 2>/dev/null "
            f"> {LAYOUT_ROOT}/{shlex.quote(short)}.tmp && "
            f"mv {LAYOUT_ROOT}/{shlex.quote(short)}.tmp {LAYOUT_ROOT}/{shlex.quote(short)}"
        )
        return self._run(login, snippet).returncode == 0

    def layout_read(self, login, node):
        short = self.backend.short(node)
        snippet = f"cat {LAYOUT_ROOT}/{shlex.quote(short)} 2>/dev/null || true"
        text = self._sh(login, snippet)
        entries = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                entries.append(
                    {"session": parts[0], "window": parts[1],
                     "name": parts[2], "path": parts[3]}
                )
        return entries

    def layout_forget(self, login, node):
        short = self.backend.short(node)
        return self._run(
            login, f"rm -f {LAYOUT_ROOT}/{shlex.quote(short)}"
        ).returncode == 0

    # --- session operations -------------------------------------------------
    def create(self, login, session, command=None, cwd=None):
        """Create *session* on *login*'s node, tagged as *login*'s and recorded.

        The owner tag and the breadcrumb are written by the command that
        creates the session, right after it, so a session this makes is never
        left untagged by a connection that drops between round trips. When
        that command's answer is lost (ANSWER_LOST), the session may exist all
        the same, and the node is asked before anything is reported.
        """
        require_name(session, "session name")
        parts = ["$S", "tmux", "new-session", "-d", "-s",
                 shlex.quote(session)]
        if cwd:
            parts += ["-c", home_path(cwd)]
        if command:
            parts.append(shlex.quote(command))
        # The create's own status is preserved across the scope report: a
        # session that exists must not be reported as a failure because the
        # report after it had nothing to say.
        command = (self._linger()
                   + f"if {' '.join(parts)}; then rc=0; "
                   + record_session_snippet(login, session) + "; "
                   + "else rc=1; fi; "
                   + linger.scope_report(self.logins) + "exit $rc")
        proc = self._run(login, command, timeout=self._create_timeout())
        if proc.returncode in ANSWER_LOST:
            return self._settle_lost_create(login, session, proc)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            ui.die(f"could not create session '{session}'", detail or "no error output")
        self._warn_if_unscoped(proc.stdout)
        self.warn_unrecorded(login, session, parse_register_steps(proc.stdout),
                              ("owner", "crumb"))
        self.state.note_sessions(login, add=[session])
        return True

    def _settle_lost_create(self, login, session, lost):
        """Find out whether a create whose answer was lost made the session.

        Tagged as *login*'s, it did: the tag is written by the same command.
        Its breadcrumb is written again, since the command may have been cut
        off before it. A session there without a tag is not claimed, because
        nothing shows it is the one asked for rather than one made by hand.
        """
        said = (lost.stderr or "").strip().splitlines()
        why = ("it timed out" if lost.returncode == 124 else
               said[-1] if said else f"ssh exited {lost.returncode}")
        complete, rows = self.list_sessions_checked(login)
        row = next((row for row in rows if row.name == session), None)
        if row is None:
            hints = [why]
            if not complete:
                node = self.backend.short(self.logins.node_of(login)) or "the node"
                hints += [f"the request may still have reached {node}, and the "
                          "session be running there",
                          f"look with: cluster sessions {login}"]
            ui.die(f"could not create session '{session}'", *hints)
        if not row.tagged:
            ui.die(f"session '{session}' is on {login} with no ownership tag",
                   f"the answer to creating it was lost ({why}), so it may be "
                   "the one just asked for, or one made by hand",
                   f"if it is yours, claim it by attaching: cluster attach "
                   f"{login} {session}")
        if row.owner != login:
            ui.die(f"session '{session}' on {login} belongs to '{row.owner}'",
                   "it was not created by this command")
        ui.note(f"the answer to creating session '{session}' was lost ({why}), "
                "but the session is there")
        steps = parse_register_steps(self._sh(
            login, record_session_snippet(login, session)))
        self.warn_unrecorded(login, session, steps, ("crumb",))
        self.state.note_sessions(login, add=[session])
        return True

    @staticmethod
    def warn_unrecorded(login, session, steps, which=tuple(STEP_COMPLAINTS)):
        """Say which of *which* steps of registering a session failed."""
        for step in which:
            if not steps.get(step):
                ui.warn(f"{STEP_COMPLAINTS[step]} for '{session}' on {login}")

    def new_window(self, login, session, window, command=None):
        require_name(session, "session name")
        require_name(window, "window name")
        self.ensure_server(login, session)
        parts = ["tmux", "new-window", "-t", tmux_target(session),
                 "-n", shlex.quote(window)]
        if command:
            parts.append(shlex.quote(command))
        proc = self._run(login, " ".join(parts))
        return proc.returncode == 0

    def send(self, login, target, command):
        require_target(target)
        snippet = (f"tmux send-keys -t {split_target(target)} "
                   f"{shlex.quote(command)} Enter")
        return self._run(login, snippet).returncode == 0

    @staticmethod
    def _kills(sessions):
        """Remote shell killing *sessions* by exact name, naming each tmux found."""
        return "; ".join(
            f"tmux kill-session -t {tmux_target(session)} >/dev/null 2>&1 && "
            f'printf "%s\\t%s\\n" {STEP_MARKER} {shlex.quote(session)}'
            for session in sessions)

    @staticmethod
    def _found_by_kill(text):
        """The names :meth:`_kills` reported tmux found and killed."""
        return {parts[1] for parts in (line.split("\t", 1)
                                       for line in (text or "").splitlines())
                if len(parts) == 2 and parts[0] == STEP_MARKER}

    def _confirmed(self, wanted, proc):
        """``(killed, failed)``: only a complete listing without a name is a kill."""
        complete, rows = self._listed(proc)
        if not complete:
            return [], list(wanted)
        still_live = {row.name for row in rows}
        return ([session for session in wanted if session not in still_live],
                [session for session in wanted if session in still_live])

    def kill_session(self, login, session):
        """Kill a session and prune its breadcrumb — but only once confirmed.

        Returns True only when the session is verifiably gone. A pin or a
        breadcrumb must never be dropped on an unconfirmed kill, or real work
        becomes unfindable.
        """
        return self.kill_session_checked(login, session)[0]

    def kill_session_checked(self, login, session):
        """``(gone, killed, remaining)`` for one kill, in one round trip.

        *gone* is what :meth:`kill_session` returns. *killed* says whether tmux
        found a session of exactly that name to kill: gone without killed means
        nothing by that name was there, which must not be reported as a kill.
        *remaining* is the names the confirming catalogue read, so a caller can
        say what was probably meant without paying for another round trip.
        """
        require_name(session, "session name")
        proc = self._run(login, self._listing_snippet(self._kills([session])))
        killed = session in self._found_by_kill(proc.stdout)
        complete, rows = self._listed(proc)
        remaining = [row.name for row in rows]
        if not complete or session in remaining:
            return False, killed, remaining
        self.crumb_remove(login, session)
        self.state.note_sessions(login, remove=[session])
        return True, killed, remaining

    def owned_sessions_checked(self, login):
        """``(catalogue_complete, owned_names)`` without failure/empty ambiguity."""
        complete, rows = self.list_sessions_checked(login)
        return complete, [row.name for row in rows
                          if row.owner == login and not row.foreign]

    def kill_owned_checked(self, login):
        """``(catalogue_complete, killed, failed)`` for this login's work."""
        complete, owned = self.owned_sessions_checked(login)
        if not complete:
            return False, [], []
        killed, failed = self.kill_sessions(login, owned)
        return True, killed, failed

    def kill_sessions(self, login, sessions, settle=False):
        """Kill an already catalogued sequence on *login*'s node and confirm it.

        One round trip for every kill and the listing that confirms them, and
        one more for the breadcrumbs of the confirmed ones. *settle* settles
        the node in the same command, after the kills. Its work grows with the
        sessions, so it runs for as long as it keeps printing (_run_printing).
        """
        wanted = list(dict.fromkeys(sessions))
        if not wanted:
            if settle:
                self.settle_node(self.logins.node_of(login), login)
            return [], []
        snippet = self._listing_snippet(self._kills(wanted)) + self._then_settle(settle)
        killed, failed = self._confirmed(
            wanted, self._run_printing(login, snippet))
        if killed:
            self.crumbs_remove(login, killed)
            self.state.note_sessions(login, remove=killed)
        return killed, failed

    def kill_sessions_direct(self, node, sessions, timeout=COMMAND_TIMEOUT, settle=False):
        """Kill an already catalogued sequence directly and confirm the result.

        One connection for all of it; *settle* settles the node in the same
        command, which on a TOTP-paced backend saves an authentication.
        """
        wanted = list(dict.fromkeys(sessions))
        if not wanted:
            if settle:
                self.settle_node(node)
            return [], []
        snippet = self._listing_snippet(self._kills(wanted)) + self._then_settle(settle)
        proc = self._direct_run(node, snippet, timeout=timeout)
        if proc is None:
            return [], wanted
        return self._confirmed(wanted, proc)

    # --- attaching ----------------------------------------------------------
    def attach_argv(self, session, create=False):
        require_name(session, "session name")
        if create:
            # This one carries its own setup: it is handed to ssh as the whole
            # remote command, with no linger prefix in front of it, and -A on
            # a server-less node starts the server.
            return (linger.scope_setup(self.logins) +
                    f"$S tmux new-session -A -s {shlex.quote(session)}")
        return f"tmux attach-session -t {tmux_target(session)}"

    def reattach_argv(self, session):
        """What an attach runs after its connection dropped: the session, and
        never a new one. One that ended while the connection was down is said
        to have, rather than made again, empty, under its name."""
        require_name(session, "session name")
        target = tmux_target(session)
        gone = shlex.quote(f"cluster: session '{session}' ended while the "
                           "connection was down; not starting a new one")
        return (f"tmux has-session -t {target} 2>/dev/null || "
                f"{{ echo {gone} >&2; exit 1; }}; "
                f"exec tmux attach-session -t {target}")
