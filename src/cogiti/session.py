"""(speaker_id, thread) -> a Turn machine and a context.

`docs/architecture.md` §2. One session holds the conversation with one person
on one thread, runs one turn at a time, and keeps the little context an
escalation is given.

With no perception adapter there is nobody to identify, so `speaker_id` is a
constant. That is a deployment fact rather than a simplification: a device with
no camera and no voice print genuinely does not know who is speaking, and
inventing an identity would be worse than admitting there is one user.
"""

import asyncio
import sys

from . import detach
from . import jobs
from . import phrases as _phrases
from . import escalate
from .turn import State, Turn


def _single_word(text):
    """Exactly one word. Not "noise" — that takes a resolver to decide.

    Named for what it measures. The first version called this _is_noise and
    answered True for "hello", which is not noise at all: it is a greeting the
    resolver handles. The guard that uses this checks both, and a helper whose
    name claims more than it tests is how the wrong one gets reused later.

    Deliberately not a list of filler words. "mmm", "uh" and "hmm" are what
    this room produces; the next one produces different ones, and a list is a
    thing that is always slightly wrong somewhere else.
    """
    return len((text or "").strip().split()) == 1

#: Returned by _fill_slot when the person asked to drop it. A distinct
#: object rather than None, which already means "nothing was filled, escalate".
CANCELLED = object()

UNKNOWN_SPEAKER = "unknown"     # no perception adapter, so nobody is identified


#: Words that mean nothing unless something asked. Written out rather than
#: taken from `Turn.YES` and `Turn.NEGATIONS`, which was the first attempt and
#: was wrong: `stop` is in NEGATIONS *and* is an intent — the barge-in one,
#: where latency is the whole feature — so reusing that set would have made
#: the device ignore the one word it must never ignore. "Cancel", "wait" and
#: "hold" are in there for the same reason. A set that has to exclude the
#: interesting half of another set is a different set.
BARE_ANSWERS = frozenset(("yes", "yeah", "yep", "yup", "yes please", "sure",
                          "correct", "ok", "okay", "no", "nope", "nah"))


def bare_answer(text):
    """Is this only an answer — a word that means nothing on its own?

    Deliberately narrow. Anything longer is left alone: "no, the other one" is
    a sentence about something, and guessing at those is how a filter starts
    eating speech.
    """
    word = (text or "").strip().strip(".,!?;:").lower()
    return word in BARE_ANSWERS
#: Exchanges an escalation is given. Six while every one of them was
#: re-rendered as prose inside a single user message and paid for in full on
#: every turn. They are a real message array now and the stable half of the
#: request is cached, so depth costs almost nothing — and six was visibly too
#: few: it is about two minutes of talking, after which the device forgets a
#: name it was told.
HISTORY = 20

#: Intents that mean "I am talking to you" rather than merely being said near
#: the device. `greeting` is one of them and is not a compromise: saying hello
#: to something is addressing it, and the device answering "Hello." is both
#: the reply and the acknowledgement.
ADDRESSING = ("wake", "greeting")

#: How long after speaking an utterance may still be a follow-up. Matches
#: reflexi's own `[context] window_ms`, which is 45 seconds — the two are
#: answering the same question and should not disagree about it.
FOLLOW_UP_S = 45.0


def _debug():
    import os
    return bool(os.environ.get("COGITI_TURN_DEBUG"))

#: And the one that means "we are done". `stop` already meant stop talking;
#: to a person it always also meant stop listening, and now it does.
RELEASING = ("stop",)


class Session:
    def __init__(self, cogiti, speaker_id=UNKNOWN_SPEAKER, thread="main"):
        self.cogiti = cogiti
        self.key = (speaker_id, thread)
        self.history = []           # [{...}], most recent last
        self.current = None
        self._last_turn_ns = None
        self._attending_until = 0.0

    def remember(self, **entry):
        """Record one exchange for the next escalation to read.

        Every hole this closes was found in one live transcript. The device
        asked "Are you sure?", the person said "am i sure what?", and the
        model that answered them had no idea a question had been put: the
        confirm was never written down, and the turn that asked it was
        interrupted, which used to mean it was never written down either.

        So the four things that reach a person are the four things recorded:
        what they said and what was answered, a question the device put, the
        answer to it, and an answer delivered late with nobody asking. An
        exchange the person witnessed and the model cannot see is the whole
        of that failure, and it does not matter which of the four it was.
        """
        import time as _time
        self.history.append(entry)
        del self.history[:-HISTORY]
        self._last_turn_ns = _time.monotonic_ns()

    def on_state(self, turn, state):
        self.cogiti.trace.state(self, turn, state)
        # The face follows the turn, not the answer. An output that has no
        # opinion about states simply does not implement this.
        hook = getattr(self.cogiti.output, "on_state", None)
        if hook:
            hook(state.value)

    # ------------------------------------------------------------- input --

    async def utterance(self, text):
        """Always accepted. If a turn is running, it is interrupted first.

        The drain matters and is the reason this is not simply 'cancel and
        go': an interrupted turn may already have written part of an answer,
        and dispatching the next one before that settles mixes them.
        """
        # SPEAKING used to be excluded here, on the reasoning that a turn
        # that is already answering is nearly done. Barge-in is exactly the
        # case that reasoning misses: interrupting a device mid-sentence is
        # the whole point, and a turn that cannot be stopped while it talks
        # is one that talks over you.
        if self.current and self.current.state is not State.IDLE:
            old = self.current
            old.interrupt()
            try:
                await old._task
            except (asyncio.CancelledError, Exception):
                pass                       # it was interrupted; that is the point
            self.cogiti.trace.interrupted(self, old)

        # Show what was understood, whatever port it arrived on. This lived
        # in the speech path, so a typed utterance drew nothing — which made
        # the caption untestable by anything except a microphone, and was a
        # discourtesy to the text spine for no reason.
        show = getattr(self.cogiti.output, "heard", None)
        if show:
            show(text)

        turn = Turn(self, text)
        self.current = turn
        turn._task = asyncio.ensure_future(self._run(turn))
        return await turn._task

    #: States in which the next thing said is an answer, not a new utterance.
    #: Confirming belongs here for the reason the whole confirm path exists: a
    #: "no" answering "shut down the device?" must reach the turn that asked.
    #: Treated as a fresh utterance instead, it resolved to `mute` and the
    #: shutdown was cancelled only because the question timed out — which is
    #: the right outcome reached by luck rather than by design.
    AWAITING = (State.NEEDS_INPUT, State.CONFIRMING)

    def awaiting_answer(self):
        return self.current is not None and self.current.needs_answer()

    async def asked(self, turn, question):
        """Say the question the turn is waiting on.

        Twelve seconds is a long time to stand in front of something that has
        gone quiet, and the answer it wants is one word. Saying it is not a
        courtesy; it is the difference between a question and a hang.
        """
        say = getattr(self.cogiti.output, "say", None)
        if say is None:
            return
        self.remember(asked=question)
        self.cogiti.trace.exchange(self, turn, question=question)
        try:
            await say({"type": "result", "say": question, "show": question})
        except Exception:                                     # noqa: BLE001
            # A voice that cannot speak must not take the turn down with it —
            # the answer may still arrive by other means.
            pass

    async def answer(self, value):
        """The person answered a question that was put to them."""
        if self.awaiting_answer():
            self.remember(said=value, answering=self.current.question)
            self.cogiti.trace.exchange(self, self.current, answer=value)
            self.current.answer(value)
            return True
        return False


    # --------------------------------------------------------------- run --

    async def _run(self, turn):
        detached = False
        turn.to(State.RESOLVING)

        # The fast path. A resolver that always escalates is also valid and is
        # how cogiti runs with no fast path at all — so an absent one is not a
        # special case here, it is simply no decision.
        decision = self.cogiti.resolve(turn.text)
        turn.decision = decision
        if not self._addressed(decision):
            # Said in the room, not to the device. Nothing is answered and
            # nothing is spoken — a device that says "sorry, I didn't catch
            # that" over two people talking is itself the interruption it is
            # apologising for.
            #
            # **But not nothing at all.** Silence here is indistinguishable
            # from a broken device, and was: the same question went up on the
            # screen three times with the face motionless, until a greeting
            # opened the window and the fourth attempt answered. Nobody
            # watching that can tell "I am ignoring you on purpose" from "I am
            # not working", and the transcript appearing makes it worse by
            # proving it heard.
            #
            # So it shakes its head. That is a gesture rather than a sentence
            # for the reason above — speaking over the room is the
            # interruption this is avoiding — and it is `not_for_me` rather
            # than a shake here, because which motion says it belongs to the
            # face (architecture.md §2).
            # Every output answers this; TextOutput does nothing with it. It
            # was guarded by a bare `except Exception: pass`, and the guard
            # was hiding an AttributeError rather than a missing face — so
            # the shake never once fired on a real device.
            self.cogiti.output.not_for_me()
            self.cogiti.trace.decided(self, turn, decision)
            turn.to(State.IDLE)
            return None
        self.cogiti.trace.decided(self, turn, decision)
        result = await self._act(turn, decision)

        if (result is None and _single_word(turn.text)
                and getattr(self.cogiti, "resolver", None) is not None):
            # A single word that resolved to nothing is a room, not a request.
            #
            # Measured on a device in an ordinary hour: five of ten
            # escalations were one or two words — "mmm" four times, "else"
            # once — and they cost fifty-seven seconds and five model calls
            # between them. Nobody was talking to it.
            #
            # Safe because every single word that *means* something already
            # resolves: hello, stop, no, weather, time, louder, mute, thanks,
            # bitcoin. And "yes" or "cancel" only mean anything as answers,
            # which reach the turn that asked and never come through here.
            #
            # **Only when a resolver is configured**, and the existing tests
            # are what caught that: ports.md says a deployment with no
            # resolver escalates everything and is valid, and there the
            # premise above is false — nothing resolves, so every single word
            # would vanish silently and the device would look broken.
            #
            # Ignored rather than answered, and this is only tolerable because
            # the transcript is on screen: the person sees "mmm" appear and
            # nothing happen, which is feedback without the device talking
            # back at a noise.
            self.cogiti.trace.decided(self, turn, decision)
            turn.to(State.IDLE)
            return None

        if result is None:
            # services.md §5: before escalating, and only ever *after* the
            # resolver has had its say. A service born this afternoon must not
            # be able to take a sentence a built-in owns, so this is reached
            # only when the built-ins produced nothing at all — which is what
            # "built-ins always win a tie" means when there is no scoring.
            result = self.cogiti.answer_from_service(turn.text)

        if result is None:
            turn.to(State.THINKING)
            limit = jobs.LIMITS["concurrent_agent_jobs"]
            if not self.cogiti.pending.has_room(limit):
                # Accepted, not started, and said out loud. docs/jobs.md §5 is
                # emphatic: "I'll start that when the other one finishes" is an
                # answer; starting it silently in twenty minutes is not.
                #
                # Until escalations detached, this could not happen — only one
                # could exist at a time because the turn waited for it. The cap
                # has been in LIMITS since the module was written and was
                # unreachable until last night.
                return await self._queue(turn)

            # The holding line exists because nothing was happening for five
            # seconds. If the device has begun answering out loud, something
            # is happening, and cutting in with "I'll tell you when I have
            # it" over its own answer is the one thing worse than silence.
            result, running = await detach.with_deadline(
                escalate.run(self.cogiti, self, turn),
                answering=lambda: turn.spoke)
            if running is not None:
                # It is still working. The turn ends anyway — that is the
                # whole point of the stage — and the answer is delivered when
                # it arrives, by _delivers().
                result = self._detach(turn, running)
                detached = True

        if turn.interrupted:
            # It still happened. Cutting a turn short is a reason to say
            # nothing, not a reason to forget: the person spoke, the device
            # started on it, and a model told none of that reads the next
            # sentence as though it came out of nowhere.
            self.remember(said=turn.text, interrupted=True)
            return None

        turn.result = result
        turn.to(State.SPEAKING)
        if turn.spoke and isinstance(result, dict):
            # Said already, a sentence at a time while it was being written.
            # The screen still composes once from this; only the speaking is
            # suppressed, or the device repeats the whole answer.
            result = dict(result, already_spoken=True)
        said = await self.cogiti.output.say(result)
        self.cogiti.trace.spoke(self, turn, said)
        # A holding line is not an answer, and it must not be recorded as
        # one. "I'm still working on that" went into the history as the
        # device's reply, so the model read itself saying it and would
        # eventually have learnt to say it unprompted — a stall phrase is a
        # thing this device says *instead* of speaking, not a thing it said.
        # The real answer arrives later through _deliver_pending, naming the
        # question it belongs to.
        self.remember(said=turn.text,
                      answered=None if detached else said,
                      **({"pending": True} if detached else {}))
        turn.to(State.IDLE)

        # The end of a turn is the one safe moment to mention work that
        # finished while nobody was listening. Not a callback: a callback
        # fires into whichever turn happens to be running and the answer
        # lands in the wrong conversation.
        await self._deliver_pending()
        return result

    # ------------------------------------------------------------ heard --

    async def heard_start(self):
        """Someone began speaking. Barge-in, if we were the one talking.

        The adapter has already stopped its own audio — `speech-protocol.md`
        §5 — so what is left is the face and the turn. `ports.md` fixes that
        order and this is the second half of it.

        No transcript exists yet and may never: a cough, a door, a passing
        conversation. So this interrupts but does not start anything.
        """
        # **A turn that asked a question is not interrupted by the answer.**
        #
        # CONFIRMING and NEEDS_INPUT exist to wait for the person to speak, so
        # somebody speaking is the expected event and not an interruption.
        # Treating it as barge-in cancelled the question at the exact moment
        # it was being answered: "remove the clock" — "are you sure?" — "yes"
        # ended the turn, and the yes then arrived as a fresh utterance with
        # nothing to attach to and went to the model. Seen on a device, twice,
        # each time 2.8 seconds after the question — the length of the pause
        # before the answer.
        if self.awaiting_answer():
            return
        stop = getattr(self.cogiti.output, "barge_in", None)
        if stop:
            stop()
        if self.current and self.current.state is not State.IDLE:
            old = self.current
            old.interrupt()
            try:
                await old._task
            except (asyncio.CancelledError, Exception):
                pass
            self.cogiti.trace.interrupted(self, old)
            self.current = None

    async def _queue(self, turn):
        """Take the request, name what it is behind, and end the turn."""
        self.cogiti.pending.enqueue(self, turn.text)
        waiting = self.cogiti.pending.waiting_on()
        first = waiting[0] if waiting else "what I'm doing"
        result = {"type": "result", "linger": 0,
                  "say": "I'll get to that when I've finished %s." % first}
        turn.result = result
        turn.to(State.SPEAKING)
        said = await self.cogiti.output.say(result)
        self.remember(said=turn.text, answered=said)
        turn.to(State.IDLE)
        return None

    async def start_queued(self):
        """Start the next accepted request, if there is room for it now.

        Called when a detached job ends — done, failed or cancelled — because
        that is the only moment a slot appears.
        """
        limit = jobs.LIMITS["concurrent_agent_jobs"]
        while self.cogiti.pending.has_room(limit):
            q = self.cogiti.pending.next_queued()
            if q is None:
                return
            turn = Turn(q.session, q.text)
            # No user to ask: the turn that asked is over. A queued escalation
            # that stops for a question would be waiting on nobody, so it is
            # told there is nobody rather than left hanging.
            turn.ask = lambda _q: None
            task = asyncio.ensure_future(
                escalate.run(self.cogiti, q.session, turn))
            self._track(turn, task, q.title)

    def _detach(self, turn, task):
        """Stop waiting for an escalation, and arrange for its answer.

        The job row already exists — the agent adapter inserts one before it
        spawns anything — so this creates nothing. It records who is waiting,
        and hands the turn a sentence to say meanwhile.
        """
        d = self._track(turn, task, turn.text[:60])
        # The row stays open under the job's name, so what the model does
        # after the turn ends is still written down. Without it a detached
        # escalation showed no tools at all — it had called one and the trace
        # was flushed five seconds before it did.
        if d is not None:
            self.cogiti.trace.detached(self, turn, d.job_id)
        # `pending`, so the screen knows this is not the end of anything. The
        # face clears its thought stream when an answer lands, which is right
        # for an answer and exactly wrong for this one: the work carries on
        # for another minute, and the moment somebody most needs to see it is
        # the moment they have just been told to wait.
        return {"type": "result", "say": detach.STILL_WORKING, "linger": 0,
                "pending": True}

    def _track(self, turn, task, title):
        """Register a running job and arrange for whatever it produces."""
        run = getattr(turn, "agent_run", None)
        job_id = getattr(run, "job_id", None) or task.get_name()
        d = detach.Detached(job_id, title, task, self)
        self.cogiti.pending.add(d)

        def arrived(t):
            answer = None
            if t.cancelled():
                self.cogiti.pending.drop(d.job_id)
            elif t.exception() is not None:
                self.cogiti.pending.done(d.job_id, {
                    "type": "failed", "kind": "job",
                    "message": "that job failed: %s" % t.exception()})
            else:
                answer = t.result()
                if turn.spoke and isinstance(answer, dict):
                    # Streamed already, sentence by sentence. Delivering it
                    # again would have the device read the whole answer out a
                    # second time, having just finished saying it.
                    answer = dict(answer, already_spoken=True)
                self.cogiti.pending.done(d.job_id, answer)
            self.cogiti.trace.job_done(
                d.job_id, "cancelled" if t.cancelled() else "done",
                answered=(answer or {}).get("say"))
            # A slot just opened. This is the only moment one does.
            asyncio.ensure_future(self.start_queued())
            # And say it, if nobody is talking. Delivery used to happen only
            # at the end of a turn, which meant "I'll tell you when I have it"
            # was true exactly when the person spoke again — ask something
            # slow, then stay quiet, and the answer never came at all. A
            # promise with no mechanism behind it, like the thirty days.
            #
            # _deliver_pending returns early when a turn is running, so this
            # is safe from here: the answer then waits for that turn to end,
            # which is the behaviour that was already correct.
            asyncio.ensure_future(self._deliver_pending())

        task.add_done_callback(arrived)
        return d

    async def _deliver_pending(self):
        """Say what finished while the user was busy.

        Only when nothing else is happening. A device that speaks an old answer
        over a new question is worse than one that waits another minute.
        """
        if self.current is not None and self.current.state is not State.IDLE:
            return
        for d, result in self.cogiti.pending.take():
            if result is None:
                continue
            # Said out loud, and said in the log. An answer delivered outside
            # a turn appears in no trace, so without this the one path that
            # speaks without being asked is also the one path with no record
            # that it did.
            print("(delivering %s: %s)" % (d.job_id, d.title),
                  file=sys.stderr, flush=True)
            said = dict(result)
            # Name it. An answer arriving a minute later with no reference to
            # the question is an announcement out of nowhere.
            if said.get("type") != "failed" and said.get("say"):
                said["say"] = "About %s — %s" % (d.title, said["say"])
            await self.cogiti.output.say(said)
            self.remember(answered=said.get("say", ""), unprompted=True)

    async def heard_partial(self, text, stable):
        """The transcript so far.

        Resolved on every one of them — reflexi is a linked library at
        microseconds, which is the entire reason that is affordable — and
        recorded, but **not acted on**. `architecture.md` §3 allows a
        deterministic match to pre-warm on a partial: open the socket, start
        the fetch, produce no effect. Nothing here has anything to pre-warm
        yet, and inventing one would be inventing a use for a mechanism rather
        than the other way round.

        An unstable partial is not resolved at all. A recogniser that rewrites
        its own text as a window slides cannot support the promise cogiti makes
        about a pattern-tier match, and reading it anyway would break that
        promise quietly.
        """
        if not stable or not text:
            return None
        # Show it while it is still arriving. Only stable partials: an
        # unstable one rewrites as the window slides, and a caption that
        # rewrites itself reads as the device changing its mind rather than as
        # it listening.
        show = getattr(self.cogiti.output, "heard", None)
        if show:
            show(text)
        return self.cogiti.resolve(text)

    async def heard(self, text):
        """A final transcript. This is an utterance like any other — unless
        there is nothing in it.

        The final is shown too, not only the partials: a short utterance can
        produce no stable partial at all, and the caption would then never
        appear for exactly the sentences that are quickest to mishear.

        A recogniser returns an empty final for a sound that was loud enough
        to end the silence and had no words in it: a door, a chair, a cough.
        That is a normal answer and not an error, and it was starting a turn —
        which resolved to nothing, escalated, and spent thirteen seconds and a
        language model call establishing that the empty string means nothing.

        Only genuinely empty. Where the line falls for a *short* transcript is
        a judgement about this room and this microphone, and it belongs in the
        resolver's thresholds rather than hidden in a guard here.

        **And a pending question takes it.** The typed loop has always done
        this; the microphone never did, so a confirm could not be answered by
        voice at all. "Pin bitcoin price on screen" — "Keep it on the screen
        from now on?" — "yes, please": the yes arrived here, started a fresh
        turn, interrupted the turn that was waiting for it, and went to the
        model, which was asked out of nowhere to react to somebody agreeing
        to nothing.

        `heard_start` already refuses to barge in on a question, which is the
        same fix one event earlier and was landed alone. It stops the person
        *beginning* to speak from cancelling the question and does nothing
        about what they then say.
        """
        if not (text or "").strip():
            return None
        if self.awaiting_answer():
            await self.answer(text)
            return None
        if bare_answer(text):
            # A yes with nothing to agree to. Heard on the device: two
            # escalations were already detached, so the queue took it and
            # said "I'll get to that when I've finished the bitcoin price" —
            # a slot spent, and a sentence offered, in reply to a word that
            # requested nothing.
            #
            # The same reasoning as the empty final above, one step along. A
            # cough is not a turn because there are no words in it; "yes" on
            # its own is not a turn because it only means anything against a
            # question, and there is not one. Silence is what a person gets
            # for agreeing with nobody.
            return None
        return await self.utterance(text)

    # ------------------------------------------------------------ acting --

    async def _act(self, turn, decision):
        """Handle or confirm a resolved intent. `None` means escalate.

        Every route out of here that returns None is a deliberate one, and
        they are the interesting part:

        - no decision, or `escalate`: the resolver could not do it;
        - an intent the table has no entry for: a device that has not been
          taught that yet, which is what the model is for;
        - a missing required slot: reflexi hands back the intent *and* the
          slot it lacks, so this is the one escalation that arrives knowing
          what it wants. Asking "a timer for how long?" belongs here and is
          not built yet, so for now it escalates — with the intent recorded,
          which is the difference between a gap and a bug.
        """
        if decision is None:
            return None
        table = self.cogiti.table

        # reflexi recognised the intent but a required slot was empty, and it
        # hands back both. That is the one escalation that arrives knowing what
        # it wants — so ask for it rather than paying a model to.
        if decision.missing_slot and decision.intent_id and table:
            filled = await self._fill_slot(turn, decision)
            if filled is CANCELLED:
                return {"type": "result", "say": "Never mind, then.",
                        "did": ["asked, and was told to drop it"]}
            if filled is not None:
                decision = filled
                turn.decision = decision

        if decision.verdict == "escalate":
            return None
        cmd = table.get(decision.intent_id) if table else None
        if cmd is None:
            return None

        if decision.verdict == "confirm" and _debug():
            import time as _t
            gap = ((_t.monotonic_ns() - self._last_turn_ns) / 1e9
                   if self._last_turn_ns else -1.0)
            print("(confirm %s: mid=%s gap=%.1fs pending=%d)"
                  % (decision.intent_id, self.mid_conversation(), gap,
                     len(getattr(self.cogiti.pending, "running", {}) or {})),
                  file=sys.stderr, flush=True)
        if (decision.verdict == "confirm" and self.mid_conversation()
                and getattr(decision, "tier", None) != "pattern"):
            # **Unsure, mid-conversation: ask the party that has the
            # conversation.**
            #
            # reflexi resolves each utterance alone. It has a context buffer
            # for follow-ups, and thresholds.toml caps those at four tokens —
            # "and tomorrow?" inherits, a whole sentence does not, and that
            # limit is right: a full utterance that merely happens to follow
            # another is its own.
            #
            # So a long follow-up is matched standalone and can drift.
            # Measured: "show me the results with example pictures", said
            # straight after a product recommendation, scored 0.539 against
            # `list_services` — inside the confirm band — and the device
            # asked about services and then cancelled.
            #
            # The band is justified as "a confirm is a much cheaper mistake
            # than a wrong action", and that is true of a *standalone*
            # command. Mid-conversation it is not the choice on offer: the
            # model can reach every one of these through the device tool, and
            # anything carrying a `confirm` still asks, in the table's own
            # words. The question is not skipped — it is asked by whoever can
            # see what the conversation is about.
            #
            # **Except a pattern-tier match, which is not drift.**
            #
            # The measured case above scored 0.539 on the `similar` tier —
            # a sentence that wandered into the confirm band. A pattern match
            # is the deterministic pre-matcher: an exact phrase the device was
            # taught, at 1.00, with no score involved. Handing that to the
            # model costs the thing this whole path exists to protect.
            #
            # It cost it on a device: "software update" resolved `update`,
            # confirm, pattern, 1.00 — and was escalated anyway. The model
            # asked "Shall I install the available updates?", the turn ended,
            # and the answer arrived in the next utterance with nothing left
            # waiting for it. The device replied "I asked, and you didn't say
            # yes, so I held off." to somebody who had said yes four times.
            #
            # Asked on the fast path instead, the turn stays alive and
            # `heard` routes the next utterance to `answer` before anything
            # else looks at it — which is what makes a question a question
            # rather than a sentence the device happened to say.
            return None

        if decision.verdict == "confirm":
            # The resolver decided this needs asking; the table only supplies
            # the wording. cogiti never auto-answers one and never lets an
            # agent answer one.
            question = cmd.confirm or "Are you sure?"
            if not await turn.confirm(question):
                # Cancelled, or timed out, or anything that was not an explicit
                # yes. Not an error, and not escalated: the user was asked and
                # the answer was no.
                return {"type": "result", "say": "Cancelled.",
                        "did": ["asked, and did not do it"]}

        turn.to(State.ACTING)
        if cmd.job:
            # Started, not awaited. The whole point of a job is that the turn
            # ends and it keeps going.
            # The turn goes with it: a job that has to ask something — the
            # review gate is one — needs the turn that is asking, and every
            # other kind ignores it.
            return await self.cogiti.start_job(cmd, decision,
                                               "%s/%s" % self.key, turn=turn)
        return await self.cogiti.run_command(cmd, decision)

    async def _fill_slot(self, turn, decision):
        """Ask for the one slot that was missing, and resolve again.

        The answer is **not** resolved on its own. Measured against reflexi:
        a bare "make it 20 minutes" answering "a timer for how long?" resolves
        to `volume_down` with a confirm verdict, and "ten" and "for 20 minutes"
        resolve to nothing at all. Appended to what was originally said, all
        three come back as `set_timer` with the right duration, because the
        resolver is then reading a sentence rather than a fragment.

        Two guards, and the first is the one that matters:

        **The new decision is only accepted if it is the same intent.** A
        follow-up must not be able to change what is being done — that is how
        an answer about a timer turns into a volume change, and one day into
        something worse.

        **The slot must actually be filled**, or nothing was gained and it
        escalates as it would have anyway.
        """
        cmd = self.cogiti.table.get(decision.intent_id)
        if cmd is None:
            return None
        question = cmd.ask_for(decision.missing_slot)
        if not question:
            return None

        said = await turn.ask_slot(question)
        if said is None:
            return CANCELLED

        again = self.cogiti.resolve("%s %s" % (turn.text, said))
        if (again is not None
                and again.intent_id == decision.intent_id
                and not again.missing_slot):
            return again
        return None

    # ----------------------------------------------------------- context --

    # ------------------------------------------------------ attention --

    def attention_s(self):
        """Zero — always listening — for anything that cannot say otherwise.

        A missing or unreadable setting must not be able to make the device
        deaf. Every way this can fail fails towards hearing you.
        """
        cfg = getattr(self.cogiti, "config", None)
        if cfg is None:
            return 0.0
        try:
            return float(cfg["attention_s"])
        except (KeyError, TypeError, ValueError):
            return 0.0

    def attending(self):
        import time as _time
        return _time.monotonic() < self._attending_until

    def attend(self):
        import time as _time
        self._attending_until = _time.monotonic() + self.attention_s()

    def release(self):
        self._attending_until = 0.0

    def mid_conversation(self):
        """Is something still going on that an utterance could be part of?

        The same window reflexi uses for its own follow-ups, so the two
        agree about when a conversation is live rather than each having an
        opinion.
        """
        import time as _time
        # **What is still on the screen counts, however long ago it went up.**
        #
        # Three products are up, somebody reads them for a minute and asks
        # "tell me more about the first one" — measured at 58 seconds, past
        # the window, so it was treated as a fresh command, matched
        # `repeat` at 0.58 and answered "Are you sure?".
        #
        # Silence in front of a screen full of what the device just offered
        # is not a lapsed conversation, it is somebody reading. The clock is
        # the wrong instrument for that: what makes this a follow-up is that
        # the thing being asked about is still there.
        try:
            p = getattr(self.cogiti.output, "p", None)
            if p is not None and p.on_screen():
                return True
        except Exception:                                     # noqa: BLE001
            pass

        # **Owing somebody an answer counts.** The clock below starts when a
        # turn *ends*, and a detached escalation ends five seconds in while
        # the answer takes another minute — so the window expired on somebody
        # who was standing there waiting, which is the most conversational
        # state there is. Seen in a replay: a follow-up seventy seconds after
        # the holding line and before the answer, treated as a fresh command.
        if getattr(getattr(self.cogiti, "pending", None), "running", None):
            return True
        if not self._last_turn_ns:
            return False
        gap = (_time.monotonic_ns() - self._last_turn_ns) / 1e9
        return gap <= FOLLOW_UP_S

    def _addressed(self, decision):
        """Is this being said to the device, or merely near it?

        A window rather than a wake word on every sentence: nobody says "hey
        computer" before each clause, and a device that demands it is one
        people stop talking to. Address it once and it stays with you; say
        `stop`, or leave it alone for a minute, and it stops listening.

        Off when `attention_s` is 0, which is the behaviour that existed
        before this and is still right for a close-talk microphone. The escape
        hatch matters more than the feature: a wake word that mishears leaves
        a device that is simply deaf, and nothing on its face says why.
        """
        if self.attention_s() <= 0:
            return True
        if getattr(self.cogiti, "resolver", None) is None:
            # Nothing can recognise a greeting or the device's name, so
            # nothing could ever address it and it would be deaf for good.
            # `ports.md` allows a deployment with no resolver — it escalates
            # everything — and this must not quietly turn that into a brick.
            return True
        intent = getattr(decision, "intent_id", None)
        if intent in RELEASING:
            # Still handled — `stop` has work to do — and then the window
            # closes behind it.
            self.release()
            return True
        if intent in ADDRESSING:
            self.attend()
            return True
        if self.attending():
            # Every answered turn extends it. A conversation is not a series
            # of separately addressed requests.
            self.attend()
            return True

        # **Not addressed, but unmistakable.** A `handle` is a phrase the
        # device was taught and recognised past its own confidence threshold,
        # and nobody says "what time is it" to another person and expects
        # nothing to happen. Requiring a greeting before every cold request is
        # the bargain a smart speaker makes and it is a poor one: the common
        # case becomes two sentences.
        #
        # This is also where the cost actually is. Ambient speech does not
        # resolve — "you don't win it now" and "personal cost in 8 terabytes
        # effectively" reach no intent at all — so gating escalation gates
        # every model call, which is the thing that was being spent on other
        # people's conversations.
        #
        # `handle`, or a `confirm` the pre-matcher is certain of.
        #
        # A confirm on a *score* is still refused here: that would have the
        # device asking a question of a room that was not talking to it, and
        # the score is exactly what cannot tell the two apart. A pattern-tier
        # match is not a score. It is an exact phrase the device was taught,
        # and nobody says "software update" or "power off" to another person
        # and expects nothing to happen.
        #
        # Refusing it cost a real conversation: "software update" resolved
        # `update`, confirm, pattern, 1.00 — and the face shook its head,
        # because a confirm could not get through this gate at all. The
        # answer is the same as for `handle`, for the same reason: the tier
        # says how it matched, the verdict says what to do about it, and only
        # one of those belongs in a policy about whether it was addressed.
        #
        # It still only ever *asks*. Nothing here can act without an explicit
        # yes, and cogiti never answers one on anybody's behalf.
        #
        # **Not the tier.** This tested `tier == "pattern"` as well, which
        # sounds like the same sentence and is not. The tier says *how* the
        # blob matched, not how sure it is — an exemplar hit literally, or the
        # same exemplar reached through the normaliser. On a real device that
        # produced a split nobody could have predicted from the outside: "what
        # time is it" answered and "what's your ip" did not, both `get_ip` and
        # `get_time` at confidence 1.00 and both `handle`. The person sees the
        # transcript go up and the device sit there, with nothing on its face
        # to say why. The verdict is reflexi's judgement about certainty and
        # is the whole of what belongs here; the tier is an implementation
        # detail of the matcher leaking through a policy.
        verdict = getattr(decision, "verdict", None)
        if verdict == "handle" or (verdict == "confirm"
                                   and getattr(decision, "tier", None) == "pattern"):
            self.attend()
            return True
        return False

    def context(self):
        """What an escalation is told beyond the utterance itself.

        `prompt.context` was left undefined in the agent protocol so its shape
        could come from real prompts rather than a guess. It is two things: the
        exchanges, and where the device is standing while it has them.
        """
        return {"recent": list(self.history), "situation": self.situation()}

    def situation(self):
        """Where the device is, when it is, and what is in front of it.

        The model knew none of this. It could *fetch* the time by spending a
        tool call, so "is it getting late?" cost a round trip to learn
        something the device has always known — and questions like "is that
        still up?" had nothing to refer to at all.

        Everything here is cheap and local: a clock, a hostname, and a list
        the supervisor already holds. Nothing that needs the network belongs
        in a block assembled on every escalation.
        """
        import time as _time
        out = {"time": _time.strftime("%H:%M"),
               "date": _time.strftime("%A %d %B %Y")}
        # Who is talking. `unknown` until a perception adapter says otherwise,
        # and said plainly rather than left out: a model that is not told it
        # does not know who this is will happily assume.
        out["speaker"] = self.key[0]
        try:
            from . import readings
            name = readings.read("hostname")
            if name:
                out["device"] = name
        except Exception:                                     # noqa: BLE001
            pass
        try:
            live = self.cogiti._svc()
            out["pinned"] = [s.m.title for s in live]
        except Exception:                                     # noqa: BLE001
            pass
        try:
            p = getattr(self.cogiti.output, "p", None)
            seen = p.on_screen() if p is not None else None
            if seen:
                out["on_screen"] = seen
        except Exception:                                     # noqa: BLE001
            pass
        if self._last_turn_ns:
            gap = (_time.monotonic_ns() - self._last_turn_ns) // 1_000_000_000
            # A gap is the difference between a follow-up and a fresh start,
            # and "and at sunset?" means nothing without it.
            out["since_last_s"] = int(gap)
        return out
