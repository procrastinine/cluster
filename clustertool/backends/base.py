"""The backend interface.

A backend knows four things the rest of the tool refuses to guess: how to prove
who you are, which hosts exist and which of them are reachable, how to address
one specific node, and which local quirks the remote software imposes.

Everything else — logins, pins, tmux, mounts, transfers, the watcher — is
written once against this interface.
"""

from __future__ import annotations

import collections
import os
import subprocess
import sys
from pathlib import Path

from .. import config, platform as plat
from ..auth import read_secret_file, run_with_prompts, totp, totp_key
from ..config import Setting
from ..nodes import LOGIN, MOUNT, TRANSFER, NodeMap

CRED_OK = "ok"
#: Everything this machine must know is there, and the credential made from it
#: is fetched when first needed.
CRED_READY = "ready"
CRED_EXPIRING = "expiring"
CRED_MISSING = "missing"


def totp_seed(text):
    """The TOTP seed in *text*, as it is stored; ValueError saying what is wrong.

    Either the seed itself, in base32 however it was shown (spaces, hyphens
    and case do not matter), or the otpauth:// link a QR code holds. A link is
    refused when it asks for codes the tool does not make: anything but SHA1,
    6 digits and 30 seconds.
    """
    text = text.strip()
    if text.lower().startswith("otpauth-migration:"):
        raise ValueError("that is an authenticator app's export of several "
                         "accounts, not one seed: use the seed or QR code the "
                         "site shows when the token is created")
    if text.lower().startswith("otpauth:"):
        import urllib.parse

        link = urllib.parse.urlsplit(text)
        if link.netloc.lower() != "totp":
            raise ValueError(f"that link is for {link.netloc or 'no'} codes, "
                             "not time-based (TOTP) ones")
        query = {name.lower(): values[-1] for name, values
                 in urllib.parse.parse_qs(link.query).items()}
        for name, wanted in (("algorithm", "SHA1"), ("digits", "6"), ("period", "30")):
            if query.get(name, wanted).upper() != wanted:
                raise ValueError(f"that link asks for {name}={query[name]}; "
                                 f"only {name}={wanted} is supported")
        text = query.get("secret", "")
        if not text:
            raise ValueError("that link has no secret= in it")
    if text.isdigit():
        raise ValueError("that is a 6-digit code, not a base32 TOTP seed: "
                         "the seed is the text shown beside the QR code")
    try:
        totp_key(text)
    except ValueError:
        raise ValueError("not a base32 TOTP seed: a seed is letters A-Z and "
                         "digits 2-7 (spaces and hyphens are ignored), or the "
                         "otpauth:// link its QR code holds") from None
    return text


class Credential(collections.namedtuple(
        "Credential", "label filename keys secret check hint verify",
        defaults=(True, None, "", ""))):
    """One thing a backend needs from you, kept as one file in its credential
    directory.

    *keys* are the names `cluster config get/set/unset` know it by. *check*
    turns what was typed into what is stored, or raises ValueError saying what
    is wrong with it. *verify* is how a typed value is confirmed before it is
    saved: "repeat" asks for it twice at a terminal, "code" shows the TOTP
    code it makes, to compare with the authenticator app.
    """

    __slots__ = ()

    def clean(self, text):
        value = text.strip()
        if not value:
            raise ValueError(f"the {self.label} is empty")
        return self.check(value) if self.check else value


USERNAME = Credential("username", "user", ("USERNAME", "USER", "LOGIN_NAME"),
                      secret=False)
PASSWORD = Credential("password", "pass", ("PASSWORD", "PASS"), verify="repeat")
TOTP_SEED = Credential("TOTP seed", "key.txt", ("TOTP", "TOTP_SEED", "OTP"),
                       check=totp_seed, verify="code",
                       hint="base32, or the otpauth:// link in its QR code")

#: What a backend asks for unless it declares otherwise (Backend.CREDENTIALS).
CREDENTIALS = (USERNAME, PASSWORD, TOTP_SEED)

#: The files a credential directory holds. Anything else there is clutter, and
#: `doctor` notes it.
CRED_FILES = tuple(field.filename for field in CREDENTIALS)

#: Which of those hold a secret and must be mode 600.
CRED_SECRETS = tuple(field.filename for field in CREDENTIALS if field.secret)


def resolve_cred_dir(backend_name, settings):
    """Where this backend's credentials live.

    ``CRED_DIR`` (e.g. ``CLUSTER_<BACKEND>_CRED_DIR``) when it is set: an
    explicit answer, always honoured. Otherwise
    ``$XDG_CONFIG_HOME/cluster/credentials/<backend>``, whether or not it exists
    yet, so error messages and setup instructions name it: one private tree,
    nothing else in it, easy to exclude from backups and impossible to confuse
    with the tool's own code.
    """
    raw = settings._raw("CRED_DIR")
    if raw:
        return Path(raw).expanduser()
    return config.CRED_ROOT / backend_name


def _file_marks(directory, names):
    """[name, mtime, size] of each of *names* in *directory*, sorted, and
    [name] for one that is not there; None when there is no directory, or a
    file there cannot be looked at."""
    if not directory:
        return None
    marks = []
    for name in sorted(set(names)):
        try:
            st = (Path(directory) / name).stat()
        except FileNotFoundError:
            marks.append([name])
            continue
        except OSError:
            return None
        marks.append([name, st.st_mtime_ns, st.st_size])
    return marks


class BackendUnavailable(SystemExit):
    """This backend cannot be used on this machine yet: nobody said who you are.

    A SystemExit, so a command that needs the backend still stops with the
    message; whole-fleet commands catch it and skip the backend instead.
    """

    def __init__(self, backend, reason, fix):
        self.backend, self.reason, self.fix = backend, reason, fix
        super().__init__(f"cluster: {reason}\n  {fix}")


class Backend:
    #: The settings only a backend reads, by name (see clustertool.config).
    #: A subclass extends this with its own; `cluster config set` writes each
    #: to its backend's section of the settings file.
    SETTINGS = {
        "CRED_DIR": Setting("", "credential directory override for this backend"),
        "NODES": Setting("", "space-separated login-node override for this backend"),
    }

    # --- identity -----------------------------------------------------------
    name = ""
    label = ""
    user = ""
    cred_dir = None

    #: What `cluster init` and `cluster config credentials` ask for, in order.
    CREDENTIALS = CREDENTIALS

    #: Settings they also offer, with the credentials: optional per-account
    #: answers such as a collaboration account.
    enroll_settings = ()

    #: Where the answers come from, said before asking for them.
    enroll_hint = ()

    # --- topology -----------------------------------------------------------
    pool_host = ""
    node_domain = ""

    # --- capabilities -------------------------------------------------------
    #: ssh must run under a pty that types the password and OTP.
    interactive_auth = False
    #: the cluster rejects TOTP code reuse, so authentications must be paced
    #: one per 30s window and serialized across processes.
    paces_totp = False
    #: the login node pool can be chosen from, rather than handed out by a
    #: load balancer.
    node_choosable = False

    #: the site's logind kills an account's leftover processes when its last
    #: session on a node ends (KillUserProcesses=yes), so a tmux server only
    #: outlives a disconnect while the account has linger enabled on that node.
    #: Declared rather than discovered: reading logind's configuration would
    #: cost a round trip on every connection to answer a question that is a
    #: property of the site and not of the day. See clustertool.linger.
    reaps_on_logout = False

    #: The cluster the `nersc` companion drives from elsewhere, so there is
    #: nothing for `cluster setup` to install on it.
    companion_drives = False

    #: declared node classes; see clustertool.nodes. An instance's own copy
    #: carries the NODES setting, if any: see configured_node_classes.
    node_classes = ()

    #: Where a mount lives by default.
    #:   "login"      - ride the login's own master; costs no extra connection.
    #:   "mount_node" - open a dedicated master on a MOUNT-purpose node, because
    #:                  the login node is the wrong place for bulk I/O.
    mount_via = "login"

    def __init__(self, settings):
        self.settings = settings
        self.node_classes = self.configured_node_classes(settings)
        self.nodes = NodeMap(self.node_classes)

    @classmethod
    def configured_node_classes(cls, settings):
        """The declared classes, with NODES replacing the login nodes' list.

        One place for the override, so everything that asks about nodes sees
        the same list: the pool, transfer and mount nodes where the login
        class also serves those, `cluster nodes`, and `doctor`. Only the first
        class that serves logins is replaced; another class (NERSC's DTNs)
        keeps its own members. A class method, since knowing the nodes needs
        no username.
        """
        declared = tuple(cls.node_classes)
        explicit = (settings._raw("NODES") or "").split()
        if not explicit:
            return declared
        classes = list(declared)
        for index, node_class in enumerate(classes):
            if node_class.serves(LOGIN):
                classes[index] = node_class._replace(
                    hosts=tuple(cls.fqdn(n) for n in explicit),
                    template="", count=0,
                    note=(f"{node_class.note}; " if node_class.note else "")
                    + "list set by NODES")
                break
        return tuple(classes)

    # --- identity -----------------------------------------------------------
    @classmethod
    def local_username(cls, settings):
        """The username this machine has for *cls*, or ``""``. Never raises.

        ``CLUSTER_<NAME>_USER``, then the ``user`` file in the credential
        directory: local configuration only, so it answers "is this backend
        set up here" for every backend without constructing any of them.
        """
        explicit = os.environ.get(f"CLUSTER_{cls.name.upper()}_USER", "").strip()
        if explicit:
            return explicit
        path = resolve_cred_dir(cls.name, settings) / USERNAME.filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    @classmethod
    def missing_credentials(cls, settings):
        """The labels of the credentials this machine has no file for."""
        cred_dir = resolve_cred_dir(cls.name, settings)
        missing = []
        for field in cls.CREDENTIALS:
            if field is USERNAME:
                present = bool(cls.local_username(settings))
            else:
                try:
                    present = (cred_dir / field.filename).stat().st_size > 0
                except OSError:
                    present = False
            if not present:
                missing.append(field.label)
        return missing

    def read_credential(self, field):
        """The stored value of *field*; OSError or ValueError says what is wrong."""
        return read_secret_file(Path(self.cred_dir) / field.filename,
                                f"{self.label} {field.label}")

    def _require_username(self):
        user = self.local_username(self.settings)
        if not user:
            raise BackendUnavailable(
                self.name, f"{self.label} ({self.name}) is not set up on this "
                "machine: its username is unknown",
                f"set it up with: cluster --{self.name} config credentials")
        return user

    # --- credentials --------------------------------------------------------
    def credential_state(self):
        """(state, human detail). Backends without a cached credential say ok."""
        return CRED_OK, "authenticates per connection"

    def ensure_credential(self, force=False, quiet=False):
        """Make a usable credential available. Returns True if one now exists."""
        return True

    def drop_credential(self):
        return False

    #: Whether a person is connecting by hand (a `login` or `attach` at a
    #: terminal): one then tries a credential that was refused, saying so,
    #: where an unattended process waits (state.Refusals).
    by_hand = False

    def credential_refused(self, detail, quiet=False):
        """ssh refused the credential a connection presented, saying *detail*:
        True when a new one is now in place for the next try. A backend
        whose connections present a password has the shared record of
        refusals instead (state.Refusals), and nothing here to replace."""
        return False

    def credential_accepted(self):
        """A connection presenting the credential opened."""

    def refusal_holds_connections(self):
        """Whether a refusal on the shared record (state.Refusals) holds this
        backend's connections: where each presents the password, it does."""
        return self.interactive_auth

    def credential_marks(self):
        """What tells the credential presented from any other, without reading
        it: [name, mtime, size] of each credential file the backend declares
        (CREDENTIALS), or None when there is none to look at. Nothing else in
        the directory counts: a .DS_Store or an editor's backup there changes
        nothing a connection presents."""
        return _file_marks(self.cred_dir,
                           [field.filename for field in self.CREDENTIALS])

    #: Whether this backend's credential can be lent to ssh running on another
    #: host — declared, not discovered, so that *planning* a transfer never has
    #: the side effect of fetching a certificate. agent_identities() does the
    #: fetching, and is called only once a plan has been committed to.
    lends_credential = False

    #: The site's Globus collection for this cluster's filesystems. Declared here
    #: rather than left to an environment variable because cron does not read a
    #: shell profile, and a scheduled transfer failing for want of a UUID is a
    #: silly way to lose a night. A collection id is public infrastructure, not a
    #: secret. The GLOBUS_COLLECTION setting overrides it.
    globus_collection = ""

    #: Some collections enforce a session policy: a consent is not enough, the
    #: session must have authenticated recently through *this* identity provider.
    #: Recorded so the remedy can be printed before the site has to say so.
    globus_session_domain = ""

    #: ((prefix, why), ...) for paths the Globus collection refuses to export.
    #: A collection is not simply "the filesystem": sites commonly leave home
    #: directories out. Declared so the refusal happens here, with the working
    #: alternative, instead of arriving as a bare 403 from the endpoint.
    globus_excluded_paths = ()

    #: A path shape that does work, for error messages.
    globus_path_example = ""

    def globus_path_problem(self, path):
        """Why this collection cannot serve *path*, or None if it can."""
        for prefix, why in self.globus_excluded_paths:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return why
        return None

    def agent_identities(self):
        """Key files that may be lent to ssh running on *another* host.

        This is what makes a cluster reachable as the far side of a direct
        cluster-to-cluster transfer: the executing cluster needs to authenticate
        here, and the only credential that can travel is one that exists as a
        file and can be loaded into an ssh-agent. A password typed into a pty
        cannot, so the default is "nothing to lend" and such a backend can only
        ever be the executor, never the peer.

        A matching ``<key>-cert.pub`` is picked up by ssh-add automatically.
        """
        return []

    def agent_identity_seconds(self):
        """How long a lent identity stays valid, or None for "no known limit"."""
        return None

    def inbound_transfer_hosts(self):
        """Hosts another cluster may dial for bulk data, best first.

        Not the same question as ``transfer_nodes``, which answers "where should
        *we* run I/O". Login nodes here may be firewalled from the outside while
        the data transfer nodes are routable, and it is the routable one that a
        peer has to be told about.

        A *list*, because one data transfer node being down or drained should
        cost a second's probing rather than the whole transfer.
        """
        routable = [n for n in self.transfer_nodes() if self.nodes.routable(n)]
        return routable or [self.pool_host]

    def inbound_transfer_host(self):
        """The first choice among inbound_transfer_hosts()."""
        return self.inbound_transfer_hosts()[0]

    # --- ssh ----------------------------------------------------------------
    def common_opts(self, forward_agent=False):
        # -F /dev/null by default: this tool is the single source of truth for
        # how it connects, so a stray ~/.ssh/config stanza can never change its
        # behaviour. CLUSTER_SSH_CONFIG opts back in.
        #
        # ForwardAgent is a parameter rather than something a caller appends,
        # because ssh takes the *first* value it is given for an option: a
        # trailing "-o ForwardAgent=yes" after this list is silently ignored.
        ssh_config = config.global_value("SSH_CONFIG", "/dev/null")
        return [
            "-F", ssh_config,
            "-o", f"User={self.user}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ForwardAgent={'yes' if forward_agent else 'no'}",
            "-o", "ForwardX11=no",
            "-o", f"ServerAliveInterval={self.settings.int('SSH_SERVER_ALIVE_INTERVAL')}",
            "-o", f"ServerAliveCountMax={self.settings.int('SSH_SERVER_ALIVE_COUNT_MAX')}",
            "-o", f"ConnectTimeout={self.settings.int('CONNECT_TIMEOUT')}",
        ]

    def identity_opts(self):
        return []

    def jump_opts(self, node):
        """Options needed to reach *node* when it is not directly routable."""
        return []

    def host_for(self, node=None):
        """The hostname ssh should connect to."""
        return node or self.pool_host

    def target(self, node=None):
        return f"{self.user}@{self.host_for(node)}"

    def ssh_opts(self, node=None, sock=None, master=False, persist=None,
                 forward_agent=False):
        """The options of an ssh that authenticates by itself: a master on
        *sock*, or with no *sock* a connection of its own.

        What rides a master is built by sshmux.rider_argv, with the guard that
        keeps it from connecting by itself once the master is gone.
        """
        opts = list(self.common_opts(forward_agent=forward_agent))
        opts += list(self.identity_opts())
        if node:
            opts += self.jump_opts(node)
        if sock is not None:
            if not master:
                raise ValueError("a rider is built by sshmux.rider_argv")
            opts += ["-o", f"ControlPath={sock}", "-o", "ControlMaster=yes",
                     "-o", f"ControlPersist={persist or 'yes'}"]
        return opts

    def ssh_argv(self, node=None, sock=None, master=False, extra=(), remote=None,
                 persist=None, forward_agent=False):
        argv = ["ssh"] + self.ssh_opts(node=node, sock=sock, master=master,
                                       persist=persist, forward_agent=forward_agent)
        argv += list(extra)
        argv.append(self.target(node))
        if remote is not None:
            argv.append(remote)
        return argv

    def run_ssh(self, argv, timeout=None, capture=True, quiet=True, env=None):
        """Run an ssh argv, answering auth prompts if this backend needs it."""
        if not self.interactive_auth:
            return plat.run(argv, timeout=timeout, capture=capture, env=env)
        said = []
        rc = self._run_interactive(argv, quiet=quiet, transcript=said, env=env,
                                   timeout=timeout)
        # A pty has one stream: the transcript is the authentication chatter and
        # the command's own output, inseparably. It stands in for stderr, because
        # callers reporting a failure have nothing else to show — but it is also
        # *stdout*: leaving that empty would make every marker-framed read over
        # a fresh connection come back "could not be reached" on a password
        # backend, which would disable `clean`'s direct-node sweep and `strays
        # check` for exactly the nodes with no login on them, the only nodes
        # either one exists to ask about. The marker is what separates the
        # payload from the chatter, so both streams can safely carry it.
        text = "".join(said).replace("\r\n", "\n")
        return subprocess.CompletedProcess(argv, rc, text, text)

    def _run_interactive(self, argv, quiet=True, sink=None, transcript=None,
                         env=None, timeout=None):
        raise NotImplementedError

    def exec_interactive(self, argv, transcript=None):
        """Hand the terminal to ssh and unconditionally repair it afterwards.

        Most managed sessions use ``Logins.interactive``, which also repairs
        between reconnect attempts.  Keeping this guard at the backend boundary
        covers direct/disposable/rescue sessions and exceptions as well; nested
        restoration is idempotent. *transcript*, a list, collects the end of
        what a backend answering prompts saw (auth.run_with_prompts).
        """
        saved_tty = plat.save_tty()
        plat.restore_tty(saved_tty)
        try:
            if not self.interactive_auth:
                return subprocess.run(argv).returncode
            # A real session's output is data: it belongs on stdout.
            return self._run_interactive(argv, quiet=False, sink=sys.stdout.buffer,
                                         transcript=transcript)
        finally:
            plat.restore_tty(saved_tty)

    # --- nodes --------------------------------------------------------------
    @classmethod
    def fqdn(cls, name):
        """Expand a short node name to a fully qualified one."""
        if not name:
            return name
        if "." in name:
            return name
        return f"{name}.{cls.node_domain}" if cls.node_domain else name

    @staticmethod
    def short(fqdn):
        return (fqdn or "").split(".", 1)[0]

    def pool_nodes(self):
        """Every node that could serve a login, as fqdns."""
        return self.nodes.for_purpose(LOGIN)

    def node_candidates_for(self, login_name, avoid=(), limit=None):
        """Nodes to try for a new login, in order.

        ``[None]`` means "let the pool balancer decide", which is the only
        affordable answer when asking for a specific node costs an
        authentication.
        """
        return [None]

    def node_probe_host(self, node):
        """Host/port to probe when checking whether a node is usable."""
        return self.host_for(node), 22

    def node_reachable(self, node, tries=None):
        tries = tries or self.settings.int("NODE_PROBE_TRIES")
        host, port = self.node_probe_host(node)
        for _ in range(max(1, tries)):
            if plat.tcp_open(host, port,
                             timeout=self.settings.int("NODE_PROBE_TIMEOUT")):
                return True
        return False

    # --- data paths ---------------------------------------------------------
    def transfer_nodes(self):
        """Hosts suited to bulk data movement."""
        return self.nodes.for_purpose(TRANSFER) or self.pool_nodes()

    def mount_nodes(self):
        """Hosts that may serve an sshfs mount (storage has no node affinity)."""
        return self.nodes.for_purpose(MOUNT) or self.transfer_nodes()

    def home_remote(self):
        """Default remote path for a mount: '.', the login shell's own cwd.

        Not an absolute home path: on NERSC /global/homes/<i>/<user> is a
        symlink to /global/u2/..., and sshfs refuses a symlink as its root.
        """
        return "."

    # --- misc ---------------------------------------------------------------
    def describe(self):
        return f"{self.label} ({self.name}) as {self.user}"


class InteractiveTotpBackend(Backend):
    """A backend that types password + TOTP on every connection."""

    interactive_auth = True
    paces_totp = True

    def _password(self):
        return self.read_credential(PASSWORD)

    def _secret(self):
        return self.read_credential(TOTP_SEED)

    def _otp(self):
        return totp(self._secret())

    def _run_interactive(self, argv, quiet=True, sink=None, transcript=None,
                         env=None, timeout=None):
        return run_with_prompts(argv, self._password(), self._otp, quiet=quiet,
                                sink=sink, transcript=transcript, env=env,
                                timeout=timeout)

    def credential_state(self):
        try:
            self._password()
            self._secret()
        except PermissionError as exc:
            return CRED_MISSING, str(exc)
        except (OSError, ValueError) as exc:
            return CRED_MISSING, (f"{exc}; set it with: "
                                  f"cluster --{self.name} config credentials")
        return CRED_OK, "password + TOTP per connection"
