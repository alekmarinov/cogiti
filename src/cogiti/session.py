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

    async def answer(self, value):
        """The person answered a question the agent asked."""
        if self.current and self.current.state is State.NEEDS_INPUT:
            self.current.answer(value)
            return True
        return False

    # --------------------------------------------------------------- run --

    async def _run(self, turn):
        turn.to(State.RESOLVING)

        # No resolver is configured, so everything escalates. ports.md: "a
        # resolver that always escalates is also valid, and is how cogiti runs
        # with no fast path at all."
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
