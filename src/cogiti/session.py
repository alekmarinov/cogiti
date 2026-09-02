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
        if self.current and self.current.state not in (State.IDLE, State.SPEAKING):
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
            result = await escalate.run(self.cogiti, self, turn)

        if turn.interrupted:
            return None

        turn.result = result
        turn.to(State.SPEAKING)
        said = await self.cogiti.output.say(result)
        self.history.append((turn.text, said))
        del self.history[:-HISTORY]
        turn.to(State.IDLE)
        return result

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
