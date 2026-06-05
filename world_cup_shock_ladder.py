#!/usr/bin/env python3
"""
Polymarket World Cup Shock Ladder Trader

Implements a quant-style shock/recovery strategy inspired by:
https://x.com/RohOnChain/status/2061814989279949126

Core idea:
- Detect fast downward YES-price shocks over a short window
- Classify shock state (favoritism, time, score, depth proxy)
- Place laddered buy orders at percentile depths
- Size deeper ladders larger than shallower ladders

This script is intentionally conservative by default:
- dry-run unless --live
- strict spread/slippage filters
- daily budget cap + per-run trade cap
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(line_buffering=True)

from simmer_sdk.skill import load_config, update_config, get_config_path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_SCHEMA = {
    "market_query": {
        "env": "SIMMER_WC_MARKET_QUERY",
        "default": "World Cup",
        "type": str,
        "help": "Text filter for market questions",
    },
    "import_source": {
        "env": "SIMMER_WC_IMPORT_SOURCE",
        "default": "polymarket",
        "type": str,
        "help": "Market import source filter",
    },
    "scan_limit": {
        "env": "SIMMER_WC_SCAN_LIMIT",
        "default": 250,
        "type": int,
        "help": "Max active markets to scan",
    },
    "shock_window_seconds": {
        "env": "SIMMER_WC_SHOCK_WINDOW_SECONDS",
        "default": 120,
        "type": int,
        "help": "Window for shock detection",
    },
    "shock_drop_pct": {
        "env": "SIMMER_WC_SHOCK_DROP_PCT",
        "default": 0.15,
        "type": float,
        "help": "Minimum percentage drop from local peak",
    },
    "shock_drop_abs": {
        "env": "SIMMER_WC_SHOCK_DROP_ABS",
        "default": 0.08,
        "type": float,
        "help": "Minimum absolute drop (price units, e.g. 0.08 = 8c)",
    },
    "cooldown_seconds": {
        "env": "SIMMER_WC_COOLDOWN_SECONDS",
        "default": 180,
        "type": int,
        "help": "Per-market cooldown between shock entries",
    },
    "max_spread": {
        "env": "SIMMER_WC_MAX_SPREAD",
        "default": 0.03,
        "type": float,
        "help": "Skip markets with spread above this (e.g. 0.03 = 3c)",
    },
    "max_slippage_pct": {
        "env": "SIMMER_WC_MAX_SLIPPAGE_PCT",
        "default": 0.04,
        "type": float,
        "help": "Skip if estimated slippage exceeds this fraction",
    },
    "max_position_usd": {
        "env": "SIMMER_WC_MAX_POSITION_USD",
        "default": 20.0,
        "type": float,
        "help": "Max total USD allocation per detected shock",
    },
    "max_trades_per_run": {
        "env": "SIMMER_WC_MAX_TRADES_PER_RUN",
        "default": 4,
        "type": int,
        "help": "Max buy orders per run",
    },
    "daily_budget_usd": {
        "env": "SIMMER_WC_DAILY_BUDGET_USD",
        "default": 60.0,
        "type": float,
        "help": "Daily spend cap",
    },
    "exit_bounce_cents": {
        "env": "SIMMER_WC_EXIT_BOUNCE_CENTS",
        "default": 4.0,
        "type": float,
        "help": "Reference bounce target in cents",
    },
    "favor_heavy_cutoff": {
        "env": "SIMMER_WC_FAVOR_HEAVY",
        "default": 0.85,
        "type": float,
        "help": "Favoritism heavy threshold",
    },
    "favor_moderate_cutoff": {
        "env": "SIMMER_WC_FAVOR_MODERATE",
        "default": 0.75,
        "type": float,
        "help": "Favoritism moderate threshold",
    },
    "favor_slight_cutoff": {
        "env": "SIMMER_WC_FAVOR_SLIGHT",
        "default": 0.60,
        "type": float,
        "help": "Favoritism slight threshold",
    },
    "favor_balanced_cutoff": {
        "env": "SIMMER_WC_FAVOR_BALANCED",
        "default": 0.45,
        "type": float,
        "help": "Favoritism balanced threshold",
    },
}

_config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-world-cup-shock-ladder")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

SKILL_SLUG = "polymarket-world-cup-shock-ladder"
TRADE_SOURCE = "sdk:world-cup-shock-ladder"
BASE_DIR = Path(__file__).parent
COOLDOWN_STATE = BASE_DIR / "cooldown_state.json"
DAILY_SPEND = BASE_DIR / "daily_spend.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def _load_daily_spend() -> Dict[str, float]:
    today = _utc_now().strftime("%Y-%m-%d")
    data = _load_json(DAILY_SPEND, {"date": today, "spent": 0.0, "trades": 0})
    if data.get("date") != today:
        data = {"date": today, "spent": 0.0, "trades": 0}
    return data


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------

_client = None


def get_client(live: bool):
    global _client
    if _client is None:
        try:
            from simmer_sdk import SimmerClient
        except ImportError:
            print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
            sys.exit(1)

        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            sys.exit(1)

        _client = SimmerClient(
            api_key=api_key,
            venue="polymarket",
            live=live,
        )
    return _client


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


@dataclass
class ShockEvent:
    peak_price: float
    floor_price: float
    peak_ts: datetime
    floor_ts: datetime
    drop_abs: float
    drop_pct: float


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _detect_shock(points: List[dict], window_seconds: int, min_drop_pct: float, min_drop_abs: float) -> Optional[ShockEvent]:
    parsed: List[Tuple[datetime, float]] = []
    for p in points:
        ts = _parse_ts(str(p.get("timestamp") or ""))
        price = p.get("price_yes")
        if ts is None or price is None:
            continue
        try:
            parsed.append((ts, float(price)))
        except Exception:
            continue

    if len(parsed) < 3:
        return None

    parsed.sort(key=lambda x: x[0])
    now = parsed[-1][0]
    start = now.timestamp() - window_seconds
    window = [(t, v) for (t, v) in parsed if t.timestamp() >= start]
    if len(window) < 3:
        return None

    # One-pass max drawdown over window: best peak -> later floor
    peak_price = window[0][1]
    peak_ts = window[0][0]
    best: Optional[ShockEvent] = None

    for ts, price in window[1:]:
        if price > peak_price:
            peak_price = price
            peak_ts = ts
            continue
        drop_abs = peak_price - price
        drop_pct = drop_abs / peak_price if peak_price > 0 else 0
        if drop_abs >= min_drop_abs and drop_pct >= min_drop_pct:
            candidate = ShockEvent(
                peak_price=peak_price,
                floor_price=price,
                peak_ts=peak_ts,
                floor_ts=ts,
                drop_abs=drop_abs,
                drop_pct=drop_pct,
            )
            if best is None or candidate.drop_abs > best.drop_abs:
                best = candidate

    return best


def _favoritism_bucket(price: float) -> str:
    if price >= _config["favor_heavy_cutoff"]:
        return "heavy"
    if price >= _config["favor_moderate_cutoff"]:
        return "moderate"
    if price >= _config["favor_slight_cutoff"]:
        return "slight"
    if price >= _config["favor_balanced_cutoff"]:
        return "balanced"
    return "underdog"


def _league_tier(question: str) -> str:
    q = question.lower()
    if "world cup" in q or "fifa" in q:
        return "deep"
    return "unknown"


def _match_time_bucket(question: str) -> str:
    m = re.search(r"(\d{1,3})\s*[’']", question)
    if not m:
        return "unknown"
    minute = int(m.group(1))
    if minute < 15:
        return "early"
    if minute <= 60:
        return "mid"
    if minute <= 80:
        return "late"
    return "final"


def _goal_state_bucket(question: str) -> str:
    m = re.search(r"(\d+)\s*[-:]\s*(\d+)", question)
    if not m:
        return "unknown"
    a, b = int(m.group(1)), int(m.group(2))
    d = abs(a - b)
    if d == 0:
        return "level"
    if d == 1:
        return "close"
    if d == 2:
        return "comfortable"
    return "blowout"


def _depth_proxy_bucket(spread: Optional[float]) -> str:
    # Proxy for orderbook quality when full L2 snapshot isn't available.
    if spread is None:
        return "balanced"
    if spread <= 0.01:
        return "deep"
    if spread <= 0.02:
        return "balanced"
    return "top-heavy"


def _depth_percentiles_cents(favor: str, depth_proxy: str, match_time: str) -> Dict[str, float]:
    # Sparse-bucket conservative fallback from article summary: 6/9/13/18 cents.
    p50, p75, p90, p95 = 6.0, 9.0, 13.0, 18.0

    # Lightly adapt by favor bucket.
    if favor in {"heavy", "moderate"}:
        p50 -= 0.5
        p75 -= 0.5
    if favor == "underdog":
        p90 += 1.0
        p95 += 1.5

    # Wider spreads demand deeper entries.
    if depth_proxy == "top-heavy":
        p75 += 1.0
        p90 += 2.0
        p95 += 2.5

    # Late-game shocks are often less mean-reverting than early/mid.
    if match_time in {"late", "final"}:
        p50 += 0.5
        p75 += 1.0

    return {
        "p50": max(2.0, p50),
        "p75": max(3.0, p75),
        "p90": max(4.0, p90),
        "p95": max(5.0, p95),
    }


def _choose_allocations(max_position_usd: float) -> Dict[str, float]:
    # 10/20/30/40 ladder allocation
    return {
        "p50": round(max_position_usd * 0.10, 2),
        "p75": round(max_position_usd * 0.20, 2),
        "p90": round(max_position_usd * 0.30, 2),
        "p95": round(max_position_usd * 0.40, 2),
    }


def _bucket_key(league: str, favor: str, depth: str, time_bucket: str, goal: str) -> str:
    return f"{league} {favor} {depth} {time_bucket} {goal}"


def _candidate_world_cup_market(question: str, tags: Optional[List[str]]) -> bool:
    q = question.lower()
    tagset = {str(t).lower() for t in (tags or [])}
    return (
        "world cup" in q
        or "fifa" in q
        or "world-cup" in tagset
        or "soccer" in tagset
    )


def _est_slippage_pct(ctx: dict) -> float:
    sl = (ctx or {}).get("slippage") or {}
    estimates = sl.get("estimates") or []
    if not estimates:
        return 0.0
    vals = []
    for e in estimates:
        try:
            vals.append(float(e.get("slippage_pct", 0.0)))
        except Exception:
            pass
    return max(vals) if vals else 0.0


def _safe_spread(ctx: dict, market) -> Optional[float]:
    spread = None
    try:
        spread = float((ctx or {}).get("market", {}).get("spread"))
    except Exception:
        spread = None
    if spread is None:
        try:
            spread = float(getattr(market, "spread", None))
        except Exception:
            spread = None
    return spread


def get_positions(client) -> List[dict]:
    try:
        from dataclasses import asdict

        positions = client.get_positions(venue="polymarket")
        return [asdict(p) for p in positions]
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []


def check_context_safeguards(context: dict) -> Tuple[bool, List[str]]:
    """Check context for deal-breakers. Returns (should_trade, reasons)."""
    if not context:
        return True, []

    reasons: List[str] = []
    warnings = context.get("warnings", [])
    discipline = context.get("discipline", {})

    for warning in warnings:
        if "MARKET RESOLVED" in str(warning).upper():
            return False, ["Market already resolved"]

    warning_level = discipline.get("warning_level", "none")
    if warning_level == "severe":
        return False, [f"Severe flip-flop warning: {discipline.get('flip_flop_warning', '')}"]
    if warning_level == "mild":
        reasons.append("Mild flip-flop warning (proceed with caution)")

    return True, reasons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(live: bool, quiet: bool = False, positions_only: bool = False, use_safeguards: bool = True) -> int:
    client = get_client(live=live)

    if positions_only:
        positions = get_positions(client)
        print(json.dumps(positions, indent=2))
        return 0

    daily = _load_daily_spend()
    cooldown = _load_json(COOLDOWN_STATE, {})
    now_ts = _utc_now().timestamp()

    if not quiet:
        mode = "LIVE" if live else "DRY RUN"
        print(f"🏆 World Cup Shock Ladder ({mode})")
        print("=" * 56)
        print(f"scan_limit={_config['scan_limit']} | daily_spent=${daily['spent']:.2f}/{_config['daily_budget_usd']:.2f}")

    markets = client.get_markets(
        status="active",
        import_source=_config["import_source"],
        limit=int(_config["scan_limit"]),
    )

    # Filter to likely World Cup markets + query term
    query = str(_config["market_query"]).lower()
    filtered = []
    for m in markets:
        q = (getattr(m, "question", "") or "")
        tags = getattr(m, "tags", None)
        if query in q.lower() and _candidate_world_cup_market(q, tags):
            filtered.append(m)

    if not quiet:
        print(f"scanned={len(markets)} | world_cup_candidates={len(filtered)}")

    if not filtered:
        print("No World Cup candidates found.")
        return 0

    trades = 0
    spent_this_run = 0.0
    executed = []

    for m in filtered:
        if trades >= int(_config["max_trades_per_run"]):
            break
        if daily["spent"] + spent_this_run >= float(_config["daily_budget_usd"]):
            break

        market_id = m.id
        question = m.question

        # Cooldown gate
        last_ts = float(cooldown.get(market_id, 0.0))
        if now_ts - last_ts < float(_config["cooldown_seconds"]):
            continue

        # Context + risk gates
        ctx = client.get_market_context(market_id, venue="polymarket") or {}
        if use_safeguards:
            should_trade, reasons = check_context_safeguards(ctx)
            if not should_trade:
                continue
            if reasons and not quiet:
                print(f"safeguard: {question[:64]}... -> {'; '.join(reasons)}")

        spread = _safe_spread(ctx, m)
        if spread is not None and spread > float(_config["max_spread"]):
            continue
        slippage = _est_slippage_pct(ctx)
        if slippage > float(_config["max_slippage_pct"]):
            continue

        points = client.get_price_history(market_id)
        shock = _detect_shock(
            points,
            window_seconds=int(_config["shock_window_seconds"]),
            min_drop_pct=float(_config["shock_drop_pct"]),
            min_drop_abs=float(_config["shock_drop_abs"]),
        )
        if shock is None:
            continue

        # Classification
        favor = _favoritism_bucket(shock.peak_price)
        league = _league_tier(question)
        time_bucket = _match_time_bucket(question)
        goal_bucket = _goal_state_bucket(question)
        depth_proxy = _depth_proxy_bucket(spread)
        bucket = _bucket_key(league, favor, depth_proxy, time_bucket, goal_bucket)

        depths = _depth_percentiles_cents(favor, depth_proxy, time_bucket)
        alloc = _choose_allocations(float(_config["max_position_usd"]))

        # Ladder entries relative to pre-shock peak
        rung_prices = {
            k: max(0.001, round(shock.peak_price - (v / 100.0), 3))
            for k, v in depths.items()
        }

        current_price = float((ctx.get("market") or {}).get("current_price") or getattr(m, "current_probability", 0.5))

        # Eligible rungs: current ask has dropped to rung or deeper
        eligible = [k for k in ["p50", "p75", "p90", "p95"] if current_price <= rung_prices[k]]
        if not eligible:
            continue

        # Trade only deepest currently-eligible rung for conservative fill logic
        rung = eligible[-1]
        amount = alloc[rung]

        if amount <= 0.0:
            continue
        if daily["spent"] + spent_this_run + amount > float(_config["daily_budget_usd"]):
            continue

        bounce_target = round(min(0.999, current_price + (float(_config["exit_bounce_cents"]) / 100.0)), 3)

        note = (
            f"WC shock ladder | bucket={bucket} | drop={shock.drop_abs:.3f} ({shock.drop_pct:.1%}) "
            f"| rung={rung}@{rung_prices[rung]:.3f} | cur={current_price:.3f} | target={bounce_target:.3f}"
        )

        if live:
            result = client.trade(
                market_id=market_id,
                side="yes",
                amount=amount,
                action="buy",
                venue="polymarket",
                order_type="FAK",
                price=rung_prices[rung],
                reasoning=note,
                source=TRADE_SOURCE,
                skill_slug=SKILL_SLUG,
                allow_rebuy=False,
                signal_data={
                    "bucket": bucket,
                    "drop_abs": round(shock.drop_abs, 5),
                    "drop_pct": round(shock.drop_pct, 5),
                    "rung": rung,
                    "current_price": round(current_price, 5),
                    "rung_price": rung_prices[rung],
                    "bounce_target": bounce_target,
                    "spread": None if spread is None else round(spread, 5),
                    "slippage_pct": round(slippage, 5),
                },
            )
            ok = bool(getattr(result, "success", False))
            if ok:
                trades += 1
                spent_this_run += amount
                cooldown[market_id] = now_ts
                executed.append((question, rung, amount, current_price, bounce_target, getattr(result, "order_id", None)))
        else:
            trades += 1
            spent_this_run += amount
            cooldown[market_id] = now_ts
            executed.append((question, rung, amount, current_price, bounce_target, "dry-run"))

        if trades >= int(_config["max_trades_per_run"]):
            break

    daily["spent"] = round(float(daily["spent"]) + spent_this_run, 2)
    daily["trades"] = int(daily.get("trades", 0)) + len(executed)
    _save_json(DAILY_SPEND, daily)
    _save_json(COOLDOWN_STATE, cooldown)

    if executed:
        print(f"\nExecuted {len(executed)} ladder entries:")
        for q, rung, amt, cur, tgt, oid in executed:
            print(f"- {q[:92]}{'...' if len(q) > 92 else ''}")
            print(f"  rung={rung} amount=${amt:.2f} cur={cur:.3f} target={tgt:.3f} order={oid}")
    else:
        print("No eligible shocks found this run.")

    print(f"Daily spent: ${daily['spent']:.2f} / ${float(_config['daily_budget_usd']):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="World Cup shock-ladder trader")
    ap.add_argument("--live", action="store_true", help="Execute real orders (default is dry-run)")
    ap.add_argument("--positions", action="store_true", help="Show current positions and exit")
    ap.add_argument("--no-safeguards", action="store_true", help="Disable context safeguards")
    ap.add_argument("--quiet", action="store_true", help="Less verbose output")
    ap.add_argument("--config", action="store_true", help="Print resolved config and exit")
    ap.add_argument("--set", action="append", default=[], help="Update config key=value")
    args = ap.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set value: {item} (expected key=value)")
                return 2
            k, v = item.split("=", 1)
            k = k.strip()
            if k not in CONFIG_SCHEMA:
                print(f"Unknown config key: {k}")
                return 2
            t = CONFIG_SCHEMA[k]["type"]
            try:
                updates[k] = t(v)
            except Exception as e:
                print(f"Failed to parse {k}: {e}")
                return 2
        update_config(CONFIG_SCHEMA, updates, __file__, slug=SKILL_SLUG)
        print(f"Updated config at {get_config_path(__file__, slug=SKILL_SLUG)}")
        return 0

    if args.config:
        print(json.dumps(_config, indent=2))
        return 0

    return run(
        live=args.live,
        quiet=args.quiet,
        positions_only=args.positions,
        use_safeguards=not args.no_safeguards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
