"""İşlem kayıtlarını inceleme ve güvenli onarım aracı.

Hiçbir komut varsayılan olarak veritabanını DEĞİŞTİRMEZ: önce yapılacak değişikliği gösterir.
Değişiklik için --apply gerekir; uygulamadan önce otomatik yedek alınır. Kayıt SİLİNMEZ.

Kullanım (proje klasöründe, BOT KAPALIYKEN):
  python -m tools.repair_trades list
  python -m tools.repair_trades show --id 7
  python -m tools.repair_trades backup
  python -m tools.repair_trades reopen --id 7 --quantity 5045.9          (önizleme)
  python -m tools.repair_trades reopen --id 7 --quantity 5045.9 --apply  (uygula)
  python -m tools.repair_trades set-exit --id 7 --exit-quote 1650.25 --exit-price 0.3268 --apply
  python -m tools.repair_trades clear-review --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.join("data", "bot.db")


def backup(db_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = f"{db_path}.yedek-{stamp}"
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    src.close()
    out.close()
    return dst


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        sys.exit(f"Veritabanı bulunamadı: {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


COLS = ("id, symbol, mode, status, quantity, entry_price, entry_quote, entry_time, exit_price, "
        "exit_quote, exit_time, exit_reason, pnl_usdt, pnl_pct, stop_order_id, notes")


def fmt(r: sqlite3.Row) -> str:
    return (f"#{r['id']} {r['symbol']} [{r['mode']}] {r['status']} miktar={r['quantity']:.8g} "
            f"alış={r['entry_price']:.8g} maliyet={r['entry_quote']:.4f} "
            f"çıkış={r['exit_price'] if r['exit_price'] is not None else '-'} "
            f"gelir={r['exit_quote'] if r['exit_quote'] is not None else '-'} "
            f"PNL={r['pnl_usdt'] if r['pnl_usdt'] is not None else '-'} "
            f"sebep={r['exit_reason'] or '-'} giriş={r['entry_time']}")


def get_trade(con: sqlite3.Connection, trade_id: int) -> sqlite3.Row:
    row = con.execute(f"SELECT {COLS} FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        sys.exit(f"#{trade_id} numaralı işlem bulunamadı")
    return row


def suspicious(r: sqlite3.Row) -> list[str]:
    notes = []
    if r["entry_quote"] and r["entry_price"] and r["quantity"] * r["entry_price"] < 0.5 * r["entry_quote"]:
        notes.append("miktar maliyetle tutarsız")
    if r["status"] == "CLOSED" and r["pnl_pct"] is not None and r["pnl_pct"] <= -90:
        notes.append("%90+ zarar")
    return notes


def cmd_list(con, args) -> int:
    rows = con.execute(f"SELECT {COLS} FROM trades ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    for r in rows:
        flag = suspicious(r)
        print(fmt(r) + (f"   <-- ŞÜPHELİ: {', '.join(flag)}" if flag else ""))
    review = con.execute("SELECT value FROM bot_state WHERE key = 'review_required'").fetchone()
    if review and review[0] not in (None, "[]", "null"):
        print(f"\nİnceleme bayrakları: {review[0]}")
    return 0


def cmd_show(con, args) -> int:
    r = get_trade(con, args.id)
    for k in r.keys():
        print(f"{k:16s}: {r[k]}")
    flag = suspicious(r)
    if flag:
        print(f"\nŞÜPHELİ: {', '.join(flag)}")
    return 0


def _apply(con, db_path, args, sql, params, summary) -> int:
    print(summary)
    if not args.apply:
        print("\n(ÖNİZLEME) Değişiklik yapılmadı. Uygulamak için aynı komutu --apply ile çalıştırın.")
        return 0
    dst = backup(db_path)
    print(f"Yedek alındı: {dst}")
    with con:
        con.execute(sql, params)
    print("Uygulandı.")
    return 0


def cmd_reopen(con, db_path, args) -> int:
    """Yanlışlıkla kapatılmış, ama coinleri hesapta duran bir işlemi tekrar açar."""
    r = get_trade(con, args.id)
    if r["status"] != "CLOSED":
        sys.exit("Yalnızca CLOSED işlemler tekrar açılabilir")
    if args.quantity <= 0:
        sys.exit("--quantity pozitif olmalı (Binance TR'deki gerçek miktar)")
    entry_quote = args.entry_quote if args.entry_quote is not None else r["entry_quote"]
    open_same = con.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN' AND mode=?",
                            (r["mode"],)).fetchone()[0]
    if open_same:
        print(f"UYARI: '{r['mode']}' defterinde zaten {open_same} açık işlem var.")
    implied = entry_quote / args.quantity
    if abs(implied / r["entry_price"] - 1) > 0.05:
        print(f"UYARI: maliyet/miktar ({implied:.8g}) kayıtlı alış fiyatından ({r['entry_price']:.8g}) "
              f"%5'ten fazla farklı. Miktarı ve maliyeti tekrar kontrol edin.")
    note = (f"{r['notes'] or ''} | {datetime.now():%Y-%m-%d %H:%M} onarım: tekrar açıldı "
            f"(eski miktar={r['quantity']:.8g}, eski PNL={r['pnl_usdt']}, eski sebep={r['exit_reason']})")
    summary = (f"Önce : {fmt(r)}\nSonra: #{r['id']} {r['symbol']} OPEN miktar={args.quantity:.8g} "
               f"maliyet={entry_quote:.4f} (çıkış/PNL alanları temizlenir, borsa stopu bot açılınca konur)")
    sql = ("UPDATE trades SET status='OPEN', quantity=?, entry_quote=?, exit_price=NULL, exit_quote=NULL, "
           "exit_fee_usdt=0, exit_external_fee_usdt=0, exit_time=NULL, exit_order_id=NULL, exit_reason=NULL, "
           "pnl_usdt=NULL, pnl_pct=NULL, stop_order_id=NULL, stop_order_price=NULL, breakeven_active=0, "
           "trailing_active=0, notes=? WHERE id=?")
    return _apply(con, db_path, args, sql, (args.quantity, entry_quote, note, r["id"]), summary)


def cmd_set_exit(con, db_path, args) -> int:
    """Kapalı bir işlemin çıkışını gerçek satış raporuna göre düzeltir (PNL yeniden hesaplanır)."""
    r = get_trade(con, args.id)
    if r["status"] != "CLOSED":
        sys.exit("Yalnızca CLOSED işlemlerin çıkışı düzeltilebilir")
    ext = con.execute("SELECT entry_external_fee_usdt, exit_external_fee_usdt FROM trades WHERE id=?",
                      (r["id"],)).fetchone()
    pnl = args.exit_quote - r["entry_quote"] - (ext[0] or 0.0)
    pct = pnl / r["entry_quote"] * 100 if r["entry_quote"] else 0.0
    exit_price = args.exit_price if args.exit_price is not None else r["exit_price"]
    note = (f"{r['notes'] or ''} | {datetime.now():%Y-%m-%d %H:%M} onarım: çıkış düzeltildi "
            f"(eski gelir={r['exit_quote']}, eski PNL={r['pnl_usdt']})")
    summary = (f"Önce : {fmt(r)}\nSonra: gelir={args.exit_quote:.4f} çıkış fiyatı={exit_price} "
               f"PNL={pnl:.4f} ({pct:.2f}%)")
    sql = ("UPDATE trades SET exit_quote=?, exit_price=?, exit_external_fee_usdt=0, pnl_usdt=?, pnl_pct=?, "
           "notes=? WHERE id=?")
    return _apply(con, db_path, args, sql, (args.exit_quote, exit_price, pnl, pct, note, r["id"]), summary)


def cmd_clear_review(con, db_path, args) -> int:
    row = con.execute("SELECT value FROM bot_state WHERE key = 'review_required'").fetchone()
    summary = f"Mevcut inceleme bayrakları: {row[0] if row else '[]'}"
    sql = "UPDATE bot_state SET value='[]' WHERE key='review_required'"
    return _apply(con, db_path, args, sql, (), summary)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="İşlem kayıtlarını inceleme/onarma (varsayılan: önizleme)")
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("show"); s.add_argument("--id", type=int, required=True)
    sub.add_parser("backup")
    s = sub.add_parser("reopen")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--entry-quote", type=float, default=None)
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("set-exit")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--exit-quote", type=float, required=True)
    s.add_argument("--exit-price", type=float, default=None)
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("clear-review"); s.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)

    con = connect(args.db)
    try:
        if args.cmd == "list":
            return cmd_list(con, args)
        if args.cmd == "show":
            return cmd_show(con, args)
        if args.cmd == "backup":
            print(f"Yedek alındı: {backup(args.db)}")
            return 0
        if args.cmd == "reopen":
            return cmd_reopen(con, args.db, args)
        if args.cmd == "set-exit":
            return cmd_set_exit(con, args.db, args)
        if args.cmd == "clear-review":
            return cmd_clear_review(con, args.db, args)
    finally:
        con.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
