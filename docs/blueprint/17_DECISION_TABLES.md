# 17. Таблицы решений

Числа ниже — стартовые настройки для тестов, а не доказанные универсальные игровые балансы. В production они должны настраиваться и иметь hysteresis.

## 17.1. Потребности

| Состояние | Trigger | Exit | Приоритет |
|---|---:|---:|---:|
| Critical bleeding | любое активное опасное кровотечение | лечение/нет supplies | 4 |
| Critical thirst | 0.70 | 0.20 | 6 |
| Thirst | 0.35 | 0.15 | 8 |
| Critical hunger | 0.70 | 0.25 | 7 |
| Hunger | 0.35 | 0.15 | 9 |
| Critical fatigue | 0.80 | 0.35 | 10 |
| Low endurance | 0.15 | 0.65 | 11 |
| Boredom | moodle >= 3 | moodle <= 1 | 20 |

## 17.2. Threat

| Условие | Action |
|---|---|
| panic stop | disarm + cancel owned |
| user movement | cancel + suspend |
| zombie chasing and distance < critical | short flee |
| visible zombie near during read/eat | interrupt |
| zombie far, not chasing | alert only |
| no safe flee square | stop + warn |

## 17.3. Food policy pseudocode

```python
def choose_food(items, state, policy):
    candidates = []
    for item in items:
        if not item.food:
            continue
        if item.favorite or item.reserved:
            continue
        if item.food.poisonous:
            continue
        if item.food.raw_risk and not policy.allow_raw:
            continue
        if item.food.freshness == "rotten" and not policy.allow_rotten:
            continue
        if item.food.requires_preparation:
            continue

        need = state.hunger
        reduction = abs(item.food.hunger_change)
        target_fit = -abs(max(0, need - policy.target_hunger) - reduction)
        scarcity = policy.scarcity_penalty(item.full_type)
        waste = max(0, reduction - need) * policy.waste_weight

        score = (
            4.0 * target_fit
            + 1.0 * item.food.freshness_score
            + 0.5 * item.food.happiness_score
            + 0.2 * item.food.calorie_fit(state.weight_goal)
            - scarcity
            - waste
            - item.food.thirst_penalty
        )
        candidates.append((score, item))
    return max(candidates, default=None)
```

## 17.4. Drink policy

Hard reject:

- tainted;
- poison;
- forbidden alcohol;
- reserved emergency item при неcritical thirst.

Score:

- clean water;
- thirst fit;
- remaining uses;
- weight;
- side effects;
- scarcity.

## 17.5. Reading policy

| Goal | Preferred |
|---|---|
| reduce boredom | magazine/newspaper/generic book |
| learn recipe | unread recipe magazine |
| skill multiplier | matching range skill book |
| explore lore | requested print media |
| no goal | no autonomous reading unless mood policy |

## 17.6. Recovery table

| Failure | Recovery |
|---|---|
| INVALID_REF | refresh once |
| PLAYER_BUSY_MANUAL_ACTION | suspend |
| PATH_NOT_FOUND | alternate adjacent square once |
| PATH_STUCK | cancel, one replan |
| ACTION_TIMEOUT | inspect queue, stop |
| POSTCONDITION_FAILED | no blind repeat; report |
| THREAT_INTERRUPTED | emergency policy |
| CAPABILITY_UNAVAILABLE | fallback only if enabled |
| LEASE_EXPIRED | reject |
| GAME_DISCONNECTED | mark lost |
