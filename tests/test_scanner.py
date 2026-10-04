from bot.scanner import MarketScanner, is_leveraged_token, is_stablecoin
from tests.conftest import make_settings


def test_stable_and_leveraged_detection():
    assert is_stablecoin("USDC") and is_stablecoin("FDUSD")
    assert not is_stablecoin("ETH")
    assert is_leveraged_token("BTCUP") and is_leveraged_token("ETHDOWN")
    assert not is_leveraged_token("JUP") and not is_leveraged_token("ETH")


def test_tradable_universe_excludes_bad_symbols(fake):
    scanner = MarketScanner(fake, make_settings(symbol_blacklist="ETHUSDT"))
    universe = scanner.tradable_symbols()
    assert "BTCUSDT" in universe
    assert "USDCUSDT" not in universe  # stable/stable
    assert "BTCUPUSDT" not in universe  # leveraged
    assert "DEADUSDT" not in universe  # TRADING değil (delist süreci)
    assert "ETHUSDT" not in universe  # kara liste


def test_scan_filters_and_scores(fake):
    fake.spread["ETHUSDT"] = 0.01
    fake.change_pct["SOLUSDT"] = 25.0
    scanner = MarketScanner(fake, make_settings())
    res = scanner.scan("BULL")
    symbols = [s.symbol for s in res.scored]
    assert "LOWUSDT" not in symbols and res.rejected["LOWUSDT"] == "Düşük hacim"
    assert "NEWUSDT" not in symbols and res.rejected["NEWUSDT"] == "Yeni listelenmiş"
    assert "Yüksek spread" in res.rejected["ETHUSDT"]
    assert "pump/dump" in res.rejected["SOLUSDT"]
    assert res.best is not None and res.best.symbol == "BTCUSDT"
    assert all(0 <= s.score <= 100 for s in res.scored)


def test_scan_summary_reports_rejections(fake):
    scanner = MarketScanner(fake, make_settings(min_quote_volume_usdt=1e12))
    res = scanner.scan("BULL")
    assert not res.scored
    text = res.summary()
    assert "taranabilir parite" in text and "Düşük hacim" in text
