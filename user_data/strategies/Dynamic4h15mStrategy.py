from __future__ import annotations

import csv
import json
import logging
import math
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
from pandas import DataFrame, Series

from freqtrade.exceptions import OperationalException
from freqtrade.persistence import Order, Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.scripts.dynamic_4h_live_features import (
    add_dynamic_4h_features,
)
from user_data.scripts.strategy2_parameters import (
    Strategy2Parameters,
    load_strategy2_parameters,
)
from user_data.asset_filters import is_tokenized_stock_pair


logger = logging.getLogger(__name__)


class QueueCandidate(NamedTuple):
    pair: str
    rank: int
    score: float
    quote_volume: float
    atr14: float
    atr22: float
    chandelier_high22: float
    swing_low4: float


class Dynamic4h15mStrategy(IStrategy):
    """Dry-run implementation of the guarded dynamic 4h/15m strategy."""

    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "15m"
    startup_candle_count = 1800
    process_only_new_candles = True

    minimal_roi = {}
    stoploss = -0.99
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    candidate_path = (
        ROOT
        / "user_data/scan_results/strategy2/daily_trend_candidates.csv"
    )
    runtime_settings_path = (
        ROOT / "user_data/config_strategy2_parameters.json"
    )
    queue_state_path = (
        ROOT / "user_data/scan_data/strategy2/queue_state.json"
    )
    binance_risk_assets_path = (
        ROOT / "user_data/scan_data/strategy2/binance_risk_assets.json"
    )

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "dynamic_ema20": {"color": "#0788b5"},
            "dynamic_ma7": {"color": "#7c52c7"},
            "strategy2_stop": {"color": "#dc3f56"},
        },
        "subplots": {
            "Trend": {
                "dynamic_adx14": {"color": "#0f766e"},
                "dynamic_rsi14": {"color": "#d97706"},
            }
        },
    }

    def bot_start(self, **kwargs) -> None:
        if not bool(self.config.get("dry_run")):
            raise OperationalException(
                "Dynamic4h15mStrategy is locked to dry_run mode."
            )
        self._ensure_runtime_state()
        self._load_parameters(force=True)
        self._load_queue(force=True)

    def bot_loop_start(
        self,
        current_time: datetime,
        **kwargs,
    ) -> None:
        self._ensure_runtime_state()
        self._load_parameters()
        self._load_queue()

    def _ensure_runtime_state(self) -> None:
        if hasattr(self, "_queue_lock"):
            return
        self._queue_lock = threading.RLock()
        self._parameters = Strategy2Parameters()
        self._runtime_valid = False
        self._entry_enabled = False
        self._settings_mtime_ns: int | None = None
        self._candidate_mtime_ns: int | None = None
        self._risk_asset_mtime_ns: int | None = None
        self._tokenized_stock_assets: set[str] = set()
        self._queue_version: str | None = None
        self._queue: list[QueueCandidate] = []
        self._exited_pairs: set[str] = set()
        self._pending_replacement_pair: str | None = None
        self._pending_entry_setups: dict[
            str,
            tuple[float, float, float],
        ] = {}
        self._restore_queue_state()

    def _is_tokenized_stock_pair(self, pair: str) -> bool:
        if is_tokenized_stock_pair(pair):
            return True
        try:
            mtime_ns = self.binance_risk_assets_path.stat().st_mtime_ns
            if mtime_ns != self._risk_asset_mtime_ns:
                payload = json.loads(
                    self.binance_risk_assets_path.read_text(encoding="utf-8")
                )
                assets = payload.get("assets", {})
                if not isinstance(assets, dict):
                    raise ValueError("Invalid Binance risk asset cache")
                self._tokenized_stock_assets = {
                    str(asset).upper()
                    for asset, reasons in assets.items()
                    if isinstance(reasons, list)
                    and any(
                        str(reason).lower() == "tag:bstocks"
                        for reason in reasons
                    )
                }
                self._risk_asset_mtime_ns = mtime_ns
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            # The static catalog still protects known bStocks during an outage.
            pass
        return pair.split("/", maxsplit=1)[0].upper() in self._tokenized_stock_assets

    def _load_parameters(self, force: bool = False) -> None:
        try:
            mtime_ns = self.runtime_settings_path.stat().st_mtime_ns
            if not force and mtime_ns == self._settings_mtime_ns:
                return
            payload = json.loads(
                self.runtime_settings_path.read_text(encoding="utf-8")
            )
            if payload.get("active_strategy") != "strategy2":
                raise ValueError("active_strategy must be strategy2")
            parameters = load_strategy2_parameters(
                self.runtime_settings_path
            )
            if (
                parameters.dynamic_entry_enabled
                or parameters.scan_interval_minutes != 240
                or not parameters.candidate_queue_refill_enabled
                or not parameters.candidate_queue_exclude_exited
            ):
                raise ValueError(
                    "strategy2 queue safety settings do not match"
                )
            self._parameters = parameters
            self._entry_enabled = bool(
                payload.get("entry_enabled", True)
            )
            self._runtime_valid = True
            self._settings_mtime_ns = mtime_ns
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._runtime_valid = False
            self._entry_enabled = False
            logger.error("Strategy2 runtime settings rejected: %s", exc)

    @staticmethod
    def _number(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float("nan")
        return number if math.isfinite(number) else float("nan")

    @staticmethod
    def _queue_boundary(value: Any) -> str | None:
        try:
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            else:
                timestamp = timestamp.tz_convert("UTC")
            return timestamp.floor("4h").isoformat()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _candidate_to_dict(candidate: QueueCandidate) -> dict[str, Any]:
        return {
            "pair": candidate.pair,
            "rank": candidate.rank,
            "score": candidate.score,
            "quote_volume": candidate.quote_volume,
            "atr14": candidate.atr14,
            "atr22": candidate.atr22,
            "chandelier_high22": candidate.chandelier_high22,
            "swing_low4": candidate.swing_low4,
        }

    @classmethod
    def _candidate_from_dict(
        cls,
        payload: dict[str, Any],
    ) -> QueueCandidate | None:
        pair = str(payload.get("pair", "")).strip()
        if not pair or is_tokenized_stock_pair(pair):
            return None
        return QueueCandidate(
            pair=pair,
            rank=int(payload.get("rank", 0)),
            score=cls._number(payload.get("score")),
            quote_volume=cls._number(payload.get("quote_volume")),
            atr14=cls._number(payload.get("atr14")),
            atr22=cls._number(payload.get("atr22")),
            chandelier_high22=cls._number(
                payload.get("chandelier_high22")
            ),
            swing_low4=cls._number(payload.get("swing_low4")),
        )

    def _restore_queue_state(self) -> None:
        try:
            payload = json.loads(
                self.queue_state_path.read_text(encoding="utf-8")
            )
            queue = [
                candidate
                for item in payload.get("queue", [])
                if isinstance(item, dict)
                and (
                    candidate := self._candidate_from_dict(item)
                )
                is not None
            ]
            self._queue_version = payload.get("queue_version")
            self._queue = sorted(queue, key=lambda item: item.rank)
            self._exited_pairs = {
                str(pair)
                for pair in payload.get("exited_pairs", [])
                if pair
            }
            pending = payload.get("pending_replacement_pair")
            self._pending_replacement_pair = (
                str(pending) if pending else None
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def _persist_queue_state(self) -> None:
        self.queue_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "queue_version": self._queue_version,
            "queue": [
                self._candidate_to_dict(candidate)
                for candidate in self._queue
            ],
            "exited_pairs": sorted(self._exited_pairs),
            "pending_replacement_pair": self._pending_replacement_pair,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.queue_state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.queue_state_path)

    def _read_candidate_csv(
        self,
        mtime: float,
    ) -> tuple[str, list[QueueCandidate]]:
        with self.candidate_path.open(
            encoding="utf-8",
            newline="",
        ) as handle:
            rows = list(csv.DictReader(handle))
        updated_values = [
            row.get("updated_at")
            for row in rows
            if row.get("updated_at")
        ]
        version = self._queue_boundary(
            max(updated_values)
            if updated_values
            else datetime.fromtimestamp(mtime, tz=timezone.utc)
        )
        if version is None:
            raise ValueError("candidate queue has no valid UTC boundary")
        queue = []
        for row in rows:
            candidate = self._candidate_from_dict(
                {
                    "pair": row.get("pair"),
                    "rank": row.get("rank"),
                    "score": row.get("entry_score"),
                    "quote_volume": row.get("quote_volume_24h"),
                    "atr14": row.get("atr_4h"),
                    "atr22": row.get("atr22_4h"),
                    "chandelier_high22": row.get(
                        "chandelier_high_4h"
                    ),
                    "swing_low4": row.get("swing_low_4h"),
                }
            )
            if candidate is not None:
                queue.append(candidate)
        queue.sort(
            key=lambda item: (
                item.rank,
                -item.score if math.isfinite(item.score) else math.inf,
                -item.quote_volume
                if math.isfinite(item.quote_volume)
                else math.inf,
                item.pair,
            )
        )
        return version, queue

    def _load_queue(self, force: bool = False) -> None:
        self._ensure_runtime_state()
        try:
            stat = self.candidate_path.stat()
            if not force and stat.st_mtime_ns == self._candidate_mtime_ns:
                return
            version, queue = self._read_candidate_csv(stat.st_mtime)
        except (OSError, TypeError, ValueError, csv.Error) as exc:
            logger.warning("Strategy2 candidate queue unavailable: %s", exc)
            return

        with self._queue_lock:
            self._candidate_mtime_ns = stat.st_mtime_ns
            if version == self._queue_version:
                return
            if (
                self._queue_version is not None
                and pd.Timestamp(version) < pd.Timestamp(self._queue_version)
            ):
                return
            self._queue_version = version
            self._queue = queue
            self._exited_pairs.clear()
            self._pending_replacement_pair = None
            self._persist_queue_state()
            logger.info(
                "Loaded strategy2 queue %s with %d candidates",
                version,
                len(queue),
            )

    def _queue_is_current(self, current_time: datetime) -> bool:
        if self._queue_version is None:
            return False
        boundary = pd.Timestamp(self._queue_version)
        now = pd.Timestamp(current_time)
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        else:
            now = now.tz_convert("UTC")
        return bool(boundary <= now < boundary + pd.Timedelta(hours=4))

    def _open_pairs(self) -> set[str]:
        try:
            return {
                trade.pair
                for trade in Trade.get_trades_proxy(is_open=True)
            }
        except (AttributeError, TypeError):
            return set()

    def _eligible_queue(
        self,
        current_time: datetime,
    ) -> list[QueueCandidate]:
        self._load_queue()
        if (
            not self._runtime_valid
            or not self._entry_enabled
            or not self._queue_is_current(current_time)
        ):
            return []
        open_pairs = self._open_pairs()
        eligible = [
            candidate
            for candidate in self._queue
            if candidate.pair not in open_pairs
            and candidate.pair not in self._exited_pairs
            and not self._is_tokenized_stock_pair(candidate.pair)
        ]
        if self._pending_replacement_pair:
            eligible.sort(
                key=lambda item: (
                    item.pair != self._pending_replacement_pair,
                    item.rank,
                )
            )
        return eligible

    def _candidate(self, pair: str) -> QueueCandidate | None:
        if self._is_tokenized_stock_pair(pair):
            return None
        return next(
            (
                candidate
                for candidate in self._queue
                if candidate.pair == pair
            ),
            None,
        )

    def _latest_dataframe(self, pair: str) -> DataFrame | None:
        if not self.dp:
            return None
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(
                pair,
                self.timeframe,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return dataframe if not dataframe.empty else None

    def _prepare_entry(
        self,
        candidate: QueueCandidate,
        entry_price: float,
    ) -> tuple[float, float, float] | None:
        values = (
            entry_price,
            candidate.atr14,
            candidate.atr22,
            candidate.chandelier_high22,
            candidate.swing_low4,
        )
        if not all(math.isfinite(value) for value in values):
            return None
        chandelier_stop = (
            candidate.chandelier_high22
            - candidate.atr22
            * self._parameters.chandelier_atr_multiplier
        )
        if entry_price <= chandelier_stop:
            return None
        hard_stop = entry_price * (
            1.0 - self._parameters.stop_pct / 100.0
        )
        stop_rate = max(
            hard_stop,
            candidate.swing_low4 - candidate.atr14 * 0.25,
        )
        if stop_rate >= entry_price * 0.995:
            stop_rate = hard_stop
        initial_risk = entry_price - stop_rate
        if initial_risk <= 0:
            return None
        return entry_price, stop_rate, initial_risk

    @staticmethod
    def _trade_float(
        trade: Trade,
        key: str,
        default: float,
    ) -> float:
        try:
            value = float(trade.get_custom_data(key, default))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    def _initialize_trade(
        self,
        trade: Trade,
        entry_price: float | None = None,
    ) -> bool:
        if trade.get_custom_data("strategy2_initial_risk") is not None:
            return True
        candidate = self._candidate(trade.pair)
        if candidate is None:
            return False
        price = float(entry_price or trade.open_rate)
        self._pending_entry_setups.pop(trade.pair, None)
        setup = self._prepare_entry(candidate, price)
        if setup is None:
            return False
        _, stop_rate, initial_risk = setup
        trade.set_custom_data("strategy2_initial_stop", stop_rate)
        trade.set_custom_data("strategy2_initial_risk", initial_risk)
        trade.set_custom_data("strategy2_stop_rate", stop_rate)
        trade.set_custom_data("strategy2_stop_reason", "stop_loss")
        trade.set_custom_data(
            "strategy2_target_rate",
            price + initial_risk * self._parameters.reward_risk,
        )
        trade.set_custom_data("strategy2_target_reached", False)
        trade.set_custom_data("strategy2_target_runner", False)
        trade.set_custom_data(
            "strategy2_entry_queue_version",
            self._queue_version,
        )
        return True

    def populate_indicators(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        self._ensure_runtime_state()
        self._load_parameters()
        self._load_queue()
        result = add_dynamic_4h_features(dataframe)
        result["strategy2_candidate"] = int(
            not self._is_tokenized_stock_pair(metadata["pair"])
            and self._candidate(metadata["pair"]) is not None
        )
        result["strategy2_entry_enabled"] = int(
            self._runtime_valid and self._entry_enabled
        )
        result["strategy2_stop"] = float("nan")
        return result

    def populate_entry_trend(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        dataframe["enter_long"] = 0
        entry = (
            (dataframe["strategy2_entry_enabled"] == 1)
            & (dataframe["strategy2_candidate"] == 1)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[entry, ["enter_long", "enter_tag"]] = (
            1,
            "strategy2_queue",
        )
        return dataframe

    def populate_exit_trend(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        self._ensure_runtime_state()
        self._load_parameters()
        if self._is_tokenized_stock_pair(pair):
            return False
        eligible = self._eligible_queue(current_time)
        if not eligible or eligible[0].pair != pair:
            return False
        setup = self._prepare_entry(eligible[0], float(rate))
        if setup is None:
            return False
        self._pending_entry_setups[pair] = setup
        return True

    def order_filled(
        self,
        pair: str,
        trade: Trade,
        order: Order,
        current_time: datetime,
        **kwargs,
    ) -> None:
        if (
            order.ft_order_side == trade.exit_side
            and order.safe_filled > 0
        ):
            with self._queue_lock:
                self._exited_pairs.add(pair)
                self._persist_queue_state()
            return
        if (
            order.ft_order_side != trade.entry_side
            or order.safe_filled <= 0
        ):
            return
        self._initialize_trade(
            trade,
            float(trade.open_rate or order.safe_price),
        )
        if self._pending_replacement_pair == pair:
            self._pending_replacement_pair = None
            self._persist_queue_state()

    def _strong_trend(self, dataframe: DataFrame) -> bool:
        if len(dataframe) < 2:
            return False
        row = dataframe.iloc[-2]
        values = (
            self._number(row.get("dynamic_adx14")),
            self._number(row.get("dynamic_ema20_slope")),
            self._number(row.get("dynamic_rsi14")),
            self._number(row.get("dynamic_volume_ratio")),
        )
        return bool(
            all(math.isfinite(value) for value in values)
            and values[0] >= self._parameters.target_hold_adx_min
            and values[1] >= self._parameters.target_hold_slope_min
            and values[2] >= self._parameters.target_hold_rsi_min
            and values[3]
            >= self._parameters.target_hold_volume_ratio_min
        )

    def _update_protection(
        self,
        trade: Trade,
        row: Series,
        current_rate: float,
    ) -> tuple[float, str]:
        entry_price = float(trade.open_rate)
        risk = self._trade_float(
            trade,
            "strategy2_initial_risk",
            entry_price * self._parameters.stop_pct / 100.0,
        )
        stop_rate = self._trade_float(
            trade,
            "strategy2_stop_rate",
            entry_price - risk,
        )
        stop_reason = str(
            trade.get_custom_data(
                "strategy2_stop_reason",
                "stop_loss",
            )
        )
        peak_rate = max(
            entry_price,
            current_rate,
            float(getattr(trade, "max_rate", entry_price) or entry_price),
            self._number(row.get("high")),
        )

        new_stop = stop_rate
        new_reason = stop_reason
        if bool(trade.get_custom_data("strategy2_target_runner", False)):
            atr14 = self._number(row.get("dynamic_atr14"))
            if math.isfinite(atr14):
                runner_stop = max(
                    peak_rate
                    - atr14 * self._parameters.target_trailing_atr,
                    entry_price + risk * self._parameters.target_lock_r,
                )
                if runner_stop > new_stop:
                    new_stop = runner_stop
                    new_reason = "target_trailing_stop"

        if peak_rate >= (
            entry_price + risk * self._parameters.break_even_r
        ):
            break_even_stop = entry_price * 1.002
            if break_even_stop > new_stop:
                new_stop = break_even_stop
                new_reason = "break_even_stop"

        atr22 = self._number(row.get("dynamic_atr22"))
        chandelier_high = self._number(
            row.get("dynamic_chandelier_high22")
        )
        if (
            self._parameters.chandelier_exit_enabled
            and math.isfinite(atr22)
            and math.isfinite(chandelier_high)
        ):
            chandelier_stop = (
                chandelier_high
                - atr22 * self._parameters.chandelier_atr_multiplier
            )
            if chandelier_stop > new_stop:
                new_stop = chandelier_stop
                new_reason = "chandelier_exit"

        if new_stop > stop_rate:
            trade.set_custom_data("strategy2_stop_rate", new_stop)
            trade.set_custom_data("strategy2_stop_reason", new_reason)
        return new_stop, new_reason

    def _replacement_candidate(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        row: Series,
    ) -> QueueCandidate | None:
        if (
            not self._parameters.candidate_replacement_enabled
            or not self._queue_is_current(current_time)
            or self._queue_version is None
            or trade.get_custom_data(
                "strategy2_replacement_checked",
            )
            == self._queue_version
        ):
            return None
        trade.set_custom_data(
            "strategy2_replacement_checked",
            self._queue_version,
        )
        held_minutes = (
            current_time - trade.open_date_utc
        ).total_seconds() / 60.0
        incumbent_score = self._number(row.get("dynamic_score"))
        if (
            held_minutes
            < self._parameters.replacement_min_hold_minutes
            or not math.isfinite(incumbent_score)
        ):
            return None
        for challenger in self._eligible_queue(current_time):
            if challenger.pair == pair or not math.isfinite(
                challenger.score
            ):
                continue
            if (
                challenger.score
                <= incumbent_score
                + self._parameters.replacement_min_score_advantage
            ):
                continue
            dataframe = self._latest_dataframe(challenger.pair)
            if dataframe is None:
                continue
            entry_price = self._number(dataframe.iloc[-1].get("close"))
            if (
                math.isfinite(entry_price)
                and self._prepare_entry(challenger, entry_price)
                is not None
            ):
                return challenger
        return None

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool:
        self._ensure_runtime_state()
        self._load_parameters()
        dataframe = self._latest_dataframe(pair)
        if dataframe is None or not self._initialize_trade(trade):
            return False
        row = dataframe.iloc[-1]
        stop_rate = self._trade_float(
            trade,
            "strategy2_stop_rate",
            trade.open_rate * (1.0 - self._parameters.stop_pct / 100.0),
        )
        stop_reason = str(
            trade.get_custom_data("strategy2_stop_reason", "stop_loss")
        )
        if current_rate <= stop_rate:
            return stop_reason

        target_reached = bool(
            trade.get_custom_data("strategy2_target_reached", False)
        )
        target_rate = self._trade_float(
            trade,
            "strategy2_target_rate",
            float("inf"),
        )
        peak_rate = max(
            current_rate,
            float(getattr(trade, "max_rate", current_rate) or current_rate),
            self._number(row.get("high")),
        )
        if (
            not target_reached
            and self._parameters.take_profit_mode != "none"
            and peak_rate >= target_rate
        ):
            if (
                self._parameters.take_profit_mode == "adaptive"
                and self._strong_trend(dataframe)
            ):
                trade.set_custom_data(
                    "strategy2_target_runner",
                    True,
                )
                trade.set_custom_data(
                    "strategy2_target_reached",
                    True,
                )
            elif self._parameters.take_profit_mode in {
                "trailing",
                "partial",
            }:
                trade.set_custom_data(
                    "strategy2_target_runner",
                    True,
                )
                trade.set_custom_data(
                    "strategy2_target_reached",
                    True,
                )
            else:
                return "take_profit"

        stop_rate, stop_reason = self._update_protection(
            trade,
            row,
            current_rate,
        )
        if current_rate <= stop_rate:
            return stop_reason

        close = self._number(row.get("close"))
        ema20 = self._number(row.get("dynamic_ema20"))
        ma7 = self._number(row.get("dynamic_ma7"))
        if math.isfinite(close) and math.isfinite(ema20) and close < ema20:
            return "ema20_exit"
        if (
            math.isfinite(close)
            and math.isfinite(ma7)
            and close
            < ma7
            * (1.0 - self._parameters.ma7_exit_threshold_pct / 100.0)
        ):
            return "ma7_exit"

        held_minutes = (
            current_time - trade.open_date_utc
        ).total_seconds() / 60.0
        if (
            self._parameters.time_exit_mode != "none"
            and held_minutes >= self._parameters.max_hold_minutes
            and not (
                self._parameters.time_exit_mode == "runner"
                and bool(
                    trade.get_custom_data(
                        "strategy2_target_runner",
                        False,
                    )
                )
            )
        ):
            return "time_exit"

        challenger = self._replacement_candidate(
            pair,
            trade,
            current_time,
            row,
        )
        if challenger is not None:
            self._pending_replacement_pair = challenger.pair
            self._persist_queue_state()
            trade.set_custom_data(
                "strategy2_replacement_pair",
                challenger.pair,
            )
            return "candidate_replacement"
        return False

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float:
        dataframe = self._latest_dataframe(pair)
        if dataframe is None or not self._initialize_trade(trade):
            return self._parameters.stop_pct / 100.0
        stop_rate, _ = self._update_protection(
            trade,
            dataframe.iloc[-1],
            current_rate,
        )
        return stoploss_from_absolute(
            stop_rate,
            current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        return True
