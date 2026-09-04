"""Putting pictures on the screen, offered to the model as a tool.

An escalation could say a thing and show one line of text. Asked to compare
two products it produced a paragraph, out loud, listing figures — which is
the one job a screen is better at than a voice, on a device that has a
screen.

The model does not draw anything and is not given a path. It names what it
wants shown and where each picture is on the web; cogiti fetches, checks and
draws. That is the same rule as everywhere else here — an agent proposes and
cogiti decides — and it is what keeps `images.py`'s limits in force rather
than optional.

**A failed picture is not a failed panel.** The words were the answer and the
photograph was the illustration, so a panel whose image will not load still
draws, and the model is told which ones did not so it can say so rather than
describe something nobody can see.
"""

from . import images

MAX_PANELS = 4          # a stage, not a catalogue
MAX_LINES = 400         # what fits under a picture before it stops being read


def tool():
    return {
        "name": "display",
        "description":
            "Put one or more things on the screen, each as a picture with a "
            "title and a few lines under it. Use it whenever the answer is "
            "something to look at rather than only to hear — a product, a "
            "comparison, anything with a photograph or a set of figures. "
            "Say the short version out loud and let the screen carry the "
            "detail; do not read the specifications aloud one by one. "
"For a picture, prefer `image_page`: give the https address of a page "
            "about the thing — a review, a product listing, an encyclopedia "
            "entry — and the device reads that page's own photograph off it. "
            "You cannot see images in a fetched page, because fetching gives "
            "you text, so naming the page is how you get one without "
            "guessing at a URL. Use `image_url` only when you have the "
            "address of the image itself. A panel still appears if its "
            "picture cannot be fetched, so never invent either one.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["panels"],
            "properties": {
                "panels": {
                    "type": "array",
                    "description": "up to %d, shown side by side" % MAX_PANELS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title"],
                        "properties": {
                            "title": {"type": "string"},
                            "image_url": {
                                "type": "string",
                                "description": "https url of the image file "
                                               "itself, if you have one",
                            },
                            "image_page": {
                                "type": "string",
                                "description": "https url of a page about it; "
                                               "the device takes the picture "
                                               "from the page",
                            },
                            "lines": {
                                "type": "string",
                                "description": "a few short lines, one per "
                                               "fact, separated by newlines",
                            },
                        },
                    },
                },
            },
        },
    }


def picture_tool():
    return {
        "name": "find_pictures",
        "description":
            "List the pictures on a web page, so you can choose one. Give an "
            "https page address — a review, a product listing, an "
            "encyclopedia entry — and you get back each picture's URL and "
            "whatever the page says it shows. You cannot see images in a "
            "page you fetch, because fetching gives you text; this is how "
            "you find out what is on it. Judge by the description and by the "
            "file name, which usually names the product even when the "
            "description is empty, then pass the one you want to `display` "
            "as `image_url`.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["page"],
            "properties": {"page": {"type": "string",
                                    "description": "https url of the page"}},
        },
    }


async def find(cogiti, args):
    """What is on that page, for the model to choose between.

    The split that makes this work: cogiti can read markup and the model
    cannot; the model knows what was asked and cogiti does not. `og:image`
    was cogiti guessing at relevance on its own, and on a roundup page it
    answers "what represents this page" — the banner — which is why the
    pictures kept arriving irrelevant.
    """
    page = ((args or {}).get("page") or "").strip()
    if not page:
        return {"ok": False, "problem": "no page"}
    try:
        found = images.candidates(page)
    except images.Refused as e:
        return {"ok": False, "problem": str(e)}
    return {"ok": True, "pictures": found}


async def run(cogiti, args):
    """Fetch what it named and draw it. Says which pictures did not arrive."""
    wanted = (args or {}).get("panels") or []
    if not isinstance(wanted, list) or not wanted:
        return {"ok": False, "problem": "no panels"}

    into = cogiti.config["state_dir"]
    import os
    into = os.path.join(os.path.expanduser(into), "panels")

    items, missing = [], []
    for spec in wanted[:MAX_PANELS]:
        if not isinstance(spec, dict):
            continue
        item = {"title": (spec.get("title") or "").strip(),
                "lines": (spec.get("lines") or "")[:MAX_LINES]}
        url = (spec.get("image_url") or "").strip()
        page = (spec.get("image_page") or "").strip()
        if url or page:
            try:
                item["image"] = (images.fetch(url, into) if url
                                 else images.from_page(page, into))
            except images.Refused as e:
                # Named, not swallowed. The model is about to describe this
                # panel out loud and should not describe a picture that is
                # not there.
                missing.append("%s (%s)" % (item["title"] or url, e))
        items.append(item)

    presenter = getattr(cogiti.output, "p", None)
    if presenter is None or not hasattr(presenter, "panels"):
        return {"ok": False,
                "problem": "this device has no screen to put them on"}
    drawn = presenter.panels(items)
    if not drawn:
        return {"ok": False, "problem": "nothing in those panels to draw"}
    out = {"ok": True, "shown": len(drawn)}
    if missing:
        out["no_picture_for"] = missing
    return out
