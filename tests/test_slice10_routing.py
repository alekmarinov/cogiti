"""Reaching a service the resolver has never heard of.

`docs/services.md` §5. The blob was compiled before the service was born, so
this layer is the only thing that can route to it — and §5's warning is that
it must not be able to route to anything else.

"Whatever the mechanism, the false-accept rule stands: a born service must not
answer a sentence that was meant for something else. Adding a service is a
reason to add negative eval cases, not just positive ones."
"""

import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import phrases                                    # noqa: E402


class FakeManifest:
    def __init__(self, name, title, patterns):
        self.name, self.title, self.phrases = name, title, patterns


BTC = FakeManifest("bitcoin-price", "the Bitcoin price",
                   ["What's the bitcoin price?", "Show me bitcoin",
                    "What is BTC trading at?"])
ETH = FakeManifest("eth-price", "the ETH price", ["eth price"])


class TestItFindsTheService(unittest.TestCase):
    def test_a_listed_phrase_reaches_it(self):
        for said in ("what's the bitcoin price",
                     "What's the bitcoin price?",
                     "  SHOW ME BITCOIN  ",
                     "please show me bitcoin"):
            m = phrases.match(said, [BTC, ETH])
            self.assertIsNotNone(m, said)
            self.assertEqual(m.name, "bitcoin-price", said)

    def test_an_unlisted_phrasing_does_not(self):
        """§5, in its own words: 'what is eth at' reaches the service; 'how is
        ethereum looking' does not, and escalates like any other unrecognised
        sentence. The fast path covers what the manifest names and nothing
        more."""
        for said in ("how is bitcoin looking",
                     "is bitcoin up or down",
                     "bitcoin"):
            self.assertIsNone(phrases.match(said, [BTC, ETH]), said)


class TestItDoesNotStealAnything(unittest.TestCase):
    """The failure this layer could cause, which is worse than the one it
    fixes."""

    def test_a_sentence_meant_for_a_built_in_is_not_claimed(self):
        for said in ("what time is it", "what's my ip", "turn the volume up",
                     "hello", "cancel that job"):
            self.assertIsNone(phrases.match(said, [BTC, ETH]), said)

    def test_a_service_claiming_a_built_in_sentence_still_never_sees_it(self):
        """Even if a manifest lists it. This layer runs only after the
        resolver has produced nothing at all, so a claimed built-in sentence
        never reaches here — and if it somehow did, the claim would work,
        which is why the phrases are read aloud at the gate."""
        greedy = FakeManifest("greedy", "a greedy service", ["what time is it"])
        self.assertIsNotNone(phrases.match("what time is it", [greedy]))
        # ...and the guard that actually protects this is ordering, asserted
        # where the ordering lives: session consults this only when the
        # resolver returned nothing.

    def test_two_services_claiming_one_sentence_answer_neither(self):
        """Answering with one of them at random is how a device becomes
        unpredictable in the way people remember."""
        other = FakeManifest("btc2", "another", ["Show me bitcoin"])
        self.assertIsNone(phrases.match("show me bitcoin", [BTC, other]))

    def test_nothing_installed_claims_nothing(self):
        self.assertIsNone(phrases.match("what's the bitcoin price", []))

    def test_an_empty_utterance_claims_nothing(self):
        for said in ("", "   ", ".", "?"):
            self.assertIsNone(phrases.match(said, [BTC]))


class TestCollisionsAreVisible(unittest.TestCase):
    def test_it_can_say_which_phrases_are_already_taken(self):
        """For the review gate: a service asking for a sentence another
        service already answers is a collision the user should hear about
        before approving, not after."""
        taken = phrases.claimed_by_others(
            ["Show me bitcoin", "something new"], [BTC, ETH], me="new-one")
        self.assertEqual(taken, {"Show me bitcoin": "the Bitcoin price"})

    def test_a_service_does_not_collide_with_itself(self):
        taken = phrases.claimed_by_others(BTC.phrases, [BTC, ETH],
                                          me="bitcoin-price")
        self.assertEqual(taken, {})


if __name__ == "__main__":
    unittest.main()


class TestPhrasesItWillNeverGet(unittest.TestCase):
    """Built-ins always win (§5), so a phrase the resolver already recognises
    never reaches the service that claimed it. Found on a device: a weather
    service claimed "show the weather", which is an exemplar of the built-in
    pin_thing. The built-in took it — correctly — and the manifest went on
    saying the service would answer it."""

    class Decision:
        def __init__(self, intent_id):
            self.intent_id = intent_id

    def resolver(self, known):
        def resolve(text):
            return self.Decision(known[text]) if text in known else None
        return resolve

    def test_it_names_the_phrase_a_built_in_already_owns(self):
        taken = phrases.unreachable(
            ["Show the weather", "pin the weather"],
            self.resolver({"show the weather": "pin_thing"}))
        self.assertEqual(taken, {"Show the weather": "pin_thing"})

    def test_a_phrase_nothing_recognises_is_reachable(self):
        self.assertEqual(
            phrases.unreachable(["what is btc trading at"],
                                self.resolver({})), {})

    def test_a_resolver_that_throws_does_not_stop_the_gate(self):
        """The gate asking is more important than this warning being complete;
        an exception here would mean nothing is asked at all."""
        def angry(_text):
            raise RuntimeError("no resolver configured")
        self.assertEqual(phrases.unreachable(["anything"], angry), {})
