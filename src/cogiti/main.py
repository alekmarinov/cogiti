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
from . import broker as _broker
from . import detach
from . import jobs
from .adapters import agent, audi, presentation, resolver
from . import present
from . import presentation_templates as templates
from . import providers
from . import secrets
from . import services as _services
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
        # Escalations that outlived their turn, and answers waiting for a
        # moment to be said. See detach.py.
        self.pending = detach.Pending()

        # Standing duties. The directory is the truth (services.md §2), so
        # this holds no state that outlives a restart — it reads what is there.
        self.services_root = os.path.join(state, "services")
        self.removed_root = os.path.join(state, "removed")
        self.broker_path = os.path.join(state, "broker.sock")
        self.services = _services.Services(
            self.services_root,
            on_warn=lambda m: print("(%s)" % m, file=sys.stderr, flush=True),
            broker=self.broker_path)
        self.broker = None
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
        handler = {
            "what_are_you_doing": self.what_are_you_doing,
            "list_jobs": self.list_jobs,
            "job_status": self.job_status,
            "job_logs": self.job_logs,
            "cancel_job": self.cancel_a_job,
            "list_services": self.list_services,
            "service_status": self.service_status,
            "pause_service": self.pause_service,
            "remove_service": self.remove_service,
        }.get(cmd.job)
        if handler is not None:
            return await handler(cmd, decision, session_id)
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

    # ------------------------------------------------- talking about work --
    #
    # These read and act on the registry, so they run here on the loop rather
    # than as providers: a provider is called in a worker thread, and the
    # sqlite connection is single threaded by construction. Snapshotting rows
    # for a provider would work for the three that only read, and not for the
    # one that cancels — so all four live together instead of two of them
    # being somewhere else for a reason nobody would remember.

    def _live(self, exclude_timers=True):
        """What is running, newest first, as plain rows.

        Timers are excluded because they are jobs in the registry but not work
        in the sense anybody means when they ask what the device is doing.
        "What are you doing" answered with "a timer" is a wrong answer to the
        question that was asked.
        """
        rows = [dict(r) for r in _db.live_jobs(self.db)]
        if exclude_timers:
            rows = [r for r in rows if r["kind"] != "timer"]
        rows.sort(key=lambda r: r["created_ns"], reverse=True)
        return rows

    async def what_are_you_doing(self, _cmd, _decision, _session_id):
        """Small talk, unless something is running.

        The resolver keeps this separate from list_jobs precisely so that this
        answer can be "nothing" — the resolver cannot know, and a device that
        reports on its scheduler when asked how it is doing is answering a
        question nobody asked.
        """
        live = self._live()
        if not live:
            return {"type": "result", "say": "Nothing at the moment.",
                    "linger": 8}
        if len(live) == 1:
            return {"type": "result",
                    "say": "I'm still working on %s." % live[0]["title"],
                    "linger": 8}
        return {"type": "result",
                "say": "I've got %d things going: %s."
                       % (len(live), ", ".join(r["title"] for r in live[:3])),
                "linger": 8}

    async def list_jobs(self, _cmd, _decision, _session_id):
        live = self._live()
        if not live:
            return {"type": "result", "say": "Nothing is running.", "linger": 8}
        lines = ["%s — %s" % (r["title"], r["state"]) for r in live]
        return {"type": "result",
                "say": "%d running: %s." % (len(live),
                                            ", ".join(r["title"] for r in live)),
                "show": "\n".join(lines),
                # Zero: a list of what is running is something to act on, not
                # something to read once. It stays until it is replaced.
                "linger": 0}

    async def job_status(self, _cmd, _decision, _session_id):
        live = self._live()
        if not live:
            return {"type": "result", "say": "Nothing is running just now.",
                    "linger": 8}
        r = live[0]
        age = max(0, (time.monotonic_ns() - r["created_ns"]) // 1_000_000_000)
        progress = r["progress"] or r["state"]
        return {"type": "result",
                "say": "%s: %s, %s so far." % (r["title"], progress,
                                               timers.human(int(age))),
                "linger": 12}

    async def job_logs(self, _cmd, _decision, _session_id):
        """The last few lines, on the screen rather than read aloud.

        Nobody wants a build log spoken. docs/jobs.md §4: a running job's log
        is `attention: watch` — external, something happening to the system —
        which is what a stream is for, and it is shown rather than said.
        """
        live = self._live()
        if not live:
            return {"type": "result", "say": "Nothing is running.", "linger": 8}
        r = live[0]
        lines = [row["line"] for row in _db.tail_log(self.db, r["id"], 12)]
        if not lines:
            return {"type": "result",
                    "say": "%s hasn't said anything yet." % r["title"],
                    "linger": 8}
        return {"type": "result",
                "say": "Showing the last %d lines." % len(lines),
                "show": {"id": "brain/joblog", "kind": "stream",
                         "append": "\n".join(lines), "lines": 12,
                         "style": "caption", "attention": "watch",
                         "fallback": "%s: %d lines" % (r["title"], len(lines))},
                "linger": 0}

    async def cancel_a_job(self, _cmd, _decision, _session_id):
        """Stop something that is running. Ambiguity is a question.

        The intent is `confirm` in the registry, so the user has already been
        asked before this runs. What is left is *which* — and with more than
        one candidate this says so rather than picking the newest. Picking
        would be right most of the time, and wrong exactly when it mattered.
        """
        live = self._live()
        if not live:
            return {"type": "result", "say": "There's nothing to stop.",
                    "linger": 8}
        if len(live) > 1:
            return {"type": "result",
                    "say": "There are %d things running — which one?"
                           % len(live),
                    "did": ["asked which of %d" % len(live)],
                    "linger": 0}
        r = live[0]
        await jobs.cancel_async(self.db, r["id"], "user")
        self.pending.drop(r["id"])
        return {"type": "result", "say": "Stopped %s." % r["title"],
                "did": ["cancelled %s" % r["title"]], "linger": 8}

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

        # Services last, and after the adapters — services.md §6. Not
        # sequenced against the renderer on purpose: the SDK reconnects, so a
        # service starting first is a thing to survive rather than to order,
        # and ordering it would make the boot as slow as its slowest member.
        installed = self.services.load()
        if installed:
            self.broker = _broker.Broker(
                self.broker_path,
                {n: sv.m for n, sv in self.services.services.items()},
                on_warn=lambda m: print("(%s)" % m, file=sys.stderr, flush=True))
            await self.broker.start()
            await self.services.start_all()
            self._sampler = asyncio.ensure_future(self.services.run_sampler())
            print("services: %s" % ", ".join(installed), file=sys.stderr,
                  flush=True)
        return caps

    # ------------------------------------------------------- the pinned --

    def _svc(self):
        return list(self.services.services.values())

    async def list_services(self, _cmd, _decision, _session_id):
        live = self._svc()
        if not live:
            return {"type": "result", "say": "Nothing is pinned.", "linger": 8}
        return {"type": "result",
                "say": "%d pinned: %s." % (len(live),
                                           ", ".join(s.m.title for s in live)),
                "show": "\n".join("%s — %s" % (s.m.title, s.state)
                                   for s in live),
                "linger": 0}

    async def service_status(self, _cmd, _decision, _session_id):
        """§9: a service that has been broken for a day and said nothing is
        the failure this whole design is trying to avoid. So the broken ones
        are named first, and 'everything is fine' is only said when it is."""
        live = self._svc()
        if not live:
            return {"type": "result", "say": "Nothing is pinned.", "linger": 8}
        bad = [s for s in live if s.state == _services.NEEDS_ATTENTION]
        if not bad:
            return {"type": "result",
                    "say": "All %d are running." % len(live), "linger": 8}
        s = bad[0]
        return {"type": "result",
                "say": "%s has stopped: %s." % (s.m.title, s.detail or "it kept failing"),
                "linger": 0}

    async def pause_service(self, _cmd, _decision, _session_id):
        live = [s for s in self._svc() if s.alive]
        if not live:
            return {"type": "result", "say": "Nothing is running to pause.",
                    "linger": 8}
        if len(live) > 1:
            return {"type": "result",
                    "say": "There are %d — which one?" % len(live),
                    "linger": 0}
        await self.services.stop(live[0].m.name, _services.PAUSED)
        return {"type": "result", "say": "Paused %s." % live[0].m.title,
                "linger": 8}

    async def remove_service(self, _cmd, _decision, _session_id):
        live = self._svc()
        if not live:
            return {"type": "result", "say": "There's nothing to remove.",
                    "linger": 8}
        if len(live) > 1:
            return {"type": "result",
                    "say": "There are %d — which one?" % len(live),
                    "linger": 0}
        title = await self.services.remove(live[0].m.name, self.removed_root)
        # §7.4: say what went, by title, so a misheard removal is caught now
        # rather than in a month when somebody misses it.
        return {"type": "result",
                "say": "Removed %s. I've kept it for thirty days." % title,
                "did": ["removed %s" % title], "linger": 12}

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
