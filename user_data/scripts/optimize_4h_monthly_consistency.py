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
    period_returns,
    simulate,
)


DAYS = 180
INITIAL_BALANCE = 1000.0
MONTHLY_TARGET_PCT = 10.0
MIN_TRADES = 60
MAX_DRAWDOWN_PCT = 18.0
SAMPLES = 2500
SEED = 20260818
OUTPUT_PATH = ROOT_DIR / "user_data/backtest_results/optimization_4h_monthly10_consistent.json"


def random_parameters(rng: random.Random) -> StrategyParameters:
    rsi_min = rng.choice((34.0, 38.0, 42.0, 46.0, 50.0, 54.0))
    rsi_max = rng.choice(
        tuple(value for value in (58.0, 60.0, 64.0, 68.0, 72.0) if value > rsi_min)
    )
    atr_min = rng.choice((0.3, 0.5, 0.7, 1.0))
    atr_max = rng.choice(
        tuple(value for value in (4.0, 6.0, 8.0, 10.0) if value > atr_min)
    )
    return StrategyParameters(
        mode=rng.choice(("pullback", "pullback", "breakout")),
        adx_min=rng.choice((10.0, 12.0, 14.0, 16.0, 18.0, 22.0, 26.0)),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=rng.choice((0.5, 0.6, 0.8, 1.0, 1.2, 1.4)),
        touch_pct=rng.choice((0.25, 0.5, 1.0, 1.5, 2.0)),
        breakout_bars=rng.choice((4, 6, 8, 12, 16, 20, 24)),
        market_filter=rng.choice((True, True, False)),
        stop_pct=rng.choice((4.0, 5.0, 6.0, 7.0, 8.0, 9.0)),
        reward_risk=rng.choice((0.7, 0.9, 1.1, 1.3, 1.5, 1.7)),
        break_even_r=rng.choice((0.2, 0.4, 0.6, 0.8, 1.0)),
        max_hold_bars=rng.choice((6, 12, 18, 24)),
        max_open_trades=rng.choice((1, 1, 2, 3, 4)),
        ma7_exit_threshold_pct=rng.choice((0.0, 0.5, 1.0, 1.5, 2.0)),
        chandelier_exit_enabled=False,
        partial_take_profit_enabled=False,
        ema20_slope_min=rng.choice((-0.1, 0.0, 0.1, 0.2)),
        atr_pct_min=atr_min,
        atr_pct_max=atr_max,
        market_adx_min=rng.choice((10.0, 12.0, 14.0, 16.0, 18.0, 20.0)),
    )


def evaluate(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    result = simulate(frames, parameters, start, end, INITIAL_BALANCE)
    monthly_returns = period_returns(result, 30)
    return {
        "parameters": asdict(parameters),
        "summary": result["summary"],
        "monthly_returns_pct": monthly_returns,
        "monthly_minimum_pct": min(monthly_returns) if monthly_returns else -100.0,
        "monthly_average_pct": (
            sum(monthly_returns) / len(monthly_returns) if monthly_returns else -100.0
        ),
    }


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        -item["monthly_minimum_pct"],
        -item["monthly_average_pct"],
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -(summary["profit_factor"] or 0.0),
    )


def local_refine(
    frames: dict[str, pd.DataFrame],
    seed: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    values = {
        "adx_min": (max(8.0, seed.adx_min - 2), seed.adx_min, seed.adx_min + 2),
        "volume_factor": (
            max(0.4, seed.volume_factor - 0.2),
            seed.volume_factor,
            seed.volume_factor + 0.2,
        ),
        "touch_pct": (
            max(0.1, seed.touch_pct - 0.5),
            seed.touch_pct,
            seed.touch_pct + 0.5,
        ),
        "stop_pct": (max(3.0, seed.stop_pct - 1), seed.stop_pct, seed.stop_pct + 1),
        "reward_risk": (
            max(0.5, seed.reward_risk - 0.2),
            seed.reward_risk,
            seed.reward_risk + 0.2,
        ),
        "break_even_r": (
            max(0.1, seed.break_even_r - 0.2),
            seed.break_even_r,
            seed.break_even_r + 0.2,
        ),
        "max_open_trades": (
            max(1, seed.max_open_trades - 1),
            seed.max_open_trades,
            seed.max_open_trades + 1,
        ),
    }
    results = []
    for adx_min in values["adx_min"]:
        for volume_factor in values["volume_factor"]:
            for touch_pct in values["touch_pct"]:
                for stop_pct in values["stop_pct"]:
                    for reward_risk in values["reward_risk"]:
                        for break_even_r in values["break_even_r"]:
                            for max_open_trades in values["max_open_trades"]:
                                parameters = replace(
                                    seed,
                                    adx_min=adx_min,
                                    volume_factor=volume_factor,
                                    touch_pct=touch_pct,
                                    stop_pct=stop_pct,
                                    reward_risk=reward_risk,
                                    break_even_r=break_even_r,
                                    max_open_trades=max_open_trades,
                                )
                                results.append(evaluate(frames, parameters, start, end))
    return results


def main() -> int:
    frames, start, end = load_4h_frames(DAYS)
    if not frames:
        raise RuntimeError("No 4h frames available")
    rng = random.Random(SEED)
    broad = []
    seen = set()
    while len(broad) < SAMPLES:
        parameters = random_parameters(rng)
        key = tuple(asdict(parameters).values())
        if key in seen:
            continue
        seen.add(key)
        broad.append(evaluate(frames, parameters, start, end))
        if len(broad) % 250 == 0:
            print(f"Monthly consistency search: {len(broad)}/{SAMPLES}", flush=True)

    eligible = [
        item
        for item in broad
        if item["summary"]["trade_count"] >= MIN_TRADES
        and item["summary"]["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
    ]
    eligible.sort(key=rank)
    local = []
    for item in eligible[:3]:
        local.extend(
            local_refine(
                frames,
                StrategyParameters(**item["parameters"]),
                start,
                end,
            )
        )
    results = eligible + [
        item
        for item in local
        if item["summary"]["trade_count"] >= MIN_TRADES
        and item["summary"]["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
    ]
    results.sort(key=rank)
    monthly_target_hits = [
        item
        for item in results
        if item["monthly_minimum_pct"] >= MONTHLY_TARGET_PCT
    ]
    payload = {
        "data": {
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": DAYS,
            "fee_rate": FEE_RATE,
            "monthly_target_pct": MONTHLY_TARGET_PCT,
            "minimum_trades": MIN_TRADES,
            "maximum_drawdown_pct": MAX_DRAWDOWN_PCT,
            "broad_samples": len(broad),
            "local_samples": len(local),
        },
        "monthly_target_hit_count": len(monthly_target_hits),
        "selected": monthly_target_hits[0] if monthly_target_hits else results[0],
        "target_hits": monthly_target_hits[:30],
        "best_candidates": results[:30],
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload["selected"], ensure_ascii=False, indent=2))
    print(f"Monthly target hits: {len(monthly_target_hits)}")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
