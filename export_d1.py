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

import ai                  # noqa: E402
import db                  # noqa: E402
import market              # noqa: E402
import os                  # noqa: E402
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
            "ai": _ai_brief(conn, item["item_id"]),
            "topics": _topics(conn, item["item_id"]),
            "sectors": market.sectors_for(_topics(conn, item["item_id"]),
                                          "%s %s" % (item["title"] or "", item["body"] or "")),
            "audience": _meta(conn, item["item_id"], "audience", []),
            "gist": _meta(conn, item["item_id"], "gist", {}),
        })
    return picked


def source_health(conn, now, days=7):
    """
    По каждому источнику: когда последний раз работал, сколько прогонов
    подряд с ошибкой, последняя ошибка. Бот по этому предупреждает, что
    источник отвалился, и сообщает, когда он вернулся.
    """
    out = {}
    rows = conn.execute("SELECT ts, source, found, ok, note FROM runs WHERE ts >= ? ORDER BY ts DESC",
                        (now - days * 86400,)).fetchall()
    for r in rows:
        h = out.setdefault(r["source"], {"last_ts": r["ts"], "last_ok": None, "fails": 0, "err": "", "found": r["found"], "_streak": True})
        if r["ok"]:
            if h["last_ok"] is None:
                h["last_ok"] = r["ts"]
            h["_streak"] = False
        elif h["_streak"]:
            h["fails"] += 1
            if not h["err"]:
                h["err"] = (r["note"] or "")[:200]
    for h in out.values():
        h.pop("_streak", None)
    return out


def market_block(conn, now):
    """
    Рынок для бота: отчёт в цифрах, готовый текст на трёх языках, названия
    секторов и аналитические заголовки по каждому. Карточку сектора Worker
    собирает сам — из этих цифр и находок среза с тем же сектором.
    """
    rep = market.last_report(conn)
    if not rep:
        return None
    try:
        analysis = json.loads(db.kv_get(conn, "market_analysis", "{}") or "{}")
    except ValueError:
        analysis = {}
    texts = {}
    for lang in ai.LANGS:
        # Вывод модели — только из кэша: срез выгружается каждые 10 минут,
        # а запрос к модели делает market_step раз в 12 часов.
        texts[lang] = market.render(rep, lang, db.kv_get(conn, "market_story_" + lang))
    try:
        chat = market.chat_facts(conn, now)
    except Exception as e:       # чат — надстройка: без фактов он просто скажет, что данных нет
        print("факты для чата не собраны: %s" % e)
        chat = None
    return {"report": rep, "texts": texts, "analysis": analysis, "chat": chat,
            "sectors": [{"id": sid, "emoji": market.SECTOR[sid]["emoji"],
                         "names": market.SECTOR[sid]["names"]} for sid in market.SECTOR_IDS],
            "physical": list(market.PHYSICAL)}


def _topics(conn, item_id):
    """Темы находки (для категорий в боте и приложении) или []."""
    try:
        row = conn.execute("SELECT topics FROM item_topics WHERE item_id = ?",
                           (item_id,)).fetchone()
    except Exception:
        return []
    if not row or not row["topics"]:
        return []
    try:
        return [x for x in json.loads(row["topics"]) if x != "other"]
    except ValueError:
        return []


def _ai_brief(conn, item_id):
    """
    Разбор для карточки: общие поля + тексты на каждом языке.
    Старые разборы (только русский, плоские) раскладываются под "ru".
    """
    note = ai.get_note(conn, item_id)
    if not note:
        return None
    brief = {k: note.get(k) for k in ("clone_effort", "kind", "business_potential", "audience")}
    if "i18n" in note:
        brief["i18n"] = note["i18n"]
    else:
        brief["i18n"] = {"ru": {k: note.get(k) for k in ("summary", "clone_note", "monetization")}}
    return brief


def _meta(conn, item_id, col, default):
    """Колонка из item_topics (audience / gist) как JSON или default."""
    try:
        row = conn.execute("SELECT %s FROM item_topics WHERE item_id = ?" % col,
                           (item_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else default
    except Exception:
        return default


def sync_rounds(conn, now, max_rows=3000):
    """
    Раунды за полгода — в таблицу rounds бота (POST /ingest-rounds), только
    изменившиеся с прошлой отправки: суточный лимит записи D1 общий на
    аккаунт с tm-scout, и слать все 10 тыс. строк каждые 10 минут нельзя.
    Отпечатки отправленного хранятся в своей базе (kv ds_hash).
    """
    import hashlib
    import os
    import requests
    url, secret = os.environ.get("WORKER_URL", "").strip(), os.environ.get("LS_INGEST_SECRET", "").strip()
    if not url or not secret:
        return 0, "нет WORKER_URL/LS_INGEST_SECRET"
    try:
        sent = json.loads(db.kv_get(conn, "ds_hash", "{}") or "{}")
    except ValueError:
        sent = {}
    rows = market.dataset_rows(conn, now)
    todo = []
    for r in rows:
        h = hashlib.sha1(json.dumps(r, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        if sent.get(r["key"]) != h:
            todo.append((r, h))
    todo = todo[:max_rows]
    done, err = 0, None
    for i in range(0, len(todo), 400):
        chunk = todo[i:i + 400]
        try:
            resp = requests.post(url.rstrip("/") + "/ingest-rounds", timeout=60,
                                 headers={"x-ingest-secret": secret, "content-type": "application/json"},
                                 json={"rows": [r for r, _h in chunk]})
        except requests.RequestException as e:
            err = str(e)[:120]
            break
        if resp.status_code != 200:
            err = "Worker %d: %s" % (resp.status_code, resp.text[:120])
            break
        for r, h in chunk:
            sent[r["key"]] = h
        done += len(chunk)
    keep = {r["key"] for r in rows}
    db.kv_set(conn, "ds_hash", json.dumps({k: v for k, v in sent.items() if k in keep}))
    conn.commit()
    return done, err


# Версия отпечатков матрицы: новая версия = отправить всё заново. v2 —
# после того как 497 ниш из 557 не попали в векторный индекс (2026-09-30).
MX_KEY = "mx_hash_v2"


def sync_matrix(conn, now, max_rows=600):
    """
    Матрица ниш — в бота (POST /ingest-matrix), только изменившиеся строки.
    Бот кладёт их в D1 и строит по ним векторный индекс (Workers AI +
    Vectorize), так что поиск по смыслу работает на любом языке.
    """
    import hashlib
    import os
    import requests
    url, secret = os.environ.get("WORKER_URL", "").strip(), os.environ.get("LS_INGEST_SECRET", "").strip()
    if not url or not secret:
        return 0, "нет WORKER_URL/LS_INGEST_SECRET"
    try:
        sent = json.loads(db.kv_get(conn, MX_KEY, "{}") or "{}")
    except ValueError:
        sent = {}
    rows = market.niche_matrix(conn, now)
    todo = []
    for r in rows:
        h = hashlib.sha1(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]
        if sent.get(r["niche"]) != h:
            todo.append((r, h))
    todo = todo[:max_rows]
    # Ниши, отправленные раньше, но пропавшие из матрицы (склеены с дублем,
    # мусорные ярлыки), — удалить в боте из базы и векторного индекса.
    current = {r["niche"] for r in rows}
    gone = [k for k in sent if k not in current] + ["unknown", "other"]
    done, err = 0, None
    if gone:
        try:
            resp = requests.post(url.rstrip("/") + "/ingest-matrix", timeout=150,
                                 headers={"x-ingest-secret": secret, "content-type": "application/json"},
                                 json={"rows": [], "delete": gone[:500]})
            if resp.status_code != 200:
                err = "удаление: Worker %d" % resp.status_code
        except requests.RequestException as e:
            err = "удаление: %s" % str(e)[:80]
    # По 20 строк: на 60 боевой бот (эмбеддинги + индекс) не укладывался в
    # 90 секунд (2026-09-30).
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        try:
            resp = requests.post(url.rstrip("/") + "/ingest-matrix", timeout=150,
                                 headers={"x-ingest-secret": secret, "content-type": "application/json"},
                                 json={"rows": [r for r, _h in chunk]})
        except requests.RequestException as e:
            err = str(e)[:120]
            break
        if resp.status_code != 200:
            err = "Worker %d: %s" % (resp.status_code, resp.text[:160])
            break
        for r, h in chunk:
            sent[r["niche"]] = h
        done += len(chunk)
    keep = {r["niche"] for r in rows}
    db.kv_set(conn, MX_KEY, json.dumps({k: v for k, v in sent.items() if k in keep}))
    conn.commit()
    return done, err


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
        # «Тренды» по нашим же находкам (trends.py) в срез больше не идут
        # (2026-09-28): это зеркало наших фильтров, а не рынка, и бот не
        # должен выдавать его за аналитику. Рынок строится на внешних данных
        # — market_block ниже. Остались только названия тем для фильтров.
        import trends
        data["trends"] = {"topic_names": trends.TOPIC_NAMES, "topic_ru": trends.TOPIC_RU}
        try:
            data["health"] = source_health(conn, now)
            data["pipeline_errors"] = [e for e in json.loads(db.kv_get(conn, "pipeline_errors", "[]") or "[]")
                                       if e[0] >= now - 86400]
        except Exception as e:  # здоровье — надстройка
            print("здоровье источников не выгружено: %s" % e)
        try:
            data["market"] = market_block(conn, now)
        except Exception as e:  # рынок — тоже надстройка
            print("рынок не выгружен: %s" % e)
        try:
            n, err = sync_rounds(conn, now)
            print("датасет раундов для чата: отправлено %d%s" % (n, (" — " + err) if err else ""))
        except Exception as e:      # датасет — надстройка: срез уходит в любом случае
            print("датасет раундов не отправлен: %s" % e)
        try:
            n, err = sync_matrix(conn, now)
            print("матрица ниш для чата: отправлено %d%s" % (n, (" — " + err) if err else ""))
        except Exception as e:      # матрица — надстройка
            print("матрица ниш не отправлена: %s" % e)
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
