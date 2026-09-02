"""The speech port, against a fake that is a real long-lived process.

The fake does not import cogiti and writes its own codec from the document, so
agreement between the two sides is evidence rather than tautology — the same
arrangement the agent port uses and for the same reason.
"""

import asyncio, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti.adapters import audi                              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = [sys.executable, os.path.join(HERE, "fakes", "audi.py")]


class Base(unittest.IsolatedAsyncioTestCase):
    def script(self, spec):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        with open(path, "w") as f:
            json.dump(spec, f)
        return FAKE + ["--script", path]

    async def listen(self, spec, **callbacks):
        s = audi.Speech(self.script(spec), on_warn=lambda m: None, **callbacks)
        await s.start()
        self.addCleanup(lambda: asyncio.get_event_loop().run_until_complete(
            s.close()) if False else None)
        return s

    async def drain(self, predicate, timeout=6.0):
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False


class TestCapabilities(Base):
    async def test_probed_once_and_asserted_at_startup(self):
        s = audi.Speech(FAKE)
        caps = await s.capabilities()
        self.assertTrue(caps["partials"])
        self.assertTrue(caps["barge_in"])
        s.require(["partials", "barge_in"])

    async def test_a_missing_capability_stops_startup_by_name(self):
        s = audi.Speech(self.script({"capabilities": {"barge_in": False}}))
        await s.capabilities()
        with self.assertRaises(audi.SpeechError) as e:
            s.require(["barge_in"])
        self.assertIn("barge_in", str(e.exception))

    async def test_half_duplex_is_a_valid_adapter_not_a_broken_one(self):
        """A deployment that declines barge-in needs no echo cancellation and
        is a perfectly good appliance you have to wait for."""
        s = audi.Speech(self.script({"capabilities": {"barge_in": False}}))
        caps = await s.capabilities()
        self.assertFalse(caps["barge_in"])
        s.require(["partials"])          # does not raise


class TestEvents(Base):
    async def test_an_utterance_arrives_as_start_partials_end_final(self):
        seen = []
        s = await self.listen(
            {"steps": [{"emit": {"type": "speech_start"}},
                       {"partials": ["turn the", "turn the volume up"]},
                       {"emit": {"type": "speech_end"}},
                       {"emit": {"type": "final", "text": "turn the volume up",
                                 "ms": 900}}]},
            on_speech_start=lambda: seen.append("start"),
            on_partial=lambda t, stable: seen.append(("partial", t, stable)),
            on_speech_end=lambda: seen.append("end"),
            on_final=lambda t, ms: seen.append(("final", t)))

        await self.drain(lambda: any(x[0] == "final" for x in seen
                                     if isinstance(x, tuple)))
        await s.close()
        self.assertEqual(seen[0], "start")
        self.assertEqual(seen[-1], ("final", "turn the volume up"))
        partials = [x for x in seen if isinstance(x, tuple) and x[0] == "partial"]
        self.assertEqual([p[1] for p in partials],
                         ["turn the", "turn the volume up"])

    async def test_partials_declare_whether_they_may_be_acted_on(self):
        """The field that decides which recognisers suit this port. A
        window-based recogniser whose partials rewrite themselves says so
        rather than breaking the promise cogiti makes about pattern-tier
        matches."""
        seen = []
        s = await self.listen(
            {"steps": [{"partials": ["ten", "turn"], "stable": False}]},
            on_partial=lambda t, stable: seen.append(stable))
        await self.drain(lambda: len(seen) >= 2)
        await s.close()
        self.assertEqual(seen, [False, False])

    async def test_speech_start_need_not_become_words(self):
        """A cough, a door, a passing conversation. cogiti must be able to
        return to what it was doing."""
        seen = []
        s = await self.listen(
            {"steps": [{"emit": {"type": "speech_start"}},
                       {"wait_ms": 30},
                       {"emit": {"type": "speech_end"}}]},
            on_speech_start=lambda: seen.append("start"),
            on_speech_end=lambda: seen.append("end"),
            on_final=lambda t, ms: seen.append("final"))
        await self.drain(lambda: "end" in seen)
        await s.close()
        self.assertEqual(seen, ["start", "end"])

    async def test_a_garbled_line_does_not_deafen_the_device(self):
        """A device that garbles one line has not gone deaf, and dropping the
        connection over it would make it so."""
        seen = []
        s = await self.listen(
            {"steps": [{"emit_raw": "{ not json"},
                       {"emit": {"type": "final", "text": "still here", "ms": 1}}]},
            on_final=lambda t, ms: seen.append(t))
        await self.drain(lambda: seen)
        await s.close()
        self.assertEqual(seen, ["still here"])

    async def test_an_unknown_event_is_ignored_not_fatal(self):
        seen = []
        s = await self.listen(
            {"steps": [{"emit": {"type": "humming", "bpm": 120}},
                       {"emit": {"type": "final", "text": "ok", "ms": 1}}]},
            on_final=lambda t, ms: seen.append(t))
        await self.drain(lambda: seen)
        await s.close()
        self.assertEqual(seen, ["ok"])


class TestSaying(Base):
    async def test_say_returns_marks_and_never_audio(self):
        """The samples stay in the process that also holds the microphone,
        which is what makes echo cancellation possible at all."""
        s = await self.listen({"steps": [{"await_say": True},
                                         {"say_back": True},
                                         {"wait_ms": 2000}]})
        marks = await s.say("It's half past two.", "u1")
        await s.close()
        self.assertIsNotNone(marks)
        self.assertIn("visemes", marks)
        self.assertIn("audio_start_ns", marks)
        self.assertNotIn("audio", marks, "no samples and no path cross the wire")

    async def test_an_adapter_that_never_reports_speaking_does_not_hang(self):
        s = await self.listen({"steps": [{"await_say": True},
                                         {"wait_ms": 3000}]})
        marks = await s.say("hello", "u1", timeout_s=0.4)
        await s.close()
        self.assertIsNone(marks)

    async def test_stop_reaches_the_adapter(self):
        seen = []
        s = await self.listen({"steps": [{"expect_stop": True}]},
                              on_error=lambda k, m: seen.append(m))
        await asyncio.sleep(0.2)
        await s.stop()
        await self.drain(lambda: seen)
        await s.close()
        self.assertEqual(seen, ["stop received"])


class TestBargeIn(Base):
    async def test_the_adapter_stops_its_own_audio_before_telling_cogiti(self):
        """Protocol §5. A round trip is time spent talking over someone, and
        the adapter is the only party that can act sooner."""
        seen = []
        s = await self.listen(
            {"steps": [{"await_say": True}, {"say_back": True},
                       {"wait_ms": 50},
                       {"barge_in": {"partials": ["no"], "final": "no stop"}}]},
            on_speech_start=lambda: seen.append("start"),
            on_final=lambda t, ms: seen.append(("final", t)))
        marks = await s.say("a long answer", "u1")
        self.assertIsNotNone(marks)
        await self.drain(lambda: any(isinstance(x, tuple) for x in seen))
        await s.close()
        self.assertEqual(seen[0], "start", "speech_start must arrive first")
        self.assertEqual(seen[-1], ("final", "no stop"))


class TestSurvival(Base):
    async def test_a_dead_adapter_is_restarted(self):
        """Losing this adapter is losing the ears, and a device that has gone
        deaf without mentioning it is the worst version of that."""
        warned, seen = [], []
        s = audi.Speech(
            self.script({"steps": [{"emit": {"type": "final", "text": "one",
                                             "ms": 1}}, {"exit": 1}]}),
            on_warn=warned.append, on_final=lambda t, ms: seen.append(t))
        s._backoff = 0.05
        await s.start()
        await self.drain(lambda: len(seen) >= 2, timeout=8)
        await s.close()
        self.assertGreaterEqual(len(seen), 2, "did not restart and resume")
        self.assertTrue(any("restarting" in w for w in warned))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSessionByVoice(unittest.IsolatedAsyncioTestCase):
    """The loop driven by speech events rather than by typing."""

    def setUp(self):
        sys.path.insert(0, HERE)
        from test_slice5_fastpath import (FakeCogiti, FakeDecision, command)
        from cogiti import providers, session as session_mod
        providers.load_all()
        self.mod = session_mod
        self.c = FakeCogiti(
            {"turn the volume up": FakeDecision("volume_up")},
            {"volume_up": command("volume_up", speak="Turned it up.")})
        self.s = session_mod.Session(self.c)

        async def fake_escalate(cogiti, sess, turn):
            self.c.escalated.append(turn.text)
            await asyncio.sleep(5)          # a slow answer, so it can be cut off
            return {"type": "result", "say": "a long answer"}

        self._saved = session_mod.escalate.run
        session_mod.escalate.run = fake_escalate
        self.addCleanup(lambda: setattr(session_mod.escalate, "run",
                                        self._saved))

    async def test_a_final_transcript_drives_a_turn(self):
        await self.s.heard("turn the volume up")
        self.assertEqual([i for i, _ in self.c.ran], ["volume_up"])

    async def test_an_unstable_partial_is_not_resolved(self):
        """A recogniser that rewrites its own text cannot support the promise
        cogiti makes about a pattern-tier match."""
        self.assertIsNone(await self.s.heard_partial("turn the", False))

    async def test_a_stable_partial_resolves_but_does_not_act(self):
        d = await self.s.heard_partial("turn the volume up", True)
        self.assertIsNotNone(d)
        self.assertEqual(self.c.ran, [], "a partial must produce no effect")

    async def test_speech_start_interrupts_a_turn_in_flight(self):
        """The case the old condition missed: a turn that cannot be stopped
        while it talks is one that talks over you."""
        task = asyncio.ensure_future(self.s.heard("something open ended"))
        for _ in range(200):
            if self.c.escalated:
                break
            await asyncio.sleep(0.005)

        await self.s.heard_start()
        await asyncio.sleep(0.05)
        self.assertTrue(task.done() or task.cancelled()
                        or self.s.current is None)
        task.cancel()

    async def test_speech_start_that_never_becomes_words_starts_nothing(self):
        """A cough, a door, a passing conversation."""
        await self.s.heard_start()
        self.assertEqual(self.c.ran, [])
        self.assertEqual(self.c.escalated, [])

    async def test_barge_in_stops_the_mouth(self):
        stopped = []
        self.c.output.barge_in = lambda: stopped.append(True)
        await self.s.heard_start()
        self.assertEqual(stopped, [True])


class TestReaderNeverBlocks(Base):
    """A `final` starts a turn; the turn may need an event from this adapter.

    The reader delivers both. If it waits for the turn while the turn waits
    for `speaking`, nothing arrives until say() gives up — which is what
    happened on the device: every answer took exactly the timeout, twenty
    seconds, and was spoken long after the question.
    """

    async def test_a_turn_may_ask_the_adapter_to_speak_and_be_answered(self):
        marks = {}
        ready = asyncio.Event()

        async def on_final(text, ms):
            # A turn, in miniature: it answers by asking this same adapter to
            # speak, and cannot finish until the adapter reports back.
            marks["got"] = await speech.say("It's half past two.", "u1",
                                            timeout_s=4.0)
            ready.set()

        speech = audi.Speech(
            self.script({"steps": [
                {"emit": {"type": "final", "text": "what time is it", "ms": 400}},
                {"await_say": True},
                {"say_back": True},
            ]}),
            on_warn=lambda m: None, on_final=on_final)
        await speech.start()

        self.assertTrue(await self.drain(ready.is_set, timeout=6.0),
                        "the turn never finished: the reader was blocked "
                        "inside it, so `speaking` could not be delivered")
        self.assertIsNotNone(marks["got"], "say() timed out under its own reader")
        self.assertIn("visemes", marks["got"])
        await speech.close()

    async def test_a_slow_turn_does_not_stop_the_next_event_arriving(self):
        """The second half of the same rule. While a turn is running, speech
        events keep arriving — which is what makes barge-in possible at all."""
        seen = []

        async def on_final(text, ms):
            seen.append(("final", text))
            await asyncio.sleep(1.5)          # a turn that takes its time
            seen.append(("done", text))

        speech = audi.Speech(
            self.script({"steps": [
                {"emit": {"type": "final", "text": "first", "ms": 400}},
                {"wait_ms": 200},
                {"emit": {"type": "speech_start"}},
            ]}),
            on_warn=lambda m: None,
            on_final=on_final,
            on_speech_start=lambda: seen.append(("start", None)))
        await speech.start()

        ok = await self.drain(lambda: ("start", None) in seen, timeout=3.0)
        self.assertTrue(ok, "speech_start waited for the turn to finish")
        self.assertNotIn(("done", "first"), seen,
                         "it arrived only after the turn ended, which is the "
                         "blocking this test exists to catch")
        await speech.close()
