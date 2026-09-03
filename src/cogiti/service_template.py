"""The service cogiti writes, from a form the model fills in.

`docs/services.md` §4 step 2 says an agent "writes main.py against the SDK and
a template". This is the template, and the decision taken with it is that the
model fills the blanks and **never writes Python at all**.

That is a deliberate trade and the losing side is real: this can build a thing
that fetches JSON and shows a value from it, and nothing else. A service that
needed to do arithmetic across two endpoints, or watch a file, is not
expressible here and would need the other shape.

What it buys is that there is no generated code to review. The static checks
still run — the output is Python and cheap to check, and a template with a
mistake in it should be caught by the same net as anything else — but the
review gate is left with what it should have been about all along: a URL, an
interval, a field, and some phrases.

The one place a model's text reaches the running program is inside a string
literal, and `repr()` is what puts it there. Nothing it supplies becomes code.
"""

import json
import re

#: Where a value lives in the fetched document, as a list of keys and indices:
#: ["current_weather", "temperature"], or ["prices", 0, "amount"].
PATH_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")

#: Readings that come from the device rather than the network, and the
#: strftime that produces each. A fixed set, chosen here: the model names one,
#: it does not describe how to compute it. That is the same discipline as the
#: url shape — the model says *what*, never *how*.
#:
#: These exist because a clock is not expressible as "fetch a url", and a
#: device that can only pin things it downloaded is a device that shows an
#: empty screen when the network is out. Nothing here needs a network, a key,
#: or a permission.
LOCAL = {
    "time": "%H:%M",
    "date": "%A %-d %B",
    "day": "%A",
    "datetime": "%a %-d %b %H:%M",
}

LOCAL_TEMPLATE = '''"""%(title)s.

Written by InteliBoy from a template. It reads the device's own clock and
reaches no network at all.
"""

import time

from cogiti.service import Service, every

svc = Service()

WHEN = %(strftime)r
FORMAT = %(format)r


@every(%(interval)d)
def tick():
    svc.show(kind="text", style="headline",
             text=FORMAT.format(value=time.strftime(WHEN)))


svc.run()
'''

TEMPLATE = '''"""%(title)s.

Written by InteliBoy from a template. The values below are the only thing
that was chosen for this service; the code is the same for every one of them.
"""

from cogiti.service import Service, every

svc = Service()

URL = %(url)r
PATH = %(path)r
FORMAT = %(format)r


def dig(doc, path):
    """Walk to the value. A missing key is a missing reading, not a crash:
    the panel keeps its last value, which beats going blank."""
    for key in path:
        if isinstance(doc, list):
            doc = doc[int(key)]
        else:
            doc = doc[key]
    return doc


@every(%(interval)d)
async def tick():
    doc = await svc.get_json(URL)
    svc.show(kind="text", style="headline", text=FORMAT.format(value=dig(doc, PATH)))


svc.run()
'''


class BadSpec(Exception):
    """What was wrong with the form, in the words the model will be told.

    One reason at a time: the model is going to fix it and try again, and a
    list of five complaints is a rewrite that addresses none of them properly.
    """


def validate(spec):
    """Check the form. Returns a normalised copy; raises BadSpec.

    Strict about types because the model is filling this in from a sentence
    somebody said, and "every minute" arriving as the string "60" rather than
    the number 60 is the sort of thing that would otherwise reach the template
    and produce code that does not run.
    """
    out = {}

    # Exactly one source, and which one decides the whole shape of the
    # service: a url means a fetch and an allow-list, a local name means the
    # device's own clock and no network at all.
    source = spec.get("source") or spec.get("url")
    if not isinstance(source, str) or not source.strip():
        raise BadSpec("source must be an https url, or one of: %s"
                      % ", ".join(sorted(LOCAL)))
    source = source.strip()
    out["local"] = None if source.startswith("https://") else source
    if out["local"] is not None and out["local"] not in LOCAL:
        raise BadSpec("%r is not something the device can read. Use an https "
                      "url, or one of: %s"
                      % (source, ", ".join(sorted(LOCAL))))
    out["url"] = source if out["local"] is None else ""

    for key in ("name", "title", "format"):
        v = spec.get(key)
        if not isinstance(v, str) or not v.strip():
            raise BadSpec("%s must be a non-empty string" % key)
        out[key] = v.strip()

    if not re.match(r"^[a-z][a-z0-9-]{0,31}$", out["name"]):
        raise BadSpec("name must be lowercase letters, digits and hyphens, "
                      "starting with a letter, like 'eth-price'")

    if "{value}" not in out["format"]:
        raise BadSpec("format must contain {value}, which is where the "
                      "reading goes — for example 'ETH ${value}'")

    interval = spec.get("interval_s")
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise BadSpec("interval_s must be a whole number of seconds")
    if not 10 <= interval <= 86400:
        raise BadSpec("interval_s must be between 10 seconds and a day; %d is "
                      "outside that" % interval)
    out["interval_s"] = interval

    if out["local"] is not None:
        # Nothing to walk: the reading is the value.
        out["path"] = []
        return _phrases_of(spec, out)

    path = spec.get("path")
    if not isinstance(path, list) or not path:
        raise BadSpec("path must be a non-empty list saying where the value "
                      "is, like ['current_weather', 'temperature']")
    for p in path:
        if isinstance(p, bool) or not isinstance(p, (str, int)):
            raise BadSpec("every element of path must be a key or an index")
        if isinstance(p, str) and not PATH_RE.match(p):
            raise BadSpec("path element %r is not a plain key" % p)
    out["path"] = list(path)
    return _phrases_of(spec, out)


def _phrases_of(spec, out):
    phrases = spec.get("phrases") or []
    if not isinstance(phrases, list) or not all(
            isinstance(p, str) and p.strip() for p in phrases):
        raise BadSpec("phrases must be a list of sentences")
    if len(phrases) > 6:
        # They are read aloud at the gate. Six is already a long sentence to
        # listen to, and a service claiming twenty phrases is claiming the
        # device's vocabulary.
        raise BadSpec("at most 6 phrases; they are read aloud for approval")
    out["phrases"] = [p.strip() for p in phrases]
    return out


def render(spec):
    """The two files, as text. `spec` must have been through `validate`."""
    from urllib.parse import urlparse
    host = ""
    if spec["local"] is not None:
        code = LOCAL_TEMPLATE % {
            "title": spec["title"],
            "strftime": LOCAL[spec["local"]],
            "format": spec["format"],
            "interval": spec["interval_s"],
        }
    else:
        host = (urlparse(spec["url"]).hostname or "").lower()
        code = TEMPLATE % {
            "title": spec["title"],
            "url": spec["url"],
            "path": spec["path"],
            "format": spec["format"],
            "interval": spec["interval_s"],
        }

    manifest = [
        "# Written by InteliBoy from a template, and approved out loud.",
        "name        = %s" % json.dumps(spec["name"]),
        "title       = %s" % json.dumps(spec["title"]),
        "namespace   = %s" % json.dumps(spec["name"]),
        'region      = "periphery"',
        'exec        = ["python3", "main.py"]',
        "interval_s  = %d" % spec["interval_s"],
        "",
        "[limits]",
        "cpu_seconds  = 10",
        "memory_mb    = 96",
        "open_files   = 32",
        "processes    = 2",
        "",
        "[network]",
        # Empty for a local reading, and that is the whole of its network
        # policy: the broker permits nothing to a service that declared
        # nothing, so a clock cannot reach anything even if it tried.
        "allow = [%s]" % (json.dumps(host) if host else ""),
        "",
        "[secrets]",
        "require = []",
        "",
        "[phrases]",
        "patterns = [%s]" % ", ".join(json.dumps(p) for p in spec["phrases"]),
        "",
    ]
    return code, "\n".join(manifest)
