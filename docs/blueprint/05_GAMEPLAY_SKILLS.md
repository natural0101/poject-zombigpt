# 5. Игровые навыки и сценарии

## 5.1. Общий принцип skill

Skill — детерминированная или частично планируемая процедура с:

- целью;
- trigger;
- preconditions;
- candidate generation;
- scoring;
- typed steps;
- success criteria;
- interruption policy;
- recovery;
- user-facing summary.

## 5.2. Skill: достать еду из сумки и поесть

### Trigger

- явная команда;
- hunger выше policy threshold.

### Candidate generation

Рекурсивно обходятся:

- main inventory;
- worn containers;
- carried containers;
- optionally nearby accessible containers.

### Hard reject

- предмет не Food;
- raw и опасен;
- rotten;
- poison power;
- tainted;
- заморожен и нельзя есть;
- требует открытия/приготовления, которого skill не умеет;
- пользователь пометил reserve;
- является ингредиентом protected recipe;
- отрицательный эффект выше policy.

### Score

```text
score =
  urgency_fit
+ hunger_reduction_fit
+ freshness
+ happiness_value
+ calories_need_fit
+ portability
- scarcity_penalty
- thirst_penalty
- weight_penalty
- waste_penalty
- preparation_cost
```

### Plan

```text
select item
→ ensure item in main inventory
→ choose fraction
→ enqueue eat action
→ verify
→ resume suspended task
```

### Success

- hunger уменьшился либо item amount уменьшился;
- нет нового опасного состояния;
- action terminal succeeded.

### Voice

До действия: короткое объяснение выбора, только если выбор неоднозначен.
После: результат и остаток.

## 5.3. Skill: попить

Приоритет:

1. безопасная вода в carried container;
2. безопасный другой напиток;
3. доступный world water source;
4. поиск воды;
5. запрос пользователю.

Система не должна автоматически пить алкоголь ради жажды, если policy не разрешает. Tainted water запрещена.

## 5.4. Skill: читать

Типы целей:

- уменьшить boredom;
- получить рецепт;
- читать skill book;
- читать конкретный предмет;
- читать заданное число страниц;
- читать до угрозы/времени.

Выбор:

- magazine/newspaper для mood;
- unread recipe magazine;
- skill book соответствующего диапазона;
- print media по запросу.

Проверки:

- Illiterate;
- required skill;
- уже прочитано;
- освещение/безопасность;
- fatigue;
- pain;
- окружение.

Чтение немедленно прерывается при threat policy.

## 5.5. Skill: разгрузить рюкзак

Цель: перенести категории в подходящие контейнеры базы.

Пользовательские правила:

```yaml
storage_rules:
  food:
    preferred_tags: [kitchen, pantry, fridge]
  medical:
    preferred_tags: [medical]
  weapons:
    preferred_tags: [armory]
  books:
    preferred_tags: [bookshelf]
```

Система:

1. определяет home;
2. знает semantic tags контейнеров;
3. составляет batch;
4. выполняет transfer по одному/маленькими группами;
5. останавливается при capacity;
6. не переносит equipped, favorite, reserve;
7. выдаёт отчёт.

## 5.6. Skill: осмотреть дом

- построить список комнат/контейнеров в загруженном радиусе;
- не блуждать бесконечно;
- помечать осмотренные контейнеры;
- классифицировать припасы;
- сообщать дефициты;
- не брать предметы без отдельной policy.

## 5.7. Skill: поддерживать потребности

State machine:

```text
CRITICAL_BLEEDING
CRITICAL_THREAT
CRITICAL_THIRST
CRITICAL_HUNGER
FATIGUE
PAIN
TEMPERATURE
MOOD
USER_GOAL
IDLE
```

Hysteresis обязателен, чтобы избежать дрожания:

- начать пить при одном пороге;
- считать потребность удовлетворённой при более низком;
- cooldown после успешного действия.

## 5.8. Skill: ожидать безопасно

`wait` — активное наблюдение, а не sleep процесса.

- heartbeat;
- threat;
- needs;
- user voice;
- action queue;
- game pause.

## 5.9. Skill: безопасно отступить

Первая стабильная версия не должна пытаться автономно бить зомби через непроверенный API.

Алгоритм:

1. определить threat centroid;
2. оценить 8 направлений;
3. исключить blocked/fire/window/fall;
4. выбрать клетку с увеличением дистанции;
5. короткий move;
6. повторная оценка;
7. если выхода нет — stop, голосовое предупреждение.

## 5.10. Skill: открыть сумку

На уровне игрового API доступ к содержимому переносимого контейнера не требует обязательно показывать UI. Поэтому определить две разные операции:

- `inventory.inspect_container` — логически прочитать содержимое;
- `ui.show_container` — визуально открыть панель, optional visual/input adapter.

Базовые пищевые сценарии используют первую операцию. Не эмулировать клики только ради внешней видимости интерфейса.

## 5.11. Skill: взять предмет из мирового контейнера

```text
inspect nearby
→ select world container
→ move_near
→ re-resolve container ref
→ inspect contents
→ transfer
→ verify
```

После движения refs обновляются. Нельзя использовать старый object index без re-resolution.

## 5.12. Skill: экипировать предмет

- item accessible;
- slots;
- two-hand semantics;
- current hands;
- protected item;
- transfer to main;
- vanilla equip action;
- verify hand/worn state.

## 5.13. Skill: сон

Необязательный до подтверждения API, но архитектура:

- fatigue threshold;
- find bed;
- path;
- pain check;
- danger check;
- sleep;
- wake reason;
- verify fatigue change.

## 5.14. Skill: медицина

Разделить:

- observe wounds;
- select treatment;
- ensure supplies;
- perform treatment;
- verify body part.

Никакой диагностики реального здоровья человека — только игровая механика.

## 5.15. Skill: долгосрочная задача

Пример «собери запас еды на три дня»:

- вычислить policy target;
- инвентаризация;
- определить дефицит;
- выбрать безопасные nearby search zones;
- короткие supply trips;
- возвращаться домой;
- сортировать;
- остановиться при угрозе/усталости;
- не обещать глобальное знание карты.

## 5.16. Сценарий ручного перехвата

1. ИИ идёт к шкафу.
2. Пользователь нажимает `W`.
3. Lua фиксирует manual input.
4. Mod-owned movement отменяется.
5. Sidecar получает `USER_TAKEOVER`.
6. Plan переходит `suspended`.
7. Агент не пытается продолжить, пока пользователь не скажет «продолжай».

## 5.17. Сценарий voice barge-in

1. Агент говорит.
2. Пользователь произносит «стоп».
3. STT stop intent идёт по bypass path.
4. Создаётся `panic.stop` и MCP stop.
5. TTS немедленно прерывается.
6. Агент подтверждает остановку только после ack.

## 5.18. Сценарий ошибки

Если еда исчезла до исполнения:

- ref invalid;
- refresh inventory;
- не выбирать случайный предмет;
- один replan;
- при отсутствии кандидата сообщить пользователю.

## 5.19. Поведенческие ограничения

Автономно запрещено без отдельного permission:

- есть неизвестные грибы/ягоды;
- пить tainted water;
- прыгать из окна;
- разбивать окно;
- включать сирену;
- стрелять;
- разводить огонь;
- сжигать предметы;
- принимать необратимые traits/debug changes;
- удалять сохранение;
- бросать favorite/reserve items;
- уезжать далеко от home.
