"""Credential primitives: TOTP generation and answering SSH's auth prompts.

Both clusters authenticate a human with password + a 6-digit TOTP code. They
differ in *how often*: FASRC wants it on every connection (so every ssh runs
under a pty that types the answers), while NERSC wants it once a day in exchange
for a 24-hour certificate.
"""

from __future__ import annotations

import base64
import codecs
import os
import pty
import re
import select
import signal
import sys
import time
from pathlib import Path


def totp_key(secret):
    """The key bytes of a base32 TOTP seed, formatted however it was shown.

    Spaces and hyphens are ignored, case does not matter and padding is
    optional. Raises ValueError for anything that is not base32.
    """
    cleaned = secret.strip().replace(" ", "").replace("-", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    key = base64.b32decode(cleaned + padding)  # binascii.Error is a ValueError
    if not key:
        raise ValueError("empty TOTP seed")
    return key


def totp(secret, when=None, step=30, digits=6):
    """RFC 6238 TOTP. Accepts a base32 secret with spaces or hyphens."""
    # Imported here: most commands authenticate nothing, and these load OpenSSL.
    import hashlib
    import hmac

    key = totp_key(secret)
    counter = int((time.time() if when is None else when) // step)
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{code % (10 ** digits):0{digits}d}"


def totp_window(when=None, step=30):
    """Index of the current TOTP window — the unit of code reuse."""
    return int((time.time() if when is None else when) // step)


def seconds_left_in_window(when=None, step=30):
    now = time.time() if when is None else when
    return step - (now % step)


def read_secret_file(path, what):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"missing {what}: {path}")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"{what} {path} is mode {oct(mode)[2:]}; it holds a secret, "
            f"run: chmod 600 {path}"
        )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{what} {path} is empty")
    return value


PASSWORD_PROMPT = re.compile(rb"(?i)(?:password|passcode)\s*:\s*$")
OTP_PROMPT = re.compile(rb"(?i)(?:verification\s*code|verificationcode|otp)\s*:\s*$")
COMBINED_PROMPT = re.compile(rb"(?i)password\s*\+\s*otp\s*:\s*$")

#: What may arrive *between* two answers without meaning the session has begun.
#: OpenSSH puts "(user@host) " in front of every keyboard-interactive prompt,
#: and a PAM stack that rejects a code says so before asking again. Nothing
#: else is authentication; see :class:`_AnswerLatch`.
_KI_PREFIX = re.compile(rb"^\([^()\s]*@[^()\s]*\)\s*")
_RETRY_LINE = re.compile(rb"(?i)^permission denied, please try again\.?$")
_BARE_PROMPT = re.compile(
    rb"(?i)^(?:password|passcode|verification\s*code|otp|password\s*\+\s*otp)\s*:$")
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
#: Room given to a line that has not ended yet to turn into a prompt. A real
#: prompt is a few dozen bytes; anything longer without a newline is output.
_PARTIAL_LIMIT = 256


def _auth_line(line):
    """Is *line* (one line of pty output) nothing but authentication chatter?"""
    text = _KI_PREFIX.sub(b"", line.strip(), count=1).strip()
    return not text or bool(_RETRY_LINE.match(text) or _BARE_PROMPT.match(text))


class _AnswerLatch:
    """A one-way switch from "authenticating" to "a session is running".

    The pty is the whole connection, so a prompt printed *inside* the session
    — ``sudo``, a nested ``ssh otherhost``, ``kinit``, a script's ``read -p
    "Password: "`` — looks to a regex exactly like the one ssh printed. Answering
    those types the cluster password into whatever asked.

    So answering stops for good once the session has visibly started. Before
    the first answer anything goes: banners and post-quantum warnings arrive
    there, and nothing has been typed yet. After it, every byte up to the next
    prompt must be authentication chatter — line breaks, ssh's retry line, the
    keyboard-interactive "(user@host) " prefix and the prompt itself. The first
    byte of anything else ("Last login: ...", a shell prompt) closes the latch,
    and a closed latch never reopens during this run.
    """

    def __init__(self):
        self.answered = False
        self.open = True
        #: output since the last answer, less the lines already judged
        self.pending = b""

    def feed(self, data):
        if not (self.answered and self.open):
            return
        *complete, partial = _LINE_BREAK.split(self.pending + data)
        if not all(_auth_line(line) for line in complete) \
                or len(partial) > _PARTIAL_LIMIT:
            self.open, self.pending = False, b""
            return
        self.pending = partial

    def may_answer(self):
        """Whether the prompt that just completed may be answered."""
        if not self.open:
            return False
        if not self.answered:
            return True
        # The line the prompt is on must hold nothing else: "x@y's password: "
        # or "[sudo] password for x: " is some other program asking.
        if _auth_line(self.pending):
            return True
        self.open, self.pending = False, b""
        return False

    def answering(self):
        self.answered = True
        self.pending = b""


#: Failures that happen *before* authentication and so say nothing about the
#: credentials. A login pool published as one hostname can hand a connection to a
#: node that completes the TCP handshake and then drops it, and ssh does not fall
#: through to the pool's other addresses, because connecting is exactly what
#: succeeded. Asking again is the only way past it.
TRANSIENT_SETUP = re.compile(r"""(?xi)
    connection\s+closed\s+by
  | connection\s+reset
  | kex_exchange_identification
  | banner\s+exchange
  | connection\s+timed\s+out
  | broken\s+pipe
  | no\s+route\s+to\s+host
  | temporary\s+failure\s+in\s+name\s+resolution
""")


def is_transient_setup(detail):
    """True when *detail* is a pre-authentication failure worth another try.

    Deliberately narrow. A rejected password or a refused key must never be
    retried: on a cluster that authenticates every connection, each retry spends
    a TOTP window, and repeated authentication failures are what locks an
    account. Only failures that never reached the prompt qualify.
    """
    return bool(detail) and bool(TRANSIENT_SETUP.search(detail))


#: What a refused or unusable credential says, as opposed to a failed network:
#: sshd's and sshproxy's refusals, the shared record of one (state.Refusals),
#: and read_secret_file's own complaints about a missing, exposed, empty or
#: malformed secret. None of these clears itself.
REJECTION = re.compile(r"""(?xi)
    permission\s+denied
  | credentials\s+were\s+refused
  | authentication\s+failed
  | rejected\s+the\s+credentials
  | too\s+many\s+authentication\s+failures
  | \bmissing\b[^:]*\b(?:password|seed)\b
  | holds\s+a\s+secret
  | \b(?:password|seed)\b.*\bis\s+empty\b
  | empty\s+totp\s+seed
  | base32 | incorrect\s+padding
""")


def is_rejection(detail):
    """True when *detail* says the credential itself is what failed.

    Retrying one only repeats the refusal, and on a cluster that locks an
    account after repeated failures it makes things worse.
    """
    return bool(detail) and bool(REJECTION.search(detail))


def failure_text(exc, detail=""):
    """What *exc* says went wrong, on one line: for a log, and for is_rejection.

    A SystemExit carrying text (sshproxy's refusals) says it itself. A ui.Die
    has already printed its message and carries only a status, so *detail*,
    the master's own explanation (Logins.last_failure), stands in for it. Any
    other exception is its message, or failing that its type.
    """
    if isinstance(exc, SystemExit):
        text = exc.code if isinstance(exc.code, str) else ""
    else:
        text = str(exc) or type(exc).__name__
    text = " ".join((text or detail or "no error output").split())
    return text[len("cluster: "):] if text.startswith("cluster: ") else text


def refused_by(exc, detail=""):
    """Whether *exc*, raised on the way to a connection, is the credentials
    being refused (is_rejection). Only a refusal to connect says that: an
    OSError's "Permission denied" is a file on this machine, and a
    ValueError a secret that could not be read, neither of them an answer
    from the cluster."""
    return isinstance(exc, SystemExit) and is_rejection(failure_text(exc, detail))


#: Output that explains nothing about a failure: the server's login banner,
#: ssh's own debug chatter, and the prompts we answered ourselves.
NOISE = re.compile(r"""(?xi)
    ^\s*(?: \*\* .*                       # banner lines
          | debug\d*:.*                     # ssh -v output
          | \(.*\)\s*(?:password|verificationcode|passcode)\s*:?  # prompts
          | (?:password|verificationcode)\s*:?                       # bare ones
          )\s*$""")


def explain_failure(lines):
    """The last line of a failed connection's output that says anything.

    Taking the final line would report "The server may need to be upgraded"
    for every failure, because a post-quantum banner is the last thing the
    server prints — including when what actually happened was a rejected
    password.
    """
    for line in reversed([text.strip() for text in lines if text.strip()]):
        if not NOISE.match(line):
            return line
    return "no error output"


#: How much of a pty conversation a transcript keeps. The transcript is also
#: the stdout of a marker-framed read on a backend that authenticates under a
#: pty, so it must hold a whole reply; only an interactive session prints more.
TRANSCRIPT_LIMIT = 1 << 20


class _Transcript:
    """Pty output as text, kept to the last TRANSCRIPT_LIMIT characters.

    Decoded incrementally, so a character split across two reads survives.
    Whole chunks are dropped from the front, since only the end of a long
    session can explain how it ended.
    """

    def __init__(self, chunks):
        self.chunks = chunks
        self.kept = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def add(self, data, final=False):
        text = self.decoder.decode(data, final)
        if not text:
            return
        self.chunks.append(text)
        self.kept += len(text)
        while self.kept > TRANSCRIPT_LIMIT and len(self.chunks) > 1:
            self.kept -= len(self.chunks.pop(0))


def run_with_prompts(argv, password, otp_factory, max_answers=3, quiet=False,
                     sink=None, transcript=None, env=None, timeout=None,
                     forward_stdin=True):
    """Run *argv* under a pty, typing the password and OTP when asked.

    Stdin is forwarded, so this works for interactive shells as well as for
    one-shot commands; that is what lets a single code path cover both
    ``cluster shell`` and ``cluster run``.

    The child's output goes to **stderr** by default, because for everything
    except a genuine interactive session it is authentication chatter rather
    than data — echoing "Password:" onto stdout would corrupt the output of
    ``cluster ssh-command``, whose stdout another program parses. Interactive
    callers pass ``sink=sys.stdout.buffer``.

    Only ssh's own prompts are answered: once the session has produced output
    of its own, a later password prompt belongs to something running inside
    it and is left for the person at the keyboard. See :class:`_AnswerLatch`.

    *timeout* bounds the whole run, as for :func:`clustertool.platform.run`:
    once it passes, the child's process group is killed and the result is
    124. *transcript*, a list, collects the output as text.

    *forward_stdin* False keeps this process's stdin out of the pty, for a
    child that reads nothing but the answers: a master opened from a script
    fed on stdin would otherwise swallow the rest of that script.
    """
    if sink is None:
        sink = sys.stderr.buffer
    record = _Transcript(transcript) if transcript is not None else None
    deadline = None if timeout is None else time.monotonic() + timeout

    child_pid, fd = pty.fork()
    if child_pid == 0:
        # execvpe, not execvp, so a caller can hand this child an environment —
        # a scoped ssh-agent is passed to the master this way (SSH_AUTH_SOCK
        # must be set for the *master*, since it is the master that proxies a
        # forwarded agent, not the client that asks for one later).
        try:
            os.execvpe(argv[0], argv, env or os.environ.copy())  # noqa: S606
        except BaseException as exc:  # noqa: BLE001 - nothing may survive this
            # Falling out of here would leave a forked copy of the whole CLI
            # running on the pty, holding the parent's locks and state.
            try:
                os.write(2, f"cluster: cannot run {argv[0]}: {exc}\n".encode(
                    "utf-8", "replace"))
            finally:
                os._exit(127)

    try:
        timed_out = _converse(fd, password, otp_factory, max_answers, quiet,
                              sink, record, deadline, forward_stdin)
        if timed_out:
            # pty.fork made the child a session leader, so its process group
            # is everything it started. SIGKILL, because what is hung here is
            # typically blocked in a way that ignores anything gentler.
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _, status = os.waitpid(child_pid, 0)
    finally:
        os.close(fd)
    if record is not None:
        record.add(b"", final=True)
        if timed_out:
            record.chunks.append(f"\ncluster: gave up after {timeout:g}s\n")
    if timed_out:
        return 124
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _converse(fd, password, otp_factory, max_answers, quiet, sink, record,
              deadline, forward_stdin=True):
    """Relay between the pty and stdin until the child closes it.

    Returns True when *deadline* passed first.
    """
    sent_password = 0
    sent_otp = 0
    tail = b""
    latch = _AnswerLatch()

    try:
        stdin_fd = sys.stdin.fileno()
        stdin_open = forward_stdin and (sys.stdin.isatty() or not sys.stdin.closed)
    except (ValueError, AttributeError, OSError):
        stdin_fd, stdin_open = -1, False

    while True:
        wait = None
        if deadline is not None:
            wait = deadline - time.monotonic()
            if wait <= 0:
                return True
        watch = [fd] + ([stdin_fd] if stdin_open else [])
        try:
            readable, _, _ = select.select(watch, [], [], wait)
        except InterruptedError:
            continue
        except OSError:
            return False

        if fd in readable:
            try:
                data = os.read(fd, 4096)
            except OSError:
                return False
            if not data:
                return False
            if not quiet:
                sink.write(data)
                sink.flush()
            if record is not None:
                record.add(data)
            tail = (tail + data)[-512:]
            latch.feed(data)

            answer = None
            if COMBINED_PROMPT.search(tail):
                # NERSC-style single field: password immediately followed by OTP.
                if sent_password < max_answers and latch.may_answer():
                    answer = lambda: password + otp_factory()  # noqa: E731
                    sent_password += 1
            elif PASSWORD_PROMPT.search(tail):
                if sent_password < max_answers and latch.may_answer():
                    answer = lambda: password  # noqa: E731
                    sent_password += 1
            elif OTP_PROMPT.search(tail):
                if sent_otp < max_answers and latch.may_answer():
                    answer = otp_factory
                    sent_otp += 1
            if answer is not None:
                latch.answering()
                os.write(fd, answer().encode() + b"\r")
                tail = b""

        if stdin_open and stdin_fd in readable:
            try:
                data = os.read(stdin_fd, 4096)
            except OSError:
                data = b""
            if data:
                os.write(fd, data)
            else:
                stdin_open = False
