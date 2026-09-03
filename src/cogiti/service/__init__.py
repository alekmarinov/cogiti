"""The service SDK. `docs/services.md` §3.

**This is not a convenience, it is what makes the review gate real.**

An agent writing a service from nothing writes three hundred lines: a socket, a
reconnect loop, a backoff, a JSON encoder, a poller, error handling. Nobody
reviews three hundred lines, so the gate becomes theatre. With the connection
discipline owned here, the same service is twelve lines that a person who does
not program can read aloud and understand.

Every capability this does not expose is a capability a generated service
cannot casually acquire. That is the security model, and it is why the surface
is this small: `show`, `get_json`, `every`, `run`.

The connection rules are `avatari/tools/avatari_feed.py`'s, which were each a
bug the weather feed shipped first:

  * reconnect with backoff — a feed that dies with the first renderer restart
    is not pinning anything;
  * **re-declare on every connect** — the renderer forgot everything the last
    connection made, so "send only when the value changed" pins nothing across
    a restart, because the value has not changed;
  * notice promptly — a keepalive finds a dead renderer in seconds rather than
    whenever the value next happens to move.
"""

import asyncio
import json
import os
import socket
import sys
import time

V = 1

RECONNECT_START_S = 0.5
RECONNECT_CEILING_S = 30.0

#: §8. A service is a duty, not an application.
MAX_PINNED = 4


def _socket_candidates():
    env = os.environ.get("AVATARI_SOCKET")
    if env:
        return [env]
    paths = ["/run/avatari.sock"]
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        paths.append("%s/avatari.sock" % rt)
    return paths


class Service:
    """One standing duty.

        svc = Service("eth-price")

        @every(60)
        async def tick():
            ...
            svc.show(kind="text", text="...")

        svc.run()
    """

    def __init__(self, name=None):
        # The supervisor puts it in the environment, so the name in the code
        # and the name in the manifest cannot drift apart.
        self.name = name or os.environ.get("COGITI_SERVICE")
        if not self.name:
            raise RuntimeError(
                "a service needs a name: pass one, or let the supervisor set "
                "COGITI_SERVICE")
        self.broker = os.environ.get("COGITI_BROKER")
        self._sock = None
        self._shown = {}         # id -> the last op, for re-declaring
        self._tasks = []
        self._stopping = False

    # ------------------------------------------------------- the screen --

    def log(self, msg):
        print("%s: %s" % (self.name, msg), file=sys.stderr, flush=True)

    def show(self, id="main", **op):
        """Pin something. Last write wins.

        Remembered as well as sent, because the renderer is a display and
        never a database: on the next connect this is sent again, unasked,
        which is the rule the weather feed got wrong.
        """
        if id not in self._shown and len(self._shown) >= MAX_PINNED:
            raise RuntimeError(
                "a service may pin %d objects (services.md §8); this is the "
                "%dth" % (MAX_PINNED, len(self._shown) + 1))
        full = dict(op)
        full.update({"op": "create", "id": "%s/%s" % (self.name, id),
                     "region": "periphery", "lifetime": "pinned"})
        full.setdefault("fallback", str(op.get("text", ""))[:120])
        self._shown[id] = full
        self._send(full)

    def _send(self, op):
        if self._sock is None:
            return False
        try:
            self._sock.sendall(
                (json.dumps(op, separators=(",", ":")) + "\n").encode())
            return True
        except (BrokenPipeError, ConnectionError, OSError):
            self._sock = None
            return False

    def _connect(self):
        for path in _socket_candidates():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                self._sock = s
                # Re-declare everything, always. Not an optimisation to skip:
                # after a renderer restart the values have not changed, so a
                # feed that only sends on change shows nothing at all.
                for op in self._shown.values():
                    self._send(op)
                self.log("connected to %s" % path)
                return True
            except OSError:
                continue
        return False

    async def _keep_connected(self):
        delay = RECONNECT_START_S
        while not self._stopping:
            if self._sock is None:
                if self._connect():
                    delay = RECONNECT_START_S
                else:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_CEILING_S)
                    continue
            await asyncio.sleep(1.0)

    # ------------------------------------------------------ the network --

    async def get_json(self, url, params=None):
        """Fetch, through cogiti, and only if the manifest allowed the host.

        There is no socket here on purpose. A service that could open its own
        would make `[network] allow` a comment — and in 5b that manifest is
        what a person approves by ear.
        """
        if params:
            from urllib.parse import urlencode
            url = "%s?%s" % (url, urlencode(params))
        if not self.broker:
            raise RuntimeError(
                "no broker: this service was not started by cogiti, and a "
                "service does not reach the network any other way")

        reader, writer = await asyncio.open_unix_connection(self.broker)
        try:
            writer.write((json.dumps(
                {"v": V, "service": self.name, "op": "fetch", "url": url}
            ) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
        msg = json.loads(line or b"{}")
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error", "the fetch was refused"))
        return json.loads(msg.get("body") or "null")

    # -------------------------------------------------------- the loop --

    def run(self):
        """Start everything and stay until SIGTERM. §3: a clean exit."""
        asyncio.run(self._run())

    async def _run(self):
        loop = asyncio.get_event_loop()
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop)
            except NotImplementedError:
                pass
        self._tasks.append(asyncio.ensure_future(self._keep_connected()))
        for fn, interval in _SCHEDULE:
            self._tasks.append(asyncio.ensure_future(self._ticker(fn, interval)))
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    def _stop(self):
        self._stopping = True
        for t in self._tasks:
            t.cancel()

    async def _ticker(self, fn, interval):
        while not self._stopping:
            started = time.monotonic()
            try:
                out = fn()
                if asyncio.iscoroutine(out):
                    await out
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                # Never silently: §9, a service that has been broken for a day
                # and said nothing is the failure this document is avoiding.
                # Not fatal either — one failed poll is not a broken service,
                # and exiting would spend a restart on a network blip.
                self.log("tick failed: %s" % e)
            wait = interval - (time.monotonic() - started)
            if wait > 0:
                await asyncio.sleep(wait)


_SCHEDULE = []


def every(seconds):
    """@every(60) — run this, now and every `seconds`."""
    def wrap(fn):
        _SCHEDULE.append((fn, seconds))
        return fn
    return wrap
