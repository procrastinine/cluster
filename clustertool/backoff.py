"""When to try again, and when to stop trying.

One rule for every loop that keeps something going across failures (an
attach reconnecting, a transfer resuming, the watcher repairing, the relay
re-running a stream). Each failure adds one to a score, and every second that
things then go well fades the score with a half-life. The wait before the next
try doubles with the score, up to a ceiling, and a loop that gives up at all
gives up only when the score passes its limit.

So a run that meets a drop now and then over days never adds those up to
"gave up": each has faded by the time the next comes. A burst of failures one
after another does, and stops, after waits that grew instead of a line a
second. A fixed count of failures over the whole run cannot tell the two
apart, and a counter reset after so many healthy seconds forgets a burst the
moment it pauses. The score stops growing where one more failure would change
nothing (FailureMemory.ceiling), so an outage of a day is forgotten as fast
as the burst that first reached the longest wait.

It depends on nothing else in the package, so bin/cluster-relay's client
half, which reads none of the package's state, can use it too.
"""

from __future__ import annotations

import math


class FailureMemory:
    """Recent failures, remembered less the longer things go well between them.

    *half_life* is the healthy seconds that halve the score (0 forgets at
    once). *limit* is the score a caller gives up beyond, or None for a loop
    that never gives up. The wait starts at *delay* once more than *grace*
    failures are fresh, and doubles with each one after, up to *delay_max*.
    The caller says how long things went well; only it knows whether the time
    since the last failure was spent working or waiting to reconnect.
    """

    def __init__(self, half_life, limit=None, delay=1.0, delay_max=60.0, grace=0):
        self.half_life = max(0.0, float(half_life))
        self.limit = limit
        self.delay = max(0.0, float(delay))
        self.delay_max = max(self.delay, float(delay_max))
        self.grace = max(0, grace)
        self.score = 0.0

    def healthy(self, seconds):
        """Fade the score by *seconds* of things going well; the new score."""
        if seconds > 0 and self.score:
            if self.half_life:
                self.score *= 0.5 ** (seconds / self.half_life)
            else:
                self.score = 0.0
        return self.score

    def failed(self, healthy=0.0):
        """Record a failure that came after *healthy* seconds of things going
        well; the new score."""
        self.healthy(healthy)
        self.score = min(self.score + 1.0, self.ceiling)
        return self.score

    @property
    def ceiling(self):
        """The highest score worth keeping: the one whose wait has reached
        *delay_max*, or for a loop that gives up, one past its *limit* if
        that is higher. A failure past it changes nothing a caller does, and
        counting it would only make healthy time after a long outage take
        longer to fade it."""
        steps = math.ceil(math.log2(self.delay_max / self.delay)) if self.delay else 0
        top = 1.0 + self.grace + steps
        return top if self.limit is None else max(top, self.limit + 1.0)

    @property
    def exhausted(self):
        """True once the score has passed the limit: time to stop trying."""
        return self.limit is not None and self.score > self.limit

    @property
    def fresh(self):
        """How many recent failures the score amounts to, as a whole number."""
        return int(round(self.score))

    def wait(self):
        """Seconds to wait before the next try."""
        steps = self.score - 1.0 - self.grace
        if steps < 0:
            return 0.0
        # Past 64 doublings the ceiling has long been reached, and 2.0 ** a
        # large enough exponent overflows.
        return min(self.delay_max, self.delay * 2.0 ** min(steps, 64.0))
