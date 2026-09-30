# Backends and their types

Everything `cluster` does (logins, pins, tmux, mounts, transfers, the watcher)
is written once, against the interface in `clustertool/backends/base.py`. A
backend answers what that code refuses to guess: how to prove who you are,
which hosts exist and how to reach each one, and what the site does to your
processes. This page is for whoever needs a backend the built-in ones do not
cover. For a host ssh already reaches, none is needed: see
[A backend of your own](../USAGE.md#a-backend-of-your-own).

## Profiles and types

A **type** is a class of hooks, a subclass of `Backend`. A **profile** is what
every command names: a type under a name, which its settings section, state
directory, control sockets and credential directory are all kept by.

| Type | Module | Profiles |
|---|---|---|
| `ssh` | `backends/ssh.py` | any section of `settings.ini` with `TYPE = ssh` |
| `fasrc` | `backends/fasrc.py` | the built-in `fasrc` (`--fasrc`, `--fas`) |
| `nersc` | `backends/nersc.py` | the built-in `nersc` (`--nersc`, `--perlmutter`) |
| a file of your own | anywhere | any section with `TYPE = /path/to/type.py` |

A site type such as `fasrc` sets `name` on its class, and that makes it one
built-in profile with the short forms `--NAME` and `NAME:PATH`. Every other
profile is named with `--backend NAME`, because a `--NAME` taken from every
command line would take options from other commands. `clustertool/backends/__init__.py`
reads the profiles from the settings file whenever it changes, and a section
that cannot be one is warned about once and left out.

## A type of your own

A type in a file is loaded by the profile that names it:

```ini
[lab]
TYPE = ~/.config/cluster/types/lab.py
HOST = lab-login
```

A relative path is read from `~/.config/cluster/`. The file must be yours and
not writable by anyone else, since `cluster` runs it as you, and it defines
`BACKEND`:

```python
from clustertool.backends.ssh import SshBackend


class LabBackend(SshBackend):
    """The lab cluster: the ssh type, plus the lab's own quirks."""

    # The lab's logind ends every process of a user at the last logout.
    reaps_on_logout = True

    def home_remote(self):
        return "/home/lab"      # the mount root, where '.' is not the home


BACKEND = LabBackend
```

Subclass `SshBackend` for a site ssh can already authenticate to on its own, and
override only what differs. Subclass `Backend` or `InteractiveTotpBackend` when
the site needs the tool to authenticate for you. The type's name, as `cluster
backends` shows it, is `type_name`, or the file's name without `.py`. A setting
the type declares in `SETTINGS` is written to the profile's section by `cluster
config set`, as for any backend.

## The hooks

Each hook has a default, so a type overrides only what its site needs. The
docstrings in `base.py` are the reference; this is the map.

**Identity**

| Hook | What it answers | Default |
|---|---|---|
| `name`, `type_name`, `aliases`, `shorthand` | the profile's name, the type's, and the short forms of a built-in profile | set by the registry for a profile |
| `label`, `LABEL` | the name shown | the class's `label`; the `LABEL` setting wins |
| `user`, `local_username()`, `is_configured()` | who you are there, and whether this machine knows enough to use it | the `user` file in the credential directory |
| `CREDENTIALS`, `enroll_hint`, `enroll_settings` | what `cluster init` and `config credentials` ask for, and the notes said before | username, password and TOTP seed |
| `setup_command()`, `credentials_command()`, `setup_steps()`, `cli_flag()` | the commands messages name: to set it up, to check what was refused, to try next | `config credentials` with the profile's flag |
| `first_check`, `login_cost` | what `cluster init` offers once it is set up: a test login, or fetching a credential | a test login |

**Credentials**

| Hook | What it answers | Default |
|---|---|---|
| `credential_state()` | ok, ready, expiring or missing, and why | ok |
| `ensure_credential()`, `drop_credential()` | fetch or remove a cached credential (a certificate) | nothing to do |
| `records_refusals`, `refusal_holds_connections()` | whether a refusal goes on the shared record that holds unattended connections (`state.Refusals`) | no; yes for the password types and `ssh` |
| `credential_marks()` | what tells the credential presented from another, without reading it, so a change ends a refusal's hold | a stat of the `CREDENTIALS` files |
| `credential_refused()`, `credential_accepted()` | replace a refused credential once, and hear that one worked | nothing |
| `lends_credential`, `agent_identities()`, `agent_identity_seconds()` | a key file another cluster's ssh may borrow for a direct transfer | nothing to lend |

**Connecting**

| Hook | What it answers | Default |
|---|---|---|
| `common_opts()`, `identity_opts()`, `jump_opts(node)` | ssh's options: shared ones, how to identify, how to reach a firewalled node | `-F SSH_CONFIG`, the username, the timeouts |
| `host_for(node)`, `target(node)` | where ssh connects for a node, which need not be the name the node answers to | the node's own name, or `pool_host` |
| `ssh_argv()`, `run_ssh()`, `exec_interactive()` | the ssh command, and running it (under a pty that answers prompts, when `interactive_auth`) | plain ssh |
| `interactive_auth`, `paces_totp` | the tool types a password per connection, one per TOTP window | no |
| `by_hand` | set while a person is connecting at a terminal: a refusal is tried again, and a prompt may be shown | set by the command |
| `pin_hint(pinned, landed)` | what to say when a pinned login lands on another node | retry, or repin |

**Nodes**

| Hook | What it answers | Default |
|---|---|---|
| `node_classes`, `configured_node_classes()` | the classes of node, what each is for, which are routable (`clustertool/nodes.py`) | none; `NODES` replaces the login list |
| `pool_host`, `node_domain`, `fqdn()`, `short()` | the balancer's address, and node names in full or short | |
| `node_choosable`, `node_candidates_for()` | whether a node is picked up front (free) or left to the balancer | the balancer |
| `node_probe_host()`, `node_reachable()`, `reach_host()` | what a TCP probe can tell, for a node and for the site; None when only a connection can | port 22 of the node, and of `pool_host` |
| `transfer_nodes()`, `mount_nodes()`, `inbound_transfer_hosts()`, `mount_via`, `home_remote()` | where bulk I/O runs, where a mount lives, what another cluster dials | the login nodes; mount over the login |

**What the site does**

| Hook | What it answers | Default |
|---|---|---|
| `reaps_on_logout` | logind ends your processes at the last logout, so tmux needs linger | no |
| `companion_drives` | the `nersc` companion drives it from elsewhere, so `cluster setup` installs nothing there | no |
| `globus_collection`, `globus_session_domain`, `globus_excluded_paths`, `globus_path_example` | the site's Globus collection and what it refuses | none |
| a type's `SETTINGS` | its own settings, and its defaults for shared ones (the ssh type's `AUTO_MOUNT` is 0) | `CRED_DIR`, `NODES`, `LABEL` |

## Shipping a type with the tool

A type every user of a site would want belongs in `clustertool/backends/`, with
a built-in profile of its own; `CONTRIBUTING.md` has the steps.
