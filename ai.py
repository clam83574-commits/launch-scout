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
# Бесплатный Groq (замерено 2026-09-26 по заголовкам ответа): на КАЖДУЮ
# модель 1000 запросов в сутки и 8000 токенов в минуту, плюс 200 тыс.
# токенов в сутки (TPD — его в заголовках нет, только в тексте 429 и на
# console.groq.com/docs/rate-limits, найдено 2026-09-27). Минута держит
# потолки за прогон ниже, сутки — общий объём: 120b делят разборы,
# выводы трендов и карточки идей Worker'а. Суточный предел запросов ниже
# квоты — запас на ручные запуски и повторы.
DEFAULT_DAILY_MAX = 900
# Разбор сразу на трёх языках — втрое длиннее ответ. 10 за прогон: без
# разбора пост из X не уходит в пуш (scout.dispatch), и очередь из 4 в
# прогон не успевала за потоком X (2026-09-29).
NOTES_PER_RUN = 10
TAGS_PER_RUN = 40
# С ключом OpenRouter разметка не упирается в минутные пределы Groq: остаток
# пачек уходит дешёвой модели. Так за прогон доразмечается хвост за неделю —
# после добавления поля relevant (2026-10-10) переразметить надо было всю
# ленту, а по 40 в час это заняло бы сутки. 20 находок — около 0,05 цента.
TAGS_PER_RUN_OR = 240

# Языки пользователей: Казахстан первым (решение владельца 2026-09-26),
# затем англоязычный рынок. Всё, что видит пользователь, готовится сразу
# на трёх: переводить по запросу — втрое больше обращений к квоте.
LANGS = ("ru", "kk", "en")
AUDIENCES = ["b2b", "b2c", "b2g"]

LANG_NAMES = {
    "ru": "Russian", "en": "English", "kk": "Kazakh", "uz": "Uzbek",
    "ar": "Arabic", "tr": "Turkish", "uk": "Ukrainian", "es": "Spanish",
    "de": "German", "fr": "French", "pt": "Portuguese", "id": "Indonesian",
}

KINDS = ["b2b_saas", "b2c_app", "dev_tool", "ai_agent", "marketplace",
         "hardware", "content_media", "fintech", "other"]
EFFORTS = ["days", "weeks", "months", "unclear"]
NICHE = ["ok", "borderline", "excluded"]
POTENTIAL = ["none", "low", "medium", "high"]

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

_TXT = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "monetization": {"type": "string"},
                   "clone_note": {"type": "string"}},
    "required": ["summary", "monetization", "clone_note"],
    "additionalProperties": False,
}

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_product_launch": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "clone_effort": {"type": "string", "enum": EFFORTS},
        "niche": {"type": "string", "enum": NICHE},
        "business_potential": {"type": "string", "enum": POTENTIAL},
        "audience": {"type": "array", "items": {"type": "string", "enum": AUDIENCES}},
        "i18n": {"type": "object", "properties": {"ru": _TXT, "kk": _TXT, "en": _TXT},
                 "required": ["ru", "kk", "en"], "additionalProperties": False},
    },
    "required": ["is_product_launch", "kind", "clone_effort", "niche",
                 "business_potential", "audience", "i18n"],
    "additionalProperties": False,
}

SYSTEM = """You review early signals of new products for a founder who looks for startup ideas to build quickly or to localize for another market (Kazakhstan first, then the English-speaking world).

For each item you get the source post and, when available, the product page title and description. Reply with JSON only, matching the schema.

Rules:
- i18n: the same three texts in Russian (ru), Kazakh in Cyrillic (kk) and English (en).
  - summary: 2-3 plain sentences: what the product does and who it is for. No hype, no marketing tone. If the post is too vague to tell, say so.
  - monetization: how it makes money if visible; otherwise exactly "не видно" (ru), "көрінбейді" (kk), "not visible" (en).
  - clone_note: one line naming the hardest part to replicate.
- is_product_launch: true only if a startup product, company or software/hardware tool is being launched or shipped. False for: opinions, news reports, fundraising announcements without a product, memes, art commissions, game patches and content updates, music/album/vinyl/merch releases, concerts and tickets, celebrity or fan content, courses and ebooks by influencers, people joining a company, personal milestones.
- clone_effort: rough time for a small team to build a comparable first version: days, weeks, months, or unclear.
- business_potential: could this become a business someone pays for? "none" for jokes, art, fan projects and pure entertainment; "low" for hobby tools and demos with no clear buyer; "medium" when a clear user group would plausibly pay; "high" when it solves a costly problem for businesses or has visible traction or revenue.
- audience: who pays: businesses (b2b), consumers (b2c), government (b2g); one or two values.
- niche: "excluded" for lending or credit with interest, gambling or betting, alcohol, adult content, speculative crypto tokens or memecoins. "borderline" for conventional insurance, crypto infrastructure, dating. Otherwise "ok".
- Keep product and company names as in the original."""

TAG_SYSTEM = """You label new product launches for a trend feed read in Russian, Kazakh and English.
For every item return:
- topics: 1-2 topics ONLY from this list: %s. If none fits well, use "other" and put a short English name of the real topic (1-3 words, lowercase) in new_topic; otherwise new_topic is "".
- audience: who pays: businesses "b2b", consumers "b2c", government "b2g"; one or two values.
- gist: ONE plain sentence (max 15 words) saying what the product does and for whom, in Russian (ru), Kazakh in Cyrillic (kk) and English (en). Keep product names as in the original. No hype.
- relevant: true if the item is about a startup, a product or software/hardware tool, a tech company, a funding round or the craft of building and selling products. false for politics, crime, war, migration, general news, sports, celebrities, personal life, and opinions or jokes not about building products — even when posted by a famous founder or investor.
Return an entry for EVERY input item, irrelevant ones too: for them set relevant to false, topics to ["other"] and let the gist say what the post is about.
Reply with JSON only: {"items": [{"id": <id>, "relevant": true, "topics": [...], "new_topic": "...", "audience": [...], "gist": {"ru": "...", "kk": "...", "en": "..."}}]}, one entry per input item, same ids.""" % ", ".join(TOPICS)


def _keys():
    """
    Все ключи Groq: GROQ_API_KEY, GROQ_API_KEY_2 … _5 и список через запятую
    в GROQ_API_KEYS. Ротация помогает, только если ключи из РАЗНЫХ
    организаций Groq: лимиты считаются на организацию, а не на ключ.
    """
    raw = [os.environ.get("GROQ_API_KEYS") or ""] + [
        os.environ.get("GROQ_API_KEY" + sfx) or "" for sfx in ("", "_2", "_3", "_4", "_5")]
    out = []
    for chunk in raw:
        for k in chunk.split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def _key():
    keys = _keys()
    return keys[0] if keys else ""


def _cap():
    """Суточный предел запросов на модель: на каждый ключ — своя квота."""
    return int(os.environ.get("LS_AI_DAILY_MAX") or DEFAULT_DAILY_MAX * max(len(_keys()), 1))


# Ключи, упёршиеся в предел в этом прогоне, по моделям: {(модель, ключ)}.
_SPENT = set()


def available():
    """Есть ли ключ. Без него слой выключен, остальное работает."""
    if not _key():
        return False, "нет GROQ_API_KEY — ИИ-разбор выключен"
    return True, None


class RateLimited(Exception):
    """
    Groq ответил 429: бесплатная квота на сейчас кончилась.

    В тексте — какой именно предел (TPM — токены в минуту, TPD — в сутки)
    и сколько ждать: «TPM» проходит за минуту, «TPD» — только к следующим
    суткам, и повторять запросы до тех пор бессмысленно.
    """

    def __init__(self, retry_after, kind=""):
        super().__init__("%s retry-after %s" % (kind or "?", retry_after))
        self.kind = kind
        self.retry_after = retry_after


# Сколько токенов потрачено в этом процессе, по моделям: у бесплатного
# Groq 200 тыс. токенов в сутки на модель (console.groq.com/docs/rate-limits,
# проверено 2026-09-27), и расход надо видеть в логе каждого прогона.
USAGE = {}


def _limit_kind(text):
    """
    «TPD 199103/200000, ждать 11m7s» из текста 429 — без номера организации.

    Кодов больше, чем кажется: у qwen3.8-27b есть ещё OTPM — 1000 ВЫХОДНЫХ
    токенов в минуту (найдено 2026-09-27), то есть одна пачка разметки за
    прогон. Поэтому код берём любой, а не из заранее известного списка.
    """
    m = re.search(r"\(([A-Z]{3,5})\): Limit (\d+), Used (\d+)", text or "")
    wait = re.search(r"try again in ([0-9hms.]+)", text or "")
    if not m:
        return ""
    return "%s %s/%s%s" % (m.group(1), m.group(3), m.group(2),
                           (", ждать " + wait.group(1)) if wait else "")


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
    keys = [k for k in _keys() if (model, k) not in _SPENT]
    if not keys:
        raise RateLimited("?", "все ключи на пределе в этом прогоне")
    key = keys[0]
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "User-Agent": UA}
    for attempt in range(2 + len(keys)):
        try:
            r = requests.post(API, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            return None, "сеть: %s" % str(e)[:120]
        if r.status_code == 429:
            # Ротация: этот ключ на пределе для этой модели — следующий.
            _SPENT.add((model, key))
            rest = [k for k in _keys() if (model, k) not in _SPENT]
            if not rest:
                raise RateLimited(r.headers.get("retry-after", "?"), _limit_kind(r.text))
            key = rest[0]
            headers["Authorization"] = "Bearer " + key
            continue
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
            payload = r.json()
            content = payload["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError):
            return None, "непонятный ответ Groq"
        used = (payload.get("usage") or {}).get("total_tokens") or 0
        USAGE[model] = USAGE.get(model, 0) + int(used)
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
        " item_id INTEGER PRIMARY KEY, topics TEXT, new_topic TEXT, ts INTEGER,"
        " audience TEXT, gist TEXT)")
    # Базы, созданные до мультиязычности, дополняем колонками на месте.
    have = {r[1] for r in conn.execute("PRAGMA table_info(item_topics)")}
    for col in ("audience", "gist"):
        if col not in have:
            conn.execute("ALTER TABLE item_topics ADD COLUMN %s TEXT" % col)
    # relevant: 1 — про стартапы и продукты, 0 — нет, NULL — ещё не размечено.
    if "relevant" not in have:
        conn.execute("ALTER TABLE item_topics ADD COLUMN relevant INTEGER")


def relevance(conn, item_id):
    """
    Относится ли находка к стартапам и продуктам: True / False / None
    (ещё не размечена). Списки «умных денег» читаются без фильтра по
    маркерам запуска, и в ленту с баллом 95 попадала политика из X
    (пост @dhh про суд в Копенгагене, 2026-10-10).
    """
    try:
        row = conn.execute("SELECT relevant FROM item_topics WHERE item_id = ?",
                           (item_id,)).fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return bool(row[0])


def get_note(conn, item_id, lang=None):
    """
    Сохранённый разбор (сырой, со всеми языками) или None.

    Новые разборы лежат под lang="multi" и содержат i18n на трёх языках;
    старые, до 2026-09-27, лежат под "ru" и плоские. Берём новый, иначе старый.
    """
    _ensure_tables(conn)
    for key in ("multi", lang or "ru"):
        row = conn.execute("SELECT data FROM ai_notes WHERE item_id = ? AND lang = ?",
                           (item_id, key)).fetchone()
        if row and row["data"]:
            try:
                return json.loads(row["data"])
            except ValueError:
                return None
    return None


def note_for(note, lang):
    """Разбор, развёрнутый на один язык: плоский словарь для показа."""
    if not note:
        return None
    texts = ("summary", "monetization", "clone_note")
    tx = note.get("i18n")
    if tx is None:      # разбор до 2026-09-27: плоский и только русский
        tx = {"ru": {k: note.get(k) for k in texts}}
    flat = {k: v for k, v in note.items() if k != "i18n" and k not in texts}
    # Только язык читателя: чужой язык хуже выжимки на своём — её покажет
    # format_item, а если нет и её, то исходный пост.
    flat.update(tx.get(lang) or {})
    return flat


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


def _item_prompt(item, lang=None):
    src = {"x": "X (Twitter) post", "hn": "Hacker News post", "yc": "Y Combinator directory entry",
           "gh": "GitHub repository"}.get(item["source"], item["source"])
    lines = ["Source: %s" % src]
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
    if not isinstance(n, dict):
        return False
    ru = (n.get("i18n") or {}).get("ru") or {}
    return (isinstance(ru.get("summary"), str) and bool(ru["summary"].strip())
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
    cap = _cap()
    done, last_err = 0, None
    # Сначала отбросить уже разобранных, потом брать первые N: наоборот
    # очередь вставала, как только первые N кандидатов были разобраны.
    todo, seen = [], set()
    for item in candidates:
        existing = get_note(conn, item["item_id"])
        if item["item_id"] in seen or (existing is not None and "i18n" in existing):
            continue
        seen.add(item["item_id"])
        todo.append(item)
    for item in todo[:NOTES_PER_RUN]:
        if _calls_today(conn, now, model) >= cap:
            last_err = "дневной предел ИИ-запросов исчерпан (%d)" % cap
            break
        try:
            note, err = _chat(model, SYSTEM, _item_prompt(item, lang), schema=NOTE_SCHEMA)
        except RateLimited as e:
            last_err = "429 от Groq — бесплатная квота на сейчас кончилась (%s)" % e
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
                     (item["item_id"], "multi", json.dumps(note, ensure_ascii=False), model, now))
        done += 1
    conn.commit()
    if verbose:
        print("  ИИ-разборов новых: %d (остальные кандидаты уже разобраны; запросов к %s за сутки: %d,"
              " токенов в прогоне: %d)%s"
              % (done, model, _calls_today(conn, now, model), USAGE.get(model, 0),
                 (" — " + last_err) if last_err else ""))
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


def not_business(note):
    """
    Продукт, но не бизнес-идея: игрушка, арт, демо без покупателя.

    Первая же выдача с ИИ (2026-09-26) показала, что «продукт ли это» не
    отсекает главное: шрифты, Pokémon и рыбки — честно продукты, и модель
    так и ответила. Владельцу же нужны идеи, за которые платят. Такие
    находки не выбрасываются, а опускаются из мгновенных в сводку.
    Старые разборы без этого поля не трогаем.
    """
    return bool(note) and note.get("business_potential") in ("none", "low")


# --- темы для трендов --------------------------------------------------------

TAG_BATCH = 20
# Разметка идёт по очереди на двух бесплатных моделях: у каждой свои
# 200 тыс. токенов в сутки и 8 тыс. в минуту, а пачка из 20 находок — около
# 4–6 тыс. токенов. На одной модели вторая пачка прогона упиралась в
# минутный предел, а суточного не хватало на весь поток (2026-09-27).
# qwen3.8-27b проверена на той же пачке: 20 из 20 с выжимками на трёх языках,
# 4,2 тыс. токенов; у неё ещё предел 1000 выходных токенов в минуту, так что
# за прогон она берёт одну пачку. Итого запас — около 1600 находок в сутки
# при притоке ~400–500.
DEFAULT_TAG_MODELS = "openai/gpt-oss-20b,qwen/qwen3.8-27b"


def _tag_models():
    raw = (os.environ.get("LS_TAG_MODELS") or os.environ.get("LS_TAG_MODEL")
           or DEFAULT_TAG_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def enrich_items(conn, now, max_items=TAGS_PER_RUN, days=8, verbose=True):
    """
    Пакетная разметка свежих находок: темы, аудитория (B2B/B2C/B2G) и
    выжимка в одну фразу на трёх языках, по 20 находок на запрос.

    Так каждая находка в ленте получает текст на языке пользователя, а не
    только те немногие, что дошли до полного разбора. Пачка, а не поштучно:
    весь поток (сотни записей в сутки) в бесплатную квоту иначе не влез бы.
    Первыми идут находки с баллом выше нуля — их видят в ленте; нулевые
    размечаются на остаток квоты (они нужны только трендам).
    Возвращает (размечено, ошибка).
    """
    ok, why = available()
    if not ok:
        return 0, why
    _ensure_tables(conn)
    models = _tag_models()
    cap = _cap()
    bulk = (os.environ.get("LS_BULK_MODEL") or OR_BULK_MODEL) if openrouter_key() else None
    if bulk:
        max_items = max(max_items, TAGS_PER_RUN_OR)
    rows = conn.execute(
        "SELECT i.item_id, i.source, i.title, i.body FROM items i "
        "LEFT JOIN item_topics t ON t.item_id = i.item_id "
        "LEFT JOIN (SELECT item_id, MAX(score) AS s FROM scores GROUP BY item_id) sc "
        "  ON sc.item_id = i.item_id "
        "WHERE (t.item_id IS NULL OR t.gist IS NULL OR t.relevant IS NULL) AND i.first_seen >= ? "
        "ORDER BY (COALESCE(sc.s, 0) > 0) DESC, i.first_seen DESC LIMIT ?",
        (now - days * 86400, max_items)).fetchall()
    done, last_err = 0, None
    allowed = set(TOPICS)
    spent, turn = set(), 0          # модели, упёршиеся в предел в этом прогоне
    for i in range(0, len(rows), TAG_BATCH):
        chunk = rows[i:i + TAG_BATCH]
        payload = [{"id": r["item_id"], "source": r["source"],
                    "text": ((r["title"] or "") + " — " + (r["body"] or ""))[:240]}
                   for r in chunk]
        data, err = None, None
        for _ in range(len(models)):
            model = models[turn % len(models)]
            turn += 1
            if model in spent:
                continue
            if _calls_today(conn, now, model) >= cap:
                spent.add(model)
                last_err = "дневной предел запросов к %s исчерпан (%d)" % (model, cap)
                continue
            try:
                data, err = _chat(model, TAG_SYSTEM, json.dumps(payload, ensure_ascii=False),
                                  schema=None, max_tokens=4000)
            except RateLimited as e:
                _count_call(conn, now, model)
                spent.add(model)
                last_err = "429 от Groq на разметке, %s (%s)" % (model, e)
                continue
            _count_call(conn, now, model)
            break
        if data is None and bulk:
            data, err = _chat_or(bulk, TAG_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=4000)
            if data is None:
                last_err = "разметка через %s: %s" % (bulk, err)
                break
        if len(spent) >= len(models) and data is None:
            break
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
            topics = [x for x in (e.get("topics") or []) if x in allowed][:2] or ["other"]
            new_topic = (e.get("new_topic") or "").strip().lower()[:40]
            aud = [a for a in (e.get("audience") or []) if a in AUDIENCES][:2]
            g = e.get("gist") if isinstance(e.get("gist"), dict) else {}
            gist = {k: str(g.get(k) or "").strip()[:200] for k in LANGS if g.get(k)}
            rel = e.get("relevant")
            # Модель пропустила поле — считаем релевантной: иначе находка
            # возвращалась бы в очередь разметки каждый прогон.
            rel = int(rel) if isinstance(rel, bool) else 1
            conn.execute("INSERT OR REPLACE INTO item_topics "
                         "(item_id, topics, new_topic, ts, audience, gist, relevant) VALUES (?,?,?,?,?,?,?)",
                         (iid, json.dumps(topics), new_topic if "other" in topics else "", now,
                          json.dumps(aud), json.dumps(gist, ensure_ascii=False) if gist else None, rel))
            done += 1
        conn.commit()
    if verbose and (rows or last_err):
        spent_tokens = ", ".join("%s %d" % (m.split("/")[-1], USAGE[m])
                                 for m in models + ([bulk] if bulk else []) if USAGE.get(m))
        print("  размечено (темы, аудитория, выжимка на 3 языках): %d из %d%s%s"
              % (done, len(rows), (" · токенов: " + spent_tokens) if spent_tokens else "",
                 (" — " + last_err) if last_err else ""))
    return done, last_err


tag_items = enrich_items   # прежнее имя — для совместимости


# --- раунды: разбор заголовков о сделках --------------------------------------

DEAL_SYSTEM = """You extract venture funding rounds from news headlines and posts (in any language, including Russian) for a market radar read by startup founders.
For every input item return:
- id: the same id.
- is_round: true ONLY if one specific company raised an equity/venture round (pre-seed to late stage). false for: VC firms raising their own funds, IPOs, acquisitions, grants, pure debt or loans, reports, roundups and lists of several deals, rumors ("in talks").
- company: the startup's own name exactly as written, without descriptors ("AI startup Foo" -> "Foo", "AÏZA parent Amaani" -> "Amaani").
- usd: the round amount in US dollars as a plain number (convert other currencies approximately; 1 crore INR = 120000 USD). null if not stated. Never the valuation.
- stage: one of "pre-seed", "seed", "a", "b", "c+", "growth", "unknown".
- sector: one id from this list: %s.
- niche: the specific market niche a founder could enter — WHO the customer is plus WHAT job is done, without geography: "en" in 2-5 lowercase English words (good: "identity for ai agents", "warehouse picking robots", "ai agents for hotels", "sme lending", "insurance claims automation"; bad, too broad: "ai agent platform", "digital health platform", "consumer app", "fintech", "saas") and "ru" — the same in Russian. Never use the words platform, app, solution or tech as the core of the niche. If one of the KNOWN NICHES given in the input means the same thing, reuse its English name exactly; do not force an item into a known niche that is only loosely related.
- what: what the company does, max 12 words, plain, in Russian (ru) and English (en).
- country: ISO 3166 two-letter code of the company's home country, or "".
- investors: up to 3 investors named in the text as leading or joining the round (firm names as written, e.g. "a16z", "Sequoia", "Y Combinator"); [] if none are named.
Reply with JSON only: {"items": [{"id": ..., "is_round": ..., "company": "...", "usd": ..., "stage": "...", "sector": "...", "niche": {"en": "...", "ru": "..."}, "what": {"ru": "...", "en": "..."}, "country": "...", "investors": ["..."]}]}. For is_round=false items you may leave the other fields empty."""

DEAL_BATCH = 20
DEAL_BATCHES_PER_RUN = 2
DEFAULT_DEAL_MODELS = "openai/gpt-oss-120b,openai/gpt-oss-20b"
# С ключом OpenRouter массовый разбор идёт через дешёвую модель без минутного
# лимита Groq — так за день разбирается и история за полгода (~8 тыс.
# заголовков, около $1,5 на gemini-3.1-flash-lite по ценам 2026-09-29).
OR_BULK_MODEL = "google/gemini-3.1-flash-lite"
OR_BATCHES_PER_RUN = 10
OR_API = "https://openrouter.ai/api/v1/chat/completions"
# Поиск в сети для досье ниш и проверки аналогов — Perplexity sonar (~0,6 цента
# за запрос). gemini-3.8-flash:online стоил 9–24 цента: длинные рассуждения плюс
# плагин поиска, 7–8 октября 2026 это было ~$2,5 в сутки — 75% всего расхода.
SEARCH_MODEL = "perplexity/sonar"


def openrouter_key():
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _loose_json(text):
    """
    JSON из ответа модели, даже если вокруг него текст, ```-обёртка, ссылки
    веб-поиска или висячие запятые. None, если достать не удалось.
    """
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    for cand in (t, t[t.find("{"):t.rfind("}") + 1]):
        if not cand:
            continue
        for fix in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                return json.loads(fix)
            except ValueError:
                continue
    return None


def _chat_or(model, system, user, max_tokens=4000, timeout=90):
    """
    Запрос к OpenRouter, ответ — JSON. (словарь, ошибка).

    Режим json_object — только без веб-поиска: с плагином :online модель
    отвечает текстом со ссылками, и строгий режим на проде (2026-09-29)
    возвращал «не JSON». Там JSON просим словами и достаём из текста.
    """
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    # Строгий JSON — только у обычных моделей: с поиском (:online, Perplexity)
    # ответ текстом со ссылками, JSON достаём из текста.
    if not model.endswith(":online") and not model.startswith("perplexity/"):
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(OR_API, timeout=timeout, json=body,
            headers={"Authorization": "Bearer " + openrouter_key(), "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/clam83574-commits/launch-scout", "X-Title": "launch-scout"})
    except requests.RequestException as e:
        return None, "сеть: %s" % str(e)[:120]
    if r.status_code != 200:
        return None, "OpenRouter %d: %s" % (r.status_code, r.text[:160])
    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return None, "непонятный ответ OpenRouter"
    used = (payload.get("usage") or {}).get("total_tokens") or 0
    USAGE[model] = USAGE.get(model, 0) + int(used)
    data = _loose_json(content)
    if data is None:
        finish = ((payload.get("choices") or [{}])[0] or {}).get("finish_reason")
        return None, "ответ не JSON (%s, %d симв.): %s" % (finish, len(content), content[:120].replace("\n", " "))
    return data, None


def extract_deals(conn, now, items, sectors, known_niches):
    """
    Разобрать заголовки о раундах пачками. items — [{"id", "text"}],
    sectors — [(id, английское описание)]. Возвращает (ответы, id пачек,
    на которые модель ответила, ошибка): строку из отвеченной пачки, которую
    модель пропустила, повторно не разбираем.

    Модели — по приоритету, а не по кругу: на пробе 2026-09-28 gpt-oss-20b
    оставлял нишу пустой примерно в половине сделок и путал описания,
    120b — нет. Вторая модель берётся, только когда первая упёрлась в
    предел. Больше DEAL_BATCHES_PER_RUN пачек за прогон не берём — прогон
    раз в 10 минут, очередь разбирается постепенно, квота не выгорает залпом.
    """
    system = DEAL_SYSTEM % ", ".join('"%s" (%s)' % s for s in sectors)
    out, answered, last_err, spent = [], set(), None, set()
    if openrouter_key():
        model = os.environ.get("LS_BULK_MODEL") or OR_BULK_MODEL
        for i in range(0, min(len(items), DEAL_BATCH * OR_BATCHES_PER_RUN), DEAL_BATCH):
            chunk = items[i:i + DEAL_BATCH]
            payload = json.dumps({"known_niches": known_niches[:60], "items": chunk}, ensure_ascii=False)
            # Провайдер иногда обрывает ответ посреди JSON (finish_reason
            # «error», 2026-09-29) — пачку повторяем один раз, а сбой одной
            # пачки не останавливает остальные. Стоп — только ключ и оплата.
            for _attempt in range(2):
                data, err = _chat_or(model, system, payload, max_tokens=6000)
                if not err:
                    break
            if err:
                last_err = err
                if re.match(r"OpenRouter (401|402|403)", err):
                    break
                continue
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                out += [e for e in data["items"] if isinstance(e, dict)]
                answered |= {c["id"] for c in chunk}
        return out, answered, last_err
    ok, why = available()
    if not ok:
        return [], set(), why
    models = [m.strip() for m in (os.environ.get("LS_DEAL_MODELS") or DEFAULT_DEAL_MODELS).split(",")
              if m.strip()]
    cap = _cap()
    for i in range(0, min(len(items), DEAL_BATCH * DEAL_BATCHES_PER_RUN), DEAL_BATCH):
        chunk = items[i:i + DEAL_BATCH]
        user = json.dumps({"known_niches": known_niches[:60], "items": chunk}, ensure_ascii=False)
        data = None
        for model in models:
            if model in spent or _calls_today(conn, now, model) >= cap:
                spent.add(model)
                continue
            try:
                data, err = _chat(model, system, user, schema=None, max_tokens=5000)
            except RateLimited as e:
                _count_call(conn, now, model)
                spent.add(model)
                last_err = "429 на разборе раундов, %s (%s)" % (model, e)
                continue
            _count_call(conn, now, model)
            if err:
                last_err = err
                data = None
            break
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            out += [e for e in data["items"] if isinstance(e, dict)]
            answered |= {c["id"] for c in chunk}
        if len(spent) >= len(models):
            break
    return out, answered, last_err


# --- 🇰🇿 Свободна ли ниша в Казахстане и СНГ ---------------------------------

GAP_SYSTEM = """You check whether a startup niche that is attracting venture rounds abroad is already served in Kazakhstan and in Russia/CIS (Uzbekistan, Kyrgyzstan, Belarus, Armenia, Georgia, Azerbaijan).
Search the web in Russian and Kazakh as well as English (e.g. local product names, "<niche in Russian> сервис", "<niche> Казахстан").
Count only real products or companies that serve this niche IN those markets (local companies, or global ones localized with local language, payments or presence). Ignore global products merely available everywhere.
Reply with JSON only:
{"kz": "free|partly|crowded", "cis": "free|partly|crowded", "analogs": [{"name": "...", "url": "https://...", "country": "KZ|RU|UZ|KG|BY|AM|GE|AZ"}], "note": {"ru": "...", "kk": "...", "en": "..."}}
free = nothing found; partly = 1-2 small or partial players; crowded = several established players. analogs: at most 5, only ones you actually found with a real URL. note: one short sentence per language on what is missing locally or what a newcomer should do differently. Never invent companies."""


def gap_check(niche, examples):
    """
    Проверка ниши на аналоги в Казахстане и СНГ с поиском в сети. (словарь, ошибка).
    OpenRouter (:online — поиск в сети) при ключе, иначе Groq gpt-oss-120b
    со встроенным browser_search. Дорого по сравнению с разметкой, поэтому
    вызывается для нескольких ниш в неделю, результат хранится.
    """
    user = "Niche: %s\nFunded companies abroad in this niche: %s" % (niche, "; ".join(examples[:5]))
    if openrouter_key():
        return _chat_or(os.environ.get("LS_SEARCH_MODEL") or SEARCH_MODEL, GAP_SYSTEM, user, max_tokens=2500, timeout=120)
    ok, why = available()
    if not ok:
        return None, why
    body = {"model": DEFAULT_MODEL, "temperature": 0.2, "max_completion_tokens": 3000, "reasoning_effort": "low",
            "tools": [{"type": "browser_search"}], "tool_choice": "auto",
            "messages": [{"role": "system", "content": GAP_SYSTEM}, {"role": "user", "content": user}]}
    for key in _keys():
        try:
            r = requests.post(API, json=body, timeout=120,
                              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        except requests.RequestException as e:
            return None, "сеть: %s" % str(e)[:120]
        if r.status_code == 429:
            continue
        if r.status_code != 200:
            return None, "Groq %d: %s" % (r.status_code, r.text[:160])
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
            return json.loads(content[content.find("{"):content.rfind("}") + 1]), None
        except (ValueError, KeyError, IndexError):
            return None, "ответ не JSON"
    return None, "429 от Groq"


# --- 🔎 Конкуренты, цены и жалобы по нише (из сети, заранее) -----------------

WEB_SYSTEM = """You research one startup niche on the web for a market radar used by founders (Kazakhstan and CIS first, then global).
Find real products that serve this niche: global leaders and players in Kazakhstan/CIS. For each give the price if published, and what customers complain about (from reviews, forums, app stores).
Reply with JSON only:
{"competitors": [{"name": "...", "url": "https://...", "market": "global|US|EU|KZ|RU|CIS|MENA", "price": "e.g. $49/mo or 'not published'", "note": "one line: what it does / its weakness"}],
 "complaints": [{"text": "what customers dislike, one line", "source": "https://..."}],
 "pricing": "one line on typical pricing models in this niche",
 "icp": "one line: who buys first and where to find them"}
At most 8 competitors and 5 complaints, only ones you actually found with real URLs. Never invent."""


def web_dossier(niche, examples):
    """Сведения из сети по нише — заранее, чтобы «Глубже» отвечал за секунды. (словарь, ошибка)."""
    if not openrouter_key():
        return None, "нет OPENROUTER_API_KEY"
    user = "Niche: %s\nFunded companies in this niche: %s" % (niche, "; ".join(examples[:6]))
    return _chat_or(os.environ.get("LS_SEARCH_MODEL") or SEARCH_MODEL, WEB_SYSTEM, user, max_tokens=3000, timeout=150)


# --- ✂️ Дробление слишком широких ниш ------------------------------------------

SPLIT_SYSTEM = """A startup niche label is too broad to be useful for founders: "%s".
For each company below give a NARROWER niche: WHO the customer is plus WHAT job is done, 2-5 lowercase English words, no geography (e.g. "employee mental health benefits", "clinical trial recruitment", "remote patient monitoring"). Reuse the same narrower niche for companies that do the same thing; aim for clusters, not one label per company. Never output the broad label itself.
Reply JSON only: {"items": [{"id": "...", "niche": "...", "niche_ru": "..."}]} — niche_ru is the same niche in Russian."""


def split_niche(broad, items):
    """[(id, узкая ниша, по-русски)] для компаний широкой ниши. (список, ошибка)."""
    if not openrouter_key():
        return [], "нет OPENROUTER_API_KEY"
    data, err = _chat_or(os.environ.get("LS_BULK_MODEL") or OR_BULK_MODEL, SPLIT_SYSTEM % broad,
                         json.dumps({"items": items}, ensure_ascii=False), max_tokens=6000)
    if err or not isinstance(data, dict):
        return [], err or "не JSON"
    out = []
    for e in data.get("items") or []:
        if isinstance(e, dict) and e.get("id") and e.get("niche") and str(e["niche"]).lower().strip() != broad:
            out.append((str(e["id"]), re.sub(r"\s+", " ", str(e["niche"]).lower()).strip()[:48],
                        str(e.get("niche_ru") or "").strip()[:60]))
    return out, None


# --- 🙋 «Боль» из X -> ниша ---------------------------------------------------

DEMAND_SYSTEM = """You match posts where people ask for a product ("someone should build...", "I'd pay for...") to startup niches.
For each post pick the ONE niche from KNOWN NICHES whose products would solve the request, or "" if none fits closely. Reply JSON only: {"items": [{"id": "...", "niche": "..."}]}."""


def tag_demand(posts, niches):
    """[(id, ниша)] для постов спроса. Дёшево: одна пачка до 30 постов."""
    if not posts or not niches:
        return [], None
    user = json.dumps({"known_niches": niches[:60], "items": posts[:30]}, ensure_ascii=False)
    if openrouter_key():
        data, err = _chat_or(os.environ.get("LS_BULK_MODEL") or OR_BULK_MODEL, DEMAND_SYSTEM, user, max_tokens=2000)
    else:
        ok, why = available()
        if not ok:
            return [], why
        try:
            data, err = _chat("openai/gpt-oss-20b", DEMAND_SYSTEM, user, max_tokens=2000)
        except RateLimited as e:
            return [], "429 (%s)" % e
    if err or not isinstance(data, dict):
        return [], err or "не JSON"
    allowed = set(niches)
    return [(str(e.get("id")), e.get("niche")) for e in (data.get("items") or [])
            if isinstance(e, dict) and e.get("niche") in allowed], None


# --- 🔎 Поисковый запрос ниши для Google Trends --------------------------------

GT_TERMS_SYSTEM = """For each startup niche give the ONE short search phrase (1-3 lowercase English words) that buyers or users of such products actually type into Google, so its search interest over time reflects demand for the niche (e.g. "ai agents for customer support" -> "ai customer service", "employee mental health benefits" -> "employee mental health", "clinical trial recruitment" -> "clinical trial recruitment").
Prefer the common everyday wording over startup jargon. If no phrase would measure this niche without mixing in unrelated searches, give "".
Reply JSON only: {"items": [{"niche": "...", "term": "..."}]}"""


def gt_terms(niches):
    """{ниша: поисковая фраза} для пачки ниш (до 60). (словарь, ошибка)."""
    if not niches:
        return {}, None
    user = json.dumps({"niches": niches[:60]}, ensure_ascii=False)
    if openrouter_key():
        data, err = _chat_or(os.environ.get("LS_BULK_MODEL") or OR_BULK_MODEL, GT_TERMS_SYSTEM, user, max_tokens=4000)
    else:
        ok, why = available()
        if not ok:
            return {}, why
        try:
            data, err = _chat("openai/gpt-oss-20b", GT_TERMS_SYSTEM, user, max_tokens=4000)
        except RateLimited as e:
            return {}, "429 (%s)" % e
    if err or not isinstance(data, dict):
        return {}, err or "не JSON"
    allowed = set(niches)
    return {e["niche"]: re.sub(r"\s+", " ", str(e.get("term") or "").lower()).strip()[:40]
            for e in (data.get("items") or []) if isinstance(e, dict) and e.get("niche") in allowed}, None


def write_market_story(stats_text, lang="ru"):
    """
    Вывод «куда движется рынок» по готовой таблице цифр. (текст, ошибка).

    Модели запрещено придумывать цифры и компании: всё, что она может
    назвать, уже есть во входе. Её работа — связать опоры (деньги, отбор YC,
    запуски) в два-три предложения и назвать, где окно для небольшой команды.
    """
    ok, why = available()
    if not ok:
        return None, why
    model = os.environ.get("LS_AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    system = ("You write a market brief for founders deciding what to build next. "
              "Your only evidence is the investor data given: venture rounds by sector and niche, their stage, "
              "YC batch shares and analyst headlines. Never invent figures, companies or links, and never cite "
              "social-media hype. Reply as JSON {\"text\": \"...\"}: 3-5 short bullet lines in %s, each starting "
              "with •: (1) where money is moving, citing round counts and sums; (2) which niches are opening — "
              "several EARLY rounds (pre-seed/seed/A) in one niche — naming the companies; (3) what is crowded or "
              "cooling (late rounds, falling share); (4) one concrete product a small team could build next to an "
              "opening niche, e.g. a tool those funded companies' customers will need. Plain, no hype."
              % LANG_NAMES.get(lang, lang))
    try:
        data, err = _chat(model, system, stats_text, schema=None, max_tokens=1500)
    except RateLimited:
        return None, "429 от Groq"
    if err or not isinstance(data, dict) or not data.get("text"):
        return None, err or "пустой ответ"
    return data["text"].strip(), None


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
