"""Terminal output helpers."""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap

PROG = "cluster"

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

#: Gap between table columns.
_GAP = "  "
#: Narrowest a column may be squeezed to before the table is given up on and
#: the stacked layout takes over. Eight characters is where a node name stops
#: being recognisable ('holy7c0…'), and an unreadable identifier is worse than
#: the taller layout that keeps it. The gate scales with the column count, so a
#: three-column table stays tabular far longer than a seven-column one.
_MIN_CELL = 8
#: Assumed geometry when the terminal cannot be measured.
_FALLBACK_SIZE = (80, 24)


def _wants_color(stream):
    """Colour only for a terminal, and never under NO_COLOR."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


def _paint(text, code, stream=None):
    """*text* in colour *code* if *stream* (stdout by default) shows colour.

    Decided for the stream the text is written to, when it is written: the
    colour helpers below feed `say` and `table`, which print to stdout, so
    `cluster clean | tee log` gets plain text however stderr is connected.
    """
    stream = sys.stdout if stream is None else stream
    return f"\033[{code}m{text}\033[0m" if _wants_color(stream) else text


class Die(SystemExit):
    """Fatal error already reported to the user."""


def die(message, *hints, code=1):
    print(f"{PROG}: {message}", file=sys.stderr)
    for hint in hints:
        print(f"  {hint}", file=sys.stderr)
    raise Die(code)


def confirm(question, yes=False, unattended_hint="pass -y to go ahead"):
    """Ask *question*, or die rather than guess when nobody can answer.

    The refusal is the point. A prompt that falls back to "no" makes a cron
    line silently skip the work it was asked to do; one that falls back to
    "yes" lets an unattended run destroy something nobody agreed to. Neither
    is a default worth having, so an unattended caller has to say -y.
    """
    if yes:
        return True
    from . import platform as plat

    if not plat.terminal_attached():
        die("refusing to change anything unattended", unattended_hint)
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def stdin_is_terminal():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except ValueError:  # closed
        return False


def _answer(shown, secret=False):
    """One line from stdin after *shown*, on stderr; EOFError once it ends.

    At a terminal a secret is read without echo. Anywhere else the answer is
    simply the next line of stdin, which is what makes the questions
    scriptable: getpass would read the terminal instead of the pipe, or warn.
    """
    if secret and stdin_is_terminal():
        import getpass

        return getpass.getpass(shown)
    sys.stderr.write(shown)
    sys.stderr.flush()
    try:
        line = sys.stdin.readline() if sys.stdin is not None else ""
    except ValueError:  # closed
        line = ""
    if not stdin_is_terminal() or not line:
        sys.stderr.write("\n")
    if not line:
        raise EOFError(shown)
    return line


def ask(question, default="", secret=False):
    """The answer to *question* from whoever is at stdin, a person or a script.

    Returns it stripped, or *default* when it is empty. The question goes to
    stderr, so stdout keeps only results. Raises EOFError when stdin has
    ended, since no answer is not the same as an empty one.
    """
    shown = question + (f" [{default}]" if default and not secret else "") + ": "
    return _answer(shown, secret=secret).strip() or default


def ask_yes(question, default=False):
    """A yes or no to *question*; *default* when the answer is empty or stdin
    has ended. A script's answer that is neither stops rather than being
    guessed at; a person is asked again."""
    shown = f"{question} [{'Y/n' if default else 'y/N'}] "
    for _ in range(3 if stdin_is_terminal() else 1):
        try:
            answer = _answer(shown).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if not stdin_is_terminal():
            die(f"expected y or n, not {answer!r}")
        print("  answer y or n", file=sys.stderr)
    return default


def warn(message):
    print(f"{PROG}: {_paint('warning', '33', sys.stderr)}: {message}",
          file=sys.stderr)


def info(message):
    print(f"{PROG}: {message}", file=sys.stderr)


def note(message):
    print(f"  {message}", file=sys.stderr)


def say(message=""):
    # Flushed because every other voice in this module writes to stderr, which
    # is unbuffered: without this, `cluster ls | tee log` prints the hints
    # *above* the table they are about.
    print(message, flush=True)


def bold(text):
    return _paint(text, "1")


def dim(text):
    return _paint(text, "2")


def green(text):
    return _paint(text, "32")


def red(text):
    return _paint(text, "31")


def yellow(text):
    return _paint(text, "33")


# --- terminal geometry -------------------------------------------------------
# Everything below asks the *output* stream, like the colour decision above: a
# table goes to stdout, and using another stream's answer for it is how bold
# escapes end up inside a pipe and how a redirected table gets truncated to a
# tty's width.

def terminal_size(stream=None):
    """``(columns, rows)`` for `stream`, or ``(0, 0)`` when it is not a tty.

    ``(0, 0)`` means "unconstrained" and is deliberately distinct from a
    fallback guess: a pipe or a file has no width, and trimming output there
    would corrupt whatever is parsing it on the other end.
    """
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return (0, 0)
    except Exception:
        return (0, 0)
    try:
        columns, rows = os.get_terminal_size(stream.fileno())
        if columns > 0:
            return (columns, rows)
    except (OSError, ValueError, AttributeError):
        pass
    # COLUMNS/LINES, then the stdout fallback, is what shutil consults; it is
    # also the escape hatch for forcing a width (`COLUMNS=200 cluster ls`).
    columns, rows = shutil.get_terminal_size(_FALLBACK_SIZE)
    return (max(columns, 0), max(rows, 0))


def terminal_width(stream=None):
    """Usable columns for `stream`, or 0 when output is not a terminal."""
    return terminal_size(stream)[0]


def visible(text):
    """`text` without escape sequences, i.e. what the terminal actually shows."""
    return _ANSI.sub("", str(text))


def visual_lines(text, width):
    """Terminal rows `text` occupies once the terminal has wrapped it.

    Needed by any caller that wants to erase what it printed: the number of
    ``\\n`` in a string stops matching the number of rows on screen as soon as
    one line is longer than the terminal is wide.
    """
    total = 0
    for line in str(text).split("\n"):
        length = len(visible(line))
        total += 1 if not width or length == 0 else -(-length // width)
    return total


# --- tables ------------------------------------------------------------------

def _ellipsis():
    """'…' where the encoding allows it, ASCII where it does not."""
    encoding = getattr(sys.stdout, "encoding", None) or ""
    try:
        "…".encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return "~"
    return "…"


def _clip(text, cap):
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    mark = _ellipsis()
    return text[:cap - len(mark)] + mark if cap > len(mark) else mark[:cap]


def _water_fill(widths, budget):
    """Cap every column at the largest shared limit that fits, or None.

    A shared limit takes the space out of the widest columns first, which is
    the right place for it — the wide columns here are free text (MOUNT,
    SESSIONS, MEANING) while the narrow ones are identifiers (BACKEND, STATE)
    that go to nonsense the moment a character is removed.
    """
    if not widths:
        return []
    if budget < _MIN_CELL * len(widths):
        return None
    if sum(widths) <= budget:
        return list(widths)
    low, high, cap = _MIN_CELL, max(widths), None
    while low <= high:
        mid = (low + high) // 2
        if sum(min(w, mid) for w in widths) <= budget:
            cap, low = mid, mid + 1
        else:
            high = mid - 1
    if cap is None:
        return None
    caps = [min(w, cap) for w in widths]
    # Give the rounding slack back, widest column first, so the budget is spent.
    order = sorted(range(len(widths)), key=lambda i: widths[i], reverse=True)
    slack = budget - sum(caps)
    while slack > 0:
        spent = False
        for index in order:
            if caps[index] < widths[index]:
                caps[index] += 1
                slack -= 1
                spent = True
                if not slack:
                    break
        if not spent:
            break
    return caps


def _fit_caps(widths, width):
    """Per-column caps that make one row fit `width`, or None if impossible."""
    budget = width - len(_GAP) * (len(widths) - 1)
    if budget < _MIN_CELL * len(widths):
        return None
    if sum(widths) <= budget:
        return list(widths)
    # The first column is the row's identity in every table here — SETTING,
    # LOGIN, NODE, CLASS — and the string you retype into the next command, so
    # it gets up to a third of the line before the shared cap applies to it.
    # A shared cap alone would squeeze BOOT_RETRY_DELAY and BOOT_RETRY_DELAY_MAX
    # to the same fifteen characters. Where the key is already short, as in the
    # BACKEND-first tables, this reserve is inert.
    keep = min(widths[0], max(_MIN_CELL, budget // 3))
    rest = _water_fill(widths[1:], budget - keep)
    if rest is None:
        return _water_fill(widths, budget)
    # `keep` only sizes the other columns' budget. Whatever they did not need
    # comes back here rather than being left as trailing blank, which is what
    # a table of one wide column plus a few short ones is made of.
    return [min(widths[0], budget - sum(rest))] + rest


def _stacked(rows, headers, width):
    """One field per line: the only layout that survives any width.

    Reached when even a squeezed table cannot fit. Records are separated by a
    blank line, and a long value wraps under a hanging indent, so nothing runs
    into the record above it and nothing is dropped.
    """
    label = max(len(h) for h in headers)
    room = max(1, width - label - len(_GAP))
    pad = " " * (label + len(_GAP))
    lines = []
    for index, row in enumerate(rows):
        if index:
            lines.append("")
        for header, cell in zip(headers, row):
            lead = header.rjust(label) + _GAP
            pieces = textwrap.wrap(cell, room, break_long_words=True,
                                   break_on_hyphens=False) or [""]
            lines.append(lead + pieces[0])
            lines.extend(pad + piece for piece in pieces[1:])
    return "\n".join(lines)


def render_table(rows, headers, width=None):
    """Return aligned columns as one redraw-friendly string, sized to fit.

    Sizing is the point. Rendered at their natural width these tables run to
    about 110 columns, and a terminal narrowed for an editor would otherwise
    hard-wrap every row at the edge with no indent, so each row would spill
    onto the next and consecutive rows would run together unreadably.

    `width` of 0 means unconstrained, which is what a pipe or a file gets: only
    a terminal has a width to respect, and a script reading this output wants
    every character. Pass it explicitly to render for a width other than the
    one the output stream currently has.
    """
    if not rows:
        return ""
    headers = [str(h) for h in headers]
    count = len(headers)
    # Tolerate a short or long row rather than raising from a display path.
    cells = [[visible(row[i]) if i < len(row) else "" for i in range(count)]
             for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    if width is None:
        width = terminal_width()
    caps = widths
    if width and sum(widths) + len(_GAP) * (count - 1) > width:
        caps = _fit_caps(widths, width)
        if caps is None:
            return _stacked(cells, headers, max(width, _MIN_CELL * 2))

    def line(values):
        return _GAP.join(_clip(v, caps[i]).ljust(caps[i])
                         for i, v in enumerate(values)).rstrip()

    lines = [bold(line(headers))]
    lines.extend(line(row) for row in cells)
    return "\n".join(lines)


def table(rows, headers):
    """Print aligned columns, sized to the terminal."""
    rendered = render_table(rows, headers)
    if rendered:
        print(rendered)
