# 2. Архитектура

## 2.1. Общая схема

```text
┌─────────────────────────────────────────────────────────────┐
│ TeamON / голосовой контур                                   │
│ STT → Intent → MCP client → TTS / barge-in                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP stdio
┌──────────────────────────▼──────────────────────────────────┐
│ pz-agent-mcp                                                │
│ typed tools, resources, subscriptions, policy boundary      │
└──────────────────────────┬──────────────────────────────────┘
                           │ local typed API
┌──────────────────────────▼──────────────────────────────────┐
│ pz-agent-core / sidecar                                     │
│ session manager, planner, executor, memory, logs, watchdog   │
├───────────────────────┬───────────────────────┬─────────────┤
│ deterministic policy  │ optional LLM planner  │ voice adapter│
└───────────────────────┴────────────┬──────────┴─────────────┘
                                    │ atomic file IPC
┌───────────────────────────────────▼─────────────────────────┐
│ PZAgentBridge — клиентский Lua-мод                          │
│ observations, capabilities, command queue, timed actions,   │
│ reflex guard, manual takeover, HUD                          │
└───────────────────────────────────┬─────────────────────────┘
                                    │ Project Zomboid APIs
┌───────────────────────────────────▼─────────────────────────┐
│ Project Zomboid 42.20 Stable                                │
└─────────────────────────────────────────────────────────────┘
```

## 2.2. Почему гибридная архитектура

LLM слишком медленный и недетерминированный для покадрового управления. Lua-мод находится внутри игры, имеет доступ к объектам мира и timed actions, но не должен содержать сетевой клиент и сложный ИИ. Поэтому:

- Lua отвечает за наблюдение и фактическое исполнение;
- sidecar отвечает за протокол, lifecycle, память и валидацию;
- deterministic policy отвечает за срочные потребности;
- LLM отвечает только за высокоуровневые цели и выбор из разрешённых инструментов;
- reflex guard может прервать LLM без ожидания ответа.

## 2.3. Частоты контуров

- игровой `OnTick`: минимальная проверка stop/takeover/threat;
- reflex loop: 10–20 Гц, только дешёвые локальные проверки;
- observation diff: 4 Гц в активном действии, 1 Гц в ожидании;
- planner: по событию или не чаще 0.5–1 Гц;
- voice status: по существенным переходам, без спама;
- full snapshot: при подключении, рассинхронизации или запросе.

## 2.4. Компоненты Lua-мода

Рекомендуемые модули:

```text
PZAgent_Main.lua
PZAgent_Config.lua
PZAgent_Capabilities.lua
PZAgent_IPC.lua
PZAgent_Observation.lua
PZAgent_Inventory.lua
PZAgent_World.lua
PZAgent_ActionQueue.lua
PZAgent_Actions_Move.lua
PZAgent_Actions_Transfer.lua
PZAgent_Actions_Consume.lua
PZAgent_Actions_Read.lua
PZAgent_Actions_Interact.lua
PZAgent_Reflex.lua
PZAgent_Takeover.lua
PZAgent_Telemetry.lua
PZAgent_UI.lua
PZAgent_Compat.lua
```

Каждый модуль владеет своим состоянием. Глобальная таблица используется только как namespace. Межмодульные записи идут через публичные функции.

## 2.5. Компоненты sidecar

```text
session/
ipc/
protocol/
capabilities/
planner/
policy/
executor/
memory/
voice/
mcp/
diagnostics/
installer/
```

Sidecar не должен зависеть от активного LLM для запуска, остановки, наблюдения и детерминированных сценариев.

## 2.6. Transport

Основной MCP transport — `stdio`. Локальный HTTP/WebSocket допускается только для dashboard/voice adapter и должен:

- слушать `127.0.0.1`;
- использовать случайный session token;
- не открывать firewall;
- отключаться конфигурацией;
- не быть необходимым для ядра.

## 2.7. IPC между игрой и sidecar

Предпочтительный вариант — append-only JSONL queues и отдельные atomic snapshot-файлы:

```text
~/Zomboid/Lua/pz_agent/
  session.json
  capabilities.json
  observation.snapshot.json
  observation.events.jsonl
  command.queue.jsonl
  command.ack.jsonl
  heartbeat.game.json
  heartbeat.sidecar.json
  panic.stop
```

Требования:

- монотонный `seq`;
- `session_id`;
- `command_id`;
- TTL/lease;
- idempotency;
- ack `accepted`, `started`, `progress`, `succeeded`, `failed`, `cancelled`;
- атомарная запись через временный файл и rename там, где возможно;
- устойчивость к обрыву посередине записи;
- ограничение размеров и ротация;
- запрет `..` и произвольных путей;
- очистка старой сессии по идентификатору, а не слепое удаление каталога.

## 2.8. Не использовать как основной канал

- чтение пикселей для каждого решения;
- эмуляцию клавиш для еды/питья/инвентаря;
- RCON для управления локальным персонажем;
- прямое изменение характеристик персонажа;
- произвольный `eval` Lua;
- TCP-сервер внутри Kahlua;
- polling без sequence/ack.
