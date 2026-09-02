"""Local effects. One file each, and none may take longer than its timeout.

`docs/architecture.md` §2 and `docs/command-table.md`. A provider takes
resolved arguments and returns a `Result`. It may not touch the screen, may not
prompt, and may not outlive the turn.

**If it needs to ask a question, run for a minute, hold a lock, or keep running
after the answer, it is not a provider** — it is a job or a service. Forcing
one into the table is how a device ends up with a two-second pause in the
middle of a conversation.

Failures carry a **category, not a string**: `offline`, `refused`,
`not_found`, `timeout`, `unavailable`. The categories map to spoken apologies
in one place, so the device fails the same way every time — which is most of
what makes an appliance feel like one thing rather than a pile of features.
"""

import importlib
import pkgutil

CATEGORIES = ("offline", "refused", "not_found", "timeout", "unavailable")


class Result:
    __slots__ = ("ok", "values", "provenance", "ttl_s", "source", "reason")

    def __init__(self, ok=True, values=None, provenance=None, ttl_s=None,
                 source=None, reason=None):
        if not ok and reason not in CATEGORIES:
            raise ValueError(
                "a failed result needs a category, not %r. One of: %s"
                % (reason, ", ".join(CATEGORIES)))
        self.ok = ok
        self.values = values or {}
        # Which values were defaulted rather than said. Carried from the
        # decision and never dropped: it is the one thing the resolver port
        # went out of its way to preserve.
        self.provenance = provenance or {}
        # How long this stays true, and where it came from. Both exist for
        # things that arrive later and are miserable to retrofit — memory
        # needs to know what is worth keeping, and the audit log needs to
        # answer "why did it say that" months afterwards.
        self.ttl_s = ttl_s
        self.source = source
        self.reason = reason

    @classmethod
    def failed(cls, reason, **kw):
        return cls(ok=False, reason=reason, **kw)

    def __repr__(self):
        return ("<Result ok %r>" % self.values if self.ok
                else "<Result failed %s>" % self.reason)


_REGISTRY = {}


def provider(name):
    """@provider("time.now"). The name is what the command table refers to."""
    def wrap(fn):
        if name in _REGISTRY:
            raise ValueError("provider %r is registered twice" % name)
        _REGISTRY[name] = fn
        return fn
    return wrap


def load_all():
    """Import every module in this package so its decorators run."""
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module("%s.%s" % (__name__, mod.name))
    return dict(_REGISTRY)


def get(name):
    return _REGISTRY.get(name)


def names():
    return sorted(_REGISTRY)
