# 15. Матрица приёмки

## A. Установка

| ID | Проверка | Pass |
|---|---|---|
| A01 | Game path auto-detected | |
| A02 | Build displayed | |
| A03 | User directory handles non-ASCII | |
| A04 | Mod installed atomically | |
| A05 | Uninstall removes only manifest files | |
| A06 | No admin required | |

## B. Session

| ID | Проверка | Pass |
|---|---|---|
| B01 | OBSERVE default | |
| B02 | Arm requires backup | |
| B03 | New save requires new arm | |
| B04 | Stale sidecar blocks commands | |
| B05 | Restart does not replay command | |

## C. Safety

| ID | Проверка | Pass |
|---|---|---|
| C01 | Hotkey stop works without sidecar | |
| C02 | Voice stop bypasses planner | |
| C03 | Manual movement cancels automation | |
| C04 | Foreign timed action preserved | |
| C05 | Expired command rejected | |
| C06 | Multiplayer blocked by default | |

## D. Observation

| ID | Проверка | Pass |
|---|---|---|
| D01 | Player stats | |
| D02 | Nested inventory | |
| D03 | Current action | |
| D04 | Nearby threat | |
| D05 | Stable refs per session | |
| D06 | Full snapshot recovery | |

## E. Actions

| ID | Проверка | Pass |
|---|---|---|
| E01 | Move | |
| E02 | Stuck detection | |
| E03 | Transfer from bag | |
| E04 | Eat safe food | |
| E05 | Drink clean water | |
| E06 | Read suitable literature | |
| E07 | Cancel read | |
| E08 | Invalid ref honest failure | |

## F. MCP

| ID | Проверка | Pass |
|---|---|---|
| F01 | stdio startup | |
| F02 | tools typed | |
| F03 | resources bounded | |
| F04 | stop always available | |
| F05 | idempotency | |
| F06 | stable errors | |

## G. Autonomy

| ID | Проверка | Pass |
|---|---|---|
| G01 | Hunger maintenance | |
| G02 | Thirst maintenance | |
| G03 | Goal suspension/resume | |
| G04 | No repeated failure loop | |
| G05 | Permission boundary | |
| G06 | 30-minute endurance | |

## H. Voice

| ID | Проверка | Pass |
|---|---|---|
| H01 | Intent forwarded | |
| H02 | Status spoken | |
| H03 | Barge-in | |
| H04 | Stop latency | |
| H05 | No tick spam | |

## I. Quality

| ID | Проверка | Pass |
|---|---|---|
| I01 | CI green | |
| I02 | Type/lint clean | |
| I03 | Schemas valid | |
| I04 | Secret scan clean | |
| I05 | Release artifact | |
| I06 | Final report | |

Release 1.0 запрещён при любом fail C01–C06, E03–E08 или F04–F06.
