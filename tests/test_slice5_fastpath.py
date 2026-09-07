"""Resolve, act, confirm, and ask for a missing slot.

Against fakes rather than the real resolver: what is under test is the routing
cogiti does with a decision, not reflexi's decisions. The real resolver has its
own suite next door and `test_slice4_resolver.py` checks the binding between.
"""

import asyncio, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import providers, session as session_mod, table as table_mod  # noqa: E402
from cogiti import detach
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
    def exchange(self, *a, **k): pass
    def spoke(self, *a, **k): pass


class FakeOutput:
    def __init__(self): self.said = []
    async def say(self, result):
        text = (result or {}).get("say", "")
        self.said.append(text)
        return text


class FakeCogiti:
    """Just enough of Cogiti for a Session."""

    def __init__(self, decisions, commands):
        self.pending = detach.Pending()
        self.decisions = decisions          # utterance -> FakeDecision
        self.table = table_mod.Table(commands)
        self.trace = FakeTrace()
        self.output = FakeOutput()
        self.ran = []
        self.escalated = []

    def resolve(self, text):
        return self.decisions.get(text)

    def answer_from_service(self, _text):
        """Nothing is installed in these tests, which is the ordinary case:
        no service claims any sentence."""
        return None

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


class TestBeingAddressed(Base):
    """Said to the device, or merely near it.

    In one evening it took "personal cost in 8 terabytes effectively", "will
    this is the kuni materiality?" and "you don't win it now" as things said
    to it, and answered the last with an eleven-second call to a language
    model. Every one of them was said to somebody else in the room.
    """

    def listening(self, decisions, commands=None, attention_s="60"):
        c, s = self.session(decisions, commands or {})
        c.resolver = object()
        c.config = {"attention_s": attention_s}
        return c, s

    async def test_it_ignores_what_was_not_said_to_it(self):
        c, s = self.listening({"you don't win it now": None})
        await s.utterance("you don't win it now")
        self.assertEqual(c.escalated, [], "it answered somebody else")
        self.assertEqual(c.output.said, [])

    async def test_a_greeting_opens_the_window(self):
        """Saying hello to something is addressing it, and the reply is both
        the answer and the acknowledgement — so `greeting` is not a
        compromise here, it is the natural way in."""
        c, s = self.listening(
            {"hello": FakeDecision("greeting"),
             "what time is it": FakeDecision("get_time")},
            {"greeting": command("greeting", speak="Hello."),
             "get_time": command("get_time", speak="It is late.")})
        self.assertFalse(s.attending())
        await s.utterance("hello")
        self.assertTrue(s.attending(), "a greeting did not open it")
        await s.utterance("what time is it")
        self.assertIn("get_time", [i for i, _ in c.ran])

    async def test_an_exact_command_needs_no_greeting(self):
        """Nobody says "what time is it" to another person and expects
        nothing to happen, and requiring a greeting before every cold
        request makes the common case two sentences."""
        c, s = self.listening(
            {"what time is it": FakeDecision("get_time", tier="pattern")},
            {"get_time": command("get_time", speak="It is late.")})
        await s.utterance("what time is it")
        self.assertIn("get_time", [i for i, _ in c.ran])
        self.assertTrue(s.attending(), "answering it did not open the window")

    async def test_a_near_miss_still_needs_addressing(self):
        """What a room's conversation actually does is fail to resolve. It
        reaches no intent, so no verdict of `handle`, and nothing runs."""
        c, s = self.listening(
            {"i want that clock gone": FakeDecision("remove_service",
                                                    tier="similar",
                                                    verdict="escalate")},
            {"remove_service": command("remove_service", speak="Gone.")})
        await s.utterance("i want that clock gone")
        self.assertEqual(c.ran, [], "a near miss acted unaddressed")

    async def test_the_tier_is_not_the_gate(self):
        """Measured on the device: "what's your ip" is `get_ip`, confidence
        1.00, verdict `handle` — and tier `similar`, because the normaliser
        folded "what's" before the exemplar matched. "what time is it" is
        `pattern`. Gating on the tier answered one and silently dropped the
        other, which from outside is a device that works for some questions
        and ignores others for no visible reason."""
        c, s = self.listening(
            {"what's your ip": FakeDecision("get_ip", tier="similar")},
            {"get_ip": command("get_ip", speak="192.168.1.174.")})
        await s.utterance("what's your ip")
        self.assertIn("get_ip", [i for i, _ in c.ran],
                      "a confident handle was dropped for its tier")
        self.assertTrue(s.attending())

    async def test_a_confirm_never_gets_through_unaddressed(self):
        """It would have the device asking a question of a room that was not
        talking to it."""
        c, s = self.listening(
            {"power off": FakeDecision("power_off", tier="pattern",
                                       verdict="confirm")},
            {"power_off": command("power_off", speak="Bye.")})
        await s.utterance("power off")
        self.assertEqual(c.ran, [])

    async def test_its_own_name_opens_it_too(self):
        c, s = self.listening({"inteliboy": FakeDecision("wake")})
        await s.utterance("inteliboy")
        self.assertTrue(s.attending())

    async def test_stop_closes_it(self):
        """`stop` already meant stop talking. To a person it always also
        meant stop listening, and now it does."""
        c, s = self.listening({"inteliboy": FakeDecision("wake"),
                               "stop": FakeDecision("stop")})
        await s.utterance("inteliboy")
        await s.utterance("stop")
        self.assertFalse(s.attending(), "stop left it listening")

    async def test_every_answered_turn_extends_it(self):
        """Nobody says "hey computer" before each clause, and a device that
        demands it is one people stop talking to."""
        c, s = self.listening(
            {"inteliboy": FakeDecision("wake"),
             "what time is it": FakeDecision("get_time")},
            {"get_time": command("get_time", speak="It is late.")})
        await s.utterance("inteliboy")
        s._attending_until -= 55                  # nearly expired
        await s.utterance("what time is it")
        self.assertTrue(s.attending(), "answering did not extend it")

    async def test_zero_is_always_listening(self):
        """cogiti's default: being addressed is a property of a room, and
        cogiti is general. InteliBoy sets 60 in its own config."""
        c, s = self.listening({"what time is it": FakeDecision("get_time")},
                              {"get_time": command("get_time",
                                                   speak="It is late.")},
                              attention_s="0")
        await s.utterance("what time is it")
        self.assertIn("get_time", [i for i, _ in c.ran])

    async def test_with_no_resolver_it_still_hears_everything(self):
        """Nothing could recognise a greeting or the device's name, so
        nothing could ever address it. ports.md allows a deployment with no
        resolver — it escalates everything — and this must not quietly turn
        that into a brick."""
        c, s = self.session({"anything at all": None}, {})
        c.config = {"attention_s": "60"}          # and no resolver
        await s.utterance("anything at all")
        self.assertEqual(c.escalated, ["anything at all"])

    async def test_an_unreadable_setting_fails_towards_hearing_you(self):
        c, s = self.listening({}, attention_s="not a number")
        self.assertEqual(s.attention_s(), 0.0)


class TestAWeakMatchMidConversation(Base):
    """reflexi resolves each utterance alone. Its context buffer handles
    follow-ups of four tokens or fewer — "and tomorrow?" inherits — and a
    longer one is matched standalone, where it can drift.

    Measured: "show me the results with example pictures", said straight
    after a product recommendation, scored 0.539 against `list_services`,
    inside the confirm band. The device asked about services and cancelled.
    """

    def weak(self, intent="list_services"):
        return FakeDecision(intent, verdict="confirm", tier="similar")

    async def test_it_escalates_instead_of_asking(self):
        c, s = self.session({"show me the results": self.weak()},
                            {"list_services": command("list_services")})
        s._last_turn_ns = __import__("time").monotonic_ns()   # just spoke
        await s.utterance("show me the results")
        self.assertEqual(c.escalated, ["show me the results"])
        self.assertEqual(c.ran, [], "it acted on a 54% guess")

    async def test_out_of_the_window_it_still_asks(self):
        """Nothing is going on, so the resolver's guess is the best thing
        anybody has and a question is the cheap way to check it."""
        import time as _t
        c, s = self.session({"show me the results": self.weak()},
                            {"list_services": command("list_services")})
        s._last_turn_ns = _t.monotonic_ns() - int(120e9)      # two minutes ago
        await s.utterance("show me the results")
        self.assertEqual(c.escalated, [], "it escalated a standalone command")

    async def test_waiting_for_an_answer_counts_as_talking(self):
        """The window starts when a turn ends, and a detached escalation ends
        five seconds in while its answer takes another minute. So it expired
        on somebody standing there waiting — which is the most conversational
        state there is."""
        import time as _t
        c, s = self.session({"show me the results": self.weak()},
                            {"list_services": command("list_services")})
        s._last_turn_ns = _t.monotonic_ns() - int(120e9)      # long ago
        c.pending.add(detach.Detached("j1", "the usb sticks", None, s))
        await s.utterance("show me the results")
        self.assertEqual(c.escalated, ["show me the results"])

    async def test_what_is_on_screen_keeps_the_conversation_open(self):
        """Three products up, somebody reads them for a minute and asks
        "tell me more about the first one" — measured at 58 seconds, past
        the window, matched `repeat` at 0.58 and answered "Are you sure?".
        Silence in front of what the device just offered is somebody
        reading, not a conversation that ended."""
        import time as _t

        class Screen:
            def on_screen(self):
                return ["Sketch Pad", "Crystal Kit", "Soccer Ball"]

        c, s = self.session({"tell me more about the first one":
                             self.weak("repeat")},
                            {"repeat": command("repeat")})
        c.output.p = Screen()
        s._last_turn_ns = _t.monotonic_ns() - int(120e9)      # two minutes ago
        await s.utterance("tell me more about the first one")
        self.assertEqual(c.escalated, ["tell me more about the first one"])
        self.assertEqual(c.ran, [], "it acted on a 58% guess")

    async def test_an_empty_screen_does_not(self):
        """Nothing up and nothing said for two minutes is a fresh command,
        and a question is the cheap way to check it."""
        import time as _t

        class Blank:
            def on_screen(self):
                return []

        c, s = self.session({"show me the results": self.weak()},
                            {"list_services": command("list_services")})
        c.output.p = Blank()
        s._last_turn_ns = _t.monotonic_ns() - int(120e9)
        await s.utterance("show me the results")
        self.assertEqual(c.escalated, [])

    async def test_a_confident_match_is_untouched(self):
        """Only the unsure band moves. `handle` still acts, immediately,
        which is the whole reason the fast path exists."""
        c, s = self.session({"what is pinned": FakeDecision("list_services")},
                            {"list_services": command("list_services")})
        s._last_turn_ns = __import__("time").monotonic_ns()
        await s.utterance("what is pinned")
        self.assertEqual([i for i, _ in c.ran], ["list_services"])


class TestARoomIsNotARequest(Base):
    """A single word the resolver made nothing of is not worth a model call.

    Measured on a device in an ordinary hour: five of ten escalations were one
    or two words — "mmm" four times, "else" once — costing fifty-seven seconds
    and five model calls between them. Nobody was talking to it.
    """

    def with_resolver(self, decisions, commands=None):
        c, s = self.session(decisions, commands or {})
        c.resolver = object()      # configured; what it is does not matter
        return c, s

    async def test_a_lone_unresolved_word_is_not_escalated(self):
        c, s = self.with_resolver({})
        await s.utterance("mmm")
        self.assertEqual(c.escalated, [],
                         "a noise in the room reached the model")

    async def test_two_words_still_escalate(self):
        """Two words can be a question — "bitcoin price" is one — and the
        cheapness of this guard is that it only ever drops a single word."""
        c, s = self.with_resolver({})
        await s.utterance("daily floor")
        self.assertEqual(c.escalated, ["daily floor"])

    async def test_a_lone_word_that_resolves_is_untouched(self):
        """Every single word that means anything already resolves: hello,
        stop, no, weather, time, louder, mute, thanks, bitcoin."""
        c, s = self.with_resolver({"hello": FakeDecision("greeting")},
                                  {"greeting": command("greeting",
                                                       speak="Hello.")})
        await s.utterance("hello")
        self.assertEqual(c.escalated, [])
        self.assertEqual([i for i, _ in c.ran], ["greeting"])

    async def test_with_no_resolver_everything_escalates(self):
        """ports.md: a deployment with no resolver escalates everything and is
        valid. There this guard's premise is false — nothing resolves — so
        every single word would vanish and the device would look broken. The
        existing suite caught exactly this, which is why the guard asks."""
        c, s = self.session({}, {})           # no resolver attribute at all
        await s.utterance("anything")
        self.assertEqual(c.escalated, ["anything"])


class TestTheDeviceIsOfferedToTheModel(unittest.TestCase):
    """An escalation could only talk about the device. "Turn it up a bit,
    would you" in a sentence the resolver does not match got an agreeable
    answer and no change in volume."""

    def offers(self, spec):
        from cogiti import device_tool
        t = table_mod.Table({k: table_mod.Command(k, v)
                             for k, v in spec.items()})
        return device_tool.offered(t)

    def test_a_reading_is_offered(self):
        offers = self.offers({"get_time": {"provider": "clock.now"}})
        self.assertIn("get_time", offers)

    def test_everything_in_the_table_is_offered(self):
        """No hole in the list. Whatever a person could get by saying the
        right sentence, an escalation can get by calling this — because an
        escalation happens exactly when they did not find that sentence."""
        offers = self.offers({
            "get_time": {"provider": "clock.now"},
            "greeting": {"provider": "conversation.acknowledge"},
            "list_services": {"job": "list_services"},
            "power_off": {"provider": "shell.run", "command": ["true"],
                          "confirm": "Shut down?"},
        })
        self.assertEqual(sorted(offers),
                         ["get_time", "greeting", "list_services",
                          "power_off"])

    def test_the_ones_that_ask_are_named_with_their_wording(self):
        """So the model knows a refusal is an ordinary outcome, and does not
        promise the thing is done before anybody has been asked."""
        from cogiti import device_tool
        t = table_mod.Table({k: table_mod.Command(k, v) for k, v in {
            "get_time": {"provider": "clock.now"},
            "remove_service": {"job": "remove_service",
                               "confirm": "Delete it for good?"},
        }.items()})
        self.assertEqual(device_tool.asks(t),
                         {"remove_service": "Delete it for good?"})
        d = device_tool.tool(device_tool.offered(t),
                             device_tool.asks(t))["description"]
        self.assertIn('remove_service ("Delete it for good?")', d)


    def test_a_required_slot_is_advertised(self):
        offers = self.offers({"get_price": {
            "provider": "price.spot",
            "args": {"symbol": {"slot": "symbol", "required": True}}}})
        self.assertEqual(offers["get_price"], "symbol")

    def test_a_model_calling_a_confirm_asks_the_person(self):
        """The whole of the safety argument, now that nothing is withheld.

        The model proposes and cogiti decides — and deciding here means
        asking whoever is standing in front of it, in the words the table
        gives, through the turn they are already in.
        """
        import asyncio
        from cogiti import device_tool
        asked, ran = [], []

        class Turn:
            def can_ask(self):
                return True
            async def confirm(self, q):
                asked.append(q)
                return False           # they said no

        class Brain:
            table = table_mod.Table({"remove_service": table_mod.Command(
                "remove_service", {"job": "remove_service",
                                   "confirm": "Delete it for good?"})})
            async def start_job(self, *a, **k):
                ran.append(a)
                return {"type": "result", "say": "gone"}

        out = asyncio.run(device_tool.run(
            Brain(), device_tool.offered(Brain.table),
            {"command": "remove_service"}, "s1", Turn()))
        self.assertEqual(asked, ["Delete it for good?"])
        self.assertEqual(ran, [], "it ran anyway after being refused")
        self.assertTrue(out.get("refused"))
        self.assertFalse(out["ok"])

    def test_it_will_not_perform_a_confirm_with_nobody_to_ask(self):
        """A detached or interrupted turn has nobody waiting on it. Doing it
        anyway performs the confirm on somebody's behalf, which is the exact
        thing the wording exists to prevent."""
        import asyncio
        from cogiti import device_tool
        ran = []

        class Gone:
            def can_ask(self):
                return False

        class Brain:
            table = table_mod.Table({"power_off": table_mod.Command(
                "power_off", {"provider": "shell.run", "command": ["true"],
                              "confirm": "Shut down?"})})
            async def run_command(self, *a):
                ran.append(a)
                return {"type": "result", "say": "bye"}

        out = asyncio.run(device_tool.run(
            Brain(), device_tool.offered(Brain.table),
            {"command": "power_off"}, "s1", Gone()))
        self.assertEqual(ran, [])
        self.assertFalse(out["ok"])
        self.assertFalse(out["asked"])

    def test_the_declaration_lists_what_each_needs(self):
        from cogiti import device_tool
        t = device_tool.tool({"get_time": None, "get_price": "symbol"})
        self.assertIn("get_price (needs symbol)", t["description"])
        self.assertEqual(sorted(t["input_schema"]["properties"]["command"]
                                ["enum"]), ["get_price", "get_time"])
