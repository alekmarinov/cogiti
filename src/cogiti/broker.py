"""The egress broker: a service asks, cogiti decides.

`docs/services.md` §1 — "only the hosts it declared, enforced by the egress
broker" — and `docs/security.md`, which says an agent proposes and cogiti
decides. The same holds for a service, and for the same reason: a rule the
subject enforces on itself is not a rule.

**Why a socket and not a library.** A service is a separate process. Anything
it can do to a socket of its own, it can do to any host, so an allow-list
checked inside the service is a comment. Checked here, on the far side of a
unix socket, it is the only path out — and the manifest that names the hosts
is read by cogiti, not by the service.

This is deliberately not a proxy. It does not forward arbitrary traffic: it
answers one question — fetch this URL — and returns a body. A service that
wants a protocol cogiti does not speak does not get one, which is a feature.
The list of what a generated service can casually acquire is exactly the list
of what this file implements.

One connection per request, line JSON in and out:

    -> {"v":1,"service":"eth-price","op":"fetch","url":"https://..."}
    <- {"v":1,"ok":true,"status":200,"body":"..."}
    <- {"v":1,"ok":false,"error":"host not in this service's allow-list"}
"""

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request

from . import trust

V = 1

#: A service is pinning a number on a screen. Anything slower than this is not
#: a feed, and a service that hangs on a socket holds a worker with it.
FETCH_TIMEOUT_S = 15.0

#: Bounded because the reply is read into memory and a service that asks for a
#: gigabyte should fail rather than take the device with it.
MAX_BODY_BYTES = 256 * 1024


class Broker:
    def __init__(self, path, services, on_warn=None):
        self.path = path
        self.services = services          # name -> Manifest, looked up live
        self.on_warn = on_warn or (lambda m: None)
        self.server = None
        #: name -> the last thing that service put on screen. A cache of what
        #: is already visible, so it can also be spoken.
        self.values = {}

    async def start(self):
        # Replace a socket left by a previous run. A stale one is not a
        # running cogiti — that is what the pidfile is for — and refusing to
        # start because of it means a crash needs a person.
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._client, self.path)
        # Services run as their own uid, so they must be able to connect; the
        # authority is in what this answers, never in who may ask.
        os.chmod(self.path, 0o666)
        return self.path

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 10.0)
            if not line:
                return
            try:
                req = json.loads(line)
            except ValueError:
                return await self._say(writer, ok=False, error="not json")

            name = req.get("service")
            m = self.services.get(name)
            if m is None:
                # Named a service that is not installed. Not an accusation —
                # a stale process after a removal looks exactly like this —
                # but not a fetch either.
                return await self._say(writer, ok=False,
                                       error="no such service: %r" % name)

            if req.get("op") == "value":
                # What the service is currently showing, so the device can say
                # it. Stored and not forwarded anywhere: cogiti is not a
                # display, and this is a fact about a service rather than
                # something to draw.
                self.values[name] = str(req.get("text", ""))[:200]
                return await self._say(writer, ok=True)

            if req.get("op") != "fetch":
                return await self._say(writer, ok=False,
                                       error="unknown op %r" % req.get("op"))

            url = req.get("url") or ""
            allowed, why = _permitted(url, m.allow)
            if not allowed:
                self.on_warn("%s asked for %s: %s" % (name, url, why))
                return await self._say(writer, ok=False, error=why)

            status, body = await asyncio.to_thread(_fetch, url)
            await self._say(writer, ok=True, status=status, body=body)
        except asyncio.TimeoutError:
            pass
        except Exception as e:                                # noqa: BLE001
            self.on_warn("broker: %s" % e)
        finally:
            try:
                writer.close()
            except Exception:                                 # noqa: BLE001
                pass

    async def _say(self, writer, **msg):
        msg.setdefault("v", V)
        writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass


def _permitted(url, allow):
    """Whether this service declared this host, and whether the host is safe.

    https only, and the host must be listed exactly. No wildcards: a service
    that declares `*.example.com` has declared a namespace somebody else can
    register in, and the review gate in 5b reads these aloud — "anything under
    example.com" is not a sentence a person can approve meaningfully.

    **The private-address check is `trust.check`'s and not a second copy.**
    This function originally did its own host matching and stopped there,
    which left the standard shape of a server-side request forgery wide open:
    a service declares `metadata.example.com`, that name resolves to
    169.254.169.254 or 127.0.0.1, and an allow-list entry the user approved
    becomes a way into the network the device is sitting on. `trust.py` has
    refused that since slice 1 and this simply did not call it.

    `allow_private` stays False. A service is a standing duty pinned to a
    screen; nothing about that needs the local network, and the grant exists
    for a user asking about their own printer.
    """
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme != "https":
        return False, "only https is permitted, not %r" % (u.scheme or "")
    host = (u.hostname or "").lower()
    if not host:
        return False, "no host in that url"
    # Exact, not trust's pattern match: a manifest is approved by ear and
    # "anything under example.com" is not a sentence a person can weigh.
    if host not in [h.lower() for h in allow]:
        return False, ("%s is not in this service's allow-list (%s)"
                       % (host, ", ".join(allow) or "empty"))
    try:
        trust.check(url, [host], allow_private=False)
    except trust.EgressDenied as e:
        return False, str(e)
    return True, ""


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cogiti-service"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            return r.status, r.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError, socket.timeout) as e:
        return 0, str(e)
