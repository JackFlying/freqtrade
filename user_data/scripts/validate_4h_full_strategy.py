#!/usr/bin/env python3

import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts import research_4h_strategy as strategy  # noqa: E402


SOURCE_PATH = ROOT_DIR / "user_data/backtest_results/optimization_4h_full10.json"
OUTPUT_PATH = ROOT_DIR / "user_data/backtest_results/validation_4h_full10.json"
INITIAL_BALANCE = 1000.0


def run(
    frames: dict[str, pd.DataFrame],
    parameters: strategy.StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    fee_rate: float,
) -> dict[str, Any]:
    strategy.FEE_RATE = fee_rate
    return strategy.simulate(frames, parameters, start, end, INITIAL_BALANCE)


def consecutive_windows(
    frames: dict[str, pd.DataFrame],
    parameters: strategy.StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    days: int,
) -> list[dict[str, Any]]:
    windows = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + pd.Timedelta(days=days), end)
        result = run(frames, parameters, cursor, window_end, 0.001)
        windows.append(result["summary"])
        cursor = window_end
    return windows


def trade_statistics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    profits = [float(trade["profit_pct"]) for trade in trades]
    wins = [profit for profit in profits if profit > 0]
    losses = [profit for profit in profits if profit < 0]
    longest_loss_streak = 0
    current_loss_streak = 0
    for profit in profits:
        if profit < 0:
            current_loss_streak += 1
            longest_loss_streak = max(longest_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0

    by_pair = defaultdict(lambda: {"trades": 0, "profit_abs": 0.0})
    for trade in trades:
        item = by_pair[trade["pair"]]
        item["trades"] += 1
        item["profit_abs"] += float(trade["profit_abs"])
    top_pairs = sorted(
        (
            {"pair": pair, **values}
            for pair, values in by_pair.items()
        ),
        key=lambda item: item["profit_abs"],
        reverse=True,
    )[:10]

    month_returns = defaultdict(list)
    for trade in trades:
        month = pd.Timestamp(trade["exit_time"]).strftime("%Y-%m")
        month_returns[month].append(float(trade["profit_pct"]))

    return {
        "average_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "average_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "longest_consecutive_losses": longest_loss_streak,
        "exit_reason_counts": dict(Counter(trade["exit_reason"] for trade in trades)),
        "top_pairs_by_profit": top_pairs,
        "monthly_trade_profit_pct": {
            month: sum(values) for month, values in sorted(month_returns.items())
        },
    }


def one_at_a_time_neighbors(
    frames: dict[str, pd.DataFrame],
    base: strategy.StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    variants = {
        "adx_min": (12.0, 16.0),
        "volume_factor": (0.6, 1.0),
        "touch_pct": (1.0, 2.0),
        "stop_pct": (6.0, 8.0),
        "reward_risk": (1.1, 1.5),
        "max_hold_bars": (6, 18),
        "max_open_trades": (2,),
        "ma7_exit_threshold_pct": (0.5,),
    }
    results = []
    for field, values in variants.items():
        for value in values:
            parameters = replace(base, **{field: value})
            result = run(frames, parameters, start, end, 0.001)
            results.append(
                {
                    "changed_parameter": field,
                    "value": value,
                    "summary": result["summary"],
                }
            )
    return results


def pair_exclusion_stress_test(
    frames: dict[str, pd.DataFrame],
    parameters: strategy.StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    top_pairs = trade_statistics(trades)["top_pairs_by_profit"][:5]
    results = []
    for item in top_pairs:
        pair = item["pair"]
        filtered_frames = {
            frame_pair: dataframe
            for frame_pair, dataframe in frames.items()
            if frame_pair != pair
        }
        result = run(filtered_frames, parameters, start, end, 0.001)
        results.append({"excluded_pair": pair, "summary": result["summary"]})
    return results


def main() -> int:
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    parameters = strategy.StrategyParameters(**source["selected"]["parameters"])
    frames, start, end = strategy.load_4h_frames(source["data"]["days"])
    if not frames:
        raise RuntimeError("No 4h frames available")

    fee_results = {}
    baseline = None
    for fee_rate in (0.001, 0.0015, 0.002):
        result = run(frames, parameters, start, end, fee_rate)
        fee_results[f"{fee_rate:.4f}"] = result["summary"]
        if fee_rate == 0.001:
            baseline = result
    assert baseline is not None

    strategy.FEE_RATE = 0.001
    payload = {
        "data": {
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "parameters": asdict(parameters),
        },
        "fee_sensitivity": fee_results,
        "consecutive_60d_windows": consecutive_windows(
            frames, parameters, start, end, 60
        ),
        "consecutive_90d_windows": consecutive_windows(
            frames, parameters, start, end, 90
        ),
        "monthly_equity_returns_pct": strategy.period_returns(baseline, 30),
        "trade_statistics": trade_statistics(baseline["trades"]),
        "one_at_a_time_neighbors": one_at_a_time_neighbors(
            frames, parameters, start, end
        ),
        "pair_exclusion_stress_test": pair_exclusion_stress_test(
            frames, parameters, start, end, baseline["trades"]
        ),
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
