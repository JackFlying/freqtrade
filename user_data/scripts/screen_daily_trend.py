#!/usr/bin/env python3

import argparse
import asyncio
import fcntl
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ccxt.async_support as ccxt
import pandas as pd

from freqtrade.configuration.load_config import load_config_file


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT_DIR / "user_data/config_scan.json"
DAY_MS = 24 * 60 * 60 * 1000
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
OUTPUT_COLUMNS = [
    "rank",
    "symbol",
    "pair",
    "trend_candidate",
    "is_provisional_daily_candle",
    "price",
    "ma_7",
    "ma_20",
    "ma_99",
    "four_hour_alignment",
    "change_20d",
    "drawdown_20d",
    "quote_volume_24h",
    "completed_daily_candles",
    "daily_candle_time",
    "updated_at",
    "tradingview_url",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Screen liquid Binance USDT spot pairs with provisional daily MA data."
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Config path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--refresh-universe",
        action="store_true",
        help="Refresh the 24h quote-volume universe immediately.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously using scanner.daily_trend.update_interval_seconds.",
    )
    parser.add_argument(
        "--settings-file",
        type=Path,
        help="Use runtime settings from this file instead of the live settings file.",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        help="Write scan results to this directory instead of the configured directory.",
    )
    return parser.parse_args()


def matches_any(pair: str, patterns: list[str]) -> bool:
    return any(re.fullmatch(pattern, pair) for pattern in patterns)


def atomic_write_json(path: Path, payload: Any) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def atomic_write_csv(path: Path, dataframe: pd.DataFrame) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    dataframe.to_csv(temp_path, index=False)
    temp_path.replace(path)


def load_runtime_settings(
    scanner_config: dict[str, Any],
    settings_file: Path | None = None,
) -> dict[str, float | bool | int]:
    settings_path = (
        settings_file.resolve()
        if settings_file
        else ROOT_DIR / scanner_config["cache_directory"] / "runtime_settings.json"
    )
    defaults = {
        "min_change_20d": 5.0,
        "max_change_20d": 100.0,
        "max_drawdown_20d": 12.0,
        "lookback_days": 20,
        "use_4h_ma_filter": False,
        "ma7_exit_threshold_pct": 2.0,
        "hard_stoploss_pct": 6.0,
        "entry_enabled": True,
    }
    if not settings_path.exists():
        return defaults
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        return {
            "min_change_20d": float(
                payload.get("min_change_20d", defaults["min_change_20d"])
            ),
            "max_change_20d": float(
                payload.get("max_change_20d", defaults["max_change_20d"])
            ),
            "max_drawdown_20d": float(
                payload.get("max_drawdown_20d", defaults["max_drawdown_20d"])
            ),
            "lookback_days": int(
                payload.get("lookback_days", defaults["lookback_days"])
            ),
            "use_4h_ma_filter": bool(
                payload.get(
                    "use_4h_ma_filter",
                    defaults["use_4h_ma_filter"],
                )
            ),
            "ma7_exit_threshold_pct": float(
                payload.get(
                    "ma7_exit_threshold_pct",
                    defaults["ma7_exit_threshold_pct"],
                )
            ),
            "hard_stoploss_pct": float(
                payload.get(
                    "hard_stoploss_pct",
                    defaults["hard_stoploss_pct"],
                )
            ),
            "entry_enabled": bool(
                payload.get("entry_enabled", defaults["entry_enabled"])
            ),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return defaults


def cache_path(cache_directory: Path, market_id: str) -> Path:
    safe_market_id = re.sub(r"[^A-Za-z0-9_.-]", "_", market_id)
    return cache_directory / f"{safe_market_id}-1d.feather"


def read_cached_candles(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    dataframe = pd.read_feather(path)
    dataframe["date"] = pd.to_datetime(dataframe["date"], utc=True)
    return dataframe[OHLCV_COLUMNS]


def write_cached_candles(path: Path, dataframe: pd.DataFrame) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    dataframe.reset_index(drop=True).to_feather(temp_path)
    temp_path.replace(path)


def cached_universe(
    path: Path,
    refresh_seconds: int,
    force_refresh: bool,
) -> list[dict[str, Any]] | None:
    if force_refresh or not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(payload["generated_at"])
        age = datetime.now(timezone.utc) - generated_at
        if age.total_seconds() >= refresh_seconds:
            return None
        return payload["pairs"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def refresh_universe(
    exchange: Any,
    config: dict[str, Any],
    scanner_config: dict[str, Any],
    universe_path: Path,
) -> list[dict[str, Any]]:
    tickers = await async_call_with_retry(exchange.fetch_tickers)
    whitelist = config["exchange"]["pair_whitelist"]
    blacklist = config["exchange"]["pair_blacklist"]
    stablecoin_bases = set(scanner_config.get("stablecoin_bases", []))
    minimum_quote_volume = float(scanner_config["minimum_quote_volume"])

    pairs = []
    for market in exchange.markets.values():
        pair = market.get("symbol", "")
        if not (
            market.get("spot") is True
            and market.get("active") is not False
            and market.get("quote") == config["stake_currency"]
            and pair
            and matches_any(pair, whitelist)
            and not matches_any(pair, blacklist)
            and market.get("base") not in stablecoin_bases
        ):
            continue

        quote_volume = float(tickers.get(pair, {}).get("quoteVolume") or 0)
        if quote_volume < minimum_quote_volume:
            continue

        pairs.append(
            {
                "pair": pair,
                "market_id": market["id"],
                "symbol": f"BINANCE:{market['id']}",
                "quote_volume_24h": quote_volume,
            }
        )

    pairs.sort(key=lambda item: item["quote_volume_24h"], reverse=True)
    atomic_write_json(
        universe_path,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "minimum_quote_volume": minimum_quote_volume,
            "pairs": pairs,
        },
    )
    return pairs


async def async_call_with_retry(call: Any, *args: Any, **kwargs: Any) -> Any:
    for attempt in range(3):
        try:
            return await call(*args, **kwargs)
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("Retry loop exited unexpectedly")


async def fetch_ohlcv_with_retry(
    exchange: Any,
    pair: str,
    timeframe: str,
    since: int | None,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> list[list[float]]:
    for attempt in range(3):
        try:
            async with semaphore:
                return await exchange.fetch_ohlcv(
                    pair,
                    timeframe=timeframe,
                    since=since,
                    limit=limit,
                )
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
    return []


async def has_four_hour_ma_alignment(
    exchange: Any,
    pair: str,
    semaphore: asyncio.Semaphore,
) -> bool:
    rows = await fetch_ohlcv_with_retry(
        exchange,
        pair,
        "4h",
        None,
        120,
        semaphore,
    )
    dataframe = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    if len(dataframe) < 99:
        return False

    for period in (7, 20, 99):
        dataframe[f"ma_{period}"] = dataframe["close"].rolling(
            window=period,
            min_periods=period,
        ).mean()
    latest = dataframe.iloc[-1]
    return bool(
        latest["close"] > latest["ma_7"]
        and latest["ma_7"] > latest["ma_20"]
        and latest["ma_20"] > latest["ma_99"]
    )


async def update_pair(
    exchange: Any,
    pair_config: dict[str, Any],
    scanner_config: dict[str, Any],
    cache_directory: Path,
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any] | None, str | None]:
    pair = pair_config["pair"]
    path = cache_path(cache_directory, pair_config["market_id"])
    history_limit = int(scanner_config["history_candle_limit"])

    try:
        cached = read_cached_candles(path)
        since = None
        request_limit = history_limit
        if not cached.empty:
            last_timestamp = int(cached["date"].iloc[-1].timestamp() * 1000)
            since = max(0, last_timestamp - DAY_MS)
            missing_days = max(1, (exchange.milliseconds() - since) // DAY_MS + 2)
            request_limit = min(history_limit, int(missing_days))

        rows = await fetch_ohlcv_with_retry(
            exchange,
            pair,
            "1d",
            since,
            request_limit,
            semaphore,
        )
        fresh = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
        fresh["date"] = pd.to_datetime(fresh["date"], unit="ms", utc=True)
        dataframe = (
            pd.concat([cached, fresh], ignore_index=True)
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .tail(history_limit)
            .reset_index(drop=True)
        )
        write_cached_candles(path, dataframe)

        now = pd.Timestamp.now(tz="UTC")
        completed_count = int(((dataframe["date"] + pd.Timedelta(days=1)) <= now).sum())
        minimum_listing_days = int(scanner_config["minimum_listing_days"])
        if completed_count < minimum_listing_days:
            return None, None

        for period in (7, 20, 99):
            dataframe[f"ma_{period}"] = dataframe["close"].rolling(
                window=period,
                min_periods=period,
            ).mean()

        latest = dataframe.iloc[-1]
        lookback_days = int(scanner_config["lookback_days"])
        if len(dataframe) < lookback_days + 1:
            return None, f"{pair}: insufficient data for {lookback_days}-day lookback"
        change_20d = (
            latest["close"] / dataframe["close"].iloc[-lookback_days - 1] - 1
        ) * 100
        highest_20d = dataframe["high"].tail(lookback_days).max()
        drawdown_20d = (highest_20d - latest["close"]) / highest_20d * 100
        max_drawdown_20d = float(scanner_config["max_drawdown_20d"])
        min_change_20d = float(scanner_config["min_change_20d"])
        max_change_20d = float(scanner_config["max_change_20d"])
        required_values = [
            latest["close"],
            latest["ma_7"],
            latest["ma_20"],
            latest["ma_99"],
            change_20d,
            drawdown_20d,
        ]
        if any(pd.isna(value) for value in required_values):
            return None, f"{pair}: insufficient data for MA99"

        common_candidate = bool(
            change_20d > min_change_20d
            and change_20d < max_change_20d
            and drawdown_20d <= max_drawdown_20d
        )
        daily_alignment = bool(
            latest["close"] > latest["ma_7"]
            and latest["ma_7"] > latest["ma_20"]
            and latest["ma_20"] > latest["ma_99"]
        )
        use_four_hour_filter = bool(scanner_config["use_4h_ma_filter"])
        four_hour_alignment = None
        if common_candidate and use_four_hour_filter:
            four_hour_alignment = await has_four_hour_ma_alignment(
                exchange,
                pair,
                semaphore,
            )
        selected_alignment = (
            four_hour_alignment if use_four_hour_filter else daily_alignment
        )
        trend_candidate = common_candidate and selected_alignment is True
        candle_time = pd.Timestamp(latest["date"])
        return (
            {
                "symbol": pair_config["symbol"],
                "pair": pair,
                "trend_candidate": trend_candidate,
                "is_provisional_daily_candle": bool(
                    candle_time + pd.Timedelta(days=1) > now
                ),
                "price": round(float(latest["close"]), 8),
                "ma_7": round(float(latest["ma_7"]), 8),
                "ma_20": round(float(latest["ma_20"]), 8),
                "ma_99": round(float(latest["ma_99"]), 8),
                "four_hour_alignment": four_hour_alignment,
                "change_20d": round(float(change_20d), 2),
                "drawdown_20d": round(float(drawdown_20d), 2),
                "quote_volume_24h": round(
                    float(pair_config["quote_volume_24h"]), 2
                ),
                "completed_daily_candles": completed_count,
                "daily_candle_time": candle_time.isoformat(),
                "updated_at": now.isoformat(),
                "tradingview_url": (
                    "https://www.tradingview.com/chart/?symbol="
                    + quote(pair_config["symbol"], safe="")
                ),
            },
            None,
        )
    except Exception as exc:
        return None, f"{pair}: {type(exc).__name__}: {exc}"


def write_outputs(
    results: list[dict[str, Any]],
    errors: list[str],
    output_directory: Path,
    universe_size: int,
    min_change_20d: float,
    max_change_20d: float,
    max_drawdown_20d: float,
    lookback_days: int,
    use_4h_ma_filter: bool,
) -> None:
    results.sort(
        key=lambda item: (
            not item["trend_candidate"],
            -item["quote_volume_24h"],
        )
    )
    for rank, result in enumerate(results, start=1):
        result["rank"] = rank

    all_frame = pd.DataFrame(results, columns=OUTPUT_COLUMNS)
    candidates = [result for result in results if result["trend_candidate"]]
    candidate_frame = pd.DataFrame(candidates, columns=OUTPUT_COLUMNS)

    atomic_write_csv(output_directory / "daily_trend_all.csv", all_frame)
    atomic_write_csv(
        output_directory / "daily_trend_candidates.csv",
        candidate_frame,
    )

    watchlist = "\n".join(result["symbol"] for result in candidates)
    watchlist_path = output_directory / "daily_trend_watchlist.txt"
    watchlist_temp = watchlist_path.with_suffix(".txt.tmp")
    watchlist_temp.write_text(f"{watchlist}\n" if watchlist else "", encoding="utf-8")
    watchlist_temp.replace(watchlist_path)

    error_path = output_directory / "daily_trend_errors.log"
    if errors:
        error_temp = error_path.with_suffix(".log.tmp")
        error_temp.write_text("\n".join(errors) + "\n", encoding="utf-8")
        error_temp.replace(error_path)
    else:
        error_path.unlink(missing_ok=True)

    atomic_write_json(
        output_directory / "daily_trend_status.json",
        {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": universe_size,
            "age_eligible_size": len(results),
            "candidate_size": len(candidates),
            "error_count": len(errors),
            "uses_provisional_daily_candle": True,
            "moving_average_type": "SMA",
            "min_change_20d": min_change_20d,
            "max_change_20d": max_change_20d,
            "max_drawdown_20d": max_drawdown_20d,
            "lookback_days": lookback_days,
            "ma_filter_timeframe": "4h" if use_4h_ma_filter else "1d",
        },
    )


async def run_screen(args: argparse.Namespace) -> int:
    config = load_config_file(str(args.config.resolve()))
    scanner_config = config["scanner"]["daily_trend"]
    runtime_settings = load_runtime_settings(scanner_config, args.settings_file)
    scanner_config = {**scanner_config, **runtime_settings}
    cache_directory = ROOT_DIR / scanner_config["cache_directory"]
    output_directory = (
        args.output_directory.resolve()
        if args.output_directory
        else ROOT_DIR / scanner_config["output_directory"]
    )
    cache_directory.mkdir(parents=True, exist_ok=True)
    output_directory.mkdir(parents=True, exist_ok=True)
    universe_path = cache_directory / "universe.json"

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

    try:
        await async_call_with_retry(exchange.load_markets)
        universe = cached_universe(
            universe_path,
            int(scanner_config["universe_refresh_seconds"]),
            args.refresh_universe,
        )
        if universe is None:
            print("Refreshing 24h quote-volume universe...")
            universe = await refresh_universe(
                exchange,
                config,
                scanner_config,
                universe_path,
            )
        else:
            universe = [
                item
                for item in universe
                if item["pair"] in exchange.markets
                and exchange.markets[item["pair"]].get("active") is not False
            ]

        print(f"Updating provisional daily candles for {len(universe)} pairs...")
        semaphore = asyncio.Semaphore(int(scanner_config["concurrency"]))
        tasks = [
            update_pair(
                exchange,
                pair_config,
                scanner_config,
                cache_directory,
                semaphore,
            )
            for pair_config in universe
        ]

        results = []
        errors = []
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            result, error = await task
            if result:
                results.append(result)
            if error:
                errors.append(error)
            if index % 25 == 0 or index == len(tasks):
                print(f"Processed {index}/{len(tasks)}")

        write_outputs(
            results,
            errors,
            output_directory,
            len(universe),
            float(scanner_config["min_change_20d"]),
            float(scanner_config["max_change_20d"]),
            float(scanner_config["max_drawdown_20d"]),
            int(scanner_config["lookback_days"]),
            bool(scanner_config["use_4h_ma_filter"]),
        )
        candidate_count = sum(result["trend_candidate"] for result in results)
        print(
            f"Done: {len(results)} age-eligible, "
            f"{candidate_count} trend candidates, {len(errors)} errors."
        )
        return 0
    finally:
        await exchange.close()


def main() -> int:
    args = parse_args()
    config = load_config_file(str(args.config.resolve()))
    scanner_config = config["scanner"]["daily_trend"]
    cache_directory = ROOT_DIR / scanner_config["cache_directory"]
    cache_directory.mkdir(parents=True, exist_ok=True)

    def run_once() -> int:
        lock_path = cache_directory / "screen.lock"
        lock_file = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            print("Daily trend screen is already running.")
            return 75

        try:
            try:
                return asyncio.run(run_screen(args))
            except KeyboardInterrupt:
                print("Daily trend screen interrupted.", file=sys.stderr)
                return 130
            except Exception as exc:
                print(
                    f"Daily trend screen failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 1
        finally:
            lock_file.close()

    while True:
        result = run_once()
        if not args.loop:
            return result

        interval = int(scanner_config["update_interval_seconds"])
        next_run = ((int(time.time()) // interval) + 1) * interval + 5
        delay = max(1, next_run - int(time.time()))
        print(f"Next update in {delay} seconds.", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
