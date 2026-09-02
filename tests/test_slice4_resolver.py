"""The resolver port, bound to reflexi through ctypes.

These skip rather than fail when reflexi is not built beside cogiti: this is
the one port whose adapter is a compiled artifact, and a suite that cannot run
in a checkout without a C toolchain is a suite people stop running. `make test`
in reflexi builds what is needed.

The offset test is the one that earns its place. A ctypes struct laid out
wrongly does not crash — it silently reads the wrong field, so a confidence
becomes a verdict and a destructive intent becomes `handle`. Checking the
layout against this file would prove nothing, so it is checked against the C
compiler.
"""

import ctypes, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti.adapters import resolver as R                     # noqa: E402

REFLEXI = os.environ.get("REFLEXI_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "reflexi"))
LIB = os.path.join(REFLEXI, "build", "debug", "libreflexi.so")
BLOB = os.path.join(REFLEXI, "build", "reflexi.blob")
CONF = os.path.join(REFLEXI, "config", "thresholds.toml")
HAVE = os.path.exists(LIB) and os.path.exists(BLOB)


@unittest.skipUnless(HAVE, "reflexi not built; run `make` in ../reflexi")
class TestABI(unittest.TestCase):
    """The struct layouts, against the compiler rather than against us."""

    @unittest.skipUnless(shutil.which("cc") or shutil.which("gcc"),
                         "no C compiler")
    def test_every_offset_matches_the_header(self):
        fields = []
        for cls, name in ((R._Slot, "reflexi_slot"),
                          (R._Decision, "reflexi_decision"),
                          (R._Options, "reflexi_options")):
            for f, _t in cls._fields_:
                fields.append((name, f, getattr(cls, f).offset))

        # Sizes too, and from the same source. An earlier version of this
        # test computed the expected size by hand and got it wrong by three
        # bytes of padding — proving only that hand-computing a struct layout
        # is the thing being guarded against.
        sizes = [("reflexi_slot", R._Slot), ("reflexi_decision", R._Decision),
                 ("reflexi_options", R._Options)]

        src = ['#include <stdio.h>', '#include <stddef.h>',
               '#include <reflexi.h>', 'int main(void){']
        for name, f, _ in fields:
            src.append('printf("%%zu\\n", offsetof(%s,%s));' % (name, f))
        for name, _cls in sizes:
            src.append('printf("%%zu\\n", sizeof(%s));' % name)
        src.append('return 0;}')

        d = tempfile.mkdtemp()
        c = os.path.join(d, "abi.c")
        open(c, "w").write("\n".join(src))
        exe = os.path.join(d, "abi")
        cc = shutil.which("cc") or shutil.which("gcc")
        subprocess.run([cc, "-I", os.path.join(REFLEXI, "include"), c,
                        "-o", exe], check=True, capture_output=True)
        got = subprocess.run([exe], check=True,
                             capture_output=True).stdout.decode().split()

        self.assertEqual(len(got), len(fields) + len(sizes))

        for (name, f, mine), theirs in zip(fields, got):
            self.assertEqual(mine, int(theirs),
                             "%s.%s: python %d, C %s" % (name, f, mine, theirs))

        # Offsets alone would not catch a trailing field this binding does not
        # know about: every offset it does know would still match.
        for (name, cls), theirs in zip(sizes, got[len(fields):]):
            self.assertEqual(ctypes.sizeof(cls), int(theirs),
                             "sizeof(%s): python %d, C %s"
                             % (name, ctypes.sizeof(cls), theirs))


@unittest.skipUnless(HAVE, "reflexi not built; run `make` in ../reflexi")
class TestResolve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = R.Resolver(LIB, BLOB, config=CONF, device_location="Sofia")

    @classmethod
    def tearDownClass(cls):
        cls.r.close()

    def test_a_listed_phrase_resolves_deterministically(self):
        d = self.r.resolve("turn the volume up")
        self.assertEqual(d.verdict, "handle")
        self.assertEqual(d.intent_id, "volume_up")
        self.assertEqual(d.tier, "pattern")

    def test_a_destructive_intent_never_reaches_handle(self):
        """reflexi's invariant, asserted from cogiti's side too. A 0.73 cosine
        must not turn off the device, and cogiti cannot repair a resolver that
        guessed — so if this ever regresses, it must fail here as well."""
        d = self.r.resolve("power off")
        self.assertEqual(d.intent_id, "power_off")
        self.assertEqual(d.verdict, "confirm")

    def test_an_unresolvable_utterance_escalates_with_no_intent(self):
        d = self.r.resolve("explain quantum entanglement to me")
        self.assertEqual(d.verdict, "escalate")
        self.assertIsNone(d.intent_id)

    def test_a_missing_slot_escalates_but_keeps_the_intent(self):
        """The one case where an escalation still carries an intent, so cogiti
        can ask 'a timer for how long?' instead of paying a model to."""
        d = self.r.resolve("set a timer")
        self.assertEqual(d.verdict, "escalate")
        self.assertEqual(d.intent_id, "set_timer")
        self.assertEqual(d.missing_slot, "duration")

    def test_a_stated_slot_and_a_defaulted_one_are_distinguishable(self):
        """'the weather where you always mean' and 'the weather in Sofia
        because you said Sofia' are different answers."""
        stated = self.r.resolve("what's the weather in Sofia")
        self.assertFalse(stated.slots["location"]["defaulted"])
        defaulted = self.r.resolve("what's the weather")
        self.assertTrue(defaulted.slots["location"]["defaulted"])
        self.assertEqual(defaulted.slots["location"]["value"], "Sofia")

    def test_a_duration_arrives_canonicalised(self):
        d = self.r.resolve("set a timer for 5 minutes")
        self.assertEqual(d.slots["duration"]["value"], "300")

    def test_a_decision_survives_the_next_resolve(self):
        """The C struct is reused and its buffers are overwritten, so a
        decision that was not copied out would mutate under its holder —
        a bug that shows up once, months later, in a confirmation prompt."""
        first = self.r.resolve("what's the weather in Sofia")
        self.r.resolve("turn the volume up")
        self.assertEqual(first.intent_id, "get_weather")
        self.assertEqual(first.slots["location"]["value"], "Sofia")

    def test_an_over_long_utterance_escalates_rather_than_raising(self):
        self.assertIsNone(self.r.resolve("x" * (R.UTTERANCE_MAX + 10)))

    def test_it_is_fast_enough_to_run_on_every_partial(self):
        """The number is the entire justification for the port being a linked
        library rather than a process, so it is worth asserting rather than
        believing."""
        import time
        n = 500
        t = time.perf_counter()
        for _ in range(n):
            self.r.resolve("turn the volume up")
        us = (time.perf_counter() - t) / n * 1e6
        self.assertLess(us, 500, "%.1f us per resolve" % us)


class TestMisconfiguration(unittest.TestCase):
    def test_a_missing_library_is_named_not_a_stack_trace(self):
        with self.assertRaises(R.ResolverError) as e:
            R.Resolver("/no/such/lib.so", "/no/such/blob")
        self.assertIn("library", str(e.exception))

    @unittest.skipUnless(HAVE, "reflexi not built")
    def test_a_missing_blob_is_named(self):
        with self.assertRaises(R.ResolverError) as e:
            R.Resolver(LIB, "/no/such/blob")
        self.assertIn("blob", str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
