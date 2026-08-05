# 9. Тестирование и доказательства

## 9.1. Пирамида

```text
много unit
→ contract
→ component integration
→ Lua harness
→ live game smoke
→ endurance
```

## 9.2. Unit matrix

### Protocol

- encode/decode UTF-8;
- duplicated seq;
- gap;
- incomplete line;
- stale session;
- expired lease;
- idempotent replay;
- rotation;
- oversized payload.

### Inventory

- nested bags;
- duplicate display names;
- disappearing item;
- capacity;
- equipped item;
- favorite/reserve;
- stale ref;
- partial stacks.

### Food

- safe fresh;
- raw dangerous;
- rotten;
- poison;
- last reserve;
- fraction;
- no candidate;
- hunger critical override.

### Drink

- clean water;
- tainted water;
- alcohol;
- last bottle;
- partial uses;
- world source capability false.

### Reading

- illiterate;
- wrong level;
- already read;
- magazine;
- recipe;
- interruption;
- resume.

### Policy

- manual takeover beats goal;
- threat beats reading;
- bleed beats hunger;
- stop beats all;
- P4 permission;
- loop breaker.

## 9.3. Contract fixtures

Fixtures должны быть human-readable и версионироваться:

```text
tests/fixtures/protocol/v1/
tests/fixtures/observations/
tests/fixtures/actions/
tests/fixtures/errors/
```

Каждая schema имеет valid и invalid examples.

## 9.4. Lua tests

Тестировать pure logic отдельно от engine adapters.

Engine adapter mocks должны:

- фиксировать вызванную сигнатуру;
- моделировать queue;
- моделировать completion;
- не утверждать наличие реального API.

## 9.5. Compatibility probes

Runtime probe не должен портить мир. Probe может:

- проверить наличие global/class/function;
- создать объект только в безопасной fixture, если нужно;
- выполнить no-op/read-only;
- записать результат.

Mutating probe выполняется только в test save и явно маркируется.

## 9.6. Live smoke protocol

Каждый smoke case сохраняет:

- build;
- mod version;
- sidecar version;
- save fixture;
- initial observation;
- command;
- ack stream;
- terminal observation;
- console excerpt;
- pass/fail;
- screenshot optional.

## 9.7. Fixture setup

Не коммитить save. Создать документ/скрипт, который помогает пользователю подготовить:

- безопасный дом;
- пустой main inventory;
- рюкзак с яблоком/консервой/бутылкой/книгой;
- шкаф;
- препятствие;
- один контролируемый zombie scenario.

## 9.8. Smoke cases

### S01 Heartbeat

Ожидается connect за ограниченное время.

### S02 Move

Три клетки по безопасному полу.

### S03 Manual takeover

На середине движения пользователь нажимает movement key.

### S04 Transfer nested

Предмет из рюкзака в main.

### S05 Eat

Еда в рюкзаке, hunger выше baseline.

### S06 Drink

Чистая вода в бутылке.

### S07 Read

Подходящая книга.

### S08 Read interrupt

Появление threat/ручной stop.

### S09 Stale ref

Item перемещён пользователем до action.

### S10 Path blocked

Закрытая/недоступная цель.

### S11 Sidecar restart

Во время idle и action.

### S12 Game restart

Старые refs invalid.

### S13 Backup restore

Игра закрыта.

### S14 Invalid IPC

Malformed line.

### S15 Autonomous maintenance

30 минут.

## 9.9. Acceptance thresholds

- 100% protocol contract tests;
- 100% safety critical unit tests;
- no secret findings;
- no critical lint/type errors;
- no unbounded logs;
- no action replay;
- manual takeover latency измерена;
- panic stop latency измерена;
- false success = 0 в smoke suite;
- save restore verified.

## 9.10. CI

Linux CI:

- Python/Rust tests;
- schemas;
- Lua lint/tests;
- security.

Windows CI:

- package build;
- path handling;
- installer dry-run;
- CLI tests.

Live game tests не обязаны работать в GitHub-hosted CI, но имеют локальный runner workflow и evidence format.

## 9.11. Regression policy

Каждый исправленный runtime bug получает:

- fixture;
- regression test;
- changelog;
- compatibility note, если API drift.
