from user_data.asset_filters import (
    is_tokenized_stock_metadata,
    is_tokenized_stock_pair,
)


def test_tokenized_stocks_are_excluded_without_matching_regular_crypto() -> None:
    assert is_tokenized_stock_pair("NVDAB/USDT")
    assert is_tokenized_stock_pair("SPYB/USDT")
    assert is_tokenized_stock_pair("TSLAB/USDT")
    assert not is_tokenized_stock_pair("BTC/USDT")
    assert not is_tokenized_stock_pair("ARB/USDT")
    assert not is_tokenized_stock_pair("MUB/USDT")


def test_binance_bstocks_metadata_is_recognized() -> None:
    assert is_tokenized_stock_metadata({"tags": ["bStocks", "newListing"]})
    assert not is_tokenized_stock_metadata({"tags": ["RWA"]})
