# 6. MCP API

## 6.1. Дизайн

MCP boundary должен быть небольшим, строгим и пригодным для голосового агента. Tool descriptions не должны побуждать модель импровизировать raw commands.

Все mutating tools принимают:

- `idempotency_key`;
- `timeout_ms`;
- optional `expected_observation_seq`;
- optional `reason`.

## 6.2. `pz_session_status`

Read-only.

Возвращает:

```json
{
  "connected": true,
  "game_build": "42.20",
  "mode": "ASSISTED",
  "armed": true,
  "danger_level": "none",
  "active_action": null,
  "observation_seq": 55
}
```

## 6.3. `pz_session_arm`

Аргументы:

```json
{
  "mode": "ASSISTED",
  "confirm_backup": true
}
```

Отказывает, если:

- backup не создан;
- compatibility doctor failed;
- game build unsupported;
- player absent;
- stale heartbeat.

## 6.4. `pz_session_disarm`

Отменяет mod-owned actions и запрещает новые.

## 6.5. `pz_observe_snapshot`

Параметры detail level:

- compact;
- standard;
- full.

Full ограничен размером.

## 6.6. `pz_observe_inventory`

```json
{
  "scope": "carried",
  "include_nested": true,
  "category": "food",
  "safe_only": true
}
```

## 6.7. `pz_observe_nearby`

```json
{
  "radius": 10,
  "types": ["container", "door", "water_source", "zombie"]
}
```

## 6.8. `pz_action_move_to`

Принимает coordinate/square ref/known location id. Не принимает свободный код.

## 6.9. `pz_action_transfer`

Только item ref и container ref.

## 6.10. `pz_action_eat`

Варианты:

```json
{"selection": "best_safe", "target_hunger": 0.15}
```

или:

```json
{"item_ref": "item:...", "fraction": 0.5}
```

`best_safe` выбирает deterministic policy в core, а не LLM.

## 6.11. `pz_action_drink`

```json
{"selection": "best_safe", "target_thirst": 0.1}
```

## 6.12. `pz_action_read`

```json
{
  "selection": "best_for_boredom",
  "until": {"type": "minutes", "value": 15}
}
```

## 6.13. `pz_action_wait`

```json
{
  "until": {
    "type": "duration_or_event",
    "duration_ms": 10000,
    "events": ["danger", "voice_command", "need_critical"]
  }
}
```

## 6.14. `pz_action_cancel`

Отменяет один action id, только если он mod-owned.

## 6.15. `pz_plan_execute`

Принимает пользовательскую цель, но не raw steps от внешнего недоверенного источника:

```json
{
  "goal": "поесть и продолжить читать",
  "mode": "ASSISTED",
  "limits": {
    "max_steps": 8,
    "max_real_seconds": 120
  }
}
```

Plan проходит validation.

## 6.16. `pz_safety_stop`

Всегда доступен. Не требует armed state. Должен иметь кратчайший путь.

## 6.17. `pz_memory_query`

Read-only semantic memory. Не возвращает secrets.

## 6.18. `pz_debug_doctor`

Возвращает checks и remediation.

## 6.19. `pz_debug_tail`

Ограниченный журнал. Фильтры:

- action id;
- level;
- component;
- last N.

## 6.20. Tool response

```json
{
  "ok": true,
  "request_id": "uuid",
  "action_id": "uuid",
  "status": "started",
  "message": "Начинаю чтение",
  "data": {},
  "warnings": []
}
```

Errors:

```json
{
  "ok": false,
  "error": {
    "code": "NOT_ARMED",
    "message": "Автоматизация выключена",
    "retryable": false,
    "details": {}
  }
}
```

## 6.21. Resources

Resources кэшируются и имеют ETag/seq.

## 6.22. Subscriptions

События:

- session_changed;
- action_progress;
- action_terminal;
- danger_changed;
- need_changed;
- manual_takeover;
- voice_message;
- plan_changed.

## 6.23. Ограничение контекста

MCP server должен возвращать compact summaries. Полный инвентарь на сотни предметов доступен через pagination/filter. LLM не получает каждый sprite, body part или log line без запроса.
