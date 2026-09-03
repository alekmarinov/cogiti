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

from . import detach
from . import escalate
from .turn import State, Turn

#: Returned by _fill_slot when the person asked to drop it. A distinct
#: object rather than None, which already means "nothing was filled, escalate".
CANCELLED = object()

UNKNOWN_SPEAKER = "unknown"     # no perception adapter, so nobody is identified
HISTORY = 6                     # turns of context an escalation is given


class Session:
    def __init__(self, cogiti, speaker_id=UNKNOWN_SPEAKER, thread="main"):
        self.cogiti = cogiti
        self.key = (speaker_id, thread)
        self.history = []           # [(text, said)], most recent last
        self.current = None

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
        try:
            await say({"type": "result", "say": question, "show": question})
        except Exception:                                     # noqa: BLE001
            # A voice that cannot speak must not take the turn down with it —
            # the answer may still arrive by other means.
            pass

    async def answer(self, value):
        """The person answered a question that was put to them."""
        if self.awaiting_answer():
            self.current.answer(value)
            return True
        return False


    # --------------------------------------------------------------- run --

    async def _run(self, turn):
        turn.to(State.RESOLVING)

        # The fast path. A resolver that always escalates is also valid and is
        # how cogiti runs with no fast path at all — so an absent one is not a
        # special case here, it is simply no decision.
        decision = self.cogiti.resolve(turn.text)
        turn.decision = decision
        self.cogiti.trace.decided(self, turn, decision)
        result = await self._act(turn, decision)

        if result is None:
            turn.to(State.THINKING)
            result, running = await detach.with_deadline(
                escalate.run(self.cogiti, self, turn))
            if running is not None:
                # It is still working. The turn ends anyway — that is the
                # whole point of the stage — and the answer is delivered when
                # it arrives, by _delivers().
                result = self._detach(turn, running)

        if turn.interrupted:
            return None

        turn.result = result
        turn.to(State.SPEAKING)
        said = await self.cogiti.output.say(result)
        self.history.append((turn.text, said))
        del self.history[:-HISTORY]
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

    def _detach(self, turn, task):
        """Stop waiting for an escalation, and arrange for its answer.

        The job row already exists — the agent adapter inserts one before it
        spawns anything — so this creates nothing. It records who is waiting,
        and hands the turn a sentence to say meanwhile.
        """
        job_id = getattr(turn, "job_id", None) or task.get_name()
        d = detach.Detached(job_id, turn.text[:60], task, self)
        self.cogiti.pending.add(d)

        def arrived(t):
            if t.cancelled():
                self.cogiti.pending.drop(d.job_id)
                return
            e = t.exception()
            if e is not None:
                self.cogiti.pending.done(d.job_id, {
                    "type": "failed", "kind": "job",
                    "message": "that job failed: %s" % e})
                return
            self.cogiti.pending.done(d.job_id, t.result())

        task.add_done_callback(arrived)
        return {"type": "result", "say": detach.STILL_WORKING, "linger": 0}

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
            said = dict(result)
            # Name it. An answer arriving a minute later with no reference to
            # the question is an announcement out of nowhere.
            if said.get("type") != "failed" and said.get("say"):
                said["say"] = "About %s — %s" % (d.title, said["say"])
            await self.cogiti.output.say(said)

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
        return self.cogiti.resolve(text)

    async def heard(self, text):
        """A final transcript. This is an utterance like any other — unless
        there is nothing in it.

        A recogniser returns an empty final for a sound that was loud enough
        to end the silence and had no words in it: a door, a chair, a cough.
        That is a normal answer and not an error, and it was starting a turn —
        which resolved to nothing, escalated, and spent thirteen seconds and a
        language model call establishing that the empty string means nothing.

        Only genuinely empty. Where the line falls for a *short* transcript is
        a judgement about this room and this microphone, and it belongs in the
        resolver's thresholds rather than hidden in a guard here.
        """
        if not (text or "").strip():
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
            return await self.cogiti.start_job(cmd, decision,
                                               "%s/%s" % self.key)
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

    def context(self):
        """What an escalation is told beyond the utterance itself.

        Deliberately small. `prompt.context` was left undefined in the agent
        protocol so its shape could come from the first real prompt rather than
        from a guess, and this is that first prompt: the recent turns, and
        nothing else. Memory, identity and device defaults each arrive with the
        port or the module that owns them.
        """
        return {"recent": [{"said": t, "answered": a} for t, a in self.history]}
