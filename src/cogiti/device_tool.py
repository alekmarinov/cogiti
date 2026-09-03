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

**What is offered, and what is not.**

Only commands whose intent the resolver would `handle`. Anything the registry
marks `confirm` — removing a service, shutting down, pausing something — is
withheld, and that is the whole safety argument here: a confirm exists because
a *person* should be asked, and a model that can call it has answered the
question on their behalf. The list is derived from the table rather than
written out, so an intent added later is offered or withheld by its own
verdict and nobody has to remember this file exists.

**Jobs are not withheld by being jobs**, which is what this used to do and
was wrong in both directions. "What have you got pinned" is a job because the
registry lives on the event loop, and it answers in three seconds; `pin_thing`
is a job because it writes a service, and it takes three minutes and asks a
question halfway through. Withholding by kind hid the first and, had the kind
test ever been relaxed, would have parked the model inside the second. So a
command that must not be model-run says `agent = "never"` in its own entry,
and this reads that.
"""

#: Providers that only produce a sentence. Offering them would spend a tool
#: call to be told to say hello, which the model can do by saying hello.
CHATTER = ("conversation.acknowledge",)


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
    """Which commands the model may run, and what each wants.

    Derived from the table's own fields, so nothing here is a second list to
    keep in step:

      a `confirm` wording means a *person* is meant to be asked, and a model
      that can call it has answered on their behalf — power_off, reboot, and
      anything that deletes what somebody built;

      `agent = "never"` is the entry saying so itself, for the ones no other
      field catches: authoring a service, cancelling someone's work, and the
      two that are about the conversation rather than the world;

      chatter produces a sentence and nothing else.

    An intent added later is offered or withheld by what its own entry says,
    and nobody has to remember this file exists.
    """
    out = {}
    for intent, cmd in sorted(table.commands.items()):
        if cmd.confirm or cmd.agent == "never" or cmd.provider in CHATTER:
            continue
        wants = None
        for name, spec in (cmd.args or {}).items():
            if spec.get("required"):
                wants = spec.get("slot", name)
                break
        out[intent] = wants
    return out


def withheld(table):
    """What the device can do that the model may not start itself.

    Offered as knowledge, not as power. Without it the model does not know
    these exist at all: "pin the coke on the screen" reached one that had no
    idea this device pins anything, so instead of saying the obvious thing it
    improvised. Knowing means it can hand the request back in words that
    work — which is the honest answer when the doing is somebody else's.

    Each carries its own confirm wording where it has one, because that
    wording already says what the thing does in a sentence meant for a person.
    """
    out = {}
    for intent, cmd in sorted(table.commands.items()):
        if intent in offered(table) or cmd.provider in CHATTER:
            continue
        out[intent] = cmd.confirm
    return out


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
            ". This device can also do these, but only when the person asks "
            "for them plainly, so you cannot call them: "
            + ", ".join("%s (it asks \"%s\")" % (i, c) if c else i
                        for i, c in sorted(others.items()))
            + ". If they want one, say what it is they should ask for — "
              "never say the device cannot do it, and never pretend you did "
              "it.")
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
