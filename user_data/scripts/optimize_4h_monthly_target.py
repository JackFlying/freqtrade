#!/usr/bin/env python3

import argparse
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.research_4h_strategy import (  # noqa: E402
    StrategyParameters,
    load_4h_frames,
    period_returns,
    simulate,
)


TARGET_RETURN_PCT = (1.08**6 - 1) * 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search 4h parameters for an 8 percent monthly target."
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--samples", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "user_data/backtest_results/optimization_4h_monthly8.json",
    )
    return parser.parse_args()


def random_parameters(rng: random.Random) -> StrategyParameters:
    mode = rng.choice(("breakout", "pullback"))
    rsi_min = rng.choice((38.0, 42.0, 46.0, 50.0, 54.0))
    rsi_max = rng.choice(
        tuple(value for value in (60.0, 64.0, 68.0, 72.0) if value > rsi_min)
    )
    return StrategyParameters(
        mode=mode,
        adx_min=rng.choice((14.0, 18.0, 22.0, 26.0, 30.0)),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        volume_factor=rng.choice((0.6, 0.8, 1.0, 1.2, 1.4)),
        touch_pct=rng.choice((0.5, 1.0, 1.5, 2.0)),
        breakout_bars=rng.choice((4, 6, 8, 12, 16, 20, 24)),
        market_filter=rng.choice((True, True, False)),
        stop_pct=rng.choice((4.0, 5.0, 6.0, 7.0, 9.0)),
        reward_risk=rng.choice((0.5, 0.7, 0.9, 1.1, 1.3, 1.5)),
        break_even_r=rng.choice((0.4, 0.6, 0.8, 1.0)),
        max_hold_bars=rng.choice((6, 12, 18, 24)),
        max_open_trades=rng.choice((2, 3, 4, 5)),
        ma7_exit_threshold_pct=rng.choice((0.0, 0.5, 1.0, 1.5, 2.0)),
        chandelier_exit_enabled=False,
        partial_take_profit_enabled=False,
    )


def eligible(summary: dict[str, Any]) -> bool:
    return bool(
        summary["trade_count"] >= 40
        and (summary["profit_factor"] or 0) >= 1.2
        and summary["max_drawdown_pct"] <= 20.0
    )


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    target_shortfall = max(0.0, TARGET_RETURN_PCT - summary["total_profit_pct"])
    return (
        target_shortfall,
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -summary["win_rate_pct"],
        -(summary["profit_factor"] or 0),
    )


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    frames, start, end = load_4h_frames(args.days)
    if not frames:
        raise RuntimeError("No 4h frames available")
    print(
        f"Loaded {len(frames)} pairs from {start.isoformat()} to {end.isoformat()}",
        flush=True,
    )

    evaluated = []
    seen = set()
    while len(evaluated) < args.samples:
        parameters = random_parameters(rng)
        key = tuple(asdict(parameters).values())
        if key in seen:
            continue
        seen.add(key)
        result = simulate(
            frames,
            parameters,
            start,
            end,
            args.initial_balance,
        )
        evaluated.append(
            {
                "parameters": asdict(parameters),
                "summary": result["summary"],
            }
        )
        if len(evaluated) % 250 == 0:
            print(f"Evaluated {len(evaluated)}/{args.samples}", flush=True)

    valid = [item for item in evaluated if eligible(item["summary"])]
    valid.sort(key=rank)
    target_hits = [
        item
        for item in valid
        if item["summary"]["total_profit_pct"] >= TARGET_RETURN_PCT
    ]
    chosen = target_hits[0] if target_hits else valid[0]
    parameters = StrategyParameters(**chosen["parameters"])
    full = simulate(frames, parameters, start, end, args.initial_balance)
    payload = {
        "data": {
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": args.days,
            "samples": args.samples,
            "minimum_trades": 40,
            "minimum_profit_factor": 1.2,
            "maximum_drawdown_pct": 20.0,
            "monthly_target_pct": 8.0,
            "target_total_return_pct": TARGET_RETURN_PCT,
            "target_hit_count": len(target_hits),
        },
        "selected": full,
        "selected_weekly_returns": period_returns(full, 7),
        "selected_monthly_returns": period_returns(full, 30),
        "target_hits": target_hits[:30],
        "closest_candidates": valid[:30],
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
                "target_total_return_pct": TARGET_RETURN_PCT,
                "target_hits": len(target_hits),
                "selected": {
                    "parameters": asdict(parameters),
                    "summary": full["summary"],
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
