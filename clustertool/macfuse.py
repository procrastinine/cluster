"""macFUSE on macOS: which of its two backends can mount here.

macFUSE 5 mounts through either its kernel extension or Apple's FSKit, and
each has a gate macOS keeps behind a person's consent:

* **kext**: on Apple silicon, Reduced Security with user-managed kernel
  extensions (set in recoveryOS), then the extension allowed in System
  Settings, Privacy & Security. It is loaded on demand by the setuid
  ``load_macfuse`` that every mount runs.
* **FSKit** (macOS 26 and later): macFUSE's file system modules turned on in
  System Settings, General, Login Items & Extensions, File System Extensions.
  No kernel extension, no restart.

Neither gate is visible to sshfs. A mount through a backend that is not
allowed does not fail: mount_macfuse waits for an approval that never comes,
holding a channel of the login's connection. So the backend is decided here,
before anything is started, from what macOS records:

* the kext counts as ready only once it is loaded (``kmutil showloaded``);
  ``load_macfuse`` is the authoritative test of whether it *can* load, and
  only a mount runs that, because loading it is what a mount does anyway;
* FSKit counts as ready when fskitd's own list of enabled modules names
  macFUSE's. That list is the file System Settings writes; pluginkit's
  election flag is not what fskitd consults, and can read ``+`` for a module
  fskitd refuses as "not enabled" (measured on macOS 27.0, macFUSE 5.4.0).

With FSKit, sshfs must also stay in the foreground: daemonizing after the
mount is refused ("fuse: forking after mount is not supported") and the
parent then never returns. ``Mounts.mount`` handles that.
"""

from __future__ import annotations

import collections
import os
import plistlib
from pathlib import Path

from . import platform as plat

BUNDLE = Path("/Library/Filesystems/macfuse.fs")
LOADER = BUNDLE / "Contents/Resources/load_macfuse"
APP = BUNDLE / "Contents/Resources/macfuse.app"
KEXT_ID = "io.macfuse.filesystems.macfuse"

#: The FSKit modules macFUSE ships; sshfs mounts through the ``-local`` one.
FSKIT_MODULES = ("io.macfuse.app.fsmodule.macfuse-local",
                 "io.macfuse.app.fsmodule.macfuse")
FSKIT_LOCAL = FSKIT_MODULES[0]

#: fskitd's record of the modules System Settings has turned on.
FSKIT_ENABLED_LIST = ("Library/Group Containers/group.com.apple.fskit.settings/"
                      "enabledModules.plist")

#: The first macOS with FSKit modules macFUSE can mount through.
FSKIT_MIN_MACOS = 26

KEXT, FSKIT = "kext", "fskit"
BACKENDS = (KEXT, FSKIT)

FSKIT_HOW = ("turn on macFUSE in System Settings > General > Login Items & "
             "Extensions > File System Extensions")
KEXT_HOW = ("allow the macFUSE system extension in System Settings > Privacy & "
            "Security (on Apple silicon, first choose Reduced Security in "
            "Startup Security Utility; see docs/setup.md#mounts-on-macos)")

Status = collections.namedtuple("Status", [
    "installed",        # the macFUSE bundle is present
    "version",          # its version, "" if unreadable
    "kext_shipped",     # it has a kernel extension for this macOS
    "kext_loaded",      # that extension is loaded now
    "fskit_supported",  # this macOS and this macFUSE both have FSKit modules
    "fskit_registered", # macOS knows the modules (macfuse.app has been opened)
    "fskit_enabled",    # fskitd will mount through them; None when unreadable
])


def macos_major():
    """This Mac's macOS major version, 0 when unknown."""
    import platform as pyplatform

    try:
        return int(pyplatform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def _version():
    try:
        with (BUNDLE / "Contents/Info.plist").open("rb") as handle:
            return str(plistlib.load(handle).get("CFBundleShortVersionString", ""))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""


def _kext_shipped(major):
    # The bundle keeps one kernel extension per macOS major version it
    # supports, in a directory named after it.
    return major > 0 and (BUNDLE / "Contents/Extensions" / str(major)).is_dir()


def kext_loaded():
    out = plat.out([plat.system_tool("kmutil"), "showloaded", "--list-only",
                    "--bundle-identifier", KEXT_ID], timeout=15)
    return KEXT_ID in out


def _fskit_shipped():
    return all((APP / "Contents/Extensions" / f"{module}.appex").is_dir()
               for module in FSKIT_MODULES)


def fskit_registered():
    out = plat.out(["pluginkit", "-m", "-i", FSKIT_LOCAL], timeout=15)
    return FSKIT_LOCAL in out


def fskit_module_pids():
    """This user's running macFUSE FSKit extension processes."""
    return [pid for pid, cmd in plat.own_processes()
            if "/io.macfuse.app.fsmodule.macfuse" in cmd.split(" ", 1)[0]]


def fskit_enabled(home=None):
    """Does fskitd's enabled list name macFUSE's local module? None if unread.

    A missing list is a Mac where no module was ever turned on: False.
    """
    path = Path(home or Path.home()) / FSKIT_ENABLED_LIST
    try:
        with path.open("rb") as handle:
            enabled = plistlib.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return isinstance(enabled, list) and FSKIT_LOCAL in enabled


def status():
    """What this Mac's macFUSE can do. Runs no mount and loads nothing."""
    if not BUNDLE.is_dir():
        return Status(False, "", False, False, False, False, False)
    major = macos_major()
    fskit = major >= FSKIT_MIN_MACOS and _fskit_shipped()
    return Status(
        installed=True,
        version=_version(),
        kext_shipped=_kext_shipped(major),
        kext_loaded=kext_loaded(),
        fskit_supported=fskit,
        fskit_registered=fskit and fskit_registered(),
        fskit_enabled=fskit_enabled() if fskit else False,
    )


def try_load_kext():
    """Load the kernel extension the way a mount would. True once loaded.

    load_macfuse is setuid root and returns at once, non-zero when macOS
    refuses the extension, so this is the one test that cannot be wrong
    about whether a kext mount will work.
    """
    if not os.access(LOADER, os.X_OK):
        return False
    return plat.run([str(LOADER)], timeout=30).returncode == 0 or kext_loaded()


def choose(preference="auto", st=None, load=True):
    """(backend, why_not): the backend to mount with, or (None, what to do).

    *preference* is MACFUSE_BACKEND: ``auto`` takes a loaded kext, then an
    enabled FSKit, then a kext that loads now; ``kext`` and ``fskit`` take only
    that one. *load* False never runs load_macfuse (doctor reports without
    changing anything).
    """
    st = st or status()
    if not st.installed:
        return None, "macFUSE is not installed"
    want_kext = preference in ("auto", KEXT)
    want_fskit = preference in ("auto", FSKIT)
    if want_kext and st.kext_loaded:
        return KEXT, ""
    if want_fskit and st.fskit_supported and st.fskit_enabled is not False:
        return FSKIT, ""
    if want_kext and st.kext_shipped and load and try_load_kext():
        return KEXT, ""
    return None, advice(st, preference)


#: How long a "no backend" answer is reused before macOS is asked again; a
#: backend that works is reused for the life of the process.
RECHECK = 300.0

_last = {}


def usable(preference="auto"):
    """(backend, why_not), as `choose`, reused within RECHECK.

    The watcher asks on every tick; kmutil, pluginkit and load_macfuse each
    take a moment, and an approval does not appear between two ticks.
    """
    import time

    now = time.monotonic()
    cached = _last.get(preference)
    if cached and (cached[1][0] is not None or now - cached[0] < RECHECK):
        return cached[1]
    answer = choose(preference)
    _last[preference] = (now, answer)
    return answer


def advice(st, preference="auto"):
    """What to do so a mount can work, for the backends *preference* allows."""
    if not st.installed:
        return "macFUSE is not installed"
    steps = []
    if preference in ("auto", FSKIT) and st.fskit_supported:
        if not st.fskit_registered:
            steps.append(f"open {APP} once, then {FSKIT_HOW}")
        else:
            steps.append(FSKIT_HOW)
    if preference in ("auto", KEXT) and st.kext_shipped:
        steps.append(KEXT_HOW)
    if not steps:
        if preference == FSKIT:
            return (f"FSKit needs macOS {FSKIT_MIN_MACOS} or later and macFUSE 5; "
                    "update, or set MACFUSE_BACKEND auto")
        return (f"macFUSE {st.version or ''} has no kernel extension for macOS "
                f"{macos_major()}; update macFUSE").replace("  ", " ")
    return "; or ".join(steps)


def describe(st):
    """One line per backend for doctor: (label, ok, detail)."""
    if not st.installed:
        return []
    rows = []
    if st.kext_loaded:
        rows.append(("macFUSE kext", True, "loaded"))
    elif st.kext_shipped:
        rows.append(("macFUSE kext", None,
                     "not loaded; a mount loads it if macOS allows it, "
                     "otherwise " + KEXT_HOW))
    else:
        rows.append(("macFUSE kext", False, f"none for macOS {macos_major()}"))
    if not st.fskit_supported:
        rows.append(("macFUSE FSKit", False,
                     f"needs macOS {FSKIT_MIN_MACOS}+ and macFUSE 5"))
    elif st.fskit_enabled:
        rows.append(("macFUSE FSKit", True, "enabled"))
    elif st.fskit_enabled is None:
        rows.append(("macFUSE FSKit", None,
                     "could not read whether it is enabled; if mounts hang, "
                     + FSKIT_HOW))
    elif not st.fskit_registered:
        rows.append(("macFUSE FSKit", False,
                     f"not registered; open {APP} once, then {FSKIT_HOW}"))
    else:
        rows.append(("macFUSE FSKit", False, "not enabled; " + FSKIT_HOW))
    return rows
