#!/usr/bin/env python3

import argparse
import itertools
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.optimize_4h_multigoal import (  # noqa: E402
    MIN_TRADES,
    choose_recommended,
    compact_result,
    deduplicate,
    eligible,
    evaluate,
    neighbor_results,
    pareto_frontier,
    select_diverse,
    sixty_day_windows,
    strict_key,
)
from user_data.scripts.research_4h_strategy import (  # noqa: E402
    StrategyParameters,
    load_4h_frames,
    period_returns,
    simulate,
)


DEFAULT_SOURCE = (
    ROOT_DIR / "user_data/backtest_results/optimization_4h_180d.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refine a completed 4h multi-objective optimization."
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_SOURCE)
    return parser.parse_args()


def dense_exit_search(
    frames: dict[str, pd.DataFrame],
    seed: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    for index, (
        stop_pct,
        reward_risk,
        break_even_r,
        max_hold_bars,
    ) in enumerate(
        itertools.product(
            (5.0, 6.0, 6.5, 7.0, 7.5, 8.0, 9.0),
            (0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6),
            (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0),
            (9, 12, 15, 18, 21, 24),
        ),
        start=1,
    ):
        parameters = replace(
            seed,
            stop_pct=stop_pct,
            reward_risk=reward_risk,
            break_even_r=break_even_r,
            max_hold_bars=max_hold_bars,
            chandelier_exit_enabled=False,
            partial_take_profit_enabled=False,
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
        if index % 300 == 0:
            print(f"Dense exit refinement: {index}", flush=True)
    return results


def protection_search(
    frames: dict[str, pd.DataFrame],
    seeds: list[dict[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    index = 0
    for seed in seeds:
        base = StrategyParameters(**seed["parameters"])
        for ma7_threshold, max_open_trades, chandelier, partial in itertools.product(
            (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0),
            (1, 2, 3),
            (False, True),
            (False, True),
        ):
            parameters = replace(
                base,
                ma7_exit_threshold_pct=ma7_threshold,
                max_open_trades=max_open_trades,
                chandelier_exit_enabled=chandelier,
                partial_take_profit_enabled=partial,
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
            if index % 300 == 0:
                print(f"Dense protection refinement: {index}", flush=True)
    return results


def entry_refinement(
    frames: dict[str, pd.DataFrame],
    seed: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float,
) -> list[dict[str, Any]]:
    results = []
    index = 0
    for adx_min, rsi_min, rsi_max, volume_factor in itertools.product(
        (24.0, 25.0, 26.0, 27.0, 28.0),
        (50.0, 52.0, 54.0, 56.0),
        (60.0, 62.0, 64.0, 66.0),
        (1.1, 1.2, 1.3, 1.4, 1.5),
    ):
        if rsi_min >= rsi_max:
            continue
        setup_values = (
            (4, 6, 8)
            if seed.mode == "breakout"
            else (0.5, 1.0, 1.5)
        )
        for setup_value in setup_values:
            changes: dict[str, Any] = {
                "adx_min": adx_min,
                "rsi_min": rsi_min,
                "rsi_max": rsi_max,
                "volume_factor": volume_factor,
            }
            if seed.mode == "breakout":
                changes["breakout_bars"] = int(setup_value)
            else:
                changes["touch_pct"] = float(setup_value)
            parameters = replace(seed, **changes)
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
            if index % 300 == 0:
                print(f"Dense entry refinement: {index}", flush=True)
    return results


def compact_existing(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for key in ("strict_win_rate_champion", "recommended"):
        result = payload.get(key)
        if result:
            items.append(
                {
                    "parameters": result["parameters"],
                    "summary": result["summary"],
                }
            )
    items.extend(payload.get("pareto_frontier", []))
    items.extend(payload.get("top_win_rate", []))
    return items


def main() -> int:
    args = parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    frames, start, end = load_4h_frames(args.days)
    existing = compact_existing(source)
    existing_strict, _ = choose_recommended(existing)
    seed = StrategyParameters(**existing_strict["parameters"])
    print(
        f"Refining {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}",
        flush=True,
    )

    exit_results = dense_exit_search(
        frames,
        seed,
        start,
        end,
        args.initial_balance,
    )
    exit_finalists = select_diverse(exit_results, 12)
    print(f"Dense exit finalists: {len(exit_finalists)}", flush=True)

    protection_results = protection_search(
        frames,
        exit_finalists,
        start,
        end,
        args.initial_balance,
    )
    preliminary = deduplicate(existing + exit_results + protection_results)
    strict_preliminary, recommended_preliminary = choose_recommended(preliminary)
    entry_seed = StrategyParameters(**recommended_preliminary["parameters"])
    print(
        "Preliminary champion: "
        f"{strict_preliminary['summary']['win_rate_pct']:.2f}% win rate",
        flush=True,
    )

    entry_results = entry_refinement(
        frames,
        entry_seed,
        start,
        end,
        args.initial_balance,
    )
    all_results = deduplicate(preliminary + entry_results)
    strict_champion, recommended = choose_recommended(all_results)
    strict_parameters = StrategyParameters(**strict_champion["parameters"])
    recommended_parameters = StrategyParameters(**recommended["parameters"])
    strict_full = simulate(
        frames,
        strict_parameters,
        start,
        end,
        args.initial_balance,
    )
    recommended_full = simulate(
        frames,
        recommended_parameters,
        start,
        end,
        args.initial_balance,
    )
    payload = {
        "data": {
            **source.get("data", {}),
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "minimum_trades": MIN_TRADES,
            "refined_combinations": len(all_results),
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
        "previous_search": {
            "strict": compact_result(
                seed,
                source["strict_win_rate_champion"],
            ),
            "evaluated_combinations": source.get("data", {}).get(
                "evaluated_combinations"
            ),
        },
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {args.output}", flush=True)
    print(
        json.dumps(
            {
                "strict": {
                    "parameters": asdict(strict_parameters),
                    "summary": strict_full["summary"],
                },
                "recommended": {
                    "parameters": asdict(recommended_parameters),
                    "summary": recommended_full["summary"],
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
