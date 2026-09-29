#!/usr/bin/env python3
"""Backoff: the failure memory every retrying loop shares.

Run: python3 -m unittest tests.test_backoff
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402,F401
from clustertool.backoff import FailureMemory  # noqa: E402


class TestFailureMemory(unittest.TestCase):
    def test_a_burst_passes_the_limit_after_exactly_limit_failures(self):
        memory = FailureMemory(half_life=300, limit=3)
        for _ in range(3):
            memory.failed()
            self.assertFalse(memory.exhausted)
        memory.failed()
        self.assertTrue(memory.exhausted)

    def test_failures_spread_over_days_never_add_up(self):
        memory = FailureMemory(half_life=300, limit=3)
        for _ in range(1000):
            memory.failed(healthy=6 * 3600)
            self.assertFalse(memory.exhausted)
        self.assertLess(memory.score, 1.01)

    def test_healthy_time_halves_the_score_per_half_life(self):
        memory = FailureMemory(half_life=100)
        memory.failed()
        memory.failed()
        self.assertAlmostEqual(memory.healthy(100), 1.0)
        self.assertAlmostEqual(memory.healthy(200), 0.25)

    def test_a_pause_in_a_burst_does_not_forget_it(self):
        """What a counter reset after N healthy seconds gets wrong."""
        memory = FailureMemory(half_life=600, limit=4)
        for _ in range(4):
            memory.failed()
        memory.failed(healthy=130)
        self.assertTrue(memory.exhausted, memory.score)

    def test_drops_that_come_too_often_still_stop_it(self):
        memory = FailureMemory(half_life=60, limit=8)
        for _ in range(100):
            memory.failed(healthy=5)
            if memory.exhausted:
                break
        self.assertTrue(memory.exhausted)

    def test_the_wait_doubles_with_the_score_up_to_the_ceiling_past_the_grace(self):
        for grace, delay, delay_max, wanted in ((0, 2, 60, [2, 4, 8, 16, 32, 60, 60]),
                                                (4, 30, 600, [0, 0, 0, 0, 30, 60, 120])):
            memory = FailureMemory(half_life=300, delay=delay, delay_max=delay_max,
                                   grace=grace)
            self.assertEqual(memory.wait(), 0)
            waits = []
            for _ in range(7):
                memory.failed()
                waits.append(memory.wait())
            self.assertEqual(waits, wanted)

    def test_the_wait_shrinks_again_as_the_failures_fade(self):
        memory = FailureMemory(half_life=100, delay=2, delay_max=60)
        for _ in range(5):
            memory.failed()
        long_wait = memory.wait()
        memory.failed(healthy=1000)
        self.assertLess(memory.wait(), 2.1)
        self.assertGreater(long_wait, 30)

    def test_no_limit_never_gives_up_and_never_overflows(self):
        memory = FailureMemory(half_life=300, delay=1, delay_max=600)
        for _ in range(5000):
            memory.failed()
        self.assertFalse(memory.exhausted)
        self.assertEqual(memory.wait(), 600)

    def test_a_zero_half_life_forgets_at_once(self):
        memory = FailureMemory(half_life=0, limit=1)
        memory.failed()
        memory.failed(healthy=0.1)
        self.assertEqual(memory.score, 1.0)
        self.assertFalse(memory.exhausted)

    def test_fresh_rounds_the_score(self):
        memory = FailureMemory(half_life=100)
        memory.failed()
        memory.failed()
        memory.failed()
        self.assertEqual(memory.fresh, 3)
        memory.healthy(100)
        self.assertEqual(memory.fresh, 2)


class TestTheScoreHasACeiling(unittest.TestCase):
    """Past the score whose wait is the longest, one more failure changes
    nothing a loop does, so it is not counted."""

    def test_the_score_stops_where_the_wait_is_longest_and_a_loop_still_gives_up(self):
        # 2, 4, 8, 16, 32, then 60; and with a limit, one past it.
        for limit, score in ((None, 6.0), (8, 9.0)):
            memory = FailureMemory(half_life=300, limit=limit, delay=2, delay_max=60)
            for _ in range(1000):
                memory.failed()
            self.assertEqual(memory.score, score)
            self.assertEqual(memory.wait(), 60)
            self.assertEqual(memory.exhausted, limit is not None)

    def test_a_long_outage_is_remembered_no_longer_than_a_burst(self):
        # The watcher's own memory, twelve hours down as it ticks them, and
        # the one that has just reached its longest wait: healthy time then
        # forgets both alike.
        from clustertool.config import Settings

        settings = Settings("fasrc")
        interval = settings.int("WATCH_INTERVAL")

        def watcher():
            return FailureMemory(half_life=settings.int("WATCH_FAILURE_HALF_LIFE"),
                                 delay=interval,
                                 delay_max=settings.int("WATCH_BACKOFF_MAX"),
                                 grace=settings.int("WATCH_RETRIES") - 1)

        outage, down = watcher(), 0.0
        while down < 12 * 3600:
            outage.failed()
            down += interval + outage.wait()
        burst = watcher()
        while burst.wait() < burst.delay_max:
            burst.failed()
        self.assertEqual(outage.score, burst.score)


if __name__ == "__main__":
    unittest.main()
