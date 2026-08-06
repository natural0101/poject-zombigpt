# Voice companion

`packages/pz_agent_voice/` implements the blueprint's phase 8 (§ 11.4, § 5.17,
master prompt "Этап 7. Голос"). Core never imports it: speech is an interface to
the agent, not part of its decision loop.

## Starting it

```powershell
.venv\Scripts\pz-agent voice run          # listen, on the configured adapter
.venv\Scripts\pz-agent voice check стоп   # what does this phrase resolve to?
```

Nothing else starts the companion. The sidecar does not spawn it, arming does not
imply it, and `voice.enabled = true` on its own listens to nothing — it is a
setting `voice run` reads, not a process. `pz-agent status` prints which of those
two states a machine is in, so "I enabled voice and it does not hear me" is
answerable without guessing.

`voice run` refuses rather than half-starting, and says which of these it was:
the configuration did not validate, `voice.enabled` is false, `voice.adapter` is
`"none"`, the key named by `voice.api_key_env` is not in the environment, or the
adapter could not be constructed. It never falls back — see *Adapters* below.

`voice check` needs no game, no session, no configuration and no microphone. It
runs the phrase through the same `classify()` the session runs, prints the word
tokens the matcher actually compared, the intent, and what would happen; it exits
non-zero when nothing matched, so a script can ask whether a phrase works. It is
the answer to "why was «стоп» not recognised" — usually the tokens are the
answer, because the matcher compares whole words and «стопка» is not one.

## Configuration

```toml
[voice]
enabled = true
adapter = "teamon"                      # or "none"
api_key_env = "PZ_AGENT_TEAMON_API_KEY" # the *name* of the variable
```

`api_key_env` names an environment variable and never holds the key, the same
rule and the same validator the planner's providers use: a key pasted here is
refused by `validate-config` before anything starts, because a key written into
this file is a key in every copy of this file.

`[voice]` carries only what a user chooses. The loop's own bounds — wake words,
wake TTL, confidence floor, queue depth, clarification budget, plan limits —
are `DEFAULT_VOICE_CONFIG` in `pz_agent_voice.config`, validated at construction.

## The ports

`VoiceServices` is built by `pz_agent_cli.voice.voice_services()` over the same
exchange directory the sidecar and the mod share.

`SessionPort.status()` reads the game's heartbeat. A missing or stale heartbeat
is `connected = False`, and a heartbeat that does not mention arming reads as
disarmed: an omission is not a claim.

`SessionPort.stop()` writes the mod's own panic latch, `panic.stop`, and that is
the whole route. `PZAgent.Runtime.tick` reads it on every heartbeat tick and
stops what the game is running; `SidecarLoop` reads it on every tick and before
every submission, disarms, and closes in-flight work as lost. One write reaches
both, and it reaches them **with no sidecar running at all** — which the control
channel `pz-agent disarm` uses does not. The file is written whole and not
atomically because the mod never parses it: any non-empty content is a stop, so a
torn write is still a stop, and an fsync on this path would buy nothing.

Returning from `stop()` is a claim that the *request* is in force — the latch is
on disk and its size was checked — not that the game has already stopped. That
second fact is not observable synchronously: the mod applies the latch on its
next heartbeat tick and clears the file afterwards. `StopReport.cleared` is
therefore `0`, because the count of mod-owned entries belongs to the mod, and
`disarmed` reports what the last heartbeat said rather than what the stop is
about to cause. If the write fails, `stop()` raises and «Остановился.» is
withheld.

`SessionPort.arm()` refuses, always. §7.7 puts a verified backup and an explicit
request between a save and an armed agent, and a microphone in a room with a
television is not an explicit request. `pz-agent arm` is the only way to arm.

**`PlanPort` is not wired, and says so.** There is no channel in this build that
carries a goal from a second process into the running sidecar: the control
channel carries arm, disarm and stop, and the planner proposes from observations
rather than from requests. So `UnroutedPlanPort` raises, the companion says
«Не получилось.», nothing is submitted anywhere, and `pz-agent status` prints
the gap. The alternative — writing to the command queue the sidecar owns — would
put the microphone past the reflex guard, the capability gate and the policy
engine in one step, which is the privileged caller `pz_agent_voice.ports` exists
to refuse. A spoken **stop** and a spoken **status** work; a spoken **goal** is
refused. `voice check` says so for any phrase that resolves to a goal.

## The contract

```python
class VoiceAdapter(Protocol):
    async def events(self) -> AsyncIterator[VoiceInput]: ...
    async def speak(self, message: VoiceOutput) -> None: ...
    async def cancel_speech(self) -> None: ...
```

`events` is a coroutine that *returns* an async iterator rather than being an
async generator. That is the blueprint's signature and it is what lets a backend
open a socket or a microphone before the first transcript exists; an
implementation keeps its generator in a second method and returns it.

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
  state, then calls `SessionPort.stop()` — the blueprint's shortest path
  (§ 6.16), which needs no armed state.
- "Остановился." is spoken **only after the port acknowledges** (§ 5.17 step 6).
  If the port raises, the failure is what gets said.

`VoiceSession.stop()` is public so a panic hotkey reaches exactly the same code
as a stop word.

## A transcript is data

The only thing that crosses from the microphone to the planner is a member of
`VoiceGoal` (`eat`, `drink`, `read`, `resume`), submitted as a `PlanRequest`
through the same `PlanPort` every other caller uses — so a spoken goal meets the
same validation, limits and permission policy as one that arrived as an MCP tool
call. Transcript text is matched against a closed vocabulary in
`pz_agent_voice.intent` and then dropped. It is never forwarded, never stored and
never spoken back: every sentence the companion can say lives in
`pz_agent_voice.phrases`, and none of them has a slot for free text.

That is the shape of the channel; in this build the port at the end of it
refuses, and *The ports* above says why. The width of the channel is the point
either way: whatever is eventually wired there receives a token from a closed
enum, never a transcript.

## Not speaking

The blueprint forbids narrating every tick, every internal transfer and every
observation. `UtteranceQueue` makes that structural rather than aspirational:
messages carry a `topic`, and a second message about a topic that already has one
pending *replaces* it instead of queueing behind it. Identical messages collapse
(`collapsed`), stale ones — a lower `revision` — are discarded (`dropped`, since
nothing else is going to say what they said), and the queue is bounded by distinct
subject: at the cap the least urgent, oldest pending message is evicted, and a
newcomer less urgent than everything pending is refused. A `STOP` utterance
cannot be evicted.

`VoiceSession.report_plan` may be called at whatever rate the sidecar observes
plans; it returns `None` and enqueues nothing unless the status actually changed,
and only a terminal status is spoken.

## When a part of the system stops answering

None of these end the conversation, because the transcript that arrives next may
be "стоп":

- **The planner raises.** The goal is reported refused, nothing is submitted, and
  the exception type and message go into `VoiceTurn.detail`.
- **`SessionPort.stop()` raises.** "Остановился." is withheld and
  "Не смог остановить. Останови вручную." is said instead.
- **`SessionPort.status()` raises.** The port is documented to answer even when
  the game is gone, so this is a broken port rather than a disconnected game. It
  is caught anyway: not knowing the state is not the same as knowing it is fine,
  so nothing is submitted and "Нет связи с игрой." is said.
- **The synthesiser raises.** The utterance is reported `cancelled` — it was not
  heard — the reason is kept in `VoiceCompanion.speech_failures` (bounded), and
  the speech pump carries on. A backend that broke once must not leave the
  companion mute for the rest of the session.
- **A listener on the TTS stream raises.** It is unsubscribed and its failure
  recorded; the other listeners still receive.

`VoiceCompanion.run` gives its subscription back on every exit — a stream that
never opened, a failure, or a clean shutdown — and on cancellation it abandons
speech in flight rather than waiting for a backend that may never return.

## Event stream

`TtsEventStream` publishes every transition an utterance makes — `queued`,
`collapsed`, `superseded`, `dropped`, `cleared`, `started`, `cancelled`,
`finished` — so a consumer can see what was *not* said as well as what was.
Subscribers and history are bounded; a subscriber that raises is unsubscribed and
its failure recorded rather than breaking the speaker.

## Adapters

`voice.adapter` selects one, and `SUPPORTED_VOICE_ADAPTERS` is `("teamon",
"none")`. **`FakeVoiceAdapter` is not in it and must never be**: it answers
scripted transcripts, so selecting it would leave a user being told the companion
is listening while «стоп» reached a list. `select_adapter()` refuses it even when
handed a configuration no validator would have produced, and refuses to fall back
to it — a configured adapter that cannot be built stops the command.

`FakeVoiceAdapter` is deterministic and does no IO. It takes scripted inputs
(final or interim, with a confidence), captures what it was asked to say, and
with `hold_speech=True` parks inside `speak` until the test finishes or cancels
the utterance — which is how "stop while the adapter is mid-sentence" is
exercised.

`TeamONVoiceAdapter` is written against `TeamONClient`, a three-method protocol
this package defines and documents in `adapters/teamon.py`. The vendor SDK is not
installed here and its surface is unverified, so no call to it is invented: the
only vendor import in the project is `require_teamon_sdk()`, which reports a
missing install step rather than an `ImportError`. Binding `TeamONClient` to the
real SDK is the integrator's three methods; everything the adapter itself owns —
clamping an out-of-range confidence, bounding an over-long transcript, mapping
urgency to priority, deciding that a synthesis call which returned after an
interrupt did not deliver its sentence — is implemented here and tested.

That has a consequence a user meets immediately: **`voice run` cannot start a
TeamON session in this build.** With the SDK absent it refuses with the install
step; with the SDK present it refuses because no binding from it to
`TeamONClient` has been verified from this repository, and none is invented.
Whoever has the SDK writes those three methods and passes the client to
`select_adapter(..., client=...)`; that seam is exercised by
`tests/contract/test_voice_wiring.py`, which asserts that a supplied client is
what gets constructed and that nothing else ever is.

## What is verified, and what is not

Verified by `tests/unit/test_voice_*.py`, with an injected clock and nothing
sleeping: stop from idle, listening, mid-utterance, mid-plan, mid-clarification
and after wake expiry; stop from an interim low-confidence transcript; the stop
acknowledgement withheld when the port fails; barge-in on any speech including
interim; the dedup queue's collapse, supersede, stale and eviction rules; every
bound (queue, subscribers, event history, listener failures, turn history, speech
failures, transcript length, clarification budget); a hostile transcript reaching
the planner as the token `eat` and nothing else; clarification asked rather than a
goal guessed, including that a misheard *answer* to one buys a repeat rather than
a plan; a planner, a session port and a synthesiser that raise, none of which
leave the loop unable to take the next stop.

Verified by `tests/contract/test_voice_wiring.py`, against a real `SidecarLoop`,
a real exchange directory and a fake mod: `pz-agent voice check стоп` resolving
to the stop intent; a spoken stop reaching the same disarmed session
`pz-agent disarm` reaches, and reaching the mod's latch besides, which `disarm`
does not; a stop decided on an **interim** transcript with no final one ever
arriving; an unrecognised phrase leaving the session and the mod's queue exactly
as they were; a spoken goal refused and submitted nowhere; a latch that cannot be
written reported as a failed stop; TeamON refusing without a client and never
falling back; and `status` printing the configured adapter, a listening companion
and the goal gap. Verified by `tests/unit/test_cli_voice.py`: every reading of
the session port, every refusal `voice run` can produce, and the record's
round-trip and malformed forms.

## Deliberate deviations from the blueprint

- **No numbers in the confirmation.** § 5.2 asks for "результат и остаток" after
  an action, and the master prompt's example is «Готово. Голод снизился». What is
  said is «Готово.» — `phrases` is a closed table with no free slot in it, and
  `PlanRecord` carries no quantity to fill one from. Adding the remainder means a
  second closed table keyed by goal plus a value on the record; it is a phrasing
  change, not a plumbing one, and it is not done here.
- **A spoken goal reaches no planner.** § 11.4 has the transcript become a
  `PlanRequest` on the `PlanPort`, and it does — the port is just one that
  refuses, because this build has no channel from a second process into the
  running sidecar's planner. The gap is reported to the user, recorded in the
  voice record and printed by `status` rather than closed by inventing a route
  into the command queue. Closing it honestly means a goal channel the sidecar
  consumes, which is a protocol change, not a wiring change.

Not verified, and cannot be here:

- **Anything against the real TeamON SDK.** The package is not installed in this
  environment. `require_teamon_sdk()` and `teamon_sdk_available()` are exercised
  in their absent branch only.
- **Stop latency as a number.** Acceptance item H04 asks for a measured latency.
  The path is now countable — one interim transcript, one `cancel_speech`, one
  synchronous `handle()`, one file write, then the mod's next heartbeat tick
  (ten game ticks) — but the two ends of it are a real recogniser and a real
  game, and neither exists here. No number is claimed.
- **Anything in-game.** The voice loop drives `SessionPort` and `PlanPort`; what
  those do inside Project Zomboid needs a live session.
