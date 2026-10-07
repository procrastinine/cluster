# cluster

The goal of the `cluster` tool is to make working on HPC clusters less of a headache. Here is what it helps me do:
- Automates logins so that you don't have to do TOTP every single time
- Keeps track of login nodes and tmux sessions, and keeps connections open
- Can optionally mount cluster home directories over SSHFS for quick viewing/editing of files on clusters
- Auto-heals stuff, auto reconnects
- Easy file transfers to and from the cluster (again without authentication each time)
- I tried to make it as painless as possible, with niceties like tab autocompletions and making the tmux friendly for AI agents
- There is very careful support for the two clusters I use, Harvard FASRC (`fasrc`) and NERSC Perlmutter (`nersc`), including specifics for the login nodes, TOTP retry times, limits, etc. You can also use your own clusters by writing configs.

I currently use it on my free Oracle Cloud VPS (always online and no stupid disconnects), although it should work with macOS as well. For me personally, it has replaced manual SSH logins because it is so much more convenient.

## Basic Features

| Feature | Explanation |
|---|---|
| Named logins | `cluster new work` opens login `work`; names are global, so `cluster where gpu` finds a NERSC login without being told where it lives |
| Node pinning | a login always reconnects to the node its tmux sessions are on, and never moves silently |
| Persistent tmux | sessions are tagged with their login and recorded in the cluster home, so they can be listed, reattached, cleaned up and recovered |
| Self-healing mounts | a watcher probes each sshfs mount, tells busy from wedged, and repairs it without tearing down the login |
| Transfers | `push`/`pull` (rsync) and `transfer` (rclone, `cp`-like) over the open connection; cluster-to-cluster copies that run on a cluster; Globus |
| Looking is free | `ls`, `status`, `where`, `channels` and tab completion never authenticate |
| Careful cleanup | `clean` kills only what it can prove is orphaned; `strays` and `restore-layout` recover sessions nothing is watching |
| One place for settings | `cluster init` and `cluster config`, all kept in `~/.config/cluster/` |
| Relay client | `bin/cluster-relay` gives a laptop the same command line by forwarding every command to the machine running `cluster` |

## Requirements

Required: Linux or macOS, Python 3.8 or newer (standard library only), and the
OpenSSH client (`ssh` and `ssh-keygen`). Clusters should have `tmux`.

Everything else is for optional features. `cluster doctor` shows what is deactivated and what to install:

| Feature | Needs | Linux | macOS |
|---|---|---|---|
| everything | Python 3.8+, OpenSSH | `apt install python3 openssh-client`, or `dnf install python3 openssh-clients` | `xcode-select --install` provides `/usr/bin/python3`; OpenSSH ships with macOS |
| mounts | sshfs and FUSE | `apt install sshfs`, or `dnf install fuse-sshfs` | macFUSE and SSHFS; see [docs/setup.md](docs/setup.md#mounts-on-macos) |
| `push`, `pull`, the NERSC bridge | rsync | `apt install rsync`, or `dnf install rsync` | ships with macOS; `brew install rsync` adds an overall progress line |
| `transfer`, archive-sync | rclone 1.64 or newer | the official binary from rclone.org | `brew install rclone`, or the official binary |
| Globus transfers | globus-cli | `pipx install globus-cli` | `pipx install globus-cli` |
| linger shutdown hook | systemd | most distributions have it | not available; see [docs/setup.md](docs/setup.md#automation) |
| restoring logins after a reboot | cron, or a LaunchAgent | cron | a LaunchAgent; see [docs/setup.md](docs/setup.md#automation) |

## Install and first run

Just install from here:

```bash
git clone https://github.com/procrastinine/cluster.git ~/cluster
~/cluster/bin/cluster init
```

`cluster init` is a guided setup that you can run again at any time. It checks
this machine, asks which clusters you use and for each one's username, password
and TOTP seed, shows the current code so you can compare it with your
authenticator app, and saves everything at once. Then it says which optional
parts work here and offers the further steps: the `PATH` link, the completion
line, and a test login or certificate. Nothing contacts a cluster unless you
answer yes to a question that says it will; those questions default to no.

To set up or change one cluster on its own:

```bash
cluster --fasrc config credentials
cluster --nersc config credentials
cluster doctor                       # check everything, the network included
```

Where each site's password and TOTP seed come from is in
[docs/setup.md](docs/setup.md#credentials).

## Quickstart

```bash
cluster new work                     # open login "work" and attach tmux session "work"
cluster ls                           # every login and session, on every cluster set up here
cluster push work ./data projects/   # rsync ./data into ~/projects/ on the cluster
cluster close work                   # close the login and its sessions
```

Detach from tmux as usual (`Ctrl-b d`). The session keeps running while the
login is open, and `cluster attach work` brings you back. With both clusters set
up, `cluster new gpu --nersc` opens a login on NERSC; `--fasrc` and `--nersc`
choose the cluster, and the default is the only one set up, else `fasrc`
(`cluster config set BACKEND nersc` changes it). On FASRC, sessions also
outlive the login's connection, because `cluster` keeps linger on there; see
[USAGE.md](USAGE.md#keeping-tmux-alive-after-you-disconnect-fasrc).

## AI agents (optional)

[`skills/cluster/`](skills/cluster/SKILL.md) is an [Agent Skills](https://agentskills.io)
skill that teaches a coding agent (Claude Code, Codex, Pi and others) to use
`cluster`: what costs an authentication, which commands need your yes first,
and how to run and read sessions without a terminal. Install it if you want
agents to drive your clusters:

```bash
npx skills add procrastinine/cluster -g     # asks which agents to install it for
```

Or link it by hand, so it stays current with `git pull`:

```bash
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s ~/cluster/skills/cluster ~/.agents/skills/cluster   # Codex, Pi and others
ln -s ~/cluster/skills/cluster ~/.claude/skills/cluster   # Claude Code
```

## What/Where

| What | Where |
|---|---|
| settings | `~/.config/cluster/settings.ini` (`cluster config path`) |
| credentials | `~/.config/cluster/credentials/<backend>/` |
| state and logs | `~/.local/state/cluster/` |
| control sockets | `~/.ssh/controlmasters/` |
| mounts | `~/cluster_mounts/<backend>/<login>/` |
| on each cluster | `~/.cluster/` in your cluster home, and a marked block in `~/.tmux.conf` once you run `cluster setup` |

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` move the first three. Every file the tool
reads or writes, here and on the clusters, is listed in
[docs/files.md](docs/files.md).

## Security

Secrets are stored in `~/.config/cluster/credentials/` and only sent to their corresponding backend. This is not some sort of encryption manager so do your due diligence...

## Documentation

- [USAGE.md](USAGE.md): every command and setting, by task.
- [docs/setup.md](docs/setup.md): credentials for each site, optional tools on
  Linux and macOS, tab completion, and running `cluster` from cron or launchd.
- [docs/files.md](docs/files.md): every file the tool reads or writes.
- [docs/design.md](docs/design.md): design rationale, site behaviour and dated
  measurements.
- [docs/nersc-bridge.md](docs/nersc-bridge.md): the bridge that lets jobs and
  agents on FASRC drive NERSC.
- [extras/README.md](extras/README.md): `archive-sync`, an optional encrypted
  rclone backup of a cluster home.
- [CONTRIBUTING.md](CONTRIBUTING.md): source layout, tests, adding a backend.

## License

MIT. See [LICENSE](LICENSE).
