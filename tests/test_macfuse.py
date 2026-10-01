#!/usr/bin/env python3
"""macFUSE on macOS: choosing the kext or FSKit, and mounting through each.

Run: python3 -m unittest tests.test_macfuse
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import MACOS_ONLY, _patched, temp_state  # noqa: E402
from clustertool import macfuse, platform as plat  # noqa: E402
from clustertool.backends import load  # noqa: E402


def status(**changes):
    base = dict(installed=True, version="5.4.0", kext_shipped=True,
                kext_loaded=False, fskit_supported=True, fskit_registered=True,
                fskit_enabled=False)
    base.update(changes)
    return macfuse.Status(**base)


class TestChoosingABackend(unittest.TestCase):
    def choose(self, preference="auto", loads=False, **changes):
        tried = []

        def load_kext():
            tried.append(True)
            return loads
        with _patched(macfuse, "try_load_kext", load_kext):
            return macfuse.choose(preference, status(**changes)), tried

    def test_a_loaded_kext_comes_first(self):
        (backend, _), tried = self.choose(kext_loaded=True, fskit_enabled=True)
        self.assertEqual(backend, "kext")
        self.assertEqual(tried, [])

    def test_an_enabled_fskit_is_used_without_loading_anything(self):
        (backend, _), tried = self.choose(fskit_enabled=True)
        self.assertEqual(backend, "fskit")
        self.assertEqual(tried, [], "load_macfuse is only a last resort")

    def test_a_kext_that_loads_now_is_taken(self):
        (backend, _), tried = self.choose(loads=True)
        self.assertEqual((backend, tried), ("kext", [True]))

    def test_neither_says_what_to_allow_for_both(self):
        (backend, why), _ = self.choose()
        self.assertIsNone(backend)
        self.assertIn("File System Extensions", why)
        self.assertIn("Privacy & Security", why)

    def test_an_unregistered_fskit_module_says_to_open_the_app_first(self):
        (_, why), _ = self.choose(fskit_registered=False, kext_shipped=False)
        self.assertIn("macfuse.app once", why)

    def test_a_preference_takes_only_that_backend(self):
        (backend, why), tried = self.choose("fskit", kext_loaded=True)
        self.assertIsNone(backend)
        self.assertNotIn("Privacy & Security", why)
        self.assertEqual(tried, [])
        (backend, _), _ = self.choose("kext", fskit_enabled=True, loads=True)
        self.assertEqual(backend, "kext")

    def test_fskit_enablement_is_read_from_fskitds_own_list(self):
        import plistlib
        import tempfile

        home = Path(tempfile.mkdtemp())
        self.assertFalse(macfuse.fskit_enabled(home), "never turned on")
        path = home / macfuse.FSKIT_ENABLED_LIST
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps(["com.apple.fskit.exfat"]))
        self.assertFalse(macfuse.fskit_enabled(home))
        path.write_bytes(plistlib.dumps([macfuse.FSKIT_LOCAL, "com.apple.fskit.exfat"]))
        self.assertTrue(macfuse.fskit_enabled(home))
        path.write_bytes(b"not a plist")
        self.assertIsNone(macfuse.fskit_enabled(home))


class TestMountingThroughFskit(unittest.TestCase):
    """sshfs stays in the foreground, and a live volume is released first."""

    def setUp(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        temp_state(self)
        self.mounts = Mounts(Logins(load("fasrc")))
        self.mounts.logins.is_active = lambda _name: True
        self.mounts.logins.node_of = lambda _name: "login01.example"
        self.events, self.table, self.spawned = [], set(), []
        self.mounts.logins._stop_pid = lambda pid: self.events.append(("stop", pid))
        self.processes = []
        self.addCleanup(lambda: None)
        patches = [
            (plat, "IS_MAC", True),
            (plat, "mount_tools_missing", lambda: []),
            (plat, "mount_table_has", lambda mp: str(mp) in self.table),
            (plat, "run", lambda argv, timeout=None, **_k:
                subprocess.CompletedProcess(argv, 0, "", "")),
            (plat, "unmount", self._unmount),
            (plat, "own_processes", lambda: self.processes),
            (plat, "spawn_detached", self._spawn),
            (macfuse, "usable", lambda _p="auto": ("fskit", "")),
        ]
        import contextlib
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target, attr, value in patches:
            stack.enter_context(_patched(target, attr, value))

    def _unmount(self, mp):
        self.events.append(("unmount", str(mp)))
        self.table.discard(str(mp))
        return True

    def _spawn(self, argv, log_path=None, env=None):
        self.spawned.append(argv)
        self.table.add(argv[-1])
        self.processes.append((4242, " ".join(argv)))

        class Running:
            returncode = None

            def poll(self):
                return None
        return Running()

    def test_sshfs_runs_in_the_foreground_with_the_fskit_backend(self):
        self.mounts.mount("main", quiet=True)
        argv = self.spawned[0]
        self.assertEqual(argv[:2], ["sshfs", "-f"])
        self.assertIn("backend=fskit", argv)
        self.assertEqual(self.mounts.state.read_meta("main")["fuse_backend"], "fskit")

    def test_a_live_fskit_volume_is_unmounted_before_its_daemon_is_stopped(self):
        self.mounts.mount("main", quiet=True)
        mp = str(self.mounts.mountpoint("main"))
        self.mounts.healthy = lambda _name: True
        with _patched(self.mounts, "_await_daemon_exit", lambda *a, **k: None):
            self.mounts.unmount("main", quiet=True)
        self.assertEqual(self.events[0], ("unmount", mp))

    def test_a_wedged_fskit_volume_is_released_with_its_daemon_still_there(self):
        self.mounts.mount("main", quiet=True)
        mp = str(self.mounts.mountpoint("main"))
        with _patched(self.mounts, "_await_daemon_exit", lambda *a, **k: None):
            self.assertTrue(self.mounts.unwedge("main"))
        self.assertEqual(self.events[0], ("unmount", mp))


class TestMountTable(unittest.TestCase):
    def test_fskit_volumes_are_told_apart_by_their_source(self):
        entries = [plat.MountEntry("macfuse", "/m/a", "macfuse://1234-5678"),
                   plat.MountEntry("macfuse", "/m/b", "me@host:/home")]
        with _patched(plat, "IS_MAC", True), \
                _patched(plat, "mount_entries", lambda: entries):
            self.assertTrue(plat.is_fskit_mount("/m/a"))
            self.assertFalse(plat.is_fskit_mount("/m/b"))
            self.assertFalse(plat.is_fskit_mount("/m/c"))

    def test_both_mount8_formats_are_read(self):
        text = ("/dev/disk3s1 on / (apfs, sealed, local)\n"
                "u@h:/x on /Users/u/m (macfuse, nodev, nosuid)\n"
                "u@h: on /home/u/m type fuse.sshfs (rw,nosuid)\n")
        with _patched(plat, "FORCE_PORTABLE", True), \
                _patched(plat, "out", lambda *a, **k: text):
            entries = plat.mount_entries()
        self.assertEqual([(e.on, e.fstype) for e in entries],
                         [("/", "apfs"), ("/Users/u/m", "macfuse"),
                          ("/home/u/m", "fuse.sshfs")])

    @MACOS_ONLY
    def test_getfsstat_lists_the_root_volume(self):
        entries = plat._getfsstat()
        self.assertIsNotNone(entries)
        self.assertIn("/", [entry.on for entry in entries])


if __name__ == "__main__":
    unittest.main()
