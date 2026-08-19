#!/usr/bin/env python3

import argparse
import asyncio
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import pandas as pd

from freqtrade.configuration.load_config import load_config_file


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT_DIR / "user_data/config_scan.json"
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
FEE_RATE = 0.001
FIFTEEN_MINUTES_MS = 15 * 60 * 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay historical daily-trend selection and trade execution."
    )
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status-path", type=Path, required=True)
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def update_status(path: Path, progress: float, message: str) -> None:
    atomic_write_json(
        path,
        {
            "running": True,
            "progress": round(max(0.0, min(100.0, progress)), 1),
            "message": message,
            "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        },
    )


def safe_market_id(market_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", market_id)


def load_settings(config: dict[str, Any]) -> dict[str, Any]:
    scanner_config = config["scanner"]["daily_trend"]
    settings_path = (
        ROOT_DIR / scanner_config["cache_directory"] / "runtime_settings.json"
    )
    defaults = {
        "min_change_20d": 5.0,
        "max_change_20d": 100.0,
        "max_drawdown_to_gain_ratio_pct": 50.0,
        "lookback_days": 20,
        "use_4h_ma_filter": False,
        "use_ma99_filter": False,
        "ma7_reclaim_enabled": True,
        "ma7_reclaim_tolerance_pct": 1.0,
        "ma7_reclaim_lookback_days": 2,
        "ma7_exit_threshold_pct": 2.0,
        "hard_stoploss_pct": 6.0,
        "peak_drawdown_stop_enabled": False,
        "peak_drawdown_stop_pct": 5.0,
        "dynamic_drawdown_stop_enabled": False,
        "dynamic_drawdown_activation_pct": 3.0,
        "dynamic_max_profit_giveback_pct": 5.0,
        "chandelier_exit_enabled": False,
        "partial_take_profit_enabled": False,
        "no_progress_exit_enabled": False,
        "candidate_reentry_required": False,
        "cooldown_enabled": False,
        "cooldown_hours": 4.0,
    }
    if not settings_path.exists():
        return defaults
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return {key: payload.get(key, value) for key, value in defaults.items()}


def load_universe(config: dict[str, Any]) -> list[dict[str, Any]]:
    scanner_config = config["scanner"]["daily_trend"]
    path = ROOT_DIR / scanner_config["cache_directory"] / "universe.json"
    if not path.exists():
        raise RuntimeError("候选交易池不存在，请先运行一次正式扫描")
    payload = json.loads(path.read_text(encoding="utf-8"))
    pairs = payload.get("pairs", [])
    if not pairs:
        raise RuntimeError("候选交易池为空，请先运行一次正式扫描")
    return pairs


def read_intraday_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    dataframe = pd.read_feather(path)
    dataframe["date"] = pd.to_datetime(dataframe["date"], utc=True)
    for column in OHLCV_COLUMNS[1:]:
        dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")
    return dataframe[OHLCV_COLUMNS]


def write_intraday_cache(path: Path, dataframe: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    dataframe.reset_index(drop=True).to_feather(temp_path)
    temp_path.replace(path)


async def fetch_ohlcv_range(
    exchange: Any,
    pair: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    semaphore: asyncio.Semaphore,
) -> pd.DataFrame:
    since = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[list[float]] = []
    while since < end_ms:
        chunk: list[list[float]] | None = None
        for attempt in range(3):
            try:
                async with semaphore:
                    chunk = await exchange.fetch_ohlcv(
                        pair,
                        timeframe="15m",
                        since=since,
                        limit=1000,
                    )
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2**attempt)
        if not chunk:
            break
        rows.extend(chunk)
        next_since = int(chunk[-1][0]) + FIFTEEN_MINUTES_MS
        if next_since <= since:
            break
        since = next_since
        if len(chunk) < 1000:
            break

    dataframe = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    if dataframe.empty:
        return dataframe
    dataframe["date"] = pd.to_datetime(dataframe["date"], unit="ms", utc=True)
    return dataframe.loc[
        (dataframe["date"] >= start) & (dataframe["date"] < end),
        OHLCV_COLUMNS,
    ]


async def load_pair_candles(
    exchange: Any,
    pair_config: dict[str, Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
    cache_directory: Path,
    semaphore: asyncio.Semaphore,
) -> tuple[str, pd.DataFrame]:
    pair = pair_config["pair"]
    path = cache_directory / f"{safe_market_id(pair_config['market_id'])}-15m.feather"
    cached = read_intraday_cache(path)
    has_range = (
        not cached.empty
        and cached["date"].min() <= start
        and cached["date"].max() >= end - pd.Timedelta(minutes=15)
    )
    if not has_range:
        fresh = await fetch_ohlcv_range(exchange, pair, start, end, semaphore)
        if not fresh.empty:
            cached = (
                pd.concat([cached, fresh], ignore_index=True)
                .drop_duplicates(subset=["date"], keep="last")
                .sort_values("date")
                .reset_index(drop=True)
            )
            cached["date"] = pd.to_datetime(cached["date"], utc=True)
            for column in OHLCV_COLUMNS[1:]:
                cached[column] = pd.to_numeric(cached[column], errors="coerce")
            cached = cached.dropna(subset=OHLCV_COLUMNS).reset_index(drop=True)
            write_intraday_cache(path, cached)
    selected = cached.loc[
        (cached["date"] >= start) & (cached["date"] < end),
        OHLCV_COLUMNS,
    ].copy()
    for column in OHLCV_COLUMNS[1:]:
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
    selected = selected.dropna(subset=OHLCV_COLUMNS).reset_index(drop=True)
    return pair, selected


def read_daily_candles(
    pair_config: dict[str, Any],
    config: dict[str, Any],
    intraday: pd.DataFrame,
) -> pd.DataFrame:
    cache_directory = ROOT_DIR / config["scanner"]["daily_trend"]["cache_directory"]
    path = cache_directory / f"{safe_market_id(pair_config['market_id'])}-1d.feather"
    cached = (
        pd.read_feather(path)
        if path.exists()
        else pd.DataFrame(columns=OHLCV_COLUMNS)
    )
    if not cached.empty:
        cached["date"] = pd.to_datetime(cached["date"], utc=True)

    derived = (
        intraday.set_index("date")
        .resample("1D")
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
        .reset_index()
    )
    return (
        pd.concat([cached[OHLCV_COLUMNS], derived], ignore_index=True)
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def add_period_features(
    dataframe: pd.DataFrame,
    history: pd.DataFrame,
    period: str,
    periods: tuple[int, ...],
    prefix: str,
) -> pd.DataFrame:
    result = dataframe.copy()
    result["_period"] = result["date"].dt.floor(period)
    for moving_period in periods:
        result[f"{prefix}_ma_{moving_period}"] = math.nan

    history = history.sort_values("date").reset_index(drop=True)
    for period_start, indexes in result.groupby("_period").groups.items():
        previous = history.loc[history["date"] < period_start]
        closes = previous["close"].to_numpy(dtype=float)
        current_closes = result.loc[indexes, "close"].to_numpy(dtype=float)
        for moving_period in periods:
            if len(closes) < moving_period - 1:
                continue
            prior_sum = closes[-(moving_period - 1) :].sum()
            result.loc[indexes, f"{prefix}_ma_{moving_period}"] = (
                prior_sum + current_closes
            ) / moving_period
    return result.drop(columns="_period")


def add_daily_features(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    settings: dict[str, Any],
) -> pd.DataFrame:
    result = intraday.copy()
    result["day"] = result["date"].dt.floor("1d")
    result["day_high"] = result.groupby("day")["high"].cummax()
    for column in (
        "ma_7",
        "ma_20",
        "ma_99",
        "change",
        "drawdown",
        "chandelier_stop",
    ):
        result[column] = math.nan
    result["ma_99_slope_positive"] = False
    result["ma7_reclaim"] = False
    result["listing_days"] = 0

    lookback_days = int(settings["lookback_days"])
    daily = daily.sort_values("date").reset_index(drop=True)
    previous_close = daily["close"].shift(1)
    true_range = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - previous_close).abs(),
            (daily["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    daily["chandelier_atr_22"] = true_range.rolling(22, min_periods=22).mean()
    daily["chandelier_high_22"] = daily["high"].rolling(
        22,
        min_periods=22,
    ).max()
    daily_reclaim_events: dict[pd.Timestamp, bool] = {}
    for day, indexes in result.groupby("day").groups.items():
        previous = daily.loc[daily["date"] < day]
        closes = previous["close"].to_numpy(dtype=float)
        highs = previous["high"].to_numpy(dtype=float)
        result.loc[indexes, "listing_days"] = len(previous)
        current_closes = result.loc[indexes, "close"].to_numpy(dtype=float)
        current_highs = result.loc[indexes, "day_high"].to_numpy(dtype=float)
        if not previous.empty:
            last_completed = previous.iloc[-1]
            chandelier_atr = float(last_completed["chandelier_atr_22"])
            chandelier_high = float(last_completed["chandelier_high_22"])
            if math.isfinite(chandelier_atr) and math.isfinite(chandelier_high):
                result.loc[indexes, "chandelier_stop"] = (
                    chandelier_high - chandelier_atr * 3.0
                )

        for moving_period in (7, 20):
            if len(closes) >= moving_period - 1:
                prior_sum = closes[-(moving_period - 1) :].sum()
                result.loc[indexes, f"ma_{moving_period}"] = (
                    prior_sum + current_closes
                ) / moving_period
        if len(closes):
            available_previous = min(98, len(closes))
            prior_sum = closes[-available_previous:].sum()
            result.loc[indexes, "ma_99"] = (
                prior_sum + current_closes
            ) / (available_previous + 1)
            previous_ma99 = closes[-min(99, len(closes)) :].mean()
            result.loc[indexes, "ma_99_slope_positive"] = (
                result.loc[indexes, "ma_99"] > previous_ma99
            )
        touched_ma7 = False
        day_reclaim = False
        tolerance_pct = float(settings.get("ma7_reclaim_tolerance_pct", 1.0))
        for index in indexes:
            row = result.loc[index]
            if (
                pd.notna(row["ma_7"])
                and float(row["low"]) <= float(row["ma_7"])
                * (1 + tolerance_pct / 100)
            ):
                touched_ma7 = True
            if (
                touched_ma7
                and pd.notna(row["ma_7"])
                and float(row["close"]) > float(row["ma_7"])
                and float(row["close"]) > float(row["open"])
            ):
                day_reclaim = True
            daily_reclaim_events[day] = day_reclaim

        if len(closes) >= lookback_days:
            reference_close = closes[-lookback_days]
            result.loc[indexes, "change"] = (
                current_closes / reference_close - 1
            ) * 100
        if len(highs) >= lookback_days - 1:
            previous_high = (
                highs[-(lookback_days - 1) :].max()
                if lookback_days > 1
                else float("-inf")
            )
            interval_high = pd.Series(current_highs).clip(lower=previous_high).to_numpy()
            result.loc[indexes, "drawdown"] = (
                (interval_high - current_closes) / interval_high
            ) * 100
    reclaim_enabled = bool(settings.get("ma7_reclaim_enabled", True))
    reclaim_lookback_days = int(settings.get("ma7_reclaim_lookback_days", 2))
    days = sorted(daily_reclaim_events)
    for position, day in enumerate(days):
        recent_days = days[max(0, position - reclaim_lookback_days + 1) : position + 1]
        allowed = reclaim_enabled and any(
            daily_reclaim_events[recent_day] for recent_day in recent_days
        )
        result.loc[result["day"] == day, "ma7_reclaim"] = allowed
    return result


def prepare_pair_data(
    pair_config: dict[str, Any],
    intraday: pd.DataFrame,
    config: dict[str, Any],
    settings: dict[str, Any],
    simulation_start: pd.Timestamp,
) -> pd.DataFrame:
    if intraday.empty:
        return intraday
    daily = read_daily_candles(pair_config, config, intraday)
    dataframe = add_daily_features(intraday, daily, settings)
    dataframe["quote_volume_24h"] = (
        (dataframe["close"] * dataframe["volume"])
        .rolling(96, min_periods=1)
        .sum()
    )

    if bool(settings["use_4h_ma_filter"]):
        four_hour = (
            intraday.set_index("date")
            .resample("4h")
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
            .reset_index()
        )
        dataframe = add_period_features(
            dataframe,
            four_hour,
            "4h",
            (7, 20, 99),
            "four_hour",
        )

    change = dataframe["change"]
    drawdown = dataframe["drawdown"]
    common = (
        (dataframe["listing_days"] >= int(config["scanner"]["daily_trend"]["minimum_listing_days"]))
        & (dataframe["quote_volume_24h"] >= float(config["scanner"]["daily_trend"]["minimum_quote_volume"]))
        & (change > float(settings["min_change_20d"]))
        & (change < float(settings["max_change_20d"]))
        & (change > 0)
        & (
            drawdown
            <= change * float(settings["max_drawdown_to_gain_ratio_pct"]) / 100
        )
    )
    if bool(settings["use_4h_ma_filter"]):
        aligned = (
            (dataframe["close"] > dataframe["four_hour_ma_7"])
            & (dataframe["four_hour_ma_7"] > dataframe["four_hour_ma_20"])
        )
        if bool(settings["use_ma99_filter"]):
            aligned &= dataframe["four_hour_ma_20"] > dataframe["four_hour_ma_99"]
    else:
        aligned = (
            (dataframe["close"] > dataframe["ma_7"])
            & (dataframe["ma_7"] > dataframe["ma_20"])
        )
        if bool(settings["use_ma99_filter"]):
            aligned &= (
                (dataframe["ma_20"] > dataframe["ma_99"])
                & dataframe["ma_99_slope_positive"]
            )
    if bool(settings.get("ma7_reclaim_enabled", True)):
        aligned &= dataframe["ma7_reclaim"]
    dataframe["candidate"] = common & aligned
    dataframe["scan_time"] = dataframe["date"] + pd.Timedelta(minutes=15)
    dataframe["is_scan"] = dataframe["scan_time"].dt.minute.isin((0, 30))
    return dataframe.loc[dataframe["date"] >= simulation_start].reset_index(drop=True)


@dataclass
class Position:
    pair: str
    entry_time: pd.Timestamp
    entry_price: float
    amount: float
    stake: float
    peak_rate: float
    stop_rate: float
    stop_reason: str = "stop_loss"
    partial_take_profit_done: bool = False
    partial_proceeds: float = 0.0
    partial_exit_time: pd.Timestamp | None = None
    partial_exit_price: float | None = None


def dynamic_stop_rate(
    position: Position,
    peak_rate: float,
    settings: dict[str, Any],
) -> float | None:
    if not bool(settings["dynamic_drawdown_stop_enabled"]):
        return None
    peak_profit_pct = max(0.0, (peak_rate / position.entry_price - 1) * 100)
    if peak_profit_pct < float(settings["dynamic_drawdown_activation_pct"]):
        return None
    allowed_giveback_pct = min(
        peak_profit_pct * 0.5,
        float(settings["dynamic_max_profit_giveback_pct"]),
    )
    locked_profit_pct = peak_profit_pct - allowed_giveback_pct
    return position.entry_price * (1 + locked_profit_pct / 100)


def calculate_stop_rate(
    position: Position,
    peak_rate: float,
    settings: dict[str, Any],
    chandelier_stop: float | None = None,
    include_partial_trailing: bool = True,
) -> float:
    return calculate_stop_details(
        position,
        peak_rate,
        settings,
        chandelier_stop,
        include_partial_trailing,
    )[0]


def calculate_stop_details(
    position: Position,
    peak_rate: float,
    settings: dict[str, Any],
    chandelier_stop: float | None = None,
    include_partial_trailing: bool = True,
) -> tuple[float, str]:
    candidates = [
        (position.stop_rate, position.stop_reason),
        (
            position.entry_price
            * (1 - float(settings["hard_stoploss_pct"]) / 100),
            "stop_loss",
        ),
    ]
    if bool(settings["peak_drawdown_stop_enabled"]):
        candidates.append(
            (
                peak_rate
                * (1 - float(settings["peak_drawdown_stop_pct"]) / 100),
                "peak_drawdown",
            )
        )
    dynamic_rate = dynamic_stop_rate(position, peak_rate, settings)
    if dynamic_rate is not None:
        candidates.append((dynamic_rate, "dynamic_peak_drawdown"))
    if (
        bool(settings.get("chandelier_exit_enabled", False))
        and chandelier_stop is not None
        and math.isfinite(chandelier_stop)
    ):
        candidates.append((chandelier_stop, "chandelier_exit"))
    if (
        include_partial_trailing
        and bool(settings.get("partial_take_profit_enabled", False))
        and position.partial_take_profit_done
    ):
        candidates.append((peak_rate * 0.95, "partial_trailing_stop"))
    return max(candidates, key=lambda item: item[0])


def simulate(
    pair_frames: dict[str, pd.DataFrame],
    settings: dict[str, Any],
    config: dict[str, Any],
    initial_balance: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    bars = {
        pair: frame.set_index("date", drop=False)
        for pair, frame in pair_frames.items()
        if not frame.empty
    }
    timeline = pd.date_range(
        start=start,
        end=end - pd.Timedelta(minutes=15),
        freq="15min",
        tz="UTC",
    )
    candidate_events: dict[pd.Timestamp, list[str]] = {}
    for pair, frame in pair_frames.items():
        scans = frame.loc[frame["is_scan"] & frame["candidate"]]
        for row in scans.itertuples(index=False):
            candidate_events.setdefault(row.scan_time, []).append(pair)
    for timestamp, pairs in candidate_events.items():
        pairs.sort(
            key=lambda pair: float(
                bars[pair].loc[timestamp - pd.Timedelta(minutes=15), "quote_volume_24h"]
            ),
            reverse=True,
        )

    max_open_trades = int(config.get("max_open_trades", 5))
    minimum_stake_ratio = float(
        config.get("last_stake_amount_min_ratio", 0.1)
    )
    cash = initial_balance
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, pd.Timestamp] = {}
    waiting_for_candidate_absence: set[str] = set()
    candidate_reentry_ready: set[str] = set()
    pending_entries: list[str] = []
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    candidate_pairs_seen: set[str] = set()
    candidate_snapshots = 0

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
        profit_pct = profit_abs / position.stake * 100
        trades.append(
            {
                "pair": position.pair,
                "entry_time": position.entry_time.isoformat(),
                "exit_time": timestamp.isoformat(),
                "entry_price": round(position.entry_price, 10),
                "exit_price": round(price, 10),
                "stake": round(position.stake, 2),
                "entry_amount": round(position.stake, 2),
                "exit_amount": round(proceeds, 2),
                "profit_abs": round(profit_abs, 4),
                "profit_pct": round(profit_pct, 2),
                "peak_profit_pct": round(
                    (position.peak_rate / position.entry_price - 1) * 100,
                    2,
                ),
                "partial_take_profit": position.partial_take_profit_done,
                "partial_exit_time": (
                    position.partial_exit_time.isoformat()
                    if position.partial_exit_time is not None
                    else None
                ),
                "partial_exit_price": (
                    round(position.partial_exit_price, 10)
                    if position.partial_exit_price is not None
                    else None
                ),
                "exit_reason": reason,
            }
        )
        if bool(settings["cooldown_enabled"]):
            cooldown_until[position.pair] = timestamp + pd.Timedelta(
                hours=float(settings["cooldown_hours"])
            )
        if bool(settings["candidate_reentry_required"]):
            waiting_for_candidate_absence.add(position.pair)
            candidate_reentry_ready.discard(position.pair)
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
        # Size the next batch from the last completed candle. This keeps the
        # compounding calculation free of entry-candle future information.
        equity_before_entry = cash
        previous_timestamp = timestamp - pd.Timedelta(minutes=15)
        for pair, position in positions.items():
            frame = bars.get(pair)
            if frame is not None and previous_timestamp in frame.index:
                equity_before_entry += position.amount * float(
                    frame.loc[previous_timestamp, "close"]
                )
        dynamic_stake = equity_before_entry / max_open_trades
        minimum_last_stake = dynamic_stake * minimum_stake_ratio

        for pair in pending_entries:
            if pair in positions or len(positions) >= max_open_trades:
                continue
            frame = bars.get(pair)
            if frame is None or timestamp not in frame.index:
                continue
            if cooldown_until.get(pair, timestamp) > timestamp:
                continue
            available_stake = min(dynamic_stake, cash)
            if available_stake < minimum_last_stake:
                continue
            row = frame.loc[timestamp]
            entry_price = float(row["open"])
            amount = available_stake * (1 - FEE_RATE) / entry_price
            cash -= available_stake
            hard_stop = entry_price * (
                1 - float(settings["hard_stoploss_pct"]) / 100
            )
            positions[pair] = Position(
                pair=pair,
                entry_time=timestamp,
                entry_price=entry_price,
                amount=amount,
                stake=available_stake,
                peak_rate=entry_price,
                stop_rate=hard_stop,
            )
            candidate_reentry_ready.discard(pair)
        pending_entries = []

        for pair, position in list(positions.items()):
            frame = bars.get(pair)
            if frame is None or timestamp not in frame.index:
                continue
            row = frame.loc[timestamp]
            chandelier_stop = (
                float(row["chandelier_stop"])
                if pd.notna(row["chandelier_stop"])
                else None
            )
            previous_stop_rate, previous_stop_reason = calculate_stop_details(
                position,
                position.peak_rate,
                settings,
                chandelier_stop,
            )
            position.stop_rate = previous_stop_rate
            position.stop_reason = previous_stop_reason
            if float(row["low"]) <= previous_stop_rate * (1 + 1e-12):
                exit_price = min(float(row["open"]), previous_stop_rate)
                close_position(
                    position,
                    timestamp,
                    exit_price,
                    previous_stop_reason,
                )
                continue

            partial_was_done = position.partial_take_profit_done
            partial_trigger = position.entry_price * 1.15
            if (
                bool(settings.get("partial_take_profit_enabled", False))
                and not position.partial_take_profit_done
                and float(row["high"]) >= partial_trigger
            ):
                partial_price = max(float(row["open"]), partial_trigger)
                take_partial_profit(position, timestamp, partial_price)

            peak_rate = max(position.peak_rate, float(row["high"]))
            stop_rate, stop_reason = calculate_stop_details(
                position,
                peak_rate,
                settings,
                chandelier_stop,
                include_partial_trailing=partial_was_done,
            )
            position.peak_rate = peak_rate
            position.stop_rate = stop_rate
            position.stop_reason = stop_reason
            if (
                stop_rate > previous_stop_rate
                and float(row["close"]) <= stop_rate * (1 + 1e-12)
            ):
                close_position(position, timestamp, stop_rate, stop_reason)
                continue
            if position.partial_take_profit_done and not partial_was_done:
                position.stop_rate, position.stop_reason = calculate_stop_details(
                    position,
                    peak_rate,
                    settings,
                    chandelier_stop,
                )
            if (
                bool(settings["no_progress_exit_enabled"])
                and timestamp - position.entry_time >= pd.Timedelta(hours=12)
                and (peak_rate / position.entry_price - 1) * 100 < 2.0
            ):
                close_position(
                    position,
                    timestamp,
                    float(row["close"]),
                    "no_progress_12h",
                )
                continue
            threshold = float(settings["ma7_exit_threshold_pct"]) / 100
            ma7 = float(row["ma_7"])
            if math.isfinite(ma7) and float(row["close"]) < ma7 * (1 - threshold):
                close_position(position, timestamp, float(row["close"]), "ma7_exit")

        scan_time = timestamp + pd.Timedelta(minutes=15)
        candidates = candidate_events.get(scan_time, [])
        if (
            bool(settings["candidate_reentry_required"])
            and scan_time.minute in (0, 30)
        ):
            candidate_set = set(candidates)
            became_absent = waiting_for_candidate_absence - candidate_set
            waiting_for_candidate_absence.difference_update(became_absent)
            candidate_reentry_ready.update(became_absent)
        if candidates:
            candidate_snapshots += 1
            candidate_pairs_seen.update(candidates)
            slots = max_open_trades - len(positions)
            if slots > 0:
                pending_entries = [
                    pair
                    for pair in candidates
                    if pair not in positions
                    and pair not in waiting_for_candidate_absence
                    and cooldown_until.get(pair, scan_time) <= scan_time
                ][:slots]

        mark_prices = 0.0
        for pair, position in positions.items():
            frame = bars.get(pair)
            price = (
                float(frame.loc[timestamp, "close"])
                if frame is not None and timestamp in frame.index
                else position.entry_price
            )
            mark_prices += position.amount * price
        equity = cash + mark_prices
        equity_curve.append(
            {
                "time": scan_time.isoformat(),
                "equity": round(equity, 2),
            }
        )

    for position in list(positions.values()):
        frame = bars[position.pair]
        available = frame.loc[frame.index < end]
        if available.empty:
            continue
        last = available.iloc[-1]
        close_position(position, end, float(last["close"]), "end_of_backtest")

    ending_balance = cash
    equities = pd.Series([point["equity"] for point in equity_curve], dtype=float)
    running_max = equities.cummax()
    drawdowns = (equities / running_max - 1) * 100
    wins = [trade for trade in trades if trade["profit_abs"] > 0]
    losses = [trade for trade in trades if trade["profit_abs"] < 0]
    gross_profit = sum(trade["profit_abs"] for trade in wins)
    gross_loss = abs(sum(trade["profit_abs"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else None

    return {
        "summary": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "initial_balance": round(initial_balance, 2),
            "position_sizing": "dynamic_compounding",
            "max_open_trades": max_open_trades,
            "stake_formula": "equity_before_entry / max_open_trades",
            "ending_balance": round(ending_balance, 2),
            "total_profit_abs": round(ending_balance - initial_balance, 2),
            "total_profit_pct": round(
                (ending_balance / initial_balance - 1) * 100,
                2,
            ),
            "max_drawdown_pct": round(abs(float(drawdowns.min())), 2),
            "trade_count": len(trades),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "candidate_pair_count": len(candidate_pairs_seen),
            "candidate_snapshot_count": candidate_snapshots,
        },
        "equity_curve": equity_curve,
        "trades": sorted(trades, key=lambda trade: trade["exit_time"], reverse=True),
    }


async def run(args: argparse.Namespace) -> int:
    config = load_config_file(str(args.config.resolve()))
    settings = load_settings(config)
    universe = load_universe(config)
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    simulation_end = now
    simulation_start = simulation_end - pd.Timedelta(days=args.days)
    warmup_days = 20 if bool(settings["use_4h_ma_filter"]) else 1
    download_start = simulation_start - pd.Timedelta(days=warmup_days)
    cache_directory = ROOT_DIR / "user_data/backtest_cache"
    update_status(args.status_path, 1, "正在初始化历史行情...")

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
    candles: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    try:
        await exchange.load_markets()
        tasks = [
            load_pair_candles(
                exchange,
                pair_config,
                download_start,
                simulation_end,
                cache_directory,
                semaphore,
            )
            for pair_config in universe
            if pair_config["pair"] in exchange.markets
        ]
        total = len(tasks)
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            try:
                pair, dataframe = await task
                if not dataframe.empty:
                    candles[pair] = dataframe
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            if index == total or index % 5 == 0:
                update_status(
                    args.status_path,
                    5 + index / max(total, 1) * 60,
                    f"正在加载历史行情 {index}/{total}",
                )
    finally:
        await exchange.close()

    pair_config_map = {item["pair"]: item for item in universe}
    prepared: dict[str, pd.DataFrame] = {}
    total = len(candles)
    for index, (pair, dataframe) in enumerate(candles.items(), start=1):
        try:
            prepared[pair] = prepare_pair_data(
                pair_config_map[pair],
                dataframe,
                config,
                settings,
                simulation_start,
            )
        except Exception as exc:
            errors.append(f"{pair}: {type(exc).__name__}: {exc}")
        if index == total or index % 10 == 0:
            update_status(
                args.status_path,
                65 + index / max(total, 1) * 20,
                f"正在回放历史选股 {index}/{total}",
            )

    update_status(args.status_path, 88, "正在模拟交易与止损...")
    result = simulate(
        prepared,
        settings,
        config,
        args.initial_balance,
        simulation_start,
        simulation_end,
    )
    result["meta"] = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "days": args.days,
        "scan_timeframe": "30m",
        "execution_timeframe": "15m",
        "universe_size": len(universe),
        "processed_pairs": len(prepared),
        "error_count": len(errors),
        "errors": errors[:20],
        "fee_rate": FEE_RATE,
        "survivorship_bias_notice": "使用当前可交易池回放，不包含历史期间已退市交易对。",
        "quote_volume_notice": "历史 24h 成交额按 15 分钟收盘价乘成交量估算。",
        "settings": settings,
    }
    atomic_write_json(args.output, result)
    atomic_write_json(
        args.status_path,
        {
            "running": False,
            "progress": 100.0,
            "message": "回测完成",
            "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
        },
    )
    return 0


def main() -> int:
    args = parse_args()
    if not 7 <= args.days <= 540:
        raise SystemExit("--days must be between 7 and 540")
    if args.initial_balance <= 0:
        raise SystemExit("--initial-balance must be positive")
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        atomic_write_json(
            args.status_path,
            {
                "running": False,
                "progress": 0.0,
                "message": f"回测失败：{type(exc).__name__}: {exc}",
                "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
