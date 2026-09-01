"""Prompt assembly -> a job with an agent driver.

`docs/architecture.md` §2. Escalation is what happens when the fast path did
not answer — here, always, because no resolver is configured.

It owns one decision the rest of cogiti depends on: **what a job is granted**.
Tools and hosts are decided here, before the job starts, from what the user
asked for — never widened later because something the agent read suggested it.
"""

from . import secrets
from .adapters import agent


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
    return [{"name": "http", "hosts": hosts}], False


async def run(cogiti, session, turn):
    tools, allow_private = grants(cogiti, turn.text)

    prompt = {"text": turn.text, "context": session.context()}
    budget = {"wall_ms": 120000}

    # The adapter's environment carries whatever credential it was granted; a
    # tool's carries none. Both are built, never inherited — a tool has no
    # business holding the key that talks to the model, and inheriting would
    # hand it every one cogiti's own shell happened to export.
    state = cogiti.config["state_dir"]
    env = secrets.env_for(state, cogiti.config.secret_grants())
    tool_env = secrets.env_for(state, {})

    run = agent.AgentRun(cogiti.db, cogiti.agent_argv, "%s/%s" % session.key,
                         on_event=lambda e: cogiti.trace.event(session, turn, e),
                         env=env, tool_env=tool_env)

    # A question from the adapter is a question for the person, now that there
    # is one. The broker answered 'nobody available' while cogiti had no user
    # loop; that was right then and is wrong now.
    run.ask_user = lambda q: turn.ask(q)

    return await run.run(prompt, tools, budget, allow_private=allow_private)
