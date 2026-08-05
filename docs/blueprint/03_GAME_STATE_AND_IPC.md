# 3. Состояние игры и IPC

## 3.1. Цели протокола

Протокол между Lua-модом и sidecar должен быть:

- локальным;
- наблюдаемым;
- устойчивым к падению любой стороны;
- идемпотентным;
- версионируемым;
- ограниченным по объёму;
- безопасным для Kahlua;
- не зависящим от точного тайминга polling.

Файловый IPC выбран потому, что внутри Project Zomboid доступна безопасная работа с файлами в пользовательском Lua-каталоге, а сетевое взаимодействие из модов ограничено. Файлы не должны превращаться в неконтролируемую shared mutable state: протокол строится как журнал команд и подтверждений.

## 3.2. Каталог

```text
%USERPROFILE%\Zomboid\Lua\pz_agent\
```

Все имена фиксированы в коде. Команда не может передать произвольное имя файла.

```text
session.json
capabilities.json
observation.snapshot.json
observation.events.0001.jsonl
command.queue.0001.jsonl
command.ack.0001.jsonl
heartbeat.game.json
heartbeat.sidecar.json
panic.stop
logs/
```

## 3.3. Session handshake

Sidecar создаёт:

```json
{
  "protocol_version": "1.0",
  "session_id": "uuid",
  "created_at_ms": 0,
  "sidecar_version": "0.1.0",
  "requested_observation_hz": 4,
  "mode": "observe",
  "nonce": "random"
}
```

Lua принимает только новую сессию, если:

- session id корректен;
- protocol major поддерживается;
- timestamp не слишком старый;
- nonce отличается от предыдущей сессии;
- sidecar heartbeat жив.

Lua отвечает в `heartbeat.game.json` с тем же session id и собственным nonce.

## 3.4. Sequence

Каждый поток имеет независимый монотонный sequence:

- observation seq;
- command seq;
- ack seq;
- event seq.

При gap:

- sidecar просит full snapshot;
- Lua не пытается угадывать пропущенную команду;
- duplicate command распознаётся по `command_id` и idempotency key;
- уже завершённая команда возвращает прежний terminal result.

## 3.5. Atomic write

Для snapshot:

1. записать `filename.tmp.<pid>.<seq>`;
2. flush/close;
3. заменить целевой файл;
4. sidecar проверяет JSON целиком.

Для JSONL:

- одна запись — одна строка;
- newline обязателен;
- неполная последняя строка игнорируется до следующего чтения;
- reader хранит byte offset;
- после ротации читатель получает rotate event;
- файлы ограничены размером.

Если атомарный rename через доступный API не подтверждён внутри Lua, использовать две alternating slots:

```text
observation.snapshot.a.json
observation.snapshot.b.json
observation.snapshot.pointer
```

Pointer обновляется последним.

## 3.6. Observation tiers

### Tier 0 — heartbeat

- session;
- seq;
- game version;
- mod version;
- player present;
- armed;
- mode;
- active action;
- danger level.

### Tier 1 — compact diff

Только изменившиеся scalar fields и списки refs.

### Tier 2 — full snapshot

Полное состояние, используемое при подключении и recovery.

### Tier 3 — requested detail

Содержимое конкретного контейнера, подробность раны, описание клетки, candidate path.

## 3.7. Stable references

Нельзя передавать Java/Lua object pointer.

### Item ref

```text
item:<session>:<container-ref>:<runtime-id>:<generation>
```

Ref живёт только в текущей сессии. После load/save transition generation увеличивается.

### Container ref

```text
container:<session>:player-main
container:<session>:worn:<slot>:<item-runtime-id>
container:<session>:world:<x>:<y>:<z>:<object-index>:<container-index>
```

### Square ref

```text
square:<session>:<x>:<y>:<z>
```

### Entity ref

```text
zombie:<session>:<online-or-runtime-id>:<generation>
```

Перед действием ref повторно разрешается и валидируется.

## 3.8. Capability negotiation

`capabilities.json` содержит не обещания, а результаты probes:

```json
{
  "build": "42.20",
  "protocol_version": "1.0",
  "capabilities": {
    "move_to_square": {"state": "verified"},
    "inventory_transfer": {"state": "verified"},
    "eat_percentage": {"state": "verified"},
    "drink_carried": {"state": "verified"},
    "read_literature": {"state": "verified"},
    "drink_world_source": {"state": "experimental"},
    "autonomous_attack": {"state": "unsupported", "reason": "NO_VERIFIED_API"}
  }
}
```

States:

- `verified`;
- `available_unverified`;
- `experimental`;
- `unsupported`;
- `disabled_by_policy`.

MCP не публикует write tool как готовый, если capability unsupported.

## 3.9. Command envelope

```json
{
  "protocol_version": "1.0",
  "session_id": "uuid",
  "seq": 12,
  "command_id": "uuid",
  "idempotency_key": "goal-step-attempt",
  "issued_at_ms": 0,
  "lease_ms": 10000,
  "expected_observation_seq": 55,
  "action": "consume.eat",
  "args": {
    "item_ref": "item:...",
    "fraction": 0.5
  },
  "policy": {
    "allow_interrupt": true,
    "max_retries": 1
  }
}
```

## 3.10. Ack lifecycle

```text
received
accepted
rejected
started
progress
succeeded
failed
cancelled
lost
```

Terminal:

- succeeded;
- failed;
- cancelled;
- rejected;
- lost.

Reason codes должны быть стабильными:

```text
NOT_ARMED
STALE_SESSION
LEASE_EXPIRED
SEQ_CONFLICT
CAPABILITY_UNAVAILABLE
INVALID_REF
PRECONDITION_FAILED
PLAYER_BUSY_MANUAL_ACTION
PATH_NOT_FOUND
PATH_STUCK
THREAT_INTERRUPTED
USER_TAKEOVER
QUEUE_REJECTED
ACTION_TIMEOUT
POSTCONDITION_FAILED
GAME_DISCONNECTED
SAVE_CHANGED
INTERNAL_ERROR
```

## 3.11. Backpressure

- Lua не принимает более одной mutating command одновременно.
- Sidecar может держать план, но отправляет следующий шаг только после terminal ack.
- Stop обходит очередь.
- Observation events могут coalesce.
- При перегрузке сбрасываются вторичные telemetry events, но не action result и safety events.

## 3.12. Recovery

### Sidecar restart

- читает session;
- создаёт новый sidecar nonce;
- не переисполняет команды;
- получает active action;
- либо продолжает monitor, либо cancel по policy.

### Game restart

- новая session generation;
- все старые refs invalid;
- sidecar закрывает active commands как `lost`;
- требует re-arm.

### Save/load

- save id изменился;
- cache world refs очищается;
- user preferences остаются;
- autonomous mode не включается автоматически.

## 3.13. Приватность

По умолчанию protocol не передаёт:

- полный Windows username;
- абсолютный путь;
- Steam token;
- chat text;
- список процессов;
- содержимое произвольных файлов.

Диагностический bundle должен уметь redact paths и secrets.
