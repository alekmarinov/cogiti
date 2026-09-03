"""A service, as a file. `docs/services.md` §2.

**The manifest is the truth.** cogiti holds nothing about a service that is not
derivable from its directory — which is the same discipline the presentation
port demands of an adapter (a display, never a database) one layer up, and it
buys the same things: a service can be inspected, diffed, backed up, and
deleted with `rm`, and a cogiti that has just restarted knows what should be
running without having remembered anything.

The moment there is a second place, `rm -rf` stops working and restarting stops
being safe.

Everything here is validation, and it is strict on purpose. In 5b an agent
writes these files and a person approves them by ear; a field that is silently
ignored is a field the review gate cannot show, and a field that is silently
accepted in the wrong form is a service that behaves unlike its description.
"""

import os
import re
import tomllib

#: `docs/services.md` §8. Not tuning parameters — the shape of the product. A
#: device that will grow forty services is a device nobody understands.
MAX_SERVICES = 32
MAX_PINNED_PER_SERVICE = 4
MAX_SOURCE_BYTES = 32 * 1024

#: The only region a service may own. §9: never the conversational one. The
#: rule is enforced here rather than trusted, because the presentation port
#: cannot tell a service's connection from anyone else's.
REGIONS = ("periphery",)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

REQUIRED = ("name", "title", "exec")


class ManifestError(Exception):
    """Always names the file and the field. A service that will not start is
    a thing somebody has to fix, and 'invalid manifest' is not a repair."""


def _fail(path, msg):
    raise ManifestError("%s: %s" % (path, msg))


class Manifest:
    __slots__ = ("path", "dir", "name", "title", "namespace", "region", "exec",
                 "interval_s", "created_by", "created_at", "source_sha",
                 "limits", "allow", "secrets", "phrases")

    def __init__(self, path, raw):
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))

        for key in REQUIRED:
            if not raw.get(key):
                _fail(path, "no %s" % key)

        self.name = raw["name"]
        if not NAME_RE.match(self.name):
            _fail(path, "name %r must be lowercase letters, digits and "
                        "hyphens, starting with a letter" % self.name)

        self.title = raw["title"]

        # §2: "namespace; must equal name". Two names for one thing is two
        # things that can disagree, and the one that disagrees is the one on
        # screen.
        self.namespace = raw.get("namespace", self.name)
        if self.namespace != self.name:
            _fail(path, "namespace %r must equal name %r"
                        % (self.namespace, self.name))

        self.region = raw.get("region", "periphery")
        if self.region not in REGIONS:
            _fail(path, "region %r is not permitted; a service is pinned and "
                        "never conversational (services.md §9)" % self.region)

        self.exec = raw["exec"]
        if not isinstance(self.exec, list) or not all(
                isinstance(a, str) for a in self.exec):
            # A list, never a string. The same rule the command table has, for
            # the same reason: a string is a thing a shell would parse.
            _fail(path, "exec must be a list of strings, so nothing reaches a "
                        "shell")

        self.interval_s = raw.get("interval_s")
        self.created_by = raw.get("created_by")
        self.created_at = raw.get("created_at")
        self.source_sha = raw.get("source_sha")

        limits = raw.get("limits") or {}
        unknown = set(limits) - {"cpu_seconds", "memory_mb", "open_files",
                                 "processes"}
        if unknown:
            _fail(path, "unknown limit(s): %s" % ", ".join(sorted(unknown)))
        self.limits = {
            "cpu_seconds": int(limits.get("cpu_seconds", 30)),
            "memory_mb":   int(limits.get("memory_mb", 128)),
            "open_files":  int(limits.get("open_files", 64)),
            "processes":   int(limits.get("processes", 4)),
        }
        for k, v in self.limits.items():
            if v <= 0:
                _fail(path, "limit %s must be positive, not %r" % (k, v))

        net = raw.get("network") or {}
        self.allow = list(net.get("allow") or [])
        if not all(isinstance(h, str) and h for h in self.allow):
            _fail(path, "network.allow must be a list of hostnames")

        sec = raw.get("secrets") or {}
        self.secrets = list(sec.get("require") or [])

        # Validated and unused in 5a. Parsed now so the format is settled
        # before 5b writes one and 5c reads it — a field invented twice is a
        # field that means two things.
        ph = raw.get("phrases") or {}
        self.phrases = list(ph.get("patterns") or [])
        if not all(isinstance(p, str) and p.strip() for p in self.phrases):
            _fail(path, "phrases.patterns must be non-empty strings")

    @property
    def argv(self):
        return list(self.exec)

    def __repr__(self):
        return "<service %s>" % self.name


def load(path):
    """One manifest. Raises ManifestError, never returns a broken one."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ManifestError("%s: cannot read it: %s" % (path, e))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError("%s: not valid TOML: %s" % (path, e))
    return Manifest(path, raw)


def load_all(root):
    """Every installed service, by name.

    A directory without a manifest is not a service and is not an error: it is
    how a half-finished install looks, and 5b's staging directory lives beside
    these. A manifest that is *present and broken* is an error, because
    something meant it to run.
    """
    out, broken = {}, []
    if not os.path.isdir(root):
        return out, broken
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, "service.toml")
        if not os.path.exists(path):
            continue
        try:
            m = load(path)
        except ManifestError as e:
            broken.append(str(e))
            continue
        if m.name != entry:
            broken.append("%s: name %r does not match its directory %r"
                          % (path, m.name, entry))
            continue
        out[m.name] = m
    return out, broken
