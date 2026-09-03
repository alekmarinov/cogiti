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


class TestTheQueue(unittest.TestCase):
    """docs/jobs.md §5. The caps have been in LIMITS since jobs.py was
    written and were unreachable until escalations detached: only one could
    exist at a time, because the turn waited for it."""

    def setUp(self):
        self.p = detach.Pending()

    def _running(self, n):
        for i in range(n):
            self.p.add(detach.Detached("j%d" % i, "job %d" % i, None, None))

    def test_room_is_about_what_is_already_detached(self):
        self._running(1)
        self.assertTrue(self.p.has_room(2))
        self._running(2)          # j0 again plus j1 -> two distinct
        self.assertFalse(self.p.has_room(2))

    def test_a_request_over_the_cap_is_accepted_not_refused(self):
        """Never 'I can't'. The request is taken and the person is told what
        it is behind — a spinner and a silent twenty minute delay are the two
        things §5 rules out."""
        self._running(2)
        self.p.enqueue(None, "summarise the repository")
        self.assertEqual(len(self.p.queued), 1)

    def test_it_can_say_what_you_are_waiting_for(self):
        self._running(2)
        waiting = self.p.waiting_on()
        self.assertEqual(sorted(waiting), ["job 0", "job 1"])

    def test_the_queue_is_first_in_first_out(self):
        self.p.enqueue(None, "first")
        self.p.enqueue(None, "second")
        self.assertEqual(self.p.next_queued().text, "first")
        self.assertEqual(self.p.next_queued().text, "second")
        self.assertIsNone(self.p.next_queued())

    def test_finishing_one_makes_room(self):
        """The only moment a slot appears."""
        self._running(2)
        self.assertFalse(self.p.has_room(2))
        self.p.done("j0", {"say": "done"})
        self.assertTrue(self.p.has_room(2))

    def test_a_queued_request_keeps_no_turn(self):
        """It holds the text and the session, never the turn. The turn that
        asked is over, and keeping it would keep alive the one thing that must
        not be reused: its ability to ask the user something."""
        q = self.p.enqueue("a-session", "a question")
        self.assertFalse(hasattr(q, "turn"))
        self.assertEqual(q.text, "a question")


if __name__ == "__main__":
    unittest.main()
