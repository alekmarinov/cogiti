"""The presentation and speech ports, against a renderer that records.

A fake renderer rather than avatari itself, for the reason the fake agent
exists: this asserts what cogiti *says*, and a test that needs a GPU and a
window is a test nobody runs. avatari's own suite covers whether it draws.

It is a real socket and a real process boundary, though — newline framing, a
hello handshake, a connection that can be closed underneath us — because those
are the three things a client gets wrong.
"""

import asyncio, json, os, socket, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import present, speech                          # noqa: E402
from cogiti.adapters import presentation                    # noqa: E402

CAPS = {"event": "hello", "protocol": 1,
        "kinds": ["image", "text", "group", "stream"],
        "regions": ["stage", "periphery"],
        "lifetimes": ["turn", "pinned"],
        "ops": ["hello", "create", "update", "destroy", "speak", "expression",
                "gaze", "busy", "query", "idle", "stop"]}


class FakeRenderer:
    """Accepts one connection at a time and records every line."""

    def __init__(self, caps=CAPS, answer_hello=True):
        self.path = os.path.join(tempfile.mkdtemp(), "r.sock")
        self.caps, self.answer_hello = caps, answer_hello
        self.ops = []
        self.connections = 0
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            self._read(c)

    def _read(self, c):
        buf = b""
        try:
            while not self._stop:
                data = c.recv(65536)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    msg["_t"] = time.monotonic()   # when it actually arrived
                    self.ops.append(msg)
                    if msg.get("op") == "hello" and self.answer_hello:
                        c.sendall((json.dumps(self.caps) + "\n").encode())
        except OSError:
            return
        finally:
            c.close()

    def kinds(self):
        return [o.get("op") for o in self.ops]

    def by_op(self, name):
        return [o for o in self.ops if o.get("op") == name]

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


class Base(unittest.TestCase):
    def setUp(self):
        self.r = FakeRenderer()
        self.addCleanup(self.r.close)
        self.a = presentation.Presentation(self.r.path)
        self.addCleanup(self.a.close)
        self.p = present.Presenter(self.a)

    def settle(self):
        for _ in range(200):
            if self.r.ops:
                break
            threading.Event().wait(0.005)
        threading.Event().wait(0.05)


class TestHandshake(Base):
    def test_hello_declares_a_namespace_and_capabilities_come_back(self):
        self.assertTrue(self.a.send(op="busy", state=True))
        self.settle()
        self.assertEqual(self.r.ops[0]["op"], "hello")
        self.assertEqual(self.r.ops[0]["namespace"], "brain")
        self.assertTrue(self.a.supports("kinds", "stream"))
        self.assertFalse(self.a.supports("kinds", "hologram"))

    def test_every_message_carries_the_protocol_version(self):
        self.a.send(op="busy", state=True)
        self.settle()
        self.assertTrue(all(o.get("v") == 1 for o in self.r.ops))

    def test_a_renderer_that_never_answers_hello_is_still_usable(self):
        """Assuming nothing is right; refusing to draw anything is not."""
        r = FakeRenderer(answer_hello=False)
        self.addCleanup(r.close)
        a = presentation.Presentation(r.path)
        self.addCleanup(a.close)
        self.assertTrue(a.send(op="busy", state=True))
        self.assertTrue(a.supports("kinds", "text"))


class TestAbsentRenderer(unittest.TestCase):
    def test_a_missing_renderer_never_raises_and_is_counted(self):
        """A device that stops answering because its screen went away is
        worse than one that answers to nobody."""
        a = presentation.Presentation("/no/such/socket")
        p = present.Presenter(a)
        p.busy(True)
        p.result({"say": "still works", "show": "still works"})
        p.idle()
        self.assertFalse(a.connected)
        self.assertGreater(a.dropped, 0)

    def test_it_reconnects_when_the_renderer_comes_back(self):
        r = FakeRenderer()
        a = presentation.Presentation(r.path)
        self.addCleanup(a.close)
        self.assertTrue(a.send(op="busy", state=True))
        first = r.connections

        # The renderer dies mid-conversation.
        a._sock.close()
        a._sock = None
        a._next_try = 0.0
        self.assertTrue(a.send(op="busy", state=False))
        self.assertGreater(r.connections, first, "did not reconnect")
        r.close()

    def test_pinned_objects_are_re_declared_on_reconnect(self):
        """The renderer is a display, never a database: it kept nothing, so
        whatever should outlive the connection lives in our configuration."""
        r = FakeRenderer()
        self.addCleanup(r.close)
        a = presentation.Presentation(r.path)
        self.addCleanup(a.close)
        a.send(op="create", id="svc/clock", kind="text", text="12:00",
               lifetime="pinned", region="periphery")
        a._sock.close(); a._sock = None; a._next_try = 0.0
        a.send(op="busy", state=True)
        threading.Event().wait(0.05)
        clocks = [o for o in r.ops if o.get("id") == "svc/clock"]
        self.assertEqual(len(clocks), 2, "pinned object was not re-declared")

    def test_a_turn_object_is_not_re_declared(self):
        r = FakeRenderer()
        self.addCleanup(r.close)
        a = presentation.Presentation(r.path)
        self.addCleanup(a.close)
        a.send(op="create", id="brain/answer", kind="text", text="hi",
               lifetime="turn")
        a._sock.close(); a._sock = None; a._next_try = 0.0
        a.send(op="busy", state=True)
        threading.Event().wait(0.05)
        self.assertEqual(len([o for o in r.ops if o.get("id") == "brain/answer"]), 1)


class TestPresenter(Base):
    def test_a_thought_stream_never_takes_the_gaze(self):
        """`watch` would have the head staring at its own reasoning for the
        whole of a long escalation."""
        self.p.thought("checking two sources")
        self.settle()
        c = self.r.by_op("create")[0]
        self.assertEqual(c["kind"], "stream")
        self.assertEqual(c["attention"], "never")

    def test_a_renderer_without_streams_is_shown_no_thoughts(self):
        caps = dict(CAPS, kinds=["text", "image"])
        r = FakeRenderer(caps=caps)
        self.addCleanup(r.close)
        a = presentation.Presentation(r.path)
        self.addCleanup(a.close)
        a.send(op="busy", state=True)          # forces the handshake
        present.Presenter(a).thought("nobody sees this")
        threading.Event().wait(0.05)
        self.assertEqual(r.by_op("create"), [])

    def test_show_becomes_an_object_and_say_does_not(self):
        """They are deliberately different strings: one is heard, one is read."""
        self.p.result({"say": "a long spoken sentence", "show": "short line"})
        self.settle()
        c = self.r.by_op("create")[0]
        self.assertEqual(c["text"], "short line")
        self.assertEqual(c["region"], "stage")
        self.assertEqual(c["lifetime"], "turn")

    def test_a_spoken_only_result_draws_nothing(self):
        self.p.result({"say": "just heard"})
        self.settle()
        self.assertEqual(self.r.by_op("create"), [])

    def test_a_structured_show_always_gets_a_fallback(self):
        """The port says an unknown kind must still hold its place, which it
        can only do if it was given something to draw."""
        self.p.result({"say": "the price is 42", "show": {"kind": "chart"}})
        self.settle()
        c = self.r.by_op("create")[0]
        self.assertEqual(c["kind"], "chart")
        self.assertIn("42", c["fallback"])

    def test_thoughts_are_cleared_when_the_answer_arrives(self):
        self.p.thought("thinking out loud")
        self.p.result({"say": "done", "show": "done"})
        self.settle()
        self.assertEqual(self.r.by_op("destroy")[0]["id"], present.THOUGHTS)

    def test_a_failure_is_shown_not_swallowed(self):
        self.p.result({"type": "failed", "kind": "upstream", "message": "429"})
        self.settle()
        self.assertIn("429", self.r.by_op("create")[0]["text"])


class TestSpeak(Base):
    def test_one_speak_carries_the_marks_and_the_clock(self):
        marks = {"visemes": [[0.0, "AA"], [0.3, "sil"]],
                 "audio": "/tmp/u.wav", "audio_start_ns": 123456789}
        self.p.speak(marks)
        self.settle()
        sp = self.r.by_op("speak")
        self.assertEqual(len(sp), 1, "one message, not one per viseme")
        self.assertEqual(sp[0]["visemes"], marks["visemes"])
        self.assertEqual(sp[0]["audio_start_ns"], 123456789)
        self.assertEqual(sp[0]["audio"], "/tmp/u.wav")

    def test_silence_still_moves_the_mouth(self):
        """avatari lipsyncs with no sound card, which is the whole reason the
        head is demonstrable on a workstation with no audio at all."""
        self.p.speak({"visemes": [[0.0, "AA"]], "audio_start_ns": 1})
        self.settle()
        self.assertNotIn("audio", self.r.by_op("speak")[0])


class TestSpeakingTakesTime(unittest.IsolatedAsyncioTestCase):
    """A turn must stay in `speaking` for as long as speaking takes.

    Found by running it, not by reading it: cogiti sent `speak` and then
    `idle` in the same millisecond, and avatari's idle handler calls
    audio_stop() and viseme_stop() — so cogiti cancelled its own utterance
    120 ms before it was due to start. Every individual message was correct
    and the head never moved.

    Tested through main.FaceOutput rather than the Presenter, because the
    defect was in the *ordering* the output imposes, and a Presenter test
    would have passed throughout.
    """

    def setUp(self):
        from cogiti import main as _main
        self.main = _main
        self.r = FakeRenderer()
        self.addCleanup(self.r.close)
        self.a = presentation.Presentation(self.r.path)
        self.addCleanup(self.a.close)

    def output(self, seconds):
        class FakeSpeech:
            async def marks(self, text):
                import time as _t
                return {"visemes": [[0.0, "AA"], [seconds, "sil"]],
                        "seconds": seconds, "audio": "/tmp/x.wav",
                        "audio_start_ns":
                            _t.clock_gettime_ns(_t.CLOCK_MONOTONIC)}
        return self.main.FaceOutput(present.Presenter(self.a), FakeSpeech())

    async def test_say_does_not_return_before_the_utterance_ends(self):
        out = self.output(0.4)
        t0 = asyncio.get_event_loop().time()
        await out.say({"say": "a short line", "show": "line"})
        self.assertGreaterEqual(asyncio.get_event_loop().time() - t0, 0.4)

    async def test_idle_never_lands_while_the_mouth_is_still_moving(self):
        """The assertion that would have caught it: idle after speak is not
        wrong, idle *immediately* after speak is."""
        out = self.output(0.3)
        await out.say({"say": "hello", "show": "hello"})
        out.on_state("idle")
        threading.Event().wait(0.05)
        ops = self.r.kinds()
        self.assertIn("speak", ops)
        self.assertIn("idle", ops)
        self.assertLess(ops.index("speak"), ops.index("idle"))

        spoke = [o for o in self.r.ops if o.get("op") == "speak"][0]
        idled = [o for o in self.r.ops if o.get("op") == "idle"][0]
        self.assertGreaterEqual(idled["_t"] - spoke["_t"], 0.3,
                                "idle arrived while the utterance was playing")

    async def test_an_interruption_stops_the_mouth(self):
        """Cancelling the turn must not leave a mouth running under whatever
        comes next. ports.md fixes the order: presentation stops first."""
        out = self.output(5.0)
        task = asyncio.ensure_future(out.say({"say": "a long one", "show": "x"}))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        threading.Event().wait(0.05)
        self.assertIn("stop", self.r.kinds())


class TestSpeechAdapter(unittest.IsolatedAsyncioTestCase):
    def adapter(self, script):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "speak")
        open(p, "w").write("#!/bin/sh\n" + script + "\n")
        os.chmod(p, 0o755)
        return [p]

    async def test_marks_come_back_with_a_monotonic_start(self):
        s = speech.Speech(self.adapter(
            'echo \'{"visemes":[[0.0,"AA"],[0.2,"sil"]],"audio":"/tmp/a.wav"}\''))
        m = await s.marks("hello")
        self.assertEqual(len(m["visemes"]), 2)
        self.assertGreater(m["audio_start_ns"], 0)

    async def test_the_lead_is_in_the_future(self):
        """Without it the first viseme is always already late: the message
        still has to cross a socket and wait for the renderer's next frame."""
        import time as _t
        s = speech.Speech(self.adapter('echo \'{"visemes":[[0.0,"AA"]]}\''),
                          lead_ms=120)
        m = await s.marks("hi")
        self.assertGreater(m["audio_start_ns"],
                           _t.clock_gettime_ns(_t.CLOCK_MONOTONIC))

    async def test_engine_chatter_on_stderr_is_not_a_failure(self):
        s = speech.Speech(self.adapter(
            'echo "loading voices" >&2; echo \'{"visemes":[[0.0,"AA"]]}\''))
        self.assertIsNotNone(await s.marks("hi"))

    async def test_an_adapter_that_writes_nothing_is_not_a_crash(self):
        """An appliance that cannot speak should still show its answer."""
        warned = []
        s = speech.Speech(self.adapter("exit 3"), on_warn=warned.append)
        self.assertIsNone(await s.marks("hi"))
        self.assertTrue(warned)

    async def test_a_missing_adapter_is_not_a_crash(self):
        s = speech.Speech(["/no/such/engine"], on_warn=lambda m: None)
        self.assertIsNone(await s.marks("hi"))

    async def test_it_does_not_hang_under_the_event_loop(self):
        """The bug this file was written after: the loop's child watcher reaps
        the process, so a blocking Popen.wait() never sees the exit status and
        stalls until its timeout. 60 ms of synthesis became 20 seconds."""
        s = speech.Speech(self.adapter('echo \'{"visemes":[[0.0,"AA"]]}\''))
        m = await asyncio.wait_for(s.marks("hi"), timeout=5)
        self.assertIsNotNone(m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
