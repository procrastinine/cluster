"""A private ssh-agent holding exactly one cluster's credential.

A direct cluster-to-cluster transfer runs on one cluster and authenticates to
the other, so the executing node needs the peer's key. There are two ways to
arrange that and only one of them is acceptable:

* copy the key onto the executor's disk — a credential at rest on a shared,
  multi-user login node, outliving the transfer unless cleanup runs;
* forward an agent — the key never leaves this machine, the executor gets a
  socket that can only *use* it, and the socket dies with the connection.

This is the second, with one refinement: it is deliberately **not** the user's
own agent. Forwarding that would expose every key it holds (GitHub, other
sites, other clusters) to root on a shared node for the duration. Instead a
fresh agent is spawned, exactly the peer's identity is loaded, and it is killed
when the transfer ends, including when the transfer is interrupted (SIGINT,
SIGTERM, SIGHUP). The identity is added with a lifetime matching the
credential's own expiry, so even a leaked socket grants nothing after that,
and a transfer may run for as long as the credential is valid.

The residual exposure is real and worth naming: while the transfer runs, root
on the executor node can sign with the forwarded key. That is strictly less
than copying the key there, and it is bounded by the transfer's duration and by
the credential's remaining validity — for NERSC, a certificate that expires
within 24 hours anyway.
"""

from __future__ import annotations

import os
import re
import tempfile

from . import platform as plat, ui


class ScopedAgent:
    """An ssh-agent holding only *identities*, killed on exit."""

    def __init__(self, identities, lifetime=None, label=""):
        self.identities = [str(path) for path in identities]
        self.lifetime = lifetime
        self.label = label
        self.sock = None
        self.pid = None
        self._handlers = {}

    # --- lifecycle ----------------------------------------------------------
    def start(self):
        if not self.identities:
            raise ValueError("a scoped agent needs at least one identity")
        self._catch_ending_signals()
        proc = plat.run(["ssh-agent", "-s"], timeout=20)
        if proc.returncode != 0:
            ui.die("could not start an ssh-agent for the transfer",
                   (proc.stderr or "").strip())
        sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", proc.stdout or "")
        pid = re.search(r"SSH_AGENT_PID=([0-9]+);", proc.stdout or "")
        if not sock or not pid:
            ui.die("could not parse ssh-agent output",
                   (proc.stdout or "").strip()[:200])
        self.sock = sock.group(1)
        self.pid = pid.group(1)

        argv = ["ssh-add"]
        if self.lifetime and self.lifetime > 0:
            # Bound the loaded key to the credential's own remaining validity.
            argv += ["-t", str(int(self.lifetime))]
        argv += self.identities
        added = plat.run(argv, timeout=30, env=self.env())
        if added.returncode != 0:
            self.close()
            ui.die("could not load the transfer identity into the agent",
                   (added.stderr or added.stdout or "").strip())
        return self

    def env(self, base=None):
        """*base* environment plus this agent. Never mutates os.environ."""
        env = dict(base if base is not None else os.environ)
        env["SSH_AUTH_SOCK"] = self.sock or ""
        env["SSH_AGENT_PID"] = self.pid or ""
        return env

    def loaded(self):
        """What the agent holds, for diagnostics."""
        return plat.out(["ssh-add", "-l"], timeout=15, env=self.env())

    def _catch_ending_signals(self):
        # SIGTERM and SIGHUP unwind the transfer (plat.Terminated), so the
        # agent is killed on the way out instead of outliving this process.
        self._handlers = plat.unwind_on_signals()

    def _restore_signals(self):
        plat.restore_signals(self._handlers)
        self._handlers = {}

    def close(self):
        self._restore_signals()
        if not self.pid:
            return
        plat.run(["ssh-agent", "-k"], timeout=15, env=self.env())
        # ssh-agent -k removes its own socket; the directory it made may remain.
        parent = _agent_dir(self.sock)
        if parent:
            try:
                os.rmdir(parent)
            except OSError:
                pass
        self.sock = self.pid = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False


def _agent_dir(sock):
    """The ``ssh-XXXX`` directory ssh-agent made for *sock*, or None.

    ssh-agent makes it in $TMPDIR, else /tmp, and on macOS $TMPDIR is a
    per-user directory under /var/folders, so any of those counts, with
    symlinks resolved (/tmp is /private/tmp there).
    """
    parent = os.path.dirname(sock or "")
    if not os.path.basename(parent).startswith("ssh-"):
        return None
    roots = {os.path.realpath(root) for root in
             ("/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR", "")) if root}
    return parent if os.path.realpath(os.path.dirname(parent)) in roots else None


def borrow(backend):
    """A ScopedAgent carrying *backend*'s credential, or None if it has none."""
    identities = backend.agent_identities()
    if not identities:
        return None
    missing = [path for path in identities if not os.path.isfile(str(path))]
    if missing:
        ui.die(f"{backend.label} identity is missing: {missing[0]}",
               f"run: cluster {backend.cli_flag()} auth")
    return ScopedAgent(identities, lifetime=backend.agent_identity_seconds(),
                       label=backend.name)
