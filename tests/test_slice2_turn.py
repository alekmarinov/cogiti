"""The turn machine, end to end: type something, get an answer."""
import asyncio, io, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import config as C, db as D                        # noqa: E402
from cogiti.main import Cogiti                                 # noqa: E402
from cogiti.turn import State                                  # noqa: E402
from cogiti.session import HISTORY                             # noqa: E402

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


class TestWhatTheModelIsTold(unittest.IsolatedAsyncioTestCase):
    """The escalation context, and the four ways it used to lose an exchange.

    All four were found in one live transcript. The device asked "Are you
    sure?"; the person said "am I sure what?"; the model that answered them
    had no record that a question had been put, because a confirm was never
    written down and the turn that put it was interrupted, which was not
    written down either. Everything a person witnessed has to be in here, or
    the next thing they say arrives with half its meaning missing.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.c = cogiti_for("two-tools.json", self.tmp)
        self.s = self.c.session()

    def recent(self):
        return self.s.context()["recent"]

    async def test_a_question_the_device_put_is_recorded(self):
        from cogiti.turn import Turn
        await self.s.asked(Turn(self.s, "pin the clock"),
                           "Keep it on the screen from now on?")
        self.assertEqual(self.recent(),
                         [{"asked": "Keep it on the screen from now on?"}])

    async def test_the_answer_to_it_is_recorded_as_an_answer(self):
        """Not as a fresh utterance. "yes" on its own is meaningless; "yes",
        against the question it answers, is the whole exchange."""
        class Waiting:
            text = "delete the bitcoin service"
            question = "Remove it? I can put it back for a month."
            state = State.CONFIRMING
            def needs_answer(self):
                return True
            def answer(self, _v):
                pass
        self.s.current = Waiting()
        self.assertTrue(await self.s.answer("go on then"))
        self.assertEqual(self.recent(), [{
            "said": "go on then",
            "answering": "Remove it? I can put it back for a month."}])

    async def test_an_interrupted_turn_is_still_recorded(self):
        """Cutting a turn short is a reason to say nothing, not a reason to
        forget. Every `[interrupted]` in the live trace was an utterance the
        person made and the model was never shown."""
        from cogiti.turn import Turn
        await self.c.start()
        turn = Turn(self.s, "what is eth at")
        turn.interrupted = True
        out = io.StringIO()
        real, sys.stdout = sys.stdout, out
        try:
            await self.s._run(turn)
        finally:
            sys.stdout = real
        self.assertEqual(self.recent(),
                         [{"said": "what is eth at", "interrupted": True}])

    async def test_a_spoken_answer_answers_rather_than_interrupts(self):
        """A confirm could not be answered by voice at all.

        "Pin bitcoin price on screen" — "Keep it on the screen from now on?"
        — "yes, please": the yes started a fresh turn, interrupted the one
        waiting for it, and went to the model, which was asked out of nowhere
        to react to somebody agreeing to nothing. The typed loop had always
        checked for a pending question; the microphone path never did.
        """
        answered = []
        class Waiting:
            text = "pin bitcoin price on screen"
            question = "Keep it on the screen from now on?"
            state = State.CONFIRMING
            def needs_answer(self):
                return True
            def answer(self, v):
                answered.append(v)
        self.s.current = Waiting()
        await self.s.heard("yes, please")
        self.assertEqual(answered, ["yes, please"],
                         "the answer started a new turn instead")

    async def test_a_stray_yes_is_not_a_request(self):
        """Heard on the device: two escalations were already detached, so the
        queue took a bare "yes" and said "I'll get to that when I've finished
        the bitcoin price" — a slot spent and a sentence offered in reply to
        a word that requested nothing."""
        self.assertIsNone(await self.s.heard("yes."))
        self.assertEqual(self.recent(), [], "it started a turn")

    async def test_the_words_that_are_also_commands_still_get_through(self):
        """`stop` is in Turn.NEGATIONS and is also the barge-in intent, where
        latency is the whole feature. The first cut of this filter reused
        that set and would have made the device ignore the one word it must
        never ignore."""
        from cogiti.session import bare_answer
        for word in ("stop", "cancel that", "wait", "never mind"):
            self.assertFalse(bare_answer(word), word)

    async def test_it_keeps_only_the_last_few(self):
        for i in range(HISTORY + 4):
            self.s.remember(said=str(i))
        self.assertEqual(len(self.recent()), HISTORY)
        self.assertEqual(self.recent()[-1], {"said": str(HISTORY + 3)})


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
            C.load(["--speech-adapter=/no/such/thing"], conf_path="/nonexistent")

    def test_a_presentation_socket_that_is_not_there_yet_is_not_an_error(self):
        """It names a socket, not a program. The renderer is a separate
        process with its own lifetime, and the port requires cogiti to survive
        it going away and coming back — so 'absent right now' is a normal
        state, and refusing to start would mean cogiti could never be started
        before the face."""
        cfg = C.load(["--presentation-adapter=/no/such/socket"],
                     conf_path="/nonexistent")
        self.assertEqual(cfg["presentation_adapter"], "/no/such/socket")


class TestPathsRelativeToTheFile(unittest.TestCase):
    """A development config names a checkout, and a checkout is not in the
    same place on two machines. Without this the file carries one developer's
    home directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "sub", "cogiti.conf")
        os.makedirs(os.path.dirname(self.conf))

    def write(self, text):
        open(self.conf, "w").write(text)
        return self.conf

    def test_relative_resolves_against_the_file_not_the_shell(self):
        os.makedirs(os.path.join(self.tmp, "cards"))
        cfg = C.load([], conf_path=self.write("presentation_dir = ../cards\n"))
        self.assertEqual(cfg["presentation_dir"],
                         os.path.join(self.tmp, "cards"))

    def test_a_tilde_is_a_home_directory(self):
        cfg = C.load([], conf_path=self.write("state_dir = ~/.local/state/x\n"))
        self.assertEqual(cfg["state_dir"],
                         os.path.expanduser("~/.local/state/x"))
        self.assertNotIn("~", cfg["state_dir"])

    def test_every_path_in_an_argv_is_resolved_and_flags_are_left_alone(self):
        """`agent_adapter` names an interpreter and a script; the speech one
        carries flags between its paths."""
        for name in ("py", "adapter.py"):
            open(os.path.join(self.tmp, name), "w").close()
        cfg = C.load([], conf_path=self.write(
            "agent_adapter = ../py --flag ../adapter.py\n"))
        self.assertEqual(cfg["agent_adapter"],
                         "%s/py --flag %s/adapter.py" % (self.tmp, self.tmp))

    def test_a_bare_word_is_never_treated_as_a_path(self):
        """Only ./ and ../ resolve. A rule that guessed which bare words were
        paths would eventually guess wrong about one that was not."""
        cfg = C.load([], conf_path=self.write(
            "egress_hosts = example.com, api.github.com\n"))
        self.assertEqual(cfg.list("egress_hosts"),
                         ["example.com", "api.github.com"])

    def test_an_absolute_path_is_untouched(self):
        """/etc/cogiti.conf on the appliance names absolute paths and must
        keep meaning exactly what it says."""
        cfg = C.load([], conf_path=self.write(
            "presentation_adapter = /run/avatari.sock\n"))
        self.assertEqual(cfg["presentation_adapter"], "/run/avatari.sock")

    def test_the_environment_is_not_resolved_against_the_file(self):
        """A path in the environment belongs to whoever set it, and they may
        not know which config file is being read."""
        cfg = C.load([], conf_path=self.write("state_dir = /x\n"),
                     environ={"COGITI_STATE_DIR": "~/from-env"})
        self.assertEqual(cfg["state_dir"], os.path.expanduser("~/from-env"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
