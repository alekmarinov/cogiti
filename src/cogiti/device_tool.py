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

Jobs are withheld too. They outlive the turn, and a model that starts one is
committing the device to work nobody asked for.
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
      that can call it has answered on their behalf — power_off and reboot;

      a `job` outlives the turn, and a model starting one commits the device
      to work nobody asked for — every service and timer command;

      chatter produces a sentence and nothing else.

    An intent added later is offered or withheld by what its own entry says,
    and nobody has to remember this file exists.
    """
    out = {}
    for intent, cmd in sorted(table.commands.items()):
        if cmd.job or cmd.confirm or cmd.provider in CHATTER:
            continue
        wants = None
        for name, spec in (cmd.args or {}).items():
            if spec.get("required"):
                wants = spec.get("slot", name)
                break
        out[intent] = wants
    return out


def tool(offers):
    """The declaration. One tool with an enum, not one tool per command: a
    model choosing from twenty tool names picks the wrong one more often than
    a model choosing from one enum, and the schema stays small enough to send
    on every turn."""
    lines = []
    for intent, wants in sorted(offers.items()):
        lines.append("%s%s" % (intent, " (needs %s)" % wants if wants else ""))
    return {
        "name": "device",
        "description":
            "Do something on this device, or read one of its values. Use it "
            "whenever the answer involves the device itself rather than "
            "general knowledge — the time here, this machine's address, the "
            "volume, a live price. Prefer it over saying what you would do. "
            "Available: " + ", ".join(lines),
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


async def run(cogiti, offers, args):
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
    result = await cogiti.run_command(cmd, Decision(intent, slots))

    if result.get("type") == "failed":
        return {"ok": False, "problem": result.get("message", "it failed")}
    return {"ok": True, "said": result.get("say", ""),
            "values": result.get("_values", {})}
