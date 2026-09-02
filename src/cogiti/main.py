"""config, open everything, install signal handlers, run forever.

`docs/architecture.md` §2. This slice runs the text-driven orchestrator that
`ports.md` describes: "a cogiti with an agent and a platform is a usable
text-driven orchestrator". You type; it escalates; it answers.
"""

import argparse
import asyncio
import os
import signal
import sys
import time

from . import config as _config
from . import db as _db
from . import jobs
from .adapters import agent, audi, presentation, resolver
from . import present
from . import presentation_templates as templates
from . import providers
from . import secrets
from . import table
from . import timers
from . import speech as speech_mod
from .session import Session
from .trace import Trace


class TextOutput:
    """The output of last resort, and a legitimate one.

    ports.md gives presentation and speech as optional ports, which leaves a
    deployment with neither unable to answer at all. Rather than a silent
    fallback, printing is a configured choice — `output = text` — so that
    'no way to reach the user' stays a startup failure rather than a surprise.
    """

    async def say(self, result):
        if result is None:
            return ""
        if result.get("type") == "failed":
            text = "I couldn't do that: %s" % result.get("message", result.get("kind"))
        else:
            text = result.get("say", "")
        print(text, flush=True)
        show = result.get("show")
        if show:
            print("  [would show: %s]" % (show if isinstance(show, str)
                                          else show.get("kind", "an object")),
                  flush=True)
        return text


class FaceOutput:
    """A result reaches the person through the presentation and speech ports.

    The two are used together but stay separable, which is the point of them
    being two ports: `say` is what is heard, `show` is what is drawn, and an
    adapter may be missing without the other stopping. With no speech adapter
    the face shows the answer silently; with no presentation adapter it is
    spoken and nothing is drawn.
    """

    def __init__(self, presenter, speech=None, echo=False):
        self.p = presenter
        self.speech = speech
        self.echo = echo            # also print, for a workstation with no ears
        self._expiry = None         # the pending "take that card down" task
        # The out half of the speech port, when one adapter implements both
        # halves. Set after construction because the speech-in adapter is
        # started later — see `listen()`.
        self.voice = None
        self._utterance = 0
        # Whether to stop listening while speaking. True for an adapter that
        # owns the speaker but cannot cancel it out of the microphone: it
        # hears its own voice otherwise, transcribes it, answers it, and hears
        # that. The device held a conversation with itself for four turns and
        # paid a language model for each one.
        #
        # Deafness while speaking is what half duplex *means*, and it is a
        # real cost: you cannot interrupt it. That is why it is tied to the
        # adapter's own `barge_in` and lifts by itself the moment an adapter
        # can cancel — nothing here needs changing when libspeexdsp arrives.
        self.half_duplex = False

    async def say(self, result):
        if result is None:
            return ""
        if result.get("type") == "failed":
            text = "I couldn't do that: %s" % (result.get("message")
                                               or result.get("kind"))
        else:
            text = result.get("say", "")

        # Any card still counting down belongs to the answer this one is
        # replacing. Cancel first: an expiry that fires late would reach
        # through and take down the answer that has just gone up.
        self._cancel_expiry()

        oid = self.p.result(result)
        marks = await self._marks_for(text)
        if marks:
            self.p.speak(marks)
            await self._deafen(True)
            try:
                await self._until_spoken(marks)
            finally:
                # In a finally because barge-in cancels this, and a device
                # that stays deaf after an interrupted sentence is worse than
                # one that never spoke.
                await self._deafen(False)
        # Armed only now, so the countdown starts when the answer has finished
        # being spoken rather than when it appeared. A long answer would
        # otherwise spend most of its ten seconds still being read aloud.
        if oid:
            self._arm_expiry(oid, result.get("linger", table.DEFAULT_LINGER))
        if self.echo or not (marks or self.p.a.connected):
            # Never silently succeed at nothing: if neither port took it, it
            # still has to go somewhere a person can see.
            print(text, flush=True)
        return text

    async def _deafen(self, on):
        """Stop or resume listening, for a device that cannot do both."""
        if not (self.half_duplex and self.voice is not None):
            return
        try:
            await self.voice.listen(not on)
        except Exception as e:                                # noqa: BLE001
            # Never fail an answer over the microphone. The worst case is a
            # device that hears itself, which is where this started.
            print("(could not %s the microphone: %s)"
                  % ("mute" if on else "unmute", e), file=sys.stderr, flush=True)

    async def _marks_for(self, text):
        """Marks from whichever half of the speech port is configured.

        **The adapter holding the microphone speaks, when there is one.** audi
        keeps the played samples because they are the echo canceller's
        reference; a second process playing audio out of band would leave the
        canceller with nothing to subtract, and the device would hear its own
        voice, answer it, and hear that.

        So a standalone `speech_adapter` is the fallback, for a deployment
        whose perception adapter has no voice — not the other way round.
        """
        if not text:
            return None
        if self.voice is not None:
            self._utterance += 1
            return await self.voice.say(text, "u%d" % self._utterance)
        if self.speech is not None:
            return await self.speech.marks(text)
        return None

    def _cancel_expiry(self):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None

    def _arm_expiry(self, oid, seconds):
        """Take the answer down after `seconds`, unless something replaces it.

        `linger = 0` means no timer at all: the card stays until the next
        answer arrives. That is the behaviour every card had before this
        existed, and it remains right for anything a person is meant to act on
        rather than read — a timer that has finished, most obviously.
        """
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = table.DEFAULT_LINGER
        if seconds <= 0:
            return

        async def countdown():
            try:
                await asyncio.sleep(seconds)
                self.p.expire(oid)
            except asyncio.CancelledError:
                pass                     # replaced before its time was up

        self._expiry = asyncio.ensure_future(countdown())

    async def _until_spoken(self, marks):
        """Stay in `speaking` for as long as speaking takes.

        Without this the turn reached `idle` in the same millisecond the
        `speak` went out — and avatari's `idle` calls audio_stop() and
        viseme_stop(), so cogiti was cancelling its own utterance 120 ms
        before it was due to start. The mouth never moved and nothing was
        heard, on a path where every individual message was correct.

        Being a real wait is also what makes `speaking` a state rather than a
        label: an interruption arriving now cancels this, which is barge-in.
        """
        duration = marks.get("seconds")
        if not duration:
            v = marks.get("visemes") or [[0.0]]
            duration = v[-1][0]
        start = marks.get("audio_start_ns") or 0
        lead = max(0.0, (start - time.clock_gettime_ns(time.CLOCK_MONOTONIC)) / 1e9)
        # The mouth releases back to the expression layer about 150 ms after
        # the last viseme, so going idle exactly on the last mark would clip it.
        try:
            await asyncio.sleep(lead + duration + 0.15)
        except asyncio.CancelledError:
            # Barge-in, or a new question typed over the answer. Stop the mouth
            # and the audio now rather than letting them run under whatever
            # comes next; ports.md fixes this order.
            self.p.stop()
            raise

    def on_state(self, state):
        """The face is a status display for the turn, not a printer that runs
        once at the end — architecture.md §3."""
        if state == "resolving":
            self.p.busy(True)
            self.p.expression("listening")
        elif state == "thinking":
            self.p.expression("thinking")
        elif state == "idle":
            self.p.busy(False)
            self.p.idle()

    def on_thought(self, text):
        self.p.thought(text)

    def barge_in(self):
        """Someone started speaking over us. Stop the mouth now.

        Synchronous and unconditional: this runs on the interrupt path, and
        anything awaited here is time the face spends still talking.
        """
        self.p.stop()


class Cogiti:
    def __init__(self, cfg):
        self.config = cfg
        self.output_kind = _config.require_output(cfg)
        self.output = self._build_output(cfg)

        state = cfg["state_dir"]
        os.makedirs(state, exist_ok=True)
        self.db = _db.open_db(os.path.join(state, "jobs.db"))
        self.trace = Trace(cfg["trace_file"])

        self.agent_argv = cfg["agent_adapter"].split()
        if not self.agent_argv:
            raise _config.ConfigError(
                "agent_adapter is not set. It is a required port: cogiti has no "
                "default, because naming one would mean having an opinion about "
                "which model runs.")

        # The fast path. Both optional: cogiti with neither escalates
        # everything, which ports.md says is a valid deployment and is how it
        # ran before this existed.
        self.templates = {}
        self.speech_in = None
        self.timers = timers.Timers(self)
        self.resolver = self._build_resolver(cfg)
        self.table = self._build_table(cfg)

        self.sessions = {}

    def _build_resolver(self, cfg):
        lib, blob = cfg["resolver_library"], cfg["resolver_blob"]
        if not lib and not blob:
            return None
        if not (lib and blob):
            raise _config.ConfigError(
                "resolver_library and resolver_blob go together; only one is set")
        return resolver.Resolver(
            lib, blob, config=cfg["resolver_config"] or None,
            device_location=cfg["device_location"] or None)

    def _build_table(self, cfg):
        if not cfg["command_table"]:
            return None
        providers.load_all()
        t = table.load(cfg["command_table"])
        self.templates = templates.load_dir(cfg["presentation_dir"])
        missing = sorted({c.present for c in t.commands.values()
                          if c.present and c.present != "none"
                          and c.present not in self.templates
                          and "{" not in c.present})
        if missing:
            # At load, not at first use. A card that is wrong is wrong the day
            # it is written, and finding out when someone finally asks about
            # the weather is finding out in the worst place.
            raise table.TableError(
                "commands name presentation templates that do not exist: %s"
                % ", ".join(missing))
        return t

    def resolve(self, text):
        """The fast path, or None when there is no resolver."""
        if self.resolver is None:
            return None
        return self.resolver.resolve(text)

    async def start_job(self, cmd, decision, session_id):
        """An intent that outlives its turn. Only timers, for now."""
        args, _prov = cmd.bind(decision)
        if cmd.job == "cancel_timer":
            return await self.cancel_job(cmd, decision, session_id)
        if cmd.job == "timer":
            seconds = int(args.get("duration") or 0)
            if seconds <= 0:
                return {"type": "result", "say": "I need a length for that."}

            async def fire(job_id, title):
                await self.announce(cmd, {"title": title,
                                          "duration_human": timers.human(seconds)})

            _job_id, title = self.timers.set(session_id, seconds, fire)
            values = dict(args, title=title,
                          duration_human=timers.human(seconds))
            return {"type": "result", "say": table.render(cmd.speak, values),
                    "did": ["started %s" % title]}
        return {"type": "failed", "kind": "table",
                "message": "no runner for job kind %r" % cmd.job}

    async def cancel_job(self, cmd, _decision, _session_id):
        """"Stop the timer." Selection is contextual, and ambiguity is a
        question rather than a guess.

        `jobs.md` §6: nobody says "cancel job 01J8ZQ". They say "stop that",
        "cancel the repository thing", "never mind" — so the reference is to
        the only one running, or the most recent, and **cancelling the wrong
        job is a small disaster**. With more than one candidate this says so
        and stops. Picking the newest would be right most of the time, and the
        times it was wrong would be exactly the times it mattered.

        Choosing from a list is the real answer and is not built; until it is,
        saying "there are two" is honest and a guess would not be.
        """
        live = timers.running(self.db)
        if not live:
            return {"type": "result", "say": "There's no timer running."}
        if len(live) > 1:
            return {"type": "result",
                    "say": "There are %d timers running — which one?"
                           % len(live),
                    "did": ["asked which of %d" % len(live)]}
        job = live[0]
        self.timers.cancel(job["id"])
        return {"type": "result",
                "say": table.render(cmd.speak, {"title": job["title"]}),
                "did": ["cancelled %s" % job["title"]]}

    async def announce(self, cmd, values):
        """Say something nobody asked for, right now.

        The first output that does not belong to a turn. Deliberately the
        dumbest possible version: one line, when the job finishes, and never at
        any other moment. Whether it is a *good* moment to speak is stage 12's
        subject and wants a person's attention and a load model, neither of
        which exists — so this does not pretend to have an opinion.
        """
        text = table.render(cmd.announce or "That's done.", values)
        await self.output.say({"type": "result", "say": text,
                               "show": text})

    async def run_command(self, cmd, decision):
        """A resolved command, run as a local effect.

        In a thread, not on the loop: a provider is synchronous by contract and
        may block for up to its timeout. architecture.md §1 says the loop is a
        router that does not compute — and a provider that stalls the loop
        would stall the barge-in that is supposed to interrupt it.
        """
        fn = providers.get(cmd.provider)
        try:
            args, provenance = cmd.bind(decision)
        except table.TableError as e:
            return {"type": "failed", "kind": "table", "message": str(e)}

        call = dict(args)
        if cmd.command:
            call["_command"] = cmd.command
        if cmd.source:
            call["_source"] = cmd.source

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(fn, **call), cmd.timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            result = providers.Result.failed("timeout")
        except Exception as e:                                # noqa: BLE001
            return {"type": "failed", "kind": "provider", "message": str(e)}

        if not result.ok:
            return {"type": "result", "say": table.apology(result.reason),
                    "did": ["tried %s" % cmd.provider]}

        values = dict(args, **result.values)
        values.update({"_provenance": provenance})
        out = {"type": "result", "say": table.render(cmd.speak, values),
               "linger": cmd.linger}
        if cmd.present and cmd.present != "none":
            tpl = self.templates.get(cmd.present)
            # A named template renders to ops; anything else is a plain line,
            # which is what a device with a one-line display wants and is not
            # worth a file of its own.
            out["show"] = (tpl.ops(values) if tpl
                           else table.render(cmd.present, values))
        return out

    def _build_output(self, cfg):
        """Which ports actually answer. `output = text` stays a legitimate
        choice rather than a fallback, so 'no way to reach the user' remains a
        startup failure and never a surprise."""
        socket_path = cfg["presentation_adapter"]
        speech_argv = cfg["speech_adapter"].split()
        if not socket_path and not speech_argv:
            return TextOutput()

        warn = lambda m: print("(%s)" % m, file=sys.stderr, flush=True)
        adapter = presentation.Presentation(socket_path, on_warn=warn)
        speech = None
        if speech_argv:
            speech = speech_mod.Speech(
                speech_argv, on_warn=warn,
                env=secrets.env_for(cfg["state_dir"],
                                    cfg.secret_grants("speech_secrets")))
        return FaceOutput(present.Presenter(adapter), speech,
                          echo=(cfg["output"] == "text"))

    async def listen(self, argv, session=None):
        """Wire a speech adapter to a session, for the life of the process.

        The adapter is a device, not a job: it is started once, restarted if it
        dies, and stopped when cogiti stops.
        """
        s = session or self.session()
        self.speech_in = audi.Speech(
            argv,
            on_warn=lambda m: print("(%s)" % m, file=sys.stderr, flush=True),
            on_speech_start=s.heard_start,
            on_partial=s.heard_partial,
            on_final=lambda text, ms: s.heard(text),
        )
        caps = await self.speech_in.capabilities()

        # One adapter, both halves of the port: it hears and it speaks. The
        # output was reaching for a separate `speech_adapter` that this
        # deployment does not configure, so every answer was drawn and none
        # was spoken — each part working, and nothing joining them.
        #
        # `speaks` and not the presence of a --tts flag: audi claims it only
        # when it has both a voice and a speaker it can actually open, which
        # is the difference between configured and working.
        if caps.get("speaks") and isinstance(self.output, FaceOutput):
            self.output.voice = self.speech_in
            # It speaks but cannot cancel itself out of its own microphone,
            # so it must not be listening while it does.
            self.output.half_duplex = not caps.get("barge_in")
        # Partials are required; barge-in is not. A half-duplex device that
        # finishes its sentence is a valid appliance, and refusing to start
        # for it would be cogiti having an opinion about someone's hardware.
        self.speech_in.require(["partials"])
        await self.speech_in.start()
        return caps

    async def start(self):
        # Orphan recovery before anything else runs, so the table never claims
        # a process that is not there.
        orphaned = jobs.recover(self.db)
        if orphaned:
            print("(%d job(s) from a previous run marked orphaned)" % len(orphaned),
                  file=sys.stderr)

        # Capabilities are probed once, here, and a missing one stops startup
        # rather than surfacing at the first escalation.
        caps = await agent.capabilities(self.agent_argv)
        agent.require(caps, ["tools"])
        return caps

    def session(self, speaker="unknown", thread="main"):
        key = (speaker, thread)
        if key not in self.sessions:
            self.sessions[key] = Session(self, speaker, thread)
        return self.sessions[key]


async def repl(c):
    loop = asyncio.get_event_loop()
    s = c.session()
    pending = None
    interactive = sys.stdin.isatty()

    async def settle():
        """Scripted input only: let the turn get somewhere before reading on.

        It ends on either of two things, and the second is the one that
        matters: the turn finished, *or* it started waiting for an answer.
        Waiting only for the first means nothing reads stdin while a confirm
        is pending, so the answer sitting in the pipe is never delivered and
        the question dies of its timeout — after which the "no" resolves as a
        fresh utterance and turns out to mean `mute`.

        A poll rather than an event because this is a development entry point
        and 10 ms of latency in a REPL buys nothing worth the plumbing.
        """
        while pending and not pending.done() and not s.awaiting_answer():
            await asyncio.sleep(0.01)

    def _report(task):
        """A dispatched turn's failure has nowhere to surface on its own."""
        if task.cancelled():
            return
        e = task.exception()
        if e is not None:
            print("(turn failed: %r)" % e, file=sys.stderr, flush=True)

    print("cogiti — type to ask, ctrl-d to leave", flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        # A pending question takes the next thing typed. Nothing else can be
        # meant by it, and treating it as a new utterance would abandon the
        # turn that is waiting.
        if s.awaiting_answer():
            await s.answer(line)
            await settle()
            continue

        pending = asyncio.ensure_future(s.utterance(line))
        pending.add_done_callback(_report)

        # A person and a script want opposite things from the same code.
        #
        # At a terminal, typing over an answer is the typed form of barge-in:
        # the turn is dispatched, reading continues, and the next line
        # interrupts. Awaiting here instead would mean nothing reads stdin
        # while a turn runs, so a confirm could never be answered and would
        # always die of its timeout.
        #
        # Piped in, every line arrives at once. Interrupting on each would
        # cancel every turn before it spoke — which it did: a six-line session
        # produced no output at all. A script means "these, in order".
        if not interactive:
            await settle()

    # Input ended. A turn still waiting on an answer will never get one, so it
    # is cancelled rather than waited for — and a cancelled confirm is a
    # cancelled action, which is the same answer silence gives.
    if pending and not pending.done():
        if s.awaiting_answer():
            pending.cancel()
        try:
            await asyncio.wait_for(pending, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="cogiti", add_help=False)
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--help", "-h", action="store_true")
    # Where the config file is, which cannot itself come from the config file.
    # /etc/cogiti.conf is the appliance's; a checkout needs to name its own,
    # and the alternative is a pile of flags that drift from what ships.
    ap.add_argument("--conf", default="/etc/cogiti.conf")
    known, rest = ap.parse_known_args(argv)

    if known.help:
        print(__doc__.strip())
        print("\n  --conf PATH      the config file, default /etc/cogiti.conf")
        print("  --print-config   every setting, and who decided it")
        return 0

    cfg = _config.load(rest, conf_path=known.conf)
    if known.print_config:
        cfg.print_config()
        return 0

    c = Cogiti(cfg)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(c.start())
        if cfg["speech_in_adapter"]:
            loop.run_until_complete(c.listen(cfg["speech_in_adapter"].split()))

        # A device is not a terminal. Started by init with stdin on /dev/null,
        # the repl reads EOF on its first line, returns, and the process exits
        # — so the appliance's brain would stop within a second of booting,
        # taking the speech adapter with it, and the only symptom would be a
        # face that never answers.
        #
        # So: type at it when there is somebody to type, and otherwise just
        # run. Ears are enough of a reason to stay alive.
        if sys.stdin.isatty() or not cfg["speech_in_adapter"]:
            loop.run_until_complete(repl(c))
        else:
            print("cogiti — listening", flush=True)
            loop.run_forever()          # until a signal stops it
    finally:
        # Live timers are cancelled rather than left behind. Their `sleep`
        # processes would otherwise outlive cogiti with nothing to announce
        # them, and their rows would say `running` for ever — the orphan
        # `jobs.recover` exists to clean up, created deliberately.
        #
        # A timer that survives a restart needs somewhere to survive *to*, and
        # this deployment has no writable state that outlives an update yet.
        # Cancelling is the honest version of not having that.
        loop.run_until_complete(c.timers.shutdown())
        if c.speech_in:
            loop.run_until_complete(c.speech_in.close())
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
