"""How an answer is spoken, and how long it stays on the screen afterwards.

Two rules that live in `FaceOutput`, both of them learned from a device:

  **The adapter holding the microphone speaks, when it can.** The samples it
  plays are its echo canceller's reference; a second process playing audio out
  of band leaves the canceller nothing to subtract.

  **A device that cannot cancel must not listen while it talks.** Without it,
  the device transcribed its own answer, escalated it, answered *that*, and
  kept going.
"""

import asyncio, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import table                                      # noqa: E402
from cogiti.main import FaceOutput                            # noqa: E402


class FakeAdapter:
    connected = True


class FakePresenter:
    """Records what reached the screen, in order."""

    def __init__(self):
        self.a = FakeAdapter()
        self.shown = []
        self.expired = []
        self.spoke = []

    def result(self, result):
        if result is None or not result.get("show"):
            return None
        oid = "brain/answer"
        self.shown.append(oid)
        return oid

    def speak(self, marks):
        self.spoke.append(marks)
        return True

    def expire(self, oid):
        self.expired.append(oid)

    def stop(self):
        pass


class FakeVoice:
    """The speech port's out half, as audi implements it."""

    def __init__(self, seconds=0.0):
        self.said = []
        self.listening = []
        self.seconds = seconds

    async def say(self, text, utterance_id, timeout_s=25.0):
        self.said.append(text)
        return {"visemes": [[0.0, "AA"], [self.seconds, "sil"]],
                "audio_start_ns": 0, "seconds": self.seconds}

    async def listen(self, enabled):
        self.listening.append(bool(enabled))


def answer(say="It's two.", show="14:00", **kw):
    return dict({"type": "result", "say": say, "show": show}, **kw)


class TestTheVoice(unittest.IsolatedAsyncioTestCase):
    async def test_the_adapter_that_hears_is_the_one_that_speaks(self):
        """Not the standalone `speech_adapter`. It is the fallback for a
        deployment whose perception adapter has no voice, not the default."""
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer())
        self.assertEqual(v.said, ["It's two."])
        self.assertEqual(len(p.spoke), 1, "the marks never reached the mouth")

    async def test_a_half_duplex_device_stops_listening_while_it_talks(self):
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice, out.half_duplex = v, True
        await out.say(answer())
        self.assertEqual(v.listening, [False, True],
                         "muted before speaking and unmuted after, in that order")

    async def test_a_device_that_can_cancel_keeps_listening(self):
        """Muting is the price of having no echo canceller, not a policy. An
        adapter that declares barge_in must stay interruptible."""
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice, out.half_duplex = v, False
        await out.say(answer())
        self.assertEqual(v.listening, [], "it deafened a device that need not be")

    async def test_an_interrupted_answer_still_leaves_it_listening(self):
        """The failure that would be permanent: cancelled mid-sentence, and
        the microphone never comes back."""
        p, v = FakePresenter(), FakeVoice(seconds=5.0)
        out = FaceOutput(p, speech=None)
        out.voice, out.half_duplex = v, True

        task = asyncio.ensure_future(out.say(answer()))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(v.listening, [False, True],
                         "it stayed deaf after being interrupted")


class TestHowLongAnAnswerStays(unittest.IsolatedAsyncioTestCase):
    async def test_an_answer_comes_down_after_its_time(self):
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer(linger=0.1))
        self.assertEqual(p.expired, [], "it went before it was ever read")
        await asyncio.sleep(0.25)
        self.assertEqual(p.expired, ["brain/answer"])

    async def test_the_countdown_starts_when_the_answer_stops_being_spoken(self):
        """Not when it appeared. A long answer would otherwise spend most of
        its ten seconds still being read aloud."""
        p, v = FakePresenter(), FakeVoice(seconds=0.4)
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer(linger=0.2))
        self.assertEqual(p.expired, [])
        await asyncio.sleep(0.35)
        self.assertEqual(p.expired, ["brain/answer"])

    async def test_zero_means_it_stays_until_something_replaces_it(self):
        """The behaviour every card had before this existed, and still the
        right one for anything a person is meant to act on."""
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer(linger=0))
        await asyncio.sleep(0.25)
        self.assertEqual(p.expired, [])

    async def test_a_new_answer_cancels_the_old_countdown(self):
        """Otherwise the expiry set by the previous answer fires late and
        takes down the one that just replaced it — the two share an id."""
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer(linger=0.15))
        await out.say(answer(say="It's three.", linger=5.0))
        await asyncio.sleep(0.3)
        self.assertEqual(p.expired, [],
                         "the old timer reached through and removed the new card")

    async def test_a_command_with_no_linger_gets_the_default(self):
        self.assertEqual(table.DEFAULT_LINGER, 10.0)
        p, v = FakePresenter(), FakeVoice()
        out = FaceOutput(p, speech=None)
        out.voice = v
        await out.say(answer())          # no linger at all
        self.assertIsNotNone(out._expiry, "nothing was ever going to take it down")
        out._expiry.cancel()


if __name__ == "__main__":
    unittest.main()
