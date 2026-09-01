# The agent protocol

The wire between cogiti and an agent adapter. `ports.md` says what the port is
for and why; this says what is on it.

This is **not** a model API. An adapter speaks this on pipes to cogiti, and
whatever it likes over the network to whatever is behind it — a coding-agent
SDK, an HTTP endpoint, a local model. Translating between the two is the
adapter's whole job.

## 1. Lifecycle

An agent adapter is **not a long-lived connection**. It is spawned per
escalation, it is the job, and it dies with it:

```
cogiti spawns adapter in a new process group   ← pgid recorded before anything else
   stdin    one `run`, then answers to what it asked for
   stdout   newline-delimited JSON events
   stderr   free text, ring-buffered into the job log
adapter emits a terminal event and exits
```

That is why cancellation is a signal to a group: there is no session to close,
only a process tree to end.

**Capabilities are probed once, at cogiti's startup, not per run.** Negotiating
per job would pay for it on every escalation and would move a configuration
error from startup to first use:

```
$ adapter --capabilities
{"v":1,"type":"capabilities","tools":true,"questions":true,"streaming":true}
```

cogiti asserts what its configuration needs against that line and **stops at
startup naming anything missing** — the rule every port shares. A `v` it does
not know is refused here too, which is the only place a version mismatch can be
handled without losing work.

## 2. Framing

One JSON object per line, UTF-8, `"v":1` on every message, the shared line
limit from `ports.md`. Anything unparseable is a protocol error: recorded in
the trace, the job failed with `kind=protocol`. A well-formed message of an
**unknown type** is recorded and ignored rather than fatal — an adapter that
learns to say something new must not break an older cogiti — but a terminal
event must be one of the two below, or the job ends as `protocol` when the
process exits without one.

## 3. cogiti → adapter

```json
{"v":1,"type":"run","job":"J1",
 "prompt":{"text":"…","context":{…}},
 "tools":[{"name":"http","schema":{…}}],
 "budget":{"wall_ms":30000,"tokens":8000}}
```

Exactly one `run`, first, before anything else. `tools` is **the whole of what
this job can reach**; a tool absent from it has no channel and asking for one is
a security event, not an error.

```json
{"v":1,"type":"tool_result","id":"t1","ok":true,"value":{…}}
{"v":1,"type":"tool_result","id":"t1","ok":false,"error":{"kind":"timeout","message":"…"}}
{"v":1,"type":"answer","id":"q1","value":"…"}
{"v":1,"type":"cancel"}
```

`cancel` is a courtesy, not the mechanism. It gives an adapter the chance to
stop cleanly; `SIGTERM` to the group follows, then `SIGKILL` five seconds later
per `jobs.md` §4. An adapter that ignores `cancel` is not misbehaving, it is
merely about to be killed.

## 4. adapter → cogiti

```json
{"v":1,"type":"thought","text":"checking both sources"}
{"v":1,"type":"progress","note":"2 of 3 fetched","pct":66}
{"v":1,"type":"tool","id":"t1","name":"http","args":{…}}
{"v":1,"type":"question","id":"q1","ask":"which repository?","expects":"text"}
```

`thought` and `progress` are informational and may be dropped under load.
`tool` and `question` are requests: the adapter continues working and waits for
the matching `tool_result` or `answer`.

Two terminal events, after which the adapter exits:

```json
{"v":1,"type":"result",
 "say":"about two thousand four hundred dollars",
 "show":{…},
 "did":[{"tool":"http","host":"api.coingecko.com"}]}

{"v":1,"type":"failed","kind":"upstream","message":"…"}
```

`say` is plain text for the speech port — **not prose from the model**, but the
sentence the adapter constructed to be spoken. `show` is a presentation
composition or absent. `did` is what actually happened, for the audit log; it is
cogiti's record and not the model's account of itself, so cogiti fills what it
knows and treats this as a claim to be checked against what it brokered.

`kind` on `failed` is a **category, not a message** — `jobs.md`'s `error_kind`
column exists for exactly this, so failures can be counted.

## 5. Rules that are easy to get wrong

**Ids are the adapter's, unique within a run.** cogiti echoes them back
untouched and never invents one.

**Several tool calls may be outstanding, and answers arrive in whatever order
the work finishes.** Correlate by `id`, never by arrival. An adapter that
assumes its second answer belongs to its second request will be wrong the first
time a cache hit races a network call.

**A failed tool does not cancel its siblings.** The agent decides what a
failure means.

**Fan-out is bounded by cogiti, invisibly.** Requests past the cap are queued,
not refused; the adapter is told nothing and simply waits longer. The cap is
`jobs.md` §5 and it is cogiti's business, not an adapter's.

**A tool result for a cancelled job is dropped**, not delivered. The tool job
was killed with its parent's group; anything already in flight is discarded
without being written to the pipe, because a cancelled job must produce no
further effects.

**A `question` blocks nothing else.** The adapter may continue and may have
tool calls outstanding while it waits. Pending questions are a list with a
deadline, not a callback — `jobs.md` failure mode 4.

## 6. A run, end to end

```
cogiti → {"v":1,"type":"run","job":"J1","tools":[{"name":"http",…}],…}
     ← {"v":1,"type":"thought","text":"I'll check two sources"}
     ← {"v":1,"type":"tool","id":"t1","name":"http","args":{"url":"…coingecko…"}}
     ← {"v":1,"type":"tool","id":"t2","name":"http","args":{"url":"…kraken…"}}
                    ↑ both outstanding; cogiti spawns J2 and J3
cogiti → {"v":1,"type":"tool_result","id":"t2","ok":true,"value":{…}}
cogiti → {"v":1,"type":"tool_result","id":"t1","ok":true,"value":{…}}
                    ↑ t2 answered first. The adapter matched on id, not order
     ← {"v":1,"type":"result","say":"about 2,400 dollars","show":{…},"did":[…]}
                                                              adapter exits 0
```

## 7. Deliberately absent

- **Streaming a partial `result`.** A result is structured and arrives once.
  `thought` and `progress` carry the sense of movement; a half-built result
  would tempt the presentation layer into rendering something that is about to
  change.
- **Any notion of a conversation.** The adapter is given a prompt and returns an
  answer. Threading, history and identity are cogiti's, in `session.py`, and an
  adapter that kept its own would have a second one that disagreed.
- **Model names, temperatures, token counts.** Configuration of whatever is
  behind the adapter, not of the port. What differs between a local model and a
  hosted one is latency and declared capabilities, and both are already here.
- **Anything the adapter does internally.** Tools are what the grant says they
  are. An adapter's own work is bounded by `confine` and the egress broker, not
  by this protocol, and does not appear on this wire.
