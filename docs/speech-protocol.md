# The speech protocol

The wire between cogiti and a speech adapter — `audi` in the InteliBoy
deployment, and nothing in this document knows that. `ports.md` gives the port;
this is the normative version, and where they disagree this file is wrong.

## 1. Why one adapter and not two

Speech in and speech out are one adapter, one process, one connection. They
could obviously be two, and the reason they are not is barge-in.

**Interrupting requires knowing what the device itself is saying.** A
microphone in a room with a speaker hears the speaker. Measured on the
development hardware: the device's own voice arrives at the microphone at
**23× the silent noise floor**. A detector that fires on sound fires on that,
so every sentence the device speaks interrupts itself.

Telling one voice from the other is acoustic echo cancellation, and it needs
the played samples and the captured samples **in one clock domain** — the same
process, the same device, the same timebase. Split the port in two and there is
no place left where that is possible.

So the adapter owns the speaker as well as the microphone. That is a real cost:
in a deployment whose renderer can also play audio, the renderer must stop
doing so. It buys the only interaction that makes a talking device feel
responsive rather than rude.

**A deployment may decline all of this.** An adapter that declares
`barge_in: false` is half duplex: it stops listening while it speaks, needs no
echo cancellation, and is a perfectly good appliance that you have to wait for.
cogiti supports both and neither is a degraded mode.

## 2. Lifecycle

Unlike the agent port, a speech adapter is **long-lived**. It is not a job, it
is a device: it starts with cogiti, it holds the audio hardware for the life of
the process, and losing it is losing the ears.

```
cogiti spawns the adapter in its own process group
   stdin    newline-delimited JSON commands
   stdout   newline-delimited JSON events
   stderr   free text, ring-buffered into the log
```

It is therefore the one adapter cogiti **reconnects to**. An agent adapter that
dies has failed its job; a speech adapter that dies has to be restarted with
backoff, because the alternative is a device that has gone deaf and does not
mention it.

Capabilities are probed once, at startup:

```
$ audi --capabilities
{"v":1,"type":"capabilities","partials":true,"barge_in":true,
 "wake_word":false,"languages":["en"],"sample_rate":16000}
```

cogiti asserts what its configuration needs against that line and stops at
startup naming anything missing.

## 3. adapter → cogiti

### `speech_start`

Someone began speaking. **No words, and that is the point** — it must arrive in
tens of milliseconds, far sooner than any recogniser can produce text, because
its whole job is to interrupt.

```json
{"v":1,"type":"speech_start","at_ns":1234567890}
```

It comes from voice activity detection, never from the recogniser. An adapter
that waits for a first word before sending this has made barge-in as slow as
transcription and there was no reason to.

`at_ns` is `CLOCK_MONOTONIC`, the same clock `speak` is scheduled against.

### `partial`

The transcript so far. Sent as often as the adapter likes.

```json
{"v":1,"type":"partial","text":"turn the volume","stable":true}
```

**Partials must grow, not rewrite.** cogiti resolves every one of them and may
act early on a deterministic match — a listed phrase cannot become something
else with the next word. A recogniser whose partials rewrite themselves as a
window slides breaks that promise, so it sets `stable: false` and cogiti treats
its partials as advisory: they may pre-warm, never commit.

This is the field that decides which recognisers suit this port. A streaming
transducer emits growing partials and sets `stable: true`. A window-based
Whisper does not and must not claim to.

### `final`

The utterance, finished.

```json
{"v":1,"type":"final","text":"turn the volume up","ms":1840}
```

The adapter is free to spend more effort here than on the partials — a
different and better model over the buffered audio is not just allowed, it is
the expected shape. The partials serve the fast path; the final is what a model
sees.

### `speech_end`

Speech stopped. Usually just before `final`, and separate from it because
silence and a transcript are different facts arriving at different times.

```json
{"v":1,"type":"speech_end","at_ns":1234567890}
```

### `error`

```json
{"v":1,"type":"error","kind":"device","message":"capture device went away"}
```

A category, not a message. `device`, `model`, `overrun`, `unsupported`.

## 4. cogiti → adapter

### `say`

```json
{"v":1,"type":"say","id":"u17","text":"It's half past two."}
```

The adapter synthesises, **plays it**, and reports back marks:

```json
{"v":1,"type":"speaking","id":"u17","visemes":[[0.0,"AA"],[0.31,"sil"]],
 "audio_start_ns":1234567890,"seconds":1.2}
```

cogiti forwards those marks to the presentation adapter so a mouth can move to
them. **cogiti never sees the audio**, and the renderer never plays it: the
samples stay in the one process that also holds the microphone, which is what
makes echo cancellation possible at all.

`audio_start_ns` is `CLOCK_MONOTONIC` and is the contract between the voice and
the mouth. It is the adapter's, because the adapter is what knows when playback
actually started.

### `stop`

Stop speaking, now. Idempotent, and never an error if nothing was.

```json
{"v":1,"type":"stop"}
```

### `listen`

```json
{"v":1,"type":"listen","enabled":false}
```

Explicitly deafen or un-deafen, for a deployment with a mute button. Not used
for barge-in — see below.

## 5. Barge-in, and who stops what

`ports.md` fixes the order: stop the presentation, stop the audio, then listen.
This refines it, and the refinement matters.

**The adapter stops its own audio the instant it detects speech — before it
sends anything to cogiti, and without being told to.** A round trip is tens of
milliseconds during which the device is still talking over someone, and the
adapter is the only party that can act sooner. Waiting for permission to stop
talking is the wrong shape for the same reason a person does not.

So the order in practice is:

```
adapter  detects speech ─▶ stops its own playback ─▶ emits speech_start
cogiti   receives speech_start ─▶ stops the presentation ─▶ interrupts the turn
```

The face keeps moving for the few milliseconds in between. That is invisible,
and the alternative — a device that finishes its sentence while being
interrupted — is not.

`stop` still exists, because cogiti also cancels turns for reasons that have
nothing to do with anyone speaking.

## 6. Rules that are easy to get wrong

**Its own voice is not a transcript.** With echo cancellation converged this is
handled; while it is converging, or when a deployment has none, the adapter is
responsible for not reporting the device's own speech as the user's. cogiti has
no way to check and will act on whatever it is told.

**`speech_start` is not a promise of a `final`.** A cough, a door, a passing
conversation — a turn may be interrupted by something that never becomes words.
cogiti must be able to return to what it was doing, so an adapter that sends
`speech_start` and then nothing must eventually send `speech_end`.

**Partials are droppable, finals are not.** Under load cogiti may ignore
partials; it may not ignore a final.

**One utterance at a time.** No `id` on the inbound events, because there is
one microphone and one person in front of it. The day that stops being true it
is the perception port's business to say who is speaking, not this one's.

## 7. Deliberately absent

- **Wake word.** Declared as a capability so cogiti knows whether to expect
  one, but the protocol carries nothing about it: to cogiti, a wake word is
  the absence of `speech_start` until someone says it.
- **Audio, in either direction.** No samples cross this wire. That is the whole
  design: the audio stays with the device that owns the clock.
- **Speaker identity.** The perception port's, and it has its own.
- **Language switching mid-utterance.** The capability line lists what an
  adapter supports and cogiti picks one at startup.
