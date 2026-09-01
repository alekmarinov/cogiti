# Jobs

A job is a bounded unit of work that may outlive the turn that started it. An
agent answering a hard question, a repository being summarised, a service being
authored, a file being fetched. It is not a command (which finishes inside a
turn) and not a service (which never finishes).

**This is a proposal.** Agree the lifecycle and the schema before any of it is
written; the failure modes below are the reason for that rule.

## 1. Lifecycle

```
   spawn ──▶ running ──┬──▶ done
                       ├──▶ failed
                       ├──▶ cancelled
                       └──▶ needs-input ──▶ running
```

| state | meaning | who moves it |
|---|---|---|
| `spawn` | the row exists, the process does not yet | cogiti |
| `running` | the process is alive and has not asked for anything | the child |
| `needs-input` | it stopped and asked a question of the user | the child |
| `done` | it produced a result | the child |
| `failed` | it exited non-zero, timed out, or was orphaned | cogiti |
| `cancelled` | the user or the load manager stopped it | cogiti |

Only these six. Resist `paused`, `queued` and `retrying` — each of them turns
out to be a policy that belongs somewhere else, and each of them doubles the
number of transitions the supervisor has to be correct about.

**`needs-input` is the state that will cause the bugs.** A job asks a
question; the user has moved on and is asking about something else; the answer
must not be captured by the wrong turn, and the question must not be lost.
The resolution: a question goes on a pending-questions list, the device
mentions it when the current turn ends ("the repository job wants to know
something"), and answering is a turn like any other with the job named. A job
waiting on input has a deadline like everything else, and expires into
`failed`, never into a guess.

## 2. Registry schema

```sql
CREATE TABLE job (
  id            TEXT PRIMARY KEY,      -- ULID; sortable, and legible in a log
  kind          TEXT NOT NULL,         -- 'agent' | 'tool' | 'author_service'
  title         TEXT NOT NULL,         -- what the device calls it out loud
  state         TEXT NOT NULL,
  session_id    TEXT NOT NULL,         -- who asked; (speaker, thread)
  parent_job    TEXT,                  -- an agent job may start several child
                                       -- jobs, in parallel; none of them may
                                       -- start their own. Depth two, breadth
                                       -- capped in load.toml
  pgid          INTEGER,               -- what cancel signals
  created_ns    INTEGER NOT NULL,      -- monotonic, for durations
  created_wall  TEXT NOT NULL,         -- for "you asked me this morning"
  updated_ns    INTEGER NOT NULL,
  deadline_ns   INTEGER,               -- NULL means no deadline, which is rare
  result_json   TEXT,                  -- the structured result, on done
  error_kind    TEXT,                  -- category, not a message
  error_detail  TEXT,
  cost_tokens   INTEGER,               -- so "why is this slow/expensive" is answerable
  progress      TEXT                   -- one line, the job's own words
);

CREATE TABLE job_log (
  job_id  TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  ns      INTEGER NOT NULL,
  stream  TEXT NOT NULL,               -- 'out' | 'err' | 'event'
  line    TEXT NOT NULL,
  PRIMARY KEY (job_id, seq)
);
```

`job_log` is a ring per job — the last N lines, N in the low thousands — and
not an archive. An unbounded log is a device that fills its disk because
someone ran a build once. The panel on screen shows fewer still; `lines` in the
presentation adapter is intent, not history.

## 3. Cancellation, which is the whole stage

Everything else here is bookkeeping. Cancellation is the part that is actually
hard, and it is hard because of grandchildren.

- Every job is started with `start_new_session=True` so it is a process group
  leader. `pgid` goes in the row before anything else happens.
- Cancel sends `SIGTERM` to `-pgid`, waits 5 seconds, then `SIGKILL` to
  `-pgid`. Not to the pid. A `kill(pid)` that leaves an agent's own subprocess
  running is the bug this stage exists to prevent.
- **Reap before verifying.** A killed child stays in the process table as a
  zombie until its parent calls `waitpid`, and a zombie has the pgid it always
  had. Verifying first and reaping second reports an escape on every single
  cancellation — measured, not supposed: a group killed with `SIGKILL` still
  showed one member in state `Zs` until the parent reaped it.
- After the kill and the reap, verify. Read the process table and assert
  nothing in that group survives. Log it if something does, because that means
  an escaped double-fork and it will happen again. A survivor that is a zombie
  is not an escape; a survivor in any other state is.
- The test for this spawns a child that spawns a grandchild that ignores
  `SIGTERM`, cancels it, and asserts the process table is clean. If that test
  does not exist, cancellation does not work — it has only not been observed
  failing.

## 4. Streaming to the screen

A running job gets a `stream` object in cogiti's own namespace, and the
attention semantics an adapter should offer are these:

| what | `attention` | why |
|---|---|---|
| a model thinking out loud | `never` | people look *away* while thinking; the face reading its own thoughts reads as reading, not thinking |
| a build, a fetch, a long tool | `watch` | external, something happening *to* the system |
| a finished result | the ordinary glance | it is being shown, so look at it |

Pair a thought stream with `busy`. Do not send an explicit `gaze` at a thought
stream — that clears the thinking state, and the face stops looking like it is
working.

Streams are cheap on the renderer's side (a ring buffer, one texture upload a
frame) but not free on ours: batch appends per event-loop tick rather than per
line, exactly as `tools/feed-log.py` does on the writing side.

## 5. Backpressure

`config/load.toml`. Proposed defaults, to be argued with once there are real
numbers:

| budget | default | on exceeding |
|---|---|---|
| concurrent agent jobs | 2 | queue, and say so |
| concurrent tool jobs per agent | 4 | queue, silently |
| concurrent jobs total | 4 | queue, and say so |
| queued jobs | 8 | refuse, and say why |
| one job's wall clock | 30 min | cancel and report |
| tokens per job | a ceiling | cancel and report |
| tokens per hour, device-wide | a ceiling | degrade to a smaller model, and say so |

**Always accept the request; never silently defer it.** "I'll start that when
the repository summary finishes" is an answer. Starting it silently in twenty
minutes is not, and neither is a spinner.

The one exception is the row above it, and it is an exception because it is not
the user's request. A tool call queued behind an agent's fan-out cap is an
internal step of an escalation the user has already been told about; narrating
it would be reporting on cogiti's scheduler rather than on their errand. It
queues silently and the agent simply waits. This is the load-behaviour rule arriving
early, because backpressure is where it first becomes real.

The shedding order when the device is over budget, first to go: feed refresh
rates, then optional presentation (a chart degrades to a line of text), then
queued background jobs, then agent quality (a smaller model, announced), and
never the turn in front of the user.

## 6. Intents

In the resolver's registry, not in cogiti's code:

`list_jobs`, `job_status`, `job_logs`, `cancel_job`, and the one people
actually say — `what_are_you_doing`. Each needs eval cases including negatives:
"what are you doing" must not resolve to `job_status` when nothing is running
and the user is making conversation.

Slots are awkward here and worth thinking about early: nobody says "cancel job
01J8ZQ". They say "stop that", "cancel the repository thing", "never mind".
Which means job selection is mostly contextual — the most recent job, the one
just mentioned, the only one running — and ambiguity is a question, not a
guess. Cancelling the wrong job is a small disaster.

## 7. Failure modes, listed before they happen

The stage prompt asks for these, so here they are as a starting list:

1. **The grandchild that survives.** Covered above; the reason for process
   groups and the reason for the verify step.
2. **The orphaned row.** cogiti restarts; jobs marked `running` in the database
   have no process. They are `failed` with `orphaned`, at startup, before
   anything else runs.
3. **The zombie panel.** A job dies without its stream being destroyed, and a
   dead log sits on screen forever. Destroy on every terminal transition, in a
   `finally`.
4. **The lost question.** `needs-input` arrives while the user is mid-turn and
   is never surfaced. Pending questions are a list with a deadline, not a
   callback.
5. **The wrong cancel.** "Stop" during a conversation about a job cancels the
   job; "stop" while the device is speaking is barge-in. The turn state
   decides, and the two must never be confused.
6. **The log that ate the disk.** Ring buffers, both in memory and in SQLite.
7. **The interleaved answer.** Two jobs finish while the user is talking to a
   third thing; both want to speak. One speech queue, one speaker, and
   everything else waits or goes to the quiet queue.
8. **The job that outlives its purpose.** The user asked, then said never mind,
   then walked away. A job whose session has been idle for its whole duration
   is worth asking about before it is worth finishing.
