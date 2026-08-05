# English implementation brief

Build a production-oriented local AI companion for Project Zomboid 42.20 Stable.

The system is a hybrid:

- an in-game Lua bridge observes structured state and executes verified timed actions;
- a local sidecar owns session lifecycle, IPC, policies, planning, memory and diagnostics;
- an MCP stdio server exposes a small typed tool surface;
- a deterministic reflex guard can interrupt the planner;
- a voice adapter integrates with TeamON;
- screen capture and virtual input are optional experimental fallbacks.

The first stable release must support connection, observation, nested inventory, movement, timed transfer, safe eating, safe drinking, reading, cancellation, manual takeover, panic stop, save backup, MCP, deterministic hunger/thirst maintenance, packaging and tests.

Do not use blind keyboard automation for actions supported by the game API. Do not expose raw Lua or shell execution. Do not report success before a postcondition is observed. Do not vendor game files. Do not enable autonomy by default. Do not claim combat is solved unless a separately gated experimental adapter passes dedicated tests.
