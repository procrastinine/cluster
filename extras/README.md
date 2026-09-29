# Extras

Optional pieces that sit beside `cluster`. The core tool does not depend on any
of them, and none of them does anything until you install or configure it.

| File | What it is |
| --- | --- |
| `archive-sync` | Mirrors a cluster home into an rclone remote, usually an encrypted one. Described below. |
| `archive-sync-cron` | Runs `archive-sync` unattended, from cron or a macOS LaunchAgent: tries again after a failure, log rotation, stale-mirror warnings. |
| `home.filter` | The filter `archive-sync` uses for any backend that has no filter of its own. |
| `cluster-linger.service` | Linux with systemd: a *user* unit that asserts linger on login nodes before this machine shuts down, so remote tmux survives a local reboot. Best effort, no root needed. The file's header explains how to install it. |
| `cluster-linger-system.service` | Linux with systemd: the *system* version of the same unit. It needs root, and it is the only form whose shutdown ordering is guaranteed. The file's header explains how to install it. |

`cluster linger --install-hook` installs whichever linger unit it can. macOS has
no systemd and so no shutdown hook: there, the watcher's assertion every
`LINGER_INTERVAL` seconds and the optional node-side keeper are what protect
tmux across a reboot (see
[USAGE.md](../USAGE.md#keeping-tmux-alive-after-you-disconnect-fasrc)).

## archive-sync

`archive-sync` keeps an off-site copy of your home directory on each cluster.
Every run makes the destination match the cluster home, and it keeps whatever
it replaces or deletes.

- **Transport.** It uses rclone's sftp backend over an ssh command line from
  `cluster ssh-command`. It needs no SSH keys, TOTP handling or jump-host setup
  of its own. On a login transport it rides the login's existing control
  master. On a transfer transport, the default for `nersc`, it uses a dedicated
  connection to a data transfer node.
- **Nothing is lost.** Files that a run overwrites or deletes are moved to
  `<dest>-trash/<timestamp>/`. The trash is never pruned.
- **Symlink fence.** rclone's sftp backend follows symlinks, so a link from your
  home into scratch or project space would pull that whole tree into the
  archive. Before each sync, a remote `find` lists every symlink, and the run
  excludes each one that leads out of the home. See
  [How the fence decides](#how-the-fence-decides).
- **`.nosync` markers.** A directory that contains a file named `.nosync` is
  left out, along with its contents.
- **Channel budget.** One SSH connection allows only about ten channels. On a
  login transport, `archive-sync` asks `cluster channels --free` how many are
  free, keeps one for rclone's own commands and one spare, and scales its
  concurrency to fit the rest. If the shared master has no room, or its node
  refuses to run commands, the run opens a login of its own (`FALLBACK_LOGIN`),
  moves it once to a fresh node if that node is bad too, and closes it
  afterwards. A fallback login that already exists is ridden and left open.

From the core tool it calls only these:

- `cluster ssh-command`, for the transport;
- `cluster channels --free`, to size its concurrency and to see whether the
  fallback login exists;
- `cluster config get BACKEND` when `BACKENDS` is not set, and
  `cluster config get RCLONE` when no rclone is named;
- `cluster transfer --close`, `cluster close` and `cluster refresh`, only on a
  connection or login that the run opened itself.

`channels` and `config get` read local state only and never authenticate.

Until a config file exists, `archive-sync` and `archive-sync-cron` exit with
status 3 and a short message, and they create nothing.

### Files

| Path | What it holds |
| --- | --- |
| `~/.config/cluster/archive-sync.conf` | The settings. `ARCHIVE_SYNC_CONFIG` names another file. |
| `~/.config/cluster/archive/<backend>-home.filter` | A backend's own filter. Optional. |
| `extras/home.filter` | The default filter, found beside the script. |
| `~/.local/state/cluster/archive-sync/` | Mode 700: per-run logs (`sync-<backend>-<timestamp>.log`), the generated fences (`<backend>-symlink-fence.filter`), success stamps (`last-success-<backend>`), locks and `cron.log`. The logs list every file name in the home. |

`XDG_CONFIG_HOME` replaces `~/.config` and `XDG_STATE_HOME` replaces
`~/.local/state`. An empty or relative value counts as unset.

`archive-sync` finds `home.filter` beside itself, following symlinks. Link the
scripts onto your `PATH`; do not copy them.

### Prerequisites

- **rclone 1.64 or newer.** Older versions lack `--sftp-ssh`, and distribution
  packages are often older: Ubuntu 24.04 ships 1.60. On Linux, install the
  official binary from <https://rclone.org/downloads/>. On macOS, run
  `brew install rclone`. `archive-sync` checks the version before it connects,
  and `--show-config` shows the binary it would use. It searches in this
  order:
  1. `ARCHIVE_SYNC_RCLONE`;
  2. `RCLONE` in the config file;
  3. the core tool's `RCLONE` setting, so one
     `cluster config set RCLONE /path/to/rclone` serves both;
  4. `rclone` on `PATH`, then `/opt/homebrew/bin/rclone`, then
     `/usr/local/bin/rclone`. The first one that is new enough wins.

  A binary named by the first three is the only one tried.
- **Local tools.** `bash` (the 3.2 that macOS ships is fine), `python3` and
  `awk`. `flock` is used where it exists, but it is not needed: `python3`
  takes the same lock without it.
- **`cluster` on `PATH`**, set up for each backend you want to mirror: that
  is, `cluster login` works.
- **GNU `find` on the cluster.** Linux clusters have it.

### 1. Create a storage remote

This is where the encrypted data will live. rclone supports many storage
systems, among them S3-compatible stores, Backblaze B2, Google Drive, pCloud,
WebDAV, and any machine you can reach over SFTP.

```bash
rclone config        # n) New remote, name it e.g. "store", then pick the type
rclone lsd store:    # check that it works
```

Most types can also be created without the interactive prompts. For example,
for a server you reach over SSH:

```bash
rclone config create store sftp host=backup.example.org user=user key_file=~/.ssh/id_backup
```

### 2. Create a crypt remote on top of it

A crypt remote encrypts file contents, file names and directory names before
anything reaches the storage remote. Interactively:

```bash
rclone config
#   n) New remote          name> mycrypt
#   Storage> crypt
#   remote> store:cluster-archive     (a folder on the storage remote)
#   filename_encryption> standard
#   directory_name_encryption> true
#   password> choose your own, or let rclone generate one
#   password2 (the salt)> choose one too; recommended
```

Or without the prompts. This works in bash and in zsh:

```bash
printf 'crypt password: ';        read -rs P;  echo
printf 'crypt salt (password2): '; read -rs P2; echo
rclone config create mycrypt crypt remote=store:cluster-archive \
    password="$(printf '%s' "$P"  | rclone obscure -)" \
    password2="$(printf '%s' "$P2" | rclone obscure -)"
unset P P2
```

The non-interactive form briefly puts the obscured, and therefore reversible,
values on a command line, where other users of the machine can see them. Use
the interactive form on a shared machine.

> **Back up the crypt password and salt now, somewhere other than this
> machine** (for example, a password manager). The data on `store:` is
> unrecoverable without them. Nobody can reset them, and that includes the
> storage provider. A copy of `rclone.conf` also works as a backup, because
> it holds both, obscured.

Check the setup:

```bash
rclone mkdir mycrypt:
rclone lsd mycrypt:                  # readable through the crypt remote
rclone lsd store:cluster-archive     # scrambled names on the storage side
```

### 3. Optional: encrypt `rclone.conf`

Once `rclone.conf` holds your crypt passwords, anyone who can read the file
can read the archive. You can encrypt the file itself with
`rclone config` → `s) Set configuration password`. Unattended runs then need a
way to unlock it. Give archive-sync one of these, and it passes it to rclone as
`RCLONE_PASSWORD_COMMAND`. rclone then runs that command and reads the password
from the command's output, so the password never appears in a process's
arguments or environment.

- **A password file, readable only by you.** Create it:

  ```bash
  mkdir -p -m 700 ~/.config/cluster
  printf 'rclone.conf password: '; read -rs P; echo
  (umask 077 && printf '%s' "$P" > ~/.config/cluster/archive-sync.pass); unset P
  RCLONE_PASSWORD_COMMAND="cat $HOME/.config/cluster/archive-sync.pass" rclone listremotes
  ```

  Then set `RCLONE_PASSWORD_FILE=~/.config/cluster/archive-sync.pass` in the
  config file. The path may contain spaces and quotes. A run prints a note when
  the file is readable by other users.
- **A secret store.** Set `RCLONE_PASSWORD_COMMAND` to a command such as
  `pass show rclone/config`, `secret-tool lookup service rclone`, or, on macOS,
  `security find-generic-password -s rclone -w`. rclone splits the command at
  spaces; a double-quoted field keeps its spaces. The macOS login keychain
  belongs to your login session: a LaunchAgent (step 7) can read it, and a
  cron job may not be able to.

### 4. Write the config file

The config file is `~/.config/cluster/archive-sync.conf`. Make it readable
only by you:

```bash
mkdir -p -m 700 ~/.config/cluster
touch ~/.config/cluster/archive-sync.conf && chmod 600 ~/.config/cluster/archive-sync.conf
"${EDITOR:-vi}" ~/.config/cluster/archive-sync.conf
```

The file holds `KEY=VALUE` lines, one per line; a leading `export` is
allowed. Values may be quoted. A `#` starts a comment at the start of a line,
or after a value and a space. archive-sync parses the file; it never runs it.
`$HOME`, `${HOME}` and a leading `~` are expanded, except in single quotes,
and nothing else is: any other `$`, a `$(...)` or a backquote is an error. Add
`_<backend>` to any key to set it for one backend only, for example
`LOGIN_fasrc=work`. The suffixed form wins over the plain one.

A minimal config:

```bash
REMOTE=mycrypt:
BACKENDS="fasrc nersc"
LOGIN_fasrc=work
RCLONE_PASSWORD_FILE=~/.config/cluster/archive-sync.pass
```

This mirrors your FASRC home to `mycrypt:fasrc-home` and your NERSC home to
`mycrypt:nersc-home`. Each destination has its trash beside it, for example
`mycrypt:fasrc-home-trash/`.

| Key | Default | Meaning |
| --- | --- | --- |
| `REMOTE` | required, unless `DEST` is set | The rclone remote. Each backend's destination is `<REMOTE><backend>-home`; a `/` is added when `REMOTE` does not end in `:` or `/`. |
| `DEST` | `<REMOTE><backend>-home` | The full destination, usually set per backend as `DEST_<backend>`. It must be `name:path` or an absolute path. |
| `TRASH` | `<DEST>-trash` | Each run's replaced and deleted files go to `<TRASH>/<timestamp>/`. It must stay outside `DEST`. |
| `BACKENDS` | `cluster config get BACKEND`, else `fasrc` | The backends that `archive-sync-cron` mirrors, in order. The first one is also the default `--backend`. |
| `TRANSPORT` | `login` (`transfer` for `nersc`) | `login` rides a login's master. `transfer` opens a dedicated connection to a data transfer node. |
| `LOGIN` | the backend's default login | The login whose master to ride, when `TRANSPORT` is `login`. |
| `FALLBACK_LOGIN` | `archive-<backend>` | The login opened when the shared master is full or unusable. Login names are global across backends. |
| `TRANSFERS`, `CHECKERS` | 3 and 3 (`login`), 4 and 3 (`transfer`) | rclone concurrency. On a login transport these are upper limits, lowered to fit the free channels. |
| `CONNECTIONS` | `TRANSFERS + CHECKERS + 1` | rclone's `--sftp-connections`. |
| `RCLONE` | the search in [Prerequisites](#prerequisites) | The rclone binary. It must be 1.64 or newer. |
| `RCLONE_PASSWORD_FILE` | none | A file holding the `rclone.conf` password. Read with `/bin/cat`. |
| `RCLONE_PASSWORD_COMMAND` | none | A command that prints the `rclone.conf` password. Set this or `RCLONE_PASSWORD_FILE`, not both. |
| `FILTER_DIR` | `~/.config/cluster/archive` | The directory that holds `<backend>-home.filter`. |
| `FILTER` | `<FILTER_DIR>/<backend>-home.filter` if it exists, else `extras/home.filter` | The filter file itself, usually set per backend. |
| `SCAN_PRUNE_PATHS` | `.conda .cache .local .local/lib .npm .nvm .vscode-server miniconda3 anaconda3 miniforge3 mambaforge` | Home-relative trees that the symlink scan skips. The globs `*` and `?` match within one path component. |
| `SCAN_PRUNE_NAMES` | `node_modules .venv venv .git __pycache__ .tox .ipynb_checkpoints .pytest_cache .mypy_cache` | Directory names that the symlink scan skips at any depth. |
| `IO_TIMEOUT` | `2m` | rclone's `--timeout`: how long a connection to the destination may go idle before rclone gives up on it and tries again, as an rclone duration (`90s`, `5m`; `0` never). The cluster side's connection is watched by the `cluster` tool's keepalives instead. |

For a single run, environment variables override the file:
`ARCHIVE_SYNC_DEST`, `ARCHIVE_SYNC_TRASH`, `ARCHIVE_SYNC_RCLONE`,
`ARCHIVE_SYNC_TRANSFERS`, `ARCHIVE_SYNC_CHECKERS`, `ARCHIVE_SYNC_CONNECTIONS`,
`ARCHIVE_SYNC_FALLBACK_LOGIN`, `ARCHIVE_SYNC_BACKENDS` and
`ARCHIVE_SYNC_IO_TIMEOUT`. The cron wrapper also reads `ARCHIVE_SYNC_RETRIES`
(default 2), `ARCHIVE_SYNC_RETRY_WAIT` (600 seconds),
`ARCHIVE_SYNC_RETRY_WAIT_MAX` (3600 seconds), `ARCHIVE_SYNC_RETRY_HALF_LIFE`
(1800 seconds), `ARCHIVE_SYNC_RETRY_WINDOW` (72000 seconds),
`ARCHIVE_SYNC_KEEP_LOG_DAYS` (30), `ARCHIVE_SYNC_CRON_LOG_MAX` (5242880 bytes)
and `ARCHIVE_SYNC_STALE_DAYS` (2); see step 7 for the first five.

### 5. Optional: a filter of your own

Every backend uses a filter. A backend without one of its own uses the shipped
[`home.filter`](home.filter), and every run names the filter it uses. That
filter leaves out what can be installed, rebuilt or downloaded again: conda and
Python environments, JavaScript packages, caches, the VS Code server and editor
swap files. It keeps everything else, including `.git` directories, because
they can hold commits and branches that exist nowhere else.

To change it for one backend, copy it and edit the copy:

```bash
mkdir -p ~/.config/cluster/archive
cp ~/cluster/extras/home.filter ~/.config/cluster/archive/fasrc-home.filter
```

archive-sync uses the copy from then on. `FILTER_<backend>` names a filter file
anywhere else. The syntax is rclone's:

- The first rule that matches a path wins.
- `- ` excludes and `+ ` includes.
- A leading `/` anchors a pattern at the top of the cluster home.
- To match a directory and everything in it, write `dir/**`.
- rclone treats only whole lines as comments. Never add a trailing comment
  after a rule, because it becomes part of the pattern.

For example, lines you might add to a copy:

```
# every repository here is pushed to a forge
- .git/**
# rebuilt by the package manager
- /.julia/**
# half-written files
- *.tmp
- *.partial
```

You do not need to list symlinks into scratch or project space: the fence
covers them. To leave out one directory without editing the filter, create a
`.nosync` file in it.

**Scan prunes.** `SCAN_PRUNE_PATHS` and `SCAN_PRUNE_NAMES` only make the
symlink scan faster by skipping trees the sync never reads. A symlink inside a
skipped tree is never examined, so skipping a tree is only safe if the filter
excludes it too. For that reason archive-sync checks every entry against the
filter file. An entry counts as covered only if the filter has the exact rule
`- /PATH/**` or `- /PATH/` (for a name, `- NAME/**` or `- NAME/`), with no `+`
or `!` rule above it. An entry that is not covered is scanned anyway, and if
you set the entry yourself, the run prints a note. With the shipped filter,
`.local` and `.git` are scanned, because that filter keeps them; add
`- .git/**` to your copy and `.git` directories are skipped as well. Setting
either key replaces its default list. `archive-sync --show-config` shows which
prunes are in effect.

### How the fence decides

A symlink is fenced, that is excluded along with everything under it, when its
target leaves the home. That is judged twice: from the target as written, and
after following the other symlinks in the home that the target passes through.
A link that is inside the home by one reading and outside by the other is
fenced, and so is a loop.

A name that a filter rule cannot hold exactly, such as one with a tab, a
newline, trailing spaces or bytes that are not UTF-8, is fenced with a `?`
wildcard in place of each such character. That can also exclude a sibling that
differs only there, never less than the link itself.

A scan that cannot read a directory refuses to sync and shows `find`'s message.
Fix the directory's permissions, or leave it out with both a `- /PATH/**`
filter rule and a `SCAN_PRUNE_PATHS` entry.

One limit: a link inside a pruned tree, or outside the home, is not examined,
so a chain that passes through one is read as written.

### 6. Check the configuration, then do a dry run

```bash
archive-sync --show-config                 # effective settings; touches nothing
archive-sync --backend fasrc --fence-only  # the symlink scan alone; runs no rclone
archive-sync --backend fasrc --dry-run     # reports what would be copied; changes nothing
```

`--show-config` prints something like this:

```
config:       ~/.config/cluster/archive-sync.conf (present)
backend:      fasrc   (archive-sync-cron mirrors: fasrc nersc)
destination:  mycrypt:fasrc-home
trash:        mycrypt:fasrc-home-trash/<timestamp>/
rclone:       /usr/local/bin/rclone (rclone 1.68; 1.64 or newer is needed)
rclone.conf:  password read from ~/.config/cluster/archive-sync.pass (present)
transport:    login 'work', fallback 'archive-fasrc'
concurrency:  3 transfers + 3 checkers (before channel fitting)
idle limit:   2m (rclone --timeout, on the destination's connections)
filter:       ~/cluster/extras/home.filter (present, the default shipped with archive-sync)
              to change it, copy it to ~/.config/cluster/archive/fasrc-home.filter and edit the copy
scan prunes:  -path "$H/.conda" -o -path "$H/.cache" -o ... -name .mypy_cache
state:        ~/.local/state/cluster/archive-sync
```

The dry run writes its full report to
`~/.local/state/cluster/archive-sync/sync-<backend>-<timestamp>.log` and ends
with `done (dry run: nothing was changed; the report is in ...)`. Before the
first real run, check that report for anything large you do not want to
archive, and check the fence at
`~/.local/state/cluster/archive-sync/<backend>-symlink-fence.filter`. Then run
the command again without `--dry-run`. The first real run copies everything
and can take a long time. After that, each run reads the cluster home's
metadata and transfers only what changed.

To see what is in the archive, or to restore from it:

```bash
rclone lsd mycrypt:fasrc-home
rclone copy mycrypt:fasrc-home/project/results ./restored-results
rclone lsd mycrypt:fasrc-home-trash        # one directory per run that replaced something
```

To browse it as a folder, mount it with rclone. This needs FUSE (`fuse3` on
Linux, macFUSE on macOS):

```bash
mkdir -p ~/archive
rclone mount mycrypt: ~/archive --read-only --daemon
fusermount3 -u ~/archive                   # to unmount; on macOS: umount ~/archive
```

Leave `--read-only` in place unless you mean to write there. Each run makes
`<backend>-home` match its cluster home, so a file written into it through
the mount moves to the trash on the next run. To mount it whenever you log in
on Linux, write a systemd user unit, for example
`~/.config/systemd/user/archive-mount.service`, giving rclone's full path as
`command -v rclone` prints it:

```ini
[Unit]
Description=rclone mount of the archive at %h/archive
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
# Only when rclone.conf is encrypted (step 3):
Environment="RCLONE_PASSWORD_COMMAND=/bin/cat %h/.config/cluster/archive-sync.pass"
ExecStartPre=/bin/mkdir -p %h/archive
ExecStart=/usr/local/bin/rclone mount mycrypt: %h/archive --read-only
ExecStop=/bin/fusermount3 -uz %h/archive
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now archive-mount.service
```

To reach the archive from another machine, such as a laptop, give it the same
two remotes. Either run the commands of steps 1 and 2 there with the same crypt
password and salt, or copy the two sections from `rclone config show` into its
`rclone.conf`; the passwords in them are obscured, not encrypted. It then reads,
restores and mounts the archive as above. On macOS, `--vfs-cache-mode full`
keeps files you have opened readable while offline.

### Options and exit status

| Option | Effect |
| --- | --- |
| `--backend NAME` | The cluster home to mirror. Default: the first of `BACKENDS`. |
| `--login NAME` | Ride this login's master. It selects the login transport. |
| `--dry-run`, `-n` | Report what would change, and change nothing. |
| `--fence-only` | Regenerate the symlink fence, then stop. No rclone runs. |
| `--show-config` | Print the effective settings, then stop. Touches nothing. |
| `--list-backends` | Print the backends `archive-sync-cron` mirrors, then stop. |
| `-h`, `--help` | Print the script's header. |

Any other option is passed to rclone.

| Exit | Meaning |
| --- | --- |
| 0 | Done. |
| 1 | The run failed: the connection, the symlink scan or rclone. Every rclone failure but a usage error lands here, failing to reach the server included. A later run may succeed, so `archive-sync-cron` tries it again (see step 7). |
| 2 | A configuration or usage error, including rclone's own exit status 2 (a bad flag or flag value). Retrying will not help, so `archive-sync-cron` does not. |
| 3 | Not configured. |
| 4 | Another archive-sync for this backend is running. |
| 129, 130, 143 | Stopped by a signal: a hangup, Ctrl-C or a terminate, as at a shutdown. `archive-sync-cron` then tries nothing more in that run. |

`archive-sync-cron` exits with archive-sync's status, the last non-zero one
when there are several backends, or a signal's when one stopped the run. For
it, 4 also means that an earlier `archive-sync-cron` is still running.

### 7. Run it unattended

Put both scripts on your `PATH` as symlinks (here in `~/.local/bin`, which must
be on your `PATH`), and rehearse exactly what the scheduled job will run:

```bash
mkdir -p ~/.local/bin
ln -s ~/cluster/extras/archive-sync ~/cluster/extras/archive-sync-cron ~/.local/bin/
archive-sync-cron --dry-run; tail ~/.local/state/cluster/archive-sync/cron.log
```

`archive-sync-cron` does the following:

- Sets a `PATH` that finds `cluster` and rclone without your shell's setup:
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin` and
  `/bin`, then the `PATH` it was given.
- Mirrors each backend in `BACKENDS` in turn, so one cluster being down does
  not stop the others. Every backend has its first try before any is tried
  again, and while one waits for its next try, the others go ahead.
- Tries a run that failed (exit 1) again, by the rule the `cluster` tool's own
  reconnects follow. Each failure counts one, and the time a failed run spent
  getting further fades the count, halving it every
  `ARCHIVE_SYNC_RETRY_HALF_LIFE` seconds (1800). Getting further is its log
  recording a file copied, moved or deleted, unless it failed on nothing but
  the files and directories the try before it failed on. A live home has
  something new to copy on every try (a shell history, say), so a file or a
  directory that cannot be read, or a file that changes as it is read, would
  otherwise earn a retry each time it fails. The
  wait before the next try is `ARCHIVE_SYNC_RETRY_WAIT` (600 seconds), doubling
  with the count up to `ARCHIVE_SYNC_RETRY_WAIT_MAX` (3600). More than
  `ARCHIVE_SYNC_RETRIES` (2) failures in quick succession leave that backend
  for the next run. So does a try that would start more than
  `ARCHIVE_SYNC_RETRY_WINDOW` seconds (72000, 20 hours) after the wrapper
  started. Keep the window below the cron interval, so that one run is over
  before the next is due; a try that is going is never cut short. So a
  cluster that is down costs three tries and half an hour, a run that fails
  the same way each time stops, and a long first sync that drops now and then
  is carried on for as long as the window lasts, then taken up by the next
  run from where it stopped. A configuration error is not retried, and a run
  stopped by a signal (exit 129, 130 or 143) ends the wrapper's run: the
  backends not yet tried are logged as not tried.
- Logs to `~/.local/state/cluster/archive-sync/cron.log`, keeping one older
  generation once the log passes 5 MiB, and deletes per-run sync logs after
  30 days.
- Prints a `STALE:` line for every backend in `BACKENDS` whose last successful
  sync is two or more days old, that has no successful sync on record, or whose
  success stamp cannot be read. Each successful sync records its time in
  `last-success-<backend>`.

Arguments are passed through to `archive-sync`, so
`archive-sync-cron --backend nersc` mirrors only that backend.
`archive-sync-cron --help` prints its header.

**Credentials.** On a login transport, the scheduled run rides a master that is
already open whenever there is one. If none is open, or the fallback login is
needed, `cluster` authenticates the way it always does. On FASRC that costs one
TOTP window.

#### On Linux: cron

Give cron the absolute path:

```bash
crontab -e
#   0 8 * * * /home/user/.local/bin/archive-sync-cron
```

Until archive-sync is configured, the job prints its "not configured" message
on stderr, where cron's mail picks it up, and creates nothing.

#### On macOS: a LaunchAgent

cron works on macOS too, with the path under `/Users`
(`0 8 * * * /Users/user/.local/bin/archive-sync-cron`). But cron skips a job
whose time passes while the Mac is asleep. A LaunchAgent with
`StartCalendarInterval` runs it when the Mac wakes instead. Save this as
`~/Library/LaunchAgents/local.cluster.archive-sync.plist`, with your own home
directory in both paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.cluster.archive-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/user/.local/bin/archive-sync-cron</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardErrorPath</key>
    <string>/Users/user/Library/Logs/archive-sync-cron.log</string>
</dict>
</plist>
```

Then load it:

```bash
plutil -lint ~/Library/LaunchAgents/local.cluster.archive-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.cluster.archive-sync.plist
launchctl print gui/$(id -u)/local.cluster.archive-sync | head    # loaded?
```

`launchctl bootout gui/$(id -u)/local.cluster.archive-sync` removes it. After
you edit the file, bootout and bootstrap it again. The job's output goes to
`cron.log` as above; `StandardErrorPath` catches only what the wrapper prints
before it opens that log, such as the "not configured" message.

macOS privacy protection keeps jobs that cron or launchd starts out of
`~/Desktop`, `~/Documents`, `~/Downloads` and iCloud Drive unless they are
granted access (Full Disk Access, in System Settings). Keep the checkout
outside those folders; `~/cluster` is fine.
