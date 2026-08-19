#!/usr/bin/env python3

import argparse
import asyncio
import itertools
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import pandas as pd
import talib.abstract as ta
from freqtrade.configuration.load_config import load_config_file


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from user_data.scripts.backtest_daily_trend import (
    load_pair_candles,
    load_universe as load_backtest_universe,
    update_status,
)


CACHE_DIR = ROOT_DIR / "user_data/backtest_cache"
UNIVERSE_PATH = ROOT_DIR / "user_data/scan_data/daily_trend/universe.json"
DAILY_CACHE_DIR = ROOT_DIR / "user_data/scan_data/daily_trend"
RESEARCH_PARAMETERS_PATH = DAILY_CACHE_DIR / "research_4h_parameters.json"
DEFAULT_CONFIG_PATH = ROOT_DIR / "user_data/config_scan.json"
FEE_RATE = 0.001
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class StrategyParameters:
    mode: str = "breakout"
    adx_min: float = 22.0
    rsi_min: float = 46.0
    rsi_max: float = 64.0
    volume_factor: float = 1.0
    touch_pct: float = 1.0
    breakout_bars: int = 12
    market_filter: bool = True
    stop_pct: float = 7.0
    reward_risk: float = 0.7
    break_even_r: float = 0.8
    max_hold_bars: int = 12
    max_open_trades: int = 2
    ma7_exit_threshold_pct: float = 1.0
    chandelier_exit_enabled: bool = False
    partial_take_profit_enabled: bool = False
    ema20_slope_min: float = 0.0
    atr_pct_min: float = 0.5
    atr_pct_max: float = 8.0
    market_adx_min: float = 16.0


@dataclass
class Position:
    pair: str
    entry_time: pd.Timestamp
    entry_price: float
    amount: float
    stake: float
    stop_rate: float
    target_rate: float
    initial_risk: float
    peak_rate: float
    bars_held: int = 0
    stop_reason: str = "stop_loss"
    partial_take_profit_done: bool = False
    partial_proceeds: float = 0.0
    partial_exit_time: pd.Timestamp | None = None
    partial_exit_price: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research a 4h trend strategy.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--ma7-exit-threshold-pct", type=float)
    parser.add_argument("--stop-pct", type=float)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--chandelier-exit-enabled", action="store_true")
    parser.add_argument("--partial-take-profit-enabled", action="store_true")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Fetch missing 15m candles before running the 4h backtest.",
    )
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/research-4h-strategy.json"),
    )
    return parser.parse_args()


def load_default_parameters() -> StrategyParameters:
    try:
        payload = json.loads(RESEARCH_PARAMETERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return StrategyParameters()
    supported = StrategyParameters.__dataclass_fields__
    return StrategyParameters(
        **{key: value for key, value in payload.items() if key in supported}
    )


def load_universe() -> list[dict[str, Any]]:
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return payload["pairs"]


def historical_cache_universe() -> tuple[list[dict[str, str]], float, int]:
    config = load_config_file(str(DEFAULT_CONFIG_PATH.resolve()))
    scanner_config = config["scanner"]["daily_trend"]
    current_pairs = {
        item["market_id"]: item["pair"] for item in load_universe()
    }
    whitelist = config["exchange"]["pair_whitelist"]
    blacklist = config["exchange"]["pair_blacklist"]
    stablecoin_bases = set(scanner_config.get("stablecoin_bases", []))
    pairs = []
    for path in CACHE_DIR.glob("*-15m.feather"):
        market_id = path.name.removesuffix("-15m.feather")
        if not market_id.endswith("USDT"):
            continue
        pair = current_pairs.get(market_id, f"{market_id[:-4]}/USDT")
        base = pair.split("/", maxsplit=1)[0]
        if (
            not any(re.fullmatch(pattern, pair) for pattern in whitelist)
            or any(re.fullmatch(pattern, pair) for pattern in blacklist)
            or base in stablecoin_bases
        ):
            continue
        pairs.append({"pair": pair, "market_id": market_id})
    return (
        sorted(pairs, key=lambda item: item["pair"]),
        float(scanner_config["minimum_quote_volume"]),
        int(scanner_config["minimum_listing_days"]),
    )


def historical_listing_date(intraday: pd.DataFrame) -> pd.Timestamp:
    # Daily scan caches retain only a rolling window, so their first row is
    # not a listing date. The first locally observed 15m candle is a
    # conservative upper bound for listing and cannot admit a pair too early.
    return pd.to_datetime(intraday["date"], utc=True).min()


async def refresh_intraday_cache(days: int, status_path: Path | None = None) -> None:
    config = load_config_file(str(DEFAULT_CONFIG_PATH.resolve()))
    universe = load_backtest_universe(config)
    end = pd.Timestamp.now(tz="UTC").floor("15min")
    minimum_listing_days = int(
        config["scanner"]["daily_trend"]["minimum_listing_days"]
    )
    start = end - pd.Timedelta(days=days + 35 + minimum_listing_days)
    exchange_class = getattr(ccxt, config["exchange"]["name"])
    ccxt_config = config["exchange"].get("ccxt_async_config", {})
    exchange = exchange_class(
        {
            **ccxt_config,
            "timeout": max(int(ccxt_config.get("timeout", 0)), 30000),
            "options": {
                **ccxt_config.get("options", {}),
                "defaultType": "spot",
            },
        }
    )
    semaphore = asyncio.Semaphore(
        int(config["scanner"]["daily_trend"].get("concurrency", 6))
    )
    if status_path is not None:
        update_status(status_path, 5, f"正在补齐 {days} 天历史行情...")
    try:
        await exchange.load_markets()
        tasks = [
            load_pair_candles(
                exchange,
                item,
                start,
                end,
                CACHE_DIR,
                semaphore,
            )
            for item in universe
            if item["pair"] in exchange.markets
        ]
        total = len(tasks)
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            try:
                await task
            except Exception as exc:
                print(
                    f"Cache refresh skipped a pair: {type(exc).__name__}: {exc}",
                    flush=True,
                )
            if index == total or index % 10 == 0:
                print(f"Cache refresh: {index}/{total}", flush=True)
                if status_path is not None:
                    update_status(
                        status_path,
                        5 + index / max(total, 1) * 65,
                        f"正在补齐历史行情 {index}/{total}",
                    )
    finally:
        await exchange.close()


def load_4h_frames(days: int) -> tuple[dict[str, pd.DataFrame], pd.Timestamp, pd.Timestamp]:
    end = pd.Timestamp.now(tz="UTC").floor("4h")
    requested_start = end - pd.Timedelta(days=days + 1)
    warmup_start = requested_start - pd.Timedelta(days=35)
    universe, minimum_quote_volume, minimum_listing_days = historical_cache_universe()
    frames: dict[str, pd.DataFrame] = {}
    for item in universe:
        path = CACHE_DIR / f"{item['market_id']}-15m.feather"
        if not path.exists():
            continue
        dataframe = pd.read_feather(path)
        dataframe["date"] = pd.to_datetime(dataframe["date"], utc=True)
        listing_date = historical_listing_date(dataframe)
        dataframe = dataframe.loc[
            (dataframe["date"] >= warmup_start)
            & (dataframe["date"] < end),
            OHLCV_COLUMNS,
        ].copy()
        if dataframe.empty:
            continue
        for column in OHLCV_COLUMNS[1:]:
            dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")
        dataframe = dataframe.dropna()
        indexed = dataframe.set_index("date")
        four_hour = (
            indexed
            .resample("4h", label="left", closed="left")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        four_hour["candle_count"] = indexed["close"].resample(
            "4h",
            label="left",
            closed="left",
        ).count()
        four_hour = (
            four_hour.loc[four_hour["candle_count"] == 16]
            .drop(columns="candle_count")
            .reset_index()
        )
        if len(four_hour) < 120:
            continue
        four_hour["ema20"] = ta.EMA(four_hour, timeperiod=20)
        four_hour["ema50"] = ta.EMA(four_hour, timeperiod=50)
        four_hour["ema100"] = ta.EMA(four_hour, timeperiod=100)
        four_hour["ma7"] = four_hour["close"].rolling(7).mean()
        four_hour["rsi"] = ta.RSI(four_hour, timeperiod=14)
        four_hour["adx"] = ta.ADX(four_hour, timeperiod=14)
        four_hour["atr"] = ta.ATR(four_hour, timeperiod=14)
        four_hour["chandelier_atr_22"] = ta.ATR(four_hour, timeperiod=22)
        four_hour["chandelier_high_22"] = (
            four_hour["high"].rolling(22, min_periods=22).max()
        )
        four_hour["volume_sma20"] = four_hour["volume"].rolling(20).mean()
        four_hour["ema20_slope"] = (
            four_hour["ema20"] / four_hour["ema20"].shift(3) - 1
        ) * 100
        four_hour["atr_pct"] = four_hour["atr"] / four_hour["close"] * 100
        four_hour["quote_volume_24h"] = (
            four_hour["volume"] * four_hour["close"]
        ).rolling(6, min_periods=6).sum()
        four_hour["minimum_quote_volume_24h"] = minimum_quote_volume
        four_hour["eligible_from"] = listing_date + pd.Timedelta(
            days=minimum_listing_days
        )
        four_hour["swing_low"] = four_hour["low"].rolling(4).min()
        for window in (4, 6, 8, 12, 16, 20, 24):
            four_hour[f"prior_high_{window}"] = (
                four_hour["high"].shift(1).rolling(window).max()
            )
        frames[item["pair"]] = (
            four_hour.loc[four_hour["date"] >= requested_start]
            .set_index("date", drop=False)
            .copy()
        )
    if not frames:
        return frames, requested_start, end
    reference = frames.get("BTC/USDT")
    if reference is None or reference.empty:
        reference = max(frames.values(), key=len)
    actual_end = min(end, reference.index.max() + pd.Timedelta(hours=4))
    actual_start = actual_end - pd.Timedelta(days=days)
    frames = {
        pair: dataframe.loc[
            (dataframe.index >= actual_start) & (dataframe.index < actual_end)
        ].copy()
        for pair, dataframe in frames.items()
    }
    return frames, actual_start, actual_end


def market_regime(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
) -> pd.Series:
    btc = frames.get("BTC/USDT")
    if btc is None:
        raise RuntimeError("BTC/USDT data is required for the market filter")
    return (
        (btc["close"] > btc["ema100"])
        & (btc["ema20"] > btc["ema50"])
        & (btc["ema20_slope"] > parameters.ema20_slope_min)
        & (btc["adx"] >= parameters.market_adx_min)
    )


def entry_signals(
    dataframe: pd.DataFrame,
    parameters: StrategyParameters,
    regime: pd.Series,
) -> pd.Series:
    trend = (
        (dataframe["close"] > dataframe["ema20"])
        & (dataframe["ema20"] > dataframe["ema50"])
        & (dataframe["ema50"] > dataframe["ema100"])
        & (dataframe["ema20_slope"] > parameters.ema20_slope_min)
    )
    quality = (
        (dataframe["date"] >= dataframe["eligible_from"])
        & (
            dataframe["quote_volume_24h"]
            >= dataframe["minimum_quote_volume_24h"]
        )
        & (dataframe["adx"] >= parameters.adx_min)
        & (dataframe["rsi"] >= parameters.rsi_min)
        & (dataframe["rsi"] <= parameters.rsi_max)
        & (dataframe["atr_pct"] >= parameters.atr_pct_min)
        & (dataframe["atr_pct"] <= parameters.atr_pct_max)
        & (
            dataframe["volume"]
            >= dataframe["volume_sma20"] * parameters.volume_factor
        )
    )
    if parameters.mode == "pullback":
        setup = (
            (
                dataframe["low"].shift(1)
                <= dataframe["ema20"].shift(1)
                * (1 + parameters.touch_pct / 100)
            )
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["close"] > dataframe["high"].shift(1))
        )
    else:
        setup = (
            dataframe["close"]
            > dataframe[f"prior_high_{parameters.breakout_bars}"]
        )
    signal = trend & quality & setup
    if parameters.market_filter:
        signal &= regime.reindex(dataframe.index).fillna(False)
    return signal.fillna(False)


def simulate(
    frames: dict[str, pd.DataFrame],
    parameters: StrategyParameters,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_balance: float = 1000.0,
) -> dict[str, Any]:
    regime = market_regime(frames, parameters)
    signal_events: dict[pd.Timestamp, list[tuple[str, float]]] = {}
    for pair, dataframe in frames.items():
        signals = entry_signals(dataframe, parameters, regime)
        for timestamp in dataframe.index[signals]:
            row = dataframe.loc[timestamp]
            score = float(row["adx"]) + float(
                row["volume"] / row["volume_sma20"]
            )
            signal_events.setdefault(timestamp, []).append((pair, score))
    for candidates in signal_events.values():
        candidates.sort(key=lambda item: item[1], reverse=True)

    cash = initial_balance
    max_open_trades = parameters.max_open_trades
    positions: dict[str, Position] = {}
    pending_entries: list[str] = []
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    timeline = pd.date_range(start=start, end=end, freq="4h", inclusive="left")

    def close_position(
        position: Position,
        timestamp: pd.Timestamp,
        price: float,
        reason: str,
    ) -> None:
        nonlocal cash
        final_proceeds = position.amount * price * (1 - FEE_RATE)
        cash += final_proceeds
        proceeds = position.partial_proceeds + final_proceeds
        profit_abs = proceeds - position.stake
        trades.append(
            {
                "pair": position.pair,
                "entry_time": position.entry_time.isoformat(),
                "exit_time": timestamp.isoformat(),
                "entry_price": position.entry_price,
                "exit_price": price,
                "stake": position.stake,
                "profit_abs": profit_abs,
                "profit_pct": profit_abs / position.stake * 100,
                "peak_profit_pct": (
                    position.peak_rate / position.entry_price - 1
                )
                * 100,
                "bars_held": position.bars_held,
                "partial_take_profit": position.partial_take_profit_done,
                "partial_exit_time": (
                    position.partial_exit_time.isoformat()
                    if position.partial_exit_time is not None
                    else None
                ),
                "partial_exit_price": position.partial_exit_price,
                "exit_reason": reason,
            }
        )
        positions.pop(position.pair, None)

    def take_partial_profit(
        position: Position,
        timestamp: pd.Timestamp,
        price: float,
    ) -> None:
        nonlocal cash
        sold_amount = position.amount * 0.5
        proceeds = sold_amount * price * (1 - FEE_RATE)
        position.amount -= sold_amount
        position.partial_proceeds += proceeds
        position.partial_take_profit_done = True
        position.partial_exit_time = timestamp
        position.partial_exit_price = price
        cash += proceeds

    for timestamp in timeline:
        # Size all entries from the last fully closed 4h candle. This avoids
        # using the entry candle's future high/close while allowing compounding.
        equity_before_entry = cash
        previous_timestamp = timestamp - pd.Timedelta(hours=4)
        for pair, position in positions.items():
            dataframe = frames.get(pair)
            if dataframe is not None and previous_timestamp in dataframe.index:
                equity_before_entry += position.amount * float(
                    dataframe.loc[previous_timestamp, "close"]
                )
        dynamic_stake = equity_before_entry / max_open_trades
        for pair in pending_entries:
            if pair in positions or len(positions) >= max_open_trades:
                continue
            dataframe = frames.get(pair)
            if dataframe is None or timestamp not in dataframe.index:
                continue
            previous_time = timestamp - pd.Timedelta(hours=4)
            if previous_time not in dataframe.index:
                continue
            signal_row = dataframe.loc[previous_time]
            row = dataframe.loc[timestamp]
            stake = min(dynamic_stake, cash)
            if stake < dynamic_stake * 0.1:
                continue
            entry = float(row["open"])
            hard_stop = entry * (1 - parameters.stop_pct / 100)
            swing_stop = float(signal_row["swing_low"]) - float(
                signal_row["atr"]
            ) * 0.25
            stop = max(hard_stop, swing_stop)
            if stop >= entry * 0.995:
                stop = hard_stop
            risk = entry - stop
            if risk <= 0:
                continue
            amount = stake * (1 - FEE_RATE) / entry
            cash -= stake
            positions[pair] = Position(
                pair=pair,
                entry_time=timestamp,
                entry_price=entry,
                amount=amount,
                stake=stake,
                stop_rate=stop,
                target_rate=entry + risk * parameters.reward_risk,
                initial_risk=risk,
                peak_rate=entry,
            )
        pending_entries = []

        for pair, position in list(positions.items()):
            dataframe = frames.get(pair)
            if dataframe is None or timestamp not in dataframe.index:
                continue
            row = dataframe.loc[timestamp]
            position.bars_held += 1
            if float(row["low"]) <= position.stop_rate:
                exit_price = min(float(row["open"]), position.stop_rate)
                close_position(
                    position,
                    timestamp,
                    exit_price,
                    position.stop_reason,
                )
                continue
            if (
                not parameters.partial_take_profit_enabled
                and float(row["high"]) >= position.target_rate
            ):
                exit_price = max(float(row["open"]), position.target_rate)
                close_position(position, timestamp, exit_price, "take_profit")
                continue
            partial_was_done = position.partial_take_profit_done
            partial_trigger = position.entry_price * 1.15
            if (
                parameters.partial_take_profit_enabled
                and not position.partial_take_profit_done
                and float(row["high"]) >= partial_trigger
            ):
                take_partial_profit(
                    position,
                    timestamp,
                    max(float(row["open"]), partial_trigger),
                )
            position.peak_rate = max(position.peak_rate, float(row["high"]))
            if (
                position.peak_rate
                >= position.entry_price
                + position.initial_risk * parameters.break_even_r
            ):
                break_even_stop = position.entry_price * (1 + FEE_RATE * 2)
                if break_even_stop > position.stop_rate:
                    position.stop_rate = break_even_stop
                    position.stop_reason = "break_even_stop"
            if parameters.chandelier_exit_enabled:
                chandelier_atr = float(row["chandelier_atr_22"])
                chandelier_high = float(row["chandelier_high_22"])
                if pd.notna(chandelier_atr) and pd.notna(chandelier_high):
                    chandelier_stop = chandelier_high - chandelier_atr * 3.0
                    if chandelier_stop > position.stop_rate:
                        position.stop_rate = chandelier_stop
                        position.stop_reason = "chandelier_exit"
            if partial_was_done:
                partial_trailing_stop = position.peak_rate * 0.95
                if partial_trailing_stop > position.stop_rate:
                    position.stop_rate = partial_trailing_stop
                    position.stop_reason = "partial_trailing_stop"
            if float(row["close"]) < float(row["ema20"]):
                close_position(position, timestamp, float(row["close"]), "ema20_exit")
                continue
            if float(row["close"]) < float(row["ma7"]) * (
                1 - parameters.ma7_exit_threshold_pct / 100
            ):
                close_position(position, timestamp, float(row["close"]), "ma7_exit")
                continue
            if position.partial_take_profit_done and not partial_was_done:
                partial_trailing_stop = position.peak_rate * 0.95
                if partial_trailing_stop > position.stop_rate:
                    position.stop_rate = partial_trailing_stop
                    position.stop_reason = "partial_trailing_stop"
            if position.bars_held >= parameters.max_hold_bars:
                close_position(position, timestamp, float(row["close"]), "time_exit")

        candidates = signal_events.get(timestamp, [])
        slots = max_open_trades - len(positions)
        if slots > 0:
            pending_entries = [
                pair for pair, _ in candidates if pair not in positions
            ][:slots]

        marked_value = 0.0
        for pair, position in positions.items():
            dataframe = frames[pair]
            price = (
                float(dataframe.loc[timestamp, "close"])
                if timestamp in dataframe.index
                else position.entry_price
            )
            marked_value += position.amount * price
        equity_curve.append(
            {
                "time": (timestamp + pd.Timedelta(hours=4)).isoformat(),
                "equity": cash + marked_value,
            }
        )

    for position in list(positions.values()):
        dataframe = frames[position.pair]
        available = dataframe.loc[dataframe.index < end]
        if available.empty:
            continue
        close_position(
            position,
            end,
            float(available.iloc[-1]["close"]),
            "end_of_backtest",
        )

    profits = pd.Series([trade["profit_abs"] for trade in trades], dtype=float)
    equities = pd.Series(
        [point["equity"] for point in equity_curve],
        dtype=float,
    )
    drawdown = equities / equities.cummax() - 1
    wins = profits[profits > 0]
    losses = profits[profits < 0]
    return {
        "parameters": asdict(parameters),
        "summary": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "initial_balance": initial_balance,
            "ending_balance": cash,
            "total_profit_pct": (cash / initial_balance - 1) * 100,
            "max_drawdown_pct": abs(float(drawdown.min())) * 100,
            "trade_count": len(trades),
            "win_rate_pct": float((profits > 0).mean() * 100) if len(profits) else 0,
            "profit_factor": (
                float(wins.sum() / abs(losses.sum()))
                if len(losses) and losses.sum()
                else None
            ),
            "average_profit_pct": (
                float(pd.Series([trade["profit_pct"] for trade in trades]).mean())
                if trades
                else 0
            ),
        },
        "equity_curve": equity_curve,
        "trades": trades,
    }


def period_returns(result: dict[str, Any], days: int) -> list[float]:
    equity = pd.DataFrame(result["equity_curve"])
    equity["time"] = pd.to_datetime(equity["time"], utc=True)
    equity = equity.set_index("time")["equity"].astype(float)
    start = pd.Timestamp(result["summary"]["start"])
    end = pd.Timestamp(result["summary"]["end"])
    previous = float(result["summary"]["initial_balance"])
    returns = []
    timestamp = start + pd.Timedelta(days=days)
    while timestamp <= end:
        current = float(equity.asof(timestamp))
        returns.append((current / previous - 1) * 100)
        previous = current
        timestamp += pd.Timedelta(days=days)
    return returns


def optimize(
    frames: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    entry_grid = []
    for adx_min, rsi_min, rsi_max, volume_factor, touch_pct in itertools.product(
        (18.0, 22.0, 26.0),
        (46.0, 50.0),
        (64.0, 68.0),
        (0.8, 1.0),
        (0.5, 1.0, 2.0),
    ):
        entry_grid.append(
            (
                "pullback",
                adx_min,
                rsi_min,
                rsi_max,
                volume_factor,
                touch_pct,
                12,
            )
        )
    for adx_min, rsi_min, rsi_max, volume_factor, breakout_bars in itertools.product(
        (18.0, 22.0, 26.0),
        (46.0, 50.0),
        (64.0, 68.0),
        (0.8, 1.0),
        (6, 12, 20),
    ):
        entry_grid.append(
            (
                "breakout",
                adx_min,
                rsi_min,
                rsi_max,
                volume_factor,
                1.0,
                breakout_bars,
            )
        )
    candidates: list[dict[str, Any]] = []
    for index, (
        mode,
        adx_min,
        rsi_min,
        rsi_max,
        volume_factor,
        touch_pct,
        breakout_bars,
    ) in enumerate(entry_grid, start=1):
        if rsi_min >= rsi_max:
            continue
        parameters = StrategyParameters(
            mode=mode,
            adx_min=adx_min,
            rsi_min=rsi_min,
            rsi_max=rsi_max,
            volume_factor=volume_factor,
            touch_pct=touch_pct,
            breakout_bars=breakout_bars,
        )
        result = simulate(frames, parameters, start, end)
        summary = result["summary"]
        if summary["trade_count"] >= 40:
            candidates.append(
                {
                    "parameters": asdict(parameters),
                    "summary": summary,
                }
            )
        if index % 250 == 0:
            print(f"Entry search: {index}", flush=True)

    candidates.sort(
        key=lambda item: (
            item["summary"]["win_rate_pct"],
            item["summary"]["profit_factor"] or 0,
            item["summary"]["total_profit_pct"],
        ),
        reverse=True,
    )
    finalists = candidates[:8]
    results: list[dict[str, Any]] = []
    for finalist in finalists:
        base = finalist["parameters"]
        for stop_pct, reward_risk, break_even_r, max_hold_bars in itertools.product(
            (3.5, 5.0),
            (0.8, 1.0, 1.2),
            (0.6, 0.8),
            (6, 12),
        ):
            parameters = StrategyParameters(
                **{
                    **base,
                    "stop_pct": stop_pct,
                    "reward_risk": reward_risk,
                    "break_even_r": break_even_r,
                    "max_hold_bars": max_hold_bars,
                }
            )
            result = simulate(frames, parameters, start, end)
            weeks = period_returns(result, 7)
            months = period_returns(result, 30)
            summary = result["summary"]
            results.append(
                {
                    "parameters": asdict(parameters),
                    "summary": summary,
                    "weekly_returns": weeks,
                    "monthly_returns": months,
                    "weekly_average": sum(weeks) / len(weeks) if weeks else 0,
                    "weekly_minimum": min(weeks) if weeks else 0,
                    "monthly_average": sum(months) / len(months) if months else 0,
                    "monthly_minimum": min(months) if months else 0,
                }
            )
    results.sort(
        key=lambda item: (
            item["summary"]["win_rate_pct"]
            if (item["summary"]["profit_factor"] or 0) >= 1
            and item["summary"]["trade_count"] >= 40
            else 0,
            item["summary"]["profit_factor"] or 0,
            item["summary"]["total_profit_pct"],
        ),
        reverse=True,
    )
    return results


def main() -> int:
    args = parse_args()
    if args.status_path is not None:
        update_status(args.status_path, 2, "正在准备 4h 回测...")
    if args.refresh_cache:
        asyncio.run(refresh_intraday_cache(args.days, args.status_path))
    if args.status_path is not None:
        update_status(args.status_path, 75, "正在构建 4h K 线并执行回测...")
    frames, start, end = load_4h_frames(args.days)
    if args.optimize:
        results = optimize(frames, start, end)
        payload = {
            "data": {
                "pairs": len(frames),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "results": results,
        }
    else:
        parameters = load_default_parameters()
        overrides: dict[str, Any] = {}
        if args.ma7_exit_threshold_pct is not None:
            overrides["ma7_exit_threshold_pct"] = args.ma7_exit_threshold_pct
        if args.stop_pct is not None:
            overrides["stop_pct"] = args.stop_pct
        if args.chandelier_exit_enabled:
            overrides["chandelier_exit_enabled"] = True
        if args.partial_take_profit_enabled:
            overrides["partial_take_profit_enabled"] = True
        result = simulate(
            frames,
            parameters if not overrides else replace(parameters, **overrides),
            start,
            end,
            args.initial_balance,
        )
        payload = {
            "data": {
                "pairs": len(frames),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "result": result,
        }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
