# Using cluster

This guide is organised by task. `cluster --help` lists every command, and
`cluster COMMAND --help` explains one; asking for help never runs anything.
Setting up a machine is in [docs/setup.md](docs/setup.md), every file the tool
touches is in [docs/files.md](docs/files.md), and design rationale and
measurements are in [docs/design.md](docs/design.md).

## The shape of a command

```
cluster [--backend NAME] COMMAND [ARGS]
```

The first word is always a command. There is no bare `cluster NAME` form:
`cluster work` is an error that suggests `cluster attach work`. Frequent verbs
have short aliases, listed in `cluster --help`: `a` attach, `n` new, `l` login,
`ls` list, `w` where, `ss` sessions, `r` run, `sh` shell, `m` mount, `st`
status, `k` kill-session, `ch` channels, `cp` transfer.

### Login names are global

A login is one named SSH connection to one node of one backend. No two backends
may have a login with the same name, and a login may not be named `fasrc`,
`nersc` or one of their aliases. A name is up to 32 characters of letters, digits, `.`, `_`
and `-`, starting with a letter or digit, and two names that differ only in
case are refused, because they would share files on a case-insensitive disk.
A control socket's path is limited too (103 bytes on macOS, 107 on Linux); a
name that would overrun it is refused with the fix, a shorter name or a shorter
socket directory (`cluster config set CTL_DIR ~/.ssh/cm`). A name is therefore
enough to find a login:

```bash
cluster where work      # a FASRC login
cluster where gpu       # a NERSC login; no backend flag needed
```

You name a backend only to create a login somewhere other than the default
backend, or to narrow a fleet-wide command such as `ls`, `status`, `close --all`
or `clean` to one backend. The default is the only backend set up on this
machine, else `fasrc`; `cluster config set BACKEND nersc` changes it. These are
equivalent:

```bash
cluster login gpu --nersc              # a --BACKEND flag, anywhere before any --
cluster nersc:login gpu                # a prefix on the verb
cluster --backend nersc login gpu      # must come first
CLUSTER_BACKEND=nersc cluster login gpu
```

The alias `fas` means `fasrc`, and `perlmutter` means `nersc`. A flag after
`--` belongs to the remote command: `cluster run work -- echo --nersc` prints
`--nersc`. A [backend of your own](#a-backend-of-your-own) is named with
`--backend NAME` or `CLUSTER_BACKEND` only: the short forms would take its name
from every command line.

Naming a backend that disagrees with where a login lives is an error. To resolve
a clash, rename one login; the connection and its sessions carry over, so this
costs no reauthentication:

```bash
cluster rename work work-old
```

While other commands ride the login (an `attach`, a `run`, a transfer), it is
renamed only with `--force`: they know it by its old name, so their next
connection fails. `rename` names them.

### Questions, and `-y`

A command that is about to destroy something asks first: `repin` and `unpin`
before closing sessions, `forget` before dropping local state, `strays` before
visiting or changing a node, and `transfer --sync` before deleting files. `-y`
answers yes. Without a terminal, as under cron, a command that would ask
refuses instead and changes nothing, unless it was given `-y`. `clean` is the
exception: unattended, it keeps the stale records it would have asked about and
finishes the sweep.

## Logins and sessions

A **login** is an authenticated, node-pinned SSH control master that `cluster`
keeps open on your workstation. A **session** is a tmux session on that login's
node. It keeps running when you detach or close your terminal, because the
login's connection stays open.

```bash
cluster new work            # open login "work" and attach tmux session "work"
cluster new work api        # tmux session "api" on login "work"
cluster new -d work api -- ./serve.py   # start it running, detached
cluster attach work api     # come back later
cluster sh work             # plain shell over login "work"; the login stays open
cluster sh                  # disposable direct shell: no master, pin, mount or state
cluster login work          # open the connection only
```

- One name follows `NEW_LOGIN_MODE`: `tmux` (the default) opens the same-named
  session, and `shell` opens a plain shell instead. Two names always mean tmux.
  `new-session LOGIN SESSION` (aliases `task`, `session`) is the explicit form.
- `-c DIR` starts the session in a remote directory, and `--no-mount` skips the
  automatic mount. `--here` creates or attaches a session even when that name
  is recorded on another node.
- On FASRC, a new login may wait up to 30 seconds for a TOTP window whose code
  has not been used, because each code is accepted once; a window with less
  than 8 seconds left is skipped. On NERSC it is instant once the day's
  certificate exists.
- Without a terminal (in a script, for example), the session is created and left
  detached.

Detach from tmux as usual (`Ctrl-b d`).

### Which session `cluster attach LOGIN` means

When you do not name a session, `attach` takes the one named after the login;
otherwise the only one, if there is exactly one; otherwise it lists them and
asks, without guessing. If there are none, it starts one named after the login.
So if login `gpu` keeps its work in `train`, `cluster attach gpu` goes to
`train`.

A shell with no tmux (`cluster sh`) carries no ownership tag and leaves no record
in the cluster home. `ls` and `clean` cannot see it, and it dies with its
connection. Use tmux for anything you would mind losing.

### Running commands and driving sessions

Anything that takes a remote command needs an explicit `--`:

```bash
cluster run work -- hostname                # run once, print the output
cluster run work -- sh -c 'ls ~/projects'   # run quotes its arguments; use sh -c for ~ and globs
cluster send work api -- make test          # type a line into a running session
cluster window work api build               # add window "build" to session "api"
cluster sessions work                       # sessions, windows and attached clients
cluster kill-session work api               # alias: k
```

`kill-session` matches the name exactly; if nothing has that name, it kills
nothing and suggests the closest match. A kill that cannot be confirmed leaves
its records in place.

### Closing, moving and renaming logins

```bash
cluster close work                 # close the connection and kill its sessions
cluster close work --keep-tmux     # leave the sessions, keep the pin
cluster close work --abandon-tmux  # leave the sessions, record them for clean, drop the pin
cluster close --all                # every login, on every backend unless you name one
```

On FASRC, sessions survive the end of the connection because `cluster` keeps
linger on; see [Keeping tmux alive](#keeping-tmux-alive-after-you-disconnect-fasrc).

A login is pinned to its node because its sessions are node-local. A move cannot
take them along, so `repin` and `unpin` close them as part of the move, after
warning you and asking:

```bash
cluster pin                               # which node each login is pinned to
cluster repin work holylogin07            # asks before closing sessions; -y skips the prompt
cluster repin work holylogin07 --migrate  # rebuild session names, windows and cwds there
cluster repin work holylogin07 --abandon  # leave the old sessions, tracked for clean
cluster unpin work                        # the next connection takes a fresh node
cluster refresh work                      # move to a fresh node now
```

`--migrate` does not restart commands. If the old node is unreachable, the move
still happens, the sessions are recorded from the cluster home, and the command
exits non-zero. `refresh` restores the login's mount and watcher on the new
node. `repin` writes the pin without connecting, which is also how you steer a
FASRC login onto a node of your choice.

## Seeing what is there

```bash
cluster ls            # every login and its sessions on every backend set up here (-q: no sessions)
cluster where work    # one login: node, sessions, tmux clients, your SSH clients there
cluster status        # ls plus credential, mount and breadcrumb detail
cluster doctor        # this machine, credential modes, every login
cluster nodes         # the backend's node classes and what each is for
cluster backends      # known backends and credential state
cluster channels work # how much of the login's SSH channel budget is free
```

Example output of `cluster ls`:

```
BACKEND  LOGIN  STATE   NODE         PINNED       MOUNT              SESSIONS
fasrc    work   active  holylogin06  holylogin06  mounted            api, work
fasrc    build  active  boslogin07   boslogin07   shares work        build
nersc    gpu    active  login17      login17      mounted via dtn02  train
```

None of these commands authenticate. `ls` asks each live login for its node and
sessions in one round trip, all at once; in a terminal it draws the last saved
table first and replaces it as the replies arrive. For a login that is down,
`cached: NAME, ...` is what a live query last found, and `none` means a live
query found nothing. `shares work` means the backend is already mounted for
another login. Tables fit the terminal; piped output is never trimmed, and
`COLUMNS=200 cluster ls` forces the wide layout.

### Channels

A login is one SSH connection, and the server caps a connection at a fixed
number of channels (sshd's `MaxSessions`, 10 by default; `SSH_MAX_SESSIONS`
here). Every open `attach`, the mount and each sftp connection of a transfer
share them. When they run out, the server says only `channel N: open failed:
connect failed: open failed`, and the login still looks healthy.

Example output of `cluster channels work`:

```
work: 8 of ~10 channels in use, 2 free
   5 x interactive session      pid 2902569 2902712 2902848 3993301 4104036
   2 x sftp (rclone/transfer)   pid 4168760 4169296
   1 x sshfs mount              pid 4111801
```

It reads the local process list and costs nothing. `--free` prints only the
number, for scripts.

### Finding cluster's processes (Linux)

Long-running processes rename themselves, so `ps`, `pgrep` and `top` show
`cluster:gpu/api` for an attach to session `api` on login `gpu`, and
`cluster:w:gpu` for its watcher. The kernel allows 15 characters, so the login
name is shortened first: `cluster a work train` shows as `cluster:w/train`. On
macOS, read the command line in `ps aux` instead.

## Files

### Mounts

A login's home can be mounted at `~/cluster_mounts/<backend>/<login>/`:

```bash
cluster mount work       # also happens automatically before interactive work
cluster mounts           # list managed mounts
cluster umount work
cluster repair work      # run the repair ladder once, by hand
cluster watch work       # start the background watcher (unwatch stops it)
```

Every login node of a backend serves the same home, so a backend is mounted
once, and other logins show `shares LOGIN`. An explicit `cluster mount` is still
honoured, with a warning. On NERSC, mounts use a DTN.

Mounts need sshfs (and macFUSE on macOS; see
[docs/setup.md](docs/setup.md#optional-tools)). Without it, mounts are off: a
new connection says so once, `cluster mount` stops with what to install, and
everything else works. `cluster config set AUTO_MOUNT 0` stops new logins from
trying.

The watcher probes each mount, tells a busy mount from a wedged one, and repairs
a wedged one: first by remounting over the same connection, then by moving the
mount to another node, and only then by rebuilding the login. It also saves a
tmux layout snapshot every `LAYOUT_INTERVAL` seconds.

Keep editors and indexers out of the mount root. In VS Code, exclude it in
`files.watcherExclude` and `search.exclude`, and turn off
`search.followSymlinks`, or a search can walk lab filesystems over sshfs.
`cluster setup` adds the watcher exclusion; the other two are yours to set.

### push and pull (rsync)

`push` and `pull` run rsync over the login's connection. Remote paths are
relative to your cluster home, with no `host:` prefix. With no login named, the
`DEFAULT_LOGIN` (normally `main`) is used. Extra options go to rsync.

```bash
cluster push work ./data projects/     # ./data -> ~/projects/data
cluster pull work results/ ./out/      # the contents of ~/results -> ./out/
```

The login is opened first if it is not up. If its connection is lost on the
way, it is restored and rsync run again, which skips what already arrived and
finishes a file it had begun (see
[When something is slow or stops answering](#when-something-is-slow-or-stops-answering)).
A file cut short waits in a `.rsync-partial` folder beside where it goes, never
in its place, and rsync leaves that folder out of the copy, `--delete` included,
unless the options say how to treat one (`--inplace`, `--append`, a
`--partial-dir` of their own). An rsync without `--partial-dir` keeps it in
place (`--partial`), and one whose `rsync --help` fails or complains, as
openrsync may, is asked for neither. A transfer that read standard input
(`--files-from=-`, `--exclude-from=/dev/stdin`, a filter merging `-`) is not run again,
since the next run would find it empty: no list of files, or, under
`--delete`, no excludes to protect what they named.
On macOS, a pull into a folder that folds case (the default) first lists the
source, and refuses, listing them, if names in it differ only by case or
Unicode normalisation, since they would land on one file.

### transfer (rclone)

`transfer` (aliases `cp`, `copy`, `xfer`) uses rclone over sftp and behaves like
`cp`. Prefix the remote side with a login, backend or alias name, or with
`remote:` for the current backend:

```bash
cluster transfer ./data work:projects/            # upload to login work's cluster
cluster transfer nersc:results ./out/             # download from NERSC
cluster transfer --via work ./data remote:projects/   # ride login work's connection
cluster transfer a.py b.slurm work:jobs/          # several sources, one directory
```

- A trailing slash on the source, or `--contents`, copies what is inside it.
- Several sources share one connection and land in the destination directory;
  the batch stops at the first failure. `--sync` with several sources is
  refused.
- `--sync` deletes extras at the destination, after asking. `--move` deletes each
  source file once it has arrived. `--dry-run` shows the plan.
- `--up`/`--down` force the direction. `-L`, `-l` and `--skip-symlinks` choose
  how symlinks are handled. Other rclone flags pass through.
- Without `--via`, a transfer opens its own connection so it does not compete
  with your sessions for channels. On FASRC that costs a TOTP window, so `--via`
  is cheaper for small jobs. `--keep` leaves the connection open; `cluster
  transfer --close TAG` closes that one, and a bare `--close` closes every open
  one.
- On NERSC the connection goes to a DTN. A DTN that fails to open costs one
  attempt and the next is tried, and the last one that worked is tried first
  next time. `--node` names one.
- A source that does not exist stops the transfer before anything is copied.
  A source that cannot be checked (as opposed to one that is not there) is
  asked about `TRANSFER_PROBE_TRIES` (3) times, then copied as a directory,
  with a warning, rather than renamed onto the destination. After rclone
  reports success, the destination is checked for what was sent.
- A connection lost mid-transfer is restored and the transfer resumed; what
  arrived whole is not sent again. A file that was half-sent may leave an
  rclone `.partial` file behind on the side it was going to.
- When both sides name the same cluster, a plain copy runs there as `cp -a`,
  and `~/x` means your home there. `--sync`, `--move` and filters are refused,
  with the command to run there.

A glob on the cluster side is not expanded; use `--include` and `--exclude`.
`transfer` needs rclone 1.64 or newer; `cluster doctor` says which one it
found, and [docs/setup.md](docs/setup.md#rclone) says where it looks.

### Giving another tool a connection

`ssh-command` prints an ssh command line that rides a login's master, as
shell words. rclone reads `--sftp-ssh` its own way, so `--rclone` prints the
line for it:

```bash
rclone lsf ":sftp,shell_type=unix:projects" \
  --sftp-ssh "$(cluster ssh-command work --quiet --rclone)"
```

`--transfer` uses a dedicated connection on a transfer node instead, the same
one `transfer` uses (tag `pool`, or the node's name with `--node`), and names
its TAG on stderr. The
calling script holds a lease on it until the script's process group exits or
it runs `cluster transfer --close TAG`. A `--close` from anywhere else leaves a
leased connection open and names who holds it; `--close TAG --force` closes it
anyway, with a warning. A lease has no time limit.

## Between two clusters

Name a cluster on both sides and the copy goes cluster to cluster:

```bash
cluster transfer fasrc:~/runs/big nersc:/pscratch/sd/u/user/
cluster transfer nersc:~/out/x.h5 fasrc:~/analysis/x.h5
cluster transfer --engine relay fasrc:~/a nersc:~/b     # force the slow path
```

| engine | who moves the bytes | use it for |
|---|---|---|
| `direct` | rclone on one cluster, dialling the other | the default; your workstation only supervises |
| `relay` | rclone on your workstation | when neither cluster can authenticate to the other |
| `globus` | the Globus service | very large, restartable moves on lab or scratch filesystems |

For `direct`, FASRC runs the copy and borrows the NERSC certificate through a
forwarded ssh-agent that holds only that key and dies with the transfer (see
[docs/design.md](docs/design.md#cross-cluster-transfers)). It opens its own
FASRC connection, so `--via` does not apply, and it uses the executing
cluster's own settings. `--no-agent-forward` forces `relay`. The executing
cluster needs rclone 1.64 or newer; if the one on its `PATH` is missing or too
old, load a module that provides one and name it:

```bash
cluster --fasrc config set REMOTE_RCLONE /path/to/rclone
```

The default engine, `auto`, falls back to `relay` when `direct` cannot be set up
(every DTN down, a firewall, a refused credential, no rclone on the executing
cluster, no local ssh-agent), and says why. It never falls back when you chose
`--engine`, or once bytes are moving. Both engines take their concurrency,
retries and I/O timeout from the `TRANSFER_*` settings (`SHARED_TRANSFER_*`
for `relay` under `--via`), give up on a peer that does not answer within
`PEER_CONNECT_TIMEOUT` seconds, and check the destination after rclone reports
success.

### Globus

The backends declare their Globus collections, and a setting overrides one:
`cluster --fasrc config set GLOBUS_COLLECTION <uuid>`. The Globus CLI is the
`GLOBUS` setting, else the first `globus` on `PATH`, in `~/.local/bin`,
`/opt/homebrew/bin` or `/usr/local/bin`.

```bash
cluster transfer --engine globus fasrc:/n/netscratch/lab/x nersc:/pscratch/sd/u/user/
cluster transfer --engine globus --keep fasrc:... nersc:...   # submit and return
```

Without `--keep`, the command waits and exits non-zero unless the task
succeeded. With `--keep`, it prints the task id (`globus task show ID`). FASRC's
collection does not export home directories (`/n/home*`), and it enforces a
session policy that expires, so use `direct` for home paths and for unattended
jobs. Both limits are checked before submission, and so is the source: whether
it is a file or a directory is read from the collection's listing of its parent
(a trailing `/` says directory without asking), and a missing source is
reported before anything is submitted. A file sent into an existing directory
keeps its name. Globus
follows symlinks, as `-L` does, so `-l` and `--skip-symlinks` are refused.
`--dry-run` asks Globus about both sides and submits nothing.

To authorise the CLI without a local browser, each command prints a URL and
reads back a code:

```bash
globus login --no-local-server
globus session consent --no-local-server 'urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/scopes/<collection-uuid>/data_access]'
globus session update --no-local-server globus.rc.fas.harvard.edu   # FASRC only
```

## Authentication

| | fasrc | nersc |
|---|---|---|
| Cost | password + TOTP on every new connection | password + TOTP once a day |
| Effect | a new login may wait up to 30 s for an unused TOTP window | after `cluster auth`, connections are instant |
| Node choice | whatever the balancer gives, then pinned | free: derived from the login name |

```bash
cluster auth --nersc            # fetch or renew the 24-hour certificate
cluster auth --status --nersc
cluster auth --drop --nersc     # delete the cached certificate
```

The certificate is stored at `~/.ssh/nersc` and renews itself within
`CERT_RENEW_MARGIN` seconds of expiry. On FASRC, `cluster auth` does nothing.

On FASRC, `cluster` answers the password and verification-code prompts itself.
It stops answering once the session has started, so a `sudo` or `ssh` inside
`cluster sh` prompts you as usual and never receives your cluster password.

## A backend of your own

Any other host ssh reaches can be a backend, with logins, tmux sessions, mounts,
transfers and the watcher like the built-in ones:

```bash
cluster backends add lab lab-login     # a Host of your ssh config, a hostname, or user@host
cluster --backend lab login work
cluster backends                       # every backend, and its type
cluster backends remove lab            # once it has no logins
```

`add` writes a section of `settings.ini`, which can equally be written by hand:

```ini
[lab]
TYPE = ssh
HOST = lab-login
```

Such a backend is named with `--backend lab`, `CLUSTER_BACKEND=lab` or
`cluster config set BACKEND lab`. `lab:PATH` is not a prefix for it; a transfer
names one of its logins instead (`work:~/data`). Its name is a lower-case
letter followed by up to 23 letters, digits, `-` or `_`, and is refused if it
would make a `CLUSTER_<NAME>_<KEY>` variable mean two things (`mount`, because
of `MOUNT_NODES`).

| | ssh backend |
|---|---|
| Authentication | ssh's own: keys, an agent, certificates, a jump host, as your ssh configuration says. Nothing is typed or saved by `cluster`. |
| Prompts | A connection you make at a terminal (`login`, `attach`, `new`) may ask for a passphrase, a password or a new host key. One made with nobody there (a reconnect, the watcher, `boot`) runs in `BatchMode` and fails instead of waiting. |
| A refusal | is recorded, and the unattended connections stop trying once it is confirmed, until you connect by hand or change `HOST` or the ssh configuration file ([below](#a-refused-credential-is-not-tried-again-elsewhere)). |
| Nodes | A login is pinned to the name its node reports (`hostname -f`), which need not be `HOST`; reconnects go through `HOST`. |
| Storage | Nothing is taken to be shared between nodes: `AUTO_MOUNT`, `ONE_MOUNT_PER_BACKEND` and `MOUNT_FAILOVER` are 0 for it. |

Its settings, set with `cluster --backend lab config set KEY VALUE`:

- `HOST`: where ssh connects.
- `SSH_CONFIG`: the ssh configuration file; empty, the default, is ssh's own
  (`~/.ssh/config`). The built-in backends use `/dev/null`.
- `NODE_HOSTS`: for a `HOST` that can land on more than one machine,
  `NODE=DESTINATION` pairs saying how to reach each (`NODE_HOSTS = n1=lab-n1
  n2=lab-n2`). Without one, a reconnect that lands on another node is dropped
  and says which pair to add, rather than adopt the wrong node. A command sent
  to a node over a connection of its own checks first that it is there.
- `REAPS_ON_LOGOUT` (0): set 1 if the host ends your processes when your last
  session on it ends (logind's `KillUserProcesses`), so that `LINGER` keeps
  tmux alive there as on FASRC.
- `LABEL`: the name shown for it (empty: `HOST`). Every backend has this one.

`cluster doctor` checks that the host ssh dials first answers, following a
`ProxyJump` to its first hop and that hop's port; behind a `ProxyCommand`,
only a connection can tell. A site that needs its own authentication or node
layout needs a backend type written for it: [docs/backends.md](docs/backends.md).

## When something is slow or stops answering

Work is never cut off for taking long. What ends something is evidence that the
other side has stopped answering, and each bound below is a setting:

| What | What bounds it |
|---|---|
| opening a connection | ssh's `ConnectTimeout` (`CONNECT_TIMEOUT`, 25 s) per attempt; a pool connection dropped before authentication is tried again, up to `POOL_OPEN_TRIES` |
| a command on a cluster | a small one (a tmux listing, a breadcrumb, a home directory) that has not answered after `REMOTE_COMMAND_TIMEOUT` (60 s) counts as hung, one on a connection of its own gets `CONNECT_TIMEOUT` (25 s) more, and a session create the 10 s its node-side `loginctl` and `systemd-run` are bounded by there. One whose work grows with what it finds (`rename` retagging sessions, a kill of many) runs for as long as it prints, and counts as hung after `REMOTE_COMMAND_TIMEOUT` of silence; asking whether a connection answers at all, after a session drops, gets `REMOTE_CHECK_TIMEOUT` (10 s), and a connection that is up but misses it is tried again; only one that is gone, or misses it twice in a row, is rebuilt |
| an open connection | ssh's keepalives: after `SSH_SERVER_ALIVE_COUNT_MAX` (10) unanswered probes `SSH_SERVER_ALIVE_INTERVAL` (30 s) apart, about five minutes of silence, ssh closes it, and everything riding it ends with it |
| an attached session | reconnects after a drop, first after `RECONNECT_DELAY` (2 s) and twice as long after each further drop in quick succession, up to `RECONNECT_DELAY_MAX` (60 s); time connected fades the count with `RECONNECT_HALF_LIFE` (300 s), so drops spread over days never add up, while more than `INTERACTIVE_RETRIES` (8) in quick succession end it. A reconnect that fails for a passing reason counts as one more; a refused credential ends it at once. A reconnected `attach` goes back to its session, and says so if the session ended meanwhile rather than starting a new one; `sh LOGIN` reconnects only when the connection itself is gone, since a shell's own exit 255 is its answer |
| `transfer` | no time limit. Its connection is looked at every 10 s; one that is lost is restored, with the same waits as an attached session, and the transfer resumed where it stopped. More than `TRANSFER_RECONNECTS` (8) losses in quick succession end it, a refused credential at once. On a connection that is fine, rclone's own failures are retried `TRANSFER_RETRIES` (2) times and then stand. A far side that answers but has stopped moving (a hung filesystem there) is waited for, as `cp` would |
| a transfer's path questions | a stat gets `TRANSFER_PROBE_TIMEOUT` (45 s) and is asked `TRANSFER_PROBE_TRIES` (3) times; a listing or a walk of a tree runs for as long as it keeps printing, and is given up after `TRANSFER_IO_TIMEOUT` (120 s) of silence (a listing is then replaced by a question about each path) |
| a direct cross-cluster transfer | as `transfer`; the rclone on the executing cluster stops itself after hearing nothing from this machine for as long as the connection itself rides out (`SSH_SERVER_ALIVE_INTERVAL` × `SSH_SERVER_ALIVE_COUNT_MAX`, 300 s, and 10 s more), with a SIGKILL `STOP_TIMEOUT` (5 s) after its SIGTERM. If the connection stayed up (this machine was suspended, say) the transfer resumes at once; if it was lost, a resumed rclone starts only once the old one has stopped, which the node it ran on is asked about |
| `push`, `pull` | no time limit; a lost connection is restored and rsync run again, as for `transfer` |
| a relayed transfer, from a laptop | no time limit; only a lost connection is tried again, with the `[relay]` `RETRY*` keys. A stream starts again from its first byte, so more than `RETRIES` (3) losses end it however long each ran; a staging copy resumes, and drops spread over it never add up (see [the relay client](#from-a-laptop-the-relay-client)) |
| a mount | a probe with no answer after `MOUNT_CHECK_TIMEOUT` (8 s) is watched `MOUNT_BUSY_GRACE` (6 s) longer: a moving queue is busy and left alone, a frozen one is wedged and repaired |
| the watcher | never gives up; after the `WATCH_RETRIES`-th (5th) failed tick in quick succession it waits longer between them, doubling up to `WATCH_BACKOFF_MAX` (600 s), and healthy ticks fade the count with `WATCH_FAILURE_HALF_LIFE` (600 s). The count stops growing once that wait is at its longest, so a day's outage is forgotten as fast as a short burst. It waits, saying why, while a refused credential is on record (see below) |
| the NERSC certificate | `SSHPROXY_TIMEOUT` (90 s) for the exchange with sshproxy; a renewal that fails while the current certificate still works is a warning, and the next use tries again |
| `cluster bridge push` | no time limit on a copy; one that reports no progress for `TRANSFER_IO_TIMEOUT` (120 s) has stalled. The end-to-end check on the hub gets `BRIDGE_VERIFY_TIMEOUT` (300 s) |
| `cluster boot` | `BOOT_WAIT` (180 s) for the network, then `BOOT_TRIES` attempts; when either runs out, the login's watcher goes on trying |
| `cluster setup` | `SETUP_REMOTE_TIMEOUT` (60 s) for each step on the cluster |
| Globus | no deadline of its own; the Globus CLI reports a service that does not answer |

`cluster watch LOGIN` reports a watcher that has died, with its exit status, and
the logs in `~/.local/state/cluster/<backend>/` say why each repair or
reconnect failed.

### Waiting for another cluster command

A command that needs a login another `cluster` process is opening or
repairing, or a NERSC certificate another is fetching, waits for as long as
that process lives, and says whom it is waiting for:

```
  waiting for pid 4242 (python3 /home/user/.local/bin/cluster attach work api) to finish with login 'work'
cluster: waiting for pid 4242 (python3 /home/user/.local/bin/cluster --nersc auth) to finish fetching a NERSC certificate
```

A lock goes the moment its holder exits, however it exits, so the wait never
outlasts the work it waits on. A holder that is stopped (Ctrl-Z) does no work
and lets go of nothing until it is resumed, so after `LOCK_PATIENCE` (30)
seconds of that the wait ends and says so. On FASRC, a new connection may also
wait for another authentication to claim its TOTP window. That wait goes on
while the queue moves (the lock changing hands, or a window being claimed) and
gives up only after `TOTP_LOCK_PATIENCE` (90) seconds with nothing moving,
saying what it found. A lock held only for one small write (the settings file,
the record of abandoned sessions) is waited for while it changes hands, and a
holder that keeps it `LOCK_PATIENCE` seconds is taken as stuck.

One NERSC certificate is fetched at a time, each with a TOTP code of its own,
and a command that waited for another's fetch uses the certificate that fetch
installed. A refusal from sshproxy stops the command when there is no working
certificate to carry on with, as does a failed renewal you asked for with
`cluster --nersc auth --force`; a renewal that fails while the current
certificate still works is reported and passed over. A certificate that ssh
refuses is replaced by a fresh one, once: a replacement refused in turn is not
replaced again until a connection works or you fetch one by hand.

### A refused credential is not tried again elsewhere

Every node refuses a wrong password or TOTP seed the same way, and repeated
refusals are what lock an account. So a refused credential is never retried:
opening a login stops at the node that refused it, saying `that is the
credential failing, which every node would repeat, so the other nodes were not
tried` when there were others to try, and naming the command that checks it
(`cluster --fasrc config credentials`). A mount does not fail over to another
node with it either, and `cluster bridge push --cron` tries no other login on
that cluster.

A refusal can still pass: a code another authentication had just used, or a
clock that is off for a minute after a laptop wakes. So it goes on a record the
backend's processes share, and is confirmed once. `REFUSAL_CONFIRM_DELAY` (90)
seconds after it, two TOTP windows, one process tries the credential again,
while every other waits for what it finds. A second refusal stops every
unattended try (the watcher, `boot`, a reconnecting `attach` or `sh`, a
transfer's or the relay's reconnect) until the credential files change or you
connect by hand. A command run at a terminal that connects (`login`, `attach`,
`sh`, `new`, `refresh`, `repin`, `rescue`, `clean`, `strays`, `auth`, and the
test login `init` offers) tries it anyway, and says so. What any of them finds
goes on the record, so a refusal there holds the watchers too and a success
frees them; `clean` and `strays`, which visit node after node, stop at the first
that refuses it. `cluster status` shows the record, and `cluster backends`
lists the credential as `refused`:

```
credential: refused at 14:02 and 14:04; not retrying until the credentials change or you connect by hand
```

On NERSC the record is sshproxy's, of the password: it holds a certificate
fetch, never a connection made with a certificate that works. On an [ssh
backend](#a-backend-of-your-own) it is ssh's refusal, and "the credential files"
are its `HOST` and its ssh configuration file.

### Commands that ride a connection never open their own

`run`, `attach`, the reads behind `ls`, `push` and `pull` (rsync), `transfer`
(rclone), mounts (sshfs) and the command `ssh-command` prints all ride a
login's connection. `run` and `attach` open the login first if it is down; the
ride itself never authenticates. If the connection has gone, or turns the
session away, the rider fails at once with status 255 instead of connecting by
itself (which would mean a fresh MFA prompt, or a node the pool picks):

```
cluster: the connection to login01 is gone or refused another session - not opening a new one
```

rclone does not pass ssh's messages on, so under `transfer` only the status
shows it. A mount's sshfs reconnects over the connection while it is up; when
the connection has gone, the watcher opens it again and remounts.

### Stopping a command

SIGTERM and SIGHUP end a command the way Ctrl-C does: what it started is
stopped and what it holds is let go on the way out. It exits with status 143
(SIGTERM), 129 (SIGHUP) or 130 (Ctrl-C). A signal the command was started
ignoring stays ignored, so under `nohup` a hangup changes nothing. The watcher
stops on SIGTERM, SIGHUP or Ctrl-C, and asserts linger once more as it goes.

## Cleaning up and recovering

### clean

```bash
cluster clean --dry-run          # what it would reap
cluster clean                    # this backend
cluster clean --all-backends     # each backend in turn
cluster clean --all              # every node in the pool, not just known ones
```

`clean` kills only sessions that carry this tool's ownership tag naming a login
that does not exist, and sessions you abandoned. Sessions tagged by another
tool (`FOREIGN_OWNER_OPTIONS`) and untagged sessions are reported and left
alone. `--force` also reaps foreign-tagged sessions; untagged sessions need
`--include-untagged`.

A pinned login that is not connected is reconnected first, since only its own
listing tells its sessions from orphans. If it cannot be reconnected, the sweep
goes on without it: its sessions are kept (`--force` or not), its node is not
visited, and `clean` exits 1 so the gap is visible.
Sessions another machine started are never reaped, `--force` or not; see
[Using more than one machine](#using-more-than-one-machine).

### Using more than one machine

Running `cluster` on a second machine (a laptop, alongside an always-on
server) needs nothing special: each machine has its own logins, and they share
the cluster home. Each machine tags the sessions it starts with its own ID
(`WORKSTATION`, made once and kept in the state directory), and records the
ID in each breadcrumb.

- `cluster ls` and `status` list the other machine's sessions apart, as
  `elsewhere`, rather than as strays.
- `cluster attach SESSION` reaches one: through this machine's login on that
  node, or, after asking, a new login pinned there and named after the node.
  It attaches without creating, tagging or recording anything, so the session
  stays the other machine's. `cluster attach LOGIN SESSION` does the same when
  SESSION is not on LOGIN's node but is recorded elsewhere; `--here` makes a
  new one on LOGIN's node instead.
- `clean`, `strays` and `rename` never kill, adopt, forget or retag them. A
  session made before IDs existed, owned by a login this machine does not
  know, is kept by `clean` unless `--force`; each machine claims its own such
  sessions on its next sweep.

Give logins on different machines different names (`main` on the server,
`laptop` on the laptop): a login name means one connection on one machine,
and two machines' logins of the same name on the same node would each take
the other's legacy sessions for their own.

On FASRC every node visited costs a TOTP window, so run `clean` deliberately,
not on a timer. It asks before dropping records of sessions that are gone; `-y`
answers yes, and an unattended run without `-y` keeps them.

### strays

A login can leave a node without `repin`: reset after a node broke, redrawn by
the pool, or unpinned by `forget`. The records it leaves behind are strays.
`ls`, `status` and `clean` name them, and `strays` deals with them:

```bash
cluster strays                                  # what is recorded, and where (free)
cluster strays check holylogin06                # do those sessions still exist?
cluster strays adopt holylogin06 --as old       # put them under a login called "old"
cluster strays rename holylogin06:work work-old # free the name, keep the session
cluster strays clear holylogin06:api            # forget the record only
cluster strays kill holylogin06                 # kill the sessions, then forget them
```

A target is a node, a session, or `NODE:SESSION`. Run `check` first: it drops a
record only when it proves the session is gone. Anything that visits a node
says so and asks first. States: **stranded** (the owner login is elsewhere),
**abandoned** (left deliberately), **orphan** (the owner login is gone), and
**lost** (the login is still on that node but the session is not, which usually
means the node killed it).

### restore-layout, rescue, forget

```bash
cluster restore-layout work holylogin06   # rebuild sessions, windows and cwds
cluster rescue holylogin06 api            # one-off attach on a node, no state
cluster forget --dry-run --fasrc          # drop local state; touch nothing remote
cluster forget --fasrc
```

`restore-layout` rebuilds on the login's current node from the watcher's layout
snapshot and the breadcrumbs written when each session was created. It does not
restart processes. `forget` is for after a maintenance window has cleared every
tmux server: it drops pins, mount records, sockets and watchers without
contacting the cluster. If sessions survived, use `clean` instead.

## Autostart

### Restoring logins after a reboot

`cluster boot LOGIN` waits up to `BOOT_WAIT` seconds for the network, opens the
login, mounts it and starts its watcher. It is safe to run again. If the
network is not up by then, or the login cannot be opened after `BOOT_TRIES`
attempts, it leaves the watcher retrying and exits non-zero. On Linux, run it from cron, giving the full path, because
cron's `PATH` does not include `~/.local/bin`:

```
@reboot /home/user/.local/bin/cluster --fasrc boot work
@reboot /home/user/.local/bin/cluster --nersc boot gpu
```

`cluster doctor` checks the crontab for such a line. On macOS, a LaunchAgent
with `RunAtLoad` does the same when you log in; see
[docs/setup.md](docs/setup.md#macos-launchagents) for the file.

### Keeping tmux alive after you disconnect (FASRC)

FASRC login nodes end a user's processes when that user's connections to the
node end. Without linger, the node can take your tmux server with it after
`close --keep-tmux`, a workstation reboot, or a network drop that outlasts TCP
keepalive. `LINGER` is on by default, so `cluster` keeps it enabled for you:

```bash
cluster linger                   # assert it now on every connected login
cluster linger --install-hook    # also assert it when this machine shuts down
cluster --fasrc config set LINGER 0   # opt out: nothing is asserted or released
```

With `LINGER` on, tmux servers start under `systemd-run --scope --user`, so they
do not die with the connection that started them, and `loginctl enable-linger`
keeps your user manager running after the last connection ends. The node clears
linger on its own schedule, so `cluster` re-asserts it on every connection and
session create, every `LINGER_INTERVAL` seconds from the watcher, and from the
shutdown hook. Power loss between two assertions can still lose sessions.
`cluster doctor` shows linger for each login, and its `tmux scope` line shows
whether a node's tmux server can survive; one that cannot must be recreated.

**Releasing it.** `close`, `clean`, `strays kill`, `repin` and `refresh` settle
the node on the way out: if no tmux server of yours is left there, they run
`loginctl disable-linger`, but only where `cluster` turned linger on. Each
enable leaves a note on the node (`~/.cluster/linger/<node>`) recording the
linger file it created, and settle releases linger only while that file is
unchanged. Linger that was already on, or that something else has re-enabled
since (FASRC's own login script does, on every interactive login), is left on.
Every failure leaves linger on, never off.

**The shutdown hook.** `--install-hook` installs a system unit if you have root,
and otherwise a weaker user unit (see
[docs/design.md](docs/design.md#the-shutdown-hook)).
Re-running it repairs a hook that points at a moved `cluster`, and `cluster
boot` re-arms a missing one. `active (exited)` in `systemctl status` is correct.
To install the system unit by hand (from the checkout, with `cluster` on PATH),
and to remove either unit:

```bash
sed -e "s/@USER@/$USER/g" -e "s#@HOME@#$HOME#g" \
    -e "s#@CLUSTER@#$(command -v cluster)#g" -e "s#@REPO@#$PWD#g" \
    extras/cluster-linger-system.service \
  | sudo tee /etc/systemd/system/cluster-linger.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now cluster-linger.service

sudo systemctl disable --now cluster-linger.service    # remove the system unit
systemctl --user disable --now cluster-linger.service  # remove the user unit
```

macOS has no systemd and so no shutdown hook. There, linger rests on the
watcher's assertion every `LINGER_INTERVAL` seconds while this machine is up. A
watcher also asserts linger when it is stopped, but at shutdown its connection
may already be gone, so for sessions that must survive a reboot of a Mac, turn
on the keeper.

**The keeper** is a crontab line on the login node that re-asserts linger once a
minute. It is the only protection that works when your workstation disappears
without warning. It is off by default because it writes to your crontab on a
shared machine:

```bash
cluster --fasrc config set LINGER_KEEPER 1
cluster linger                         # installs it on connected nodes
cluster --fasrc config set LINGER_KEEPER 0   # later assertions remove it again
cluster linger --remove-keeper         # remove it now
```

It touches only its own marked line. `cluster clean --all` settles every
reachable node in the pool, which cleans up after logins that died without
closing.

## Workstation and cluster setup

`cluster setup` is an editor and tmux integration pass that is safe to repeat.
Setting up the machine itself is `cluster init`.

```bash
cluster setup --fasrc          # this machine plus the FASRC home
cluster setup work --check     # report drift, change nothing
cluster setup --local-only
cluster setup work --remote-only
```

- **VS Code**, in each one installed here: excludes the mount root from file
  watching (`files.watcherExclude`). On Linux that is the VS Code Server's
  machine settings (`~/.vscode-server/data/Machine/settings.json`) and the
  desktop editor's user settings (`~/.config/Code/User/settings.json`); on
  macOS, `~/Library/Application Support/Code/User/settings.json`.
  `VSCODE_SETTINGS` names one file instead. With `VSCODE_TAB_TITLE` on, it also
  titles terminal tabs with the launching command (`cluster a work api`). Other
  settings are kept, and the old file is backed up. A file with comments is
  left alone, with a warning and the line to add by hand.
- **Remote tmux:** merges a marked block into the cluster's `~/.tmux.conf`
  (titles, focus events, OSC 52 clipboard, aggressive resize, and
  version-guarded extended keys and passthrough). It validates the result on a
  separate socket, backs up the old file, and sources a running server without
  restarting it.
- **NERSC companion:** installed on a non-NERSC cluster only with
  `SETUP_SYNC_NERSC_TOOL` on; `--no-tool` skips it. See
  [docs/nersc-bridge.md](docs/nersc-bridge.md).

`setup` exits non-zero when something it manages is not ready. Status commands
check for local drift at most once per `SETUP_DRIFT_CHECK_INTERVAL` seconds;
`-1` turns that off.

## Configuration

Every setting is changed with `cluster config set`, and kept in one file,
`~/.config/cluster/settings.ini`, beside the credentials:

```bash
cluster config set WATCH_INTERVAL 20            # every cluster: [global]
cluster --nersc config set CONNECT_TIMEOUT 40   # one cluster: [nersc]
cluster config get WATCH_INTERVAL
cluster config unset WATCH_INTERVAL
cluster config                     # what is set, and each cluster's credentials
cluster config list                # every setting: value, source, meaning
cluster config path                # where the file is
```

- `set` writes `[global]`, or with a backend flag that backend's section. A
  setting only one backend reads goes to its section by itself, and naming the
  other backend for it is refused with the command that works. A setting every
  backend declares for itself, such as `NODES`, needs a backend flag.
  `--global` writes `[global]` even with a backend flag.
- `get` prints the value, or an empty line when there is none; for a secret it
  prints only whether it is set.
- `unset` also removes a key the file holds that nothing reads, and `cluster
  config` marks such keys.
- Values are checked when they are set. Booleans accept `1/0`, `on/off`,
  `true/false` and `yes/no`, and times are in seconds.
- Editing the file by hand works too; `set` and `unset` keep its comments. A
  section or key given twice is read with the later value winning, and one
  warning.

For a single command, an environment variable overrides the file:
`CLUSTER_<NAME>` for every backend and `CLUSTER_<BACKEND>_<NAME>` for one, so
`CLUSTER_NERSC_CONNECT_TIMEOUT=60 cluster login gpu` waits longer to connect,
this once. Precedence, lowest first:

```
built-in < [global] < [backend] < CLUSTER_<NAME> < CLUSTER_<BACKEND>_<NAME>
```

An invalid value from any source prints a warning naming that source, and the
built-in value is used. `cluster config list` is the authoritative catalogue:

| Area | Settings (defaults) |
|---|---|
| Logins | `DEFAULT_LOGIN` (main), `NEW_LOGIN_MODE` (tmux or shell), `MAX_LOGINS` (5), `ONE_LOGIN_PER_NODE` (1) |
| Mounts | `AUTO_MOUNT` (1), `ONE_MOUNT_PER_BACKEND` (1), `MACFUSE_BACKEND` (auto, kext or fskit; macOS only, see [docs/setup.md](docs/setup.md#mounts-on-macos)), `MOUNT_CHECK_TIMEOUT` (8), `MOUNT_BUSY_GRACE` (6), `MOUNT_FAILOVER` (1), `MOUNT_FAILOVER_AFTER` (2), `MOUNT_FAILOVER_TRIES` (3), `MOUNT_FAILBACK_TICKS` (20), `MOUNT_NODE_MAX_DSTATE` (200), `MOUNT_NODES` |
| Watcher | `WATCH_INTERVAL` (30), `WATCH_RETRIES` (5), `WATCH_FAILURE_HALF_LIFE` (600), `WATCH_BACKOFF_MAX` (600), `WATCH_START_TIMEOUT` (3), `LAYOUT_INTERVAL` (300) |
| Linger | `LINGER` (1), `LINGER_INTERVAL` (60), `LINGER_KEEPER` (0) |
| Connections | `SSH_CONFIG` (`/dev/null`, so `~/.ssh/config` is ignored unless you point this at it; empty for an [ssh backend](#a-backend-of-your-own): ssh's own), `CONNECT_TIMEOUT` (25), `REMOTE_COMMAND_TIMEOUT` (60), `REMOTE_CHECK_TIMEOUT` (10), `SSH_SERVER_ALIVE_INTERVAL` (30), `SSH_SERVER_ALIVE_COUNT_MAX` (10), `EXTERNAL_SSH_SERVER_ALIVE_INTERVAL` (15), `EXTERNAL_SSH_SERVER_ALIVE_COUNT_MAX` (4), `POOL_OPEN_TRIES` (3), `MASTER_READY_WAIT` (6), `SSH_MAX_SESSIONS` (10), `INTERACTIVE_RETRIES` (8), `RECONNECT_DELAY` (2), `RECONNECT_DELAY_MAX` (60), `RECONNECT_HALF_LIFE` (300), `NODE_PROBE_TRIES` (3), `NODE_PROBE_TIMEOUT` (6), `REFRESH_TRIES` (5), `STOP_TIMEOUT` (5), `LOCK_PATIENCE` (30), `TOTP_LOCK_PATIENCE` (90), `REFUSAL_CONFIRM_DELAY` (90), `LIST_WORKERS` (8) |
| Boot | `BOOT_WAIT` (180), `BOOT_TRIES` (5), `BOOT_RETRY_DELAY` (5), `BOOT_RETRY_DELAY_MAX` (30), `BOOT_NETWORK_POLL_INTERVAL` (1) |
| Transfers | `TRANSFER_TRANSFERS` (4), `TRANSFER_CHECKERS` (3), `TRANSFER_CONNECTIONS` (0 = automatic), `TRANSFER_RETRIES` (2), `TRANSFER_RECONNECTS` (8), `TRANSFER_OPEN_TRIES` (3), `TRANSFER_IO_TIMEOUT` (120), `TRANSFER_PROBE_TIMEOUT` (45), `TRANSFER_PROBE_TRIES` (3), `TRANSFER_MULTI_THREAD_STREAMS` (1), `SHARED_TRANSFER_TRANSFERS` (2), `SHARED_TRANSFER_CHECKERS` (2), `RCLONE` (the first rclone 1.64 or newer found; see [docs/setup.md](docs/setup.md#rclone)), `REMOTE_RCLONE` (the one on the cluster's `PATH`), `GLOBUS_COLLECTION` (the backend's), `PEER_CONNECT_TIMEOUT` (20) |
| NERSC | `CERT_RENEW_MARGIN` (3600), `SSHPROXY_TIMEOUT` (90), `NODE_REACH_TIMEOUT` (8), `NODE_REACH_SSH_TIMEOUT` (45), `NERSC_NODE_CANDIDATES` (6) |
| Setup | `SETUP_REMOTE_TIMEOUT` (60), `SETUP_DRIFT_CHECK_INTERVAL` (86400), `SETUP_SYNC_NERSC_TOOL` (0), `VSCODE_TAB_TITLE` (0) |
| Bridge | `BRIDGE_MIN_CERT_LEFT` (72000), `BRIDGE_LOGIN`, `BRIDGE_VERIFY_TIMEOUT` (300), `COMPANION_ADOPT_HUB_EDITS` (0), `COMPANION_SYNC_TIMEOUT` (60), `COMPANION_MAX_BYTES` (1048576) |
| Doctor | `NTP_SERVER` (pool.ntp.org) |
| Each backend's own | `CRED_DIR` (its credential directory), `NODES` (space-separated login nodes; replaces the backend's list everywhere, including completion), `LABEL` (the name shown for it) |
| ssh backends only | `HOST`, `NODE_HOSTS`, `REAPS_ON_LOGOUT` (0); see [A backend of your own](#a-backend-of-your-own) |
| NERSC only | `KEY` (`~/.ssh/nersc`), `SCOPE` (the sshproxy scope, `default`), `COLLAB` (a collaboration account; empty is your own) |

These apply to the whole tool and live in `[global]`: `BACKEND` (the only
backend set up, else `fasrc`; any backend, a [backend of your own](#a-backend-of-your-own) too), `STATE_ROOT` (`~/.local/state/cluster`),
`CTL_DIR` (`~/.ssh/controlmasters`), `MOUNT_ROOT` (`~/cluster_mounts`),
`CRED_ROOT` (`~/.config/cluster/credentials`), `VSCODE_SETTINGS` (empty: every
VS Code installed here), `VSCODE_TAB_TITLE` (0), `GLOBUS` (the Globus
CLI), `FOREIGN_OWNER_OPTIONS` (empty; space-separated tmux user options that
mark sessions another tool owns, which `clean` spares), `WORKSTATION` (empty:
an ID made once for this machine; see [Using more than one machine](#using-more-than-one-machine)), and `FORCE_PORTABLE`
(0; forces the fallbacks used where Linux-only interfaces such as `/proc` are
missing, for testing). `RELAY_HOST` and `RELAY_BIN` are the relay client's
`[relay]` settings; see [below](#from-a-laptop-the-relay-client).

### Credentials

`cluster init` or `cluster --BACKEND config credentials` saves a cluster's
username, password and TOTP seed; [docs/setup.md](docs/setup.md#credentials)
says where each comes from and how to change one. They are kept in
`~/.config/cluster/credentials/<backend>/` as `user`, `pass` and `key.txt`,
mode 600 in a directory of mode 700. `cluster --BACKEND config set CRED_DIR
PATH` moves one backend's directory, and `cluster config set CRED_ROOT PATH`
moves the whole tree. `CLUSTER_FASRC_USER` or `CLUSTER_NERSC_USER` overrides a
username for one command.

### Where things are kept

Settings and credentials are in `~/.config/cluster/`, state and logs in
`~/.local/state/cluster/`, control sockets in `~/.ssh/controlmasters/`, mounts
in `~/cluster_mounts/`, and each cluster's session records in `~/.cluster/` in
your home there. [docs/files.md](docs/files.md) lists every file.

## From a laptop: the relay client

`bin/cluster-relay`, installed on a laptop as `cluster`, forwards every command
over ssh to the machine that runs `cluster` (the relay host), so the laptop
needs no credentials or state of its own:

```bash
git clone https://github.com/procrastinine/cluster.git ~/cluster
~/cluster/bin/cluster config set RELAY_HOST user@workstation.example.org
mkdir -p ~/.local/bin
ln -s ~/cluster/bin/cluster-relay ~/.local/bin/cluster
cluster ls
```

The third line runs the checkout's own `cluster`, which writes the laptop's
`[relay]` section and needs nothing else set up.

Every command is one ssh to the relay host, so give the laptop a key for it
(or an ssh `ControlMaster`) rather than a password. The laptop needs
`python3`, `ssh` and `tar`, and `rsync` for staged transfers.

The client reads the `[relay]` section of `~/.config/cluster/settings.ini`,
the file the tool itself reads (`XDG_CONFIG_HOME` moves it), and
`CLUSTER_RELAY_<KEY>` overrides a key for one run:

| Key | Set with | What it is | Default |
|---|---|---|---|
| `HOST` | `config set RELAY_HOST` | the relay host as ssh takes it: `user@host`, or a `Host` alias from `~/.ssh/config` | none: required |
| `BIN` | `config set RELAY_BIN` | the `cluster` command on the relay host | `~/.local/bin/cluster` if it exists, else `cluster` on its `PATH` |
| `RETRIES` | `config set RELAY_RETRIES` | lost connections in quick succession a transfer tries again after | 3 |
| `RETRY_DELAY` | `config set RELAY_RETRY_DELAY` | seconds before the first try again, doubling with each loss after it | 2 |
| `RETRY_DELAY_MAX` | `config set RELAY_RETRY_DELAY_MAX` | the longest of those waits | 60 |
| `RETRY_HALF_LIFE` | `config set RELAY_RETRY_HALF_LIFE` | seconds of a staging copy's progress that halve the count of losses | 300 |

A leading `~/` in `BIN` is the relay user's home. When the relay host has no
`cluster` there, the client says so and exits with status 97. Transfers run
`BIN --relay-serve` there, which is the `bin/cluster-relay` of the checkout
`BIN` belongs to, symlinks followed, with that checkout's package. The two
halves talk to each other, so keep the laptop's checkout and the relay host's
at the same version. `cluster config` on the laptop is forwarded like every
other command, so change the laptop's `[relay]` section with
`~/cluster/bin/cluster config set RELAY_HOST ...` or by hand. Before `HOST` is
set, `cluster --help` prints the client's own help, and any other command
prints the line that adds the section.

`transfer` is handled on the laptop, because one side is a laptop path. The
prefix says where a path is:

| Prefix | Where the path is |
|---|---|
| a login, backend or alias name, or `remote:` | on a cluster |
| `relay:` | on the relay host |
| `local:`, or none | on the laptop |

```bash
cluster transfer ~/data/results work:projects/      # laptop -> cluster
cluster transfer work:projects/plots ~/plots        # cluster -> laptop
cluster transfer ~/runs/out_*.json work:runs/       # a local glob is fine
```

All sources must be on the same side; downloads from several clusters run one
after another. For a copy between the laptop and the relay host, use `scp`.
A cluster path is relative to the cluster home, and a leading `~/` means the
same. `work:` or `work:~` alone, as a download's source, names the whole home
and is refused; write `work:~/` to copy what is in it.

Plain copies stream through the relay host without touching its disk, and are
checked afterwards: a missing or short file is a non-zero exit. Filters
(`--include*`, `--exclude*`, `--filter*`, `--files-from`, `--min-*`,
`--max-*`) and `--sync`, `--move`, `--ignore-existing`, `--backup-dir`,
`--suffix`, `--compare-dest`, `--copy-dest` and `--bwlimit` cannot be
streamed, so those are staged under `~/.cache/cluster-relay` on the relay
host, whose free space is then the limit. The client says when it does this.
While a staged transfer runs, the client touches its staging directory every
ten minutes, and the next staged transfer removes any staging directory that
has gone untouched for a day, which only one whose run has gone does. Staged
transfers need rsync at both ends; rsync 2.6.9 is enough, and the openrsync
that recent macOS ships as `rsync` is untested. They also run `python3` on
the relay host. `-n`/`--dry-run` says which way a transfer would go and moves
nothing.

A relayed transfer has no time limit, and rides out lost connections by the
same rule as an attached session, with the `RETRY*` keys above. Only a lost
connection is tried again: ssh's exit 255 on either hop, or the relay host
failing to open its connection to the cluster. Any other failure is the
answer and ends the transfer, saying so: a `tar` that could not read one file,
a full disk, a path the cluster cannot list. A question to the cluster
(whether a path exists, what arrived) whose connection is lost is asked again.
A stream whose connection is lost is run again from the first byte, since it
cannot resume, so its time does not count in its favour, and more than
`RETRIES` losses end it. A staging copy whose connection drops is resumed by
rsync, keeping what arrived, and the time it ran counts in its favour, so
drops spread over a long copy never add up. A credential the cluster refuses
ends the transfer at once; `cluster login`, run from the laptop, connects by
hand on the relay host, which tries it again.

On macOS, uploads carry no extended attributes and no AppleDouble `._` files.
A download into a file system that folds case, as macOS's does by default,
stops before writing anything if it would bring names that differ only in
case (`Makefile` and `makefile`).

## Terminal repair

Every interactive session restores your terminal on the way out: mouse, focus,
bracketed-paste, alternate-screen and keyboard modes, and the window size. If a
program `cluster` did not start leaves your terminal broken, run `cluster
fixterm`. It needs no login or network, and unlike `reset` it keeps the screen.

A terminal the node has no description of (a newer one's own TERM, such as
`xterm-ghostty` or `xterm-kitty`), or one that cannot clear the screen, is
attached as `xterm-256color`, which tmux accepts, instead of being refused as
"missing or unsuitable". Installing the terminal's terminfo on the cluster
keeps its own.

## Tab completion

Completion works in bash 3.2 and later and in zsh; the line to add for each is
in [docs/setup.md](docs/setup.md#tab-completion). It reads only local state; it
never opens an SSH channel or walks a mount. `cluster a TAB` offers logins, and
`cluster a work TAB` offers the sessions `work` last reported. `cluster n work
TAB` offers nothing, because that position is a new session name, and
`cluster config set TAB` offers setting names. Start a new shell after updating
the checkout.
