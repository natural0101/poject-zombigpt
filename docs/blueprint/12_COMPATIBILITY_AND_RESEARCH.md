# 12. Совместимость и исследовательская база

## 12.1. Зафиксированная цель

На дату подготовки задания Project Zomboid **42.20** выпущен в публичную Stable-ветку. Реализация всё равно обязана определять фактическую версию локальной установки и не полагаться только на этот документ.

## 12.2. Подтверждённые архитектурные выводы

### Lua timed actions

Актуальные community stubs для Build 42 содержат типы для:

- `ISWalkToTimedAction`;
- `ISInventoryTransferAction`;
- `ISEatFoodAction`;
- `ISDrinkFromBottle`;
- `ISReadABook`.

Это подтверждает правильность базового направления: использовать игровые действия, а не UI-макросы. Stubs не заменяют runtime probe.

### File APIs

Build 42 modding API содержит safe file reader/writer в Lua cache. Поэтому локальный файловый bridge реалистичен. Конкретные сигнатуры проверяются по установленной версии.

### Существовавший AutoPilot

Публичный проект `rodmen07/auto-pilot-pz` был деактивирован автором не из-за технической неработоспособности. В нём были реализованы survival state machine, nested inventory, еда, питьё, чтение, сон, медицинские действия, телеметрия и тесты для Build 42.19. Использовать его как источник идей и негативных уроков, но:

- не копировать слепо;
- проверить лицензию;
- не считать совместимым с 42.20 без probes;
- не наследовать его узкую цель auto-leveler;
- не считать autonomous combat решённым.

### MCP server для моддинга

Существующий `wink-/pz-mcp-server` предназначен для поиска vanilla content, генерации и проверки мод-скриптов. Это полезная reference-система для development tooling, но не runtime character controller. Новый MCP должен иметь другой scope.

### Control panel bridge

`fpsacha/zomboid-control-panel` демонстрирует, что внешний процесс и Lua bridge могут дополнять стандартные серверные инструменты для действий, которые RCON не покрывает. Runtime agent должен использовать отдельный минимальный протокол и не зависеть от server admin stack.

## 12.3. Источники

1. The Indie Stone, Build 42 Stable Plans:
   `https://projectzomboid.com/blog/news/2026/07/build-42-stable-plans/`
2. The Indie Stone Forums, Build 42.20 Released:
   `https://theindiestone.com/forums/topic/96977-build-4220-released/`
3. PZ-Umbrella API stubs:
   `https://github.com/PZ-Umbrella/Umbrella`
4. AutoPilot research reference:
   `https://github.com/rodmen07/auto-pilot-pz`
5. PZ mod-development MCP:
   `https://github.com/wink-/pz-mcp-server`
6. Zomboid Control Panel / PanelBridge reference:
   `https://github.com/fpsacha/zomboid-control-panel`
7. DXcam optional visual capture:
   `https://github.com/ra1nty/DXcam`
8. vgamepad optional legacy virtual input reference:
   `https://github.com/yannbouteiller/vgamepad`
9. HIDMaestro optional newer virtual HID research:
   `https://github.com/hifihedgehog/HIDMaestro`

## 12.4. Нельзя считать подтверждённым

До live tests нельзя утверждать:

- полный autonomous combat;
- управление автомобилем;
- pathfinding через все типы дверей/окон;
- world water source action;
- сон в любом объекте;
- multiplayer-safe ownership;
- работа со всеми модами;
- стабильность API между hotfixes.

## 12.5. Compatibility ledger

Создать в репозитории:

```text
docs/compatibility/42.20.md
compat/api-symbols.json
compat/runtime-probes.json
compat/known-failures.json
```

Для каждого symbol:

```json
{
  "symbol": "ISEatFoodAction.new",
  "source": "local_game",
  "build": "42.20",
  "signature": ["character", "item", "percentage"],
  "probe": "passed",
  "last_verified": "ISO timestamp"
}
```

## 12.6. API drift

При неизвестном build:

- OBSERVE разрешён;
- mutating actions blocked;
- doctor предлагает probe;
- пользователь может включить experimental override;
- override явно виден в HUD/logs.

## 12.7. Лицензирование

PZ-Umbrella на момент исследования может не иметь явной лицензии. Не vendor-ить его содержимое. Использовать как справочный источник, а production scanner строить по локальной установке пользователя.

Нельзя распространять vanilla Lua или игровые ассеты.
