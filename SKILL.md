---
name: polymarket-world-cup-shock-ladder
description: Trade FIFA World Cup markets using shock detection + percentile ladder entries (quant overreaction/recovery framework).
metadata:
  author: Alyna + Hermes
  version: "0.1.0"
  displayName: Polymarket World Cup Shock Ladder
  difficulty: advanced
---

# Polymarket World Cup Shock Ladder

A World Cup strategy skill based on the quant framework shared by Roan (@RohOnChain):
- Detect fast adverse shocks
- Bucket by market state
- Enter with a depth ladder (P50/P75/P90/P95)
- Size deepest entries largest

Reference: https://x.com/RohOnChain/status/2061814989279949126

## What this skill does

- Scans active World Cup markets (`import_source=polymarket`)
- Detects valid shocks over a rolling 2-minute window using:
  - % drop threshold (`shock_drop_pct`, default 15%)
  - absolute drop threshold (`shock_drop_abs`, default 8c)
  - per-market cooldown (`cooldown_seconds`, default 180s)
- Classifies each shock across 5 dimensions:
  - league tier
  - favoritism
  - order-book depth proxy (via spread)
  - match-time bucket
  - goal-state bucket
- Builds percentile depths (fallback conservative profile) and places one deepest eligible rung with FAK limit buy

## Defaults

- Dry-run by default (`--live` required for real orders)
- Max spread filter: 3c
- Max slippage filter: 4%
- Per-shock max capital: $20
- Daily budget: $60
- Max trades per run: 4

## Files

- `world_cup_shock_ladder.py` — strategy runtime
- `config.json` — tunables
- `daily_spend.json` — auto-created runtime state
- `cooldown_state.json` — auto-created cooldown memory

## Quick start

```bash
cd skills/polymarket-world-cup-shock-ladder

# Inspect config
python world_cup_shock_ladder.py --config

# Dry run
python world_cup_shock_ladder.py

# Live (real orders)
python world_cup_shock_ladder.py --live
```

## Configure

```bash
python world_cup_shock_ladder.py --set shock_drop_pct=0.18
python world_cup_shock_ladder.py --set shock_drop_abs=0.10
python world_cup_shock_ladder.py --set max_position_usd=30
python world_cup_shock_ladder.py --set daily_budget_usd=100
```

## Notes

- Current implementation uses spread/slippage as an orderbook-depth proxy because full L2 concentration is not always exposed in the SDK context payload.
- Exit is currently surfaced as a target price in reasoning/telemetry; explicit automated exit legs can be added in v0.2.
- Start in `sim` or dry-run and collect your own bucket-level hit-rate stats before scaling.

## Deterministic spec (Skill Builder style)

### Signal
- Fast adverse YES-price shock over `shock_window_seconds`
- Must satisfy both percentage and absolute drop thresholds

### Entry logic
- Classify shock bucket (favoritism, depth proxy, match-time, goal state)
- Compute ladder depths (P50/P75/P90/P95)
- Enter only deepest currently-eligible rung with risk filters passing

### Exit logic
- No automated exit leg in v0.1
- Bounce target is included in reasoning/telemetry for discretionary or future automation

### Market selection
- Active Polymarket-imported World Cup/FIFA/soccer markets
- Text/tag filter plus spread/slippage/cooldown gates

### Position sizing
- Fixed per-shock ladder allocation from `max_position_usd` (10/20/30/40 split)
- Enforced by per-run and daily budget caps

### Risk controls
- `max_spread`, `max_slippage_pct`
- `cooldown_seconds`
- `max_trades_per_run`
- `daily_budget_usd`
- optional context safeguards (disable with `--no-safeguards`)
