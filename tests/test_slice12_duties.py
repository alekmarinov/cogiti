"""Duties — a command line that outlives its turn, and one that nobody asked for.

The second thing the job engine has run that is not an agent. timers.py's
docstring set that experiment up: "the job engine has only ever run agent jobs
and their tools; a timer is the cheapest way to find out whether it works for
anything else." A `run` job is the answer for the other half — something that
takes minutes rather than seconds, and reports when it is done.
"""

import asyncio, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import db as D, jobs, table as table_mod                  # noqa: E402


def cmd(**spec):
    spec.setdefault("job", "run")
    return table_mod.Command(spec.pop("intent", "t"), spec)


class TestTheTable(unittest.TestCase):
    """A `run` job is the deployment's command line, and the table is where a
    deployment says what its machine is."""

    def test_a_run_job_needs_an_argv(self):
        with self.assertRaises(table_mod.TableError):
            cmd(speak="off we go")

    def test_argv_must_be_a_list_not_a_shell_string(self):
        """A table is data an operator edits, and data that becomes a shell
        string is data that becomes an injection."""
        with self.assertRaises(table_mod.TableError):
            cmd(argv="lpkg upgrade --yes")

    def test_argv_belongs_only_to_a_run_job(self):
        with self.assertRaises(table_mod.TableError):
            cmd(job="timer", argv=["true"])

    def test_a_duty_may_not_tick_faster_than_the_floor(self):
        with self.assertRaises(table_mod.TableError):
            cmd(argv=["true"], every_s=5)
        self.assertEqual(cmd(argv=["true"], every_s=10).every_s, 10)

    def test_notice_is_for_jobs(self):
        with self.assertRaises(table_mod.TableError):
            table_mod.Command("t", {"provider": "shell", "notice": "hm"})


class TestOneAtATime(unittest.TestCase):
    """lpkg has no lock of its own. Two upgrades at once interleave over the
    same files and the loser rebuilds its index from a half-written database,
    so this cap is the only serialization that exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = D.open_db(os.path.join(self.tmp.name, "s.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_second_run_job_is_refused_while_one_is_live(self):
        _id, proc = jobs.spawn(self.db, "run", "first", "duty", ["sleep", "5"])
        try:
            with self.assertRaises(jobs.Backpressure):
                jobs.spawn(self.db, "run", "second", "duty", ["sleep", "5"])
        finally:
            proc.kill(); proc.wait()

    def test_and_allowed_again_once_it_finishes(self):
        _id, proc = jobs.spawn(self.db, "run", "first", "duty", ["true"])
        proc.wait()
        D.set_state(self.db, _id, "done")
        _id2, proc2 = jobs.spawn(self.db, "run", "second", "duty", ["true"])
        proc2.wait()
        self.assertNotEqual(_id, _id2)


if __name__ == "__main__":
    unittest.main()
