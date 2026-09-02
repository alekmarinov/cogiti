# Ports

cogiti orchestrates a system it does not contain. Everything it needs from the
outside world arrives through one of six ports, and **cogiti names no
implementation of any of them.** An adapter is configuration.

This is the document that decides whether cogiti stays general. When something
specific to one deployment appears anywhere else in this repository, it belongs
here as a port, or it belongs in that deployment.

## The six

| port | what it gives cogiti | required |
|---|---|---|
| **resolver** | an utterance becomes a decision, fast, without a model | no |
| **presentation** | structured results become something a person can see | no |
| **speech** | audio in becomes text; text becomes audio and mouth timing | no |
| **perception** | who is present, what is in front of the device | no |
| **agent** | an assembled prompt becomes a structured answer | **yes** |
| **platform** | processes are supervised, state persists, services install | **yes** |

Only two are required, and that is deliberate. A cogiti with an agent and a
platform is a usable text-driven orchestrator. Everything else is a capability
it discovers it has, announces, and does without when absent — a device with no
screen still speaks, a device with no microphone still reads a socket.

**Degradation is announced, never silent.** An unavailable port produces a
spoken or printed sentence saying what cannot be done, not a hang and not a
quiet omission.

## The shape every port shares

- **A process boundary with newline-delimited JSON**, one message per line,
  over a Unix socket or a pipe — with two exceptions, below. One framing, one
  serialisation, one set of limits in the whole system.
- **Capability negotiation at connect.** The adapter states what it supports;
  cogiti asserts what its configuration requires and **fails loudly at startup
  naming the missing capability.** A deployment that silently rendered a
  fallback instead of a chart for a month is the failure this prevents.
- **cogiti reconnects, with backoff, and re-declares.** An adapter is allowed
  to restart. cogiti holds the intent; the adapter holds nothing across a
  connection.
- **Versioned.** `"v": 1` on every message. Additive changes never bump.

**The resolver has no capability negotiation, because it has no connect.** The
other five declare what they support when they attach, and cogiti fails at
startup naming anything missing. A linked library never attaches, so that
mechanism does not exist for it and today nothing needs it: the contract is one
function and every resolver must provide all of it. The moment an optional
capability is proposed here — and one was, and was rejected in
`services.md` §5 — there is no way for cogiti to ask whether this resolver has
it. Adding one is a port change, not an implementation detail.

**The first exception is the resolver**, which is a **library, linked and
called inline**. It runs on every utterance and on every partial transcript, at
microsecond scale; a process boundary would cost more than the work. It is the
only port where that trade comes out this way, and it is the reason the port
list is not simply "six sockets".

**The second is the agent, which is spawned per use rather than connected to,
because its adapter *is* the job.** Two of the three rules above therefore read
differently for it, and both differences are deliberate:

- **Capabilities are probed once at cogiti's startup** (`adapter
  --capabilities`), not negotiated per run. Negotiating per job would pay for
  it on every escalation and would move a configuration error from startup to
  first use, which is the opposite of what the rule is for.
- **cogiti does not reconnect to it.** A dead agent is a failed job, never a
  retried one — a job may have had an effect before it died, and retrying an
  effect is the user's decision, not a supervisor's.

Nothing else differs: same framing, same `"v":1`, same limits. See
`docs/agent-protocol.md`.

## resolver

An utterance in; a decision out. Never an action, never a model call, never
the network.

```
resolve(utterance) -> {
    intent_id     string or null
    confidence    0..1
    verdict       handle | confirm | escalate
    tier          how it was matched — see below
    slots         [{name, value, text, defaulted}]
    missing_slot  set when the intent was recognised but a slot was not filled
}
```

Three properties cogiti depends on, and an adapter must provide all three:

- **`escalate` means "I could not resolve this"** and nothing more. What
  happens next is cogiti's decision alone.
- **A destructive intent never reaches `handle` on a similarity score.** It
  comes from a deterministic match or it carries `confirm`. cogiti will not
  auto-answer a `confirm`, but it cannot repair a resolver that guessed.
- **`tier` distinguishes a deterministic match from a scored one**, because
  cogiti acts on a partial transcript only for the former: a listed phrase
  cannot become something else with the next word, and a cosine can.

A `defaulted` slot must be marked. "The weather where you always are" and "the
weather in Sofia because you said Sofia" are different answers and cogiti has
to be able to tell them apart before it reads one back to someone.

*Any adapter that answers within the latency budget is valid.* A resolver that
always escalates is one, and is how cogiti runs with no fast path at all.

## presentation

Structured results become visible. cogiti sends **intent, never geometry** —
objects, identities, relationships and regions. Where things go belongs to the
adapter, which is the only party that knows the screen.

```
create/update  id, kind, content, region, lifetime
destroy        id
query          what is currently shown
```

What cogiti requires of an adapter:

- **Addressable objects with caller-chosen ids**, so a result can be updated
  and referred to later without a round trip.
- **Upsert**: `create` on an existing id updates it. A restarted client
  re-declares everything and needs to know nothing about what survived.
- **Ownership per connection**, so cogiti and a service can share one adapter
  without colliding, and either can restart without disturbing the other.
- **Two regions**: a conversational one cogiti owns, and a pinned one services
  own. Without that split, pinning something means the conversation shoves it
  aside every time it needs the screen.
- **Last-write-wins updates**, never queued. Ten prices arriving during one
  frame should show the tenth, not replay all ten.
- **Unknown kinds degrade** rather than fail: the object still exists, holds
  its place and shows a fallback.

*A 3D renderer, a terminal and a web page are equally valid adapters* — the
port says what may be shown, not what draws it. A deployment with no
presentation adapter speaks its results.

## speech

Two directions, one adapter, because they share a device and because barge-in
needs them to share a clock.

```
in    speech_start | partial | final | speech_end
out   say(text)  ->  audio, and timing marks for whatever draws a mouth
```

`speech_start` while cogiti is speaking is barge-in, and the order of the
response is fixed: stop the presentation adapter, stop the audio, *then*
listen. Any other order leaves the system talking over the person for a second,
which is the worst thing it can do.

**The adapter owns the speaker, not just the microphone**, and
`speech-protocol.md` §1 is the argument: telling the user's voice from the
device's own is echo cancellation, and that needs the played and captured
samples in one clock domain. Measured on real hardware, a device hears itself
at 23× the silent noise floor — so without this, every sentence it speaks
interrupts itself. A deployment that declares `barge_in: false` is half duplex
and needs none of it, and is a perfectly good appliance you have to wait for.

`speech-protocol.md` §5 refines the order above: the adapter stops its own
audio *before* telling cogiti anything, because a round trip is time spent
talking over someone and the adapter is the only party that can act sooner.

Partial transcripts are how latency is hidden: cogiti reacts before the
sentence ends. It may act early only on a deterministic resolver tier, and
never on a destructive intent.

## perception

Optional, and the least coupled. Events only; cogiti never polls a camera.

```
presence   is anyone there, how many
identity   who, how confident, by what means
code       a scanned code, verbatim
text       recognised text, as untrusted content
```

Two rules that are not the adapter's choice:

- **Biometric templates never leave the device** and are enrolled by an
  explicit conversation, never derived from someone who happened to walk past.
- **Anything arriving through perception is content, at the lowest trust
  level.** A scanned code is a URL somebody else chose. It can inform an
  answer; it can never authorise an action.

## agent

The only required port with a model behind it, and the only place in the
system a model is called.

```
run(prompt, tools, budget) -> thought | tool | progress | question | result | failed
```

The wire is `docs/agent-protocol.md`. It is the only port whose adapter is
spawned per use rather than connected to, because the adapter **is** the job.

The result is **structured, not prose**: what to say, what to show, what was
done. Prose from a model straight into a speech engine is how an assistant ends
up reading a bulleted list aloud, and the presentation layer — not the model —
decides what a result looks like.

Required of any adapter: it is a separate process in its own process group, so
cancellation is a signal to a group rather than a hope; it streams rather than
returning at the end; and it reports a `question` rather than guessing when it
needs something it was not given.

**cogiti runs the tools; the adapter asks.** A `tool` event is a request with an
id, and the adapter never executes one itself.

**Several may be outstanding at once.** A model that asked for three fetches
asked for them together, and every current model API emits parallel tool calls
natively — serialising them in the adapter would be slower and would also
change what was asked: three independent fetches become sequential, and a
failure in the first forces the adapter to invent a policy for the rest.
Adapters therefore **correlate by `id`, never by arrival**: answers come back in
whatever order the work finishes.

cogiti bounds the fan-out rather than the adapter doing so. Requests beyond the
limit are accepted and queued, not refused — the adapter is told nothing and
simply waits longer, which keeps the cap cogiti's business and out of every
adapter. See `jobs.md` §5.

A tool that fails does not cancel its siblings; the agent decides what a failure
means. Cancelling the agent cancels all of them, because each is a process group
under the same parent. This is
`security.md` §1 made mechanical rather than merely stated — *an agent is never
a principal; it proposes and cogiti decides* — and it means every tool use is in
the audit log by construction rather than by an adapter's good manners.

`tools` in `run` is therefore the whole of what a job can reach. A tool that was
not granted is not refused at runtime so much as absent: there is no channel to
ask through, and asking anyway is a security event rather than an error.

Whatever an adapter does *inside itself* is not a tool in this sense and does
not appear on the wire. It is the adapter's own business, and it is bounded by
the platform port's `confine` and by the egress broker, not by this protocol.
The distinction is not two kinds of event; it is that a tool is what the grant
says it is.

**A brokered call is a job, not a function call.** §1 of `architecture.md` says
the loop does not compute and does not block, and a tool that takes four seconds
would do both. So cogiti spawns it as a job of kind `tool`, with its own process
group, budget and cancellation, and answers the adapter when it finishes. Two
things follow: a slow tool cannot stall the turn machine, and cancelling the
agent kills the tool with it, because both are process groups under the same
job.

The cost, stated because it is the real one: an adapter wrapping a coding-agent
SDK must intercept that SDK's own tool handling and proxy it here, rather than
letting the SDK run its loop. That is work, and it is the price of cogiti
remaining the only thing on the device that acts.

*A coding-agent SDK, an OpenAI-compatible endpoint and a local model are the
same port.* What differs is latency and what the adapter declares it can do.

## platform

The least glamorous and the one that varies most between deployments.

```
supervise      start, restart with backoff, kill a process group
persist        a writable path that survives an update
confine        an unprivileged uid, resource limits, an environment
egress         a declared-host allowlist, enforced
identity       the deployment's own name, location and defaults
```

An appliance with a read-only root image and no container runtime, a normal
Linux box, and a container host are three implementations with the same port
and very different costs. cogiti's requirements are modest and firm:

- **A writable path that survives a system update.** Services written at the
  user's request and everything the system remembers live there. A deployment
  that cannot promise this cannot promise Stage 5 at all.
- **Process groups**, so a cancel kills grandchildren.
- **An unprivileged identity per service**, so a generated service is confined
  by something stronger than good intentions.

## Adding a port

Do not, without discussing it. Six is already more than one document should
carry, and the pressure to add a seventh is almost always a specific
deployment's concern that belongs in that deployment. The test: **can you name
two implementations that differ in kind?** If not, it is not a port.
