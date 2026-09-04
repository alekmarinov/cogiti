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
import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import trust

MAX_BYTES = 4 << 20        # a screen shows one picture; this is generous
MAX_PAGE_BYTES = 2 << 20   # enough of a page to reach its <head>
MAX_SCAN_BYTES = 6 << 20   # and enough of it to reach the photographs

#: Smaller than this on either side and it is a logo, not a photograph. Real
#: pages say so: asked for four USB sticks, two of the `og:image` tags led to
#: a 30x30 and a 100x90 — a site icon, offered in the same field and with the
#: same confidence as a 2048x1536 product shot. Drawn on a screen it is a
#: smudge, and the panel is better off with no picture and honest about it.
MIN_SIDE = 160
TIMEOUT_S = 15
MAX_HOPS = 3
KEEP_S = 3600              # how long a fetched picture stays on disk
KEEP_N = 20                # and how many, whatever their age

#: The first bytes of the formats stb_image will actually decode. A server
#: may say anything in a header; this is the file agreeing with it.
MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
         (b"GIF87a", "gif"), (b"GIF89a", "gif"), (b"BM", "bmp"))


#: Where a page says what picture represents it. Open Graph first: it exists
#: precisely so that a link to this page shows the right image, which is the
#: same question being asked here, and it is one tag rather than a guess among
#: forty.
META = (re.compile(rb'<meta[^>]+property=["\']og:image["\'][^>]+content='
                   rb'["\']([^"\']+)', re.I),
        re.compile(rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property='
                   rb'["\']og:image', re.I),
        re.compile(rb'<meta[^>]+name=["\']twitter:image["\'][^>]+content='
                   rb'["\']([^"\']+)', re.I))


class Refused(Exception):
    """Not fetched, and why in a sentence somebody can act on."""


#: An <img>, and what the page says it shows. `alt` is the whole point: it is
#: the page's own words for the picture, and the only thing that tells one
#: photograph on a roundup from another.
# 2000, not 600: a modern <img> carries srcset with half a dozen widths
#: and the tag runs long. At 600 the pattern never reached the closing
#: bracket on Wikipedia's article images — forty-eight of them in the
#: page, none of them found, and what came back was the chrome.
IMG = re.compile(rb'<img\s([^>]{0,2000}?)>', re.I)
ATTR = re.compile(rb'(src|data-src|srcset|alt)\s*=\s*["\']([^"\']*)', re.I)

#: Filenames that are furniture. Cheap and not exhaustive — the size check on
#: download catches the rest, and the model can see these names anyway.
FURNITURE = ("logo", "icon", "sprite", "avatar", "badge", "banner-ad",
             "placeholder", "1x1", "pixel", "spacer")

#: What the renderer can actually decode — stb_image, so no SVG and no WebP.
#: Filtering here rather than on download because a page's markup is mostly
#: furniture: the first pass over a review site returned three tracking
#: pixels, five SVG chrome icons and a literal SPONSORED_IMAGE_URL before it
#: reached a photograph.
DRAWABLE = (".jpg", ".jpeg", ".png", ".gif", ".bmp")


def candidates(url, limit=24):
    """Every picture on a page, with the page's own words for each.

    `og:image` answers "what represents this page", which on a roundup is the
    banner and not the product — the reason the pictures were arriving
    irrelevant. This answers a different question: what is here, and what
    does the page call it. Choosing between them needs judgement about what
    was asked, which is the model's job and not this module's.

    So the split is: cogiti can read markup and the model cannot, the model
    knows what was asked and cogiti does not, and neither has to pretend
    otherwise.
    """
    # More than `from_page` reads: og:image is in the <head> and the
    # photographs are wherever the article put them, which on a review site
    # is after several hundred kilobytes of navigation.
    body, final = _read(url, MAX_SCAN_BYTES, wanted="text/html", whole=False)
    out, seen = [], set()
    for pattern in META:
        m = pattern.search(body)
        if m:
            src = urllib.parse.urljoin(
                final,
                html.unescape(m.group(1).decode("utf-8", "replace").strip()))
            out.append({"url": src, "alt": "(the page's own header image)"})
            seen.add(src)
            break
    for tag in IMG.finditer(body):
        attrs = {k.decode().lower(): v.decode("utf-8", "replace")
                 for k, v in ATTR.findall(tag.group(1))}
        src = attrs.get("src") or attrs.get("data-src") or ""
        if not src and attrs.get("srcset"):
            src = attrs["srcset"].split(",")[0].strip().split(" ")[0]
        # Unescaped, because an href in markup is HTML: `&amp;` is one
        # ampersand, and leaving it made every URL with a query string —
        # which is most of a CDN's — a 404 waiting to happen.
        src = html.unescape(src.strip())
        if not src or src.startswith("data:"):
            continue
        src = urllib.parse.urljoin(final, src)
        if not src.startswith("https://") or src in seen:
            continue
        low = src.lower()
        path = urllib.parse.urlparse(low).path
        if not path.endswith(DRAWABLE):
            continue
        if any(word in low for word in FURNITURE):
            continue
        seen.add(src)
        out.append({"url": src,
                    "alt": html.unescape(attrs.get("alt") or "").strip()[:120]})
    # Described first. `alt` is the page's own words for the picture and the
    # only thing that separates one photograph on a roundup from another, so
    # an undescribed image is the last thing worth offering.
    out.sort(key=lambda c: not c["alt"])
    if not out:
        raise Refused("no pictures on that page that this screen can draw")
    return out[:limit]


def from_page(url, into):
    """Find the picture a page says represents it, and fetch that.

    The model cannot do this itself and it is not its fault: `web_fetch`
    hands it the page as extracted text, so the markup is gone before it
    sees it. Asked for a product photograph it said so plainly — "since
    fetched content is text-only, inventing URLs isn't an option" — and drew
    four panels with no pictures, which is the correct behaviour and a
    useless answer.

    So it names a page instead of a picture, and this reads the page's own
    answer to "what image is this?" — the same rules as any other fetch,
    applied twice: once to the page and again to the image it points at.
    """
    body, final = _read(url, MAX_PAGE_BYTES, wanted="text/html", whole=False)
    for pattern in META:
        m = pattern.search(body)
        if m:
            src = m.group(1).decode("utf-8", "replace").strip()
            return fetch(urllib.parse.urljoin(final, src), into)
    raise Refused("that page does not say which picture represents it")


def _sniff(head):
    for magic, ext in MAGIC:
        if head.startswith(magic):
            return ext
    return None


def fetch(url, into):
    """Download one picture. Returns a path, or raises Refused."""
    body, _final = _read(url, MAX_BYTES, wanted="image/")
    return _save(body, into)


def _read(url, cap, wanted, whole=True):
    """Fetch, following redirects by hand. Returns (bytes, final url).

    By hand rather than by urllib so that every hop goes through the address
    check. urllib follows them inside `open`, where the only URL this
    function ever saw is the first one — which would make the check
    decorative, since the hop is chosen by the same page.
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
                kind = (r.headers.get("Content-Type") or "").split(";")[0]
                kind = kind.strip().lower()
                if not kind.startswith(wanted):
                    raise Refused("that is %s, not %s"
                                  % (kind or "unlabelled", wanted.rstrip("/")))
                body = r.read(cap + 1)
                if len(body) > cap:
                    if whole:
                        raise Refused("bigger than %d MB" % (cap >> 20))
                    # A page is read for its <head>, which is at the front.
                    # Refusing a long article for being long would rule out
                    # most review sites — the first run of this refused Tom's
                    # Hardware, which is exactly the page it had been given.
                    body = body[:cap]
                return body, seen
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


def _save(body, into):
    ext = _sniff(body)
    w, h = _dimensions(body, ext)
    if w and h and (w < MIN_SIDE or h < MIN_SIDE):
        raise Refused("that picture is %dx%d, which is an icon" % (w, h))
    if ext is None:
        # Said image/png and sent something else. The header is the server's
        # word; this is the file's.
        # The header is the server's word; this is the file's.
        raise Refused("it says image but is not one")
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


def _dimensions(body, ext):
    """Width and height from the header, or (0, 0) if it cannot be read.

    From the bytes rather than a decoder: this runs before the file is kept,
    the answer is in the first few dozen bytes of every format here, and
    pulling in an image library to reject a favicon would be the wrong
    trade on an appliance.
    """
    try:
        if ext == "png" and len(body) >= 24:
            return (int.from_bytes(body[16:20], "big"),
                    int.from_bytes(body[20:24], "big"))
        if ext == "gif" and len(body) >= 10:
            return (int.from_bytes(body[6:8], "little"),
                    int.from_bytes(body[8:10], "little"))
        if ext == "bmp" and len(body) >= 26:
            return (int.from_bytes(body[18:22], "little"),
                    int.from_bytes(body[22:26], "little"))
        if ext == "jpg":
            i = 2
            while i + 9 < len(body):
                if body[i] != 0xFF:
                    i += 1
                    continue
                marker = body[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    return (int.from_bytes(body[i + 7:i + 9], "big"),
                            int.from_bytes(body[i + 5:i + 7], "big"))
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + int.from_bytes(body[i + 2:i + 4], "big")
    except (IndexError, ValueError):
        pass
    return (0, 0)


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
