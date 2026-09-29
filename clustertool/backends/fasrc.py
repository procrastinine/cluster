"""Harvard FASRC backend.

Every connection authenticates with password + TOTP, so each ssh runs under a
pty that types the answers, and authentications must be paced one per 30-second
window because the cluster rejects a reused code.

Login nodes are individually routable and the pool balancer is sticky, so a
login takes whatever node it lands on and then pins itself there — the opposite
of the NERSC backend, which picks its node up front.
"""

from __future__ import annotations

from ..nodes import BATCH, LOGIN, MOUNT, TRANSFER, NodeClass
from .base import InteractiveTotpBackend, resolve_cred_dir


class FasrcBackend(InteractiveTotpBackend):
    name = "fasrc"
    label = "Harvard FASRC"
    node_domain = "rc.fas.harvard.edu"
    pool_host = "login.rc.fas.harvard.edu"

    #: the balancer hands out the node; asking for a specific one up front would
    #: spend a TOTP window per attempt.
    node_choosable = False

    #: measured on holylogin06, 2026-09-17: KillUserProcesses=yes, so a
    #: detached tmux server dies with the last connection unless linger is on.
    reaps_on_logout = True

    #: "Harvard FAS RC Holyoke" — serves /n/home*, /n/netscratch and lab shares,
    #: which is where this cluster's storage actually lives even though the login
    #: nodes may be in Boston. (Boston's own collection is
    #: 3593e764-9ef1-11ea-9a39-0255d23c44ef, for Boston-side storage.)
    globus_collection = "1156ed9e-6984-11ea-af52-0201714f6eab"
    #: This collection enforces a session policy, so a data_access consent is not
    #: sufficient: the CLI session must have authenticated through FASRC's own
    #: identity provider recently, and that expires. It is why Globus is a poor
    #: fit for unattended FASRC transfers — see crossxfer's direct engine.
    globus_session_domain = "globus.rc.fas.harvard.edu"
    #: Verified live 2026-08-08: /n/netscratch/... and /n/holystore01/LABS/...
    #: list fine, while /n/home*/ returns EndpointPermissionDenied. Home is not
    #: exported, so a home path can never work no matter how the session looks.
    globus_excluded_paths = (
        ("/n/home", "FASRC's Globus collection does not export home directories "
                    "(/n/home*) — only lab and scratch filesystems"),
    )
    globus_path_example = ("/n/netscratch/<lab>/... or "
                           "/n/holystore01/LABS/<lab>/...")

    #: One class: every login node is routable and serves every purpose, since
    #: home, lab and scratch are shared pool-wide. boslogin05 resolves in DNS
    #: but has never been reachable, so it is deliberately absent.
    node_classes = (
        NodeClass(
            name="login",
            routable=True,
            purposes=frozenset({LOGIN, TRANSFER, MOUNT, BATCH}),
            hosts=tuple(
                f"{short}.rc.fas.harvard.edu"
                for short in ("holylogin05", "holylogin06", "holylogin07", "holylogin08",
                              "boslogin06", "boslogin07", "boslogin08")
            ),
            note="login nodes; directly routable, shared storage, sticky balancer",
        ),
    )

    enroll_hint = (
        "Your FASRC username and password are those of your FASRC Research "
        "Computing account, which is not your HarvardKey.",
        "The TOTP seed is the text shown beside the QR code when you set up "
        "FASRC's OpenAuth token. Setting up a new token replaces the old one, "
        "so add the new QR code to your authenticator app as well.",
        "Each new connection types the password and a fresh code for you. "
        "FASRC accepts each code once, so new connections are spaced 30 "
        "seconds apart.",
    )

    def __init__(self, settings):
        super().__init__(settings)
        self.cred_dir = resolve_cred_dir(self.name, self.settings)
        self.user = self._require_username()

    def identity_opts(self):
        # Never offer a key: FASRC accounts authenticate with password + TOTP
        # only, and offering keys burns authentication attempts.
        return [
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=keyboard-interactive,password",
            "-o", "NumberOfPasswordPrompts=1",
        ]
