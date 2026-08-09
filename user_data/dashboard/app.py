#!/usr/bin/env python3

import asyncio
import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from freqtrade.configuration.load_config import load_config_file
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PATH = ROOT_DIR / "user_data/config_scan.json"
RESULTS_DIR = ROOT_DIR / "user_data/scan_results"
CANDIDATES_PATH = RESULTS_DIR / "daily_trend_candidates.csv"
STATUS_PATH = RESULTS_DIR / "daily_trend_status.json"
ALLOWED_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
MA_PERIODS = (7, 20, 99)

app = FastAPI(title="Freqtrade Candidate Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_response_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_scan_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "return_code": None,
    "message": "尚未手动扫描",
}
_scan_lock = asyncio.Lock()
_daily_open_locks: dict[str, asyncio.Lock] = {}


class DashboardSettings(BaseModel):
    min_change_20d: float = Field(ge=-100, le=10000)
    max_drawdown_20d: float = Field(ge=0, le=100)


def read_config() -> dict[str, Any]:
    return load_config_file(str(CONFIG_PATH))


def parse_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_candidates() -> list[dict[str, Any]]:
    if not CANDIDATES_PATH.exists():
        return []

    candidates = []
    with CANDIDATES_PATH.open(encoding="utf-8", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            candidates.append(
                {
                    "rank": int(row["rank"]),
                    "symbol": row["symbol"],
                    "pair": row["pair"],
                    "price": parse_number(row["price"]),
                    "ma_7": parse_number(row.get("ma_7")),
                    "ma_20": parse_number(row.get("ma_20")),
                    "ma_99": parse_number(row.get("ma_99")),
                    "change_20d": parse_number(row.get("change_20d")),
                    "drawdown_20d": parse_number(row.get("drawdown_20d")),
                    "quote_volume_24h": parse_number(row["quote_volume_24h"]),
                    "completed_daily_candles": int(row["completed_daily_candles"]),
                    "is_provisional_daily_candle": (
                        row["is_provisional_daily_candle"].lower() == "true"
                    ),
                    "updated_at": row["updated_at"],
                }
            )
    return candidates


def read_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {}
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def runtime_settings_path() -> Path:
    config = read_config()
    path = (
        ROOT_DIR
        / config["scanner"]["daily_trend"]["cache_directory"]
        / "runtime_settings.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_runtime_settings() -> dict[str, float]:
    defaults = {
        "min_change_20d": 5.0,
        "max_drawdown_20d": 12.0,
    }
    path = runtime_settings_path()
    if not path.exists():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "min_change_20d": float(
                payload.get("min_change_20d", defaults["min_change_20d"])
            ),
            "max_drawdown_20d": float(
                payload.get("max_drawdown_20d", defaults["max_drawdown_20d"])
            )
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return defaults


def write_runtime_settings(settings: DashboardSettings) -> dict[str, float]:
    path = runtime_settings_path()
    payload = {
        "min_change_20d": settings.min_change_20d,
        "max_drawdown_20d": settings.max_drawdown_20d,
    }
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)
    return payload


def read_universe() -> dict[str, dict[str, Any]]:
    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    universe_path = ROOT_DIR / scanner_config["cache_directory"] / "universe.json"
    if not universe_path.exists():
        return {}
    payload = json.loads(universe_path.read_text(encoding="utf-8"))
    return {item["pair"]: item for item in payload.get("pairs", [])}


def daily_cache_path(pair: str) -> Path | None:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        return None

    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    safe_market_id = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in pair_config["market_id"]
    )
    return (
        ROOT_DIR
        / scanner_config["cache_directory"]
        / f"{safe_market_id}-1d.feather"
    )


def read_daily_candles(pair: str, limit: int) -> pd.DataFrame | None:
    path = daily_cache_path(pair)
    if not path or not path.exists():
        return None

    dataframe = pd.read_feather(path)
    dataframe["date"] = pd.to_datetime(dataframe["date"], utc=True)
    return dataframe[OHLCV_COLUMNS].tail(limit).reset_index(drop=True)


def fetch_binance_candles(pair: str, timeframe: str, limit: int) -> pd.DataFrame:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    public_url = config["exchange"]["ccxt_config"]["urls"]["api"]["public"]
    query = urlencode(
        {
            "symbol": pair_config["market_id"],
            "interval": timeframe,
            "limit": limit,
        }
    )
    with urlopen(f"{public_url}/klines?{query}", timeout=30) as response:
        rows = json.loads(response.read().decode("utf-8"))

    dataframe = pd.DataFrame(
        [
            [
                row[0],
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ]
            for row in rows
        ],
        columns=OHLCV_COLUMNS,
    )
    dataframe["date"] = pd.to_datetime(dataframe["date"], unit="ms", utc=True)
    return dataframe


def beijing_opens_cache_path(pair: str) -> Path:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    scanner_config = config["scanner"]["daily_trend"]
    cache_directory = (
        ROOT_DIR / scanner_config["cache_directory"] / "beijing_daily_opens"
    )
    cache_directory.mkdir(parents=True, exist_ok=True)
    safe_market_id = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in pair_config["market_id"]
    )
    return cache_directory / f"{safe_market_id}.json"


def fetch_beijing_daily_opens(
    pair: str,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> dict[Any, float]:
    universe = read_universe()
    pair_config = universe.get(pair)
    if not pair_config:
        raise ValueError(f"Pair {pair} is not in the current universe")

    config = read_config()
    public_url = config["exchange"]["ccxt_config"]["urls"]["api"]["public"]
    required_dates = {
        timestamp.tz_convert("Asia/Shanghai").date()
        for timestamp in pd.date_range(
            start_time.normalize(),
            end_time.normalize(),
            freq="1D",
        )
    }
    path = beijing_opens_cache_path(pair)
    cached_payload = {}
    if path.exists():
        try:
            cached_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_payload = {}
    daily_opens = {
        pd.Timestamp(date_value).date(): float(opening_price)
        for date_value, opening_price in cached_payload.items()
    }
    missing_dates = required_dates - daily_opens.keys()
    if not missing_dates:
        return daily_opens

    first_missing = min(missing_dates)
    last_missing = max(missing_dates)
    start_utc = pd.Timestamp(first_missing, tz="Asia/Shanghai").tz_convert("UTC")
    end_utc = (
        pd.Timestamp(last_missing, tz="Asia/Shanghai") + pd.Timedelta(days=1)
    ).tz_convert("UTC")
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    rows: list[list[Any]] = []

    while start_ms < end_ms:
        query = urlencode(
            {
                "symbol": pair_config["market_id"],
                "interval": "4h",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        with urlopen(f"{public_url}/klines?{query}", timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        if not batch:
            break
        rows.extend(batch)
        next_start = int(batch[-1][0]) + 4 * 60 * 60 * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(batch) < 1000:
            break

    for row in rows:
        timestamp = pd.Timestamp(int(row[0]), unit="ms", tz="UTC")
        beijing_time = timestamp.tz_convert("Asia/Shanghai")
        if beijing_time.hour == 0:
            daily_opens[beijing_time.date()] = float(row[1])
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(
            {
                date_value.isoformat(): opening_price
                for date_value, opening_price in daily_opens.items()
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return daily_opens


async def ensure_beijing_daily_opens(
    pair: str,
    dataframe: pd.DataFrame,
) -> dict[Any, float]:
    lock = _daily_open_locks.setdefault(pair, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(
            fetch_beijing_daily_opens,
            pair,
            dataframe["date"].iloc[0],
            dataframe["date"].iloc[-1],
        )


async def prewarm_daily_open_caches() -> None:
    semaphore = asyncio.Semaphore(3)

    async def prewarm_pair(pair: str) -> None:
        async with semaphore:
            try:
                dataframe = read_daily_candles(pair, 365)
                if dataframe is not None and not dataframe.empty:
                    await ensure_beijing_daily_opens(pair, dataframe)
            except Exception:
                return

    await asyncio.gather(
        *(prewarm_pair(candidate["pair"]) for candidate in read_candidates())
    )


def build_chart_payload(
    pair: str,
    timeframe: str,
    limit: int,
    dataframe: pd.DataFrame,
    beijing_daily_opens: dict[Any, float] | None = None,
) -> dict[str, Any]:
    for period in MA_PERIODS:
        dataframe[f"ma_{period}"] = dataframe["close"].rolling(
            window=period,
            min_periods=period,
        ).mean()
    if timeframe == "1d":
        beijing_dates = dataframe["date"].dt.tz_convert("Asia/Shanghai").dt.date
        opening_prices = beijing_dates.map((beijing_daily_opens or {}).get)
        dataframe["change_today"] = (
            dataframe["close"] / pd.Series(opening_prices, index=dataframe.index) - 1
        ) * 100
    else:
        beijing_dates = dataframe["date"].dt.tz_convert("Asia/Shanghai").dt.date
        daily_open = dataframe.groupby(beijing_dates)["open"].transform("first")
        dataframe["change_today"] = (dataframe["close"] / daily_open - 1) * 100
    candles = []
    for row in dataframe.itertuples(index=False):
        candles.append(
            {
                "time": int(row.date.timestamp()),
                "date": row.date.isoformat(),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "quoteVolume": float(row.close * row.volume),
                "changeToday": (
                    None if pd.isna(row.change_today) else float(row.change_today)
                ),
                "ma7": None if pd.isna(row.ma_7) else float(row.ma_7),
                "ma20": None if pd.isna(row.ma_20) else float(row.ma_20),
                "ma99": None if pd.isna(row.ma_99) else float(row.ma_99),
            }
        )

    return {
        "pair": pair,
        "timeframe": timeframe,
        "limit": limit,
        "candles": candles,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }


@app.on_event("startup")
async def startup_prewarm() -> None:
    asyncio.create_task(prewarm_daily_open_caches())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "candidate_count": len(read_candidates())}


@app.get("/api/candidates")
async def candidates() -> dict[str, Any]:
    items = read_candidates()
    return {
        "candidates": items,
        "status": read_status(),
        "timeframes": sorted(ALLOWED_TIMEFRAMES, key=("15m", "1h", "4h", "1d").index),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, float]:
    return read_runtime_settings()


@app.put("/api/settings")
async def update_settings(settings: DashboardSettings) -> dict[str, Any]:
    return {
        "status": "saved",
        "settings": write_runtime_settings(settings),
    }


async def run_manual_scan() -> None:
    async with _scan_lock:
        _scan_state.update(
            {
                "running": True,
                "started_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "finished_at": None,
                "return_code": None,
                "message": "正在刷新成交额、日线和 MA...",
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT_DIR / "user_data/scripts/screen_daily_trend.py"),
            "--refresh-universe",
            cwd=ROOT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await process.communicate()
        output = output_bytes.decode("utf-8", errors="replace").strip()
        final_line = output.splitlines()[-1] if output else ""

        if process.returncode == 0:
            _response_cache.clear()
            asyncio.create_task(prewarm_daily_open_caches())
            message = final_line or "扫描完成"
        elif process.returncode == 75:
            message = "定时扫描正在运行，请稍后再试"
        else:
            message = final_line or "扫描失败"

        _scan_state.update(
            {
                "running": False,
                "finished_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "return_code": process.returncode,
                "message": message,
            }
        )


@app.post("/api/scan")
async def start_scan() -> dict[str, Any]:
    if _scan_state["running"] or _scan_lock.locked():
        return _scan_state.copy()

    asyncio.create_task(run_manual_scan())
    await asyncio.sleep(0)
    return _scan_state.copy()


@app.get("/api/scan/status")
async def scan_status() -> dict[str, Any]:
    return _scan_state.copy()


@app.get("/api/candles")
async def candles(
    pair: str = Query(...),
    timeframe: str = Query("1d"),
    limit: int = Query(300, ge=120, le=500),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    if timeframe not in ALLOWED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported timeframe")

    candidate_pairs = {item["pair"] for item in read_candidates()}
    if pair not in candidate_pairs:
        raise HTTPException(status_code=404, detail="Pair is not a current candidate")

    cache_key = (pair, timeframe, limit)
    cached = _response_cache.get(cache_key)
    if not refresh and cached and time.monotonic() - cached[0] < 30:
        return cached[1]

    try:
        dataframe = None if refresh else (
            read_daily_candles(pair, limit) if timeframe == "1d" else None
        )
        if dataframe is None:
            dataframe = await asyncio.to_thread(
                fetch_binance_candles,
                pair,
                timeframe,
                limit,
            )
        beijing_daily_opens = None
        if timeframe == "1d" and not dataframe.empty:
            beijing_daily_opens = await ensure_beijing_daily_opens(pair, dataframe)
        payload = build_chart_payload(
            pair,
            timeframe,
            limit,
            dataframe.copy(),
            beijing_daily_opens,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to load candles: {exc}") from exc

    _response_cache[cache_key] = (time.monotonic(), payload)
    return payload
