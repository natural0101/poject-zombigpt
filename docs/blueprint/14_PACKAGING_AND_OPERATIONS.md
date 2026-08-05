# 14. Packaging и эксплуатация

## 14.1. CLI-first

Даже при dashboard все операции доступны CLI.

## 14.2. Install flow

```text
pz-agent setup
→ detect game
→ detect build
→ choose user directory
→ backup existing same-id mod
→ install mod atomically
→ create config
→ run doctor
→ print launch steps
```

## 14.3. Start flow

```text
pz-agent start
→ validate config
→ start sidecar
→ expose MCP stdio config snippet
→ watch game heartbeat
→ OBSERVE by default
```

Не включать autonomy автоматически.

## 14.4. Combined launcher

Optional:

```text
Start-PZ-Agent.bat
```

Он:

- запускает sidecar;
- запускает игру через Steam URI;
- показывает status window;
- сохраняет PIDs;
- корректно закрывается.

## 14.5. Update

- compare versions;
- backup installed mod;
- replace atomically;
- migrate config;
- protocol compatibility check;
- rollback.

## 14.6. Uninstall

- stop sidecar;
- disarm if connected;
- remove только файлы manifest;
- restore previous mod backup optional;
- оставить user logs/config по выбору;
- не трогать saves.

## 14.7. Diagnostics bundle

```text
pz-agent support-bundle
```

Включает:

- versions;
- doctor;
- redacted config;
- recent logs;
- capabilities;
- protocol trace;
- no secrets;
- no save;
- no full game files.

## 14.8. Windows paths

Поддержать:

- кириллицу;
- пробелы;
- OneDrive Documents relocation;
- несколько Steam libraries;
- portable/custom `-homedir`;
- длинные пути;
- non-admin user.

## 14.9. Crash recovery

После crash:

- stale PID ignored;
- lock has owner metadata;
- old temp files cleaned safely;
- active plan marked interrupted;
- re-arm required.

## 14.10. Observability UI

Dashboard optional:

- connection;
- mode;
- safety;
- current goal;
- action trace;
- needs;
- inventory search;
- stop button;
- no raw eval console.
