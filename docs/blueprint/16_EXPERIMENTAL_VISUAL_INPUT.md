# 16. Экспериментальный visual/input adapter

## 16.1. Роль

Используется только если действие невозможно или ненадёжно через Lua API.

## 16.2. Изоляция

Отдельный package и feature flag:

```toml
[experimental_input]
enabled = false
```

Core не импортирует visual package.

## 16.3. Screen capture

Подходящий Windows backend может использовать Desktop Duplication / Windows Graphics Capture. Требования:

- capture только окна игры;
- пользователь явно включает;
- frame rate ограничен;
- screenshots не сохраняются по умолчанию;
- privacy indicator;
- no upload.

## 16.4. Virtual input

Предпочтение виртуальному геймпаду, если игра его поддерживает. Перед выбором библиотеки проверить:

- maintenance;
- driver signing;
- Windows versions;
- uninstall;
- Steam Input interaction;
- security implications.

Legacy ViGEm-based решения не принимать автоматически: upstream может быть archived. Рассмотреть современные alternatives и документировать выбор.

## 16.5. Visual action transaction

Даже UI action:

```text
capture before
→ locate element with confidence
→ focus verified game window
→ send bounded input
→ capture after
→ verify structured game state
```

Нельзя считать pixel click success без structured postcondition.

## 16.6. Combat

Autonomous combat — отдельная research track:

- aim semantics;
- attack timing;
- target selection;
- endurance;
- weapon reach;
- friendly safety;
- animation state;
- recovery;
- high-frequency control.

До прохождения dedicated tests system default — flee only.

## 16.7. Vehicles

Отдельная research track. Не смешивать с walking action.

## 16.8. Second character

Split-screen companion возможен теоретически через второй controller, но:

- требует отдельного player state;
- separate refs;
- viewport;
- controller ownership;
- performance;
- load order.

Не входит в 1.0.
