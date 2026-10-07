---
name: cluster
description: Drive HPC clusters (FASRC, NERSC Perlmutter, or any ssh host set up as a backend) through the `cluster` CLI - run commands on login nodes, start tmux sessions and read or steer them, move files, and check connection health without spending authentications or killing anyone's work. Use when the user mentions cluster, FASRC, NERSC, Perlmutter, a login node, a remote tmux session, or running or copying something on a cluster, and the `cluster` command is installed.
license: MIT
compatibility: Needs the cluster CLI on PATH, already set up on this machine with `cluster init` (Linux or macOS).
---

# Working with `cluster`

A **login** is a named, authenticated SSH connection that `cluster` keeps open,
pinned to one node. A **session** is a tmux session on that node; it keeps
running when nothing is attached. Login names are global across clusters, so a
name is enough to reach a login: `--nersc`, `--fasrc` or `--backend NAME` is
needed only to create a new login on a cluster other than the default.

`cluster --help` lists every command and `cluster COMMAND --help` explains one;
asking for help never runs anything. The full guide is `USAGE.md` in the
cluster repository.

## What things cost

- **Free, and the place to start:** `cluster ls` (every login and its sessions,
  on every cluster), `cluster status`, `cluster channels LOGIN`, `cluster pin`,
  `cluster strays`. None of them authenticate.
- **Anything that takes a LOGIN opens it if it is down.** On FASRC that is a
  password and a TOTP code, and may wait up to 30 s for a fresh code window. On
  NERSC it is free once the day's certificate exists. Riding a login that is
  already up costs nothing.
- **A login name that does not exist is created.** `cluster run wrok -- ls`
  opens a new connection called `wrok`, on some node. Use names exactly as
  `cluster ls` prints them, and always name the login: left out, it means the
  default login.
- **A refused credential is not retried**, because repeated refusals lock the
  account. On "refused", stop and tell the user; do not try again.
- **Do not poll.** One login carries about 10 SSH channels, shared by every
  attach, the mount and each transfer; `cluster channels LOGIN --free` says how
  many are left. Check once, then wait for the user or for real work to finish.

## Running things

No terminal is attached to commands an agent runs, so `attach`, `shell LOGIN`
and `rescue` (all interactive) are for the user. Use these instead:

```bash
cluster run work -- hostname                       # one command; its exit status is the remote one
cluster run work -- sh -c 'cd proj && ls *.out'    # arguments are quoted: ~, globs, pipes and && need sh -c
cluster new -d work api -- python train.py         # start session "api" running a command, detached
cluster sessions work                              # sessions, windows and attached clients
cluster send work api -- 'make && make test'       # type a line into session "api" and press Enter
cluster run work -- tmux capture-pane -p -t =api: -S -200   # read the last 200 lines of its screen
cluster window work api build                      # add window "build" to session "api"
```

- The first word is always a command; there is no `cluster NAME` form.
- Everything after `--` is the remote command; `run` and `send` always need
  the `--`.
- `window` is `[LOGIN] SESSION WINDOW`: `cluster window work build` adds window
  `build` to session `work` on the default login. It does not make a session.
- `send` matches the session name exactly. In raw tmux commands sent through
  `run`, write the target as `=NAME:` so tmux does not take a session whose
  name merely starts with NAME.
- Sessions are node-local: `cluster sessions LOGIN` shows only the sessions on
  that login's node.

## Files

```bash
cluster push work ./data projects/          # rsync: ./data -> ~/projects/data
cluster pull work results/ ./out/           # rsync: the contents of ~/results -> ./out/
cluster transfer ./big work:scratch/        # rclone, behaves like cp; --dry-run shows the plan
cluster transfer --via work ./a.py remote:jobs/   # ride login work instead of a new connection
cluster transfer fasrc:~/runs/x nersc:~/runs/     # cluster to cluster
```

- Remote paths in `push` and `pull` are relative to the cluster home.
- Without `--via`, `transfer` opens a connection of its own, which on FASRC is
  an authentication.
- The mount at `~/cluster_mounts/<backend>/<login>/` is sshfs. Read the files
  you need, but never walk it (`find`, `grep -r`, `ls -R`, indexers): symlinks
  there can lead into multi-terabyte lab storage, and a busy mount slows every
  process that touches it. Search on the cluster with `cluster run` instead.

## Ask the user first

These kill sessions, move logins or delete files. Say what will happen and get
a yes before running them; use the dry run first where there is one.

- `cluster close LOGIN` kills the login's tmux sessions (`--keep-tmux` keeps
  them).
- `cluster kill-session LOGIN SESSION`.
- `cluster clean` (run `cluster clean --dry-run` first). On FASRC each node it
  visits is an authentication.
- `cluster repin`, `unpin` and `refresh` move a login to another node and close
  its sessions.
- `cluster strays check|adopt|rename|clear|kill`, `cluster forget`,
  `cluster rename`.
- `transfer --sync` or `--move`, and `push` or `pull` with `--delete`.

Without a terminal, a command that would ask a question refuses and changes
nothing unless given `-y`. Never add `-y` to get past that refusal: show the
user the question and let them answer it.

## Never

- Read, print or copy anything in `~/.config/cluster/credentials/`, or put a
  password or TOTP code on a command line. `cluster config credentials` is for
  the user to run.
- Kill `cluster` processes (`cluster:w:LOGIN` in `ps` is a watcher) or ssh
  control masters to fix something: that drops every session and transfer
  riding the login.
- Run the same live command in a loop against a cluster.

## When something fails

- `cluster status` and `cluster doctor` explain most states.
- `channel N: open failed` means the login is out of channels:
  `cluster channels LOGIN` names what holds them.
- "pinned to NODE, which is not accepting connections" means that node is down
  and the login's sessions live there. Report it; moving the login is the
  user's call.
- "waiting for pid N to finish with login X" is another `cluster` command at
  work. Let it finish.
- Exit status 255 from `run` usually means the connection is gone. Running it
  again reopens the login, which on FASRC is an authentication.
