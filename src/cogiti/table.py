"""config/commands.toml: a resolved intent -> an effect and the way it is shown.

`docs/command-table.md` is the contract. **Adding a command is editing a file.**
The resolver's registry decides what an utterance *is* without touching its
runtime; this decides what it *does* without touching cogiti. Between the two,
"the device should also know about X" is two files and no build.

What this module refuses to do is as much of the design as what it does:

**Templates, never model output.** A local command has a known shape, and the
device saying it the same way every time is a feature rather than a
limitation. Prose is what escalation is for.

**An unknown intent escalates.** A table with no entry for `get_price` is not
an error; it is a device that has not been taught that yet, and the model is
the fallback. A *malformed* entry is an error, and at load, not at first use.

**A `confirm` is wording, not policy.** The resolver already decided an intent
needs confirming, and cogiti never auto-answers one, never lets it expire into
yes, and never lets an agent answer it. The table only supplies the sentence.
"""

import string

try:
    import tomllib
except ImportError:                                           # pragma: no cover
    tomllib = None

from . import providers

#: Seconds an answer stays on screen once it has finished being spoken, unless
#: the command says otherwise. Ten is long enough to read a sentence twice and
#: short enough that a device left alone returns to a face rather than to a
#: stale answer.
DEFAULT_LINGER = 10.0

# Spoken apologies, one per category, in one place — so the device fails the
# same way every time rather than in five voices.
APOLOGIES = {
    "offline": "I can't reach that right now.",
    "refused": "That didn't work.",
    "not_found": "I couldn't find that.",
    "timeout": "That took too long.",
    "unavailable": "I can't do that on this device.",
}


class TableError(Exception):
    pass


class Command:
    __slots__ = ("intent", "provider", "job", "announce", "speak", "present",
                 "confirm", "timeout_ms", "offline", "args", "command", "source",
                 "linger")

    #: Job kinds an intent may start. A job is what a command becomes when it
    #: outlives its turn — see the last section of `docs/command-table.md`,
    #: which is emphatic that forcing one into the table is how a device ends
    #: up pausing for two seconds in the middle of a conversation.
    JOBS = ("timer", "cancel_timer")

    def __init__(self, intent, spec):
        self.intent = intent
        self.provider = spec.get("provider")
        self.job = spec.get("job")
        if bool(self.provider) == bool(self.job):
            raise TableError(
                "[%s] needs exactly one of `provider` (finishes inside the "
                "turn) or `job` (outlives it)" % intent)
        if self.job and self.job not in self.JOBS:
            raise TableError("[%s] unknown job kind %r; known: %s"
                             % (intent, self.job, ", ".join(self.JOBS)))
        if self.provider and providers.get(self.provider) is None:
            raise TableError(
                "[%s] names provider %r, which is not registered. Known: %s"
                % (intent, self.provider, ", ".join(providers.names())))
        # What is said when the job finishes, which may be an hour later and
        # with nobody having asked anything. Only a job has one.
        self.announce = spec.get("announce")
        if self.announce and not self.job:
            raise TableError("[%s] has `announce` but is not a job" % intent)
        self.speak = spec.get("speak", "")
        self.present = spec.get("present", "none")
        # How long the answer stays on screen after it has been spoken, in
        # seconds. `lifetime: turn` makes expiry the adapter's business, and a
        # renderer with no notion of a turn boundary — which is every one we
        # have — simply leaves the card up until something replaces it. So the
        # answer to "what time is it" sat there for the rest of the evening.
        #
        # Per command, because the right duration is a property of the answer
        # and not of the device: a spoken confirmation is stale the moment it
        # is heard, an IP address is copied off the screen by hand, and a timer
        # that has just gone off should not vanish while you walk to it.
        # `linger = 0` keeps it up until the next answer, which is the old
        # behaviour and is still the right one for some.
        self.linger = spec.get("linger", DEFAULT_LINGER)
        try:
            self.linger = float(self.linger)
        except (TypeError, ValueError):
            raise TableError("[%s] linger must be a number of seconds, not %r"
                             % (intent, spec.get("linger")))
        if self.linger < 0:
            raise TableError("[%s] linger cannot be negative" % intent)
        self.confirm = spec.get("confirm")
        self.timeout_ms = int(spec.get("timeout_ms", 250))
        self.offline = spec.get("offline", "refuse")
        self.args = spec.get("args", {})
        # For shell.run. Kept out of `args` so a slot can never reach it.
        self.command = spec.get("command")
        self.source = spec.get("source")

    def ask_for(self, slot_name):
        """The wording for a missing slot, or None if the table has none.

        No wording means no asking: an intent whose table entry never says how
        to ask for `duration` escalates instead, which is a gap the author can
        close by adding a sentence rather than a behaviour cogiti invents.
        """
        for name, spec in self.args.items():
            if spec.get("slot", name) == slot_name:
                return spec.get("ask")
        return None

    def bind(self, decision):
        """Resolver slots -> provider arguments, with defaulting made explicit.

        A slot arrives already carrying whether it was defaulted, and that flag
        survives into the result so the speech can say "in Sofia" versus "where
        you are". Losing it here would lose the one thing the resolver port
        went out of its way to preserve.
        """
        args, provenance = {}, {}
        for name, spec in self.args.items():
            slot_name = spec.get("slot", name)
            slot = (decision.slots or {}).get(slot_name)
            if slot is not None:
                args[name] = slot["value"]
                provenance[name] = ("defaulted" if slot["defaulted"]
                                    else "stated")
            elif "default" in spec:
                args[name] = spec["default"]
                provenance[name] = "defaulted"
            elif spec.get("required"):
                # Should not happen: reflexi escalates a missing required slot
                # rather than handing over an incomplete decision. Checked
                # because "should not happen" is where the expensive ones live.
                raise TableError("[%s] requires %s and the decision has none"
                                 % (self.intent, slot_name))
        return args, provenance


class Table:
    def __init__(self, commands):
        self.commands = commands

    def __contains__(self, intent):
        return intent in self.commands

    def get(self, intent):
        return self.commands.get(intent)

    def __len__(self):
        return len(self.commands)


def load(path):
    if tomllib is None:                                       # pragma: no cover
        raise TableError("no tomllib; cogiti needs python 3.11 or newer")
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise TableError("cannot read the command table %s: %s" % (path, e))
    except Exception as e:                                    # noqa: BLE001
        raise TableError("%s is not valid toml: %s" % (path, e))

    commands = {}
    for intent, spec in raw.items():
        if not isinstance(spec, dict):
            raise TableError("[%s] is not a section" % intent)
        commands[intent] = Command(intent, spec)
    return Table(commands)


# ------------------------------------------------------------- rendering --

class _Safe(dict):
    """A missing key renders as itself rather than raising.

    A template referring to a value the provider did not return is a table bug,
    and it should read as one — "It's {temp_c} degrees" spoken aloud says
    exactly where to look. Raising mid-turn would instead lose the answer the
    provider already computed.
    """

    def __missing__(self, key):
        return "{%s}" % key


def render(template, values):
    if not template:
        return ""
    try:
        return string.Formatter().vformat(template, (), _Safe(values))
    except (ValueError, IndexError):
        return template


def apology(reason):
    return APOLOGIES.get(reason, APOLOGIES["refused"])
