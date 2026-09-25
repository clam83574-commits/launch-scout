# -*- coding: utf-8 -*-
"""
База скаута: кандидаты, замеры метрик во времени, оценки, отправленное.

Главное архитектурное решение здесь — таблица `metrics` пишется ПРИ КАЖДОМ
прогоне и НИКОГДА не перезаписывается. Без истории замеров нельзя посчитать
скорость (лайков в час) и ускорение — а именно они, а не абсолютное число
лайков, отличают «взлетает прямо сейчас» от «висит вторые сутки».
Один снимок состояния бесполезен: 500 лайков за 40 минут и 500 лайков за
двое суток выглядят одинаково.

Таблица `baseline` хранит норму автора — медиану его последних постов.
Она нужна ровно для одного: понять, что для ЭТОГО человека нынешний пост
аномален. Пост на 300 лайков у того, кто обычно собирает 20, — событие;
у того, кто собирает 5000, — провал.
"""
import sqlite3
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "scout.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id          INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,       -- x | hn | yc | gh | ph
    ext_id           TEXT NOT NULL,       -- идентификатор внутри источника
    url              TEXT,                -- ссылка на пост/страницу источника
    product_url      TEXT,                -- ссылка на сам продукт
    domain           TEXT,                -- домен продукта (ключ дедупликации)
    title            TEXT,
    body             TEXT,
    author           TEXT,
    author_followers INTEGER,
    posted_at        INTEGER,             -- unix, время публикации
    first_seen       INTEGER,             -- unix, когда мы это впервые увидели
    domain_age_days  INTEGER,             -- по RDAP; NULL = не проверяли
    tags             TEXT,
    raw              TEXT,
    -- 1 = запись пришла первым (посевным) прогоном источника.
    -- Без этой пометки первый запуск объявил бы новинкой ВЕСЬ каталог YC
    -- разом: 700 компаний, накопленных за годы, получили бы first_seen
    -- «сейчас» и улетели владельцу семьюстами уведомлениями.
    bootstrap        INTEGER DEFAULT 0,
    UNIQUE (source, ext_id)
);

CREATE INDEX IF NOT EXISTS items_domain  ON items (domain);
CREATE INDEX IF NOT EXISTS items_seen    ON items (first_seen);
CREATE INDEX IF NOT EXISTS items_posted  ON items (posted_at);

-- Временной ряд. Строка на каждый замер, старые не трогаем.
CREATE TABLE IF NOT EXISTS metrics (
    item_id   INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    likes     INTEGER,   -- X: лайки | HN: очки | PH: голоса | GH: звёзды
    replies   INTEGER,
    reposts   INTEGER,
    quotes    INTEGER,
    bookmarks INTEGER,
    views     INTEGER,
    PRIMARY KEY (item_id, ts)
);

CREATE TABLE IF NOT EXISTS scores (
    item_id   INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    score     REAL,
    tier      TEXT,      -- hot | digest | archive
    breakdown TEXT,      -- JSON: вклад каждого слагаемого, чтобы порог был объясним
    PRIMARY KEY (item_id, ts)
);

-- Что уже ушло в Telegram. Без этой таблицы один и тот же запуск
-- прилетал бы владельцу каждые полчаса, пока держится высокий балл.
CREATE TABLE IF NOT EXISTS sent (
    item_id INTEGER NOT NULL,
    tier    TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (item_id, tier)
);

-- Норма автора: медиана и MAD по его последним постам.
CREATE TABLE IF NOT EXISTS baseline (
    source       TEXT NOT NULL,
    author       TEXT NOT NULL,
    median_likes REAL,
    mad          REAL,
    n            INTEGER,
    updated      INTEGER,
    PRIMARY KEY (source, author)
);

-- Кэш RDAP: возраст домена меняется раз в год, дёргать его каждый прогон незачем.
CREATE TABLE IF NOT EXISTS domain_cache (
    domain     TEXT PRIMARY KEY,
    created_at INTEGER,   -- unix регистрации; NULL = RDAP не ответил
    checked_at INTEGER
);

-- Мелкое состояние между прогонами: когда ушла последняя сводка и т.п.
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Журнал прогонов: видно, какой источник молчит, не открывая логи.
CREATE TABLE IF NOT EXISTS runs (
    ts       INTEGER NOT NULL,
    source   TEXT NOT NULL,
    found    INTEGER,
    new_rows INTEGER,
    ok       INTEGER,
    note     TEXT,
    PRIMARY KEY (ts, source)
);
"""


def connect(path=None):
    """Открыть базу, создать схему, если её нет."""
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # WAL: сборщик пишет, а бот/ручные запросы читают в тот же момент.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """
    Дописать колонки, появившиеся после первых баз.

    CREATE TABLE IF NOT EXISTS не меняет существующую таблицу, поэтому у
    базы, созданной старой версией, новых колонок не будет — и код упадёт
    на первом же запросе к ним.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    if "bootstrap" not in have:
        conn.execute("ALTER TABLE items ADD COLUMN bootstrap INTEGER DEFAULT 0")
        conn.commit()


def source_seeded(conn, source):
    """Был ли у источника хоть один успешный прогон раньше."""
    row = conn.execute(
        "SELECT 1 FROM runs WHERE source = ? AND ok = 1 LIMIT 1",
        (source,)).fetchone()
    return row is not None


def upsert_item(conn, it):
    """
    Записать кандидата. Возвращает (item_id, is_new).

    Повторная встреча НЕ перетирает first_seen — по нему считается возраст
    находки, и обнуление сломало бы весь расчёт скорости.
    """
    cur = conn.execute(
        "SELECT item_id FROM items WHERE source = ? AND ext_id = ?",
        (it["source"], str(it["ext_id"])))
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE items SET url = COALESCE(?, url), "
            "product_url = COALESCE(?, product_url), "
            "domain = COALESCE(?, domain), title = COALESCE(?, title), "
            "body = COALESCE(?, body), author = COALESCE(?, author), "
            "author_followers = COALESCE(?, author_followers), "
            "posted_at = COALESCE(?, posted_at), tags = COALESCE(?, tags) "
            "WHERE item_id = ?",
            (it.get("url"), it.get("product_url"), it.get("domain"),
             it.get("title"), it.get("body"), it.get("author"),
             it.get("author_followers"), it.get("posted_at"),
             it.get("tags"), row["item_id"]))
        return row["item_id"], False

    cur = conn.execute(
        "INSERT INTO items (source, ext_id, url, product_url, domain, title, "
        "body, author, author_followers, posted_at, first_seen, tags, raw, bootstrap) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (it["source"], str(it["ext_id"]), it.get("url"), it.get("product_url"),
         it.get("domain"), it.get("title"), it.get("body"), it.get("author"),
         it.get("author_followers"), it.get("posted_at"), it["first_seen"],
         it.get("tags"), it.get("raw"), 1 if it.get("bootstrap") else 0))
    return cur.lastrowid, True


def add_metrics(conn, item_id, ts, m):
    """Дописать замер. INSERT OR REPLACE — на случай двух прогонов в одну секунду."""
    conn.execute(
        "INSERT OR REPLACE INTO metrics "
        "(item_id, ts, likes, replies, reposts, quotes, bookmarks, views) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (item_id, ts, m.get("likes"), m.get("replies"), m.get("reposts"),
         m.get("quotes"), m.get("bookmarks"), m.get("views")))


def log_run(conn, ts, source, found, new_rows, ok, note=""):
    conn.execute(
        "INSERT OR REPLACE INTO runs (ts, source, found, new_rows, ok, note) "
        "VALUES (?,?,?,?,?,?)", (ts, source, found, new_rows, 1 if ok else 0, note))


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, str(v)))


def already_sent(conn, item_id, tier):
    return conn.execute(
        "SELECT 1 FROM sent WHERE item_id = ? AND tier = ?",
        (item_id, tier)).fetchone() is not None


def mark_sent(conn, item_id, tier, ts):
    conn.execute("INSERT OR REPLACE INTO sent (item_id, tier, ts) VALUES (?,?,?)",
                 (item_id, tier, ts))
