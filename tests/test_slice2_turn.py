"""The turn machine, end to end: type something, get an answer."""
import asyncio, io, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import config as C, db as D                        # noqa: E402
from cogiti.main import Cogiti                                 # noqa: E402
from cogiti.turn import State                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = "%s %s/fakes/agent.py" % (sys.executable, HERE)


def cogiti_for(scenario, tmp, **over):
    values = dict(C.DEFAULTS)
    values.update({"state_dir": tmp, "output": "text",
                   "agent_adapter": "%s --script %s/scenarios/%s" % (FAKE, HERE, scenario),
                   "trace_file": os.path.join(tmp, "trace.jsonl")})
    values.update(over)
    cfg = C.Config(values, {k: "test" for k in values})
    return Cogiti(cfg)


class TestTurn(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    async def test_an_utterance_becomes_a_spoken_answer(self):
        c = cogiti_for("two-tools.json", self.tmp)
        await c.start()
        s = c.session()
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            # two-tools asks for tools it was not granted; the broker refuses
            # each one and the adapter still produces its result.
            res = await s.utterance("what is eth at")
        finally:
            sys.stdout = real
        self.assertEqual(res["type"], "result")
        self.assertIn("2,400", out.getvalue())
        self.assertEqual(s.current.state, State.IDLE)
        self.assertEqual(len(s.history), 1)

    async def test_a_question_reaches_the_person_and_the_answer_returns(self):
        c = cogiti_for("asks-then-answers.json", self.tmp)
        await c.start()
        s = c.session()
        task = asyncio.ensure_future(s.utterance("look at the repo"))
        for _ in range(100):                       # wait for the question
            await asyncio.sleep(0.05)
            if s.current and s.current.state is State.NEEDS_INPUT:
                break
        self.assertEqual(s.current.state, State.NEEDS_INPUT)
        self.assertEqual(s.current.question["ask"], "which repository?")
        out = io.StringIO(); real, sys.stdout = sys.stdout, out
        try:
            self.assertTrue(await s.answer("cogiti"))
            res = await task
        finally:
            sys.stdout = real
        self.assertEqual(res["say"], "looked at the one you named")

    async def test_a_new_utterance_interrupts_the_one_in_flight(self):
        c = cogiti_for("slow.json", self.tmp)
        await c.start()
        s = c.session()
        first = asyncio.ensure_future(s.utterance("the slow one"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if s.current and s.current.state is State.THINKING:
                break
        slow_job = None
        for row in D.live_jobs(c.db):
            if row["kind"] == "agent":
                slow_job = row["id"]
        self.assertIsNotNone(slow_job)

        out = io.StringIO(); real, sys.stdout = sys.stdout, out
        try:
            second = await s.utterance("never mind, this one")
        finally:
            sys.stdout = real
        self.assertIsNotNone(second)
        # Both turns run the same scenario, so the *second* one legitimately
        # finishes and speaks. What must not happen is the interrupted one also
        # speaking: exactly one answer, and one history entry.
        self.assertEqual(out.getvalue().count("finished"), 1)
        self.assertEqual(len(s.history), 1,
                         "an interrupted turn must not enter the history")
        # And the interrupted agent must actually be gone. Cancelling the task
        # only cancels the coroutine; the process has to be signalled.
        row = D.get_job(c.db, slow_job)
        self.assertEqual(row["state"], "cancelled")
        from cogiti import jobs
        self.assertEqual(jobs._group_members(row["pgid"]), [],
                         "the interrupted adapter is still running")

    async def test_no_output_configured_is_a_startup_failure(self):
        with self.assertRaises(C.ConfigError):
            cogiti_for("two-tools.json", self.tmp, output="", 
                       presentation_adapter="", speech_adapter="")

    async def test_no_agent_adapter_is_a_startup_failure(self):
        with self.assertRaises(C.ConfigError):
            cogiti_for("two-tools.json", self.tmp, agent_adapter="")


class TestConfig(unittest.TestCase):
    def test_precedence_and_who_decided(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "cogiti.conf")
        open(path, "w").write("state_dir = /from/file\negress_hosts = a.example\n")
        cfg = C.load(["--state-dir=/from/flag"], conf_path=path,
                     environ={"COGITI_EGRESS_HOSTS": "b.example"})
        self.assertEqual(cfg["state_dir"], "/from/flag")
        self.assertEqual(cfg.origin("state_dir"), "--state_dir")
        self.assertEqual(cfg.list("egress_hosts"), ["b.example"])
        self.assertEqual(cfg.origin("egress_hosts"), "$COGITI_EGRESS_HOSTS")
        self.assertEqual(cfg.origin("output"), "built-in default")

    def test_a_named_path_that_does_not_exist_stops_startup(self):
        with self.assertRaises(C.ConfigError):
            C.load(["--presentation-adapter=/no/such/thing"], conf_path="/nonexistent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
