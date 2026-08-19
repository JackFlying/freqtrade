#!/usr/bin/env python3

import argparse
import asyncio
import csv
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

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
    "ma_99_is_temporary",
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
        "entry_enabled": True,
        "candidate_scan_interval_seconds": int(
            scanner_config["update_interval_seconds"]
        ),
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
            "max_drawdown_to_gain_ratio_pct": float(
                payload.get(
                    "max_drawdown_to_gain_ratio_pct",
                    defaults["max_drawdown_to_gain_ratio_pct"],
                )
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
            "use_ma99_filter": bool(
                payload.get(
                    "use_ma99_filter",
                    defaults["use_ma99_filter"],
                )
            ),
            "ma7_reclaim_enabled": bool(
                payload.get(
                    "ma7_reclaim_enabled",
                    defaults["ma7_reclaim_enabled"],
                )
            ),
            "ma7_reclaim_tolerance_pct": max(
                0.1,
                min(
                    5.0,
                    float(
                        payload.get(
                            "ma7_reclaim_tolerance_pct",
                            defaults["ma7_reclaim_tolerance_pct"],
                        )
                    ),
                ),
            ),
            "ma7_reclaim_lookback_days": max(
                1,
                min(
                    5,
                    int(
                        payload.get(
                            "ma7_reclaim_lookback_days",
                            defaults["ma7_reclaim_lookback_days"],
                        )
                    ),
                ),
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
            "peak_drawdown_stop_enabled": bool(
                payload.get(
                    "peak_drawdown_stop_enabled",
                    defaults["peak_drawdown_stop_enabled"],
                )
            ),
            "peak_drawdown_stop_pct": float(
                payload.get(
                    "peak_drawdown_stop_pct",
                    defaults["peak_drawdown_stop_pct"],
                )
            ),
            "entry_enabled": bool(
                payload.get("entry_enabled", defaults["entry_enabled"])
            ),
            "candidate_scan_interval_seconds": max(
                60,
                min(
                    86400,
                    int(
                        payload.get(
                            "candidate_scan_interval_seconds",
                            defaults["candidate_scan_interval_seconds"],
                        )
                    ),
                ),
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


def load_recent_risk_pairs(scanner_config: dict[str, Any]) -> set[str]:
    configured_path = scanner_config.get("risk_flags_file")
    if not configured_path:
        return set()
    path = ROOT_DIR / str(configured_path)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("pairs", {})
        if not isinstance(entries, dict):
            return set()
        now = datetime.now(timezone.utc)
        lookback_days = int(scanner_config.get("risk_flag_lookback_days", 30))
        cutoff = now - pd.Timedelta(days=lookback_days)
        risk_pairs: set[str] = set()
        for pair, entry in entries.items():
            if isinstance(entry, str):
                flagged_at = entry
                expires_at = None
            elif isinstance(entry, dict):
                flagged_at = entry.get("flagged_at")
                expires_at = entry.get("expires_at")
            else:
                continue
            try:
                flagged_time = pd.Timestamp(flagged_at).tz_convert("UTC")
            except (TypeError, ValueError):
                continue
            if flagged_time < cutoff:
                continue
            if expires_at:
                try:
                    if pd.Timestamp(expires_at).tz_convert("UTC") < now:
                        continue
                except (TypeError, ValueError):
                    continue
            risk_pairs.add(str(pair))
        return risk_pairs
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def is_wash_trading_suspect(
    dataframe: pd.DataFrame,
    scanner_config: dict[str, Any],
) -> bool:
    if not bool(scanner_config.get("wash_trading_filter_enabled", False)):
        return False
    if len(dataframe) < 31:
        return False
    daily = dataframe.iloc[:-1].tail(30).copy()
    if len(daily) < 30:
        return False
    daily_quote_volume = daily["close"] * daily["volume"]
    median_volume = float(daily_quote_volume.median())
    minimum_volume = float(
        scanner_config.get("wash_trading_min_daily_quote_volume", 0)
    )
    if median_volume < minimum_volume:
        return False
    range_pct = (daily["high"] - daily["low"]) / daily["open"].replace(0, pd.NA) * 100
    spike_ratio = float(
        scanner_config.get("wash_trading_volume_spike_ratio", 12.0)
    )
    max_range_pct = float(
        scanner_config.get("wash_trading_max_range_pct", 0.6)
    )
    suspicious_days = int(
        ((daily_quote_volume >= median_volume * spike_ratio)
         & (range_pct <= max_range_pct)).sum()
    )
    required_days = int(
        scanner_config.get("wash_trading_min_suspicious_days", 3)
    )
    return suspicious_days >= required_days


def has_ma7_reclaim(
    dataframe: pd.DataFrame,
    tolerance_pct: float,
    lookback_bars: int,
) -> bool:
    if len(dataframe) < 7:
        return False
    start = max(0, len(dataframe) - lookback_bars)
    for touch_index in range(start, len(dataframe)):
        touch = dataframe.iloc[touch_index]
        if pd.isna(touch["ma_7"]) or float(touch["low"]) > float(
            touch["ma_7"]
        ) * (1 + tolerance_pct / 100):
            continue
        for reclaim_index in range(touch_index, len(dataframe)):
            reclaim = dataframe.iloc[reclaim_index]
            if (
                pd.notna(reclaim["ma_7"])
                and float(reclaim["close"]) > float(reclaim["ma_7"])
                and float(reclaim["close"]) > float(reclaim["open"])
            ):
                return True
    return False


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
    recent_risk_pairs = load_recent_risk_pairs(scanner_config)
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
            and pair not in recent_risk_pairs
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
    require_ma99: bool,
    require_ma7_reclaim: bool,
    ma7_reclaim_tolerance_pct: float,
    ma7_reclaim_lookback_bars: int,
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
    aligned = bool(
        latest["close"] > latest["ma_7"]
        and latest["ma_7"] > latest["ma_20"]
    )
    if require_ma99:
        ma99_rising = (
            len(dataframe) >= 2
            and pd.notna(dataframe["ma_99"].iloc[-2])
            and latest["ma_99"] > dataframe["ma_99"].iloc[-2]
        )
        aligned = aligned and bool(
            latest["ma_20"] > latest["ma_99"] and ma99_rising
        )
    if require_ma7_reclaim:
        aligned = aligned and has_ma7_reclaim(
            dataframe,
            ma7_reclaim_tolerance_pct,
            ma7_reclaim_lookback_bars,
        )
    return aligned


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
        if is_wash_trading_suspect(dataframe, scanner_config):
            return None, None

        for period in (7, 20):
            dataframe[f"ma_{period}"] = dataframe["close"].rolling(
                window=period,
                min_periods=period,
            ).mean()
        dataframe["ma_99"] = dataframe["close"].rolling(
            window=99,
            min_periods=1,
        ).mean()

        latest = dataframe.iloc[-1]
        ma99_rising = (
            len(dataframe) >= 2
            and pd.notna(dataframe["ma_99"].iloc[-2])
            and latest["ma_99"] > dataframe["ma_99"].iloc[-2]
        )
        lookback_days = int(scanner_config["lookback_days"])
        if len(dataframe) < lookback_days + 1:
            return None, f"{pair}: insufficient data for {lookback_days}-day lookback"
        change_20d = (
            latest["close"] / dataframe["close"].iloc[-lookback_days - 1] - 1
        ) * 100
        highest_20d = dataframe["high"].tail(lookback_days).max()
        drawdown_20d = (highest_20d - latest["close"]) / highest_20d * 100
        latest_open = float(latest["open"])
        change_today = (
            (float(latest["close"]) / latest_open - 1) * 100
            if latest_open
            else None
        )
        max_drawdown_to_gain_ratio_pct = float(
            scanner_config["max_drawdown_to_gain_ratio_pct"]
        )
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
            and change_20d > 0
            and drawdown_20d
            <= change_20d * max_drawdown_to_gain_ratio_pct / 100
        )
        use_ma99_filter = bool(scanner_config["use_ma99_filter"])
        ma7_reclaim_enabled = bool(scanner_config["ma7_reclaim_enabled"])
        ma7_reclaim_tolerance_pct = float(
            scanner_config["ma7_reclaim_tolerance_pct"]
        )
        ma7_reclaim_lookback_days = int(
            scanner_config["ma7_reclaim_lookback_days"]
        )
        daily_alignment = bool(
            latest["close"] > latest["ma_7"]
            and latest["ma_7"] > latest["ma_20"]
        )
        if ma7_reclaim_enabled:
            daily_alignment = daily_alignment and has_ma7_reclaim(
                dataframe,
                ma7_reclaim_tolerance_pct,
                ma7_reclaim_lookback_days,
            )
        if use_ma99_filter:
            daily_alignment = daily_alignment and bool(
                latest["ma_20"] > latest["ma_99"] and ma99_rising
            )
        use_four_hour_filter = bool(scanner_config["use_4h_ma_filter"])
        four_hour_alignment = None
        if common_candidate and use_four_hour_filter:
            four_hour_alignment = await has_four_hour_ma_alignment(
                exchange,
                pair,
                semaphore,
                use_ma99_filter,
                ma7_reclaim_enabled,
                ma7_reclaim_tolerance_pct,
                ma7_reclaim_lookback_days,
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
                "change_today": (
                    round(change_today, 2) if change_today is not None else None
                ),
                "ma_7": round(float(latest["ma_7"]), 8),
                "ma_20": round(float(latest["ma_20"]), 8),
                "ma_99": round(float(latest["ma_99"]), 8),
                "ma_99_is_temporary": completed_count < 99,
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


def telegram_credentials() -> tuple[str, str] | None:
    # Reuse the same credentials the trade bot loads from /etc/freqtrade.env.
    # The scanner systemd unit sources that file, so these env vars are present
    # on the server; locally they are simply absent and notifications are
    # skipped silently.
    token = os.environ.get("FREQTRADE__TELEGRAM__TOKEN", "").strip()
    chat_id = os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID", "").strip()
    if token and chat_id:
        return token, chat_id
    return None


def read_previous_candidate_pairs(candidates_path: Path) -> set[str] | None:
    # Returns None when no baseline exists yet (first run), so the caller can
    # establish a baseline without sending a noisy "everything is new" alert.
    if not candidates_path.exists():
        return None
    try:
        with candidates_path.open(encoding="utf-8", newline="") as csv_file:
            return {
                row["pair"]
                for row in csv.DictReader(csv_file)
                if row.get("pair")
            }
    except (OSError, KeyError):
        return None


def format_percent(value: Any, signed: bool = True) -> str:
    if value is None:
        return "--"
    number = float(value)
    prefix = "+" if signed and number >= 0 else ""
    return f"{prefix}{number:.2f}%"


def format_quote_volume(value: Any) -> str:
    if value is None:
        return "--"
    number = float(value)
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.2f}K"
    return f"{number:.0f}"


def escape_markdown(text: str) -> str:
    for character in ("_", "*", "`", "["):
        text = text.replace(character, f"\\{character}")
    return text


def format_candidate_line(result: dict[str, Any], is_new: bool = False) -> str:
    pair = escape_markdown(result["pair"])
    price = result.get("price")
    price_text = f"{price:g}" if price is not None else "--"
    marker = "🆕 " if is_new else ""
    return (
        f"{marker}• *{pair}* {price_text} "
        f"(今日 {format_percent(result.get('change_today'))})\n"
        f"  {int(result.get('lookback_days', 0)) or ''}日涨幅 "
        f"{format_percent(result.get('change_20d'))} · "
        f"高点回撤 -{float(result.get('drawdown_20d') or 0):.2f}% · "
        f"24h {format_quote_volume(result.get('quote_volume_24h'))} USDT"
    )


def build_telegram_message(
    candidates: list[dict[str, Any]],
    added_pairs: set[str],
    removed_pairs: list[str],
) -> str:
    # Push the full current candidate list on every change, marking newly added
    # pairs with 🆕 and listing removed pairs separately at the bottom.
    total = len(candidates)
    added_count = len(added_pairs)
    removed_count = len(removed_pairs)
    header = (
        f"📡 *趋势候选更新*（当前 {total} 个"
        f" · 🆕{added_count} 🔻{removed_count}）"
    )
    lines = [header]
    if candidates:
        lines.append("")
        lines.extend(
            format_candidate_line(result, result["pair"] in added_pairs)
            for result in candidates
        )
    else:
        lines.append("")
        lines.append("当前没有满足条件的候选币。")
    if removed_pairs:
        lines.append("")
        lines.append(f"🔻 *已移除 {removed_count}*")
        lines.append(
            "、".join(escape_markdown(pair) for pair in removed_pairs)
        )
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response.read()
    except Exception as exc:  # Notification failures must never break scanning.
        print(f"Telegram notification failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def notify_candidate_changes(
    candidates: list[dict[str, Any]],
    previous_pairs: set[str] | None,
    lookback_days: int,
) -> None:
    credentials = telegram_credentials()
    if credentials is None:
        return
    if previous_pairs is None:
        # First run for this output directory: establish a baseline silently.
        return

    current_pairs = {result["pair"] for result in candidates}
    added_pairs = current_pairs - previous_pairs
    removed_pairs = sorted(previous_pairs - current_pairs)
    if not added_pairs and not removed_pairs:
        return

    # Send the full current list on any change; candidates keep the rank order
    # produced by write_outputs (candidate first, then by 24h volume).
    enriched = [
        {**result, "lookback_days": lookback_days} for result in candidates
    ]
    message = build_telegram_message(enriched, added_pairs, removed_pairs)
    token, chat_id = credentials
    send_telegram_message(token, chat_id, message)


def write_outputs(
    results: list[dict[str, Any]],
    errors: list[str],
    output_directory: Path,
    universe_size: int,
    min_change_20d: float,
    max_change_20d: float,
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
        recent_risk_pairs = load_recent_risk_pairs(scanner_config)
        universe = [
            item for item in universe if item["pair"] not in recent_risk_pairs
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

        # Read the previous candidate set before overwriting the CSV so we can
        # detect additions/removals. Only the live (non-preview) scan notifies;
        # preview scans triggered from the console write to a temp directory.
        is_preview = args.output_directory is not None
        previous_candidate_pairs = (
            None
            if is_preview
            else read_previous_candidate_pairs(
                output_directory / "daily_trend_candidates.csv"
            )
        )

        write_outputs(
            results,
            errors,
            output_directory,
            len(universe),
            float(scanner_config["min_change_20d"]),
            float(scanner_config["max_change_20d"]),
            int(scanner_config["lookback_days"]),
            bool(scanner_config["use_4h_ma_filter"]),
        )
        candidate_count = sum(result["trend_candidate"] for result in results)
        if not is_preview:
            candidates = [
                result for result in results if result["trend_candidate"]
            ]
            notify_candidate_changes(
                candidates,
                previous_candidate_pairs,
                int(scanner_config["lookback_days"]),
            )
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

        # Re-read runtime settings every cycle so the console can change the
        # scan interval without restarting the scanner service. Falls back to
        # the config value when the settings file is missing or malformed.
        runtime_settings = load_runtime_settings(scanner_config, args.settings_file)
        interval = int(runtime_settings["candidate_scan_interval_seconds"])
        next_run = ((int(time.time()) // interval) + 1) * interval + 5
        delay = max(1, next_run - int(time.time()))
        print(f"Next update in {delay} seconds.", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
