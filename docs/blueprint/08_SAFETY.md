# 8. Безопасность и контроль

## 8.1. Panic stop

Минимум три пути:

1. горячая клавиша внутри игры;
2. голосовое «стоп»;
3. MCP `pz_safety_stop`.

Hotkey обрабатывается внутри Lua и не зависит от sidecar.

Результат:

- disarm;
- cancel mod-owned actions;
- clear pending commands;
- write safety event;
- TTS stop;
- HUD confirmation.

## 8.2. Manual takeover

События keyboard/mouse/controller считаются takeover, если они соответствуют управлению персонажем.

Debounce, чтобы случайный UI click не отменял всё. Movement/attack input отменяет немедленно.

## 8.3. Save backup

До первого arm на save:

- определить save directory;
- попросить игру сохранить либо предупредить;
- sidecar копирует save в versioned backup;
- manifest + hash;
- verify readable;
- retention policy;
- restore только когда игра закрыта.

Нельзя копировать save в Git.

## 8.4. Watchdog

Sidecar watchdog:

- game heartbeat stale;
- bridge parse errors;
- disk full;
- queue growth;
- high CPU;
- log directory size;
- planner timeout;
- provider errors.

Lua watchdog:

- sidecar heartbeat stale;
- command lease;
- action timeout;
- stuck;
- player absent/dead.

## 8.5. Local network

- core работает без listener;
- dashboard только localhost;
- random token;
- CORS deny;
- no remote bind by default;
- no UPnP;
- no firewall changes;
- no telemetry upload default.

## 8.6. Filesystem

Allowlist:

- repo;
- user config;
- `%USERPROFILE%\Zomboid\Lua\pz_agent`;
- mod destination;
- save destination только backup subsystem;
- game install read-only.

Path traversal запрещён.

## 8.7. Process control

Launcher может запускать sidecar и игру, но:

- не убивает другие процессы по имени без PID ownership;
- хранит собственный PID;
- graceful shutdown;
- не внедряется в процесс;
- не требует debugger.

## 8.8. Secrets

- `.env` в `.gitignore`;
- example без значений;
- log redaction;
- provider key не передаётся Lua;
- diagnostics не содержит key;
- CI secret scan.

## 8.9. Mod conflicts

Doctor:

- перечисляет активные моды;
- проверяет известные конфликты;
- не отключает их автоматически;
- предлагает чистый test profile;
- capability может перейти experimental.

## 8.10. Multiplayer

MVP должен блокировать mutating autonomy, если обнаружен неподдерживаемый multiplayer/server режим. Позже разрешается explicit private/co-op support после отдельных тестов.

## 8.11. Threat model

Учитывать:

- malformed IPC;
- stale replay;
- prompt injection;
- malicious item/display text;
- symlink/path tricks;
- compromised local MCP client;
- accidental public bind;
- disk exhaustion;
- save corruption;
- uncontrolled retries;
- stale refs;
- mod API drift.

## 8.12. Safe failure

При любой неопределённости:

- не выполнять новое mutating action;
- не очищать ручную очередь;
- сохранять trace;
- сообщать понятную ошибку;
- оставлять персонажа под ручным управлением.
