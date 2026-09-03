"""An escalation that outlives its turn.

Stage 4's bar, from the roadmap: *a long job runs while you ask three
unrelated questions, you can ask what it is doing and watch its logs, and
cancelling it leaves no process behind.* The last clause is already covered by
test_slice1; these are the other three.
"""

import asyncio, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import detach                                     # noqa: E402


class TestTheDeadline(unittest.IsolatedAsyncioTestCase):
    async def test_a_quick_answer_never_becomes_a_job(self):
        """The common case must stay a conversation. Three seconds of
        thinking is a pause, not a background task."""
        async def quick():
            await asyncio.sleep(0.01)
            return {"type": "result", "say": "half past two"}

        result, running = await detach.with_deadline(quick(), seconds=0.5)
        self.assertIsNotNone(result)
        self.assertIsNone(running, "a fast escalation was detached")

    async def test_a_slow_one_detaches_and_keeps_running(self):
        """The work survives the wait ending. This is the whole stage: not
        'give up after five seconds' but 'stop waiting after five seconds'."""
        finished = []

        async def slow():
            await asyncio.sleep(0.3)
            finished.append(True)
            return {"type": "result", "say": "the long answer"}

        result, running = await detach.with_deadline(slow(), seconds=0.05)
        self.assertIsNone(result)
        self.assertIsNotNone(running)
        self.assertFalse(running.cancelled(),
                         "wait_for cancelled the work it was waiting on")

        out = await running
        self.assertEqual(finished, [True], "the job died when the turn ended")
        self.assertEqual(out["say"], "the long answer")

    async def test_an_interrupted_turn_takes_its_escalation_with_it(self):
        """Barge-in cancels a turn, and nobody is waiting for that answer any
        more. Leaving it running would spend a model call on a question the
        user has already moved on from."""
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(5)
            return {"type": "result", "say": "nobody wants this"}

        async def turn():
            return await detach.with_deadline(slow(), seconds=5)

        t = asyncio.ensure_future(turn())
        await started.wait()
        await asyncio.sleep(0)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t


class TestWhereTheAnswerGoes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = detach.Pending()

    def _detached(self, job_id="j1", title="the long question"):
        return detach.Detached(job_id, title, None, None)

    def test_an_answer_waits_rather_than_interrupting(self):
        d = self._detached()
        self.p.add(d)
        self.assertEqual(len(self.p), 1)
        self.p.done("j1", {"type": "result", "say": "42"})
        self.assertEqual(len(self.p), 0, "still counted as running")
        self.assertEqual(len(self.p.finished), 1)

    def test_taking_it_takes_it_once(self):
        """Read at a moment of cogiti's choosing, and then gone — otherwise
        the same answer is announced at the end of every turn forever."""
        self.p.add(self._detached())
        self.p.done("j1", {"say": "42"})
        self.assertEqual(len(self.p.take()), 1)
        self.assertEqual(self.p.take(), [])

    def test_a_cancelled_job_delivers_nothing(self):
        """The user stopped it. Announcing its answer afterwards is the device
        arguing with them."""
        self.p.add(self._detached())
        self.p.done("j1", {"say": "42"})
        self.p.drop("j1")
        self.assertEqual(self.p.take(), [])

    def test_dropping_one_that_never_finished_forgets_it(self):
        self.p.add(self._detached())
        self.p.drop("j1")
        self.assertEqual(len(self.p), 0)
        self.assertEqual(self.p.take(), [])


if __name__ == "__main__":
    unittest.main()
