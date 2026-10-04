"""Telegram ile telefondan takip: bildirimler ve basit komutlar.

- Bildirim: alım, satış, kısmi kâr, güvenlik uyarıları, hatalar, bot başlat/durdur.
- Komutlar (yalnızca TELEGRAM_CHAT_ID'deki sohbetten kabul edilir):
    /durum         genel durum
    /pozisyon      açık pozisyon ayrıntısı
    /acil EVET     acil durdurma (yeni işlem açılmaz, borsadaki stoplar korunur)
    /yardim        komut listesi
- TELEGRAM_CHAT_ID boşsa bota yazılan ilk mesajın sohbet numarası yanıt olarak gönderilir;
  bu numara .env'ye yazılınca bildirimler başlar.

Telegram'a erişilemezse bot çalışmaya devam eder; bildirim hataları yalnızca loglanır.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import requests

logger = logging.getLogger(__name__)

NOTIFY_CATEGORIES = {"TRADE", "TAKE_PROFIT", "SAFETY", "EMERGENCY", "BOT", "ROTATION", "HOLDING"}
ICONS = {"TRADE": "💱", "TAKE_PROFIT": "💰", "SAFETY": "🛡️", "EMERGENCY": "⛔", "BOT": "🤖",
         "ROTATION": "🔄", "HOLDING": "📦", "ERROR": "⚠️", "STOP": "📈", "ACCOUNT": "⚠️"}


class TelegramNotifier:
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str = "", commands_enabled: bool = True,
                 session: requests.Session | None = None, timeout: int = 15):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.commands_enabled = commands_enabled
        self.timeout = timeout
        self._session = session or requests.Session()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._offset = 0
        self._last_errors: dict[str, float] = {}
        self.status_provider: Callable[[], dict] | None = None
        self.emergency_handler: Callable[[], None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # --- yaşam döngüsü ---
    def start(self) -> None:
        if not self.enabled or self._threads:
            return
        self._stop.clear()
        sender = threading.Thread(target=self._send_loop, name="telegram-send", daemon=True)
        sender.start()
        self._threads.append(sender)
        if self.commands_enabled:
            poller = threading.Thread(target=self._poll_loop, name="telegram-poll", daemon=True)
            poller.start()
            self._threads.append(poller)

    def stop(self) -> None:
        self._stop.set()
        self._threads = []

    # --- gönderim ---
    def notify(self, text: str) -> None:
        if not self.enabled or not self.chat_id:
            return
        try:
            self._queue.put_nowait(text[:3900])
        except queue.Full:
            logger.warning("Telegram bildirim kuyruğu dolu; mesaj atlandı")

    def on_log(self, category: str, message: str, level: str = "INFO") -> None:
        """Karar günlüğü dinleyicisi: önemli olayları telefona gönderir."""
        if level == "ERROR":
            # aynı hatayı 30 dakikada bir kereden fazla gönderme
            now = time.time()
            key = f"{category}:{message[:80]}"
            if now - self._last_errors.get(key, 0) < 1800:
                return
            self._last_errors[key] = now
            self.notify(f"{ICONS.get('ERROR')} {category}: {message}")
        elif category == "SAFETY" and "doğrulanamıyor" in message:
            return  # her açılışta tekrarlanan hatırlatma; telefona gönderilmez
        elif category in NOTIFY_CATEGORIES:
            self.notify(f"{ICONS.get(category, '•')} {message}")
        elif category == "STOP" and ("başa baş" in message or "yerleştirildi" in message):
            self.notify(f"{ICONS['STOP']} {message}")

    def send_now(self, text: str, chat_id: str | None = None) -> bool:
        target = chat_id or self.chat_id
        if not self.enabled or not target:
            return False
        try:
            resp = self._session.post(f"{self.API}/bot{self.token}/sendMessage",
                                      data={"chat_id": target, "text": text}, timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.warning("Telegram mesajı gönderilemedi: %s", exc)
            return False

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if not self.send_now(text):
                time.sleep(5)

    # --- komutlar ---
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self._session.get(f"{self.API}/bot{self.token}/getUpdates",
                                         params={"offset": self._offset, "timeout": 25},
                                         timeout=self.timeout + 25)
                data = resp.json() if resp.status_code == 200 else {}
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Telegram güncellemeleri alınamadı: %s", exc)
                time.sleep(10)
                continue
            for update in data.get("result", []):
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                self.handle_update(update)

    def handle_update(self, update: dict) -> str | None:
        msg = update.get("message") or update.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return None
        if not self.chat_id:
            reply = (f"Sohbet numaranız: {chat}\n.env dosyasına TELEGRAM_CHAT_ID={chat} yazıp botu "
                     f"yeniden başlatın.")
            self.send_now(reply, chat_id=chat)
            return reply
        if chat != self.chat_id:
            logger.warning("Yetkisiz Telegram sohbetinden komut yok sayıldı: %s", chat)
            return None
        reply = self.handle_command(text)
        if reply:
            self.send_now(reply)
        return reply

    def handle_command(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        status = self.status_provider() if self.status_provider else {}
        if cmd in ("/durum", "/status", "/start"):
            return format_status(status)
        if cmd in ("/pozisyon", "/pozisyonlar"):
            return format_positions(status)
        if cmd == "/acil":
            if len(parts) > 1 and parts[1].upper() == "EVET":
                if self.emergency_handler:
                    self.emergency_handler()
                return ("⛔ ACİL DURDURMA etkin. Yeni işlem açılmayacak; borsadaki stop emirleri "
                        "korunuyor. Sıfırlamak için paneli kullanın.")
            return "Acil durdurmak için şunu yazın:\n/acil EVET"
        return ("Komutlar:\n/durum - genel durum\n/pozisyon - açık pozisyon\n"
                "/acil EVET - acil durdurma\n/yardim - bu liste")


def _num(v, d=2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_status(s: dict) -> str:
    if not s:
        return "Durum bilgisi yok."
    q = s.get("quote_asset", "USDT")
    lines = [
        f"🤖 {'ÇALIŞIYOR' if s.get('running') else 'DURDU'} | {s.get('mode')} | {s.get('exchange', '')}",
        f"Rejim: {s.get('regime') or '?'}" + (" | ⛔ ACİL DURDURMA" if s.get("emergency_stop") else ""),
        f"Portföy: {_num(s.get('equity_usdt'))} USDT",
        f"Serbest {q}: {_num(s.get('quote_free'))}",
        f"Bugünkü PNL: {_num(s.get('daily_pnl'))} {q} | Toplam: {_num(s.get('total_pnl'))} {q}",
        f"Bugünkü işlem: {s.get('trades_today', 0)} | Ardışık kayıp: {s.get('consecutive_losses', 0)}",
        f"Açık pozisyon: {len(s.get('positions') or [])}",
        f"Son karar: {s.get('no_trade_reason', '')}",
    ]
    if s.get("last_error"):
        lines.append(f"⚠️ Son hata: {s['last_error']}")
    return "\n".join(lines)


def format_positions(s: dict) -> str:
    positions = (s or {}).get("positions") or []
    if not positions:
        return "Açık pozisyon yok."
    q = s.get("quote_asset", "USDT")
    out = []
    for p in positions:
        trail = "aktif" if p.get("trailing_active") else ("başa baş" if p.get("breakeven_active") else "pasif")
        out.append(
            f"📊 {p['symbol']}{' (yarısı satıldı)' if p.get('partial_taken') else ''}\n"
            f"Miktar: {p['quantity']:g}\nAlış: {p['entry_price']:g} | Güncel: {p['current_price']:g}\n"
            f"Stop: {p['stop_price']:.6g} (trailing {trail})\n"
            f"K/Z: {_num(p.get('pnl_usdt'))} {q} ({_num(p.get('pnl_pct'))}%)"
        )
    return "\n\n".join(out)


def build_notifier(settings) -> TelegramNotifier | None:
    token = settings.telegram_bot_token.get_secret_value()
    if not token.strip():
        return None
    return TelegramNotifier(token, settings.telegram_chat_id, settings.telegram_commands_enabled,
                            timeout=settings.binance_timeout_seconds)
