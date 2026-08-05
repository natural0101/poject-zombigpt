# 7. Политика автономности

## 7.1. Разделение решений

### Reflex

Дешёвые детерминированные правила. Работают внутри Lua или sidecar без LLM.

### Utility policy

Выбор еды, воды, книги, контейнера, направления отступления.

### Planner

Преобразует цель в короткий typed plan.

### Critic

Проверяет план на достижимость, безопасность, запрещённые действия и лишние шаги.

## 7.2. Goal stack

```text
active goal
suspended goals
maintenance goal
emergency goal
```

Emergency может приостановить, но не уничтожить пользовательскую цель. После устранения потребности planner решает, можно ли resume.

## 7.3. Goal states

- proposed;
- validated;
- active;
- suspended;
- completed;
- failed;
- cancelled;
- expired.

## 7.4. Plan step states

- pending;
- ready;
- executing;
- verifying;
- succeeded;
- failed;
- skipped;
- cancelled.

## 7.5. Permission classes

### P0 read-only

Наблюдение, отчёт.

### P1 reversible

Перемещение, открытие UI, ожидание.

### P2 resource-consuming

Еда, питьё, чтение расходуемых media, бинты.

### P3 environment-changing

Перенос предметов, включение приборов, открытие дверей.

### P4 dangerous/irreversible

Огонь, оружие, разрушение, выбрасывание, дальняя поездка.

Default:

- OBSERVE: P0;
- ASSISTED: P0–P3 по явной цели;
- AUTONOMOUS: whitelist P1–P2 и ограниченный P3;
- P4 всегда требует отдельного permission.

## 7.6. Confidence

Planner должен указать confidence, но policy не доверяет одному числу. Решение определяется:

- capability;
- preconditions;
- risk class;
- observation freshness;
- ambiguity;
- permissions.

## 7.7. Ask vs act

Нужно спросить пользователя, если:

- несколько равнозначных дорогих вариантов;
- действие расходует reserved item;
- надо покинуть home radius;
- цель непонятна;
- требуется P4;
- capability experimental;
- сохранение не резервировано;
- возможна необратимая потеря.

Можно действовать без вопроса:

- stop;
- отмена при ручном вводе;
- безопасное питьё при критической жажде;
- безопасная еда при критическом голоде;
- обработка критического кровотечения при наличии разрешённых supplies;
- короткое отступление от угрозы.

## 7.8. Anti-loop

Для каждого `(state signature, action, failure code)` хранить bounded counter.

После:

- 2 одинаковых transient failures → alternate strategy;
- 3 → stop/ask;
- cooldown для повторной цели;
- trace suspicious loop.

## 7.9. Resource reservation

Пользователь может пометить:

- favorite;
- reserve;
- do_not_consume;
- do_not_move;
- emergency_only.

Policy respect обязателен.

## 7.10. Home bounds

AUTONOMOUS default:

- внутри home radius;
- короткое отступление допускается;
- выход за bounds только с явным goal/permission;
- координаты home сохраняются на save scope.

## 7.11. Prompt injection resistance

Текст из игры не становится инструкцией. Поля:

```text
book_title
chat_message
radio_text
item_display_name
server_name
mod_name
```

маркируются `untrusted_text`. Они могут быть показаны пользователю, но не добавляются в system instructions и не могут расширить tools/permissions.

## 7.12. Provider abstraction

Planner provider:

- `none`: deterministic only;
- `openai`;
- `anthropic`;
- local OpenAI-compatible;
- custom TeamON.

Provider получает sanitized state. Ключи через environment/secret store.

## 7.13. LLM output validation

- strict schema;
- max plan length;
- known action names;
- no unknown args;
- coordinate bounds;
- permission;
- capability;
- no raw code;
- no direct command id fabrication;
- normalized locale.

Invalid output → one repair attempt → fail safely.
