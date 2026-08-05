# 18. Каталог отказов

## Session

### `STALE_SESSION`

Команда относится к прошлому запуску. Никогда не исполнять.

### `NOT_ARMED`

Write action при выключенной автоматизации.

### `BACKUP_REQUIRED`

Первый запуск на save без verified backup.

## References

### `INVALID_REF`

Ref не разрешается.

### `REF_GENERATION_MISMATCH`

Смена save/load или контейнер изменился.

### `REF_WRONG_SCOPE`

Item ref из другой сессии.

## Player

### `PLAYER_ABSENT`

Нет активного персонажа.

### `PLAYER_DEAD`

Сессия закрывается.

### `PLAYER_BUSY_MANUAL_ACTION`

Не вмешиваться.

### `USER_TAKEOVER`

Отмена и suspend.

## Movement

### `TARGET_UNLOADED`

Нельзя получить square.

### `DISTANCE_LIMIT`

Команда слишком дальняя.

### `PATH_NOT_FOUND`

Pathfinder не нашёл путь.

### `PATH_STUCK`

Нет наблюдаемого прогресса.

### `UNSAFE_TARGET`

Огонь, падение, запрещённое окно.

## Inventory

### `ITEM_NOT_IN_SOURCE`

Stale state или ручное перемещение.

### `DESTINATION_FULL`

Выбрать другой контейнер или спросить.

### `DESTINATION_REJECTS_ITEM`

Не retry.

### `TRANSFER_POSTCONDITION_FAILED`

Нельзя использовать direct mutation fallback.

## Consume

### `NO_SAFE_FOOD`

Сообщить варианты/причины.

### `FOOD_RESERVED`

Нужно подтверждение.

### `UNSAFE_FOOD`

Жёсткий отказ.

### `NO_SAFE_DRINK`

Не пить tainted/алкоголь автоматически.

### `CONSUME_NO_EFFECT`

Не считать success, не повторять бесконечно.

## Reading

### `ILLITERATE_TRAIT`

Capability конкретного персонажа отсутствует.

### `SKILL_LEVEL_MISMATCH`

Предложить подходящую литературу.

### `ALREADY_READ`

No-op либо выбрать другую.

### `READ_INTERRUPTED`

Можно resume только после нового решения.

## Safety

### `THREAT_INTERRUPTED`

Current task suspended.

### `PANIC_STOP`

Terminal cancel, disarm.

### `HEARTBEAT_LOST`

Новые действия запрещены.

## Protocol

### `SEQ_CONFLICT`

Full resync.

### `LEASE_EXPIRED`

Reject.

### `MALFORMED_MESSAGE`

Quarantine line, diagnostic.

### `PAYLOAD_TOO_LARGE`

Reject before parse into domain.

## Internal

### `API_PROBE_FAILED`

Capability downgraded.

### `UNEXPECTED_ENGINE_ERROR`

Capture minimal diagnostics, safe failure.

### `DISK_FULL`

Stop journaling safely, block mutation if protocol integrity cannot be guaranteed.
