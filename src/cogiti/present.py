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
        #: What is on the stage, in the order it was drawn, as short strings.
        #: Not ids: "the first one" is a thing a person says about what they
        #: can see, and `brain/panels/0` answers a different question.
        self._onscreen = []
        #: Panels drawn during the turn that is still being answered. They
        #: belong to the answer that is coming, not to the one before it.
        self._this_turn = set()

    # ------------------------------------------------------------- states --

    def busy(self, on):
        """Sent the moment a turn starts, before any work — architecture.md §3.
        The face showing it heard you is the cheapest latency there is."""
        self.a.send(op="busy", state=bool(on))

    def expression(self, name, weight=0.8, fade_ms=250):
        self.a.send(op="expression", name=name, weight=weight, fade_ms=fade_ms)

    def awake(self, on):
        """Open the face's eyes, or close them.

        The renderer starts long before this does — on the appliance, from
        rcS, while the brain is still four init scripts away — so it boots
        with `--asleep` and stares at nothing until told. Waking is therefore
        the brain's first act rather than part of its configuration.

        **Woken when cogiti is listening, not when the network is up.** Then
        closed eyes means exactly one thing, "there is no brain", which is a
        diagnosis readable from across the room. If waking also waited on a
        DHCP lease, a sleeping face could mean the brain is fine and the
        network is slow, and the signal would be worth nothing.
        """
        self.a.send(op="awake", state=bool(on))

    def notice(self, text):
        """Something the device noticed, put where it can be ignored.

        Pinned to the periphery, which already means "conversation never
        shoves it aside" — so it survives the answers that come and go beneath
        it and stays until it is replaced or taken down.

        Not spoken, and that is the point. `announce` talks over whatever is
        happening and says so in its own docstring; a device that interrupts
        to report something it merely observed is a device people stop leaving
        switched on. Anything worth saying out loud is worth a turn.
        """
        self.a.send(op="create", id="notice", kind="text", text=text,
                    style="caption", region=PERIPHERY, lifetime=PINNED,
                    attention="never")

    def not_for_me(self):
        """Something was heard and deliberately not acted on.

        The attention window drops what nothing addressed to the device, and
        that is the feature working. Dropped in silence it is indistinguishable
        from broken: measured on a device, the same question was asked three
        times, appeared on the screen three times, and nothing moved — until a
        greeting happened to open the window and the fourth one answered.

        A gesture and not a sentence. Speaking over a room that was not talking
        to the device is exactly the interruption the window exists to prevent,
        so the device says no the way a person across a table would.
        """
        self.a.send(op="gesture", name="shake")

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

    PANELS = "brain/panels"

    def panels(self, items):
        """Several things side by side, each a picture with words under it.

        `result` says why this is a separate method rather than two answers
        left on the stage: keeping two cards up is a deliberate act, and the
        protocol has a shape for it — one group, declared by whoever decided
        the things belong together. Two cards that merely happened in a row
        are not a comparison.

        One group per item, and the adapter gives groups on the stage a
        common width so the rows line up and the eye can read across. No
        coordinate is sent, here or anywhere: which column a thing lands in
        is the only party that knows the screen's business.

        An item without a picture still draws. The alternative is that one
        unreachable photograph loses the specifications as well, which is the
        wrong way round — the words were the answer and the picture was the
        illustration.
        """
        if not items:
            return None
        self.clear_thoughts()
        self.clear_heard()
        self._clear_previous(self.PANELS)
        drawn = []
        for i, item in enumerate(items):
            children = []
            if item.get("image"):
                children.append({"kind": "image", "src": item["image"]})
            if item.get("title"):
                children.append({"kind": "text", "style": "title",
                                 "text": item["title"]})
            if item.get("lines"):
                children.append({"kind": "text", "style": "caption",
                                 "text": item["lines"]})
            if not children:
                continue
            oid = "%s/%d" % (self.PANELS, i)
            self.a.send(op="create", id=oid, kind="group", children=children,
                        region=STAGE, lifetime=TURN, attention="once",
                        fallback=item.get("title") or "")
            self._showing.add(oid)
            self._this_turn.add(oid)
            self._onscreen.append(item.get("title") or item.get("lines") or "")
            drawn.append(oid)
        return drawn or None

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
        if result.get("pending"):
            # A `pending` result is the "I'm still working on that" line, and
            # the work goes on behind it. Clearing the reasoning there is the
            # one place it must not be cleared: twenty thoughts were being
            # drawn and wiped five seconds in, so a minute of searching
            # showed a blank screen and a head.
            #
            # Marked as belonging to this turn so the sweep below spares it
            # too — the same mechanism the panels use, for the same reason.
            self._this_turn.add(THOUGHTS)
        else:
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
        can go and the one it *is* is not destroyed and recreated.

        **None when it will draw nothing**, and that distinction is the whole
        of a bug people could see: a result with no `show` — anything the
        table gives `present = "none"` — claimed the answer id anyway. The
        sweep then spared an object this result was never going to redraw, and
        whatever was on the stage before simply stayed.

        Reported from a device as a confirmation that would not go away: the
        question is drawn as an answer card, `Cancelled.` and `Updating. I'll
        tell you when it's done.` both present as nothing, so the question sat
        there after it had been answered — outliving the thing it was asking
        about.
        """
        if result.get("type") == "failed":
            return ANSWER            # the failure line below is drawn
        show = result.get("show")
        if not show:
            return None              # nothing drawn, so nothing spared
        if isinstance(show, dict):
            return show.get("id", ANSWER)
        return ANSWER

    def on_screen(self):
        """What a person standing in front of it can see, in order.

        So that "tell me more about the first one" has a first one. The
        device drew three products, said their names out loud, and then met
        the obvious follow-up with no idea what was being pointed at —
        because the only record of the panels was a set of object ids on the
        way to a socket.
        """
        return [t for t in self._onscreen if t]

    def _clear_previous(self, keeping):
        """Everything from before this answer goes; what this answer drew stays.

        The distinction is the whole of it, and leaving it out cost a turn
        that worked perfectly and showed nothing: the model searched, found a
        product, fetched the photograph and drew the panel — then said "the
        specs are on the screen" while this destroyed them a second later,
        because from here a panel drawn ten seconds ago is indistinguishable
        from an answer to the previous question.
        """
        gone = False
        for oid in list(self._showing):
            if oid != keeping and oid not in self._this_turn:
                self.a.send(op="destroy", id=oid)
                self._showing.discard(oid)
                if oid.startswith(self.PANELS):
                    gone = True
        if gone:
            # They are off the screen, so they are no longer what "the first
            # one" means. A stale list is worse than none: it would have the
            # device confidently describing something nobody can see.
            self._onscreen = []
        self._this_turn.clear()

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
