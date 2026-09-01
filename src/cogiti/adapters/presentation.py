"""cogiti's half of the presentation port.

`docs/ports.md` — structured results become visible. A newline-JSON client over
a Unix socket, which is the transport the first adapter happens to use; nothing
above this file knows that.

Three properties the port demands, and they are the reason this is not just
`socket.send`:

**Reconnect with backoff, and the renderer is allowed to be absent.** The face
is a separate process with its own lifetime. cogiti must start without it,
survive it dying mid-sentence, and pick it up when it comes back — so a
connection failure is a dropped message and a retry later, never an exception
reaching the turn machine. A device that stops answering because its screen
went away is worse than one that answers to nobody.

**Re-declare on connect.** The renderer is a display, never a database:
everything dies with the connection, and pinned objects live in *our*
configuration. So whatever should outlive a reconnect is remembered here and
resent. Nothing pins anything yet — services are the ones that will — but the
mechanism belongs with the client that needs it, not with the first caller who
happens to want it.

**Capabilities are asked for, not assumed.** `hello` is answered with the kinds,
regions and ops this renderer supports. An adapter that cannot do `stream` is a
valid adapter; finding that out at the first thought event is not.
"""

import errno
import json
import socket
import time

V = 1
RECONNECT_S = 2.0          # never faster; a dead socket must not become a spin


class Presentation:
    def __init__(self, path, namespace="brain", on_warn=None):
        self.path = path
        self.namespace = namespace
        self.caps = {}
        self._sock = None
        self._next_try = 0.0
        self._pinned = {}            # id -> the op that recreates it
        self._dropped = 0
        self._warn = on_warn or (lambda m: None)

    # -------------------------------------------------------- connection --

    def _connect(self):
        """One attempt, at most once per RECONNECT_S. Never raises."""
        now = time.monotonic()
        if self._sock is not None or now < self._next_try:
            return self._sock is not None
        self._next_try = now + RECONNECT_S
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(self.path)
        except (OSError, socket.timeout):
            return False

        self._sock = s
        self.caps = self._hello() or {}
        for op in self._pinned.values():        # the renderer kept nothing
            self._write(op)
        return True

    def _hello(self):
        """Declare who is speaking, and read back what this renderer can do."""
        if not self._write({"op": "hello", "namespace": self.namespace}):
            return None
        try:
            line = self._sock.recv(8192).decode("utf-8", "replace").strip()
            for part in line.splitlines():
                msg = json.loads(part)
                if msg.get("event") == "hello":
                    return msg
        except (OSError, socket.timeout, ValueError):
            # An adapter that does not answer hello is still a usable adapter;
            # it just cannot be asked anything, so assume nothing.
            pass
        return None

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    # --------------------------------------------------------------- send --

    def _write(self, op):
        if self._sock is None:
            return False
        op.setdefault("v", V)
        try:
            self._sock.sendall((json.dumps(op, separators=(",", ":")) + "\n").encode())
            return True
        except (OSError, socket.timeout) as e:
            # EPIPE is the renderer having exited. Not an error to propagate:
            # the next send reconnects, and the turn carries on regardless.
            if getattr(e, "errno", None) not in (errno.EPIPE, errno.ECONNRESET):
                self._warn("presentation write failed: %s" % e)
            self._drop()
            return False

    def send(self, **op):
        """Best effort, always. Returns whether it went out."""
        if op.get("op") == "destroy":
            self._pinned.pop(op.get("id"), None)

        ok = self._deliver(op)

        # Recorded *after* delivery, never before. Connecting replays the
        # pinned set, so an op recorded first is sent twice on the connection
        # that carries it — once by the replay and once by its own write.
        # Recorded regardless of `ok`: pinning something while the renderer is
        # away should still put it on screen when the renderer arrives, which
        # is the whole reason this set lives here rather than there.
        if op.get("lifetime") == "pinned" and op.get("id"):
            self._pinned[op["id"]] = dict(op)
        return ok

    def _deliver(self, op):
        if not self._connect():
            self._dropped += 1
            return False
        if self._write(op):
            return True
        # One immediate retry: the common case is a renderer that restarted
        # between two messages, and making the user wait RECONNECT_S to see an
        # answer that is already computed would be a poor trade.
        self._next_try = 0.0
        if self._connect() and self._write(op):
            return True
        self._dropped += 1
        return False

    # -------------------------------------------------------------- query --

    def supports(self, what, name):
        """`supports("kinds", "stream")`. Unknown until hello has been answered,
        and an adapter that never answered is assumed to do the basics."""
        have = self.caps.get(what)
        return name in have if have else what in ("kinds", "regions", "ops")

    @property
    def connected(self):
        return self._sock is not None

    @property
    def dropped(self):
        return self._dropped

    def close(self):
        self._drop()
