# 🛡️ Safe Binance Spot Bot

Binance **Global Spot** hesabına API ile bağlanan, portföyü okuyan, sabit coin listesi olmadan
USDT paritelerini tarayan, **düşük riskle** otomatik al-sat yapan ve gerektiğinde portföyü
farklı coinlere döndürebilen bir bot. FastAPI tabanlı, mobil uyumlu bir web paneli içerir.

> ⚠️ **Uyarı:** Bu yazılım kâr garantisi **vermez**. Kripto para işlemleri yüksek risklidir ve
> sermayenizin tamamını kaybedebilirsiniz. Önce uzun süre **DRY_RUN** modunda deneyin.
> Sermaye güvenliği her zaman birinci önceliktir.

## Güvenlik ilkeleri

- Binance **şifreniz kullanılmaz**; yalnızca API Key + API Secret gerekir.
- Anahtarlar kaynak koda yazılmaz, sadece `.env` dosyasından okunur. `.env` `.gitignore` içindedir.
- **Para çekme (withdrawal) yetkisi gerekmez.** LIVE modda, anahtarda para çekme yetkisi açıksa
  bot **çalışmayı reddeder**.
- **Futures, Margin, kaldıraç ve short kullanılmaz.** Kodda bu uç noktalara çağrı yoktur.
- Varsayılan mod **DRY_RUN**'dır (gerçek emir gönderilmez). LIVE'a yalnızca `TRADING_MODE=LIVE`
  ile geçilir. LIVE modda bot kendiliğinden başlamaz; panelden "Botu Başlat" gerekir.
- Panel varsayılan olarak yalnızca `127.0.0.1` üzerinden açılır. İsterseniz `PANEL_TOKEN` ile
  kontrol butonlarını şifreleyebilirsiniz.

## Kurulum

### 1) Python ile

```bash
git clone https://github.com/alperenstc06/safe-binance-bot.git
cd safe-binance-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
python -m app.main
```

### 2) Docker ile

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

Panel: **http://127.0.0.1:8000**

## .env dosyası ve API anahtarları

`.env` dosyası projenin **kök klasöründe** (`app/`, `bot/` klasörlerinin yanında, `.env.example` ile
aynı yerde) oluşturulur:

```env
BINANCE_API_KEY=buraya_api_key
BINANCE_API_SECRET=buraya_api_secret
TRADING_MODE=DRY_RUN
```

Binance'te API anahtarı oluştururken (Hesap → API Yönetimi):
- ✅ **Enable Reading** (okuma)
- ✅ **Enable Spot & Margin Trading** (yalnızca LIVE için gerekli; bot margin kullanmaz)
- ❌ **Enable Withdrawals** – KAPALI olmalı
- ❌ Futures / Margin yetkileri – kapalı tutmanız önerilir
- Mümkünse **IP kısıtlaması** ekleyin.

## DRY_RUN (simülasyon)

`TRADING_MODE=DRY_RUN` (varsayılan). Bot:
- Gerçek Binance piyasa verisini (fiyat, mum, emir defteri, exchangeInfo) kullanır.
- Emirleri **gerçek ask/bid + slippage + komisyon** ile simüle eder, Binance'e emir göndermez.
- `DRY_RUN_START_BALANCE` (varsayılan 1000 USDT) ile kağıt hesap tutar.
- API anahtarı **olmadan da** çalışır. Anahtar girerseniz gerçek varlıklarınızı okur ve her biri
  için HOLD / PARTIAL_SELL / SELL önerisi gösterir (DRY_RUN'da hiçbir şey satmaz).

```bash
python -m app.main
```

## LIVE moda geçiş

1. En az birkaç hafta DRY_RUN sonuçlarını inceleyin.
2. `.env` içine API Key/Secret girin (para çekme yetkisi kapalı).
3. `.env` içinde `TRADING_MODE=LIVE` yapın.
4. Botu yeniden başlatın ve panelde **Botu Başlat**'a basın.

İsterseniz önce `BINANCE_TESTNET=true` ile Binance Spot Testnet anahtarlarıyla deneyebilirsiniz.

## Nasıl çalışır?

### Coin seçimi (sabit liste yok)
Her taramada (`SCAN_INTERVAL_SECONDS`, varsayılan 5 dk) tüm Spot USDT pariteleri okunur ve elenir:
- işlem durumu `TRADING` olmayan semboller (delist süreci) ve `SYMBOL_BLACKLIST`
  (delist uyarısı gelen varlıkları buraya ekleyin)
- stablecoin/stablecoin çiftleri, leveraged tokenlar (`UP/DOWN/BULL/BEAR`)
- düşük 24s hacim (`MIN_QUOTE_VOLUME_USDT`), yüksek spread (`MAX_SPREAD_PCT`)
- aşırı pump/dump (`MAX_ABS_24H_CHANGE_PCT`), son mumda aşırı hareket
- yeni listelenmiş (`MIN_LISTING_DAYS`) ve kısa fiyat geçmişi olan coinler

### Fırsat puanı (0-100)
| Faktör | Puan |
|---|---|
| EMA20 / EMA50 / EMA200 dizilimi | 25 |
| Trend gücü (EMA farkı / ATR, EMA50 eğimi) | 10 |
| RSI (50-65 ideal) | 15 |
| MACD histogramı | 15 |
| ATR % | 6 |
| Volatilite | 4 |
| 24s hacim | 10 |
| Hacim artışı | 5 |
| Bid/ask spread | 5 |
| BTC piyasa yönü | 5 |

Minimum puan **75**. Puan yüksek olsa bile: fiyat EMA20'den çok uzaksa (pump), RSI > 72 ise veya
son mumda aşırı hareket varsa işlem açılmaz.

### Piyasa rejimi (BTC, 4s)
- **BULL**: tam pozisyon boyutu
- **NEUTRAL**: pozisyon boyutu yarıya iner, minimum puan +5
- **BEAR / HIGH_VOLATILITY**: yeni işlem **açılmaz**

### Beklenen net avantaj
Her işlemden önce komisyon (`FEE_RATE`, LIVE'da hesabınızdan okunur) ve slippage dahil beklenen
net getiri hesaplanır. Hedef hareket maliyetin en az 3 katı değilse veya beklenen net getiri
`MIN_EXPECTED_EDGE_PCT` altındaysa işlem açılmaz. (Bu bir sezgisel tahmindir, garanti değildir.)

### Risk yönetimi (varsayılan)
| Kural | Değer |
|---|---|
| Maksimum tek pozisyon | portföyün %20'si |
| Aynı anda açık pozisyon | 1 |
| İşlem başına risk | portföyün %1'i (komisyon+slippage dahil) |
| Günlük maksimum zarar | %3 (aşılırsa yeni işlem yok) |
| Günlük maksimum yeni işlem | 5 |
| Ardışık kayıp limiti | 3 → 12 saat soğuma |
| Martingale / kontrolsüz DCA | yok |

### Stop ve çıkış
- Her pozisyonda **zorunlu ATR tabanlı stop** (giriş − 2×ATR).
- LIVE modda stop ayrıca Binance'e **STOP_LOSS_LIMIT** emri olarak konur (bot kapansa bile koruma).
  Stop emri konamazsa pozisyon güvenlik için hemen kapatılır.
- Stop **asla genişletilmez**, sadece yukarı taşınır.
- Fiyat +1×ATR → stop **başa baş** (maliyetler dahil) seviyesine.
- Fiyat +1.5×ATR → **ATR trailing stop** (en yüksek − 2×ATR).
- Trend bozulursa (fiyat ve EMA20 < EMA50) veya momentum ciddi negatife dönerse çıkış.

### Mevcut portföy ve rotasyon
- Bot başlarken Spot hesabındaki tüm varlıkları okur ve USDT karşılığını hesaplar.
- Her varlık için **HOLD / PARTIAL_SELL / SELL** kararı üretilir ve panelde gösterilir.
- Mevcut coinler bot başladı diye **satılmaz**. Kararların uygulanması için LIVE modda
  `MANAGE_EXISTING_HOLDINGS=true` olmalı ve aynı karar art arda 3 taramada tekrarlanmalıdır.
- Rotasyon yalnızca puan farkı **≥ 15** ise ve ek komisyon maliyetine rağmen beklenen avantaj
  varsa yapılır; küçük farklarda gereksiz coin değişimi yapılmaz.

### Binance kuralları
`exchangeInfo` üzerinden `LOT_SIZE`, `MARKET_LOT_SIZE`, `PRICE_FILTER`, `MIN_NOTIONAL` / `NOTIONAL`,
`stepSize` ve `tickSize` otomatik okunur. Miktar ve fiyatlar her zaman aşağı yuvarlanır ve doğrulanır.

### Yeniden başlatma
Açık pozisyonlar SQLite'ta saklanır. Bot yeniden başladığında:
- Stop emri kapalıyken tetiklenmişse işlem kapatılmış olarak kaydedilir.
- Varlık hesapta yoksa (elle satılmışsa) harici kapanış olarak işlenir.
- Miktar azalmışsa güncellenir; eksik stop emri yeniden konur.

## Web panel

**http://127.0.0.1:8000** (Docker'da da aynı adres)

Gösterilenler: bot durumu, DRY_RUN/LIVE, toplam portföy, USDT, bugünkü ve toplam PNL, açık pozisyon
(alış, güncel fiyat, stop, trailing stop, K/Z), en yüksek puanlı coinler, mevcut varlık kararları,
son işlemler, karar günlüğü ve **botun neden işlem açmadığı**.

Butonlar:
- **Botu Başlat / Botu Durdur**
- **Acil Durdur**: botu durdurur ve yeniden başlatmada da geçerli kalır. Açık pozisyonlar ve
  borsadaki koruyucu stoplar korunur. Sıfırlamak için panelde "Acil durumu sıfırla".
- **Tüm Pozisyonları Kapat**: iki aşamalı onay ister (`TUM POZISYONLARI KAPAT` yazılmalı).

API uç noktaları: `GET /api/status`, `/api/trades`, `/api/signals`, `/api/logs`, `/api/daily-stats`,
`POST /api/start`, `/api/stop`, `/api/emergency-stop`, `/api/emergency-reset`, `/api/close-all`.

## Veritabanı

SQLite (`data/bot.db`): işlemler, sinyaller, karar günlükleri, PNL anlık görüntüleri, komisyonlar,
bot durumu ve günlük istatistikler.

## Testler

```bash
pytest -q
```

Pozisyon boyutu, işlem başına risk, günlük zarar limiti, günlük işlem limiti, ardışık kayıp soğuması,
LOT_SIZE, MIN_NOTIONAL, tick size, stop-loss, trailing stop, komisyon hesabı, acil durdurma,
restart sonrası pozisyon senkronizasyonu, rotasyon ve panel uç noktaları test edilir.
Testler ağ bağlantısı gerektirmez (sahte Binance istemcisi kullanılır).

## Proje yapısı

```
app/            main.py (giriş), config.py (ayarlar), static/index.html (panel)
api/            routes.py (FastAPI uç noktaları)
bot/            engine, scanner, strategy, risk_manager, portfolio_manager, order_manager, market_regime
binance_client/ client.py (resmi binance-connector), filters.py (exchangeInfo filtreleri)
database/       models.py, database.py (SQLAlchemy + SQLite)
tests/          pytest testleri
```
