# Setting up cluster

`cluster init` walks through everything on this page that can be done from
the command line. This page explains each part: where the credentials come
from, which optional tools to install on Linux and macOS, tab completion, and
running `cluster` unattended.

A machine with one cluster set up, or with none of the optional tools, is an
ordinary setup; see [One cluster, or no optional tools](#one-cluster-or-no-optional-tools).

## Credentials

Each cluster needs three things: your username, your password, and the TOTP
seed behind your authenticator app. `cluster init` asks for them, or
`cluster --fasrc config credentials` and `cluster --nersc config credentials`
ask for one cluster's. Each run says where the answers come from, shows the code
the seed makes right now so you can compare it with your app, and saves
nothing until every answer is in. Enter keeps a value that is already saved.

### FASRC

| What | Where it comes from |
|---|---|
| username and password | your FASRC Research Computing account, which is not your HarvardKey |
| TOTP seed | FASRC's OpenAuth token page: the text shown beside the QR code when you set up a token |

Setting up a new OpenAuth token replaces the old one, so add the new QR code to
your authenticator app as well, or the app's codes stop working.

Every new FASRC connection types your password and a fresh code for you. FASRC
accepts each code only once, so new connections are spaced one 30-second window
apart, and a window with less than 8 seconds left is skipped rather than used.
Connections that are already open cost nothing.

### NERSC

| What | Where it comes from |
|---|---|
| username and password | the ones you sign in to Iris (`iris.nersc.gov`) with |
| TOTP seed | the secret of a NERSC MFA token; Iris lets you add a token for this tool next to the one on your phone, and shows its secret beside its QR code when you create it |
| `COLLAB` (optional) | a collaboration account to get the certificate for; empty means your own account |

The password and a code are used only to fetch a 24-hour certificate from
NERSC's sshproxy service. Every connection after that uses the certificate, so
connections are free until it expires. `cluster --nersc auth` fetches one now,
and otherwise the first connection that needs one does. One fetch runs at a
time on this machine, each in a TOTP window no other fetch has used, and a
command that waited for another's fetch uses the certificate it brought.

### Any other host

A host that ssh already reaches, with keys, an agent or a jump host configured
in `~/.ssh/config`, needs nothing saved here:

```bash
cluster backends add lab lab-login      # a Host of your ssh config, a hostname, or user@host
cluster --backend lab init              # offers a test login
```

Connections to it authenticate as ssh does on its own; see
[A backend of your own](../USAGE.md#a-backend-of-your-own).

### What a TOTP seed looks like

The seed is base32 text: letters A to Z and digits 2 to 7, with any spaces or
hyphens ignored. The `otpauth://totp/...` link that the QR code holds is
accepted too. To read that link from a saved image of the QR code:

```bash
zbarimg -q --raw qr.png       # zbar: apt install zbar-tools, or brew install zbar
```

Refused, with the reason: a 6-digit code (that is a code, not a seed), an
`otpauth-migration://` export link from an authenticator app, an HOTP link, and
a link that asks for anything other than SHA-1, 6 digits and a 30-second
period.

TOTP codes depend on the clock. `cluster doctor` checks that it is
synchronised. On Linux, `timedatectl` should report `System clock synchronized:
yes`; on macOS, turn on **Set time and date automatically** in System Settings,
General, Date & Time. A code rejected on a machine whose clock is off looks
exactly like a wrong password.

### Changing them later

```bash
cluster --nersc config credentials        # go through one cluster's again
cluster --fasrc config set username NAME
cluster --fasrc config set password       # asks; never on the command line
cluster --fasrc config set totp           # asks, and shows the code it makes
cluster config                            # what is set, for every cluster
```

`cluster config unset` does not delete a credential; remove the file by hand if
you mean to. `CLUSTER_FASRC_USER` or `CLUSTER_NERSC_USER` overrides the
username for one command.

The files are kept in `~/.config/cluster/credentials/<backend>/`, a directory of
mode 700, as `user`, `pass` and `key.txt`, each of mode 600. `cluster doctor`
checks both modes and lists any other file in the directory.

## Optional tools

Each tool below adds one feature, and `cluster doctor` and `cluster init` show
which are available. Without one, that feature is off and says what to install;
the rest of the tool is unaffected.

| Feature | Linux | macOS |
|---|---|---|
| mounts | `apt install sshfs`, or `dnf install fuse-sshfs` | macFUSE and SSHFS, [below](#mounts-on-macos) |
| `push`, `pull`, the NERSC bridge | `apt install rsync`, or `dnf install rsync` | the `rsync` that ships with macOS; `brew install rsync` for an overall progress line |
| `transfer`, archive-sync | rclone 1.64 or newer, [below](#rclone) | `brew install rclone`, or the official binary |
| Globus transfers | `pipx install globus-cli` | `pipx install globus-cli` |
| the linger shutdown hook | systemd | not available; see [Automation](#automation) |
| restoring logins after a reboot | cron | a LaunchAgent; see [Automation](#automation) |

### Python

`cluster` needs Python 3.8 or newer and nothing outside the standard library.
On macOS, `xcode-select --install` provides `/usr/bin/python3`, which is
recent enough; Homebrew's Python works as well. The python.org installer's
Python has no CA certificates of its own until its **Install Certificates**
command has run. When that is the case, the NERSC certificate request falls back
to macOS's own `/etc/ssl/cert.pem`, and if that fails too, the error names the
command to run.

### Mounts on macOS

A mount needs macFUSE and an SSHFS built for it:

1. Install macFUSE, from its site (see the
   [macFUSE wiki](https://github.com/macfuse/macfuse/wiki)) or with
   `brew install --cask macfuse`.
2. Allow its system extension in System Settings, Privacy & Security. On Apple
   silicon, macOS first asks you to allow third-party kernel extensions: shut
   down, start up holding the power button, and choose **Reduced Security**
   with user management of kernel extensions in Startup Security Utility.
3. Install SSHFS: the SSHFS 2.5.0 package from macFUSE's site, or sshfs 3.x
   with `brew install gromgit/fuse/sshfs-mac`. Both work.

`cluster doctor` reports mounts as off until both macFUSE and `sshfs` are
present.

Keep editors and indexers out of the mount root. `cluster setup --local-only`
adds `**/cluster_mounts/**` to `files.watcherExclude` in each VS Code installed
here; see [USAGE.md](../USAGE.md#workstation-and-cluster-setup).

### rclone

`transfer` and archive-sync need rclone 1.64 or newer, the first release with
`--sftp-ssh`. Distribution packages are often older; the official binary from
[rclone.org/downloads](https://rclone.org/downloads/) is not. `cluster` uses
the `RCLONE` setting if it is set, and otherwise the first rclone it finds on
`PATH`, then in `/usr/local/bin`, `/opt/homebrew/bin` and `/usr/bin`:

```bash
cluster config set RCLONE /path/to/rclone
```

A cluster-to-cluster transfer also runs rclone on a cluster; see
[USAGE.md](../USAGE.md#between-two-clusters).

## Tab completion

Completion reads only local state; pressing Tab never opens a connection.
`cluster init` prints the line for your shell. Use the path of your checkout:

| Shell | Add to | Line |
|---|---|---|
| bash 4 or newer (Linux) | `~/.bashrc` | `source ~/cluster/completions/cluster.bash` |
| bash 3.2 (macOS) | `~/.bash_profile` | `source ~/cluster/completions/cluster.bash` |
| zsh | `~/.zshrc` | the three lines below |

```zsh
autoload -U +X compinit && compinit
autoload -U +X bashcompinit && bashcompinit
source ~/cluster/completions/cluster.bash
```

With bash-completion 2 installed (it needs bash 4.2 or newer), bash can instead
load it on first use:

```bash
mkdir -p ~/.local/share/bash-completion/completions
echo 'source ~/cluster/completions/cluster.bash' \
  > ~/.local/share/bash-completion/completions/cluster
```

Start a new shell after updating the checkout. For Tab and Shift-Tab to cycle
through candidates in bash, add this to `~/.inputrc`:

```
TAB: menu-complete
"\e[Z": menu-complete-backward
```

## Automation

Cron and launchd start programs with a minimal `PATH` (`/usr/bin:/bin`), which
does not include `~/.local/bin`, so always give `cluster` by its full path. On
macOS, also set `PATH` in the job, or `cluster` will not find a Homebrew
`sshfs`.

### Linux: cron

Restore logins when this machine starts:

```
@reboot /home/user/.local/bin/cluster --fasrc boot work
@reboot /home/user/.local/bin/cluster --nersc boot gpu
```

`cluster boot LOGIN` waits up to `BOOT_WAIT` seconds for the network, opens the
login, mounts it and starts its watcher; it is safe to run again. A network
that is not up in time, or a login that will not open, is left to the watcher,
which keeps trying. `cluster doctor` checks the crontab for an `@reboot` line
that runs `cluster boot`.

Other jobs take the same form, for example the bridge push every 8 hours
(see [nersc-bridge.md](nersc-bridge.md#setting-it-up)):

```
20 1,9,17 * * * /home/user/.local/bin/cluster bridge push --cron
```

### Linux: the shutdown hook

On FASRC, `cluster linger --install-hook` makes this machine assert linger on
the way down, so a reboot here does not end your tmux sessions there. It
installs a system unit when you have root and a user unit otherwise; see
[USAGE.md](../USAGE.md#keeping-tmux-alive-after-you-disconnect-fasrc).

### macOS: LaunchAgents

On macOS, cron skips a job whose time passes while the machine sleeps, and it
cannot read `~/Documents`, `~/Desktop` or iCloud Drive without Full Disk
Access. A LaunchAgent runs as you when you log in: `RunAtLoad` runs a job at
login, and `StartCalendarInterval` runs a job that was missed during sleep when
the machine wakes.

To restore login `work` at login, save this as
`~/Library/LaunchAgents/local.cluster.boot-work.plist`, with your own paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.cluster.boot-work</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/user/.local/bin/cluster</string>
    <string>--fasrc</string>
    <string>boot</string>
    <string>work</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>AbandonProcessGroup</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>/Users/user/Library/Logs/cluster-boot-work.log</string>
</dict>
</plist>
```

`AbandonProcessGroup` keeps launchd from stopping what `cluster boot` leaves
running when it exits. Load it, which also runs it once, check it, and remove
it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.cluster.boot-work.plist
launchctl print gui/$(id -u)/local.cluster.boot-work
launchctl bootout gui/$(id -u)/local.cluster.boot-work
```

For a job on a timetable, replace `RunAtLoad` with `StartCalendarInterval`.
The bridge push every 8 hours, for example:

```xml
  <key>ProgramArguments</key>
  <array>
    <string>/Users/user/.local/bin/cluster</string>
    <string>bridge</string>
    <string>push</string>
    <string>--cron</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>1</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>20</integer></dict>
  </array>
```

The archive-sync example is in
[extras/README.md](../extras/README.md#on-macos-a-launchagent).

`cluster doctor` looks for cron entries only, so on macOS it reports restoring
after a reboot as not automatic even with a LaunchAgent in place, and points
to this section.

macOS has no shutdown hook. There, linger on a FASRC node rests on the watcher's
assertion every `LINGER_INTERVAL` seconds while this machine is up; the watcher
also asserts it when it is stopped, but at shutdown the connection may already
be gone. The node-side keeper (`LINGER_KEEPER`) covers the gap; see
[USAGE.md](../USAGE.md#keeping-tmux-alive-after-you-disconnect-fasrc).

## One cluster, or no optional tools

Set up only the cluster you use. With one set up, new logins go there, and
fleet-wide commands such as `ls` and `status` cover it alone. A command aimed at
a cluster that is not set up says so and names the command that sets it up. To add the other one
later, run `cluster init` again or its `config credentials`.

| Missing | What changes | What to use instead |
|---|---|---|
| sshfs | mounts are off; `cluster init` offers `AUTO_MOUNT 0` so new logins stop trying | `push`, `pull` and `transfer` |
| rsync | `push`, `pull` and the bridge are off | `transfer` |
| rclone | `transfer` between here and a cluster, the `relay` engine and archive-sync are off | `push` and `pull`; a `direct` cluster-to-cluster copy runs the cluster's rclone |
| globus-cli | the `globus` engine is off | the `direct` and `relay` engines |
| systemd | no shutdown hook | the keeper (`LINGER_KEEPER`) |
| cron | nothing restores logins after a reboot | `cluster boot LOGIN` by hand, or a LaunchAgent on macOS |

To turn mounts off by hand, and on again:

```bash
cluster config set AUTO_MOUNT 0
cluster config unset AUTO_MOUNT
```
