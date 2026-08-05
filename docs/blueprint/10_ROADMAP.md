# 10. Roadmap и backlog

## Phase 0 — исследование

- install detector;
- build detector;
- API indexer;
- doctor;
- ADR architecture;
- risk register.

Exit: точная target matrix и generated API report.

## Phase 1 — skeleton

- monorepo;
- CI;
- schemas;
- CLI;
- logging;
- config;
- versioning.

Exit: чистые checks, release skeleton.

## Phase 2 — bridge

- Lua heartbeat;
- sidecar handshake;
- seq/ack;
- stop;
- HUD;
- capability report.

Exit: reconnect/restart tests.

## Phase 3 — observation

- player stats;
- action queue;
- nested inventory;
- nearby world;
- diff stream;
- refs.

Exit: snapshot fixtures и live observation.

## Phase 4 — base actions

- wait;
- move;
- transfer;
- ensure main;
- eat;
- drink;
- read;
- cancel.

Exit: smoke S02–S08.

## Phase 5 — MCP

- tools;
- resources;
- subscriptions;
- errors;
- docs;
- example client.

Exit: MCP Inspector/manual test.

## Phase 6 — policy

- food/drink/read selectors;
- priorities;
- permissions;
- home bounds;
- resource reservations.

Exit: deterministic autonomous maintenance без LLM.

## Phase 7 — planner

- provider abstraction;
- typed plan;
- critic;
- recovery;
- memory.

Exit: multi-step voice/text goals.

## Phase 8 — voice

- TeamON adapter;
- barge-in;
- TTS events;
- stop bypass.

Exit: voice acceptance scenarios.

## Phase 9 — packaging

- Windows package;
- installer;
- launcher;
- backup UI/CLI;
- uninstaller;
- dashboard optional.

Exit: clean-machine install test.

## Phase 10 — advanced skills

- world water;
- sleep;
- medical;
- base sorting;
- search;
- TV/radio;
- clothing.

## Phase 11 — experimental input

- virtual controller;
- screen capture;
- UI automation;
- combat research;
- vehicle research.

Этот phase изолирован feature flag и не блокирует stable release.

## Backlog priority

### P0

- panic stop;
- manual takeover;
- heartbeat;
- no replay;
- save backup;
- honest postconditions.

### P1

- move;
- inventory;
- eat;
- drink;
- read;
- MCP;
- doctor.

### P2

- autonomous policy;
- voice;
- memory;
- packaging.

### P3

- advanced interactions;
- visual fallback;
- second character.

## Release gates

### Alpha

Developer install, safe save, observe + move + stop.

### Beta

Inventory + consume + read + MCP + voice, 30-minute endurance.

### 1.0

Installer, backup/restore, docs, compatibility report, all acceptance smoke cases.

## Issue granularity

Каждая issue содержит:

- motivation;
- scope;
- non-goals;
- API evidence;
- implementation;
- tests;
- acceptance;
- risks;
- rollback.
