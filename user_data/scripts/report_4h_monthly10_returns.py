#!/usr/bin/env python3

import json
import sys
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.research_4h_strategy import (  # noqa: E402
    StrategyParameters,
    load_4h_frames,
    simulate,
)


SOURCE_PATH = ROOT_DIR / "user_data/backtest_results/optimization_4h_360d_monthly10.json"


def main() -> None:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    parameters = StrategyParameters(**payload["selected"]["parameters"])
    frames, start, end = load_4h_frames(360)
    result = simulate(frames, parameters, start, end, 1000.0)
    equity = pd.DataFrame(result["equity_curve"])
    equity["time"] = pd.to_datetime(equity["time"], utc=True)
    equity = equity.set_index("time")["equity"].astype(float)

    previous_equity = 1000.0
    cursor = start + pd.Timedelta(days=30)
    periods = []
    while cursor <= end:
        current_equity = float(equity.asof(cursor))
        periods.append(
            {
                "start": (cursor - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
                "end": cursor.strftime("%Y-%m-%d"),
                "return_pct": round(
                    (current_equity / previous_equity - 1) * 100,
                    2,
                ),
                "ending_equity": round(current_equity, 2),
            }
        )
        previous_equity = current_equity
        cursor += pd.Timedelta(days=30)
    print(json.dumps({"summary": result["summary"], "periods": periods}, ensure_ascii=False))


if __name__ == "__main__":
    main()
