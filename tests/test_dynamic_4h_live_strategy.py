import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import talib

from freqtrade.exceptions import OperationalException
from freqtrade.plugins.pairlist.Strategy2QueuePairList import (
    Strategy2QueuePairList,
)
from user_data.scripts.dynamic_4h_15m_backtest import load_parameters
from user_data.scripts.dynamic_4h_live_features import (
    add_dynamic_4h_features,
)
from user_data.scripts.strategy2_parameters import (
    load_strategy2_parameters,
)
from user_data.strategies.Dynamic4h15mStrategy import (
    Dynamic4h15mStrategy,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SETTINGS = (
    ROOT / "user_data/config_strategy2_parameters.json"
)
DRY_CONFIG = ROOT / "user_data/config_strategy2_dry.json"
REPORT_PARAMETERS = (
    ROOT
    / "user_data/backtest_results/"
    "dynamic_4h_15m_candidate_queue_parameters_20260909.json"
)


class FakePairlistExchange:
    name = "fake"
    markets = {"BTC/USDT": {}, "AAA/USDT": {}}

    @staticmethod
    def market_is_tradable(_market) -> bool:
        return True

    @staticmethod
    def get_pair_quote_currency(_pair) -> str:
        return "USDT"


class FakePairlistManager:
    @staticmethod
    def verify_whitelist(pairs, *_args, **_kwargs):
        return pairs


def sample_15m_frame(bucket_count: int = 110) -> pd.DataFrame:
    size = bucket_count * 16
    dates = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=size,
        freq="15min",
    )
    index = np.arange(size, dtype=np.float64)
    close = 100.0 + index * 0.02 + np.sin(index / 13.0)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.03,
            "high": close + 0.15,
            "low": close - 0.18,
            "close": close,
            "volume": 1000.0 + np.cos(index / 7.0) * 100.0,
        }
    )


def test_live_dynamic_features_match_closed_4h_talib() -> None:
    dataframe = sample_15m_frame()
    actual = add_dynamic_4h_features(dataframe)
    completed = (
        dataframe.set_index("date")
        .resample("4h", origin="epoch")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
    )
    row = actual.iloc[-1]

    assert np.isclose(
        row["dynamic_ema20"],
        talib.EMA(completed["close"], timeperiod=20).iloc[-1],
    )
    assert np.isclose(
        row["dynamic_adx14"],
        talib.ADX(
            completed["high"],
            completed["low"],
            completed["close"],
            timeperiod=14,
        ).iloc[-1],
    )
    assert np.isclose(
        row["dynamic_atr14"],
        talib.ATR(
            completed["high"],
            completed["low"],
            completed["close"],
            timeperiod=14,
        ).iloc[-1],
    )


def test_live_dynamic_features_do_not_look_ahead_inside_4h() -> None:
    dataframe = sample_15m_frame()
    decision_index = len(dataframe) - 12
    original = add_dynamic_4h_features(dataframe).iloc[decision_index]
    changed = dataframe.copy()
    changed.loc[
        changed.index > decision_index,
        ["high", "low", "close", "volume"],
    ] *= 10.0
    replayed = add_dynamic_4h_features(changed).iloc[decision_index]

    for column in (
        "dynamic_ema20",
        "dynamic_ema50",
        "dynamic_ema100",
        "dynamic_rsi14",
        "dynamic_adx14",
        "dynamic_atr14",
        "dynamic_volume_ratio",
    ):
        assert np.isclose(original[column], replayed[column])


def test_strategy2_refuses_live_mode() -> None:
    strategy = Dynamic4h15mStrategy({"dry_run": False})

    try:
        strategy.bot_start()
    except OperationalException:
        return
    raise AssertionError("strategy2 must reject non-dry-run mode")


def test_strategy2_config_is_isolated_and_dry_run() -> None:
    config = json.loads(DRY_CONFIG.read_text(encoding="utf-8"))

    assert config["strategy"] == "Dynamic4h15mStrategy"
    assert config["dry_run"] is True
    assert config["db_url"].endswith("trades_strategy2_dry.sqlite")
    assert config["api_server"]["listen_port"] == 8082
    assert (
        config["scanner"]["daily_trend"]["cache_directory"]
        == "user_data/scan_data/strategy2"
    )
    assert (
        config["scanner"]["daily_trend"]["output_directory"]
        == "user_data/scan_results/strategy2"
    )
    assert (
        load_strategy2_parameters(RUNTIME_SETTINGS).__dict__
        == load_parameters(REPORT_PARAMETERS).__dict__
    )


def write_candidates(
    path: Path,
    updated_at: datetime,
    pairs: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "rank",
        "pair",
        "entry_score",
        "quote_volume_24h",
        "atr_4h",
        "atr22_4h",
        "chandelier_high_4h",
        "swing_low_4h",
        "updated_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for rank, pair in enumerate(pairs, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "pair": pair,
                    "entry_score": 30 - rank,
                    "quote_volume_24h": 1_000_000,
                    "atr_4h": 2,
                    "atr22_4h": 2,
                    "chandelier_high_4h": 110,
                    "swing_low_4h": 95,
                    "updated_at": updated_at.isoformat(),
                }
            )


def test_strategy2_pairlist_only_loads_queue_and_btc() -> None:
    with TemporaryDirectory() as directory:
        user_data = Path(directory)
        candidate_path = (
            user_data
            / "scan_results/strategy2/daily_trend_candidates.csv"
        )
        write_candidates(
            candidate_path,
            datetime.now(timezone.utc),
            ["AAA/USDT"],
        )
        pairlist = Strategy2QueuePairList(
            exchange=FakePairlistExchange(),
            pairlistmanager=FakePairlistManager(),
            config={
                "user_data_dir": user_data,
                "stake_currency": "USDT",
            },
            pairlistconfig={
                "candidate_path": (
                    "scan_results/strategy2/"
                    "daily_trend_candidates.csv"
                ),
                "include_pairs": ["BTC/USDT"],
            },
            pairlist_pos=0,
        )

        assert pairlist.gen_pairlist({}) == ["BTC/USDT", "AAA/USDT"]


def test_queue_state_only_resets_on_new_4h_boundary() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        runtime = root / "runtime.json"
        candidates = root / "candidates.csv"
        state = root / "queue.json"
        runtime.write_text(
            RUNTIME_SETTINGS.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        boundary = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        boundary -= timedelta(hours=boundary.hour % 4)
        write_candidates(
            candidates,
            boundary + timedelta(minutes=1),
            ["AAA/USDT", "BBB/USDT"],
        )

        strategy = Dynamic4h15mStrategy({"dry_run": True})
        strategy.runtime_settings_path = runtime
        strategy.candidate_path = candidates
        strategy.queue_state_path = state
        strategy.bot_start()
        strategy._exited_pairs.add("AAA/USDT")
        strategy._persist_queue_state()

        write_candidates(
            candidates,
            boundary + timedelta(hours=2),
            ["BBB/USDT", "AAA/USDT"],
        )
        strategy._load_queue(force=True)
        assert strategy._exited_pairs == {"AAA/USDT"}

        write_candidates(
            candidates,
            boundary + timedelta(hours=4, minutes=1),
            ["AAA/USDT", "BBB/USDT"],
        )
        strategy._load_queue(force=True)
        assert strategy._exited_pairs == set()

        restored = json.loads(state.read_text(encoding="utf-8"))
        assert restored["queue_version"] == (
            pd.Timestamp(boundary) + pd.Timedelta(hours=4)
        ).isoformat()
