"""NERSC (Perlmutter) backend.

Authentication is a certificate, not a password prompt. NERSC's ``sshproxy``
service takes password+OTP once and returns an SSH key plus a 24-hour
certificate; every connection after that is free. That single fact removes the
whole TOTP-pacing problem the FASRC backend has to live with.

The compensating difficulty is topology. The 40 Perlmutter login nodes are
firewalled from the outside world and the pool address is a load balancer that
hands out a *random* node, so tmux sessions would scatter across nodes you
cannot dial back. Both problems are solved by tunnelling to a specific node
through the pool address, which the certificate authenticates for free.

Data transfer nodes (dtn01-04) are routable directly and exist for I/O, so
mounts and bulk transfers use them instead of a login node.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from .. import config, platform as plat, ui
from ..auth import (failure_text, seconds_left_in_window, totp, totp_key,
                    totp_window)
from ..config import Setting
from ..nodes import BATCH, LOGIN, MOUNT, TRANSFER, NodeClass
from ..state import Refusals
from .base import (CRED_EXPIRING, CRED_MISSING, CRED_OK, CRED_READY, PASSWORD,
                   TOTP_SEED, Backend, resolve_cred_dir)

SSHPROXY_URL = "https://sshproxy.nersc.gov"

#: macOS's own certificate bundle. python.org's Python for macOS brings no CA
#: store of its own until its "Install Certificates" script has been run.
MAC_CA_BUNDLE = "/etc/ssl/cert.pem"

#: Seconds a TOTP window must still have when its code is sent: a code sent in
#: the last moments of its window can land after the window closes.
CODE_MIN_LEFT = 3


class NerscBackend(Backend):
    name = type_name = "nersc"
    aliases = ("perlmutter",)
    shorthand = True
    label = "NERSC Perlmutter"
    node_domain = "chn.perlmutter.nersc.gov"
    pool_host = "perlmutter.nersc.gov"

    SETTINGS = dict(
        Backend.SETTINGS,
        KEY=Setting("~/.ssh/nersc", "the sshproxy private key; its certificate "
                                    "is KEY-cert.pub beside it", required=True),
        SCOPE=Setting("default", "the sshproxy scope to request", required=True),
        COLLAB=Setting("", "collaboration account to get the certificate for "
                           "(empty: your own)"),
        CERT_RENEW_MARGIN=Setting(3600, "seconds before expiry when the "
                                        "certificate is renewed"),
        SSHPROXY_TIMEOUT=Setting(90, "seconds allowed for certificate acquisition"),
        NODE_REACH_TIMEOUT=Setting(8, "seconds for a reachability TCP probe"),
        NODE_REACH_SSH_TIMEOUT=Setting(45, "seconds for a reachability SSH probe"),
        NERSC_NODE_CANDIDATES=Setting(6, "fallback login nodes considered per login"),
        # The bridge keeps the NERSC credential and the companion, `nersc`,
        # fresh on a hub (a login of another cluster), so anything there drives
        # NERSC as one more Slurm backend. A fresh certificate is fetched when
        # the current one has less than BRIDGE_MIN_CERT_LEFT seconds left; with
        # the 8-hour cron cadence the default (20h) keeps the pushed
        # certificate always >=16h from expiry. The companion's own settings
        # live only in the hub's ~/.config/nersc/config; see
        # docs/nersc-bridge.md.
        BRIDGE_MIN_CERT_LEFT=Setting(72000, "certificate seconds required before "
                                            "a bridge push"),
        # Empty: the hub backend's DEFAULT_LOGIN, then any other known login
        # on it (the home is shared, so any login lands the same files).
        BRIDGE_LOGIN=Setting("", "preferred hub login for bridge pushes"),
        # The companion's `run true` opens its own connection to NERSC, up to
        # three tries of about a minute each, before it answers.
        BRIDGE_VERIFY_TIMEOUT=Setting(300, "seconds a bridge push waits for the "
                                           "hub's end-to-end check to answer"),
    )

    interactive_auth = False
    paces_totp = False
    node_choosable = True
    #: Connections present a certificate, so what init offers is fetching one.
    first_check = "credential"
    #: An sshproxy certificate is a file, so it can be lent to another cluster's
    #: ssh through a forwarded agent — which is what makes a direct
    #: cluster-to-cluster transfer possible with NERSC as the far side.
    lends_credential = True
    companion_drives = True

    #: "NERSC Perlmutter" — $HOME and $SCRATCH. Verified live 2026-08-08:
    #: lists /global/homes/<i>/<user> with only the data_access consent, no
    #: session policy.
    globus_collection = "6bdc7956-fc0f-4ad2-989c-7aa5ee643a79"
    globus_path_example = "/global/homes/<i>/<user>/... or /pscratch/sd/<i>/<user>/..."
    #: Mounts belong on a DTN, not a login node: login nodes are cgroup-capped
    #: at 30 GB / 12.5% CPU and NERSC policy reserves them for editing and
    #: compiling, while DTNs are built for I/O and are routable without a jump.
    mount_via = "mount_node"

    enroll_hint = (
        "Your NERSC username and password are the ones you sign in to Iris "
        "(iris.nersc.gov) with.",
        "The TOTP seed is the secret of a NERSC MFA token. Iris lets you add "
        "a token of your own for this tool, next to the one on your phone: "
        "its secret is shown beside its QR code when you create it.",
        "They are used only to fetch a 24-hour certificate from NERSC's "
        "sshproxy; every connection after that uses the certificate.",
    )
    enroll_settings = ("COLLAB",)

    #: login01..login40 as of 2026-08 (128.55.64.10-49).
    node_classes = (
        NodeClass(
            name="login",
            routable=False,  # firewalled: 128.55.64.0/18 is unreachable outside
            purposes=frozenset({LOGIN, BATCH}),
            template="login{n:02d}.chn.perlmutter.nersc.gov",
            count=40,
            note="Perlmutter login nodes; reached through the pool address; "
                 "cgroup-capped at 30 GB / 12.5% CPU per user",
        ),
        NodeClass(
            name="dtn",
            routable=True,
            purposes=frozenset({TRANSFER, MOUNT}),
            hosts=("dtn01.nersc.gov", "dtn02.nersc.gov",
                   "dtn03.nersc.gov", "dtn04.nersc.gov"),
            note="data transfer nodes; routable directly, global homes mounted, "
                 "interactive use restricted to data preparation",
        ),
    )

    def __init__(self, settings):
        super().__init__(settings)
        self.cred_dir = resolve_cred_dir(self.name, self.settings)
        self.user = self._require_username()
        self.key_path = Path(self.settings.str("KEY")).expanduser()
        self.scope = self.settings.str("SCOPE")
        self.collab = self.settings.str("COLLAB")
        #: renew this long before the certificate actually expires
        self.renew_margin = self.settings.int("CERT_RENEW_MARGIN")

    # --- certificate --------------------------------------------------------
    @property
    def cert_path(self):
        return self.key_path.with_name(self.key_path.name + "-cert.pub")

    def cert_valid_until(self):
        """Expiry of the cached certificate as a naive local datetime."""
        if not self.cert_path.is_file():
            return None
        text = plat.out(["ssh-keygen", "-L", "-f", str(self.cert_path)], timeout=15)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Valid:"):
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "to":
                    try:
                        return datetime.strptime(parts[4], "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        return None
        return None

    def cert_seconds_left(self):
        until = self.cert_valid_until()
        if until is None:
            return None
        return (until - datetime.now()).total_seconds()

    def agent_identities(self):
        """The sshproxy key; its certificate is found by name alongside it.

        A NERSC credential is a *file*, which is what lets another cluster
        authenticate here without a password ever leaving this machine — the
        basis of a direct cluster-to-cluster transfer.
        """
        self.ensure_credential(quiet=True)
        return [self.key_path]

    def agent_identity_seconds(self):
        left = self.cert_seconds_left()
        if left is None or left <= 0:
            return None
        return int(left)

    def _certificate(self):
        """(state, detail) of the certificate alone."""
        left = self.cert_seconds_left() if self.key_path.is_file() else None
        if left is None:
            if self.cert_path.is_file():
                return CRED_MISSING, f"cannot read {self.cert_path}"
            return CRED_MISSING, "no certificate yet"
        if left <= 0:
            return CRED_MISSING, f"certificate expired {_ago(-left)} ago"
        if left <= self.renew_margin:
            return CRED_EXPIRING, f"certificate expires in {_ago(left)}"
        return CRED_OK, f"certificate valid for {_ago(left)}"

    def credential_state(self):
        """The certificate's state, or "ready" when a new one can be fetched."""
        state, detail = self._certificate()
        if state == CRED_OK:
            return state, detail
        missing = self.missing_credentials(self.settings)
        if missing:
            return state, (f"{detail}; a new one needs the {' and '.join(missing)}: "
                           f"cluster {self.cli_flag()} config credentials")
        if state == CRED_MISSING:
            return CRED_READY, (f"{detail}; one is fetched on first use, or now "
                                f"with `cluster {self.cli_flag()} auth`")
        return state, detail

    def ensure_credential(self, force=False, quiet=False):
        """A usable certificate, fetched when there is none or it is expiring.

        A renewal that fails while the current certificate still works is
        reported and passed over, a refused one included: nothing needs the
        new one until the current one expires, the next use tries again, and
        a connection presenting the certificate risks nothing on the
        password. A renewal nobody asked for is made as an unattended one
        is, so a person's command does not send a refused password again
        before it is needed. One that was asked for (*force*) stops here.
        """
        seen = self.cert_mark()
        state, detail = self._certificate()
        if not force and state == CRED_OK:
            return True
        if not quiet:
            reason = "obtaining" if state == CRED_MISSING else "renewing"
            ui.info(f"{reason} NERSC certificate ({detail})")
        needed = force or state != CRED_EXPIRING
        try:
            self.fetch_certificate(quiet=quiet, seen=seen,
                                   by_hand=None if needed else False)
        except SystemExit as exc:
            why = failure_text(exc)
            state, detail = self._certificate()
            if force or state != CRED_EXPIRING:
                raise
            ui.warn(f"could not renew the NERSC certificate: {why}")
            ui.note(f"carrying on with the current one ({detail}); "
                    "the next use tries again")
        return True

    def credential_refused(self, detail, quiet=False):
        """ssh refused the certificate: fetch a fresh one, once. True when one
        is in place for the next try.

        One refused is replaced, since it may have been revoked or cut short
        at NERSC; one fetched to replace a refused one and refused in turn
        is not, since the next would be refused the same way. The file
        certificate.refused names the replacement until a connection
        presenting a certificate opens (credential_accepted)."""
        mark = self.cert_mark()
        record = config.state_dir(self.name) / "certificate.refused"
        try:
            replacement = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            replacement = None
        if mark is not None and replacement == list(mark):
            if not quiet:
                ui.warn(f"NERSC refused a certificate fetched to replace a refused "
                        f"one ({detail}); not fetching another")
                ui.note(f"fetch one by hand with: cluster {self.cli_flag()} auth")
            return False
        if not quiet:
            ui.warn(f"NERSC refused the certificate ({detail}); fetching a fresh one")
        self.fetch_certificate(quiet=quiet, seen=mark, min_left=0)
        new = self.cert_mark()
        if new is None or new == mark:
            return False
        try:
            plat.atomic_write_text(record, json.dumps(list(new)) + "\n")
        except OSError:
            pass
        return True

    def credential_accepted(self):
        try:
            (config.state_dir(self.name) / "certificate.refused").unlink()
        except OSError:
            pass

    @classmethod
    def setup_steps(cls):
        return [(f"cluster {cls.cli_flag()} auth",
                 "fetch a certificate now (uses one TOTP code)")]

    def refusal_holds_connections(self):
        """A refusal on record is sshproxy's, of the password: it holds a
        connection only when that needs a certificate fetched first."""
        return self._certificate()[0] == CRED_MISSING

    def cert_mark(self):
        """What tells the installed certificate file from any other, or None.

        Every install replaces the file, so a different mark means a
        certificate was installed in between.
        """
        try:
            stat = os.stat(self.cert_path)
        except OSError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def fetch_certificate(self, quiet=False, min_left=None, seen=None, by_hand=None):
        """Exchange password+OTP for a 24h key/certificate pair.

        One fetch at a time on this machine, across processes: each spends a
        TOTP code, sshproxy refuses a code that was already used, and every
        refusal counts towards locking the account. A fetch holds
        ``sshproxy.lock`` for as long as it runs, and one that finds it held
        waits for as long as its holder lives. What it waited for may have been
        the same fetch, so a certificate installed in the meantime is used
        instead of fetching again: one that is not *seen*, the certificate the
        caller looked at (by default, the one there when this was called),
        and that has more than *min_left* seconds left (by default, the
        renewal margin).

        The shared record of refusals (state.Refusals) is this fetch's: the
        password is what sshproxy refuses. *by_hand* says whether a person
        asked for it, as Backend.by_hand does by default.
        """
        if by_hand is None:
            by_hand = self.by_hand
        if seen is None:
            seen = self.cert_mark()
        need = self.renew_margin if min_left is None else min_left
        fix = f"\n  set it with: cluster {self.cli_flag()} config credentials"
        try:
            password = self.read_credential(PASSWORD)
            secret = self.read_credential(TOTP_SEED)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cluster: {exc}{fix}") from exc
        try:
            totp_key(secret)
        except ValueError as exc:
            raise SystemExit(f"cluster: the {self.label} TOTP seed is not base32 "
                             f"({exc}){fix}") from exc

        state_dir = config.state_dir(self.name)
        lock = plat.FileLock(state_dir / "sshproxy.lock", record_holder=True)

        def announce(pid):
            ui.info(f"waiting for {plat.describe_pid(pid)} to finish fetching "
                    "a NERSC certificate")

        try:
            held = lock.acquire_queued(announce=announce,
                                       stopped=self.settings.int("LOCK_PATIENCE"))
        except OSError as exc:
            raise SystemExit(f"cluster: cannot take {lock.path}: {exc}") from exc
        if not held:
            raise SystemExit("cluster: could not fetch a NERSC certificate: "
                             + plat.gave_up_text(lock, "the certificate lock"))
        try:
            if self.cert_mark() != seen:
                left = self.cert_seconds_left() if self.key_path.is_file() else None
                if left is not None and left > need:
                    if not quiet:
                        ui.info("another process installed a NERSC certificate "
                                f"meanwhile, valid {_ago(left)}")
                    return
            refusals = Refusals(self, state_dir)

            def gate(claim=True):
                # The fetch's own bound: the request, tried twice on macOS,
                # and the wait for a TOTP window before it.
                why = refusals.blocks(
                    by_hand=by_hand, claim=claim,
                    bound=2 * self.settings.int("SSHPROXY_TIMEOUT") + 60)
                if why:
                    raise SystemExit(f"cluster: not asking sshproxy for a "
                                     f"certificate: {why}{fix}\n  or fetch one "
                                     f"by hand: cluster {self.cli_flag()} auth")

            # Before the wait for a TOTP window, and again once it is had.
            gate(claim=False)
            try:
                when = self._claim_window(state_dir / "sshproxy.window", quiet)
                gate()
            except BaseException:
                refusals.released()
                raise
            try:
                body = self._request(password + totp(secret, when=when))
            except SystemExit as exc:
                refusals.settle(False, failure_text(exc))
                raise
            except BaseException:
                refusals.released()
                raise
            refusals.succeeded()
            self._install_pair(*_key_and_certificate(body))
        finally:
            lock.release()

        self.ensure_known_hosts()
        if not quiet:
            left = self.cert_seconds_left() or 0
            ui.info(f"certificate installed at {self.key_path}, valid {_ago(left)}")

    def _claim_window(self, path, quiet):
        """The time to make this fetch's code at, in a window no fetch has used.

        Recorded before the code is sent, so a fetch that dies after sending
        still marks its window spent, and one that follows a failed or
        interrupted fetch within the same 30 seconds waits for the next
        window instead of sending a code sshproxy has seen.
        """
        while True:
            now = time.time()
            window = totp_window(now)
            left = seconds_left_in_window(now)
            if left >= CODE_MIN_LEFT and _read_window(path) != window:
                try:
                    plat.atomic_write_text(path, f"{window}\n")
                except OSError as exc:
                    raise SystemExit(f"cluster: cannot record the TOTP window "
                                     f"used, in {path}: {exc}") from exc
                return now
            if not quiet:
                ui.note(f"waiting {left + 0.5:.0f}s for a fresh TOTP window")
            time.sleep(left + 0.5)

    def _request(self, secret_and_code):
        """sshproxy's answer to one request; any failure is a SystemExit saying why."""
        # Imported where they are used: an HTTPS client takes longer to import
        # than the rest of the tool, and only a certificate fetch needs one.
        import http.client
        import ssl
        import urllib.error
        import urllib.request

        url = f"{SSHPROXY_URL}/create_pair/{self.scope}/"
        payload = b""
        if self.collab:
            payload = json.dumps({"target_user": self.collab}).encode()
        credential = f"{self.user}:{secret_and_code}".encode()
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(credential).decode()
        )
        if payload:
            request.add_header("Content-Type", "application/json")
        try:
            return self._post(request)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                body = ""
            first = body.strip().splitlines()[0] if body.strip() else f"HTTP {exc.code}"
            if "Authentication failed" in body:
                raise SystemExit(
                    f"cluster: sshproxy rejected the credentials: {first}\n"
                    "  This usually means a wrong password, a wrong TOTP seed, "
                    "or a local clock that is out of sync."
                ) from exc
            raise SystemExit(f"cluster: sshproxy returned HTTP {exc.code}: {first}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise SystemExit(_unverified(exc.reason)) from exc
            raise SystemExit(f"cluster: cannot reach {SSHPROXY_URL}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # After the connection was made: no answer in time, the connection
            # closed or reset, or an answer cut short.
            if isinstance(exc, socket.timeout):
                why = (f"no answer within {self.settings.int('SSHPROXY_TIMEOUT')}s "
                       "(SSHPROXY_TIMEOUT)")
            else:
                why = str(exc) or type(exc).__name__
            raise SystemExit(f"cluster: sshproxy did not answer completely: {why}") from exc

    def _post(self, request):
        """The sshproxy response body.

        On macOS a Python with no CA store of its own is retried once against
        the system's bundle: the request never reached the server, so the
        code in it is not spent.
        """
        import ssl
        import urllib.error
        import urllib.request

        timeout = self.settings.int("SSHPROXY_TIMEOUT")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            if not (sys.platform == "darwin"
                    and isinstance(exc.reason, ssl.SSLCertVerificationError)
                    and os.path.isfile(MAC_CA_BUNDLE)):
                raise
        context = ssl.create_default_context(cafile=MAC_CA_BUNDLE)
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            return response.read().decode("utf-8", "replace")

    def _install_pair(self, key_text, cert_text):
        """Put a new key and its certificate in place, never one without the other.

        Everything is written and synced beside its destination first. Then
        the old certificate goes, the key and its public half take their
        places, and the new certificate arrives last. The certificate is what
        makes a pair look usable, so a process stopped at any point leaves the
        old pair, or a key with no certificate, which the next use fetches
        again; never a key beside another key's certificate.
        """
        pub_path = self.key_path.with_name(self.key_path.name + ".pub")
        staged = []

        def stage(path, text):
            staged.append(plat.stage_text(path, text))
            return staged[-1]

        try:
            # Created private if it is missing; an existing ~/.ssh is the user's.
            self.key_path.parent.mkdir(parents=True, exist_ok=True,
                                       mode=config.SECRET_DIR_MODE)
            key = stage(self.key_path, key_text)
            cert = stage(self.cert_path, cert_text)
            pub_text = plat.out(["ssh-keygen", "-y", "-f", str(key)], timeout=15)
            pub = stage(pub_path, pub_text + "\n") if pub_text else None
            self.cert_path.unlink(missing_ok=True)
            os.replace(key, self.key_path)
            if pub:
                os.replace(pub, pub_path)
            else:
                pub_path.unlink(missing_ok=True)
            os.replace(cert, self.cert_path)
            plat.sync_directory(self.key_path.parent)
        except OSError as exc:
            raise SystemExit(f"cluster: cannot install the NERSC certificate "
                             f"at {self.key_path}: {exc}") from exc
        finally:
            for path in staged:
                path.unlink(missing_ok=True)

    def drop_credential(self):
        removed = False
        for path in (self.key_path, self.cert_path,
                     self.key_path.with_name(self.key_path.name + ".pub")):
            if path.exists():
                path.unlink()
                removed = True
        return removed

    #: NERSC signs its login-node host keys with this CA, so one entry covers
    #: every node instead of accumulating 40 host keys.
    CERT_AUTHORITY = (
        "@cert-authority *.nersc.gov ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEA2yKBpvRbdD9MWiu"
        "+7wg17vBsKy46AjjuL27DmdpDYiCqRE2mN0om9b0jn4eI91RGykbcRa9wUKJ2qaD0zsD08A8HM+R14H"
        "4UsZ5hi7S+xGqscJH7uTmXy5Igo5xEOahS9Z+ecgonDCgWKJnbd/FRu4vITYXrvTlIIGHGRBYj0GzbgL"
        "HBzedoMaGNRwhVyadH2SGRaZCgbH+Swevzy0GwYfZJA9zd7EX0jiAClkSYcflIOsygmI3gHv+b35mrvX"
        "cHDeQOR/wg8knfpSiFLCkVDpfgnj27Lemzxe6k61Brhv9CUiq+t7WApVDBovhdXZn6pBg+OKeDk1G1OL"
        "vRbxJ2bw=="
    )

    def ensure_known_hosts(self):
        """Trust NERSC's host CA in ~/.ssh/known_hosts, saying so when it does.

        This is a change to a file every other ssh on this machine reads, so it
        is announced rather than made silently; it happens at most once.
        """
        path = Path.home() / ".ssh" / "known_hosts"
        path.parent.mkdir(parents=True, exist_ok=True, mode=config.SECRET_DIR_MODE)
        existing = path.read_text() if path.is_file() else ""
        if "cert-authority *.nersc.gov" in existing:
            return False
        with path.open("a") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(self.CERT_AUTHORITY + "\n")
        ui.note(f"added NERSC's host certificate authority "
                f"(@cert-authority *.nersc.gov) to {path}")
        return True

    # --- ssh ----------------------------------------------------------------
    def identity_opts(self):
        return [
            "-i", str(self.key_path),
            "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
        ]

    def jump_opts(self, node):
        """Reach a firewalled login node by tunnelling through the pool address.

        ``-W`` on the inner hop makes the TCP connection to the node from inside
        NERSC's network, while authentication for both hops uses the local
        certificate — so a pinned node costs no extra MFA.
        """
        if not node or node == self.pool_host or self.nodes.routable(node):
            return []
        inner = (
            ["ssh", "-F", self.settings.str("SSH_CONFIG")]
            + self.identity_opts()
            + [
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={self.settings.int('CONNECT_TIMEOUT')}",
                "-o", "ServerAliveInterval="
                      f"{self.settings.int('SSH_SERVER_ALIVE_INTERVAL')}",
                "-o", "ServerAliveCountMax="
                      f"{self.settings.int('SSH_SERVER_ALIVE_COUNT_MAX')}",
                "-o", "ControlMaster=no",
                "-o", "ControlPath=none",
                "-W", "%h:%p",
                f"{self.user}@{self.pool_host}",
            ]
        )
        return ["-o", "ProxyCommand=" + " ".join(shlex.quote(part) for part in inner)]

    # --- nodes --------------------------------------------------------------
    def node_probe_host(self, node):
        # An unroutable node cannot be TCP-probed from here, so the pool address
        # is the only thing worth asking about.
        if self.nodes.routable(node):
            return node, 22
        return self.pool_host, 22

    def node_reachable(self, node, tries=None):
        """A real connection is the only honest reachability test here."""
        if self.nodes.routable(node):
            return plat.tcp_open(
                node, 22, timeout=self.settings.int("NODE_REACH_TIMEOUT"))
        if not plat.tcp_open(
                self.pool_host, 22,
                timeout=self.settings.int("NODE_REACH_TIMEOUT")):
            return False
        argv = self.ssh_argv(
            node=node,
            extra=["-o", "BatchMode=yes", "-o", "ControlPath=none"],
            remote="true",
        )
        return plat.run(
            argv, timeout=self.settings.int("NODE_REACH_SSH_TIMEOUT")
        ).returncode == 0

    def node_candidates_for(self, login_name, avoid=(), limit=None):
        """Login nodes to try, in order, deriving the first from the name.

        Deterministic beats random here: if local state is ever lost, the same
        login name lands back on the same node, which is where its tmux sessions
        are. The rest of the list is only a fallback for a node that is down.
        """
        import hashlib

        limit = limit or self.settings.int("NERSC_NODE_CANDIDATES")
        nodes = [n for n in self.pool_nodes() if n not in set(avoid)]
        if not nodes:
            return []
        digest = hashlib.sha256(login_name.encode()).digest()
        start = int.from_bytes(digest[:4], "big") % len(nodes)
        ordered = nodes[start:] + nodes[:start]
        return ordered[:limit]


def _read_window(path):
    """The TOTP window the last fetch claimed, or None."""
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _key_and_certificate(body):
    """(private key, certificate) from sshproxy's answer, or a SystemExit."""
    lines = body.splitlines()
    if not body.strip() or "PRIVATE KEY" not in lines[0]:
        snippet = body.strip().splitlines()[0] if body.strip() else "(empty response)"
        raise SystemExit(f"cluster: sshproxy did not return a key: {snippet}")
    cert_lines = [l for l in lines if "-cert-v01@openssh.com " in l]
    key_lines = [l for l in lines if "-cert-v01@openssh.com " not in l]
    if not cert_lines:
        raise SystemExit("cluster: sshproxy response contained no certificate")
    return "\n".join(key_lines).strip() + "\n", "\n".join(cert_lines) + "\n"


def _unverified(reason):
    """The refusal for a TLS certificate this Python cannot verify."""
    if sys.platform == "darwin":
        version = f"{sys.version_info[0]}.{sys.version_info[1]}"
        return (f"cluster: cannot verify {SSHPROXY_URL}'s certificate: {reason}\n"
                "  this Python has no CA certificates; run "
                f'"/Applications/Python {version}/Install Certificates.command"')
    return f"cluster: cannot verify {SSHPROXY_URL}'s certificate: {reason}"


def _ago(seconds):
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"
