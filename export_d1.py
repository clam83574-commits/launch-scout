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


def snapshot(conn, now, window_hours=168, top_all=200, top_day=150):
    """
    Срез для бота одним JSON: лучшие за неделю плюс лучшие за сутки.

    Зачем объединение двух выборок: кнопка «За сутки» показывает свежее, а
    лучшие за неделю почти целиком — это вчерашние и позавчерашние находки.
    Бери только их — и свежих в срезе не хватило бы на одну страницу.

    Зачем один JSON, а не строки таблицы: Worker кладёт его в D1 ОДНИМ
    запросом. Построчная заливка — это сотни запросов за вызов, а у
    бесплатного Workers счёт запросов к D1 на вызов ограничен.
    """
    rows = conn.execute(
        "SELECT * FROM items WHERE first_seen >= ?",
        (now - window_hours * 3600,)).fetchall()
    scored = []
    for item in rows:
        total, tier, b = scoring.score_item(conn, item, now)
        if total <= 0:
            continue
        scored.append((total, tier, b, item))
    scored.sort(key=lambda t: -t[0])
    day = [s for s in scored if s[3]["first_seen"] >= now - 86400]
    picked, seen = [], set()
    for total, tier, b, item in scored[:top_all] + day[:top_day]:
        if item["item_id"] in seen:
            continue
        seen.add(item["item_id"])
        last = conn.execute(
            "SELECT * FROM metrics WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
            (item["item_id"],)).fetchone()
        m = dict(last) if last else {}
        picked.append({
            "id": item["item_id"], "source": item["source"], "url": item["url"],
            "product_url": item["product_url"], "domain": item["domain"],
            "domain_age_days": item["domain_age_days"],
            "title": (item["title"] or "")[:200], "body": (item["body"] or "")[:420],
            "author": item["author"], "author_followers": item["author_followers"],
            "first_seen": item["first_seen"], "score": round(total, 1), "tier": tier,
            "breakdown": breakdown_ru(b), "likes": m.get("likes"),
            "replies": m.get("replies"), "reposts": m.get("reposts"),
            "bookmarks": m.get("bookmarks"), "views": m.get("views"),
        })
    return picked


def main():
    setup_logging("export")
    ap = argparse.ArgumentParser(description="срез находок для бота (D1)")
    ap.add_argument("--json", help="записать срез одним JSON (путь) — основной режим")
    ap.add_argument("--out", default="out/d1-import.sql")
    ap.add_argument("--window-hours", type=int, default=168,
                    help="какой возраст находок выгружать (по умолчанию неделя)")
    ap.add_argument("--limit", type=int, default=600)
    args = ap.parse_args()

    if args.json:
        load_env()
        conn = db.connect()
        now = int(time.time())
        data = {"updated": now, "findings": snapshot(conn, now, args.window_hours)}
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        print("срез: %d находок, %d КБ -> %s"
              % (len(data["findings"]), out.stat().st_size // 1024, out))
        conn.close()
        return 0

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
