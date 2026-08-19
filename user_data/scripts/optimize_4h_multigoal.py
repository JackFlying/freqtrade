#!/usr/bin/env python3

import argparse
import itertools
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.research_4h_strategy import (
    StrategyParameters,
    load_4h_frames,
    period_returns,
    simulate,
)


DEFAULT_OUTPUT = (
    ROOT_DIR / "user_data/backtest_results/optimization_4h_180d.json"
)
MIN_TRADES = 40


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-objective 4h strategy optimization."
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def parameter_key(parameters: StrategyParameters) -> tuple[Any, ...]:
    return tuple(asdict(parameters).values())


def compact_result(
    parameters: StrategyParameters,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "parameters": asdict(parameters),
        "summary": result["summary"],
    }


def eligible(item: dict[str, Any]) -> bool:
    summary = item["summary"]
    return bool(
        summary["trade_count"] >= MIN_TRADES
        and summary["total_profit_pct"] > 0
        and (summary["profit_factor"] or 0) >= 1
    )


def strict_key(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        -summary["win_rate_pct"],
        summary["max_drawdown_pct"],
        -summary["total_profit_pct"],
        -(summary["profit_factor"] or 0),
    )


def low_drawdown_key(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        summary["max_drawdown_pct"],
        -summary["win_rate_pct"],
        -summary["total_profit_pct"],
    )


def return_key(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        -summary["total_profit_pct"],
        -summary["win_rate_pct"],
        summary["max_drawdown_pct"],
    )


def deduplicate(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        parameters = StrategyParameters(**item["parameters"])
        key = parameter_key(parameters)
        current = unique.get(key)
        if current is None or strict_key(item) < strict_key(current):
            unique[key] = item
    return list(unique.values())


def select_diverse(
    items: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    valid = [item for item in deduplicate(items) if eligible(item)]
    if not valid:
        return []
    max_win_rate = max(item["summary"]["win_rate_pct"] for item in valid)
    near_win = [
        item
        for item in valid
        if item["summary"]["win_rate_pct"] >= max_win_rate - 4.0
    ]
    ordered = (
        sorted(valid, key=strict_key)[: max(3, limit // 3)]
        + sorted(near_win, key=low_drawdown_key)[: max(3, limit // 3)]
        + sorted(near_win, key=return_key)[: max(2, limit // 3)]
    )
    return deduplicate(ordered)[:limit]


def evaluate(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> dict[str, Any]:
    result = simulate(
        frames,
        parameters,
        start,
        end,
        initial_balance,
    )
    return compact_result(parameters, result)


def entry_parameters() -> Iterable[StrategyParameters]:
    common = itertools.product(
        (18.0, 22.0, 26.0, 30.0),
        (42.0, 46.0, 50.0, 54.0),
        (60.0, 64.0, 68.0, 72.0),
        (0.7, 0.9, 1.1, 1.3),
    )
    common_values = [
        values for values in common if values[1] < values[2]
    ]
    for adx_min, rsi_min, rsi_max, volume_factor in common_values:
        for breakout_bars in (4, 6, 8, 12, 16, 20, 24):
            yield StrategyParameters(
                mode="breakout",
                adx_min=adx_min,
                rsi_min=rsi_min,
                rsi_max=rsi_max,
                volume_factor=volume_factor,
                breakout_bars=breakout_bars,
            )
        for touch_pct in (0.5, 1.0, 1.5, 2.0):
            yield StrategyParameters(
                mode="pullback",
                adx_min=adx_min,
                rsi_min=rsi_min,
                rsi_max=rsi_max,
                volume_factor=volume_factor,
                touch_pct=touch_pct,
            )


def stage_one(
    frames: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    for index, parameters in enumerate(entry_parameters(), start=1):
        results.append(
            evaluate(
                frames,
                parameters,
                start,
                end,
                initial_balance,
            )
        )
        if index % 250 == 0:
            print(f"Stage 1 entry search: {index}", flush=True)
    return results


def stage_two(
    frames: dict[str, pd.DataFrame],
    entry_candidates: list[dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    combinations = itertools.product(
        (4.0, 5.0, 6.0, 7.0),
        (0.5, 0.6, 0.7, 0.8, 1.0),
        (0.5, 0.8, 1.0),
        (6, 12, 18),
    )
    exit_values = list(combinations)
    index = 0
    for candidate in entry_candidates:
        base = StrategyParameters(**candidate["parameters"])
        for stop_pct, reward_risk, break_even_r, max_hold_bars in exit_values:
            parameters = replace(
                base,
                stop_pct=stop_pct,
                reward_risk=reward_risk,
                break_even_r=break_even_r,
                max_hold_bars=max_hold_bars,
            )
            results.append(
                evaluate(
                    frames,
                    parameters,
                    start,
                    end,
                    initial_balance,
                )
            )
            index += 1
            if index % 250 == 0:
                print(f"Stage 2 exit search: {index}", flush=True)
    return results


def stage_three(
    frames: dict[str, pd.DataFrame],
    candidates: list[dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    index = 0
    for candidate in candidates:
        base = StrategyParameters(**candidate["parameters"])
        for ma7_threshold, chandelier, partial, max_open_trades in itertools.product(
            (0.0, 0.5, 1.0, 1.5, 2.0),
            (False, True),
            (False, True),
            (1, 2, 3),
        ):
            parameters = replace(
                base,
                ma7_exit_threshold_pct=ma7_threshold,
                chandelier_exit_enabled=chandelier,
                partial_take_profit_enabled=partial,
                max_open_trades=max_open_trades,
            )
            results.append(
                evaluate(
                    frames,
                    parameters,
                    start,
                    end,
                    initial_balance,
                )
            )
            index += 1
            if index % 250 == 0:
                print(f"Stage 3 protection search: {index}", flush=True)
    return results


def local_values(value: float, step: float, minimum: float) -> tuple[float, ...]:
    return tuple(
        sorted(
            {
                round(max(minimum, value - step), 4),
                round(value, 4),
                round(value + step, 4),
            }
        )
    )


def stage_four(
    frames: dict[str, pd.DataFrame],
    candidates: list[dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    index = 0
    for candidate in candidates:
        base = StrategyParameters(**candidate["parameters"])
        stop_values = local_values(base.stop_pct, 1.0, 2.0)
        reward_values = local_values(base.reward_risk, 0.1, 0.3)
        break_even_values = local_values(base.break_even_r, 0.2, 0.2)
        hold_values = tuple(
            sorted(
                {
                    max(3, base.max_hold_bars - 3),
                    base.max_hold_bars,
                    base.max_hold_bars + 3,
                }
            )
        )
        for stop_pct, reward_risk, break_even_r, max_hold_bars in itertools.product(
            stop_values,
            reward_values,
            break_even_values,
            hold_values,
        ):
            parameters = replace(
                base,
                stop_pct=stop_pct,
                reward_risk=reward_risk,
                break_even_r=break_even_r,
                max_hold_bars=max_hold_bars,
            )
            results.append(
                evaluate(
                    frames,
                    parameters,
                    start,
                    end,
                    initial_balance,
                )
            )
            index += 1
            if index % 250 == 0:
                print(f"Stage 4 local refinement: {index}", flush=True)
    return results


def pareto_frontier(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [item for item in deduplicate(items) if eligible(item)]
    frontier = []
    for item in valid:
        summary = item["summary"]
        dominated = any(
            (
                other["summary"]["win_rate_pct"] >= summary["win_rate_pct"]
                and other["summary"]["max_drawdown_pct"]
                <= summary["max_drawdown_pct"]
                and (
                    other["summary"]["win_rate_pct"] > summary["win_rate_pct"]
                    or other["summary"]["max_drawdown_pct"]
                    < summary["max_drawdown_pct"]
                )
            )
            for other in valid
        )
        if not dominated:
            frontier.append(item)
    return sorted(frontier, key=strict_key)


def choose_recommended(
    items: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    valid = sorted(
        [item for item in deduplicate(items) if eligible(item)],
        key=strict_key,
    )
    if not valid:
        raise RuntimeError("No eligible parameter combination found")
    strict_champion = valid[0]
    max_win_rate = strict_champion["summary"]["win_rate_pct"]
    near_champions = [
        item
        for item in valid
        if item["summary"]["win_rate_pct"] >= max_win_rate - 2.0
    ]
    recommended = sorted(near_champions, key=low_drawdown_key)[0]
    return strict_champion, recommended


def sixty_day_windows(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    windows = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + pd.Timedelta(days=60), end)
        result = simulate(
            frames,
            parameters,
            window_start,
            window_end,
            initial_balance,
        )
        windows.append(result["summary"])
        window_start = window_end
    return windows


def neighbor_results(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    neighbors = []
    changes = {
        "adx_min": local_values(parameters.adx_min, 2.0, 10.0),
        "rsi_max": local_values(parameters.rsi_max, 2.0, parameters.rsi_min + 1),
        "volume_factor": local_values(parameters.volume_factor, 0.2, 0.1),
        "stop_pct": local_values(parameters.stop_pct, 0.5, 1.0),
        "reward_risk": local_values(parameters.reward_risk, 0.1, 0.2),
        "ma7_exit_threshold_pct": local_values(
            parameters.ma7_exit_threshold_pct,
            0.5,
            0.0,
        ),
    }
    if parameters.mode == "breakout":
        windows = (4, 6, 8, 12, 16, 20, 24)
        position = windows.index(parameters.breakout_bars)
        changes["breakout_bars"] = tuple(
            windows[index]
            for index in range(
                max(0, position - 1),
                min(len(windows), position + 2),
            )
        )
    else:
        changes["touch_pct"] = local_values(parameters.touch_pct, 0.5, 0.1)
    for field, values in changes.items():
        for value in values:
            if value == getattr(parameters, field):
                continue
            neighbor = replace(parameters, **{field: value})
            result = evaluate(
                frames,
                neighbor,
                start,
                end,
                initial_balance,
            )
            result["changed_field"] = field
            result["changed_value"] = value
            neighbors.append(result)
    return neighbors


def main() -> int:
    args = parse_args()
    frames, start, end = load_4h_frames(args.days)
    if not frames:
        raise RuntimeError("No 4h frames available")
    print(
        f"Loaded {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}",
        flush=True,
    )

    first = stage_one(frames, start, end, args.initial_balance)
    entry_finalists = select_diverse(first, 8)
    print(f"Stage 1 finalists: {len(entry_finalists)}", flush=True)

    second = stage_two(
        frames,
        entry_finalists,
        start,
        end,
        args.initial_balance,
    )
    exit_finalists = select_diverse(second, 12)
    print(f"Stage 2 finalists: {len(exit_finalists)}", flush=True)

    third = stage_three(
        frames,
        exit_finalists,
        start,
        end,
        args.initial_balance,
    )
    protection_finalists = select_diverse(third, 10)
    print(f"Stage 3 finalists: {len(protection_finalists)}", flush=True)

    fourth = stage_four(
        frames,
        protection_finalists,
        start,
        end,
        args.initial_balance,
    )
    all_results = deduplicate(first + second + third + fourth)
    strict_champion, recommended = choose_recommended(all_results)
    recommended_parameters = StrategyParameters(**recommended["parameters"])
    strict_parameters = StrategyParameters(**strict_champion["parameters"])
    recommended_full = simulate(
        frames,
        recommended_parameters,
        start,
        end,
        args.initial_balance,
    )
    strict_full = simulate(
        frames,
        strict_parameters,
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
            "initial_balance": args.initial_balance,
            "minimum_trades": MIN_TRADES,
            "evaluated_combinations": len(all_results),
            "selection_rule": (
                "Among combinations within 2 percentage points of the highest "
                "win rate, choose the lowest maximum drawdown."
            ),
        },
        "strict_win_rate_champion": strict_full,
        "recommended": recommended_full,
        "recommended_weekly_returns": period_returns(recommended_full, 7),
        "recommended_monthly_returns": period_returns(recommended_full, 30),
        "recommended_60d_windows": sixty_day_windows(
            frames,
            recommended_parameters,
            start,
            end,
            args.initial_balance,
        ),
        "neighbor_results": neighbor_results(
            frames,
            recommended_parameters,
            start,
            end,
            args.initial_balance,
        ),
        "pareto_frontier": pareto_frontier(all_results)[:30],
        "top_win_rate": sorted(
            [item for item in all_results if eligible(item)],
            key=strict_key,
        )[:30],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {args.output}", flush=True)
    print(
        json.dumps(
            {
                "strict": strict_champion,
                "recommended": recommended,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
