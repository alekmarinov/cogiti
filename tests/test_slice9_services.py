"""Services as data: the manifest, the supervisor, and removal.

Stage 5a. No agent writes anything here — that is 5b, and this stage exists to
make 5b reviewable.
"""

import asyncio, os, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import authoring, broker, manifest, services                 # noqa: E402


def write(root, name, toml, main="import time\nwhile True: time.sleep(1)\n"):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "service.toml"), "w").write(textwrap.dedent(toml))
    open(os.path.join(d, "main.py"), "w").write(main)
    return d


GOOD = '''
    name = "clock"
    title = "the clock"
    exec = ["python3", "main.py"]
'''


class TestTheManifest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def load(self, toml, name="clock"):
        write(self.root, name, toml)
        return manifest.load(os.path.join(self.root, name, "service.toml"))

    def test_a_minimal_manifest_gets_the_documented_defaults(self):
        m = self.load(GOOD)
        self.assertEqual(m.namespace, "clock")
        self.assertEqual(m.region, "periphery")
        self.assertEqual(m.limits["memory_mb"], 128)
        self.assertEqual(m.limits["processes"], 4)

    def test_a_service_may_not_take_the_conversational_region(self):
        """services.md §9, first line. A service is pinned, always."""
        with self.assertRaises(manifest.ManifestError) as e:
            self.load(GOOD + '\nregion = "stage"\n')
        self.assertIn("never conversational", str(e.exception))

    def test_the_namespace_must_equal_the_name(self):
        """Two names for one thing is two things that can disagree, and the
        one that disagrees is the one on screen."""
        with self.assertRaises(manifest.ManifestError):
            self.load(GOOD + '\nnamespace = "something-else"\n')

    def test_exec_is_a_list_so_nothing_reaches_a_shell(self):
        with self.assertRaises(manifest.ManifestError) as e:
            self.load('name="clock"\ntitle="t"\nexec="python3 main.py"\n')
        self.assertIn("shell", str(e.exception))

    def test_an_unknown_limit_is_refused_rather_than_ignored(self):
        """In 5b an agent writes this and a person approves it by ear. A field
        that is silently ignored is a field the gate cannot show."""
        with self.assertRaises(manifest.ManifestError) as e:
            self.load(GOOD + '\n[limits]\ndisk_mb = 10\n')
        self.assertIn("disk_mb", str(e.exception))

    def test_phrases_are_validated_and_not_used(self):
        """5a settles the format so 5c does not invent it twice."""
        m = self.load(GOOD + '\n[phrases]\npatterns = ["what time is it"]\n')
        self.assertEqual(m.phrases, ["what time is it"])

    def test_a_directory_that_does_not_match_its_manifest_is_refused(self):
        write(self.root, "clock", GOOD.replace('"clock"', '"other"'))
        found, broken = manifest.load_all(self.root)
        self.assertEqual(found, {})
        self.assertEqual(len(broken), 1)
        self.assertIn("does not match its directory", broken[0])

    def test_a_directory_with_no_manifest_is_not_a_service_and_not_an_error(self):
        os.makedirs(os.path.join(self.root, "half-installed"))
        found, broken = manifest.load_all(self.root)
        self.assertEqual((found, broken), ({}, []))


class TestTheSupervisor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.warnings = []

    def sup(self):
        s = services.Services(self.root, on_warn=self.warnings.append)
        s.load()
        return s

    async def test_it_starts_and_stops_a_service(self):
        write(self.root, "clock", GOOD)
        s = self.sup()
        await s.start("clock")
        self.assertTrue(s.services["clock"].alive)
        await s.stop("clock")
        self.assertFalse(s.services["clock"].alive)

    async def test_three_failures_in_a_minute_stops_it_for_good(self):
        """services.md §6. A crash loop that continues forever is worse than a
        service that is off, because nobody finds out about it."""
        write(self.root, "crasher", GOOD.replace('"clock"', '"crasher"'),
              main="import sys\nsys.exit(1)\n")
        s = self.sup()
        s.services["crasher"].backoff = 0.01      # do not wait out the real one
        await s.start("crasher")
        for _ in range(200):
            if s.services["crasher"].state == services.NEEDS_ATTENTION:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(s.services["crasher"].state, services.NEEDS_ATTENTION)
        self.assertTrue(any("stopped after 3 failures" in w
                            for w in self.warnings), self.warnings)

    async def test_stopping_it_is_not_a_crash(self):
        """A service we stopped must not be restarted, and must not count
        towards the three."""
        write(self.root, "clock", GOOD)
        s = self.sup()
        await s.start("clock")
        await s.stop("clock")
        await asyncio.sleep(0.2)
        self.assertEqual(s.services["clock"].state, services.STOPPED)
        self.assertEqual(s.services["clock"].failures, [])

    async def test_removal_moves_it_and_leaves_nothing(self):
        """§7: thirty days of undo, because removal is a voice command and
        voice commands are misheard."""
        write(self.root, "clock", GOOD)
        removed = tempfile.mkdtemp()
        s = self.sup()
        await s.start("clock")
        title = await s.remove("clock", removed)
        self.assertEqual(title, "the clock")
        self.assertNotIn("clock", s.services)
        self.assertFalse(os.path.exists(os.path.join(self.root, "clock")))
        self.assertEqual(len(os.listdir(removed)), 1)


class TestApproval(unittest.IsolatedAsyncioTestCase):
    """services.md §4: the approval binds to the code and the manifest."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.warnings = []

    def sup(self):
        s = services.Services(self.root, on_warn=self.warnings.append,
                              drop_privileges=False)
        s.load()
        return s

    async def test_an_edited_service_does_not_start(self):
        from cogiti import approval
        d = write(self.root, "clock", GOOD)
        approval.record(d, "spoken", [], [], [])
        with open(os.path.join(d, "main.py"), "a") as f:
            f.write("\n# somebody edited this\n")
        s = self.sup()
        await s.start("clock")
        self.assertEqual(s.services["clock"].state, services.NEEDS_ATTENTION)
        self.assertIn("code has changed", s.services["clock"].detail)

    async def test_a_widened_manifest_is_a_new_decision(self):
        """No code moved, and what the device hears has changed. §4 hashes the
        manifest for exactly this."""
        from cogiti import approval
        d = write(self.root, "clock", GOOD)
        approval.record(d, "spoken", [], [], [])
        with open(os.path.join(d, "service.toml"), "a") as f:
            f.write('\n[phrases]\npatterns = ["what time is it"]\n')
        s = self.sup()
        await s.start("clock")
        self.assertEqual(s.services["clock"].state, services.NEEDS_ATTENTION)
        self.assertIn("manifest has changed", s.services["clock"].detail)

    async def test_a_hand_installed_service_needs_no_approval(self):
        """5a shipped two by hand. A rule that stopped the device running what
        its owner put there would be a rule about the wrong thing."""
        write(self.root, "clock", GOOD)
        s = self.sup()
        await s.start("clock")
        self.assertTrue(s.services["clock"].alive)
        await s.stop("clock")


class TestTheBroker(unittest.IsolatedAsyncioTestCase):
    """The allow-list is enforced here or nowhere: a service is a separate
    process, and anything it can do to a socket of its own it can do to any
    host."""

    def test_a_declared_host_is_allowed(self):
        ok, _ = broker._permitted("https://api.example.com/v1",
                                  ["api.example.com"])
        self.assertTrue(ok)

    def test_an_undeclared_host_is_refused_and_says_which(self):
        ok, why = broker._permitted("https://evil.example/x",
                                    ["api.example.com"])
        self.assertFalse(ok)
        self.assertIn("evil.example", why)
        self.assertIn("api.example.com", why)

    def test_a_subdomain_is_not_the_host_that_was_declared(self):
        """No wildcards: a service declaring *.example.com has declared a
        namespace somebody else can register in, and the 5b gate reads these
        aloud."""
        ok, _ = broker._permitted("https://sub.api.example.com/x",
                                  ["api.example.com"])
        self.assertFalse(ok)

    def test_plain_http_is_refused(self):
        ok, why = broker._permitted("http://api.example.com/x",
                                    ["api.example.com"])
        self.assertFalse(ok)
        self.assertIn("https", why)

    def test_a_declared_host_that_resolves_inside_is_still_refused(self):
        """The standard shape of a server-side request forgery: the service
        declares a name the user approved, and the name resolves to the
        network the device is sitting on. trust.py has refused this since
        slice 1; the broker was not calling it."""
        ok, why = broker._permitted("https://localhost/x", ["localhost"])
        self.assertFalse(ok)
        self.assertIn("not a global address", why)

    def test_a_literal_private_address_is_refused_even_if_declared(self):
        ok, why = broker._permitted("https://127.0.0.1/x", ["127.0.0.1"])
        self.assertFalse(ok)
        self.assertIn("non-global", why)

    def test_an_empty_allow_list_permits_nothing(self):
        ok, _ = broker._permitted("https://api.example.com/x", [])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class TestAuthoring(unittest.IsolatedAsyncioTestCase):
    """services.md §4. Steps 3 and 4 exist so step 5 is a decision about
    purpose rather than about code."""

    def setUp(self):
        self.staging = tempfile.mkdtemp()

    def stage(self, toml=None, main=None):
        d = os.path.join(self.staging, "eth")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "service.toml"), "w").write(textwrap.dedent(
            toml or '''
            name = "eth"
            title = "the ETH price"
            exec = ["python3", "main.py"]
            interval_s = 1
            '''))
        open(os.path.join(d, "main.py"), "w").write(textwrap.dedent(
            main or '''
            from cogiti.service import Service, every
            svc = Service()

            @every(1)
            def tick():
                svc.show(kind="text", text="x")

            svc.run()
            '''))
        return d

    async def test_a_working_service_passes_and_produces_updates(self):
        d = self.stage()
        m, updates = await authoring.vet(d)
        self.assertGreaterEqual(len(updates), authoring.REQUIRED_UPDATES)
        self.assertEqual(m.name, "eth")

    async def test_a_forbidden_import_never_gets_run(self):
        """Static checks come first on purpose: reading the source costs
        milliseconds and running unknown code costs ninety seconds and
        whatever the code does."""
        d = self.stage(main="import subprocess\n")
        with self.assertRaises(authoring.Rejected) as e:
            await authoring.vet(d)
        self.assertIn("subprocess", str(e.exception))

    async def test_a_service_that_reaches_an_undeclared_host_is_refused(self):
        """Its description would be wrong, and the description is the thing
        the user is approving."""
        d = self.stage(main=textwrap.dedent('''
            from cogiti.service import Service, every
            svc = Service()

            @every(1)
            async def tick():
                await svc.get_json("https://sneaky.example/x")

            svc.run()
            '''))
        with self.assertRaises(authoring.Rejected) as e:
            await authoring.vet(d)
        self.assertIn("sneaky.example", str(e.exception))

    async def test_a_service_that_pins_nothing_is_refused(self):
        """A service that runs and shows nothing has produced no duty. It
        never reaches the person."""
        d = self.stage(main="import time\nwhile True: time.sleep(1)\n")
        with self.assertRaises(authoring.Rejected) as e:
            await authoring.vet(d)
        self.assertIn("update", str(e.exception))

    async def test_a_service_that_exits_immediately_is_refused(self):
        d = self.stage(main="pass\n")
        with self.assertRaises(authoring.Rejected) as e:
            await authoring.vet(d)
        self.assertIn("stopped on its own", str(e.exception))

    def test_the_gate_names_the_host_the_interval_and_every_phrase(self):
        """§4: the phrases are part of the decision, not a detail of it. They
        decide which sentences stop going to the model."""
        from cogiti import manifest as _m
        d = self.stage(toml='''
            name = "eth"
            title = "the ETH price"
            exec = ["python3", "main.py"]
            interval_s = 60
            [network]
            allow = ["api.coingecko.com"]
            [phrases]
            patterns = ["eth price", "what is eth at"]
            ''')
        said = authoring.review(_m.load(os.path.join(d, "service.toml")), [])
        self.assertIn("api.coingecko.com", said)
        self.assertIn("every minute", said)
        self.assertIn("no passwords", said)
        self.assertIn("'eth price'", said)
        self.assertIn("'what is eth at'", said)
        self.assertTrue(said.endswith("Shall I keep it?"), said)

    async def test_installing_records_what_was_said(self):
        from cogiti import approval
        d = self.stage()
        m, _ = await authoring.vet(d)
        root = tempfile.mkdtemp()
        spoken = "I've written a thing. Shall I keep it?"
        dest = authoring.install(d, root, m, spoken)
        ok, why = approval.verify(dest)
        self.assertTrue(ok, why)
        self.assertEqual(approval.load(dest)["spoken"], spoken)


class TestTheForm(unittest.IsolatedAsyncioTestCase):
    """The model fills a form; cogiti owns the code. services.md §4 step 2,
    with the decision that the model never writes Python at all."""

    def spec(self, **over):
        return dict({"name": "eth-price", "title": "the ETH price",
                     "url": "https://api.coingecko.com/api/v3/simple/price",
                     "path": ["ethereum", "usd"], "format": "ETH ${value}",
                     "interval_s": 60, "phrases": ["eth price"]}, **over)

    def test_the_generated_code_passes_the_static_checks(self):
        """The template must not trip its own net — and it is checked like
        anything else, because a mistake in a template is a mistake in every
        service made from it."""
        from cogiti import service_template, static_checks, manifest as _m
        spec = service_template.validate(self.spec())
        code, man = service_template.render(spec)
        d = tempfile.mkdtemp()
        open(os.path.join(d, "main.py"), "w").write(code)
        open(os.path.join(d, "service.toml"), "w").write(man)
        m = _m.load(os.path.join(d, "service.toml"))
        self.assertEqual(static_checks.check(code, m), {"api.coingecko.com"})

    def test_the_allow_list_comes_from_the_url_it_was_given(self):
        """Derived, not declared twice: the code and its description cannot
        disagree about which host is reached."""
        from cogiti import service_template, manifest as _m
        spec = service_template.validate(
            self.spec(url="https://api.open-meteo.com/v1/forecast"))
        _code, man = service_template.render(spec)
        d = tempfile.mkdtemp()
        open(os.path.join(d, "service.toml"), "w").write(man)
        self.assertEqual(_m.load(os.path.join(d, "service.toml")).allow,
                         ["api.open-meteo.com"])

    def test_a_model_supplied_string_cannot_become_code(self):
        """The one place its text reaches the program is inside a literal."""
        from cogiti import service_template
        spec = service_template.validate(
            self.spec(format='{value}"; import os; os.system("x")  #'))
        code, _man = service_template.render(spec)
        import ast
        tree = ast.parse(code)                      # it still parses
        self.assertNotIn("os.system", [
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)])

    def test_too_many_phrases_are_refused_because_they_are_read_aloud(self):
        from cogiti import service_template
        with self.assertRaises(service_template.BadSpec) as e:
            service_template.validate(self.spec(phrases=["a", "b", "c", "d",
                                                         "e", "f", "g"]))
        self.assertIn("read aloud", str(e.exception))

    async def test_a_bad_form_is_one_complaint_at_a_time(self):
        """The model is going to fix it and try again; five complaints is a
        rewrite that addresses none of them properly."""
        from cogiti import service_template
        with self.assertRaises(service_template.BadSpec) as e:
            service_template.validate(self.spec(url="http://x", interval_s=1))
        self.assertEqual(str(e.exception).count("must"), 1)

    async def test_a_filled_form_becomes_two_files_on_disk(self):
        root = tempfile.mkdtemp()
        d, spec = await authoring.from_spec(self.spec(), root)
        self.assertTrue(os.path.exists(os.path.join(d, "main.py")))
        self.assertTrue(os.path.exists(os.path.join(d, "service.toml")))
        self.assertEqual(spec["name"], "eth-price")


class TestTheGate(unittest.IsolatedAsyncioTestCase):
    """services.md §4 steps 5 and 6. Nothing is installed without a yes."""

    async def test_only_a_yes_installs_it(self):
        """Turn.YES is a small closed set and anything outside it is a no.
        Here that asymmetry is at its most valuable: the cost of getting it
        wrong is a service the user did not ask for, running for ever."""
        from cogiti.turn import Turn
        for answer in ("yes", "go ahead", "sure"):
            self.assertIn(answer, Turn.YES)
        for answer in ("no", "maybe", "wait", "what?", "", "yes but not that"):
            self.assertNotIn(answer, Turn.YES)

    async def test_a_second_proposal_after_a_good_one_is_refused(self):
        """One service per request. A model that proposes twice has changed
        its mind after being told the first one works, and the user is about
        to be read a sentence about a specific thing."""
        root = tempfile.mkdtemp()

        class FakeCogiti:
            broker_path = None
        a = authoring.Author(FakeCogiti(), root)
        a.ready = ("dir", "manifest", [])
        out = await a.propose({"name": "x"})
        self.assertFalse(out["ok"])
        self.assertIn("already", out["problem"])

    async def test_a_bad_form_comes_back_with_one_problem_and_a_count(self):
        """The retry loop is the model calling the tool again. It is told what
        was wrong and how many tries remain, and nothing here loops."""
        root = tempfile.mkdtemp()

        class FakeCogiti:
            broker_path = None
        a = authoring.Author(FakeCogiti(), root)
        out = await a.propose({"name": "Bad Name", "title": "t",
                               "url": "https://x.example", "path": ["a"],
                               "format": "{value}", "interval_s": 60})
        self.assertFalse(out["ok"])
        self.assertIn("lowercase", out["problem"])
        self.assertEqual(out["attempts_left"], authoring.MAX_ATTEMPTS - 1)

    async def test_it_gives_up_after_three(self):
        root = tempfile.mkdtemp()

        class FakeCogiti:
            broker_path = None
        a = authoring.Author(FakeCogiti(), root)
        bad = {"name": "Bad Name", "title": "t", "url": "https://x.example",
               "path": ["a"], "format": "{value}", "interval_s": 60}
        for _ in range(authoring.MAX_ATTEMPTS):
            await a.propose(bad)
        out = await a.propose(bad)
        self.assertIn("last of", out["problem"])


class TestTheGateIsAsked(unittest.IsolatedAsyncioTestCase):
    """The gate must ask its own question.

    It once did not. `Turn.ask` hands back the turn's existing answer future,
    which the "are you sure?" a minute earlier had already resolved with
    "yes" — so awaiting it returned that yes instantly and the gate approved
    itself with the answer to a different question. A service was installed
    without anybody being asked, which is the single thing this stage exists
    to prevent.
    """

    async def test_a_second_question_in_one_turn_is_asked_again(self):
        import asyncio
        from cogiti.turn import Turn, State

        asked = []

        class FakeSession:
            def on_state(self, _turn, _state):
                pass

            async def asked(self, _turn, question):
                asked.append(question)

        t = Turn(FakeSession(), "keep the price on screen")
        t.trace = None

        # First question, answered yes — the "did you mean from now on?".
        first = asyncio.ensure_future(t.confirm("Are you sure?", timeout_s=5))
        await asyncio.sleep(0.05)
        t.answer("yes")
        self.assertTrue(await first)

        # The gate. Nobody answers it.
        second = asyncio.ensure_future(t.confirm("Shall I keep it?",
                                                 timeout_s=0.2))
        self.assertFalse(await second, "the gate reused the earlier yes")
        self.assertEqual(asked, ["Are you sure?", "Shall I keep it?"],
                         "the gate did not put its own question")

    async def test_the_gate_expires_into_no(self):
        """A confirm never times out into yes — and of every confirm in the
        system this is the one where that matters most."""
        import asyncio
        from cogiti.turn import Turn

        class FakeSession:
            def on_state(self, _turn, _state):
                pass

            async def asked(self, _turn, _question):
                pass

        t = Turn(FakeSession(), "keep it on screen")
        self.assertFalse(await t.confirm("Shall I keep it?", timeout_s=0.1))
