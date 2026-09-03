"""Services as data: the manifest, the supervisor, and removal.

Stage 5a. No agent writes anything here — that is 5b, and this stage exists to
make 5b reviewable.
"""

import asyncio, os, sys, tempfile, textwrap, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import broker, manifest, services                 # noqa: E402


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

    def test_an_empty_allow_list_permits_nothing(self):
        ok, _ = broker._permitted("https://api.example.com/x", [])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
