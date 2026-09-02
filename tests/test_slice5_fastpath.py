"""Resolve, act, confirm, and ask for a missing slot.

Against fakes rather than the real resolver: what is under test is the routing
cogiti does with a decision, not reflexi's decisions. The real resolver has its
own suite next door and `test_slice4_resolver.py` checks the binding between.
"""

import asyncio, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import providers, session as session_mod, table as table_mod  # noqa: E402
from cogiti.turn import State                                            # noqa: E402


class FakeDecision:
    def __init__(self, intent=None, verdict="handle", tier="pattern",
                 slots=None, missing=None):
        self.intent_id, self.verdict, self.tier = intent, verdict, tier
        self.slots = slots or {}
        self.missing_slot = missing
        self.confidence, self.runner_up_id, self.runner_up = 1.0, None, 0.0
        self.rejected, self.normalized = False, ""


def slot(value, defaulted=False):
    return {"value": value, "text": value, "type": "text",
            "defaulted": defaulted}


class FakeTrace:
    def state(self, *a): pass
    def decided(self, *a): pass
    def event(self, *a): pass
    def interrupted(self, *a): pass


class FakeOutput:
    def __init__(self): self.said = []
    async def say(self, result):
        text = (result or {}).get("say", "")
        self.said.append(text)
        return text


class FakeCogiti:
    """Just enough of Cogiti for a Session."""

    def __init__(self, decisions, commands):
        self.decisions = decisions          # utterance -> FakeDecision
        self.table = table_mod.Table(commands)
        self.trace = FakeTrace()
        self.output = FakeOutput()
        self.ran = []
        self.escalated = []

    def resolve(self, text):
        return self.decisions.get(text)

    async def run_command(self, cmd, decision):
        args, _prov = cmd.bind(decision)
        self.ran.append((cmd.intent, args))
        return {"type": "result", "say": "did %s" % cmd.intent}


def command(intent, **spec):
    spec.setdefault("provider", "conversation.acknowledge")
    return table_mod.Command(intent, spec)


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        providers.load_all()

    def session(self, decisions, commands, escalation=None):
        c = FakeCogiti(decisions, commands)
        s = session_mod.Session(c)

        async def fake_escalate(cogiti, sess, turn):
            c.escalated.append(turn.text)
            return escalation or {"type": "result", "say": "escalated"}

        self._saved = session_mod.escalate.run
        session_mod.escalate.run = fake_escalate
        self.addCleanup(lambda: setattr(session_mod.escalate, "run", self._saved))
        return c, s


class TestRouting(Base):
    async def test_a_handled_intent_never_reaches_the_model(self):
        c, s = self.session({"hello": FakeDecision("greeting")},
                            {"greeting": command("greeting", speak="Hello.")})
        await s.utterance("hello")
        self.assertEqual([i for i, _ in c.ran], ["greeting"])
        self.assertEqual(c.escalated, [])

    async def test_an_intent_the_table_does_not_know_escalates(self):
        """Not a failure: a device that has not been taught that yet."""
        c, s = self.session({"x": FakeDecision("get_price")}, {})
        await s.utterance("x")
        self.assertEqual(c.escalated, ["x"])

    async def test_an_unresolved_utterance_escalates(self):
        c, s = self.session({"x": FakeDecision(None, verdict="escalate")}, {})
        await s.utterance("x")
        self.assertEqual(c.escalated, ["x"])

    async def test_no_resolver_at_all_escalates_everything(self):
        """ports.md: a resolver that always escalates is a valid deployment."""
        c, s = self.session({}, {})
        await s.utterance("anything")
        self.assertEqual(c.escalated, ["anything"])


class TestMissingSlot(Base):
    def volume(self, ask="What level?"):
        return {"set_volume": command(
            "set_volume", speak="Volume {level}.",
            args={"level": {"slot": "level", "required": True, "ask": ask}})}

    def decisions(self, answer_decision):
        return {"set the volume": FakeDecision("set_volume", verdict="escalate",
                                               missing="level"),
                "set the volume forty": answer_decision}

    async def answer(self, s, text, said):
        task = asyncio.ensure_future(s.utterance(text))
        for _ in range(200):
            if s.awaiting_answer():
                break
            await asyncio.sleep(0.005)
        await s.answer(said)
        return await task

    async def test_it_asks_and_acts_on_the_answer(self):
        c, s = self.session(
            self.decisions(FakeDecision("set_volume", slots={"level": slot("40")})),
            self.volume())
        await self.answer(s, "set the volume", "forty")
        self.assertEqual(c.ran, [("set_volume", {"level": "40"})])
        self.assertEqual(c.escalated, [], "asked, so never escalated")

    async def test_an_answer_that_changes_the_intent_is_refused(self):
        """The guard that matters. Measured against the real resolver: a bare
        'make it 20 minutes' answering a timer question comes back as
        `volume_down`. A follow-up must not change what is being done."""
        c, s = self.session(
            self.decisions(FakeDecision("volume_down")), self.volume())
        await self.answer(s, "set the volume", "forty")
        self.assertEqual(c.ran, [], "acted on a different intent")
        self.assertEqual(c.escalated, ["set the volume"])

    async def test_an_answer_that_still_leaves_the_slot_empty_escalates(self):
        c, s = self.session(
            self.decisions(FakeDecision("set_volume", missing="level")),
            self.volume())
        await self.answer(s, "set the volume", "forty")
        self.assertEqual(c.ran, [])
        self.assertEqual(c.escalated, ["set the volume"])

    async def test_a_table_with_no_wording_does_not_ask(self):
        """No `ask` means no asking — a gap someone closes with a sentence,
        not a question cogiti invents."""
        c, s = self.session(self.decisions(FakeDecision("set_volume")),
                            self.volume(ask=None))
        await s.utterance("set the volume")
        self.assertEqual(c.escalated, ["set the volume"])

    async def test_never_mind_always_leaves(self):
        c, s = self.session(self.decisions(FakeDecision("set_volume")),
                            self.volume())
        await self.answer(s, "set the volume", "never mind")
        self.assertEqual(c.ran, [])
        self.assertEqual(c.escalated, [])
        self.assertIn("Never mind", c.output.said[-1])


class TestConfirmRouting(Base):
    def power(self):
        return {"power_off": command("power_off", speak="Goodbye.",
                                     confirm="Shut down the device?")}

    async def test_yes_acts(self):
        c, s = self.session(
            {"power off": FakeDecision("power_off", verdict="confirm")},
            self.power())
        task = asyncio.ensure_future(s.utterance("power off"))
        for _ in range(200):
            if s.awaiting_answer():
                break
            await asyncio.sleep(0.005)
        await s.answer("yes")
        await task
        self.assertEqual([i for i, _ in c.ran], ["power_off"])

    async def test_anything_else_does_not(self):
        for said in ("no", "wait", "no, wait", "maybe"):
            c, s = self.session(
                {"power off": FakeDecision("power_off", verdict="confirm")},
                self.power())
            task = asyncio.ensure_future(s.utterance("power off"))
            for _ in range(200):
                if s.awaiting_answer():
                    break
                await asyncio.sleep(0.005)
            await s.answer(said)
            await task
            self.assertEqual(c.ran, [], "acted on %r" % said)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPresentationTemplates(unittest.TestCase):
    """Cards are data. `docs/command-table.md`: a new card is not a new build,
    and a template names objects and relationships but never a coordinate."""

    def tpl(self, **spec):
        from cogiti import presentation_templates as pt
        return pt.Template("card", spec)

    def err(self, **spec):
        from cogiti import presentation_templates as pt
        with self.assertRaises(pt.TemplateError) as e:
            pt.Template("card", spec)
        return str(e.exception)

    def test_a_group_renders_children_inline(self):
        op = self.tpl(kind="group", id="brain/weather", children=[
            {"kind": "text", "style": "headline", "text": "{temp_c}°"},
            {"kind": "text", "style": "caption", "text": "{condition}"},
        ]).ops({"temp_c": 21, "condition": "clear"})
        self.assertEqual(op["op"], "create")
        self.assertEqual(op["kind"], "group")
        self.assertEqual([c["text"] for c in op["children"]], ["21°", "clear"])

    def test_no_op_ever_carries_a_coordinate(self):
        """Layout belongs to the adapter, which is the only party that knows
        the screen."""
        op = self.tpl(kind="group", children=[{"kind": "text", "text": "x"}]).ops({})
        for forbidden in ("x", "y", "w", "h", "width", "height"):
            self.assertNotIn(forbidden, op)

    def test_an_id_outside_the_namespace_is_refused_at_load(self):
        """cogiti declares its namespace on connect and the adapter enforces
        ownership, so such an object could be created and never updated."""
        self.assertIn("namespace", self.err(id="weather/x", kind="text", text="x"))

    def test_the_pinned_region_is_not_available_to_a_command(self):
        """A command's output is conversational. Pinning belongs to a service,
        which is there to remove it again."""
        self.assertIn("region", self.err(kind="text", text="x",
                                         region="periphery"))

    def test_a_group_with_no_children_is_refused(self):
        self.assertIn("no children", self.err(kind="group"))

    def test_every_object_gets_a_fallback(self):
        """The port says an unknown kind still holds its place, which it can
        only do if it was given something to draw."""
        op = self.tpl(kind="group", children=[
            {"kind": "image", "src": "/tmp/x.png"},
            {"kind": "text", "text": "Sofia"}]).ops({})
        self.assertEqual(op["fallback"], "Sofia")

    def test_a_value_the_provider_did_not_return_reads_as_the_bug_it_is(self):
        """Rather than losing the answer that was already computed. This is
        how a clock card asking for {date_spoken} was caught."""
        op = self.tpl(kind="text", text="{nope}").ops({})
        self.assertEqual(op["text"], "{nope}")
