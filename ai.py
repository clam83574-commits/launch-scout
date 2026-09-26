# -*- coding: utf-8 -*-
"""
ИИ-разбор находок через Groq (бесплатный тариф): выжимка на языке читателя,
суждение «продукт ли это» и темы для аналитики трендов.

ЗАЧЕМ. Скоринг видит скорость и отклик, но не смысл. Первая же живая
выдача (2026-09-25) это показала: из пяти горячих две — прямо в цель
(компании YC), а две — игрушки (шрифты, рыбки): «взлетает на HN» от
бизнеса по цифрам не отличить. Модель читает пост и отвечает, что это за
продукт, запуск ли это вообще и сколько займёт повторить.

ПОЧЕМУ GROQ. Владелец выбрал бесплатный вариант (2026-09-26). Бесплатный
ключ открывает openai/gpt-oss-120b, gpt-oss-20b и qwen/qwen3.8-27b
(проверено запросом к /models). У бесплатного тарифа суточные лимиты на
запросы и токены, поэтому:
  * разбор делается только для кандидатов в уведомление (десятки в сутки),
    и каждая находка разбирается один раз — результат кэшируется в базе;
  * темы для трендов ставятся ПАЧКОЙ, по 25 находок в запросе: поштучно
    весь поток (сотни записей в сутки) в квоту не влез бы;
  * на 429 шаг сразу останавливается до следующего прогона.

Без ключа GROQ_API_KEY слой молча выключен, остальная система работает.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
from common import UA  # noqa: E402

API = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_TAG_MODEL = "openai/gpt-oss-20b"
# Бесплатный Groq (замерено 2026-09-26 по заголовкам ответа): на КАЖДУЮ
# модель 1000 запросов в сутки и 8000 токенов в минуту. Узкое место —
# минута: разбор ~1.3 тыс. токенов, то есть ~6 разборов в минуту. Отсюда
# потолки за прогон ниже. Суточный предел ниже квоты — запас на ручные
# запуски и повторы.
DEFAULT_DAILY_MAX = 900
NOTES_PER_RUN = 6
TAGS_PER_RUN = 50

LANG_NAMES = {
    "ru": "Russian", "en": "English", "kk": "Kazakh", "uz": "Uzbek",
    "ar": "Arabic", "tr": "Turkish", "uk": "Ukrainian", "es": "Spanish",
    "de": "German", "fr": "French", "pt": "Portuguese", "id": "Indonesian",
}

KINDS = ["b2b_saas", "b2c_app", "dev_tool", "ai_agent", "marketplace",
         "hardware", "content_media", "fintech", "other"]
EFFORTS = ["days", "weeks", "months", "unclear"]
NICHE = ["ok", "borderline", "excluded"]

# Словарь тем для трендов. Закрытый список, а не свободные теги: иначе
# одно и то же дробится на «ai agents», «agents», «agentic workflows», и
# ни одна тема не набирает веса. Новое, чего в списке нет, модель пишет
# в new_topic — так всплывают действительно новые направления.
TOPICS = [
    "ai agents", "coding assistants", "voice ai", "video generation",
    "image generation", "chatbots & support", "open-source models",
    "local & on-device ai", "devtools", "testing & qa", "observability",
    "security", "databases", "infrastructure & cloud", "data & analytics",
    "browser automation", "no-code", "design tools", "creator tools",
    "productivity", "notes & knowledge", "email & calendar", "sales & crm",
    "marketing & seo", "e-commerce", "payments", "accounting & invoicing",
    "hr & recruiting", "legal", "health & fitness", "mental health",
    "education", "language learning", "real estate", "travel",
    "food & delivery", "social & community", "dating", "gaming",
    "robotics", "hardware", "climate & energy", "crypto infrastructure",
    "privacy", "other",
]

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "is_product_launch": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "monetization": {"type": "string"},
        "clone_effort": {"type": "string", "enum": EFFORTS},
        "clone_note": {"type": "string"},
        "niche": {"type": "string", "enum": NICHE},
    },
    "required": ["summary", "is_product_launch", "kind", "monetization",
                 "clone_effort", "clone_note", "niche"],
    "additionalProperties": False,
}

SYSTEM = """You review early signals of new products for a founder who looks for startup ideas to build quickly or to localize for another market.

For each item you get the source post and, when available, the product page title and description. Reply with JSON only, matching the schema.

Rules:
- summary: 2-3 plain sentences in the requested language. Say what the product does and who it is for. No hype, no marketing tone. If the post is too vague to tell, say so.
- is_product_launch: true only if a product, company or tool is being launched or shipped. Opinions, news reports, fundraising announcements without a product, memes, art commissions, game content updates and personal milestones are false.
- monetization: how it makes money if visible, in the requested language; otherwise "не видно" / "not visible".
- clone_effort: rough time for a small team to build a comparable first version: days, weeks, months, or unclear.
- clone_note: one line in the requested language naming the hardest part to replicate.
- niche: "excluded" for lending or credit with interest, gambling or betting, alcohol, adult content, speculative crypto tokens or memecoins. "borderline" for conventional insurance, crypto infrastructure, dating. Otherwise "ok".
- Keep product and company names as in the original."""

TAG_SYSTEM = """You assign topics to new product launches for trend analytics.
For every item pick 1-2 topics ONLY from this list: %s.
If the item fits none well, use "other" AND put a short English name for its real topic (1-3 words, lowercase) in new_topic; otherwise new_topic is "".
Reply with JSON only: {"items": [{"id": <id>, "topics": [...], "new_topic": "..."}]} — one entry per input item, same ids.""" % ", ".join(TOPICS)


def _key():
    return (os.environ.get("GROQ_API_KEY") or "").strip()


def available():
    """Есть ли ключ. Без него слой выключен, остальное работает."""
    if not _key():
        return False, "нет GROQ_API_KEY — ИИ-разбор выключен"
    return True, None


class RateLimited(Exception):
    """Groq ответил 429: бесплатная квота на сейчас кончилась."""


def _chat(model, system, user, schema=None, max_tokens=1200, timeout=60):
    """
    Один запрос к Groq. Возвращает (словарь из JSON-ответа, ошибка).

    Строгая схема (json_schema) — там, где модель её поддерживает; если
    Groq её не принял, повтор в режиме json_object со схемой в тексте.
    Ответ в любом случае разбирается и проверяется здесь: кривой JSON
    хуже, чем отсутствие выжимки.
    """
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_completion_tokens": max_tokens,
        "temperature": 0.2,
    }
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    if "qwen" in model:
        body["reasoning_format"] = "hidden"
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "answer", "schema": schema,
                                                   "strict": True}}
    else:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": "Bearer " + _key(), "Content-Type": "application/json",
               "User-Agent": UA}
    for attempt in range(2):
        try:
            r = requests.post(API, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            return None, "сеть: %s" % str(e)[:120]
        if r.status_code == 429:
            raise RateLimited(r.headers.get("retry-after", "?"))
        if r.status_code == 400 and schema and attempt == 0 and (
                "json_schema" in r.text or "response_format" in r.text):
            # Модель без строгих схем: просим просто JSON, схему — словами.
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] = (system + "\n\nJSON schema:\n"
                                              + json.dumps(schema, ensure_ascii=False))
            continue
        if r.status_code in (401, 403):
            return None, "ключ GROQ_API_KEY не принят (%d)" % r.status_code
        if r.status_code != 200:
            return None, "Groq %d: %s" % (r.status_code, r.text[:160])
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError):
            return None, "непонятный ответ Groq"
        content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
        try:
            return json.loads(content), None
        except ValueError:
            return None, "ответ не JSON"
    return None, "не удалось после повтора"


# --- кэш в базе ------------------------------------------------------------

def _ensure_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_notes ("
        " item_id INTEGER NOT NULL, lang TEXT NOT NULL, data TEXT, model TEXT,"
        " ts INTEGER, PRIMARY KEY (item_id, lang))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_topics ("
        " item_id INTEGER PRIMARY KEY, topics TEXT, new_topic TEXT, ts INTEGER)")


def get_note(conn, item_id, lang):
    """Готовый разбор или None."""
    _ensure_tables(conn)
    row = conn.execute("SELECT data FROM ai_notes WHERE item_id = ? AND lang = ?",
                       (item_id, lang)).fetchone()
    if not row or not row["data"]:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None


def _counter_key(model, now):
    # Счётчик на модель: квоты Groq у каждой модели свои.
    return "ai_calls_%s_%s" % (model, time.strftime("%Y-%m-%d", time.gmtime(now)))


def _count_call(conn, now, model):
    n = int(db.kv_get(conn, _counter_key(model, now), 0) or 0) + 1
    db.kv_set(conn, _counter_key(model, now), n)
    return n


def _calls_today(conn, now, model):
    return int(db.kv_get(conn, _counter_key(model, now), 0) or 0)


def _page_hint(url, timeout=8):
    """
    Заголовок и описание страницы продукта — одним дешёвым запросом.

    По одному твиту часто не понять, что за продукт («just launched v2 🚀»),
    а заголовок и meta description его сайта почти всегда это говорят.
    """
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA},
                         allow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        h = r.text[:200000]
    except requests.RequestException:
        return ""
    parts = []
    m = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    if m:
        parts.append("title: " + re.sub(r"\s+", " ", m.group(1)).strip()[:200])
    for name in ("description", "og:description", "twitter:description"):
        m = re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]+content=["\']([^"\']+)'
                      % re.escape(name), h, re.I)
        if m:
            parts.append("description: " + m.group(1).strip()[:400])
            break
    return "\n".join(parts)


def _item_prompt(item, lang):
    src = {"x": "X (Twitter) post", "hn": "Hacker News post", "yc": "Y Combinator directory entry",
           "gh": "GitHub repository"}.get(item["source"], item["source"])
    lines = ["Requested language: %s" % LANG_NAMES.get(lang, lang), "Source: %s" % src]
    if item["author"]:
        lines.append("Author: @%s" % item["author"])
    lines.append("Title: %s" % (item["title"] or ""))
    body = (item["body"] or "").strip()
    if body and body != (item["title"] or "").strip():
        lines.append("Text: %s" % body[:1500])
    if item["tags"]:
        lines.append("Tags: %s" % item["tags"][:200])
    if item["product_url"]:
        lines.append("Product URL: %s" % item["product_url"])
        hint = _page_hint(item["product_url"])
        if hint:
            lines.append("Product page:\n" + hint)
    return "\n".join(lines)


def _valid_note(n):
    return (isinstance(n, dict) and isinstance(n.get("summary"), str) and n["summary"].strip()
            and isinstance(n.get("is_product_launch"), bool))


# --- разбор кандидатов -------------------------------------------------------

def annotate(conn, candidates, now, lang="ru", verbose=True):
    """
    Разобрать кандидатов в уведомление, у которых нет разбора на этом языке.
    candidates — строки items, важные первыми. Возвращает (сделано, ошибка).
    """
    ok, why = available()
    if not ok:
        return 0, why
    _ensure_tables(conn)
    model = os.environ.get("LS_AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    cap = int(os.environ.get("LS_AI_DAILY_MAX", DEFAULT_DAILY_MAX))
    done, last_err = 0, None
    for item in candidates[:NOTES_PER_RUN]:
        if _calls_today(conn, now, model) >= cap:
            last_err = "дневной предел ИИ-запросов исчерпан (%d)" % cap
            break
        if get_note(conn, item["item_id"], lang) is not None:
            continue
        try:
            note, err = _chat(model, SYSTEM, _item_prompt(item, lang), schema=NOTE_SCHEMA)
        except RateLimited as e:
            last_err = "429 от Groq — бесплатная квота на сейчас кончилась (retry-after %s)" % e
            _count_call(conn, now, model)
            break
        _count_call(conn, now, model)
        if err or not _valid_note(note):
            last_err = err or "разбор не прошёл проверку схемы"
            if "не принят" in (err or ""):
                break
            continue
        conn.execute("INSERT OR REPLACE INTO ai_notes (item_id, lang, data, model, ts) "
                     "VALUES (?,?,?,?,?)",
                     (item["item_id"], lang, json.dumps(note, ensure_ascii=False), model, now))
        done += 1
    conn.commit()
    if verbose:
        print("  ИИ-разборов: %d (запросов к %s за сутки: %d)%s"
              % (done, model, _calls_today(conn, now, model), (" — " + last_err) if last_err else ""))
    return done, last_err


def verdict(note, source=None):
    """
    Что разбор значит для уровня находки: (понизить_в_архив, пометка).

    Модель не решает «горячо ли» — это делают цифры. Она снимает то, что
    по цифрам похоже на запуск, а по смыслу им не является.

    «Не запуск продукта» применяется только к X и HN — там вопрос
    настоящий (личный TikTok косплеера проходил все маркеры запуска,
    2026-09-26). Репозиторий GitHub — продукт по определению, компания
    из каталога YC — компания; модель на тесте назвала репозиторий «не
    запуском», и понижать за это было бы ошибкой.
    """
    if not note:
        return False, None
    if note.get("niche") == "excluded":
        return True, "ИИ: ниша исключена правилами"
    if note.get("is_product_launch") is False and source in ("x", "hn", None):
        return True, "ИИ: не запуск продукта"
    if note.get("niche") == "borderline":
        return False, "ИИ: ниша под вопросом"
    return False, None


# --- темы для трендов --------------------------------------------------------

TAG_BATCH = 25


def tag_items(conn, now, max_items=TAGS_PER_RUN, days=8, verbose=True):
    """
    Проставить темы свежим находкам, у которых их ещё нет, — пачками по 25.
    Возвращает (размечено, ошибка).
    """
    ok, why = available()
    if not ok:
        return 0, why
    _ensure_tables(conn)
    model = os.environ.get("LS_TAG_MODEL", DEFAULT_TAG_MODEL).strip() or DEFAULT_TAG_MODEL
    cap = int(os.environ.get("LS_AI_DAILY_MAX", DEFAULT_DAILY_MAX))
    rows = conn.execute(
        "SELECT i.item_id, i.source, i.title, i.body FROM items i "
        "LEFT JOIN item_topics t ON t.item_id = i.item_id "
        "WHERE t.item_id IS NULL AND i.first_seen >= ? "
        "ORDER BY i.first_seen DESC LIMIT ?",
        (now - days * 86400, max_items)).fetchall()
    done, last_err = 0, None
    allowed = set(TOPICS)
    for i in range(0, len(rows), TAG_BATCH):
        if _calls_today(conn, now, model) >= cap:
            last_err = "дневной предел ИИ-запросов исчерпан (%d)" % cap
            break
        chunk = rows[i:i + TAG_BATCH]
        payload = [{"id": r["item_id"], "source": r["source"],
                    "text": ((r["title"] or "") + " — " + (r["body"] or ""))[:240]}
                   for r in chunk]
        try:
            data, err = _chat(model, TAG_SYSTEM, json.dumps(payload, ensure_ascii=False),
                              schema=None, max_tokens=3000)
        except RateLimited as e:
            last_err = "429 от Groq на разметке тем (retry-after %s)" % e
            _count_call(conn, now, model)
            break
        _count_call(conn, now, model)
        if err or not isinstance(data, dict):
            last_err = err or "разметка не JSON"
            continue
        ids = {r["item_id"] for r in chunk}
        for e in (data.get("items") or []):
            try:
                iid = int(e.get("id"))
            except (TypeError, ValueError):
                continue
            if iid not in ids:
                continue
            topics = [t for t in (e.get("topics") or []) if t in allowed][:2] or ["other"]
            new_topic = (e.get("new_topic") or "").strip().lower()[:40]
            conn.execute("INSERT OR REPLACE INTO item_topics (item_id, topics, new_topic, ts) "
                         "VALUES (?,?,?,?)",
                         (iid, json.dumps(topics), new_topic if "other" in topics else "", now))
            done += 1
        conn.commit()
    if verbose and (rows or last_err):
        print("  темы размечены: %d из %d%s" % (done, len(rows),
                                               (" — " + last_err) if last_err else ""))
    return done, last_err


def write_trend_story(stats_text, lang="ru"):
    """Короткий связный разбор трендов по готовой таблице цифр. (текст, ошибка)."""
    ok, why = available()
    if not ok:
        return None, why
    model = os.environ.get("LS_AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    system = ("You write a short weekly trend brief for a founder hunting startup ideas. "
              "Use ONLY the numbers given; do not invent figures, companies or links. "
              "Reply as JSON {\"text\": \"...\"}: 4-6 short bullet lines in %s, each starting "
              "with •, saying what is rising, what is new, and one concrete idea worth "
              "testing. Plain, no hype." % LANG_NAMES.get(lang, lang))
    try:
        data, err = _chat(model, system, stats_text, schema=None, max_tokens=1500)
    except RateLimited:
        return None, "429 от Groq"
    if err or not isinstance(data, dict) or not data.get("text"):
        return None, err or "пустой ответ"
    return data["text"].strip(), None
