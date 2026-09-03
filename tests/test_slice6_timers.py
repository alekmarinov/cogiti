"""Timers — the first job that is not an agent.

The job engine has only ever run agent jobs and their tools. A timer is the
cheapest thing that exercises it with something else, which is most of why it
was built first.
"""

import asyncio, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import db as D, table as table_mod, timers                # noqa: E402
from cogiti import detach                                    # noqa: E402


class FakeCogiti:
    def __init__(self, db):
        self.pending = detach.Pending()
        self.db = db


class TestHuman(unittest.TestCase):
    """Spoken aloud, so it rounds the way a person would."""

    def test_it_reads_as_speech(self):
        for seconds, said in ((1, "1 second"), (30, "30 seconds"),
                              (60, "1 minute"), (90, "1 minute 30 seconds"),
                              (300, "5 minutes"), (3600, "1 hour"),
                              (5400, "1 hour 30 minutes"), (7200, "2 hours")):
            self.assertEqual(timers.human(seconds), said)


class TestTimers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = D.open_db(os.path.join(tempfile.mkdtemp(), "jobs.db"))
        self.t = timers.Timers(FakeCogiti(self.db))
        self.fired = []

    async def fire(self, job_id, title):
        self.fired.append(title)

    async def asyncTearDown(self):
        await self.t.shutdown()

    async def test_a_timer_is_a_job_with_a_group_and_a_deadline(self):
        """A real process, so the whole of jobs.cancel applies to it without
        any of that being written a second time for a special case."""
        job_id, title = self.t.set("s1", 30, self.fire)
        row = D.get_job(self.db, job_id)
        self.assertEqual(row["kind"], "timer")
        self.assertEqual(row["state"], "running")
        self.assertTrue(row["pgid"], "not in its own process group")
        self.assertTrue(row["deadline_ns"], "no deadline, so nothing can say "
                                            "how long is left")
        self.assertEqual(title, "the timer for 30 seconds")

    async def test_it_fires_and_announces(self):
        self.t.set("s1", 1, self.fire)
        for _ in range(400):
            if self.fired:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.fired, ["the timer for 1 second"])

    async def test_a_cancelled_timer_never_goes_off(self):
        """The one outcome worth being careful about. A cancelled `sleep` dies
        of SIGTERM and exits non-zero — but so would one killed by anything
        else, which is why firing is decided by the row and not the exit code."""
        job_id, _ = self.t.set("s1", 1, self.fire)
        self.t.cancel(job_id)
        await asyncio.sleep(1.5)
        self.assertEqual(self.fired, [])
        self.assertNotEqual(D.get_job(self.db, job_id)["state"], "done")

    async def test_running_lists_the_live_ones_newest_first(self):
        self.t.set("s1", 30, self.fire)
        _second, title = self.t.set("s1", 40, self.fire)
        live = timers.running(self.db)
        self.assertEqual(len(live), 2)
        self.assertEqual(live[0]["title"], title, "newest first")

    async def test_a_cancelled_timer_leaves_the_live_set(self):
        job_id, _ = self.t.set("s1", 30, self.fire)
        self.t.set("s1", 40, self.fire)
        self.t.cancel(job_id)
        self.assertEqual(len(timers.running(self.db)), 1)

    async def test_shutdown_leaves_nothing_running(self):
        """Cancelled rather than left behind: the sleep would outlive cogiti
        with nothing to announce it, and the row would say `running` forever."""
        self.t.set("s1", 60, self.fire)
        self.t.set("s1", 60, self.fire)
        await self.t.shutdown()
        self.assertEqual(timers.running(self.db), [])


class TestTableJobs(unittest.TestCase):
    """A job is what a command becomes when it outlives its turn."""

    def err(self, **spec):
        with self.assertRaises(table_mod.TableError) as e:
            table_mod.Command("t", spec)
        return str(e.exception)

    def test_exactly_one_of_provider_or_job(self):
        self.assertIn("exactly one", self.err(speak="x"))
        self.assertIn("exactly one",
                      self.err(provider="clock.now", job="timer"))

    def test_an_unknown_job_kind_is_refused_at_load(self):
        self.assertIn("unknown job kind", self.err(job="nope"))

    def test_only_a_job_may_announce(self):
        """announce is what it says when it finishes, possibly an hour later
        with nobody having asked. A command finishes inside its turn."""
        self.assertIn("not a job",
                      self.err(provider="clock.now", announce="done"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
