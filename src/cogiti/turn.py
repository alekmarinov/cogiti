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
    LISTENING = "listening"      # someone is speaking; nothing has been said yet
    RESOLVING = "resolving"
    ACTING = "acting"            # a resolved command, running locally
    CONFIRMING = "confirming"    # asked, waiting; expires into cancelled
    THINKING = "thinking"        # escalated
    NEEDS_INPUT = "needs-input"
    SPEAKING = "speaking"


class Turn:
    """One utterance and what became of it."""

    def __init__(self, session, text):
        self.session = session
        self.text = text
        self.state = State.IDLE
        self.result = None
        self.decision = None            # the resolver's, for the trace
        self.question = None            # asked, awaiting an answer
        self._answer = asyncio.Future()
        self._task = None
        self.interrupted = False

    def needs_answer(self):
        """Is something actually waiting on a person right now?

        The state alone is not enough. `answer()` resolves the future but the
        state stays CONFIRMING until the turn wakes up and moves on, so a
        state-only test says "still waiting" to the very next line read — and
        a scripted session then dispatched the following utterance alongside a
        turn that had not finished, which printed its answers out of order.
        """
        return (self.state in (State.NEEDS_INPUT, State.CONFIRMING)
                and not self._answer.done())

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
        """Whatever was being waited for — an agent's question or a confirm."""
        if not self._answer.done():
            self._answer.set_result(value)
        self.question = None
        # A confirm resumes into acting or cancelling and sets its own state;
        # forcing THINKING here would report an escalation that never happened.
        if self.state is State.NEEDS_INPUT:
            self.to(State.THINKING)

    # -------------------------------------------------------- confirming --

    #: Words that mean yes. Deliberately a small closed set, matched exactly:
    #: anything not on it is a no, and the cost of that asymmetry is a person
    #: repeating themselves rather than a device doing something irreversible
    #: because it half-heard "no, wait".
    YES = frozenset(("yes", "yeah", "yep", "yes please", "go ahead", "do it",
                     "confirm", "ok", "okay", "sure"))

    #: Words that abandon whatever was asked. "Cancel" and "never mind" always
    #: leave — a question must never be a trap the user has to answer to escape.
    ABANDON = frozenset(("cancel", "never mind", "nevermind", "forget it",
                         "stop", "no", "nothing", "leave it", "don't"))

    async def _ask(self, question, state, timeout_s):
        """Put a question and wait for the next thing said. None if nothing was.

        Shared by `confirm` and `ask_slot` because the waiting is identical and
        the difference is only what the answer means.

        **Putting the question means saying it.** It used to be stored on the
        turn and read by nobody: the device went silent, waited its full
        timeout, and gave up — and from in front of it nothing had happened at
        all. A question nobody hears is not a question, it is a pause.
        """
        self.question = question
        self._answer = asyncio.Future()
        self.to(state)
        await self.session.asked(self, question)
        try:
            return await asyncio.wait_for(asyncio.shield(self._answer), timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self.question = None

    async def confirm(self, question, timeout_s=12.0):
        """Ask, and wait. Returns True only for an explicit yes.

        **A confirm never times out into yes.** It expires into cancelled,
        silently. This is worth being explicit about because the opposite is a
        one-line change someone will eventually make to smooth over an awkward
        pause, and the thing on the other side of it is `power_off`.
        """
        said = await self._ask(question, State.CONFIRMING, timeout_s)
        if said is None:
            return False
        return str(said).strip().lower().rstrip(".!") in self.YES

    async def ask_slot(self, question, timeout_s=12.0):
        """Ask for one missing slot. Returns what was said, or None to abandon.

        Unlike a confirm, the answer is not a word from a list — it is whatever
        the person said, handed back to the resolver. The only thing read here
        is whether they wanted out.
        """
        said = await self._ask(question, State.NEEDS_INPUT, timeout_s)
        if said is None:
            return None
        if str(said).strip().lower().rstrip(".!") in self.ABANDON:
            return None
        return str(said).strip()

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
