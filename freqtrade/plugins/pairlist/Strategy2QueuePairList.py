from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

from freqtrade.exchange.exchange_types import Tickers
from freqtrade.plugins.pairlist.IPairList import (
    IPairList,
    PairlistParameter,
    SupportsBacktesting,
)

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.asset_filters import is_tokenized_stock_pair


logger = logging.getLogger(__name__)


class Strategy2QueuePairList(IPairList):
    """Load the isolated strategy2 candidate queue from a local CSV file."""

    is_pairlist_generator = True
    supports_backtesting = SupportsBacktesting.NO

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        configured = str(
            self._pairlistconfig.get(
                "candidate_path",
                "scan_results/strategy2/daily_trend_candidates.csv",
            )
        )
        candidate_path = Path(configured)
        self._candidate_path = (
            candidate_path
            if candidate_path.is_absolute()
            else self._config["user_data_dir"] / candidate_path
        )
        self._include_pairs = [
            str(pair)
            for pair in self._pairlistconfig.get(
                "include_pairs",
                ["BTC/USDT"],
            )
        ]
        self._last_pairlist = list(self._include_pairs)

    def short_desc(self) -> str:
        return f"{self.name} from {self._candidate_path}"

    @staticmethod
    def description() -> str:
        return "Read strategy2 candidates from an isolated local CSV queue."

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "candidate_path": {
                "type": "string",
                "default": (
                    "scan_results/strategy2/"
                    "daily_trend_candidates.csv"
                ),
                "description": "Candidate queue CSV path",
                "help": "Path relative to the user_data directory.",
            },
            "include_pairs": {
                "type": "list",
                "default": ["BTC/USDT"],
                "description": "Pairs always kept for market context",
                "help": "Pairs loaded even when the candidate queue is empty.",
            },
            **IPairList.refresh_period_parameter(),
        }

    def gen_pairlist(self, tickers: Tickers) -> list[str]:
        try:
            with self._candidate_path.open(
                encoding="utf-8",
                newline="",
            ) as handle:
                candidates = [
                    str(row["pair"])
                    for row in csv.DictReader(handle)
                    if row.get("pair")
                    and not is_tokenized_stock_pair(str(row["pair"]))
                ]
            pairs = list(dict.fromkeys([*self._include_pairs, *candidates]))
            pairs = self.verify_whitelist(pairs, logger.info)
            pairs = self._whitelist_for_active_markets(pairs)
            self._last_pairlist = pairs
        except (OSError, KeyError, csv.Error) as exc:
            self.log_once(
                f"Keeping previous strategy2 pairlist after {type(exc).__name__}",
                logger.warning,
            )
        return self._last_pairlist.copy()

    def filter_pairlist(
        self,
        pairlist: list[str],
        tickers: Tickers,
    ) -> list[str]:
        return pairlist
