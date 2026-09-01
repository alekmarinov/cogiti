"""The turn state machine.

`docs/architecture.md` §3. This slice implements the text-driven path:

    idle -> resolving -> thinking -> speaking -> idle

`listening` belongs to the speech port and `acting` to the command table;
`confirm` and `choosing` are multi-turn flows that arrive with consent and
lists. The transitions here are the ones a text orchestrator actually makes,
and the states are named the same so the rest can slot in rather than replace.

Two behaviours are implemented rather than deferred, because they are the ones
that are hard to add later:

**One turn at a time per session, always accepting.** A new utterance while a
turn is `thinking` does not queue behind it. It interrupts — and the
interrupted turn's remaining messages are drained before the new one is
dispatched, or the answers mix.

**A question is not a hang.** When the agent asks something, the turn moves to
`needs-input` and the person is asked. A pending question is a list with a
deadline, not a callback.
"""

import asyncio
import enum


class State(enum.Enum):
    IDLE = "idle"
    RESOLVING = "resolving"
    THINKING = "thinking"
    NEEDS_INPUT = "needs-input"
    SPEAKING = "speaking"


class Turn:
    """One utterance and what became of it."""

    def __init__(self, session, text):
        self.session = session
        self.text = text
        self.state = State.IDLE
        self.result = None
        self.question = None            # asked, awaiting an answer
        self._answer = asyncio.Future()
        self._task = None
        self.interrupted = False

    def to(self, state):
        self.state = state
        self.session.on_state(self, state)

    # ------------------------------------------------------------ answer --

    def ask(self, question):
        """The agent needs something it was not given."""
        self.question = question
        self.to(State.NEEDS_INPUT)
        return self._answer

    def answer(self, value):
        if not self._answer.done():
            self._answer.set_result(value)
        self.question = None
        self.to(State.THINKING)

    # --------------------------------------------------------- interrupt --

    def interrupt(self):
        """A new utterance arrived. This turn stops mattering.

        The task is cancelled, which cancels the agent job and every tool job
        under it — they are process groups under one parent. What must not
        happen is this turn's result arriving after the next turn's, which is
        why the caller drains before dispatching.
        """
        self.interrupted = True
        if not self._answer.done():
            self._answer.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
