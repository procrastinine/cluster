# Files

Every file `cluster` reads or writes: on this machine, on each cluster, on a
bridge hub, on NERSC scratch, and on a relay host. Paths are the defaults. The
settings named in the right-hand column move them, and `XDG_CONFIG_HOME` and
`XDG_STATE_HOME` replace `~/.config` and `~/.local/state`.

`<backend>` is `fasrc` or `nersc`, `<login>` a login name, `<node>` a node's
short name such as `holylogin05`, and `<tag>` a transfer connection's tag.

## On this machine

### Configuration: `~/.config/cluster/`

The one place for settings and credentials. The directory is mode 700.

| Path | What it holds | Moved by |
|---|---|---|
| `settings.ini` | every setting you change, in `[global]`, `[fasrc]`, `[nersc]`, `[relay]` and a section for each backend of your own (its `TYPE` and `HOST`); written by `cluster init`, `cluster config set` and `cluster backends add`, with comments kept | `XDG_CONFIG_HOME` |
| `.settings.ini.lock` | taken while a command writes `settings.ini` | |
| `credentials/<backend>/user` | your username on that cluster | `CRED_ROOT`, or `CRED_DIR` for one backend |
| `credentials/<backend>/pass` | your password, mode 600 | same |
| `credentials/<backend>/key.txt` | the base32 TOTP seed, mode 600 | same |
| `companion/config`, `companion/mirror.exclude` | the NERSC companion's settings when it runs here (`cluster nersc-tool install-local`) | |
| `archive-sync.conf`, `archive/<backend>-home.filter` | archive-sync's settings and filters; see [extras/README.md](../extras/README.md#files) | `ARCHIVE_SYNC_CONFIG` |

The credential directories are mode 700 and their files mode 600. `cluster
doctor` checks both and lists anything else it finds there.

### State: `~/.local/state/cluster/`

Mode 700. Everything here can be rebuilt, except that a pin is how a login finds
its sessions again; `cluster forget` removes a backend's records on purpose.
Logs are rotated at 1 MiB, keeping one previous copy as `<log>.1`.

| Path | What it holds |
|---|---|
| `<backend>/<login>.json` | the login's record: its node and its mount |
| `<backend>/<login>.node` | the node the login is pinned to |
| `<backend>/<login>.mountnode` | the node the login's mount uses, when that is not the login's own node |
| `<backend>/login-<login>.lock` | taken while the login's connection is opened or torn down |
| `<backend>/master-<login>.log`, `master-mnt-<login>.log` | ssh's output from the login's connection and from its mount's own connection |
| `<backend>/sshfs-<login>.log` | sshfs's output; on macOS with FSKit, sshfs runs in the foreground and appends here for as long as the mount lives |
| `<backend>/watch-<login>.log`, `.pid`, `.lock` | the watcher's log, its process id, and the lock that keeps it to one per login |
| `<backend>/transfer-<tag>.lastnode` | the transfer node that last worked for connection `<tag>`, tried first next time, for example `transfer-pool.lastnode` |
| `<backend>/transfer-<tag>.users`, `transfer-<tag>.lock` | who holds a lease on a kept transfer connection, and the lock around it |
| `<backend>/master-xfer-<tag>.log` | ssh's output from a transfer connection |
| `<backend>/list-evidence.cache`, `list-evidence.lock` | what `ls` last learned from each node, drawn first in the next table |
| `<backend>/sessions.cache` | session names for tab completion |
| `<backend>/nodes.seen` | nodes that have held your sessions, which `clean` visits |
| `<backend>/abandoned.tsv`, `abandoned.lock` | sessions left running on purpose (`--abandon`), for `clean` |
| `<backend>/totp.window`, `totp.lock` | the last TOTP window used, so no code is sent twice (FASRC) |
| `nersc/sshproxy.lock`, `nersc/sshproxy.window` | held while a NERSC certificate is fetched, so one fetch runs at a time and a second uses what the first installed; the TOTP window the last fetch used, so no code is sent twice |
| `<backend>/probe-<login>-*.result` | a mount probe's answer; removed once read |
| `nersc/bridge.record`, `nersc/bridge.lock` | the last bridge push (when, to which login and node, until when its certificate is valid, and whether the end-to-end check passed), and the lock that keeps `--cron` pushes from overlapping |
| `workstation` | this machine's ID, made once (`WORKSTATION` overrides it); see [USAGE.md](../USAGE.md#using-more-than-one-machine) |
| `setup-local-health.json` | when `cluster setup` last checked this machine, and what it found |
| `setup-backups/` | each VS Code settings file as it was before `cluster setup` changed it |
| `relay-<host>-<pid>.conf` | a relay transfer's rclone configuration, mode 600, removed when it ends; one left by a killed transfer is removed by the next |
| `companion/` | the NERSC companion's state when it runs here |
| `nersc-tool-installed.json`, `nersc-tool-sync.lock` | what the bridge last installed on each hub, and the lock around a sync |
| `nersc-tool-backups/`, `nersc-tool-conflicts/` | companion sources replaced by a sync, and hub edits held back as conflicts |
| `archive-sync/` | archive-sync's logs, fences, stamps and locks; see [extras/README.md](../extras/README.md#files) |

`STATE_ROOT` moves the whole tree.

### Elsewhere on this machine

| Path | What it holds | Moved by |
|---|---|---|
| `~/.ssh/controlmasters/cl-<backend>-<login>.sock` | a login's control socket: an authenticated connection, so the directory is mode 700 | `CTL_DIR` |
| `~/.ssh/controlmasters/cl-<backend>-mnt-<login>.sock` | a mount's own connection, when it uses another node | `CTL_DIR` |
| `~/.ssh/controlmasters/cl-<backend>-xfer-<tag>.sock` | a transfer connection | `CTL_DIR` |
| `~/cluster_mounts/<backend>/<login>/` | a mounted cluster home; the root is made, mode 700, by the first mount | `MOUNT_ROOT` |
| `~/.ssh/nersc`, `~/.ssh/nersc-cert.pub`, `~/.ssh/nersc.pub` | the 24-hour NERSC key and certificate, mode 600 | `KEY` (NERSC) |
| `~/.ssh/known_hosts` | host keys: new FASRC and NERSC hosts are added on first contact, and NERSC's `@cert-authority` line is added once | |
| `~/.ssh/config` | not read for `fasrc` and `nersc`, which pass `-F /dev/null`; read by an ssh backend (`cluster backends add`) | `SSH_CONFIG` |
| `~/.local/bin/cluster` | the link `cluster init` offers | |
| `~/.local/bin/nersc` | the link `cluster nersc-tool install-local` makes | |
| VS Code settings (`~/.vscode-server/data/Machine/settings.json` and `~/.config/Code/User/settings.json` on Linux, `~/Library/Application Support/Code/User/settings.json` on macOS) | `cluster setup` adds the mount root to `files.watcherExclude` in each that exists | `VSCODE_SETTINGS` |
| `/etc/systemd/system/cluster-linger.service`, or `~/.config/systemd/user/cluster-linger.service` | the shutdown hook `cluster linger --install-hook` installs (Linux) | |
| your crontab | read by `cluster doctor`, to look for an `@reboot` line that runs `cluster boot`; never written | |

Short-lived files: a file written whole goes through a temporary `.<name>.*`
beside it and is renamed into place; a transfer of several files from
one directory writes its file list to the temporary directory; a direct
cluster-to-cluster transfer runs a private ssh-agent whose socket is in the
temporary directory; `cluster doctor` tests file locking with a `.locktest-*`
file in the state directory; and on macOS a download first tests whether its
destination folds case with a `.cluster-Case-*` file there.

## On each cluster

All of it in your home directory there, which every login node of a cluster
shares.

| Path | What it holds |
|---|---|
| `~/.cluster/sessions/<node>/<session>` | a breadcrumb for each session `cluster` created, naming the login that owns it and, after a tab, `ws=` and the workstation that made it; how `ls`, `clean`, `strays` and `restore-layout` know a session exists when nobody is watching it |
| `~/.cluster/layout/<node>` | the watcher's snapshot of that node's sessions, windows and working directories, every `LAYOUT_INTERVAL` seconds |
| `~/.cluster/linger/<node>` | FASRC: a note that `cluster` enabled linger on that node, so only linger it enabled is ever released |
| your crontab on a login node | FASRC, with `LINGER_KEEPER` on: one line marked `cluster-linger-keeper`; nothing else in the crontab is touched |
| `~/.tmux.conf` | `cluster setup`: a marked block merged in, validated first; the previous file is kept as `~/.tmux.conf.cluster-backup-<hash>` |
| `.cluster-probe-*` at the top of a mounted home | a mount's health probe: a directory made and removed at once |
| `~/.ssh/known_hosts` on FASRC | a direct transfer adds the NERSC host it connects to on first contact |

Each tmux session also carries two user options: `@cluster_login`, naming its
owner, and `@cluster_workstation`, naming the machine that made it. They live
in the tmux server, not in a file.

## On a bridge hub

A FASRC login node running the NERSC companion; see
[nersc-bridge.md](nersc-bridge.md).

| Path | What it holds |
|---|---|
| `~/.ssh/nersc-bridge`, `~/.ssh/nersc-bridge-cert.pub` | the 24-hour NERSC key and certificate, mode 600 |
| `~/.ssh/known_hosts` | NERSC's `@cert-authority` line |
| `~/.local/bin/nersc` | the companion, mode 755 |
| `~/.config/nersc/config`, `~/.config/nersc/mirror.exclude` | its settings and code-mirror excludes, written only when missing |
| `~/.local/state/nersc/` | the last sync time, the DTN it last used, and the return loop's records and lock |
| `$XDG_RUNTIME_DIR/nersc/`, else `/tmp/nersc-<uid>/` | the companion's connection socket, lock and log, mode 700 |
| `<dest>.nersc-part` | a returned run directory while it is still arriving |

## On NERSC

| Path | What it holds |
|---|---|
| `$PSCRATCH/.nersc-return/queue/<jobid>.json`, `done/` | the companion's return registry: runs waiting to come back, and those that have (`return_queue` moves it) |
| `~/<mirror_dest>` | the companion's code mirror |
| `~/.cluster/` | the same session records as on any cluster |

## On a relay host

The machine that runs `cluster` for a laptop with `bin/cluster-relay`.

| Path | What it holds |
|---|---|
| `~/.cache/cluster-relay/` | staged transfers, for the transfers that cannot stream; a staging directory left for more than a day is removed by the next staged transfer |

On the laptop, the client reads the `[relay]` section of its own
`~/.config/cluster/settings.ini` and writes nothing else, apart from the files a
download delivers.
