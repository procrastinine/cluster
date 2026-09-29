# Contributing

Bug reports and pull requests are welcome. This file covers the layout of the
source, how to run the tests, and how to add a backend for another site.

## Ground rules

- **Standard library only**, Python 3.8 or newer, POSIX only (Linux and macOS).
  `remote/nersc` runs on a cluster's system Python and must stay compatible with
  Python 3.6: no f-string features beyond 3.6, and no `capture_output` or `text`
  arguments to `subprocess`.
- **No secrets and nothing machine-specific in the tree.** Credentials live in
  `~/.config/cluster/credentials/`, and state lives in
  `~/.local/state/cluster/`. `.gitignore` blocks the usual credential file
  names as a backstop.
- **Never authenticate to look.** Listing, status and completion must work from
  local state and already-open masters. On a site that paces TOTP, every
  authentication costs 30 seconds.
- **Every command pays for the package's imports.** A module that only some
  commands need and that is slow to import (an HTTPS client, `hashlib`,
  `inspect`, `concurrent.futures`) is imported in the function that uses it,
  and records are namedtuples, not dataclasses. `tests/test_cli.py` holds the
  list.
- **Nothing destructive without proof.** A kill must be confirmed before a
  record is dropped, and cleanup must be able to prove what it removes.
- **No deadline on real work.** A copy or a command may take as long as it
  takes; what ends something is evidence that the far side stopped answering,
  such as ssh keepalives or rclone's I/O timeout, and the message says what
  happened.
- Record the reasoning behind non-obvious behaviour next to the code, and put
  site measurements in [docs/design.md](docs/design.md) with the date and node.

## Source layout

```
bin/cluster                  entry point; runs from the checkout
bin/cluster-relay            laptop client that forwards to the machine running cluster
bin/nersc                    runs remote/nersc in place on this machine
clustertool/                 the package
clustertool/backends/        one module per site, plus the interface in base.py
clustertool/commands/        user-facing command handlers, grouped by concern
remote/nersc                 the single-file NERSC companion (Python 3.6)
remote/hooks.example.py      an example project hooks file for the companion
remote/mirror.exclude        default excludes for the companion's code mirror
completions/cluster.bash     completion for bash 3.2+ and zsh (local state only)
extras/                      optional tools and systemd units that use cluster
docs/                        setup, file inventory, design notes, the NERSC bridge
tests/test_*.py              unit tests, one module per subject; no cluster needed
tests/support.py             the test sandbox and helpers shared by the test modules
tests/live_check.sh          end-to-end checks against real accounts
```

`clustertool/cli.py` owns only the shared invocation context, fleet-wide
maintenance, help and dispatch. Handlers live under `clustertool/commands/`:
`sessions.py` for interactive, tmux and pin lifecycle; `connections.py` for
`login`, `ls`, `where` and `channels`; `mounts.py` for sshfs, the watcher and
`boot`; `transfers.py` for `ssh-command`, rsync, rclone and cross-cluster
copies; `configure.py` for `auth`, `backends`, `bridge` and `nodes`;
`maintenance.py` for `clean`, `doctor`, `forget`, `linger`, `rename`, `status`
and `fixterm`; `init.py`, `setup.py`, `strays.py` and `tools.py` for their
commands. Commands register themselves with the `@command` decorator in
`clustertool/command.py`, and `--help` text is read from each handler's
docstring and argparse definitions without running it.

Transport and state policy live in service modules, so handlers never
reimplement SSH, tmux, mount or transfer mechanics:

| Module | Responsibility |
|---|---|
| `sshmux.py` | logins: control masters pinned to a node, reconnect, TOTP pacing |
| `state.py`, `registry.py`, `context.py` | on-disk state for one backend; which backend a login lives on; what a command runs against |
| `nodes.py` | node classes and their purposes |
| `tmuxlayer.py`, `lifecycle.py`, `strays.py` | remote tmux, ownership tags and breadcrumbs; what happens to sessions when a login leaves its node; stray records |
| `remote_sh.py` | quoting for every value spliced into a command run on a cluster |
| `linger.py` | keeping a login node's tmux alive after the last connection ends |
| `mounts.py`, `watcher.py` | sshfs mounts, the probe and repair ladder, the per-login watcher |
| `transfer.py`, `riding.py`, `crossxfer.py`, `agentscope.py`, `globuslayer.py` | rclone transfers, riding out a lost connection, cluster-to-cluster engines, the single-key ssh-agent, Globus |
| `bridge.py`, `companion.py` | keeping the NERSC credential and the companion fresh on a hub, and two-way sync of the companion |
| `config.py`, `configcmd.py` | the settings catalogue, precedence and `cluster config` |
| `auth.py` | TOTP, and answering password and code prompts under a pty |
| `platform.py` | everything that differs between Linux and macOS |
| `listing.py`, `diagnostics.py`, `ui.py`, `processname.py`, `setup.py` | `ls`, `status` and `doctor`, output, process names, setup merges |

The client half of `bin/cluster-relay` reads none of the package's state: it
imports only `clustertool.backoff`, which reads none either. Its `--serve`
half runs on the relay host and imports the package from its own checkout.
What the client duplicates (the list of rclone flags that take a separate
value, the defaults of its `[relay]` retry keys, the rsync statuses it
resumes on) is checked against the real thing by the tests.

## Running the tests

The tests come in two halves. The unit suite is offline: it uses made-up
credentials, never yours, and has no network. `tests/live_check.sh` is live:
it uses the credentials you have set up, against the real clusters.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
CLUSTER_FORCE_PORTABLE=1 python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest tests.test_transfers          # one module
```

The unit tests need no cluster, no credentials and no network. Importing
`tests/support.py` moves the whole run into a sandbox: a temporary `HOME`, no
`CLUSTER_*`, `XDG_*`, `ARCHIVE_SYNC_*` or `NERSC_*` variables from your
environment, no `PATH` entries under your real home, and no ssh-agent, tmux
or session bus of yours. Nothing a test does can reach your settings, state,
`~/.ssh` or mounts, and your settings cannot change what a test sees. A new
test module imports `support` before anything from `clustertool`; it refuses
to load otherwise.

The sandbox also has no network. Resolving or connecting to any host but this
machine fails as it would offline, and the test that tried fails as well,
naming the host. A path left unpatched (a certificate fetch, a reachability
probe) would otherwise send the sandbox's made-up credentials to a real
cluster, or pass only because the machine running it happens to be offline.
Stub it, or check it in `tests/live_check.sh`.

`CLUSTER_FORCE_PORTABLE=1` forces the fallbacks that stand in where Linux-only
interfaces such as `/proc`, `/proc/self/mountinfo` or `timedatectl` are
missing, as on macOS. It does not make the code believe it runs on macOS: the
macOS-only branches are tested by patching `platform.IS_MAC`. Run both forms
before sending a change that touches `platform.py` or anything that shells out.

After changing a backend, an alias, a node list or a setting, regenerate the
data block of the completion script, or `tests/test_completion.py` fails:

```bash
bin/cluster _complete-data --update
```

`tests/live_check.sh` runs end-to-end checks against the real clusters, with
your own credentials. It decides for each backend from local state alone
(`cluster backends` and the logins on record):

| Backend | What the checks do |
| --- | --- |
| A login is up | Ride it. |
| Credentials set up, no login up | Open a login for the checks (one authentication), and close it at the end. `NO_OPEN=1` skips the backend instead. |
| Credentials missing | Skip it, saying so. |
| Credential refused | Skip it, saying so. A refused credential is never spent on a check; `cluster login` by hand tries it again. |

Everything else rides those masters and uses `--via LOGIN` for transfers, so
the script authenticates again only for the checks that exercise the
connection-open path. Do not run it in a loop: on FASRC, every authentication
spends a 30-second TOTP window, and repeated failures can lock an account.

## Adding a backend

A backend declares what the rest of the tool refuses to guess: how to prove who
you are, which hosts exist and which are reachable, how to address one node,
and which quirks the site imposes. Read `clustertool/backends/base.py`, then
`fasrc.py` and `nersc.py` as the two worked examples.

1. **Create `clustertool/backends/<site>.py`** with a subclass:

   - Subclass `InteractiveTotpBackend` if the site asks for a password and TOTP
     code on every connection. It answers the prompts under a pty and paces
     authentications one per TOTP window (`paces_totp = True`).
   - Subclass `Backend` directly for key or certificate authentication, as
     `NerscBackend` does. Implement `identity_opts()`, and for a cached
     credential, `credential_state()`, `ensure_credential()` and
     `drop_credential()`.

2. **Declare identity and topology:**

   - `name` (the key used in flags, prefixes, state paths, settings sections and
     `CLUSTER_<NAME>_*` variables), `label`, `pool_host` and `node_domain`.
   - `node_classes`: a tuple of `NodeClass` entries from `clustertool/nodes.py`,
     each with `routable`, `purposes` (`LOGIN`, `TRANSFER`, `MOUNT`, `BATCH`),
     and either `hosts` or a `template` and `count`, plus a `note` shown by
     `cluster nodes`. The `NODES` setting replaces the login list at run time.
   - For firewalled nodes, mark the class `routable=False` and implement
     `jump_opts(node)`; override `node_probe_host()` and `node_reachable()` if a
     TCP probe cannot tell you anything.
   - `node_choosable`: `True` if picking a node is free (then override
     `node_candidates_for()`), `False` if the balancer must choose.
   - `mount_via`: `"login"` to mount over the login's own master, or
     `"mount_node"` to use a dedicated `MOUNT` node.
   - `home_remote()`: the path to mount, if the home is not the login shell's
     current directory.

3. **Declare its settings and credentials:**

   - `SETTINGS`: `Backend.SETTINGS` (`CRED_DIR`, `NODES`) plus a `Setting` for
     each value only this site reads, with its built-in value and a one-line
     meaning. `cluster config set` writes these to the backend's own section,
     refuses them for another backend, and `cluster config list` shows them.
   - `CREDENTIALS`: what `cluster init` and `cluster config credentials` ask
     for, in order. The default, username, password and TOTP seed, suits most
     sites; each `Credential` names its file in the credential directory, the
     keys `cluster config set` knows it by, and how a typed value is checked.
   - `enroll_hint`: the site notes printed before those questions. Say where
     each answer comes from: which account the username and password belong
     to, and where the site shows a TOTP token's seed.
   - `enroll_settings`: settings asked for along with the credentials, such as
     NERSC's `COLLAB`.

4. **Declare site behaviour:**

   - `reaps_on_logout`: `True` if the site's logind kills a user's processes
     when the last session ends. This enables the linger machinery, which
     `LINGER` (on by default) controls.
   - `lends_credential`, `agent_identities()` and `agent_identity_seconds()`: set
     these if the credential is a file that can be lent to another cluster for
     direct transfers.
   - `globus_collection`, `globus_session_domain`, `globus_excluded_paths` and
     `globus_path_example` if the site has a Globus collection.

5. **In `__init__`,** call `super().__init__(settings)`, set `self.cred_dir =
   resolve_cred_dir(self.name, self.settings)`, and set `self.user =
   self._require_username()`, which reads the `user` file or
   `CLUSTER_<NAME>_USER` and otherwise says how to set the site up.

6. **Register it:** add the class to `BACKENDS` in
   `clustertool/backends/__init__.py`, and any aliases to `ALIASES`. A login may
   not be named after a backend or an alias, so choose aliases with care.
   Flags, prefixes, settings sections and `BACKEND` validation all read the
   registry. The top-level help in `clustertool/cli.py` names the backends and
   aliases in prose, so update it too, then run
   `bin/cluster _complete-data --update`.

7. **If the site needs a local tool the others do not,** add a row for it to
   `feature_rows()` in `clustertool/diagnostics.py`. `cluster doctor` and
   `cluster init` both show those rows, so a row that is off must say what to
   install, on Linux and on macOS.

8. **Add tests** to `tests/test_backends.py` for anything the backend decides:
   node candidates, jump options, credential state, path exclusions.

Features that assume two particular sites stay with those sites: the NERSC
bridge and companion (`bridge.py`, `companion.py`, `remote/nersc`) are written
for a FASRC hub driving NERSC.

## Documentation

- `README.md` is the front page: what the tool is, how to install it, and where
  to read next. Keep it short.
- `USAGE.md` is the task-oriented reference. When you add or change a command
  or setting, update it, and label example output as an example.
- `docs/setup.md` covers credentials, optional tools on Linux and macOS,
  completion and automation.
- `docs/files.md` lists every file the tool reads or writes. A change that adds
  or moves one updates it.
- `docs/design.md` holds rationale, site behaviour and dated measurements.
- `docs/nersc-bridge.md` covers the bridge and the `nersc` companion.

Write the present state: what the tool does and why, not how it came to do it.

## License

By contributing, you agree that your contributions are licensed under the MIT
License in [LICENSE](LICENSE).
