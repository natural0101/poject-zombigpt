# 13. Стандарты кода

## 13.1. Python

- Python 3.12+;
- type hints для public API;
- strict type checking;
- `pathlib`;
- `asyncio` для lifecycle, но без бессмысленной асинхронности;
- structured logging;
- dataclasses/Pydantic только в одном domain layer;
- no broad `except Exception` без re-raise/report;
- no global mutable singleton;
- monotonic clock для TTL;
- UTC wall clock для журналов.

## 13.2. Lua/Kahlua

- один namespace;
- local functions/state;
- guard `nil`;
- `pcall` только на compatibility boundary;
- ошибки не должны молча проглатываться;
- ограничить работу в OnTick;
- no blocking loops;
- no arbitrary Java class loading;
- no direct container mutation fallback;
- версии и constants централизованы;
- dependency/load order документирован.

## 13.3. Error handling

Domain errors имеют:

- code;
- human message;
- retryable;
- diagnostics;
- cause chain.

Нельзя превращать исключение в `false` без причины.

## 13.4. Logging

Каждая action line:

```json
{
  "ts": "...",
  "level": "INFO",
  "component": "executor",
  "session_id": "...",
  "action_id": "...",
  "event": "postcondition_met",
  "data": {}
}
```

Не логировать полный inventory на INFO.

## 13.5. Testing style

- test name описывает поведение;
- no sleeps для синхронизации unit tests;
- fake monotonic clock;
- property tests для protocol;
- fixtures versioned;
- coverage safety branches.

## 13.6. Dependencies

Каждая runtime dependency обоснована.

Не добавлять:

- крупный web framework ради health endpoint;
- database server;
- browser automation;
- computer vision в core;
- три MCP SDK одновременно.

## 13.7. Security checks

- dependency audit;
- secret scan;
- static search `eval`, `exec`, `subprocess(...shell=True)`;
- Lua forbidden globals;
- localhost binding test;
- archive path traversal test.

## 13.8. Performance

Budgets:

- Lua observation tick не вызывает полный inventory scan каждый frame;
- full snapshot amortized;
- sidecar idle CPU низкий;
- memory bounded;
- logs rotate;
- planner state compact.

## 13.9. Documentation comments

Комментарии объясняют причины и ограничения, а не переписывают код.

## 13.10. Commit quality

Примеры:

```text
feat(protocol): add sequence-aware command acknowledgements
feat(lua): observe nested carried containers
fix(safety): cancel only bridge-owned timed actions
test(gameplay): add eat-from-backpack regression fixture
docs(compat): record 42.20 read-action probe
```
