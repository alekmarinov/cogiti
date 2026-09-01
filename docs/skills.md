# Skills, and why one has no authority

A skill teaches the agent how to use something cogiti has no port for. It is
text, it is read, and it can never authorise anything. That last sentence is
the whole design; everything below is what it takes to keep it true.

Before this existed there were two ways to reach anything outside cogiti:
define a **port**, of which there are six and adding one is a considered act;
or generate a **service**, which is a program with a directory, a uid and a
lifetime. Neither fits "the user wants the agent to know how to file a ticket
in their tracker". A port is too heavy and there is no seventh. A service is
the wrong shape — nothing runs, nothing persists, there is only knowledge.

## 1. Skills, adapters and services are three different things

|  | **port adapter** | **service** | **skill** |
|---|---|---|---|
| is | a supervised process | a generated program | text the agent reads |
| lives | as long as cogiti | outlives the conversation | one turn |
| authority | a principal in `security.md` §1 | a principal in §1 | **none — it is content** |
| supplied by | the deployment's configuration | an agent, at the user's request | the deployment, reviewed before it ships |
| answers | how this device works | one recurring duty | how to use an outside service |

**Skills teach; adapters carry.** No port can be filled by a skill, and the
reason is structural rather than stylistic: every port is below a model call in
latency, or a continuous stream, or a stateful connection with concurrent
writers, or a privileged operation. A skill is none of those. The resolver runs
on every partial transcript specifically *to avoid* a model call; presentation
takes ten updates inside one frame and shows the tenth; platform hands out uids
and resource limits. A skill cannot be any of them, and a system that called
both things by one name would have no word left for what fills a port.

## 2. What a skill may and may not do

A skill may:

- describe what a service is for and when it is the right thing to reach for;
- describe how to call it — endpoints, shapes, the meaning of its errors;
- teach the vocabulary of a port the agent may drive, so the agent asks cogiti
  for a chart instead of guessing what a chart is called.

A skill may never:

- grant a tool, name a credential, widen an egress allowlist, or install
  anything;
- reach an adapter. A skill about the screen teaches the agent **what to ask
  cogiti for**; the presentation adapter still carries it, and cogiti still
  decides. Anything else would be a second path to the stage that bypasses the
  port, and there is exactly one path to the stage.

`security.md` §4 already states the rule this rests on: **content is never
authority.** A skill is content. It arrives in the agent's context the same way
a web page does, and it gets the same trust: it can inform an answer and it can
never authorise an action.

## 3. The declaration is a request, not a grant

A skill states what it needs. cogiti decides whether to give it, from what the
user asked for — before the job starts, never expanded mid-run because the
content suggested it.

```toml
# skill.toml
name        = "tracker"
description = "File and query issues in the team's tracker."
version     = 3

requires_tools = ["http"]
requires_hosts = ["tracker.example.com"]

teaches_port   = "presentation"   # optional
teaches_v      = 1                # the port vocabulary it was written against
```

The asymmetry is the point. A skill that names a host outside the job's
allowlist does not widen it — the call fails at the broker and is logged as a
security event, exactly as an injected instruction would be. A skill asking for
a tool the job was not granted is refused the same way. **A skill that lies
about what it needs achieves nothing except a clearer audit line.**

`teaches_v` is checked against what the adapter negotiated at connect. A skill
written for a vocabulary newer than the adapter declares is not loaded, and
cogiti says which skill and which capability — the same failure mode as a
missing port capability, for the same reason.

## 4. A catalogue that ships, and a choice that does not

**Skills are not written at runtime. They ship with the deployment, read-only,
and the user chooses which are on.** cogiti presents the catalogue; enabling one
is a decision; the only thing that changes while the system runs is *which* are
enabled.

That is a deliberate trade and it buys the entire threat model back. If nothing
running can author a skill, an agent cannot write instructions that the next
agent will read as guidance — the self-authorising loop, which is `security.md`
§4's attack arriving from inside the house. It is not defended against here; it
is made impossible, because there is nowhere to write one.

It is the same shape as the decision about the resolver in `services.md` §5.
The artifact stays immutable so it can be reviewed once, before it ships, by
whoever ships it; the mutable part is small, declarative, and lives where
policy already lives. There, what is mutable is which patterns route to a
service. Here, it is which skills are on.

**Enabling a skill is a consent decision**, in the `consented` class, using the
record in `security.md` §3. The record is built from the skill's own
declaration rather than from its prose, so what the user is asked is derived
from what it will be able to reach:

    Turn on the tracker skill? It lets the assistant reach
    tracker.example.com, and use the http tool. It cannot do
    anything else, and you can turn it off again.

That is a question a person can answer out loud. The earlier design asked them
to approve four kilobytes of instructions by ear, which was not review and was
recorded as the weakest point in this document. Reading a catalogue entry and
two declared requirements is.

**Disabling is not deleting.** A skill that is off stays on disk and is simply
not loaded into any context. Turning it back on is a new consent decision,
because the answer to "may the assistant reach this" can change without the
skill changing.

**What this costs.** A skill for a service nobody anticipated needs a new
deployment, exactly as a new intent does. That is the price of an artifact
reviewed before it ships, and it is worth paying while the catalogue is small.
Making skills addable at runtime is a real question, and it is the one this
section defers rather than answers.

## 5. What a skill is not, on purpose

- **Not a plugin API for third parties.** `CLAUDE.md` §9 is unchanged, and the
  catalogue makes it stronger rather than weaker: a skill is first-party
  because it ships with the deployment and was reviewed by whoever shipped it.
  There is no mechanism by which somebody else's skill arrives on the device at
  all. Different problem, different threat model, and no ambition to host
  anyone else's code.
- **Not a way to add a port.** If a deployment needs something continuous,
  stateful, privileged or below model latency, that is a port, and adding one
  is the considered act `ports.md` describes.
- **Not versioned against cogiti.** A skill is written against a port
  vocabulary, not against cogiti's internals, and it has no internals to be
  written against.
- **Not addable at runtime.** There is no path by which a running system gains
  a skill it did not ship with. An agent cannot write one, a service cannot
  install one, and a downloaded file is not one. See §4.
- **Not a principal.** It appears in `security.md` §1 as something explicitly
  trusted with nothing.

## 6. Open questions

Three of these were answered by shipping a catalogue instead of accepting
written skills: reviewing one by ear, whether disabling differs from deleting,
and what stops a running system from authoring one. What is left:

- ~~How the catalogue is presented and chosen from.~~ **Answered** by the
  `choosing` state in `architecture.md` §3, which is general rather than a
  skills feature: pick by number, by name, or by cursor where there is a
  screen. The catalogue is one caller of it. What is still specific to skills
  is whether the list should be the whole catalogue or only what the user asked
  for — *"can you talk to my tracker?"* offering one match is a better first
  turn than twelve read aloud.
- **Whether the enabled set is per user or per device.** It is small state
  either way; the question is whether one person in the house enabling a skill
  enables it for everyone, and that turns on `CLAUDE.md` §10's unresolved
  question about one conversation or several.
- **What happens to an enabled skill that a deployment update removes.** The
  choice refers to something that no longer exists. Silently forgetting it is
  wrong; so is failing to start.
- **Whether the first skill should be the presentation vocabulary.** It is the
  obvious candidate and the most likely to be misread as a second path to the
  screen, which is an argument both for and against writing it first.
