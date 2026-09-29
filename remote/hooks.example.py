"""Example project hooks for the NERSC companion, `nersc`.

Copy this next to your project, adapt it, and name it in the companion's
config on the hub (~/.config/nersc/config):

    hooks = ~/myproject/tools/nersc_hooks.py

Every hook is optional.  The file is imported by the hub's system python
(3.6 or newer), so keep it stdlib-only; anything that needs your project's
environment should shell out to it.  Each hook receives ``tool``, a live view
of the running companion: ``tool.CFG`` (the config), ``tool.die(message,
*hints)``, ``tool.mirror_src()``, ``tool.expand_remote(path)``, ...
"""

import os

#: Patterns left out when a staged input is a whole directory.
SUBMIT_INPUT_EXCLUDES = ("*.log", "__pycache__/")


def submit_argv(args, tool):
    """`nersc submit jobs/train.py ARGS` -> run it with the mirror's venv.

    Return the remote argv, or None to run ARGS exactly as given.
    """
    if not args or not args[0].endswith(".py"):
        return None
    if not os.path.isfile(os.path.join(tool.mirror_src(), args[0])):
        tool.die("no such submitter under the mirror: %s" % args[0])
    return [".venv/bin/python", args[0]] + list(args[1:])


def submit_inputs(argv, tool):
    """Local files the job reads that are not part of the code mirror.

    Paths under return_root are staged to the same place under $PSCRATCH;
    paths under mirror_src ride along with the mirror.
    """
    return [a.split("=", 1)[1] for a in argv if a.startswith("--input=")]


def return_excludes(tool):
    """Never carry these back when a job's run directory returns."""
    return ["*.tmp", "core.*"]
