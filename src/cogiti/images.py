"""Fetch a picture the model chose, safely enough to put on a screen.

A product photograph is the one thing an escalation cannot produce for
itself: it can find a URL by searching, and the renderer draws a file. This
is the gap between those two facts, and it is the only place in cogiti that
downloads something a *model* named.

So every limit here is about that. The URL was chosen from a page written by
somebody else, which makes it exactly the input least worth trusting:

  **https, and never this network.** `trust.public_only` — an allowlist
  cannot work here, because nobody can enumerate the image CDNs of everything
  a person might ask about. What matters is that the router and the printer
  are unreachable, and that is an address rule, not a host list.

  **Re-checked on every redirect.** An image CDN redirects constantly, so
  refusing outright would refuse most real URLs; following blindly would make
  the first check decorative, since the hop is chosen by the same page.

  **A ceiling on bytes, and the read stops there** rather than trusting
  `Content-Length`, which is a claim by the same server.

  **It must actually be an image**, by content type and then by its first
  bytes — a served `image/png` that is HTML is a page that wanted to be
  fetched, not a picture.

  **The name is ours.** Nothing from the URL reaches the filesystem: a path
  chosen by a model is a path traversal waiting to be written.
"""

import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import trust

MAX_BYTES = 4 << 20        # a screen shows one picture; this is generous
TIMEOUT_S = 15
MAX_HOPS = 3
KEEP_S = 3600              # how long a fetched picture stays on disk
KEEP_N = 20                # and how many, whatever their age

#: The first bytes of the formats stb_image will actually decode. A server
#: may say anything in a header; this is the file agreeing with it.
MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
         (b"GIF87a", "gif"), (b"GIF89a", "gif"), (b"BM", "bmp"))


class Refused(Exception):
    """Not fetched, and why in a sentence somebody can act on."""


def _sniff(head):
    for magic, ext in MAGIC:
        if head.startswith(magic):
            return ext
    return None


def fetch(url, into):
    """Download one picture. Returns a path, or raises Refused.

    Redirects are followed by hand rather than by urllib so that every hop
    goes through the address check. urllib would follow them inside `open`,
    where the only URL this function ever saw is the first one.
    """
    seen = url
    for _hop in range(MAX_HOPS + 1):
        try:
            trust.public_only(seen)
        except trust.EgressDenied as e:
            raise Refused(str(e))
        req = urllib.request.Request(seen, headers={"User-Agent": "cogiti"})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=TIMEOUT_S) as r:
                return _save(r, into)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                nxt = (e.headers or {}).get("Location")
                if not nxt:
                    raise Refused("redirected to nowhere")
                seen = urllib.parse.urljoin(seen, nxt)
                continue
            raise Refused("the server said %d" % e.code)
        except urllib.error.URLError as e:
            raise Refused("could not reach it: %s" % e.reason)
        except OSError as e:                                  # noqa: BLE001
            raise Refused("could not read it: %s" % e)
    raise Refused("too many redirects")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None        # raises HTTPError, which fetch() reads and re-checks


def _save(r, into):
    kind = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not kind.startswith("image/"):
        raise Refused("that is %s, not a picture" % (kind or "unlabelled"))
    body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise Refused("bigger than %d MB" % (MAX_BYTES >> 20))
    ext = _sniff(body)
    if ext is None:
        # Said image/png and sent something else. The header is the server's
        # word; this is the file's.
        raise Refused("it calls itself %s but is not an image" % kind)
    os.makedirs(into, exist_ok=True)
    sweep(into)
    # Named from the content, never from the URL: a path chosen by a model is
    # a path traversal waiting to be written.
    path = os.path.join(into, "%s.%s" % (hashlib.sha256(body).hexdigest()[:16],
                                         ext))
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def sweep(into, keep_s=KEEP_S, keep_n=KEEP_N):
    """Old pictures go. Nothing refers to them once the card is gone, and a
    device that keeps every photograph it was ever shown fills its own disk
    at four megabytes a time.

    **Two rules, because age alone was not enough.** An hour is the right
    life for a picture nobody is looking at any more — but a run of
    questions inside that hour accumulates without limit, four megabytes at
    a time, and the appliance has about four gigabytes free. So a count as
    well, newest kept.

    Called on startup as well as before each fetch. Sweeping only on the way
    in meant a device asked for a product once and never again kept those
    files for as long as it ran, which is every device that is not being
    tested.
    """
    now = time.time()
    try:
        names = os.listdir(into)
    except OSError:
        return
    live = []
    for name in names:
        p = os.path.join(into, name)
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if age > keep_s:
            _remove(p)
        else:
            live.append((age, p))
    for _age, p in sorted(live)[keep_n:]:
        _remove(p)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
