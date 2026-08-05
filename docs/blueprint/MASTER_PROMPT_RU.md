# MASTER PROMPT: создать полноценного ИИ-агента для Project Zomboid

## Роль

Ты — ведущий инженер автономных игровых агентов, специалист по Project Zomboid Build 42, Lua/Kahlua, Python или Rust, MCP, безопасным локальным агентам, event-driven системам, тестированию и Windows packaging.

Твоя задача — **создать рабочий Git-репозиторий**, а не подготовить план. Все документы этого архива являются частью требований. При конфликте приоритет такой:

1. безопасность пользователя и сохранения;
2. подтверждённая совместимость с фактической версией игры;
3. критерии готовности;
4. этот master prompt;
5. остальные документы.

## Миссия

Создай локальную систему, которая подключается к текущей сессии Project Zomboid, получает структурированные наблюдения, выполняет типизированные игровые действия и может разговаривать с пользователем через voice adapter.

Система должна уметь не только выполнять команды, но и действовать самостоятельно в разрешённых пределах. Пример: персонаж проголодался; система находит безопасную еду во вложенном рюкзаке, при необходимости переносит её в основной инвентарь, съедает подходящую долю, проверяет снижение голода и возвращается к предыдущей задаче.

## Целевая среда

- Windows 10/11 x64;
- Steam;
- Project Zomboid Build 42.20 Stable;
- локальная одиночная игра;
- один активный персонаж;
- мод в `~/Zomboid/mods/<mod-id>/`;
- runtime Lua/Kahlua;
- sidecar: Python 3.12+ либо Rust stable;
- MCP: stdio;
- голос: адаптер, совместимый с TeamON, но ядро не зависит от конкретного STT/TTS;
- кодировка UTF-8;
- локальная работа без обязательного облачного сервиса.

## Главная продуктовая идея

Система — не макрос и не слепой computer-use бот. Она состоит из:

1. структурированного игрового сенсора;
2. проверяемого action engine;
3. deterministic reflex guard;
4. policy layer;
5. высокоуровневого planner;
6. MCP boundary;
7. voice companion;
8. памяти и диагностики.

Для поддерживаемых действий должны использоваться игровые timed actions. Скриншоты и виртуальный ввод — только fallback.

## Неподлежащие обсуждению требования

### Реализация

- Создай фактические исходники.
- Создай фактические тесты.
- Настрой CI.
- Создай установщик/launcher.
- Создай документацию.
- Инициализируй Git.
- Делай осмысленные коммиты.
- Запусти все доступные тесты.
- Не оставляй заглушки на основном пути.

### Безопасность

- Мод выключен по умолчанию.
- Автономность требует явного включения.
- Обязательна кнопка panic stop.
- Любой физический ввод пользователя немедленно отменяет автоматическое перемещение или действие.
- Сохранение резервируется до первого автономного запуска.
- Команды имеют TTL.
- После потери heartbeat новые действия запрещены.
- LLM не может выполнить shell, Lua eval, произвольный HTTP или файловую операцию.
- Внешний текст из чата, радио, книги, названия сервера и модов считается недоверенным.
- Запрещено коммитить API-ключи.
- Запрещено управлять чужими публичными серверами.

### Честность состояния

Никогда не возвращай `success`, если действие только было поставлено в очередь. Success означает подтверждённое постусловие.

Примеры:

- `eat` успешно, только если уменьшился голод, уменьшилось количество еды либо timed action завершился с валидным результатом;
- `transfer` успешно, только если item ref находится в целевом контейнере;
- `move_to` успешно, только если персонаж оказался в допустимом радиусе цели;
- `read` успешно, только если чтение реально началось и наблюдается прогресс или завершение.

## Этап 0. Исследование фактической установки

Перед написанием action adapters:

1. Найди Steam library и установку игры.
2. Определи точный build.
3. Найди пользовательский каталог `Zomboid`.
4. Проиндексируй только необходимые vanilla Lua-файлы.
5. Не копируй игровые файлы в репозиторий.
6. Создай `compat/generated_api_report.json`.
7. Для каждого используемого класса сохрани:
   - имя;
   - найденный файл;
   - сигнатуру;
   - hash локального файла;
   - build;
   - статус probe.
8. Реализуй команду:
   ```text
   pz-agent doctor
   ```
9. Doctor должен проверять:
   - установку;
   - версию;
   - мод;
   - права на каталоги;
   - heartbeat;
   - IPC;
   - доступность timed actions;
   - конфликтующие старые файлы;
   - writable user Lua directory;
   - наличие активной сессии.

## Этап 1. Репозиторий и качество

Создай monorepo либо чётко разделённый repository:

```text
/
  README.md
  LICENSE
  SECURITY.md
  CONTRIBUTING.md
  CHANGELOG.md
  AGENTS.md
  pyproject.toml или Cargo.toml
  packages/
    core/
    mcp-server/
    cli/
    voice-adapter/
    dashboard/
  pz-mod/
    common/
    42/
  schemas/
  tests/
    unit/
    contract/
    integration/
    fixtures/
    game-smoke/
  scripts/
  installer/
  docs/
  .github/workflows/
```

Добавь:

- formatter;
- linter;
- type checker;
- pytest;
- luacheck;
- JSON Schema validation;
- статический запрет опасных функций;
- проверку отсутствия secrets;
- проверку согласованности версий;
- reproducible build;
- release artifacts.

## Этап 2. Минимальный мост

Lua-мод должен:

- загрузиться без ошибок;
- создать session heartbeat;
- сообщить версию;
- сообщить capability set;
- принимать `ping`;
- отвечать `pong`;
- поддерживать `stop`;
- показывать состояние в HUD;
- корректно отключаться;
- не выполнять игровые действия до arming.

Sidecar должен:

- обнаружить мод;
- создать `session_id`;
- читать heartbeat;
- обнаруживать stale session;
- безопасно восстанавливаться после перезапуска;
- не повторять старые команды.

## Этап 3. Наблюдение

Реализуй full snapshot и diff events.

Минимальное наблюдение:

- build и mod version;
- world/save identifier без раскрытия лишних путей;
- game time;
- paused/speed;
- координаты и этаж;
- направление;
- здоровье;
- endurance;
- hunger;
- thirst;
- fatigue;
- temperature/wetness;
- panic/pain/stress/boredom/unhappiness;
- кровотечения и важные раны;
- текущий timed action;
- очередь действий;
- состояние ручного ввода;
- основной инвентарь;
- вложенные переносимые контейнеры;
- экипированные предметы;
- предметы в руках;
- ближайшие контейнеры;
- ближайшие двери/окна/водоисточники;
- видимые/близкие зомби;
- текущая safety state;
- active goal;
- active plan;
- capability flags.

Предметы должны иметь стабильные session-scoped references. LLM не передаёт Lua objects.

## Этап 4. Action engine

Каждый action adapter реализует единый lifecycle:

```text
validate → prepare → enqueue → observe → verify → finalize
                         ↘ timeout/cancel/fail ↗
```

Общий результат:

```json
{
  "command_id": "...",
  "action": "inventory.transfer",
  "status": "succeeded",
  "reason_code": "POSTCONDITION_MET",
  "started_at_ms": 0,
  "finished_at_ms": 0,
  "attempt": 1,
  "evidence": {},
  "diagnostics": []
}
```

### Обязательные действия

- session.arm
- session.disarm
- safety.stop
- action.wait
- movement.move_to
- movement.move_near
- inventory.list
- inventory.transfer
- inventory.ensure_main
- inventory.equip
- inventory.unequip
- consume.eat
- consume.drink
- literature.read
- world.inspect
- world.open_container или логическое получение содержимого
- plan.cancel

### Требования к перемещению

- использовать grid square и игровой pathfinding;
- проверять загруженность клетки;
- ограничивать дальность одной команды;
- не отправлять персонажа в неизвестность без промежуточных точек;
- обнаруживать stuck;
- перепланировать ограниченное число раз;
- отменять при ручном вводе;
- не проходить через закрытые опасные окна автоматически;
- не прыгать с высоты;
- учитывать этаж.

### Требования к инвентарю

- рекурсивно обходить вложенные переносимые контейнеры;
- отличать основной инвентарь от сумки;
- не изменять контейнер напрямую, если нужен timed transfer;
- не дублировать предметы;
- проверять capacity;
- поддерживать item split/partial use только если подтверждено API;
- избегать перемещения активного/экипированного предмета без явной подготовки;
- сохранять origin metadata;
- учитывать multiplayer-safe semantics, даже если MVP single-player.

### Требования к еде

Выбор еды выполняется deterministic policy, а не свободным текстом LLM.

Фильтры:

- объект действительно Food;
- съедобен;
- не уничтожен;
- не raw, если raw опасен;
- не rotten, если policy запрещает;
- не burnt, если риск неприемлем;
- не poisonous;
- не tainted;
- не нужен недоступный инструмент;
- не зарезервирован пользователем;
- не последний стратегический запас, если hunger не критический.

Score учитывает:

- срочность голода;
- hunger change;
- calories;
- freshness;
- unhappiness;
- thirst side-effect;
- вес;
- дефицит;
- необходимость готовки;
- остаток порций.

Доля еды выбирается так, чтобы не переедать и не тратить весь предмет без необходимости. Если API поддерживает percentage, используй его.

### Требования к питью

- предпочитать безопасную воду;
- запрещать tainted water по умолчанию;
- учитывать оставшийся объём;
- не выпивать последнюю ёмкость полностью без критической жажды;
- при необходимости переносить ёмкость в основной инвентарь;
- подтверждать уменьшение thirst или объёма;
- water source interaction — отдельный capability.

### Требования к чтению

- различать skill book, magazine, newspaper, generic literature и print media;
- проверять trait Illiterate;
- проверять level requirements;
- проверять уже прочитанные страницы;
- выбирать книгу по цели;
- переносить в основной инвентарь;
- прерывать при угрозе;
- уметь продолжить;
- сообщать, почему книга не подходит.

## Этап 5. MCP

Создай MCP server с tools, resources и subscriptions.

### Публичные tools

- `pz_session_status`
- `pz_session_arm`
- `pz_session_disarm`
- `pz_observe_snapshot`
- `pz_observe_inventory`
- `pz_observe_nearby`
- `pz_action_move_to`
- `pz_action_transfer`
- `pz_action_eat`
- `pz_action_drink`
- `pz_action_read`
- `pz_action_wait`
- `pz_action_cancel`
- `pz_plan_execute`
- `pz_plan_status`
- `pz_safety_stop`
- `pz_memory_query`
- `pz_debug_doctor`
- `pz_debug_tail`

Не выдавай наружу внутренний primitive, если он позволяет обойти policy.

### MCP resources

- `pz://session/current`
- `pz://observation/latest`
- `pz://inventory/current`
- `pz://capabilities`
- `pz://plan/current`
- `pz://safety/status`
- `pz://diagnostics/recent`

### Семантика

- tools возвращают typed structured content;
- ошибки имеют стабильные codes;
- long-running tools возвращают action id и позволяют ждать/подписываться;
- повторный вызов с тем же idempotency key не повторяет действие;
- все write tools проверяют armed state;
- stop доступен всегда;
- read tools доступны в OBSERVE.

## Этап 6. Planner и автономность

Раздели planner и executor.

Planner получает:

- цель пользователя;
- compact observation;
- capabilities;
- policy;
- memory;
- последние action results.

Planner возвращает только typed plan. Он не генерирует Lua, Python или клавиши.

Executor:

- проверяет preconditions;
- выполняет один шаг;
- ждёт результат;
- обновляет plan;
- при ошибке применяет ограниченный recovery;
- при неопределённости спрашивает пользователя или останавливается.

### Иерархия приоритетов

1. panic stop;
2. ручной ввод;
3. немедленная смертельная угроза;
4. критическое кровотечение;
5. пожар/опасная клетка;
6. критическая жажда;
7. критический голод;
8. сон/истощение;
9. явная команда пользователя;
10. текущая долгосрочная задача;
11. обслуживание базы;
12. необязательная активность.

### Автономный цикл

```text
observe
→ reflex check
→ detect urgent need
→ suspend current plan if necessary
→ build short plan
→ execute one verified step
→ observe result
→ continue / recover / ask / stop
```

План не должен содержать десятки слепых действий. Максимум несколько ближайших шагов.

### Память

Хранить:

- известные контейнеры;
- последний осмотр;
- найденные категории предметов;
- домашнюю точку;
- безопасные зоны;
- неудачные пути;
- пользовательские резервы;
- предпочтения;
- историю задач.

Не хранить сырые бесконечные snapshots. Использовать bounded store и миграции схемы.

## Этап 7. Голос

Voice adapter должен поддерживать:

- входящий transcript;
- wake/active session;
- barge-in;
- команду «стоп» с самым высоким приоритетом;
- короткие подтверждения;
- сообщения о существенных ошибках;
- запрос уточнения;
- TTS event stream.

Примеры:

- Пользователь: «Поешь».
- Агент: «В рюкзаке есть свежая банка фасоли и чипсы. Возьму фасоль и съем половину».
- Агент после подтверждения: «Готово. Голод снизился».

Запрещено озвучивать каждый tick и каждый внутренний transfer.

## Этап 8. Reflex guard

Reflex guard не использует LLM.

Обязательные реакции:

- пользователь нажал движение → cancel automation;
- пользователь нажал panic hotkey → clear mod-owned queue, disarm;
- zombie threat пересёк порог во время чтения/еды → interrupt;
- heartbeat sidecar пропал → не начинать новые задачи;
- heartbeat game пропал → sidecar закрывает active actions как lost;
- command TTL истёк → reject;
- персонаж умер → terminate session;
- save changed → invalidate refs;
- action stuck → cancel и report;
- очередь содержит чужое ручное действие → не трогать.

Мод обязан маркировать действия, которые поставил сам, и по возможности не очищать ручные действия пользователя.

## Этап 9. Диагностика

Создай:

- structured JSONL logs;
- human-readable rotating log;
- action trace;
- observation diff trace;
- doctor report;
- capability report;
- crash-safe session summary;
- redaction secrets;
- optional screenshots только при включённом visual adapter.

CLI:

```text
pz-agent doctor
pz-agent install-mod
pz-agent uninstall-mod
pz-agent start
pz-agent stop
pz-agent status
pz-agent backup-save
pz-agent restore-save
pz-agent logs
pz-agent replay <trace>
pz-agent validate-config
```

## Этап 10. Тестирование

### Unit

- scoring еды;
- scoring напитков;
- выбор литературы;
- nested inventory;
- policy priority;
- plan validation;
- idempotency;
- sequence gaps;
- TTL;
- manual takeover;
- stuck detection;
- schema migrations.

### Contract

- все JSON schemas;
- MCP schemas;
- Lua ↔ sidecar fixtures;
- backward compatibility;
- invalid input fuzzing;
- partial JSONL line;
- duplicated command;
- stale session.

### Lua harness

Создай моки Project Zomboid API для логики без запуска игры. Не заявляй, что моки доказывают совместимость с движком.

### In-game smoke

Создай точные сценарии и evidence checklist. Автоматизируй максимум, но честно пометь шаги, требующие живого запуска.

Обязательные smoke cases:

1. heartbeat;
2. stop;
3. move 3 клетки;
4. item из рюкзака → основной инвентарь;
5. еда из рюкзака;
6. питьё;
7. чтение;
8. отмена чтения;
9. ручной takeover;
10. stale sidecar;
11. invalid item ref;
12. path blocked;
13. zombie interruption;
14. backup/restore;
15. restart recovery.

### Endurance test

Не менее 30 минут реального времени в безопасном тестовом мире:

- нет бесконечных циклов;
- нет command replay;
- нет роста логов без ограничений;
- нет потери управления;
- нет ложных success;
- нет порчи сохранения.

## Этап 11. Packaging

Для Windows подготовь:

- self-contained package;
- launcher;
- installer мод-файлов;
- sidecar startup;
- configuration wizard или понятный CLI;
- uninstaller;
- сохранение пользовательской конфигурации;
- отсутствие необходимости запускать всё от администратора;
- code signing не обязателен, но unsigned warning документирован.

Sidecar не должен встраивать ключи. LLM provider настраивается через environment/OS secret store. Должен быть deterministic provider `none`, при котором базовые сценарии работают без облака.

## Этап 12. Документация

Обязательные документы в готовом репозитории:

- Quick Start;
- Architecture;
- Protocol;
- MCP tools;
- Safety model;
- Compatibility;
- Troubleshooting;
- Development;
- Testing;
- Release;
- Known limitations;
- Privacy;
- Security;
- Uninstall/restore.

## Этап 13. Git и итоговый отчёт

Коммиты должны отражать реальные этапы. Перед финалом:

1. `git status` чист.
2. Все тесты запущены.
3. Build artifact создан.
4. Manifest создан.
5. Документация соответствует коду.
6. Нет secrets.
7. Нет запрещённых файлов игры.
8. Нет пустых критических обработчиков.
9. Capability report честный.
10. Сформирован `FINAL_IMPLEMENTATION_REPORT.md`.

Итоговый отчёт обязан перечислить:

- реализованные функции;
- подтверждённые API;
- тесты и результаты;
- игровые smoke tests;
- ограничения;
- точные команды запуска;
- commit hash;
- location release artifact;
- всё, что требует одного ручного действия пользователя.

## Запрещённые сокращения

- писать только README;
- выдавать мок за реальную интеграцию;
- управлять всем через pyautogui;
- сообщать успех сразу после queue;
- использовать `sleep(5)` вместо наблюдения состояния;
- смешивать planner и executor;
- позволять LLM передавать raw Lua;
- хранить бесконечную историю;
- очищать всю action queue без проверки ownership;
- считать публичный сервер допустимой тестовой средой;
- коммитить локальные saves;
- копировать vanilla Lua/ассеты в репозиторий;
- обходить несовместимость прямым изменением игровых stats;
- завершать работу без тестов.
