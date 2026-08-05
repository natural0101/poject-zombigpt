# 4. Action Engine

## 4.1. Главная модель

Action Engine — единственный компонент, который имеет право преобразовывать валидированную команду в игровые timed actions.

Planner никогда не вызывает игровые классы напрямую.

```text
Intent → Policy → Typed Action → Preconditions → Game Adapter
       → Timed Action Queue → Progress Observer → Postcondition
```

## 4.2. Action specification

Каждое действие описывается декларативно:

```yaml
name: consume.eat
mutating: true
required_capabilities:
  - eat_percentage
preconditions:
  - player_alive
  - session_armed
  - no_manual_action
  - item_ref_resolves
  - item_is_safe_food
interruptions:
  - panic_stop
  - user_input
  - immediate_threat
timeout_policy:
  base_seconds: 30
postconditions:
  any:
    - hunger_decreased
    - item_fraction_decreased
    - item_consumed
```

## 4.3. Ownership очереди

Автоматизация маркирует каждое поставленное действие:

- Lua weak map action object → command id;
- дополнительный metadata/tag, если API допускает;
- sidecar trace.

Нельзя очищать всю очередь, если в ней есть ручное действие. Для urgent stop:

1. отменить текущую mod-owned action;
2. удалить последующие mod-owned actions;
3. не трогать foreign actions;
4. если API не позволяет точечно удалить, зафиксировать ограничение и использовать clear только при panic/immediate threat.

## 4.4. Busy semantics

Состояния:

- idle;
- mod_action;
- manual_action;
- ambiguous_action.

При `manual_action` mutating commands отклоняются либо ждут, согласно policy. При `ambiguous_action` по умолчанию не вмешиваться.

## 4.5. `movement.move_to`

Аргументы:

```json
{
  "target": {"x": 100, "y": 100, "z": 0},
  "radius": 0.75,
  "max_distance": 30,
  "allow_doors": true,
  "allow_windows": false,
  "allow_stairs": true
}
```

Алгоритм:

1. разрешить square;
2. проверить loaded cell;
3. проверить расстояние;
4. проверить floor;
5. получить безопасную конечную клетку;
6. создать `ISWalkToTimedAction`;
7. поставить callback;
8. monitor position/path result;
9. stuck, если нет прогресса N секунд;
10. при stuck отменить и один раз перестроить;
11. success только в radius.

Не использовать большие прямые команды для дальних путешествий. Planner разбивает путь на семантические waypoint.

## 4.6. `inventory.transfer`

Аргументы:

```json
{
  "item_ref": "item:...",
  "source_container_ref": "container:...",
  "destination_container_ref": "container:...",
  "quantity": 1
}
```

Проверки:

- item находится в source;
- source и destination существуют;
- destination принимает item;
- capacity;
- item не заблокирован действием;
- player может получить доступ к world container;
- если world container далеко — сначала отдельный move_near;
- перенос через `ISInventoryTransferAction`;
- verify ownership.

Никаких прямых `Remove/AddItem` как fallback в production.

## 4.7. `inventory.ensure_main`

Composite action:

1. item уже в main → success/no-op;
2. item во вложенном carried container → transfer;
3. item в world container → требует move_near;
4. item equipped/hand → policy;
5. verify.

Это обязательная подготовка для потребления и чтения, если vanilla action не умеет безопасно работать из сумки.

## 4.8. `consume.eat`

Preconditions:

- item ref валиден;
- тип Food;
- policy разрешает;
- персонаж способен есть;
- fraction в диапазоне;
- нет immediate threat.

Preparation:

- ensure main;
- при необходимости unequip conflict;
- определить final fraction.

Execution:

- `ISEatFoodAction:new(player, item, percentage)`;
- queue;
- monitor.

Verification:

- hunger delta;
- item uses/fraction delta;
- disappearance item;
- action completion.

Не считать отсутствие item после action ошибкой: он мог быть полностью съеден.

## 4.9. `consume.drink`

Для carried drink:

- проверить безопасную жидкость;
- определить uses;
- ensure main;
- `ISDrinkFromBottle`;
- verify thirst/volume.

Для world source:

- отдельный adapter;
- capability experimental до runtime probe;
- проверка tainted;
- подход к объекту;
- vanilla take water action;
- verify.

## 4.10. `literature.read`

- item Literature;
- trait check;
- skill level check;
- прочитанные страницы;
- цель чтения;
- ensure main;
- `ISReadABook`;
- progress: pages/time/action;
- разрешён pause/resume;
- threat interrupt;
- success может означать `started` только для отдельного tool `read_start`; стандартный `read` должен ждать до заданного условия:
  - complete;
  - pages target;
  - duration;
  - boredom target.

## 4.11. `world.inspect`

Read-only. Возвращает нормализованное описание объектов в радиусе:

- container;
- door;
- window;
- water source;
- bed;
- chair;
- radio/TV;
- corpse;
- vehicle;
- fire;
- stairs;
- exits.

Не передавать огромный список sprites. Отображать semantics + refs + confidence.

## 4.12. Composite actions

Composite action — sidecar-level orchestration, а не одна гигантская Lua command.

Пример `consume_best_food`:

1. observe inventory;
2. score candidates;
3. choose;
4. ensure_main;
5. eat;
6. verify;
7. summarize.

Каждый шаг имеет собственный command id.

## 4.13. Retry

Retry разрешён только для transient errors:

- stale observation;
- path temporarily blocked;
- container not yet accessible;
- action queue busy.

Не retry:

- unsafe food;
- invalid ref after refresh;
- unsupported capability;
- manual takeover;
- panic stop;
- trait prevents reading;
- repeated postcondition failure.

## 4.14. Timeouts

Timeout не фиксированный для всех действий. Он вычисляется:

- vanilla expected duration, если доступно;
- distance;
- game speed;
- paused state;
- item transfer time;
- multiplier.

Игровая пауза приостанавливает timeout action, но не heartbeat timeout.

## 4.15. Error evidence

При failed result включать:

- observation seq before/after;
- resolved refs;
- current action type;
- path result;
- exception string без secret;
- candidate postcondition values;
- retry count;
- relevant capability;
- последние 10 trace events.
