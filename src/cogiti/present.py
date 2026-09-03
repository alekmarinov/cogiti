"""A result becomes something visible.

`docs/architecture.md` §2: **nothing above this module may use a presentation
adapter's protocol vocabulary.** A region name appears here and nowhere else in
cogiti. That rule is what lets a terminal, a web page and a 3D head be equally
valid adapters — the turn machine deals in results, and only this file knows
that a result is drawn somewhere called `stage`.

cogiti sends **intent, never geometry**. There are no coordinates below, and
there must never be: where things go is the adapter's business, because it is
the only party that knows the screen.
"""

# The vocabulary. Every one of these strings is defined by the presentation
# port, and this is the only module in cogiti allowed to say them.
STAGE = "stage"              # conversational; the adapter rearranges it freely
PERIPHERY = "periphery"      # pinned; conversation never shoves it aside
TURN = "turn"
PINNED = "pinned"

ANSWER = "brain/answer"
THOUGHTS = "brain/thoughts"
HEARD = "brain/heard"


class Presenter:
    """The turn machine's view of the screen. Every method is best effort:
    a presentation adapter that is absent must never fail a turn."""

    def __init__(self, adapter):
        self.a = adapter
        self._showing = set()

    # ------------------------------------------------------------- states --

    def busy(self, on):
        """Sent the moment a turn starts, before any work — architecture.md §3.
        The face showing it heard you is the cheapest latency there is."""
        self.a.send(op="busy", state=bool(on))

    def expression(self, name, weight=0.8, fade_ms=250):
        self.a.send(op="expression", name=name, weight=weight, fade_ms=fade_ms)

    def idle(self):
        self.a.send(op="idle")

    def stop(self):
        """Barge-in, step one: the presentation adapter stops before the audio
        does. ports.md fixes that order and it is not a preference."""
        self.a.send(op="stop")

    # ------------------------------------------------------------ content --

    def thought(self, text):
        """A thought stream, explicitly `attention: never`.

        The face must not look at its own reasoning. `watch` would have it
        staring at a scrolling panel for the whole of a long escalation, which
        reads as the device ignoring the person it is talking to.
        """
        if not text:
            return
        if not self.a.supports("kinds", "stream"):
            return                       # an adapter without streams shows none
        self.a.send(op="create", id=THOUGHTS, kind="stream", append=text,
                    lines=6, style="caption", region=STAGE, lifetime=TURN,
                    attention="never", fallback="thinking")
        self._showing.add(THOUGHTS)

    def heard(self, text):
        """Show what was understood, while it is still being said.

        The cheapest reassurance there is. A device that answers three seconds
        after you stop talking has, until then, given you no evidence it heard
        anything at all — and when it eventually answers the wrong question,
        the first you learn of the mishearing is the answer.

        **One object, updated, not a stream.** A transcript grows rather than
        accumulating: "what", "what time", "what time is it" is one sentence
        arriving, and appending it would show three. Last write wins, which is
        what the port promises for an id that already exists.

        `attention: never`, for the reason the thought stream has it: the face
        should be looking at the person who is talking, not reading their
        words off its own screen.
        """
        if not text:
            return
        self.a.send(op="create", id=HEARD, kind="text", text=text,
                    style="caption", region=STAGE, lifetime=TURN,
                    attention="never", fallback=text[:120])
        self._showing.add(HEARD)

    def clear_heard(self):
        if HEARD in self._showing:
            self.a.send(op="destroy", id=HEARD)
            self._showing.discard(HEARD)

    def result(self, result):
        """The answer, as an object the adapter may later update or be asked
        about. `show` is what the agent chose to put on a screen; `say` is what
        it chose to be heard. They are deliberately not the same string.

        **The previous turn's answer is removed first.** Each card carries its
        own id — `brain/clock`, `brain/date`, `brain/answer` — so without this
        they simply accumulate: ask the time after a failed escalation and the
        stage holds "Didn't catch that" *and* the clock, side by side, with
        nothing to say which one you just asked for.

        Replacing is the right default because a conversation is a sequence,
        not a pinboard. Keeping two answers up is a deliberate act — a
        comparison — and the protocol already has a shape for it: one group
        with both inside, declared by whoever decided they belong together.
        Two cards that merely happened in a row are not that.

        Returns the id of the object it drew, or None if it drew nothing, so
        the caller can expire it later without this module having to own a
        clock.
        """
        if result is None:
            return None
        self.clear_thoughts()
        self.clear_heard()
        self._clear_previous(self._id_for(result))

        if result.get("type") == "failed":
            text = "couldn't: %s" % (result.get("message") or result.get("kind"))
            self.a.send(op="create", id=ANSWER, kind="text", text=text,
                        style="body", region=STAGE, lifetime=TURN)
            self._showing.add(ANSWER)
            return ANSWER

        show = result.get("show")
        if not show:
            return None                  # spoken only; nothing to draw
        if isinstance(show, dict) and show.get("op") == "create":
            # A rendered presentation template. It already carries its id,
            # kind, region and children — this module's job here is only to
            # remember the object so the turn can clear it.
            self.a.send(**show)
            self._showing.add(show["id"])
            return show["id"]
        if isinstance(show, str):
            self.a.send(op="create", id=ANSWER, kind="text", text=show,
                        style="title", region=STAGE, lifetime=TURN,
                        attention="once")
            self._showing.add(ANSWER)
            return ANSWER
        else:
            # A structured object. `fallback` is required rather than polite:
            # the port says an unknown kind must still hold its place, and it
            # can only do that if it was given something to draw.
            op = dict(show)
            op.setdefault("id", ANSWER)
            op.setdefault("region", STAGE)
            op.setdefault("lifetime", TURN)
            op.setdefault("fallback", result.get("say", "")[:120])
            self.a.send(op="create", **op)
            self._showing.add(op["id"])
            return op["id"]

    def speak(self, marks):
        """One `speak`, carrying the marks and the clock they run against.

        One message and not a stream of them: the renderer interpolates between
        discrete events and crossfades shapes, so sending more does not make
        the mouth smoother — it just makes the protocol chattier and the
        timing worse, because each message arrives with its own latency.
        """
        if not marks:
            return False
        op = {"op": "speak", "visemes": marks["visemes"],
              "audio_start_ns": marks["audio_start_ns"]}
        if marks.get("audio"):
            # A path, not samples. The renderer plays it only if it was started
            # with audio enabled; when it was not, the mouth still runs to the
            # same clock and the line is simply silent.
            op["audio"] = marks["audio"]
        return self.a.send(**op)

    # ------------------------------------------------------------- tidying --

    def _id_for(self, result):
        """Which object this result is about to become, so the one it replaces
        can go and the one it *is* is not destroyed and recreated."""
        show = result.get("show")
        if isinstance(show, dict):
            return show.get("id", ANSWER)
        return ANSWER

    def _clear_previous(self, keeping):
        for oid in list(self._showing):
            if oid != keeping:
                self.a.send(op="destroy", id=oid)
                self._showing.discard(oid)

    def expire(self, oid):
        """Take one object down because its time is up.

        Separate from `destroy` on purpose: an object still showing is the
        only one worth expiring, and an expiry that fires after the card was
        already replaced must not reach through and remove its replacement.
        """
        if oid in self._showing:
            self.a.send(op="destroy", id=oid)
            self._showing.discard(oid)

    def clear_thoughts(self):
        if THOUGHTS in self._showing:
            self.a.send(op="destroy", id=THOUGHTS)
            self._showing.discard(THOUGHTS)

    def clear_turn(self):
        """Objects with `lifetime: turn` are the adapter's to expire, so this
        exists for the case where cogiti wants the screen clear *now* — an
        interrupted turn, mostly, whose half-drawn answer is about to be
        replaced by a different one."""
        for oid in list(self._showing):
            self.a.send(op="destroy", id=oid)
        self._showing.clear()
