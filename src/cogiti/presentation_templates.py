"""config/presentation/*.toml — a result becomes scene ops, as data.

`docs/command-table.md`. A template names objects, kinds and relationships. It
**never names a coordinate**: layout belongs to the adapter, which is the only
party that knows the screen.

The point is that a new card is not a new build. avatari's own design note says
a weather panel is a group of a picture and two lines of text, and that
"weather support" is not a renderer feature. It should not be a cogiti feature
either — so this file has no idea what weather is.

Three rules, all enforced at load rather than at first use, because a card that
is wrong is wrong on the day it is written and should say so then:

- **Every id is namespaced.** cogiti declares its namespace on connect and the
  adapter enforces ownership; a template naming an id outside it would be
  creating objects it cannot then update or destroy.
- **The pinned region is not available here.** A command's output is
  conversational. Pinning is a service's business, and a command that pins
  something leaves it on screen with nobody left to remove it.
- **A missing value renders as itself**, so a template referring to something
  the provider never returned reads as the bug it is rather than losing the
  answer that was already computed.
"""

import glob
import os

try:
    import tomllib
except ImportError:                                           # pragma: no cover
    tomllib = None

from .table import render

NAMESPACE = "brain"
STAGE = "stage"
KINDS = ("text", "image", "group", "stream")
STYLES = ("headline", "title", "body", "caption")


class TemplateError(Exception):
    pass


class Template:
    def __init__(self, name, spec):
        self.name = name
        self.id = spec.get("id") or "%s/%s" % (NAMESPACE, name)
        if not self.id.startswith(NAMESPACE + "/"):
            raise TemplateError(
                "%s: id %r is outside cogiti's namespace %r — the adapter "
                "enforces ownership, so it could be created and never updated"
                % (name, self.id, NAMESPACE))

        self.kind = spec.get("kind", "text")
        if self.kind not in KINDS:
            raise TemplateError("%s: unknown kind %r" % (name, self.kind))

        region = spec.get("region", STAGE)
        if region != STAGE:
            raise TemplateError(
                "%s: region %r. A command's output is conversational; pinning "
                "belongs to a service, which is there to remove it again"
                % (name, region))
        self.region = region

        self.style = spec.get("style")
        self.text = spec.get("text")
        self.src = spec.get("src")
        self.attention = spec.get("attention", "once")
        self.children = [_Child(name, i, c)
                         for i, c in enumerate(spec.get("children", []))]
        if self.kind == "group" and not self.children:
            raise TemplateError("%s: a group with no children" % name)
        if self.kind == "text" and self.text is None:
            raise TemplateError("%s: a text object with no text" % name)

    def ops(self, values):
        """One `create`. Children go inline — the adapter derives their ids."""
        op = {"op": "create", "id": self.id, "kind": self.kind,
              "region": self.region, "lifetime": "turn",
              "attention": self.attention}
        if self.style:
            op["style"] = self.style
        if self.text is not None:
            op["text"] = render(self.text, values)
        if self.src:
            op["src"] = render(self.src, values)
        if self.children:
            op["children"] = [c.render(values) for c in self.children]
        # What a renderer that cannot draw this kind shows instead. Required
        # rather than polite: the port says an unknown kind still holds its
        # place, which it can only do if it was given something to draw.
        op.setdefault("fallback", _fallback(op))
        return op


class _Child:
    def __init__(self, parent, index, spec):
        self.kind = spec.get("kind", "text")
        if self.kind not in KINDS:
            raise TemplateError("%s child %d: unknown kind %r"
                                % (parent, index, self.kind))
        self.style = spec.get("style")
        self.text = spec.get("text")
        self.src = spec.get("src")
        if self.kind == "text" and self.text is None:
            raise TemplateError("%s child %d: text with no text"
                                % (parent, index))

    def render(self, values):
        out = {"kind": self.kind}
        if self.style:
            out["style"] = self.style
        if self.text is not None:
            out["text"] = render(self.text, values)
        if self.src:
            out["src"] = render(self.src, values)
        return out


def _fallback(op):
    if op.get("text"):
        return op["text"]
    for child in op.get("children") or []:
        if child.get("text"):
            return child["text"]
    return op["id"]


def load_dir(path):
    """Every *.toml in a directory, named by its filename."""
    if tomllib is None:                                       # pragma: no cover
        raise TemplateError("no tomllib; cogiti needs python 3.11 or newer")
    if not path:
        return {}
    if not os.path.isdir(path):
        raise TemplateError("presentation directory does not exist: %s" % path)

    out = {}
    for f in sorted(glob.glob(os.path.join(path, "*.toml"))):
        name = os.path.splitext(os.path.basename(f))[0]
        try:
            with open(f, "rb") as fh:
                spec = tomllib.load(fh)
        except Exception as e:                                # noqa: BLE001
            raise TemplateError("%s is not valid toml: %s" % (f, e))
        out[name] = Template(name, spec)
    return out
