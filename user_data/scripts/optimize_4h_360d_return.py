#!/usr/bin/env python3

import json
import random
import sys
import argparse
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
    load_default_parameters,
    load_4h_frames,
    period_returns,
    simulate,
)


INITIAL_BALANCE = 1000.0
BROAD_SAMPLES = 800
LOCAL_SAMPLES = 1000
SEED = 20260818
MIN_TRADES = 80
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN_PCT = 45.0
DEFAULT_OUTPUT_PATH = (
    ROOT_DIR / "user_data/backtest_results/optimization_4h_return.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Optimize the single-slot 4h strategy by total return."
    )
    parser.add_argument("--days", type=int, default=360)
    parser.add_argument("--broad-samples", type=int, default=BROAD_SAMPLES)
    parser.add_argument("--local-samples", type=int, default=LOCAL_SAMPLES)
    parser.add_argument("--initial-balance", type=float, default=INITIAL_BALANCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def random_parameters(rng: random.Random) -> StrategyParameters:
    rsi_min = rng.choice((30.0, 34.0, 38.0, 42.0, 46.0, 50.0, 54.0))
    rsi_max = rng.choice(
        tuple(value for value in (56.0, 60.0, 64.0, 68.0, 72.0) if value > rsi_min)
    )
    atr_min = rng.choice((0.3, 0.5, 0.7, 1.0))
    atr_max = rng.choice(
        tuple(value for value in (4.0, 6.0, 8.0, 10.0) if value > atr_min)
    )
    return StrategyParameters(
        mode=rng.choice(("pullback", "pullback", "breakout")),
        adx_min=rng.choice((8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 22.0, 26.0)),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=rng.choice((0.5, 0.6, 0.8, 1.0, 1.2, 1.4)),
        touch_pct=rng.choice((0.25, 0.5, 1.0, 1.5, 2.0, 2.5)),
        breakout_bars=rng.choice((4, 6, 8, 12, 16, 20, 24)),
        market_filter=rng.choice((True, True, True, False)),
        stop_pct=rng.choice((4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)),
        reward_risk=rng.choice(
            (0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 2.0, 2.2, 2.5)
        ),
        break_even_r=rng.choice((0.2, 0.4, 0.6, 0.8, 1.0)),
        max_hold_bars=rng.choice((6, 12, 18, 24, 30, 36, 42, 48)),
        max_open_trades=1,
        ma7_exit_threshold_pct=rng.choice(
            (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
        ),
        chandelier_exit_enabled=False,
        partial_take_profit_enabled=False,
        ema20_slope_min=rng.choice((-0.1, 0.0, 0.1, 0.2)),
        atr_pct_min=atr_min,
        atr_pct_max=atr_max,
        market_adx_min=rng.choice((10.0, 12.0, 14.0, 16.0, 18.0, 20.0)),
    )


def mutate_parameters(
    rng: random.Random,
    seed: StrategyParameters,
) -> StrategyParameters:
    rsi_min = rng.choice(
        tuple(
            value
            for value in (seed.rsi_min - 4, seed.rsi_min, seed.rsi_min + 4)
            if 26.0 <= value <= 54.0
        )
    )
    rsi_max = rng.choice(
        tuple(
            value
            for value in (seed.rsi_max - 4, seed.rsi_max, seed.rsi_max + 4)
            if value > rsi_min and value <= 76.0
        )
    )
    return replace(
        seed,
        adx_min=max(6.0, seed.adx_min + rng.choice((-2.0, 0.0, 2.0))),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=max(0.4, seed.volume_factor + rng.choice((-0.2, 0.0, 0.2))),
        touch_pct=max(0.1, seed.touch_pct + rng.choice((-0.5, 0.0, 0.5))),
        stop_pct=max(3.0, seed.stop_pct + rng.choice((-1.0, 0.0, 1.0))),
        reward_risk=max(0.5, seed.reward_risk + rng.choice((-0.2, 0.0, 0.2))),
        break_even_r=max(0.1, seed.break_even_r + rng.choice((-0.2, 0.0, 0.2))),
        max_hold_bars=max(6, seed.max_hold_bars + rng.choice((-6, 0, 6))),
        max_open_trades=1,
        ma7_exit_threshold_pct=max(
            0.0, seed.ma7_exit_threshold_pct + rng.choice((-0.5, 0.0, 0.5))
        ),
        market_adx_min=max(
            8.0, seed.market_adx_min + rng.choice((-2.0, 0.0, 2.0))
        ),
    )


def evaluate(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> dict[str, Any]:
    result = simulate(frames, parameters, start, end, initial_balance)
    return {"parameters": asdict(parameters), "summary": result["summary"]}


def qualified(item: dict[str, Any]) -> bool:
    summary = item["summary"]
    return bool(
        summary["trade_count"] >= MIN_TRADES
        and (summary["profit_factor"] or 0.0) >= MIN_PROFIT_FACTOR
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
    )


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -(summary["profit_factor"] or 0.0),
        -summary["win_rate_pct"],
    )


def segment_summaries(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    days: int,
) -> list[dict[str, Any]]:
    results = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + pd.Timedelta(days=days), end)
        results.append(
            simulate(frames, parameters, cursor, window_end, INITIAL_BALANCE)[
                "summary"
            ]
        )
        cursor = window_end
    return results


def main() -> int:
    args = parse_args()
    frames, start, end = load_4h_frames(args.days)
    if not frames:
        raise RuntimeError("No 4h frames available")
    print(f"Loaded {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}")

    rng = random.Random(SEED)
    baseline = load_default_parameters()
    results = [evaluate(frames, baseline, start, end, args.initial_balance)]
    seen = {tuple(asdict(baseline).values())}
    while len(results) < args.broad_samples:
        parameters = random_parameters(rng)
        key = tuple(asdict(parameters).values())
        if key in seen:
            continue
        seen.add(key)
        results.append(
            evaluate(frames, parameters, start, end, args.initial_balance)
        )
        if len(results) % 100 == 0:
            print(
                f"Broad search: {len(results)}/{args.broad_samples}",
                flush=True,
            )

    seeds = sorted(
        (item for item in results if qualified(item)),
        key=rank,
    )[:5]
    if not seeds:
        raise RuntimeError("No broad candidate satisfied basic quality constraints")
    local = []
    while len(local) < args.local_samples:
        seed = StrategyParameters(**rng.choice(seeds)["parameters"])
        parameters = mutate_parameters(rng, seed)
        key = tuple(asdict(parameters).values())
        if key in seen:
            continue
        seen.add(key)
        local.append(
            evaluate(frames, parameters, start, end, args.initial_balance)
        )
        if len(local) % 100 == 0:
            print(
                f"Local search: {len(local)}/{args.local_samples}",
                flush=True,
            )

    candidates = sorted(
        (item for item in results + local if qualified(item)),
        key=rank,
    )
    if not candidates:
        raise RuntimeError("No candidate satisfied basic quality constraints")
    selected = StrategyParameters(**candidates[0]["parameters"])
    full = simulate(frames, selected, start, end, args.initial_balance)
    payload = {
        "data": {
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": args.days,
            "fee_rate": FEE_RATE,
            "broad_samples": len(results),
            "local_samples": len(local),
            "minimum_trades": MIN_TRADES,
            "minimum_profit_factor": MIN_PROFIT_FACTOR,
            "maximum_drawdown_pct": MAX_DRAWDOWN_PCT,
        },
        "selected": full,
        "selected_180d_windows": segment_summaries(
            frames, selected, start, end, 180
        ),
        "selected_90d_windows": segment_summaries(frames, selected, start, end, 90),
        "selected_monthly_returns": period_returns(full, 30),
        "candidates": candidates[:30],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "parameters": asdict(selected),
                "summary": full["summary"],
                "qualified_candidates": len(candidates),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
