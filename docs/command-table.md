# The command table

The first artifact to build. A declarative map from a resolved intent to the effect it
produces and the way that effect is shown. Adding a command is editing a file;
it is not writing code.

The resolver has the same principle one layer down: its intent registry decides
what an utterance *is* without touching its runtime. This decides what it *does*
without touching cogiti. Between the two, the ordinary case of "the device
should also know about X" is two files and no build.

## The shape

`config/commands.toml`, one section per resolver intent id. Three parts, and
they are deliberately separate: what to run, what to say, what to show.

```toml
[get_weather]
provider   = "weather.current"
speak      = "It's {temp_c} degrees and {condition} in {location}."
present    = "weather_card"
timeout_ms = 2500
offline    = "last_known"           # or: "refuse"

  [get_weather.args]
  location = { slot = "location", default = "device.location" }
  date     = { slot = "date",     default = "today" }
```

```toml
[power_off]
provider = "system.power_off"
confirm  = "Shut down the device?"    # spoken; the verdict already said confirm
speak    = "Goodbye."
present  = "none"
```

```toml
[set_timer]
provider = "time.set_timer"
speak    = "Timer set for {duration_human}."
present  = "timer_pill"

  [set_timer.args]
  duration = { slot = "duration", required = true }
```

## The four fields that carry the design

**`provider`** names a Python callable registered under `providers/`. It takes
resolved arguments and returns a result object. It may not take longer than
`timeout_ms`, may not touch the screen, and may not prompt. A provider that
wants to do any of those is not a provider — it is a job (`CLAUDE.md` §5).

**`args`** binds the resolver's slots to the provider's parameters, and is where
defaulting is made explicit rather than accidental. A slot arrives from the resolver
already carrying whether it was defaulted; that flag survives into the result,
so the presentation can say "in Sofia" versus "where you are" and the speech
can too. Losing that distinction is losing the one thing the
resolver port went out of its way to preserve.

**`speak`** is a template over the result. Templates, not model output, for
everything a command produces: a local command has a known shape, and the
device saying it the same way every time is a feature. Escalation is where
prose comes from.

**`present`** names a presentation template (below), or `none`. It is separate
from `speak` because the two answer different questions — the spoken line is
what a person needs to hear, the card is what they might want to read a second
time — and because a device with no screen attached should still work.

**`linger`** is how many seconds the card stays up once the answer has
finished being spoken. It defaults to 10, and `linger = 0` keeps it until
something replaces it.

It exists because `lifetime: turn` makes expiry the *adapter's* business, and
a renderer with no notion of a turn boundary — which is every one we have —
simply leaves the card there. "What time is it" sat on the stage for the rest
of the evening.

The right duration is a property of the answer, not of the device, which is
why it is per command rather than one setting:

    linger   = 45     # an address, copied off the screen and typed elsewhere
    linger   = 12     # the time, stale almost immediately
    linger   = 0      # a timer that has just gone off; it waits for you

The countdown starts when speaking ends, not when the card appears — a long
answer would otherwise spend most of its ten seconds still being read aloud.

## Presentations

`config/presentation/*.toml`. A template turns a result object into presentation
ops. It names objects, regions and relationships; it never names a
coordinate, because layout belongs to the adapter (`ports.md`, presentation).

```toml
# config/presentation/weather_card.toml
id     = "brain/weather"
kind   = "group"
region = "stage"

  [[children]]
  kind  = "text"
  style = "headline"
  text  = "{glyph}  {temp_c}°"

  [[children]]
  kind  = "text"
  style = "title"
  text  = "{location}"

  [[children]]
  kind  = "text"
  style = "caption"
  text  = "{condition}\n{low_c}° to {high_c}°"
```

Three properties of this that are worth keeping:

- **Every id is namespaced.** cogiti declares its namespace on connect, so the
  adapter enforces it. A presentation template that
  names an id outside that namespace is a config error caught at load.
- **The conversational region is the default; the pinned region is not
  available here.** A
  command's output is conversational; pinning is a service (`services.md`).
- **A template is data, so a new card is not a new build.** the presentation adapter's own
  design note applies unchanged: a weather panel is a group of text and an
  image, and "weather support" is not a renderer feature. It should not be a
  cogiti feature either.

## What a provider returns

```python
Result(
    ok=True,
    values={"temp_c": 21, "condition": "clear", "location": "Sofia",
            "glyph": "☀", "low_c": 14, "high_c": 24},
    provenance={"location": "defaulted"},   # from the decision
    ttl_s=600,                              # how long this stays true
    source="wttr.in",
)
```

`ttl_s` and `source` exist for two reasons that arrive later and are annoying
to retrofit: memory needs to know how long a fact is worth keeping,
and the audit log needs to know where a number came from when the
user asks why the device said it.

A failed result carries a reason, and the reason is a category — `offline`,
`refused`, `not_found`, `timeout` — not a string. The categories map to spoken
apologies in one place, so the device fails the same way every time.

## Offline behaviour, declared per command

`offline = "last_known"` means the provider may serve a cached value and the
speech says so ("about ten minutes ago it was..."). `offline = "refuse"` means
it says it cannot. This is per command because the right answer differs: a
stale temperature is useful, a stale stock price is a hazard, and the device
should not decide that for itself at runtime.

## Asking for a missing slot

reflexi escalates when a required slot is empty — but it escalates *carrying
the intent and the name of the slot*. That is the one escalation that already
knows what it wants, so the table says how to ask:

```toml
  [set_volume.args]
  level = { slot = "level", required = true, ask = "What level? Nought to a hundred." }
```

No `ask` means no asking: the intent escalates to the model as it would have.
A gap in the table is then a sentence someone can add, not a behaviour cogiti
invents.

**The answer is never resolved on its own.** It is appended to what was
originally said and the whole sentence goes back through the resolver. Measured
against reflexi, a bare "make it 20 minutes" answering "a timer for how long?"
resolves to `volume_down`; "ten" and "for 20 minutes" resolve to nothing. As
`set a timer make it 20 minutes` all three come back correctly, because the
resolver is reading a sentence rather than a fragment.

Two guards on the result, and the first is the one that matters:

- **The same intent, or nothing.** A follow-up must not be able to change what
  is being done. That is how an answer about a timer becomes a volume change,
  and one day something worse.
- **The slot must actually be filled**, or nothing was gained and it escalates
  as it would have anyway.

"Cancel", "never mind" and "forget it" always leave. A question is never a trap
that has to be answered to escape.

## Confirm

The resolver already decides that an intent needs confirmation, and the port
forbids a destructive intent reaching `handle` through a similarity score. The command
table's `confirm` field is only the wording. cogiti never auto-answers a
confirm, never lets it expire into yes, and never lets an agent answer one.

## The eval

`eval/commands/*.yaml`, one scripted session per command:

```yaml
- utterance: "what's the weather in Sofia"
  expect_intent: get_weather
  expect_provider: weather.current
  expect_args: { location: Sofia, date: today }
  expect_ops:
    - { op: create, id: "brain/weather", kind: group, region: stage }
  expect_speech_matches: "It's .* in Sofia\\."
```

Run before and after any change to the table or the presentation layer, both
numbers in the commit message. The point is not that the assertion is clever;
it is that a table with sixty entries in it will otherwise rot silently, one
renamed slot at a time.

## When something does not fit

If a command needs to ask a question, run for a minute, hold a lock, or keep
running after the answer, it is not a command. The table is for effects that
finish inside a turn. Everything else is `jobs.md` or `services.md`, and
forcing it into the table here is how a device ends up with a two-second pause
in the middle of a conversation.
