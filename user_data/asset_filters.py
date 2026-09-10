"""Product-category filters shared by local scanners and strategies."""

from __future__ import annotations

# Binance bStocks are tokenized equities and ETFs, not crypto assets. Keep an
# explicit allow-deny catalog so crypto assets that happen to end in "B" remain
# eligible.
TOKENIZED_STOCK_BASES = frozenset(
    {
        "AAPLB",
        "AMATB",
        "AMDB",
        "ARMB",
        "ASMLB",
        "BABAB",
        "CBRSB",
        "COHRB",
        "COINB",
        "CRCLB",
        "CRDOB",
        "DJTB",
        "EWYB",
        "GMEB",
        "GOOGLB",
        "HOODB",
        "INTCB",
        "IRENB",
        "MSTRB",
        "MVLLB",
        "NBISB",
        "NVDAB",
        "QQQB",
        "RKLBB",
        "SMCIB",
        "SNDKB",
        "SNXXB",
        "SOXLB",
        "SPCXB",
        "SPYB",
        "TSLAB",
    }
)
BINANCE_TOKENIZED_STOCK_TAG = "bstocks"


def base_asset(pair: str) -> str:
    """Return the uppercase base asset from a Freqtrade pair."""
    return str(pair).split("/", maxsplit=1)[0].strip().upper()


def is_tokenized_stock_pair(pair: str) -> bool:
    """Return whether a pair is a Binance bStocks equity or ETF product."""
    return base_asset(pair) in TOKENIZED_STOCK_BASES


def is_tokenized_stock_metadata(asset: dict) -> bool:
    """Return whether Binance asset metadata explicitly labels a bStocks asset."""
    tags = asset.get("tags")
    return isinstance(tags, list) and any(
        str(tag).lower() == BINANCE_TOKENIZED_STOCK_TAG for tag in tags
    )
