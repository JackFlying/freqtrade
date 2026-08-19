#!/usr/bin/env python3

import itertools
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.research_4h_strategy import (  # noqa: E402
    FEE_RATE,
    StrategyParameters,
    entry_signals,
    load_4h_frames,
    load_default_parameters,
    market_regime,
)


DAYS = 360
INITIAL_BALANCE = 1000.0
OUTPUT_PATH = ROOT_DIR / "user_data/backtest_results/regime_switch_4h_360d.json"


@dataclass(frozen=True)
class RangeParameters:
    rsi_entry: float
    bb_std: float
    stop_pct: float
    take_profit_pct: float
    max_hold_bars: int


@dataclass
class Position:
    pair: str
    strategy: str
    entry_time: pd.Timestamp
    entry_price: float
    amount: float
    stake: float
    stop_rate: float
    target_rate: float
    bars_held: int = 0


def prepare_regime_indicators(frames: dict[str, pd.DataFrame]) -> None:
    for dataframe in frames.values():
        mid = dataframe["close"].rolling(20).mean()
        std = dataframe["close"].rolling(20).std()
        dataframe["bb_mid"] = mid
        dataframe["bb_std"] = std


def range_regime(
    frames: dict[str, pd.DataFrame],
    trend_regime: pd.Series,
) -> pd.Series:
    btc = frames["BTC/USDT"]
    weak = (btc["close"] < btc["ema100"]) | (btc["ema20"] < btc["ema50"])
    return (~trend_regime & ~weak).fillna(False)


def range_signals(
    dataframe: pd.DataFrame,
    parameters: RangeParameters,
    allowed: pd.Series,
) -> pd.Series:
    lower_band = dataframe["bb_mid"] - dataframe["bb_std"] * parameters.bb_std
    signal = (
        (dataframe["date"] >= dataframe["eligible_from"])
        & (
            dataframe["quote_volume_24h"]
            >= dataframe["minimum_quote_volume_24h"]
        )
        & allowed.reindex(dataframe.index).fillna(False)
        & (dataframe["close"] <= lower_band)
        & (dataframe["rsi"] <= parameters.rsi_entry)
        & (dataframe["close"] > dataframe["open"])
    )
    return signal.fillna(False)


def simulate(
    frames: dict[str, pd.DataFrame],
    trend_parameters: StrategyParameters,
    range_parameters: RangeParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    prepare_regime_indicators(frames)
    trend_allowed = market_regime(frames, trend_parameters)
    range_allowed = range_regime(frames, trend_allowed)
    events: dict[pd.Timestamp, list[tuple[str, str, float]]] = {}
    for pair, dataframe in frames.items():
        trend = entry_signals(dataframe, trend_parameters, trend_allowed)
        mean_reversion = range_signals(dataframe, range_parameters, range_allowed)
        for timestamp in dataframe.index[trend]:
            row = dataframe.loc[timestamp]
            score = float(row["adx"]) + float(row["volume"] / row["volume_sma20"])
            events.setdefault(timestamp, []).append((pair, "trend", score))
        for timestamp in dataframe.index[mean_reversion]:
            row = dataframe.loc[timestamp]
            distance = float(row["bb_mid"] / row["close"] - 1)
            events.setdefault(timestamp, []).append((pair, "range", distance))
    for candidates in events.values():
        candidates.sort(key=lambda item: item[2], reverse=True)

    cash = INITIAL_BALANCE
    position: Position | None = None
    pending: tuple[str, str] | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    timeline = pd.date_range(start, end, freq="4h", inclusive="left")

    def close(timestamp: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        proceeds = position.amount * price * (1 - FEE_RATE)
        cash += proceeds
        trades.append(
            {
                "pair": position.pair,
                "strategy": position.strategy,
                "entry_time": position.entry_time.isoformat(),
                "exit_time": timestamp.isoformat(),
                "profit_pct": (proceeds / position.stake - 1) * 100,
                "profit_abs": proceeds - position.stake,
                "exit_reason": reason,
                "bars_held": position.bars_held,
            }
        )
        position = None

    for timestamp in timeline:
        if pending is not None and position is None:
            pair, strategy = pending
            dataframe = frames.get(pair)
            if dataframe is not None and timestamp in dataframe.index and cash > 0:
                entry = float(dataframe.loc[timestamp, "open"])
                if strategy == "trend":
                    stop_pct = trend_parameters.stop_pct
                    target_pct = stop_pct * trend_parameters.reward_risk
                else:
                    stop_pct = range_parameters.stop_pct
                    target_pct = range_parameters.take_profit_pct
                amount = cash * (1 - FEE_RATE) / entry
                position = Position(
                    pair=pair,
                    strategy=strategy,
                    entry_time=timestamp,
                    entry_price=entry,
                    amount=amount,
                    stake=cash,
                    stop_rate=entry * (1 - stop_pct / 100),
                    target_rate=entry * (1 + target_pct / 100),
                )
                cash = 0.0
            pending = None

        if position is not None:
            dataframe = frames[position.pair]
            if timestamp in dataframe.index:
                row = dataframe.loc[timestamp]
                position.bars_held += 1
                if float(row["low"]) <= position.stop_rate:
                    close(timestamp, min(float(row["open"]), position.stop_rate), "stop")
                elif float(row["high"]) >= position.target_rate:
                    close(timestamp, max(float(row["open"]), position.target_rate), "target")
                elif position.strategy == "trend" and float(row["close"]) < float(row["ema20"]):
                    close(timestamp, float(row["close"]), "ema20_exit")
                elif position.strategy == "range" and float(row["close"]) >= float(row["bb_mid"]):
                    close(timestamp, float(row["close"]), "bb_mid_exit")
                elif position.strategy == "trend" and position.bars_held >= trend_parameters.max_hold_bars:
                    close(timestamp, float(row["close"]), "time_exit")
                elif (
                    position.strategy == "range"
                    and position.bars_held >= range_parameters.max_hold_bars
                ):
                    close(timestamp, float(row["close"]), "time_exit")

        if position is None:
            candidates = events.get(timestamp, [])
            if candidates:
                pending = (candidates[0][0], candidates[0][1])

        marked = 0.0
        if position is not None:
            dataframe = frames[position.pair]
            if timestamp in dataframe.index:
                marked = position.amount * float(dataframe.loc[timestamp, "close"])
        equity_curve.append({"time": timestamp.isoformat(), "equity": cash + marked})

    if position is not None:
        dataframe = frames[position.pair]
        last = dataframe.loc[dataframe.index < end].iloc[-1]
        close(end, float(last["close"]), "end")

    equities = pd.Series([point["equity"] for point in equity_curve], dtype=float)
    profits = pd.Series([trade["profit_abs"] for trade in trades], dtype=float)
    wins = profits[profits > 0]
    losses = profits[profits < 0]
    drawdown = equities / equities.cummax() - 1
    return {
        "summary": {
            "total_profit_pct": (cash / INITIAL_BALANCE - 1) * 100,
            "ending_balance": cash,
            "max_drawdown_pct": abs(float(drawdown.min())) * 100,
            "trade_count": len(trades),
            "win_rate_pct": float((profits > 0).mean() * 100) if len(profits) else 0,
            "profit_factor": (
                float(wins.sum() / abs(losses.sum()))
                if len(losses) and losses.sum()
                else None
            ),
        },
        "trades": trades,
        "regime_coverage_pct": {
            "trend": float(trend_allowed.mean() * 100),
            "range": float(range_allowed.mean() * 100),
            "weak": float((~trend_allowed & ~range_allowed).mean() * 100),
        },
    }


def rank(item: dict[str, Any]) -> tuple[float, ...]:
    summary = item["summary"]
    return (
        -summary["total_profit_pct"],
        summary["max_drawdown_pct"],
        -(summary["profit_factor"] or 0),
    )


def main() -> int:
    frames, start, end = load_4h_frames(DAYS)
    trend_parameters = load_default_parameters()
    grid = itertools.product(
        (25.0, 30.0, 35.0),
        (1.8, 2.0, 2.2),
        (2.0, 3.0, 4.0),
        (1.5, 2.5, 3.5),
        (3, 6, 9),
    )
    results = []
    for index, values in enumerate(grid, start=1):
        parameters = RangeParameters(*values)
        result = simulate(frames, trend_parameters, parameters, start, end)
        results.append(
            {
                "range_parameters": asdict(parameters),
                "summary": result["summary"],
                "regime_coverage_pct": result["regime_coverage_pct"],
                "strategy_counts": {
                    strategy: sum(
                        trade["strategy"] == strategy for trade in result["trades"]
                    )
                    for strategy in ("trend", "range")
                },
            }
        )
        if index % 50 == 0:
            print(f"Regime search: {index}", flush=True)
    results.sort(key=rank)
    best = results[0]
    full = simulate(
        frames,
        trend_parameters,
        RangeParameters(**best["range_parameters"]),
        start,
        end,
    )
    payload = {
        "data": {
            "days": DAYS,
            "pairs": len(frames),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fee_rate": FEE_RATE,
        },
        "trend_parameters": asdict(trend_parameters),
        "selected_range_parameters": best["range_parameters"],
        "selected": {
            "summary": full["summary"],
            "regime_coverage_pct": full["regime_coverage_pct"],
            "strategy_counts": best["strategy_counts"],
        },
        "top_candidates": results[:20],
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload["selected"], ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
