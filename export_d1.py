# -*- coding: utf-8 -*-
"""
Выгрузка находок в SQL для Cloudflare D1: python export_d1.py

Зачем отдельный формат, а не копия рабочей базы. В локальной базе живёт
история замеров — по строке на каждый опрос каждого кандидата, и растёт она
быстро. Боту эта история не нужна: он показывает готовый результат, а не
считает. Поэтому сюда уезжает ПЛОСКИЙ срез — по строке на находку с уже
посчитанным баллом и последними метриками. Так таблица в D1 остаётся
маленькой, а лимиты бесплатного тарифа не при чём.

Скрипт вызывается из GitHub Actions после прогона, результат заливается
командой `wrangler d1 execute --file`.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import db                  # noqa: E402
import score as scoring    # noqa: E402
from common import load_env, setup_logging  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    item_id          INTEGER PRIMARY KEY,
    source           TEXT,
    url              TEXT,
    product_url      TEXT,
    domain           TEXT,
    domain_age_days  INTEGER,
    title            TEXT,
    body             TEXT,
    author           TEXT,
    author_followers INTEGER,
    posted_at        INTEGER,
    first_seen       INTEGER,
    score            REAL,
    tier             TEXT,
    breakdown        TEXT,
    likes            INTEGER,
    replies          INTEGER,
    reposts          INTEGER,
    bookmarks        INTEGER,
    views            INTEGER
);
CREATE INDEX IF NOT EXISTS findings_score ON findings (score DESC);
CREATE INDEX IF NOT EXISTS findings_seen  ON findings (first_seen DESC);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, updated INTEGER);

-- Кому открыт доступ по коду. Таблица НЕ очищается при заливке (в отличие
-- от findings): выданный доступ должен переживать обновление находок,
-- иначе друг терял бы его каждые десять минут.
CREATE TABLE IF NOT EXISTS access (
    chat_id    TEXT PRIMARY KEY,
    who        TEXT,
    granted_at INTEGER
);
CREATE TABLE IF NOT EXISTS access_tries (
    chat_id      TEXT PRIMARY KEY,
    tries        INTEGER,
    locked_until INTEGER
);
"""


def lit(v):
    """Значение в SQL-литерал. Одинарные кавычки удваиваются."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def breakdown_ru(b):
    """Разбор балла одной строкой — Worker показывает его как есть."""
    if not b:
        return None
    parts = []
    for k, v in sorted(b.items(), key=lambda kv: -abs(kv[1]) if isinstance(kv[1], (int, float)) else 0):
        sign = "+" if isinstance(v, (int, float)) and v >= 0 else ""
        parts.append("%s %s%s" % (k, sign, v))
    return " · ".join(parts)[:600]


def main():
    setup_logging("export")
    ap = argparse.ArgumentParser(description="срез находок в SQL для D1")
    ap.add_argument("--out", default="out/d1-import.sql")
    ap.add_argument("--window-hours", type=int, default=168,
                    help="какой возраст находок выгружать (по умолчанию неделя)")
    ap.add_argument("--limit", type=int, default=600)
    args = ap.parse_args()

    load_env()
    conn = db.connect()
    now = int(time.time())
    cutoff = now - args.window_hours * 3600

    rows = conn.execute(
        "SELECT * FROM items WHERE first_seen >= ? "
        "ORDER BY first_seen DESC LIMIT ?", (cutoff, args.limit)).fetchall()

    lines = [SCHEMA.strip(), ""]
    # Полная замена среза, а не дозапись: балл у находки меняется от прогона
    # к прогону (метрики растут), и дозапись оставляла бы в боте устаревшие
    # значения рядом со свежими.
    lines.append("DELETE FROM findings;")

    kept = 0
    for item in rows:
        total, tier, b = scoring.score_item(conn, item, now)
        if total <= 0:
            continue
        last = conn.execute(
            "SELECT * FROM metrics WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
            (item["item_id"],)).fetchone()
        m = dict(last) if last else {}
        vals = [item["item_id"], item["source"], item["url"], item["product_url"],
                item["domain"], item["domain_age_days"], item["title"],
                (item["body"] or "")[:900], item["author"], item["author_followers"],
                item["posted_at"], item["first_seen"], round(total, 1), tier,
                breakdown_ru(b), m.get("likes"), m.get("replies"), m.get("reposts"),
                m.get("bookmarks"), m.get("views")]
        lines.append(
            "INSERT INTO findings (item_id, source, url, product_url, domain, "
            "domain_age_days, title, body, author, author_followers, posted_at, "
            "first_seen, score, tier, breakdown, likes, replies, reposts, "
            "bookmarks, views) VALUES (%s);" % ", ".join(lit(v) for v in vals))
        kept += 1

    lines.append("INSERT OR REPLACE INTO meta (k, updated) VALUES ('import', %d);" % now)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("выгружено находок: %d -> %s" % (kept, out))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
