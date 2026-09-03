"""The device's own commands, offered to the model as a tool.

The fast path can act and an escalation could only talk. Ask "turn it up a
bit, would you" in a sentence the resolver does not match, and the model
would answer *about* the volume — agreeably, and without touching it. The
device knew how to do the thing and the part of it that was listening had no
way to say so.

So the commands become a tool. cogiti already brokers tools for the agent and
already answers some itself (`local_tools`), so this needs no protocol change:
the model calls `device`, cogiti runs the command it would have run for a
resolved intent, and hands back what the provider returned. The model then
says something true about a thing that has actually happened.

**Everything the resolver can reach, the model can reach.** There is no
second list and no hole in this one: whatever a person could get by saying
the right sentence, an escalation can get by calling this, because the
escalation happens exactly when they did *not* find the right sentence.

Two earlier cuts at this were both wrong, and wrong in the same way — they
tried to encode consent as absence.

First everything with a `confirm` wording was withheld, on the reasoning that
a confirm exists because a person should be asked and a model that can call
it has answered on their behalf. The second half of that is true; the
conclusion does not follow. The model is not the one answering.

Then jobs were withheld for being jobs, which hid "what have you got pinned"
— three seconds and read-only — while the thing actually worth being careful
about, an authoring run that writes a service, is a job for the same
mechanical reason.

**So consent is enforced where it was declared, not by omission.** A command
carrying a `confirm` asks the person when the model calls it, in the same
words and through the same turn as the fast path. The model proposes and
cogiti decides — `security.md` — and "decides" here means asking whoever is
standing in front of it. A refusal comes back as a refusal, which the model
is told to relay rather than paper over.
"""


class Slot(dict):
    """What a resolver slot looks like, built from a model's argument.

    `defaulted` is False because the model stated it — the same distinction
    the resolver preserves, so a template can still say "in Sofia" rather than
    "where you are" and mean it.
    """

    def __init__(self, value):
        super().__init__(value=str(value), text=str(value), type="text",
                         defaulted=False)


class Decision:
    """A decision the resolver did not make.

    Shaped like one because `Command.bind` reads slots and nothing else, and
    giving the model a second path into providers — one that skipped binding —
    would be two ways to call the same thing, which is how they drift.
    """

    __slots__ = ("intent_id", "verdict", "tier", "slots", "missing_slot",
                 "confidence", "runner_up_id", "runner_up", "rejected",
                 "normalized")

    def __init__(self, intent, slots=None):
        self.intent_id = intent
        self.verdict = "handle"
        self.tier = "agent"          # not pattern, not similar: the model said
        self.slots = slots or {}
        self.missing_slot = None
        self.confidence = 1.0
        self.runner_up_id, self.runner_up = None, 0.0
        self.rejected, self.normalized = False, ""


def offered(table):
    """Every command in the table, and what each wants.

    The table is the list. An intent added later is offered because it is
    there, and nobody has to remember this file exists.
    """
    out = {}
    for intent, cmd in sorted(table.commands.items()):
        wants = None
        for name, spec in (cmd.args or {}).items():
            if spec.get("required"):
                wants = spec.get("slot", name)
                break
        out[intent] = wants
    return out


def asks(table):
    """Which commands put a question to the person before they happen.

    Named to the model so it knows a call may come back refused and that this
    is an ordinary outcome rather than a fault — and so it does not promise,
    before asking, that the thing is done.
    """
    return {i: c.confirm for i, c in sorted(table.commands.items())
            if c.confirm}


def tool(offers, others=None):
    """The declaration. One tool with an enum, not one tool per command: a
    model choosing from twenty tool names picks the wrong one more often than
    a model choosing from one enum, and the schema stays small enough to send
    on every turn."""
    lines = []
    for intent, wants in sorted(offers.items()):
        lines.append("%s%s" % (intent, " (needs %s)" % wants if wants else ""))
    description = (
        "Do something on this device, or read one of its values. Use it "
        "whenever the answer involves the device itself rather than "
        "general knowledge — the time here, this machine's address, the "
        "volume, a live price. Prefer it over saying what you would do. "
        "Available: " + ", ".join(lines))
    if others:
        description += (
            ". These ask the person first, in these words, and you will be "
            "told what they said: "
            + ", ".join("%s (\"%s\")" % (i, c) for i, c in sorted(others.items()))
            + ". Call them the same as any other when that is what was "
              "wanted — but do not say the thing is done until the result "
              "says it is, and if it comes back refused, say so plainly.")
    return {
        "name": "device",
        "description": description,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "enum": sorted(offers)},
                "argument": {
                    "type": "string",
                    "description": "the one value some commands need — a "
                                   "symbol like BTC, a place like London, a "
                                   "level like 40. Omit for the rest.",
                },
            },
        },
    }


async def run(cogiti, offers, args, session_id=None, turn=None):
    """Run one, and tell the model plainly what happened.

    Returns what the provider produced, not a sentence: the model is writing
    the sentence and a pre-written one would either be ignored or repeated
    verbatim, and both are worse than the values.
    """
    intent = (args or {}).get("command")
    if intent not in offers:
        return {"ok": False,
                "problem": "%r is not something this device offers" % intent}

    cmd = cogiti.table.commands.get(intent)
    if cmd is None:
        return {"ok": False, "problem": "%r has no command" % intent}

    wants = offers[intent]
    argument = (args or {}).get("argument")
    if wants and not argument:
        return {"ok": False,
                "problem": "%s needs %s — call it again with that" % (intent,
                                                                      wants)}
    slots = {wants: Slot(argument)} if wants and argument else {}
    decision = Decision(intent, slots)

    if cmd.confirm:
        # The same question, in the same words, through the same turn as the
        # fast path. Withholding these was the old answer and it encoded
        # consent as absence: the model could not ask for the thing, so the
        # person was never asked either, and what they got instead was a
        # sentence about how they might phrase it.
        if turn is None or not turn.can_ask():
            # The turn has ended — detached, interrupted, or answered — so
            # there is nobody attached to ask. Refusing is the only honest
            # move: doing it anyway performs a confirm on somebody's behalf,
            # which is the exact thing the wording exists to prevent.
            return {"ok": False, "asked": False,
                    "problem": "%s needs %s to be asked first, and the turn "
                               "that could ask them has ended. Tell them to "
                               "say it themselves." % (intent, "them")}
        if not await turn.confirm(cmd.confirm):
            return {"ok": False, "asked": True, "refused": True,
                    "problem": "they were asked %r and did not say yes"
                               % cmd.confirm}

    if cmd.job:
        # A reporting job — the slow ones are withheld — so it is awaited like
        # any other command. `start_job` wants the turn because some of these
        # read the session it was asked in.
        result = await cogiti.start_job(cmd, decision, session_id, turn=turn)
    else:
        result = await cogiti.run_command(cmd, decision)
    result = result or {}

    if result.get("type") == "failed":
        return {"ok": False, "problem": result.get("message", "it failed")}
    return {"ok": True, "said": result.get("say", ""),
            "values": result.get("_values", {})}
