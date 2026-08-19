#!/usr/bin/env python3

import json
import random
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.research_4h_strategy import (  # noqa: E402
    FEE_RATE,
    StrategyParameters,
    load_4h_frames,
    simulate,
)


DAYS = 360
INITIAL_BALANCE = 1000.0
TARGET_RETURN_PCT = (1.10**12 - 1) * 100
MIN_TRADES = 80
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN_PCT = 55.0
BROAD_SAMPLES = 1600
LOCAL_SAMPLES = 1800
OUTPUT_PATH = ROOT_DIR / "user_data/backtest_results/optimization_4h_360d_monthly10.json"


def random_parameters(rng: random.Random) -> StrategyParameters:
    rsi_min = rng.choice((26.0, 30.0, 34.0, 38.0, 42.0, 46.0, 50.0, 54.0))
    rsi_max = rng.choice(
        tuple(value for value in (56.0, 60.0, 64.0, 68.0, 72.0, 76.0) if value > rsi_min)
    )
    atr_min = rng.choice((0.3, 0.5, 0.7, 1.0))
    atr_max = rng.choice(
        tuple(value for value in (4.0, 6.0, 8.0, 10.0, 12.0) if value > atr_min)
    )
    return StrategyParameters(
        mode=rng.choice(("pullback", "pullback", "breakout")),
        adx_min=rng.choice((6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 22.0)),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=rng.choice((0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.4)),
        touch_pct=rng.choice((0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)),
        breakout_bars=rng.choice((4, 6, 8, 12, 16, 20, 24)),
        market_filter=rng.choice((True, True, True, False)),
        stop_pct=rng.choice((4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0)),
        reward_risk=rng.choice((0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 2.0)),
        break_even_r=rng.choice((0.2, 0.4, 0.6, 0.8, 1.0)),
        max_hold_bars=rng.choice((6, 12, 18, 24, 30, 36)),
        max_open_trades=1,
        ma7_exit_threshold_pct=rng.choice((0.0, 0.5, 1.0, 1.5, 2.0, 2.5)),
        chandelier_exit_enabled=False,
        partial_take_profit_enabled=False,
        ema20_slope_min=rng.choice((-0.2, -0.1, 0.0, 0.1, 0.2)),
        atr_pct_min=atr_min,
        atr_pct_max=atr_max,
        market_adx_min=rng.choice((8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0)),
    )


def evaluate(frames: dict, parameters: StrategyParameters, start, end) -> dict[str, Any]:
    result = simulate(frames, parameters, start, end, INITIAL_BALANCE)
    return {"parameters": asdict(parameters), "summary": result["summary"]}


def qualified(item: dict[str, Any]) -> bool:
    summary = item["summary"]
    return bool(
        summary["trade_count"] >= MIN_TRADES
        and (summary["profit_factor"] or 0) >= MIN_PROFIT_FACTOR
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
    )


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        max(0.0, TARGET_RETURN_PCT - summary["total_profit_pct"]),
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -(summary["profit_factor"] or 0),
    )


def mutate(rng: random.Random, seed: StrategyParameters) -> StrategyParameters:
    rsi_min = max(20.0, min(60.0, seed.rsi_min + rng.choice((-4, 0, 4))))
    rsi_max = max(rsi_min + 4, min(80.0, seed.rsi_max + rng.choice((-4, 0, 4))))
    return replace(
        seed,
        adx_min=max(4.0, seed.adx_min + rng.choice((-2, 0, 2))),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=max(0.3, seed.volume_factor + rng.choice((-0.2, 0, 0.2))),
        touch_pct=max(0.1, seed.touch_pct + rng.choice((-0.5, 0, 0.5))),
        stop_pct=max(3.0, seed.stop_pct + rng.choice((-1, 0, 1))),
        reward_risk=max(0.5, seed.reward_risk + rng.choice((-0.2, 0, 0.2))),
        break_even_r=max(0.1, seed.break_even_r + rng.choice((-0.2, 0, 0.2))),
        max_hold_bars=max(3, seed.max_hold_bars + rng.choice((-6, 0, 6))),
        ma7_exit_threshold_pct=max(
            0.0, seed.ma7_exit_threshold_pct + rng.choice((-0.5, 0, 0.5))
        ),
        ema20_slope_min=seed.ema20_slope_min + rng.choice((-0.1, 0, 0.1)),
        market_adx_min=max(6.0, seed.market_adx_min + rng.choice((-2, 0, 2))),
    )


def windows(frames: dict, parameters: StrategyParameters, start, end, days: int) -> list[dict]:
    items = []
    cursor = start
    while cursor < end:
        finish = min(cursor + pd.Timedelta(days=days), end)
        items.append(simulate(frames, parameters, cursor, finish, INITIAL_BALANCE)["summary"])
        cursor = finish
    return items


def main() -> int:
    frames, start, end = load_4h_frames(DAYS)
    print(f"Loaded {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}")
    rng = random.Random(20260819)
    results, seen = [], set()
    while len(results) < BROAD_SAMPLES:
        parameters = random_parameters(rng)
        key = tuple(asdict(parameters).values())
        if key not in seen:
            seen.add(key)
            results.append(evaluate(frames, parameters, start, end))
        if len(results) and len(results) % 200 == 0:
            print(f"Broad search: {len(results)}/{BROAD_SAMPLES}", flush=True)
    seeds = sorted((item for item in results if qualified(item)), key=rank)[:8]
    local = []
    while len(local) < LOCAL_SAMPLES:
        parameters = mutate(rng, StrategyParameters(**rng.choice(seeds)["parameters"]))
        key = tuple(asdict(parameters).values())
        if key not in seen:
            seen.add(key)
            local.append(evaluate(frames, parameters, start, end))
        if len(local) and len(local) % 200 == 0:
            print(f"Local search: {len(local)}/{LOCAL_SAMPLES}", flush=True)
    candidates = sorted(
        (item for item in results + local if qualified(item)),
        key=rank,
    )
    selected = StrategyParameters(**candidates[0]["parameters"])
    full = simulate(frames, selected, start, end, INITIAL_BALANCE)
    payload = {
        "data": {
            "days": DAYS,
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fee_rate": FEE_RATE,
            "target_monthly_pct": 10.0,
            "target_total_return_pct": TARGET_RETURN_PCT,
            "broad_samples": len(results),
            "local_samples": len(local),
        },
        "target_hit_count": sum(
            item["summary"]["total_profit_pct"] >= TARGET_RETURN_PCT
            for item in candidates
        ),
        "selected": full,
        "selected_180d_windows": windows(frames, selected, start, end, 180),
        "selected_90d_windows": windows(frames, selected, start, end, 90),
        "candidates": candidates[:30],
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": full["summary"], "parameters": asdict(selected), "hits": payload["target_hit_count"]}, ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
