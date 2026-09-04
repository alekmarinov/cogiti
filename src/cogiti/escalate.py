"""Prompt assembly -> a job with an agent driver.

`docs/architecture.md` §2. Escalation is what happens when the fast path did
not answer — here, always, because no resolver is configured.

It owns one decision the rest of cogiti depends on: **what a job is granted**.
Tools and hosts are decided here, before the job starts, from what the user
asked for — never widened later because something the agent read suggested it.
"""

import asyncio

from . import device_tool
from . import panels_tool
from . import secrets
from .adapters import agent


def device_grant(cogiti):
    """The device's own commands — all of them — as a tool the model may call.

    Without this an escalation could only talk about the device. "Turn it up a
    bit, would you" in a sentence the resolver does not match got an agreeable
    answer and no change in volume — the device knew how, and the part of it
    that was listening had no way to say so.

    There is no subset. An escalation happens precisely when somebody did not
    find the sentence the resolver wanted, so offering it less than the
    resolver can reach reproduces the original fault one level up. Consent is
    kept by asking, not by hiding: a command with a `confirm` puts that
    question to the person before it happens.
    """
    if cogiti.table is None:
        return None, {}
    offers = device_tool.offered(cogiti.table)
    if not offers:
        return None, {}
    return device_tool.tool(offers, device_tool.asks(cogiti.table)), offers


def grants(cogiti, text):
    """What this job may reach.

    A single rule for now, and it is deliberately blunt: a job gets the `http`
    tool with the deployment's configured hosts. The interesting version reads
    the utterance and grants less — a question about the weather has no
    business reaching a ticket tracker — and that is a policy decision with a
    registry behind it, not something to improvise here.

    `allow_private` is off. It is granted when the user asks for something
    local, and nothing here can tell yet.
    """
    hosts = cogiti.config.list("egress_hosts")
    granted = [{"name": "http", "hosts": hosts}]
    # Only where there is a screen. Offering it to a terminal deployment
    # would have the model composing panels nobody can see, and then saying
    # it had shown them.
    if getattr(getattr(cogiti, "output", None), "p", None) is not None:
        granted.append({"name": "display", "schema": panels_tool.tool()})
        granted.append({"name": "find_pictures",
                        "schema": panels_tool.picture_tool()})
    if cogiti.config["web_search"].strip().lower() in ("1", "true", "yes", "on"):
        # Named in the grant rather than assumed by the adapter, so it shows up
        # in the run's tool list and therefore in the dump: "did it search?"
        # should be answerable from the record, not by inference from the
        # wording of an answer.
        granted.append({"name": "web_search"})
    return granted, False


async def run(cogiti, session, turn):
    tools, allow_private = grants(cogiti, turn.text)

    prompt = {"text": turn.text, "context": session.context()}
    # What the fast path made of it, when it made something and was unsure.
    # A weak match mid-conversation now comes here instead of being asked
    # about, and arriving with the resolver's guess attached means nothing is
    # thrown away — the model can act on it or set it aside, where before it
    # was told only the words.
    d = turn.decision
    if (getattr(d, "intent_id", None)
            and getattr(d, "verdict", None) == "confirm"):
        prompt["resolver"] = {"guessed": d.intent_id,
                              "confidence": round(d.confidence or 0.0, 3)}
    budget = {"wall_ms": 120000}

    # The adapter's environment carries whatever credential it was granted; a
    # tool's carries none. Both are built, never inherited — a tool has no
    # business holding the key that talks to the model, and inheriting would
    # hand it every one cogiti's own shell happened to export.
    state = cogiti.config["state_dir"]
    env = secrets.env_for(state, cogiti.config.secret_grants())
    tool_env = secrets.env_for(state, {})

    def on_event(e):
        # While the turn lives its events belong to the turn; once it has
        # detached they belong to the job, whose row is still open.
        job = getattr(getattr(turn, "agent_run", None), "job_id", None)
        if job and turn.state.value == "idle":
            cogiti.trace.job_event(job, e)
        else:
            cogiti.trace.event(session, turn, e)
        # A thought is the only agent event with somewhere to go on a screen.
        # Routed here rather than inside the adapter because what is worth
        # showing is a presentation decision, and the adapter must not have
        # one.
        if e.get("type") == "say":
            # Straight to the speech port and nowhere else. `on_thought` goes
            # to a screen; this must not, and the whole safety of streaming
            # it rests on that separation — agent-protocol.md §7.
            text = (e.get("text") or "").strip()
            aloud = getattr(cogiti.output, "say_aloud", None)
            if text and aloud:
                turn.spoke = True
                asyncio.ensure_future(aloud(text))
            return
        if e.get("type") == "thought":
            hook = getattr(cogiti.output, "on_thought", None)
            if hook:
                hook(e.get("text", ""))

    run = agent.AgentRun(cogiti.db, cogiti.agent_argv, "%s/%s" % session.key,
                         on_event=on_event, env=env, tool_env=tool_env)

    # The device itself, as a tool. Answered here rather than run as a
    # subprocess: a command is a function call away and spawning a process to
    # ask the time would be absurd.
    device, offers = device_grant(cogiti)
    if device is not None:
        tools = list(tools) + [device]
        run.local_tools["device"] = (
            lambda args: device_tool.run(cogiti, offers, args,
                                         "%s/%s" % session.key, turn))

    # Answered here for the same reason `device` is: fetching a picture is a
    # function call away, and spawning a process to do it would be absurd.
    if any(t.get("name") == "display" for t in tools):
        run.local_tools["display"] = lambda args: panels_tool.run(cogiti, args)
        run.local_tools["find_pictures"] = (
            lambda args: panels_tool.find(cogiti, args))

    # The turn keeps a handle on it, because a turn that stops waiting still
    # has to be able to name what it stopped waiting for. Without this the
    # detached job was tracked under its asyncio task name — which cancelling
    # never matches, so stopping a job would not have stopped its answer
    # arriving anyway a minute later.
    turn.agent_run = run

    # A question from the adapter is a question for the person, now that there
    # is one. The broker answered 'nobody available' while cogiti had no user
    # loop; that was right then and is wrong now.
    run.ask_user = lambda q: turn.ask(q)

    return await run.run(prompt, tools, budget, allow_private=allow_private)
