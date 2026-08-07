# Voice companion

`packages/pz_agent_voice/` is the speech interface. Core never imports it:
speech is an interface to the agent, not part of its decision loop.

This document has two halves. The first is the surface a user meets and the
limits it has. The second is the **bridge contract** — everything an implementer
needs to write the program on the far end of the pipe, since the vendor SDK is
not installed here and this build never calls it directly.

---

## Starting it

```powershell
.venv\Scripts\pz-agent voice run          # listen, on the configured adapter
.venv\Scripts\pz-agent voice check стоп   # what does this phrase resolve to?
```

Nothing else starts the companion. The sidecar does not spawn it, arming does
not imply it, and `voice.enabled = true` on its own listens to nothing — it is a
setting `voice run` reads, not a process. `pz-agent status` prints which of those
two states a machine is in.

`voice run` refuses rather than half-starting, and says which of these it was:
the configuration did not validate, `voice.enabled` is false, `voice.adapter` is
`"none"`, the key named by `voice.api_key_env` is not in the environment, or the
adapter could not be constructed. It never falls back.

`voice check` needs no game, no session, no configuration and no microphone. It
runs the phrase through the same classifier the session runs, prints the word
tokens the matcher actually compared, the intent, and what would happen; it exits
non-zero when nothing matched. It is the answer to "why was «стоп» not
recognised" — usually the tokens are the answer, because the matcher compares
whole words and «стопка» is not one.

## Configuration

```toml
[voice]
enabled = true
adapter = "teamon"                      # or "none"
api_key_env = "PZ_AGENT_TEAMON_API_KEY" # the *name* of the variable
```

`SUPPORTED_VOICE_ADAPTERS` is `("teamon", "none")`. `api_key_env` names an
environment variable and never holds the key — the same rule and the same
validator the planner's providers use. A key pasted there is refused by
`validate-config` before anything starts.

`[voice]` carries only what a user chooses. The loop's own bounds are
`DEFAULT_VOICE_CONFIG` in `pz_agent_voice.config`, validated at construction:

| Bound | Value |
| --- | --- |
| `wake_words` | `агент`, `ассистент`, `agent` |
| `wake_ttl_ms` | 30 000 |
| `require_wake_word` | true |
| `min_confidence` | 0.6 |
| `max_clarifications` | 1 |
| `plan_max_steps` | 8 (ceiling `MAX_PLAN_STEPS` = 8) |
| `plan_max_real_seconds` | 120 (ceiling `MAX_PLAN_REAL_SECONDS` = 600) |
| `MAX_TRANSCRIPT_CHARS` | 400 |
| `MAX_TEXT_CHARS` (one spoken sentence) | 240 |

---

## Stop

The single most important behaviour, and the reason the module boundaries fall
where they do.

- `VoiceSession.handle` tests for a stop word **first** — before the wake gate,
  before the interim/final test, before the confidence threshold, before
  classification. A stop therefore works from `IDLE` with no wake word, from a
  low-confidence interim transcript, while a clarification is outstanding and
  while a plan is running.
- `VoiceCompanion.deliver` cancels speech **before** it classifies anything, so
  an utterance in flight is cut at the moment the user starts talking rather
  than after the recogniser has settled.
- The stop path clears the utterance queue, resets the wake state and the plan
  state, then calls `SessionPort.stop()` — which needs no armed state.
- «Остановился.» is spoken **only after the port acknowledges**. If the port
  raises, the failure is what gets said.
- `VoiceSession.stop()` is public so a panic hotkey reaches exactly the same
  code as a stop word.

There is one stop vocabulary, `intent.STOP_WORDS`, and every matcher in the
package reads it rather than keeping a copy. Two stop lists in one process is one
stop list that silently stops agreeing with the other.

## The ports

`VoiceServices` bundles two ports, and they are the *same* protocols the MCP
boundary reads through — `pz_agent_mcp.ports.SessionPort` and `PlanPort`. Giving
voice its own path to the planner would make the microphone a privileged caller.

**The two ports deliberately do not share a transport.** A goal has to reach the
process that owns the planner, the policy engine and the action queue, and that
is not the process holding the microphone — so `plan_port.py` puts `PlanPort` on
the Local Core RPC link. `SessionPort` stays wherever its owner put it: for
`pz-agent voice run` that is `ExchangeSessionPort`, writing the mod's own panic
latch in the exchange directory, which one write reaches whether or not a sidecar
is listening. A stop that had to dial a socket, authenticate and wait for an
answer would fail in exactly the state a user reaches for it.

`SessionPort.status()` reads the game's heartbeat. A missing or stale heartbeat
is `connected = False`, and a heartbeat that does not mention arming reads as
disarmed: an omission is not a claim.

`SessionPort.stop()` writes `panic.stop`, and that is the whole route.
`PZAgent.Runtime.tick` reads it on every heartbeat tick and stops what the game
is running; `SidecarLoop` reads it on every tick and before every submission,
disarms, and closes in-flight work as lost. One write reaches both, and it
reaches them **with no sidecar running at all**. The file is written whole and
not atomically because the mod never parses it: any non-empty content is a stop,
so a torn write is still a stop.

Returning from `stop()` is a claim that the *request* is in force — the latch is
on disk and its size was checked — not that the game has already stopped. That
second fact is not observable synchronously. `StopReport.cleared` is therefore
`0`, because the count of mod-owned entries belongs to the mod, and `disarmed`
reports what the last heartbeat said. If the write fails, `stop()` raises and
«Остановился.» is withheld.

`SessionPort.arm()` **refuses, always.** A verified backup and an explicit
request belong between a save and an armed agent, and a microphone in a room
with a television is not an explicit request. `pz-agent arm` is the only way to
arm.

### Where a spoken goal goes today

This is the part of the package that has been moving, so read it as a statement
about the tree rather than about the design.

`pz_agent_voice.plan_port` exists and provides `core_plan_port(state_dir)` and
`services_over_core_rpc(session, state_dir)`, which build
`RemoteCoreServices.from_state_dir(...)` — the same `goal.submit` the MCP
server's `pz_goal_submit` tool submits through, with no second encoder and no
second path into the engine. It owns one thing of its own: the deadline.
`VOICE_PLAN_DEADLINE_SECONDS` is 3.0 and `MAX_VOICE_PLAN_DEADLINE_SECONDS` is
10.0, refused at wiring time rather than at the first utterance, because a
companion that goes silent for ten seconds is a user who repeats themselves —
and with a fresh idempotency key per transcript, that is how one goal becomes
two.

**`pz-agent voice run` uses it now.** `pz_agent_cli.voice.voice_services` calls
`services_over_core_rpc`, so a spoken goal reaches the sidecar over the Local
Core RPC link. `UnroutedPlanPort` and `GoalUnroutable` are gone; the voice record
writes `goals_routed: true`.

A spoken goal travels on **`goal.submit`**, not `plan.execute` — the typed goal
channel, because a `PlanRequest` has nowhere to carry "twelve pages" that is not
free text. `docs/control/DECISIONS.md` records why. `VoiceGoal.RESUME` submits
nothing: it is a statement about work already sent, answered from `goal.status`.

`voice check` **dials** rather than reading the descriptor, and the difference is
the point: a sidecar killed rather than stopped leaves a valid descriptor naming
a live pid, which is exactly the state in which a file-only check answers
"routed" with nothing listening. It makes one read-only `goal.status` call,
bounded at 2 seconds, and only for a phrase that resolves to a goal — so
`voice check стоп` still runs with no sidecar, no game and no discovery.

Verify which state the tree is in with one command:

```
grep -rn "UnroutedPlanPort\|services_over_core_rpc" packages/pz_agent_cli/src/pz_agent_cli/voice.py
```

The first name should return nothing and the second should return a line.

## The adapter contract

```python
class VoiceAdapter(Protocol):
    async def events(self) -> AsyncIterator[VoiceInput]: ...
    async def speak(self, message: VoiceOutput) -> None: ...
    async def cancel_speech(self) -> None: ...
```

`events` is a coroutine that *returns* an async iterator rather than being an
async generator. That is load-bearing for the type checker — an `async def` with
a `yield` in it has type `AsyncIterator`, not `Coroutine[..., AsyncIterator]` —
so an implementation keeps its generator in a second method and returns it. In
exchange, obtaining the stream is itself awaitable, which is what a backend that
has to open a socket or a microphone needs.

The stream must yield **interim** transcripts as well as final ones: barge-in
and the stop word both depend on hearing the user before the recogniser has
endpointed.

`speak` returning normally is a claim that the user heard the whole sentence. An
adapter may raise `SpeechCancelled` to say it was cut short; raising is optional,
because the driver also tracks the cancellation it requested. What an adapter
must *not* do is return normally after an interruption.

`cancel_speech` must be safe to call when nothing is being spoken and must not
wait for a phrase boundary.

## A transcript is data

The only thing that crosses from the microphone to anything else is a token from
a closed enum — `VoiceGoal` (`eat`, `drink`, `read`, `resume`) for the session's
own vocabulary, and a `GoalKind` from `pz_agent_core.goals` for the typed goal
channel. Transcript text is matched against a closed vocabulary and then
dropped. It is never forwarded, never stored and never spoken back: every
sentence the companion can say lives in `pz_agent_voice.phrases`, and none of
them has a slot for free text.

`pz_agent_voice.intent` is the one module that maps speech onto those enums; it
is the *only* place in the package that names a `GoalKind` from a transcript
(BLOCKERS.md R-007 deleted the second, unwired resolver that once claimed the
same thing). A number reaches a goal parameter through a unit word («страниц»,
«процентов», «уровня»), through the percent sign — which is how a
numeral-emitting recogniser spells «процентов» — or, for the one kind
`BARE_NUMBER_PARAM` declares a meaning for (`train_skill`, whose bare number
can only be a target level), on its own; every route ends at the same
range check against the core's `NUMERIC_RANGES`.

`IntentRefusal` is the closed set of ways a phrase can fail to become a goal —
`not_a_goal`, `ambiguous_goal`, `skill_not_named`, `parameter_out_of_range`,
`parameter_not_accepted`, `capability_unavailable`, `internal` — and every
member has a spoken form, in one of two shapes. Four are fixed sentences in
`phrases.REFUSAL_SPEECH`; `internal` is the defensive one, spoken only if the
resolver's own range check ever accepts a number the core's constructor then
refuses, so that the constructor's message — which quotes the number the user
said — never travels. The other three cannot be said without naming
something, so they are assembled by `phrases.intent_refusal()` out of closed
tables — `PARAM_NOUNS` and `NUMERIC_RANGES` for a parameter, `CAPABILITY_NOUNS`
for a capability — and a name that is in none of them raises rather than being
interpolated. Those three are listed in `_NAMED_REFUSALS`, and
`_check_speech_tables()` runs at import: a member appearing in neither table, or
in both, or carrying a sentence too long to speak, refuses the import. What the
arrangement buys is that a refusal never quotes what the user said — the
transcript's route out through the apology is the one the whole module is
arranged to close.

Two goals matching is `ambiguous`, not "the first one in some arbitrary order".
Guessing between «поесть» and «попить» is exactly the behaviour the blueprint
asks to be replaced with a question.

## Not speaking

Messages carry a `topic`, and a second message about a topic that already has one
pending *replaces* it instead of queueing behind it. Identical messages collapse,
stale ones — a lower `revision` — are discarded, and the queue is bounded by
distinct subject: at the cap the least urgent, oldest pending message is evicted,
and a newcomer less urgent than everything pending is refused. **A `STOP`
utterance cannot be evicted.**

`VoiceSession.report_plan` may be called at whatever rate the sidecar observes
plans; it returns `None` and enqueues nothing unless the status actually changed,
and only a terminal status is spoken.

`TtsEventStream` publishes every transition an utterance makes — `queued`,
`collapsed`, `superseded`, `dropped`, `cleared`, `started`, `cancelled`,
`finished` — so a consumer can see what was *not* said as well as what was.
Subscribers and history are bounded; a subscriber that raises is unsubscribed and
its failure recorded rather than breaking the speaker.

## When a part of the system stops answering

None of these ends the conversation, because the transcript that arrives next may
be «стоп»:

- **The planner raises.** The goal is reported refused, nothing is submitted, and
  the exception type and message go into `VoiceTurn.detail`.
- **`SessionPort.stop()` raises.** «Остановился.» is withheld and «Не смог
  остановить. Останови вручную.» is said instead.
- **`SessionPort.status()` raises.** The port is documented to answer even when
  the game is gone, so this is a broken port rather than a disconnected game. Not
  knowing the state is not the same as knowing it is fine: nothing is submitted
  and «Нет связи с игрой.» is said.
- **The synthesiser raises.** The utterance is reported `cancelled` — it was not
  heard — the reason is kept in `VoiceCompanion.speech_failures` (bounded), and
  the speech pump carries on.
- **A listener on the TTS stream raises.** It is unsubscribed and its failure
  recorded; the other listeners still receive.

`VoiceCompanion.run` gives its subscription back on every exit and, on
cancellation, abandons speech in flight rather than waiting for a backend that
may never return.

## Adapters

`voice.adapter` selects one. **`FakeVoiceAdapter` is not selectable and must
never be**: it answers scripted transcripts, so selecting it would leave a user
being told the companion is listening while «стоп» reached a list.
`select_adapter()` refuses it even when handed a configuration no validator would
have produced, and refuses to fall back to it — a configured adapter that cannot
be built stops the command.

`TeamONVoiceAdapter` is written against `TeamONClient`, a three-method protocol
this package defines in `adapters/teamon.py`. Everything the adapter itself owns
— clamping an out-of-range confidence, bounding an over-long transcript, mapping
urgency to priority, deciding that a synthesis call which returned after an
interrupt did not deliver its sentence — is implemented here and tested.

The only vendor import in the project is `require_teamon_sdk()`, which reports a
missing install step rather than an `ImportError`. It is confined to one function
so that every other module in this package imports, type-checks and tests on a
machine without the SDK.

---

# The TeamON bridge contract

Everything below is what an implementer needs. It is a complete statement of the
wire: `packages/pz_agent_voice/src/pz_agent_voice/bridge/protocol.py` is the
authority, and this section is a reading of it.

## The process

The bridge is a **separate operating-system process**. That is the design, not a
workaround. A vendor SDK loaded in-process gets the sidecar's memory, its
exception handling and its exit code; a vendor SDK behind a pipe gets a fixed
byte budget and a kill switch.

- The bridge program links against the vendor SDK. This build never does.
- It reads messages on **stdin** and writes them on **stdout**, one JSON object
  per line. Anything it wants to log goes on stderr, or anywhere but stdout.
- `JsonlBridge` in `bridge/client.py` supervises it: a reader thread drains
  stdout into bounded queues, a writer thread drains a bounded outbox into
  stdin. Neither is the caller's thread, so a bridge that has stopped reading
  its stdin cannot wedge the companion's event loop — the pipe fills, the writer
  blocks, and the outbox refuses instead of growing.
- **The bridge holds its own credentials.** `BridgeConfig` refuses a
  configuration key, an environment variable name or an option-shaped command
  argument whose name contains `key`, `token`, `secret`, `password`, `passwd`,
  `credential`, `auth` or `bearer`. The child's environment is an **allowlist** —
  `PATH`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `LANG`, `LC_ALL`,
  `TZ` — so the parent's own secrets do not travel either. Whatever the SDK needs
  to authenticate, the bridge program reads for itself, from somewhere this
  process never touches.
- **The stop path never waits for agreement.** `JsonlBridge.stop` signals, waits
  `stop_timeout`, kills, waits `stop_timeout` again and gives up: at most twice a
  bound the caller set, whatever the bridge does. `terminate` is the one shutdown
  signal both platforms have.
- **Restarts are counted.** A crashed bridge is relaunched on the next call, up
  to `max_restarts`; past that the bridge is `DEAD`, and a report reaches the
  listener carrying a sentence for the user.

`BridgeConfig`, with its defaults and its own bounds:

| Field | Default | Bound |
| --- | --- | --- |
| `command` | — | required, non-empty, no empty argument, no credential-shaped option |
| `env` | `{}` | valid variable names, no credential-shaped name |
| `startup_timeout` | 5.0 s | 0 < t ≤ 60 |
| `reply_timeout` | 5.0 s | 0 < t ≤ 60 |
| `speech_timeout` | 30.0 s | 0 < t ≤ 60 |
| `stop_timeout` | 1.0 s | 0 < t ≤ 60 |
| `max_restarts` | 3 | 0–10 |
| `max_pending` | 64 | 1–1024 |

`BridgeConfig.from_mapping` refuses an unknown key rather than ignoring it: a
misspelled timeout that silently keeps the default is a bound the user believes
they set.

`check_bridge(config)` answers whether the program could be launched, without
launching it — at check time, not in the middle of a sentence the user is waiting
on. It never raises.

## The framing

**One JSON object per line, newline-terminated, UTF-8.**

- `encode()` uses `json.dumps`, which escapes every control character, so a
  newline *inside* a field is `\n` in the output and cannot end the line early.
  A message never spans two lines and a line never carries two messages.
- `MAX_MESSAGE_BYTES` = `MAX_LINE_BYTES` = **16 384**. Equal on purpose: a line
  that cannot hold a legal message is not worth buffering.
- `LineFramer` drops an over-long line **as the bytes arrive** — once the current
  line passes the cap it keeps none of it and skips to the next newline, then
  reports `OverlongLine(dropped_bytes=…)`. A bridge emitting one enormous line
  costs a fixed buffer and a counter, and *the line after it still parses*. That
  recovery is the point: the alternative is a session that ends because one
  message was malformed.
- Every message carries `v`, the protocol version, and `type`.
- `decode()` is told which direction the line is supposed to be travelling. A
  `speak` arriving *from* the bridge is refused as firmly as a type that does not
  exist, and for the same reason: something on the far end is not the bridge this
  build talks to.

Refusals are typed: `MalformedMessage` (not UTF-8, not JSON, not an object, or a
missing field), `UnknownMessageType` (unknown type, or wrong direction),
`MessageTooLarge`, `ProtocolMismatch`. All derive from `BridgeProtocolError`, and
the reader carries on after each — refusing loudly rather than ignoring, because
an ignored type is a silent protocol drift that shows up months later as a
feature that quietly never worked.

Nothing the far end wrote is quoted at length anywhere. The only far-end text
that can reach a log line is an unrecognised **type name**, cut to 32 characters.

## The closed message set

Eight types, and this is all of them. `MESSAGE_DIRECTIONS` declares the direction
of each, and an import-time check fails if any type is missing from that table.

### To the bridge

| Type | Fields | Meaning |
| --- | --- | --- |
| `hello` | — | Opens the session and declares this build's protocol version. |
| `speak` | `utterance_id`, `text`, `priority`, `interruptible` | One utterance to synthesise. `priority` 0 is most urgent; `interruptible` is `false` for the stop acknowledgement, which must be heard. |
| `interrupt` | `utterance_id` | Abandon that utterance now, without waiting for a phrase boundary. Must be safe for one that already finished. |
| `goal` | `request_id`, `goal` | One member of `VoiceGoal` — `eat`, `drink`, `read`, `resume`. A **token, never transcript text**. |

`speak_message()` refuses rather than truncates: empty text, text past
`MAX_TEXT_CHARS` (240), or a negative priority is a bug on this side, and
truncating would hide it mid-sentence. `utterance_id` and `request_id` are
handles minted by this side (`teamon-3`) and shape-checked against
`^[a-z0-9][a-z0-9_.\-]{0,63}$`, so a handle can never carry punctuation into a
JSON field or a log.

`goal_message()` takes a `VoiceGoal` enum member, not a string. The widest thing
this process can put on the pipe is one of four members, and there is no branch
that forwards a transcript.

### From the bridge

| Type | Fields | Meaning |
| --- | --- | --- |
| `ready` | `v` | Answers `hello`. **Until it arrives the session has not started.** |
| `transcript` | `text`, `at_ms`, `final`, `confidence` | One recognition result. Interim hypotheses included — the stop word depends on them. |
| `outcome` | `request_id`, `status` | A `speak` or a `goal` ended, and how. |
| `error` | `code`, optional `detail` | The bridge failed. |

`read_transcript` bounds and clamps rather than refusing: `text` is cut to
`MAX_TRANSCRIPT_CHARS` (400) because a recogniser emitting a paragraph of
accumulated context is doing something normal; `at_ms` is floored at 0;
`confidence` is clamped into 0..1, because a client reporting 1.7 would otherwise
sail past the gate that exists to stop the companion acting on a guess. `final`
defaults to `true` and must be a boolean; `at_ms` must be a whole number and
`True` is rejected as one, because `bool` is an `int` in Python and `True` as a
timestamp would read as one millisecond.

`OutcomeStatus` is a closed set of four: **`ended`** — the only member that
claims the thing was carried out — plus `failed`, `refused` and `cancelled`. An
unrecognised status is **refused**, not mapped to a failure: this is the message
the companion uses to decide whether a goal *ended*, and a status it cannot read
is a question, not an answer.

`BridgeFaultCode` is a closed set of six: `audio_device`, `recogniser`,
`synthesis`, `network`, `internal`, `unknown`. An unrecognised code becomes
`unknown` rather than being refused — an error is already the failure path, and
refusing the report of a failure loses the failure.

**`error.detail` is read to classify the fault and then discarded.** It does not
reach the returned `BridgeFault`, a log line, or the exception. It is the one
field the far end fills with free text, and every consumer of a fault in this
package eventually reaches a speech synthesiser: reading an arbitrary string from
another process through text-to-speech is how a stack trace, a file path or
somebody's key ends up spoken aloud in a room. Each code maps to one fixed
sentence in `FAULT_PHRASES`, and a bridge that is gone for good produces
`BRIDGE_UNAVAILABLE_PHRASE` — «Голосовой мост не работает.» — which names the
component and nothing else.

## The version rule

`BRIDGE_PROTOCOL_VERSION` is **`"1.0"`**, and both sides send it as `v` on
**every** message, in both directions, so a mismatch is caught on the first line
rather than on the first field that differs.

- The format is `MAJOR.MINOR`, matched against `^(\d{1,3})\.(\d{1,4})$`. Anything
  else is `MalformedMessage`.
- **A MAJOR mismatch is refused** with `ProtocolMismatch`, whose message tells
  the user to install a bridge built for `<major>.x`. The two sides then disagree
  about what the field names mean, and nothing after that point is safe.
- **A MINOR difference is accepted, in either direction.** That is what makes
  adding a field to the tables above a compatible change: MAJOR changes when a
  field changes meaning or disappears, MINOR when one is added.

So an implementer writing a bridge for this build sends `"v": "1.0"` on every
line, accepts `1.x` from the companion, and treats `2.0` as fatal.

---

## What is verified, and what is not

Verified by `tests/unit/test_voice_*.py`, with an injected clock and nothing
sleeping: stop from idle, listening, mid-utterance, mid-plan, mid-clarification
and after wake expiry; stop from an interim low-confidence transcript; the stop
acknowledgement withheld when the port fails; barge-in on any speech including
interim; the dedup queue's collapse, supersede, stale and eviction rules; every
bound; a hostile transcript reaching the planner as a token and nothing else;
clarification asked rather than a goal guessed; a planner, a session port and a
synthesiser that raise, none of which leave the loop unable to take the next
stop.

Verified by `tests/contract/test_voice_wiring.py`, against a real `SidecarLoop`,
a real exchange directory and a fake mod: `voice check стоп` resolving to the
stop intent; a spoken stop reaching the same disarmed session `pz-agent disarm`
reaches, and the mod's latch besides; a stop decided on an **interim** transcript
with no final one ever arriving; an unrecognised phrase leaving the session and
the mod's queue exactly as they were; a latch that cannot be written reported as
a failed stop; and TeamON refusing without a client and never falling back.

Verified by `tests/contract/test_teamon_bridge_e2e.py`: the exchange over a real
pipe to a **real child process** — the framing, the byte cap, the goal tokens,
that no transcript byte reaches the bridge, that four kinds of rubbish are
refused and the exchange still completes, that an oversized line is never read
whole, that the process is gone and stays gone after a stop, and that no
POSIX-only primitive appears in either source.

### Not verified, and cannot be here

- **Anything against the real TeamON SDK.** It is not installed in this
  environment. `require_teamon_sdk()` and `teamon_sdk_available()` are exercised
  in their **absent branch only**. No call into the vendor surface has ever been
  made from this repository.
- **The bridge against a real bridge program.** The child in the contract test is
  a script the test writes: it refuses to start without `--acknowledge-fake`, it
  announces an implementation name that is not in `SUPPORTED_VOICE_ADAPTERS`, and
  it says `live: false` in its handshake. What is proven is the wire contract,
  not that a bridge built on the SDK behaves the same way.
- **Stop latency as a number.** The path is countable — one interim transcript,
  one `cancel_speech`, one synchronous `handle()`, one file write, then the mod's
  next heartbeat tick — but the two ends of it are a real recogniser and a real
  game, and neither exists here. No number is claimed.
- **Anything in-game.** The voice loop drives `SessionPort` and `PlanPort`; what
  those do inside Project Zomboid needs a live session. See
  [`LIMITATIONS.md`](LIMITATIONS.md).

## Deliberate deviations from the blueprint

- **No numbers in the confirmation.** § 5.2 asks for "результат и остаток" after
  an action, and the master prompt's example is «Готово. Голод снизился». What is
  said is «Готово.» — `phrases` is a closed table with no free slot in it, and
  `PlanRecord` carries no quantity to fill one from. Adding the remainder means a
  second closed table keyed by goal plus a value on the record; it is a phrasing
  change, not a plumbing one, and it is not done here.
- **The bound on an utterance is applied before matching.** An utterance longer
  than the cap cannot become a goal at all — matching a kind out of the surviving
  prefix would be reading half a sentence — but it is still scanned for a stop
  word first, so the safety path survives the bound. The honest limit: a stop
  word occurring *after* the cap within one utterance is not seen by that
  matcher. The mitigation is upstream, where it belongs — `is_stop` runs against
  every interim transcript, so the stop is caught while the sentence is short.
