#!/usr/bin/env python3

import argparse
import asyncio
import csv
import math
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ccxt.async_support as ccxt
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from freqtrade.configuration.load_config import load_config_file
from freqtrade.resolvers import StrategyResolver
from user_data.asset_filters import is_tokenized_stock_pair


DEFAULT_CONFIG = ROOT_DIR / "user_data/config_scan.json"
CSV_FIELDS = [
    "rank",
    "symbol",
    "pair",
    "status",
    "score",
    "price",
    "rsi",
    "volume_ratio",
    "trend_pct",
    "roc_12",
    "quote_volume_24h",
    "candle_time",
    "tradingview_url",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Binance USDT spot pairs with a Freqtrade strategy."
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Scanner config path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Export all analyzed pairs instead of strategy candidates only.",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    config = load_config_file(str(path.resolve()))

    config["user_data_dir"] = ROOT_DIR / "user_data"
    strategy_path = Path(config["strategy_path"])
    if not strategy_path.is_absolute():
        strategy_path = ROOT_DIR / strategy_path
    config["strategy_path"] = strategy_path.resolve()
    return config


def matches_any(pair: str, patterns: list[str]) -> bool:
    return any(re.fullmatch(pattern, pair) for pattern in patterns)


def select_markets(
    markets: dict[str, dict[str, Any]],
    whitelist: list[str],
    blacklist: list[str],
) -> list[dict[str, Any]]:
    selected = []
    for market in markets.values():
        pair = market.get("symbol", "")
        if (
            market.get("spot") is True
            and market.get("active") is not False
            and pair
            and matches_any(pair, whitelist)
            and not matches_any(pair, blacklist)
            and not is_tokenized_stock_pair(pair)
        ):
            selected.append(market)
    return sorted(selected, key=lambda item: item["symbol"])


def completed_candles(
    rows: list[list[float]], timeframe_ms: int, now_ms: int
) -> list[list[float]]:
    while rows and rows[-1][0] + timeframe_ms > now_ms:
        rows.pop()
    return rows


def finite_number(value: Any, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def analyze_rows(
    strategy: Any,
    pair: str,
    tradingview_symbol: str,
    rows: list[list[float]],
    quote_volume: float,
    include_all: bool,
) -> dict[str, Any] | None:
    dataframe = pd.DataFrame(
        rows,
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    dataframe["date"] = pd.to_datetime(dataframe["date"], unit="ms", utc=True)
    analyzed = strategy.analyze_ticker(dataframe, {"pair": pair})
    latest = analyzed.iloc[-1]

    is_entry = int(latest.get("enter_long", 0) or 0) == 1
    is_candidate = int(latest.get("scan_candidate", 0) or 0) == 1
    if not include_all and not (is_entry or is_candidate):
        return None

    return {
        "symbol": tradingview_symbol,
        "pair": pair,
        "status": "ENTRY" if is_entry else ("WATCH" if is_candidate else "OTHER"),
        "score": finite_number(latest.get("scan_score"), 2),
        "price": finite_number(latest.get("close"), 8),
        "rsi": finite_number(latest.get("rsi"), 2),
        "volume_ratio": finite_number(latest.get("volume_ratio"), 2),
        "trend_pct": finite_number(latest.get("trend_pct"), 2),
        "roc_12": finite_number(latest.get("roc_12"), 2),
        "quote_volume_24h": finite_number(quote_volume, 2),
        "candle_time": latest["date"].isoformat(),
        "tradingview_url": (
            "https://www.tradingview.com/chart/?symbol=" + quote(tradingview_symbol, safe="")
        ),
    }


async def fetch_and_analyze(
    exchange: Any,
    strategy: Any,
    market: dict[str, Any],
    ticker: dict[str, Any],
    timeframe: str,
    candle_limit: int,
    minimum_quote_volume: float,
    semaphore: asyncio.Semaphore,
    include_all: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    pair = market["symbol"]
    quote_volume = float(ticker.get("quoteVolume") or 0)
    if quote_volume < minimum_quote_volume:
        return None, None

    try:
        async with semaphore:
            rows = await exchange.fetch_ohlcv(pair, timeframe, limit=candle_limit)
        rows = completed_candles(
            rows,
            exchange.parse_timeframe(timeframe) * 1000,
            exchange.milliseconds(),
        )
        if len(rows) < strategy.startup_candle_count:
            return None, f"{pair}: only {len(rows)} completed candles"

        tradingview_symbol = f"BINANCE:{market['id']}"
        result = analyze_rows(
            strategy,
            pair,
            tradingview_symbol,
            rows,
            quote_volume,
            include_all,
        )
        return result, None
    except Exception as exc:
        return None, f"{pair}: {type(exc).__name__}: {exc}"


def write_outputs(results: list[dict[str, Any]], output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path = output_directory / "binance_spot_15m.csv"
    watchlist_path = output_directory / "tradingview_watchlist.txt"

    csv_tmp = csv_path.with_suffix(".csv.tmp")
    with csv_tmp.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(results)
    csv_tmp.replace(csv_path)

    watchlist_tmp = watchlist_path.with_suffix(".txt.tmp")
    watchlist = "\n".join(row["symbol"] for row in results)
    watchlist_tmp.write_text(f"{watchlist}\n" if watchlist else "", encoding="utf-8")
    watchlist_tmp.replace(watchlist_path)

    print(f"CSV: {csv_path}")
    print(f"TradingView watchlist: {watchlist_path}")


async def run_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    scanner_config = config.get("scanner", {})
    strategy = StrategyResolver.load_strategy(config)

    exchange_class = getattr(ccxt, config["exchange"]["name"])
    ccxt_config = config["exchange"].get("ccxt_async_config", {})
    exchange = exchange_class(
        {
            **ccxt_config,
            "options": {
                **ccxt_config.get("options", {}),
                "defaultType": "spot",
            },
        }
    )

    try:
        print("Loading Binance spot markets...")
        await exchange.load_markets()
        markets = select_markets(
            exchange.markets,
            config["exchange"]["pair_whitelist"],
            config["exchange"]["pair_blacklist"],
        )
        tickers = await exchange.fetch_tickers()
        print(f"Scanning {len(markets)} active spot pairs on {strategy.timeframe}...")

        concurrency = int(scanner_config.get("concurrency", 6))
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [
            fetch_and_analyze(
                exchange=exchange,
                strategy=strategy,
                market=market,
                ticker=tickers.get(market["symbol"], {}),
                timeframe=strategy.timeframe,
                candle_limit=int(scanner_config.get("candle_limit", 250)),
                minimum_quote_volume=float(
                    scanner_config.get("minimum_quote_volume", 0)
                ),
                semaphore=semaphore,
                include_all=args.all,
            )
            for market in markets
        ]

        results = []
        errors = []
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            result, error = await task
            if result:
                results.append(result)
            if error:
                errors.append(error)
            if index % 50 == 0 or index == len(tasks):
                print(f"Processed {index}/{len(tasks)}, matched {len(results)}")

        status_order = {"ENTRY": 0, "WATCH": 1, "OTHER": 2}
        results.sort(
            key=lambda row: (
                status_order[row["status"]],
                -(row["score"] if row["score"] is not None else -1),
            )
        )
        maximum_results = int(scanner_config.get("maximum_results", 100))
        if not args.all:
            results = results[:maximum_results]
        for rank, result in enumerate(results, start=1):
            result["rank"] = rank

        output_directory = Path(
            scanner_config.get("output_directory", "user_data/scan_results")
        )
        if not output_directory.is_absolute():
            output_directory = ROOT_DIR / output_directory
        write_outputs(results, output_directory)

        print(f"Done: {len(results)} exported, {len(errors)} pairs failed.")
        if errors:
            error_log = output_directory / "scan_errors.log"
            error_log.write_text("\n".join(errors) + "\n", encoding="utf-8")
            print(f"Errors: {error_log}")
        else:
            (output_directory / "scan_errors.log").unlink(missing_ok=True)
        return 0
    finally:
        await exchange.close()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_scan(args))
    except KeyboardInterrupt:
        print("Scan interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
