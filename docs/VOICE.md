# Voice companion

`packages/pz_agent_voice/` implements the blueprint's phase 8 (§ 11.4, § 5.17,
master prompt "Этап 7. Голос"). Core never imports it: speech is an interface to
the agent, not part of its decision loop.

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

## Not speaking

The blueprint forbids narrating every tick, every internal transfer and every
observation. `UtteranceQueue` makes that structural rather than aspirational:
messages carry a `topic`, and a second message about a topic that already has one
pending *replaces* it instead of queueing behind it. Identical messages collapse,
stale ones (a lower `revision`) are dropped, and the queue is bounded by distinct
subject — at the cap the least urgent, oldest pending message is evicted, and a
newcomer less urgent than everything pending is refused. A `STOP` utterance
cannot be evicted.

`VoiceSession.report_plan` may be called at whatever rate the sidecar observes
plans; it returns `None` and enqueues nothing unless the status actually changed,
and only a terminal status is spoken.

## Event stream

`TtsEventStream` publishes every transition an utterance makes — `queued`,
`collapsed`, `superseded`, `dropped`, `cleared`, `started`, `cancelled`,
`finished` — so a consumer can see what was *not* said as well as what was.
Subscribers and history are bounded; a subscriber that raises is unsubscribed and
its failure recorded rather than breaking the speaker.

## Adapters

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

## What is verified, and what is not

Verified by `tests/unit/test_voice_*.py`, with an injected clock and nothing
sleeping: stop from idle, listening, mid-utterance, mid-plan, mid-clarification
and after wake expiry; stop from an interim low-confidence transcript; the stop
acknowledgement withheld when the port fails; barge-in on any speech including
interim; the dedup queue's collapse, supersede, stale and eviction rules; every
bound (queue, subscribers, event history, turn history, transcript length,
clarification budget); a hostile transcript reaching the planner as the token
`eat` and nothing else; clarification asked rather than a goal guessed.

Not verified, and cannot be here:

- **Anything against the real TeamON SDK.** The package is not installed in this
  environment. `require_teamon_sdk()` and `teamon_sdk_available()` are exercised
  in their absent branch only.
- **Stop latency as a number.** Acceptance item H04 asks for a measured latency.
  The code path is straight-line and free of IO, but no measurement exists
  without a real recogniser and a real synthesiser.
- **Anything in-game.** The voice loop drives `SessionPort` and `PlanPort`; what
  those do inside Project Zomboid needs a live session.
