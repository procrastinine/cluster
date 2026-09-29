"""Node classes.

Clusters do not have one kind of machine. NERSC alone has Perlmutter login nodes
(compute-adjacent, cgroup-capped, *not routable from outside*), data transfer
nodes (routable, built for I/O, no batch access), plus ThinLinc and HPSS hosts.
FASRC's login nodes are all one class and all routable.

Rather than sprinkle ``if node.startswith("dtn")`` through the codebase, each
backend declares its classes and what they are good for, and the rest of the tool
asks questions like "which nodes can host a mount" or "does reaching this node
need a jump".
"""

from __future__ import annotations

import collections

#: what a class of node may be used for
LOGIN = "login"        # interactive shells and tmux sessions
TRANSFER = "transfer"  # bulk data movement
MOUNT = "mount"        # can serve an sshfs mount
BATCH = "batch"        # can submit to the scheduler


class NodeClass(collections.namedtuple(
        "NodeClass", "name routable purposes hosts template count note",
        defaults=(frozenset(), (), "", 0, ""))):
    """One class of node.

    - *routable*: reachable directly from the outside world. When false,
      connections are tunnelled through the backend's pool address.
    - *purposes*: what the class may be used for (LOGIN, TRANSFER, ...)
    - *hosts*: fully qualified names, or else a *template* with ``{n:02d}``
      and a *count*
    - *note*: human note surfaced by `cluster nodes`
    """

    __slots__ = ()

    def members(self):
        if self.hosts:
            return list(self.hosts)
        if self.template and self.count:
            return [self.template.format(n=index) for index in range(1, self.count + 1)]
        return []

    def matches(self, node):
        if not node:
            return False
        short = node.split(".", 1)[0]
        return any(short == m.split(".", 1)[0] for m in self.members())

    def serves(self, purpose):
        return purpose in self.purposes


class NodeMap:
    """The node classes of one backend, and lookups over them."""

    def __init__(self, classes):
        self.classes = list(classes)

    def classify(self, node):
        for node_class in self.classes:
            if node_class.matches(node):
                return node_class
        return None

    def for_purpose(self, purpose):
        result = []
        for node_class in self.classes:
            if node_class.serves(purpose):
                result.extend(node_class.members())
        return result

    def routable(self, node):
        """Whether *node* can be dialled directly. Unknown names: assume yes."""
        found = self.classify(node)
        return True if found is None else found.routable
