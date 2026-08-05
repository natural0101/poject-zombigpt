# 11. Структура создаваемого репозитория

## 11.1. Предпочтительная структура

```text
pz-agent/
├── README.md
├── LICENSE
├── SECURITY.md
├── PRIVACY.md
├── CONTRIBUTING.md
├── AGENTS.md
├── CHANGELOG.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .editorconfig
├── .gitignore
├── .gitattributes
├── .pre-commit-config.yaml
├── packages/
│   ├── pz_agent_core/
│   │   └── src/pz_agent_core/
│   │       ├── session/
│   │       ├── protocol/
│   │       ├── ipc/
│   │       ├── capabilities/
│   │       ├── observation/
│   │       ├── actions/
│   │       ├── policy/
│   │       ├── planner/
│   │       ├── memory/
│   │       ├── safety/
│   │       └── diagnostics/
│   ├── pz_agent_mcp/
│   │   └── src/pz_agent_mcp/
│   ├── pz_agent_cli/
│   │   └── src/pz_agent_cli/
│   ├── pz_agent_voice/
│   │   └── src/pz_agent_voice/
│   └── pz_agent_dashboard/
├── pz-mod/
│   ├── mod.info
│   ├── common/
│   └── 42/
│       ├── mod.info
│       └── media/lua/client/
├── schemas/
├── installer/
├── scripts/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── lua/
│   ├── fixtures/
│   └── game-smoke/
├── docs/
├── examples/
└── .github/
    ├── workflows/
    ├── ISSUE_TEMPLATE/
    └── pull_request_template.md
```

## 11.2. Пакет core

Не импортирует MCP framework, UI или конкретный LLM SDK. Содержит domain types и business logic.

Core должен быть пригоден для запуска с `provider=none`.

## 11.3. MCP package

Тонкий adapter:

- преобразует MCP input в core commands;
- не дублирует policy;
- сериализует domain errors;
- запускается через stdio;
- имеет integration tests.

## 11.4. Voice package

Interface:

```python
class VoiceAdapter(Protocol):
    async def events(self) -> AsyncIterator[VoiceInput]: ...
    async def speak(self, message: VoiceOutput) -> None: ...
    async def cancel_speech(self) -> None: ...
```

TeamON implementation может быть отдельным plugin. Fake adapter обязателен для тестов.

## 11.5. Lua structure

`PZAgent_Main.lua` минимален. Он связывает modules, но не содержит всю логику.

Pure functions выносятся так, чтобы их можно было тестировать вне игры.

## 11.6. Schemas

Source of truth либо:

- Python domain model → generated JSON Schema;
- либо hand-written schemas с tests sync.

Нельзя иметь два расходящихся определения.

## 11.7. Generated files

В Git можно хранить:

- generated schema;
- API compatibility metadata без game code;
- version manifest.

Нельзя хранить:

- vanilla Lua source;
- saves;
- Steam credentials;
- absolute local paths;
- Workshop assets без прав.

## 11.8. Configuration

Пример:

```toml
[game]
channel = "stable"
expected_build = "42.20"

[session]
default_mode = "observe"
require_backup = true

[safety]
panic_hotkey = "F12"
manual_takeover = true
max_autonomous_radius = 30
allow_multiplayer = false

[planner]
provider = "none"
max_steps = 8

[voice]
adapter = "teamon"
enabled = false
```

Config validation до старта.

## 11.9. Versioning

Отдельно:

- product version;
- protocol version;
- schema version;
- mod version;
- compatibility build range.

Release gate проверяет синхронизацию.
