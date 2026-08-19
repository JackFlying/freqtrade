#!/usr/bin/env python3

import argparse
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


TARGET_RETURN_PCT = (1.10**6 - 1) * 100
MIN_TRADES = 80
MAX_DRAWDOWN_PCT = 15.0
MIN_PROFIT_FACTOR = 1.4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Jointly optimize screening, entry, exit, and execution for 4h."
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--samples", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "user_data/backtest_results/optimization_4h_full10.json",
    )
    return parser.parse_args()


def random_parameters(rng: random.Random) -> StrategyParameters:
    mode = rng.choice(("pullback", "pullback", "breakout"))
    rsi_min = rng.choice((34.0, 38.0, 42.0, 46.0, 50.0, 54.0))
    rsi_max = rng.choice(
        tuple(value for value in (58.0, 60.0, 64.0, 68.0, 72.0) if value > rsi_min)
    )
    atr_min = rng.choice((0.3, 0.5, 0.7, 1.0))
    atr_max = rng.choice(
        tuple(value for value in (4.0, 6.0, 8.0, 10.0) if value > atr_min)
    )
    return StrategyParameters(
        # Screening.
        adx_min=rng.choice((12.0, 14.0, 18.0, 22.0, 26.0, 30.0)),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=rng.choice((0.6, 0.8, 1.0, 1.2, 1.4)),
        ema20_slope_min=rng.choice((-0.1, 0.0, 0.1, 0.2)),
        atr_pct_min=atr_min,
        atr_pct_max=atr_max,
        market_filter=rng.choice((True, True, False)),
        market_adx_min=rng.choice((12.0, 14.0, 16.0, 18.0, 20.0)),
        # Entry.
        mode=mode,
        touch_pct=rng.choice((0.25, 0.5, 1.0, 1.5, 2.0)),
        breakout_bars=rng.choice((4, 6, 8, 12, 16, 20, 24)),
        # Exit.
        stop_pct=rng.choice((4.0, 5.0, 6.0, 7.0, 9.0)),
        reward_risk=rng.choice((0.7, 0.9, 1.1, 1.3, 1.5, 1.7)),
        break_even_r=rng.choice((0.4, 0.6, 0.8, 1.0)),
        max_hold_bars=rng.choice((6, 12, 18, 24)),
        ma7_exit_threshold_pct=rng.choice((0.0, 0.5, 1.0, 1.5, 2.0)),
        chandelier_exit_enabled=False,
        partial_take_profit_enabled=False,
        # Execution.
        max_open_trades=rng.choice((2, 3, 4, 5)),
    )


def qualified(summary: dict[str, Any]) -> bool:
    return bool(
        summary["trade_count"] >= MIN_TRADES
        and (summary["profit_factor"] or 0) >= MIN_PROFIT_FACTOR
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
    )


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    shortfall = max(0.0, TARGET_RETURN_PCT - summary["total_profit_pct"])
    return (
        shortfall,
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -(summary["profit_factor"] or 0),
        -summary["win_rate_pct"],
    )


def evaluate(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> dict[str, Any]:
    result = simulate(frames, parameters, start, end, initial_balance)
    return {
        "parameters": asdict(parameters),
        "summary": result["summary"],
    }


def local_refine(
    frames: dict[str, pd.DataFrame],
    seed: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    values = {
        "adx_min": (max(10.0, seed.adx_min - 2), seed.adx_min, seed.adx_min + 2),
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
        "stop_pct": (
            max(3.0, seed.stop_pct - 1),
            seed.stop_pct,
            seed.stop_pct + 1,
        ),
        "reward_risk": (
            max(0.5, seed.reward_risk - 0.2),
            seed.reward_risk,
            seed.reward_risk + 0.2,
        ),
        "max_hold_bars": (
            max(6, seed.max_hold_bars - 6),
            seed.max_hold_bars,
            seed.max_hold_bars + 6,
        ),
        "max_open_trades": (
            max(1, seed.max_open_trades - 1),
            seed.max_open_trades,
            seed.max_open_trades + 1,
        ),
    }
    for index, parameters in enumerate(
        (
            replace(
                seed,
                adx_min=adx_min,
                volume_factor=volume_factor,
                touch_pct=touch_pct,
                stop_pct=stop_pct,
                reward_risk=reward_risk,
                max_hold_bars=max_hold_bars,
                max_open_trades=max_open_trades,
            )
            for adx_min in values["adx_min"]
            for volume_factor in values["volume_factor"]
            for touch_pct in values["touch_pct"]
            for stop_pct in values["stop_pct"]
            for reward_risk in values["reward_risk"]
            for max_hold_bars in values["max_hold_bars"]
            for max_open_trades in values["max_open_trades"]
        ),
        start=1,
    ):
        results.append(evaluate(frames, parameters, start, end, initial_balance))
        if index % 300 == 0:
            print(f"Local refinement: {index}", flush=True)
    return results


def sixty_day_summaries(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    summaries = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + pd.Timedelta(days=60), end)
        summaries.append(
            simulate(
                frames,
                parameters,
                cursor,
                window_end,
                initial_balance,
            )["summary"]
        )
        cursor = window_end
    return summaries


def main() -> int:
    args = parse_args()
    frames, start, end = load_4h_frames(args.days)
    if not frames:
        raise RuntimeError("No 4h frames available")
    print(
        f"Loaded {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}",
        flush=True,
    )

    rng = random.Random(args.seed)
    broad = []
    seen = set()
    while len(broad) < args.samples:
        parameters = random_parameters(rng)
        key = tuple(asdict(parameters).values())
        if key in seen:
            continue
        seen.add(key)
        broad.append(evaluate(frames, parameters, start, end, args.initial_balance))
        if len(broad) % 250 == 0:
            print(f"Broad joint search: {len(broad)}/{args.samples}", flush=True)

    qualified_broad = [item for item in broad if qualified(item["summary"])]
    qualified_broad.sort(key=rank)
    seeds = qualified_broad[:1]
    local = []
    for seed in seeds:
        local.extend(
            local_refine(
                frames,
                StrategyParameters(**seed["parameters"]),
                start,
                end,
                args.initial_balance,
            )
        )
    all_results = broad + local
    qualified_all = [item for item in all_results if qualified(item["summary"])]
    qualified_all.sort(key=rank)
    if not qualified_all:
        raise RuntimeError("No result satisfied the risk constraints")
    target_hits = [
        item
        for item in qualified_all
        if item["summary"]["total_profit_pct"] >= TARGET_RETURN_PCT
    ]
    selected = target_hits[0] if target_hits else qualified_all[0]
    parameters = StrategyParameters(**selected["parameters"])
    full = simulate(frames, parameters, start, end, args.initial_balance)
    windows = sixty_day_summaries(
        frames,
        parameters,
        start,
        end,
        args.initial_balance,
    )
    payload = {
        "data": {
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": args.days,
            "broad_samples": args.samples,
            "local_samples": len(local),
            "minimum_trades": MIN_TRADES,
            "minimum_profit_factor": MIN_PROFIT_FACTOR,
            "maximum_drawdown_pct": MAX_DRAWDOWN_PCT,
            "monthly_target_pct": 10.0,
            "target_total_return_pct": TARGET_RETURN_PCT,
            "target_hit_count": len(target_hits),
            "fee_rate": FEE_RATE,
        },
        "selected": full,
        "selected_60d_windows": windows,
        "selected_weekly_returns": period_returns(full, 7),
        "selected_monthly_returns": period_returns(full, 30),
        "target_hits": target_hits[:30],
        "closest_candidates": qualified_all[:30],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.output}", flush=True)
    print(
        json.dumps(
            {
                "target_return": TARGET_RETURN_PCT,
                "hits": len(target_hits),
                "selected": {
                    "parameters": asdict(parameters),
                    "summary": full["summary"],
                    "windows": windows,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
