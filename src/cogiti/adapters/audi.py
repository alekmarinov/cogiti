"""cogiti's half of the speech port.

`docs/speech-protocol.md` is the wire. Unlike the agent port, this is **not a
job**: it is a device. It starts with cogiti, holds the audio hardware for the
life of the process, and losing it is losing the ears — so it is the one
adapter cogiti restarts, with backoff, because a device that has gone deaf and
does not mention it is the worst version of this.

The whole file is a router. Events arrive on a thread of the adapter's making
and are handed to callbacks that belong to the session; nothing here decides
what an utterance means, and nothing here knows what a turn is.
"""

import asyncio
import json
import os
import signal

V = 1
RESTART_MIN_S = 0.5
RESTART_MAX_S = 30.0


class SpeechError(Exception):
    pass


class Speech:
    """A long-lived speech adapter: events out, commands in.

    Callbacks, all optional, all called on the event loop:

        on_speech_start()      someone began speaking — barge-in
        on_partial(text, stable)
        on_final(text, ms)
        on_speech_end()
        on_error(kind, message)
    """

    def __init__(self, argv, on_warn=None, **callbacks):
        self.argv = list(argv)
        self.caps = {}
        self.cb = callbacks
        self._warn = on_warn or (lambda m: None)
        self.proc = None
        self._reader = None
        self._stopping = False
        self._backoff = RESTART_MIN_S
        self._speaking = None            # id of the utterance being played
        self._marks = {}                 # id -> a future for its `speaking`

    # ------------------------------------------------------------- startup --

    async def capabilities(self):
        """Probed once, at cogiti's startup, so a missing one is a startup
        failure rather than a surprise at the first word."""
        proc = await asyncio.create_subprocess_exec(
            *self.argv, "--capabilities",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        try:
            msg = json.loads(out.decode().strip())
        except ValueError:
            raise SpeechError("speech adapter did not declare its capabilities")
        if msg.get("v") != V or msg.get("type") != "capabilities":
            raise SpeechError("speech adapter declared %r, not a v%d "
                              "capabilities line" % (msg.get("type"), V))
        self.caps = msg
        return msg

    def require(self, needed):
        missing = [c for c in needed if not self.caps.get(c)]
        if missing:
            raise SpeechError("the speech adapter does not support: %s"
                              % ", ".join(missing))

    async def start(self):
        await self._spawn()
        self._reader = asyncio.ensure_future(self._run())

    async def _spawn(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True)      # its own group, like every child

    # -------------------------------------------------------------- events --

    async def _run(self):
        """Read events forever, restarting the adapter if it dies.

        Backoff, and it resets on a clean run rather than on a clean start: an
        adapter that starts, fails on the device and exits would otherwise be
        restarted every half second for ever.
        """
        while not self._stopping:
            started = asyncio.get_event_loop().time()
            try:
                await self._read_events()
            except asyncio.CancelledError:
                raise
            except Exception as e:                            # noqa: BLE001
                self._warn("speech adapter error: %s" % e)

            if self._stopping:
                return
            await self._reap()

            ran_for = asyncio.get_event_loop().time() - started
            if ran_for > 30:
                self._backoff = RESTART_MIN_S
            self._warn("speech adapter exited; restarting in %.1fs"
                       % self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, RESTART_MAX_S)
            try:
                await self._spawn()
            except OSError as e:
                self._warn("speech adapter would not start: %s" % e)

    async def _read_events(self):
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # Not fatal. A device that garbles one line has not gone deaf,
                # and dropping the connection over it would make it so.
                self._warn("unparseable speech event: %r" % line[:120])
                continue
            if msg.get("v") != V:
                self._warn("unknown speech protocol version %r" % msg.get("v"))
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg):
        kind = msg.get("type")
        if kind == "speech_start":
            # The adapter has already stopped its own audio — protocol §5. All
            # that is left for us is the face and the turn.
            self._speaking = None
            await self._call("on_speech_start")
        elif kind == "partial":
            await self._call("on_partial", msg.get("text", ""),
                             bool(msg.get("stable", True)))
        elif kind == "final":
            await self._call("on_final", msg.get("text", ""), msg.get("ms"))
        elif kind == "speech_end":
            await self._call("on_speech_end")
        elif kind == "speaking":
            fut = self._marks.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(msg)
        elif kind == "error":
            await self._call("on_error", msg.get("kind", "device"),
                             msg.get("message", ""))
        else:
            # An adapter that learns to say something new must not break an
            # older cogiti.
            self._warn("unknown speech event %r" % kind)

    async def _call(self, name, *args):
        fn = self.cb.get(name)
        if fn is None:
            return
        out = fn(*args)
        if asyncio.iscoroutine(out):
            await out

    # ------------------------------------------------------------ commands --

    async def say(self, text, utterance_id, timeout_s=20.0):
        """Ask it to speak, and wait for the marks a mouth can move to.

        cogiti never sees the audio: the samples stay in the process that also
        holds the microphone, which is what makes echo cancellation possible.
        """
        if not text:
            return None
        fut = asyncio.get_event_loop().create_future()
        self._marks[utterance_id] = fut
        self._speaking = utterance_id
        await self._send({"type": "say", "id": utterance_id, "text": text})
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            self._warn("speech adapter never reported speaking %r" % utterance_id)
            return None
        finally:
            self._marks.pop(utterance_id, None)

    async def stop(self):
        self._speaking = None
        await self._send({"type": "stop"})

    async def listen(self, enabled):
        await self._send({"type": "listen", "enabled": bool(enabled)})

    async def _send(self, msg):
        if self.proc is None or self.proc.stdin is None:
            return False
        msg.setdefault("v", V)
        try:
            self.proc.stdin.write(
                (json.dumps(msg, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except (OSError, ConnectionError):
            # It died. The reader will notice and restart it; losing one
            # command is better than raising into a turn.
            return False

    # ----------------------------------------------------------- shutdown --

    async def close(self):
        self._stopping = True
        if self._reader:
            self._reader.cancel()
        await self._reap()

    async def _reap(self):
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        self.proc = None
