# -*- coding: utf-8 -*-
"""
Рынок: куда движется спрос и деньги — по секторам и в цифрах.

    python market.py            посчитать и показать отчёт в консоли
    python market.py --refresh  сначала обновить данные (раунды, батчи YC)

ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ trends.py. Прежние «тренды» считали темы среди
НАЙДЕННЫХ ЗАПУСКОВ — то есть среди Show HN и твитов, которые прошли наши же
фильтры. Это зеркало фильтров, а не рынка: «ИИ-агенты 23 (было 0), ×1.6»
значило только то, что неделю назад разметки ещё не было (живой срез
2026-09-27). Стартаперу нужен ответ на другой вопрос: куда идут деньги и
спрос, и насколько быстро.

ОПОРЫ — только внешние данные, каждая с цифрой и источником:

  💰 Деньги. Раунды из новостей (Google News по секторам и стадиям, ленты
     TechCrunch и EU-Startups) и из постов основателей в X. Каждый
     заголовок разбирает ИИ: чей раунд, сумма, стадия, сектор, ниша.
     Инвестор голосует деньгами — это самый прямой сигнал спроса.
  🌱 Ранние раунды. Pre-seed, seed и A отдельно: туда, где их много,
     только начали ставить — там и открываются ниши для новых команд.
  🎓 Отбор YC. Доля сектора в последнем полном батче против среднего за три
     прошлых — по открытому каталогу yc-oss, история есть с первого дня.
     Пример: Industrials 4% в W24 → 24% в S26 (замерено 2026-09-28).
  🙋 Спрос людей. Посты в X вида «кто-нибудь, сделайте…», «заплатил бы за…»
     — по секторам. Опора слабая по объёму, поэтому весит меньше всех.

Наши собственные находки (X, HN, GitHub) в расчёт НЕ входят с 2026-09-28:
доля сектора среди того, что прошло наши фильтры, — зеркало фильтров.

Итог по сектору — «импульс»: взвешенная сумма изменений. По нему сектор
попадает в «растёт» / «остывает», а резкий переход в «растёт» рассылается
отдельным уведомлением тем, кто за этим сектором следит.

ЧЕСТНО ПРО ОГРАНИЧЕНИЯ. Google News отдаёт не больше 100 заголовков на
запрос — у самых горячих секторов окно упирается в потолок, и это помечено
в отчёте знаком «100+». Суммы раундов берутся из заголовков: где суммы нет,
сделка считается, а сумма — нет.
"""
import argparse
import email.utils
import hashlib
import html
import json
import math
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db                                   # noqa: E402
from common import UA, load_env, setup_logging  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Секторы. Крупнее, чем темы ИИ-разметки (их 45): в настройках человек
# выбирает из шестнадцати понятных направлений, а не из сорока пяти тегов.
# Порядок ключевых слов важен: первое совпадение — основной сектор.
# ---------------------------------------------------------------------------
SECTORS = [
    # id, эмодзи, {ru, kk, en}, ключевые слова (регулярки по тексту), запрос к Google News
    ("hardware", "🦾", {"ru": "Железо, роботы, производство", "kk": "Құрылғылар, роботтар, өндіріс",
                        "en": "Hardware, robotics, manufacturing"},
     [r"\brobot", r"humanoid", r"\bhardware\b", r"manufactur", r"physical ai", r"semiconductor",
      r"\bchips?\b", r"\bdrones?\b", r"\bsensors?\b", r"\bfactor(y|ies)\b", r"industrial",
      r"3d print", r"\bdevices?\b", r"wearable", r"\bgadget"],
     '(robotics OR robot OR humanoid OR hardware OR manufacturing OR "physical AI" OR semiconductor OR drone)'),
    ("defense_space", "🛰", {"ru": "Оборона и космос", "kk": "Қорғаныс және ғарыш", "en": "Defense and space"},
     [r"defen[cs]e", r"military", r"\bspace\b", r"satellite", r"rocket", r"aerospace", r"unmanned"],
     '(defense OR defence OR "defense tech" OR aerospace OR satellite OR space)'),
    ("energy", "⚡", {"ru": "Энергетика и климат", "kk": "Энергетика және климат", "en": "Energy and climate"},
     [r"\benerg", r"\bgrid\b", r"solar", r"batter(y|ies)", r"nuclear", r"fusion", r"climate",
      r"carbon", r"\bev charg", r"geotherm"],
     '(energy OR climate OR grid OR battery OR solar OR nuclear OR fusion)'),
    ("mobility", "🚚", {"ru": "Логистика и транспорт", "kk": "Логистика және көлік", "en": "Logistics and mobility"},
     [r"logistic", r"supply chain", r"freight", r"shipping", r"\bfleet", r"mobility", r"autonomous (vehicle|driving|truck)",
      r"\bdelivery\b", r"warehouse", r"maritime"],
     '(logistics OR "supply chain" OR freight OR mobility OR "autonomous vehicle" OR warehouse)'),
    ("ai_agents", "🤖", {"ru": "ИИ-агенты и автоматизация", "kk": "ЖИ-агенттер және автоматтандыру",
                         "en": "AI agents and automation"},
     [r"\bagents?\b", r"agentic", r"copilot", r"workflow automation", r"\bautomat", r"browser automation",
      r"computer use", r"\bvoice ai\b", r"ai (assistant|employee|worker)"],
     '("AI agent" OR "AI agents" OR agentic OR "AI automation")'),
    ("ai_infra", "🧠", {"ru": "ИИ-модели и инфраструктура", "kk": "ЖИ-модельдер және инфрақұрылым",
                        "en": "AI models and infrastructure"},
     [r"\bllms?\b", r"foundation model", r"\binference\b", r"\bgpus?\b", r"neocloud", r"data ?cent(er|re)",
      r"\bmodels?\b", r"fine-?tun", r"\btraining\b", r"\bcompute\b", r"\bcloud\b", r"open[- ]source model"],
     '("AI infrastructure" OR inference OR GPU OR "foundation model" OR LLM OR "data center" OR neocloud)'),
    ("devtools", "🛠", {"ru": "Инструменты разработчика", "kk": "Әзірлеуші құралдары", "en": "Developer tools"},
     [r"developer", r"devtool", r"\bcode\b", r"coding", r"\bapi\b", r"\bsdk\b", r"database", r"observab",
      r"testing", r"\bci\b", r"open[- ]source", r"terminal", r"\bcli\b", r"github"],
     '("developer tools" OR devtools OR "developer platform" OR "coding assistant" OR database OR observability)'),
    ("security", "🔐", {"ru": "Кибербезопасность", "kk": "Киберқауіпсіздік", "en": "Cybersecurity"},
     [r"secur", r"cyber", r"identity", r"fraud", r"privacy", r"compliance", r"vulnerab"],
     '(cybersecurity OR "security startup" OR identity OR fraud)'),
    ("fintech", "💳", {"ru": "Финтех", "kk": "Финтех", "en": "Fintech"},
     [r"fintech", r"payment", r"\bbank", r"payroll", r"accounting", r"invoic", r"insur", r"wealth",
      r"stablecoin", r"\btax\b", r"expense", r"treasury", r"\bcfo\b"],
     '(fintech OR payments OR banking OR payroll OR accounting OR insurtech OR stablecoin)'),
    ("health", "🩺", {"ru": "Здоровье и биотех", "kk": "Денсаулық және биотех", "en": "Health and biotech"},
     [r"health", r"medic", r"clinic", r"biotech", r"\bdrug", r"pharma", r"therap", r"patient",
      r"diagnos", r"hospital", r"\bbio\b", r"fitness", r"mental"],
     '(healthtech OR "health tech" OR medtech OR biotech OR healthcare OR clinical)'),
    ("consumer", "🛍", {"ru": "Потребительские продукты", "kk": "Тұтынушы өнімдері", "en": "Consumer products"},
     [r"consumer", r"social", r"dating", r"beauty", r"fashion", r"apparel", r"\bfood\b", r"\bd2c\b",
      r"\bdtc\b", r"\bbrand\b", r"gaming", r"\bgames?\b", r"travel", r"\bpets?\b", r"parent", r"creator"],
     '(consumer OR "consumer app" OR beauty OR fashion OR "food startup" OR DTC OR gaming)'),
    ("commerce", "🛒", {"ru": "E-commerce и ритейл", "kk": "E-commerce және бөлшек сауда", "en": "E-commerce and retail"},
     [r"e-?commerce", r"retail", r"shopping", r"marketplace", r"merchant", r"shopify", r"\bstores?\b"],
     '(ecommerce OR "e-commerce" OR retail OR marketplace)'),
    ("b2b_saas", "📊", {"ru": "B2B-софт: продажи, маркетинг, HR", "kk": "B2B-бағдарламалар: сату, маркетинг, HR",
                        "en": "B2B software: sales, marketing, HR"},
     [r"\bsaas\b", r"\bcrm\b", r"\bsales\b", r"marketing", r"\bseo\b", r"recruit", r"hiring", r"\bhr\b",
      r"legal", r"\blaw", r"customer support", r"productivity", r"\bb2b\b", r"enterprise", r"analytics"],
     '(SaaS OR CRM OR "sales software" OR martech OR "HR tech" OR legaltech)'),
    ("edu", "🎓", {"ru": "Образование", "kk": "Білім беру", "en": "Education"},
     [r"educat", r"edtech", r"learn", r"tutor", r"student", r"course", r"school"],
     '(edtech OR education OR "learning platform")'),
    ("proptech", "🏗", {"ru": "Недвижимость и стройка", "kk": "Жылжымайтын мүлік және құрылыс",
                        "en": "Real estate and construction"},
     [r"real estate", r"proptech", r"construction", r"housing", r"propert", r"rental", r"mortgage"],
     '(proptech OR "real estate" OR construction OR housing)'),
    ("crypto", "🪙", {"ru": "Крипто и web3", "kk": "Крипто және web3", "en": "Crypto and web3"},
     [r"crypto", r"blockchain", r"web3", r"\bdefi\b", r"\bnft", r"on-?chain", r"\btoken"],
     '(crypto OR blockchain OR web3)'),
]
SECTOR_IDS = [s[0] for s in SECTORS]
SECTOR = {s[0]: {"emoji": s[1], "names": s[2], "kw": [re.compile(p, re.I) for p in s[3]], "q": s[4]}
          for s in SECTORS}
# «Физический продукт» — отдельной строкой: ради этого вопроса («правда ли,
# что спрос уходит в физикалы») отчёт и затевался.
PHYSICAL = ("hardware", "defense_space", "energy", "mobility")

# Темы ИИ-разметки (ai.TOPICS) -> секторы. Нужны, чтобы находки в ленте и
# рассылке получили сектор без лишнего запроса к модели.
TOPIC_TO_SECTOR = {
    "ai agents": "ai_agents", "browser automation": "ai_agents", "chatbots & support": "ai_agents",
    "voice ai": "ai_agents", "coding assistants": "devtools", "devtools": "devtools",
    "testing & qa": "devtools", "observability": "devtools", "databases": "devtools",
    "no-code": "devtools", "open-source models": "ai_infra", "local & on-device ai": "ai_infra",
    "infrastructure & cloud": "ai_infra", "video generation": "ai_infra", "image generation": "ai_infra",
    "security": "security", "privacy": "security", "data & analytics": "b2b_saas",
    "design tools": "b2b_saas", "creator tools": "consumer", "productivity": "b2b_saas",
    "notes & knowledge": "b2b_saas", "email & calendar": "b2b_saas", "sales & crm": "b2b_saas",
    "marketing & seo": "b2b_saas", "hr & recruiting": "b2b_saas", "legal": "b2b_saas",
    "e-commerce": "commerce", "payments": "fintech", "accounting & invoicing": "fintech",
    "health & fitness": "health", "mental health": "health", "education": "edu",
    "language learning": "edu", "real estate": "proptech", "travel": "consumer",
    "food & delivery": "consumer", "social & community": "consumer", "dating": "consumer",
    "gaming": "consumer", "robotics": "hardware", "hardware": "hardware",
    "climate & energy": "energy", "crypto infrastructure": "crypto",
}

# Подкатегории каталога YC -> сектор. Поле subindustry заполнено у ~65%
# компаний, теги — от 28% (Spring 2026) до 99%: на теги опираться нельзя,
# доля «железа» по тегам прыгала 29% → 5% → 29% от батча к батчу просто
# из-за полноты разметки (замерено 2026-09-28).
YC_SUB = {
    "Industrials -> Manufacturing and Robotics": "hardware", "Industrials -> Drones": "hardware",
    "Industrials -> Agriculture": "hardware", "Industrials": "hardware",
    "Industrials -> Defense": "defense_space", "Industrials -> Aviation and Space": "defense_space",
    "Industrials -> Energy": "energy", "Industrials -> Climate": "energy",
    "B2B -> Engineering, Product and Design": "devtools", "B2B -> Infrastructure": "ai_infra",
    "B2B -> Security": "security", "B2B -> Supply Chain and Logistics": "mobility",
    "B2B -> Finance and Accounting": "fintech", "B2B -> Retail": "commerce",
    "B2B -> Productivity": "b2b_saas", "B2B -> Operations": "b2b_saas", "B2B -> Marketing": "b2b_saas",
    "B2B -> Legal": "b2b_saas", "B2B -> Analytics": "b2b_saas", "B2B -> Sales": "b2b_saas",
    "B2B -> Recruiting and Talent": "b2b_saas", "B2B -> Human Resources": "b2b_saas",
    "B2B -> Office Management": "b2b_saas",
    "Consumer -> Consumer Electronics": "hardware", "Consumer -> Transportation Services": "mobility",
    "Education": "edu", "Government": "defense_space",
}
YC_INDUSTRY = {"Healthcare": "health", "Fintech": "fintech", "Consumer": "consumer",
               "Real Estate and Construction": "proptech", "Education": "edu", "Industrials": "hardware"}


def sector_name(sid, lang="ru"):
    s = SECTOR.get(sid)
    return s["names"].get(lang) or s["names"]["ru"] if s else sid


def classify(text, limit=2):
    """Секторы по тексту: не больше двух, по порядку совпадения в SECTORS."""
    low = (text or "").lower()
    out = []
    for sid in SECTOR_IDS:
        if any(p.search(low) for p in SECTOR[sid]["kw"]):
            out.append(sid)
            if len(out) >= limit:
                break
    return out


def sectors_for(topics=None, text=None):
    """Секторы находки: из тем ИИ-разметки, а без них — по тексту."""
    out = []
    for t in topics or []:
        s = TOPIC_TO_SECTOR.get(t)
        if s and s not in out:
            out.append(s)
    if not out:
        out = classify(text)
    return out[:2]


# ---------------------------------------------------------------------------
# База
# ---------------------------------------------------------------------------
def _ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deals ("
        " key TEXT PRIMARY KEY, ts INTEGER, seen INTEGER, title TEXT, url TEXT, outlet TEXT,"
        " company TEXT, amount_usd REAL, sectors TEXT, sent INTEGER DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS deals_ts ON deals (ts)")
    # Разбор сделки ИИ (2026-09-28): стадия, ниша, что делает компания.
    # ai = 0 — ещё не разобрана, 1 — разобрана; is_round = 0 — модель
    # решила, что это не раунд (обзор, фонд, IPO), такие не считаются.
    have = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    for col, typ in (("stage", "TEXT"), ("niche", "TEXT"), ("what", "TEXT"), ("country", "TEXT"),
                     ("ai", "INTEGER DEFAULT 0"), ("is_round", "INTEGER DEFAULT 1"), ("src", "TEXT"),
                     ("investors", "TEXT")):
        if col not in have:
            conn.execute("ALTER TABLE deals ADD COLUMN %s %s" % (col, typ))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS demand ("
        " ext_id TEXT PRIMARY KEY, ts INTEGER, text TEXT, url TEXT, likes INTEGER, sectors TEXT)")
    # Ниша, которую решил бы продукт по запросу («сделайте кто-нибудь…»):
    # '' — разобрано, подходящей ниши нет; NULL — ещё не разобрано.
    if "niche" not in {r[1] for r in conn.execute("PRAGMA table_info(demand)")}:
        conn.execute("ALTER TABLE demand ADD COLUMN niche TEXT")


# ---------------------------------------------------------------------------
# 💰 Деньги: раунды из Google News
# ---------------------------------------------------------------------------
GNEWS = "https://news.google.com/rss/search"
VERBS = r"(raises?|raised|secures?|closes?|lands?|nabs?|bags?|snags?|gets?|announces?|emerges?)"
RAISE_VERB = re.compile(r"\b" + VERBS + r"\b[^|]{0,60}?(\$|€|£|eur\b|funding|seed|series|round|investment)", re.I)
# «$71 million Seed round», «$114 million Series D» — раунд без глагола.
AMOUNT_ROUND = re.compile(r"(\$|€|£|eur\s?)\s?[\d.,]+\s?(k|m|mn|million|b|bn|billion)\b[^|]{0,25}?"
                          r"\b(pre-?seed|seed|series [a-z]|funding round|round)\b", re.I)
AMOUNT = re.compile(r"(\$|€|£|US\$|USD\s?|EUR\s?)\s?([\d]+(?:[.,]\d+)?)\s?(k|m|mn|million|b|bn|billion)\b", re.I)
FX = {"$": 1.0, "us$": 1.0, "usd": 1.0, "€": 1.1, "eur": 1.1, "£": 1.3}
MULT = {"k": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
# Не раунды, хотя слова похожи: фонды собирают деньги на себя, IPO и
# «в переговорах» — ещё не сделка, «raises guidance» — отчётность.
NOT_A_ROUND = re.compile(r"(\bfund (i|ii|iii|iv|v|vi)\b|\bcloses? .{0,20}\bfund\b|\bnew fund\b|\bventure fund\b|"
                         r"\bipo\b|\blayoffs?\b|\blawsuit|valuation cut|\bin talks\b|raises? (guidance|questions|"
                         r"concerns|prices|rates|forecast)|\bweek.s \d+ biggest\b|\blargest\b.{0,30}\brounds\b)", re.I)


# Заголовок-обзор, а не новость о сделке: доли, рекорды, кварталы, итоги.
ANALYSIS = re.compile(r"(sector snapshot|\breport\b|\brecord\b|\bsurg(e|es|ing)\b|\bboom|\bfad(es|ing)\b|"
                      r"\bfalls?\b|\bclimbs?\b|data shows|\bquarter\b|\bq[1-4]\b|\d+%|biggest funding rounds|"
                      r"\bpour\b|investors (favou?r|flock|look|bet)|\bhalf\b|year[- ]over[- ]year|\byoy\b)", re.I)


# Индийские суммы: «Rs 100 Cr», «₹7.1 Crore», «INR 50 lakh». Индия — один из
# самых частых источников заголовков о раундах, и без этого такие сделки
# шли «без суммы» (живая выдача 2026-09-28: Balwaan Krishi, Primerry).
INR = re.compile(r"(?:rs\.?|inr|₹)\s?([\d]+(?:[.,]\d+)?)\s?(cr|crore|lakh|lac)\b", re.I)
INR_USD = 0.012

# Стадия раунда. Ранние (pre-seed, seed, A) — сигнал того, что ниша только
# зарождается; поздние (C и дальше) — что она уже взрослая и занята.
STAGES = ("pre-seed", "seed", "a", "b", "c+", "growth")
EARLY = ("pre-seed", "seed", "a")
_STAGE_RE = (
    ("pre-seed", re.compile(r"\bpre-?seed\b", re.I)),
    ("seed", re.compile(r"\bseed\b", re.I)),
    ("a", re.compile(r"\bseries a\b", re.I)),
    ("b", re.compile(r"\bseries b\b", re.I)),
    ("c+", re.compile(r"\bseries [c-k]\b", re.I)),
    ("growth", re.compile(r"\b(growth round|growth equity|late-stage|pre-ipo)\b", re.I)),
)


def parse_stage(t):
    for name, rx in _STAGE_RE:
        if rx.search(t or ""):
            return name
    return None


def _amount(t):
    """Сумма раунда в USD — но не оценка компании («at a $10B valuation»)."""
    m = INR.search(t)
    if m:
        try:
            n = float(m.group(1).replace(",", "."))
        except ValueError:
            n = 0
        return n * (1e7 if m.group(2).lower().startswith("cr") else 1e5) * INR_USD or None
    for a in AMOUNT.finditer(t):
        before, after = t[max(0, a.start() - 14):a.start()].lower(), t[a.end():a.end() + 14].lower()
        if "valuation" in after or "valued" in before or re.search(r"\bat( a)?\s*$", before):
            continue
        cur = a.group(1).lower().strip()
        try:
            v = float(a.group(2).replace(",", ".")) * MULT[a.group(3).lower()] * FX.get(cur, 1.0)
        except (ValueError, KeyError):
            continue
        return v if v <= 2e10 else None               # больше $20 млрд — это не раунд, а опечатка
    return None


def parse_deal(title):
    """(компания, сумма в USD или None) из заголовка — или None, если это не раунд."""
    t = html.unescape(title or "")
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t)          # « - TechCrunch» в конце
    if NOT_A_ROUND.search(t) or not (RAISE_VERB.search(t) or AMOUNT_ROUND.search(t)):
        return None
    m = re.search(r"^(.{2,80}?)\s+" + VERBS + r"\b", t, re.I) or \
        re.search(r"^(.{2,80}?)\s+(valued|with|closes|completes)\b", t, re.I)
    company = (m.group(1) if m else t[:40]).strip(" :-–—'\"")
    # «AI startup Instinct» -> «Instinct»: последние два слова перед глаголом
    # обычно и есть название; для ключа дедупликации этого хватает.
    words = re.findall(r"[\w.&'-]+", company)
    # Описания перед названием пишутся строчными («startup», «parent»),
    # само название — с заглавной: берём хвост из слов с заглавной буквы.
    tail = []
    for w in reversed(words):
        if not (w[:1].isupper() or w[:1].isdigit()):
            break
        tail.insert(0, w)
    company = " ".join(tail[-2:]) if tail else (" ".join(words[-2:]) if words else company)
    return company, _amount(t)


def _deal_key(company, amount):
    """Одна сделка у пяти изданий — один ключ: компания + порядок суммы."""
    c = re.sub(r"[^a-z0-9]", "", (company or "").lower())[:24]
    bucket = "%d" % round(math.log10(amount), 1) if amount else "na"
    return "%s|%s" % (c, bucket)


def fetch_gnews(query, timeout=20):
    """Заголовки Google News: [(title, url, outlet, ts)], ошибка."""
    try:
        r = requests.get(GNEWS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                         headers={"User-Agent": UA}, timeout=timeout)
    except requests.RequestException as e:
        return [], str(e)[:120]
    if r.status_code != 200:
        return [], "HTTP %d" % r.status_code
    out = []
    for it in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        title = re.search(r"<title>(.*?)</title>", it, re.S)
        link = re.search(r"<link>(.*?)</link>", it, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        if not title:
            continue
        try:
            ts = int(email.utils.parsedate_to_datetime(date.group(1)).timestamp()) if date else None
        except (TypeError, ValueError):
            ts = None
        out.append((html.unescape(title.group(1)).strip(), link.group(1).strip() if link else "",
                    html.unescape(src.group(1)).strip() if src else "", ts))
    return out, None


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


WINDOW_DAYS = 14

# Ленты изданий о венчуре: сделки из них не привязаны к сектору запроса,
# сектор ставит разбор ИИ (или ключевые слова, пока разбора нет).
# Crunchbase News и FinSMEs отвечают 403 на RSS (проверено 2026-09-28).
FEEDS = (
    ("TechCrunch", "https://techcrunch.com/category/venture/feed/"),
    ("EU-Startups", "https://www.eu-startups.com/feed/"),
    # Crunchbase News: полный текст статей прямо в RSS — и еженедельный
    # «топ-10 раундов» по данным Crunchbase (с суммами, стадиями, иногда
    # выручкой), и аналитика по секторам. Отвечает только на полноценный
    # браузерный User-Agent: на короткий — 403 (проверено 2026-09-29).
    # API Crunchbase платный с 2025 года, бесплатного тарифа нет, а сайт
    # закрыт Cloudflare и запрещает сбор правилами — поэтому только RSS.
    ("Crunchbase News", "https://news.crunchbase.com/feed/"),
)
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")
# Раунды любых секторов за последние дни — чтобы в поток денег попадало и
# то, что не легло ни в один секторный запрос.
GENERAL_QUERIES = (
    '(raises OR raised OR secures OR lands) ("pre-seed" OR "seed round" OR "seed funding") startup when:3d',
    '(raises OR raised OR secures) ("Series A" OR "Series B") startup when:3d',
    '(raises OR raised) ("Series C" OR "Series D" OR "growth round") startup when:4d',
)


def _strip_html(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(t or ""))).strip()


def fetch_feed(url, timeout=20, full=False):
    """
    RSS издания: [(title, url, outlet, ts)] или, при full=True,
    [(title, url, ts, текст статьи, рубрики)]. Вторым — ошибка.
    """
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
    except requests.RequestException as e:
        return [], str(e)[:120]
    if r.status_code != 200:
        return [], "HTTP %d" % r.status_code
    out = []
    for it in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        link = re.search(r"<link>(.*?)</link>", it, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not title:
            continue
        try:
            ts = int(email.utils.parsedate_to_datetime(date.group(1)).timestamp()) if date else None
        except (TypeError, ValueError):
            ts = None
        t, u = html.unescape(title.group(1)).strip(), link.group(1).strip() if link else ""
        if full:
            body = re.search(r"<content:encoded>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</content:encoded>", it, re.S)
            cats = re.findall(r"<category>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</category>", it)
            out.append((t, u, ts, _strip_html(body.group(1)) if body else "", [html.unescape(c) for c in cats]))
        else:
            out.append((t, u, "", ts))
    return out, None


def _clean_title(title):
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", html.unescape(title or "")).strip()


def add_deal(conn, title, url, outlet, ts, now, sid=None, src="news", company=None, amount=None):
    """
    Записать заголовок о раунде. Возвращает True, если это раунд.

    Ключ — компания + порядок суммы: одна сделка у пяти изданий с одинаковой
    формулировкой ложится в одну строку. Разные формулировки («startup
    Amaani» и «Amaani») склеивает потом разбор ИИ по названию компании.
    """
    t = _clean_title(title)
    if company is None:
        d = parse_deal(title)
        if not d:
            return False
        company, amount = d
    key = _deal_key(company, amount)
    row = conn.execute("SELECT sectors FROM deals WHERE key = ?", (key,)).fetchone()
    if row:
        if sid:
            secs = set(json.loads(row["sectors"] or "[]"))
            secs.add(sid)
            conn.execute("UPDATE deals SET sectors = ? WHERE key = ? AND ai = 0",
                         (json.dumps(sorted(secs)), key))
        return True
    secs = [sid] if sid else classify(t, limit=1)
    conn.execute(
        "INSERT INTO deals (key, ts, seen, title, url, outlet, company, amount_usd, sectors, stage, src) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, min(ts or now, now), now, t[:220], url, (outlet or "")[:60], (company or "")[:80], amount,
         json.dumps(secs), parse_stage(t), src))
    return True


def refresh_deals(conn, now, verbose=True):
    """
    Раунды по каждому сектору за последние четыре недели — по неделе на
    запрос. Неделями, а не двумя окнами по 14 дней (2026-09-29): так живой
    сбор режет время теми же кусками, что и загрузка истории за полгода
    (backfill_deals), и недели сравнимы между собой — у Google News потолок
    в 100 заголовков на запрос, и 14-дневное окно упиралось в него чаще.
    64 запроса раз в 6 часов — вежливо для RSS.
    """
    _ensure(conn)
    stats, errors, analysis = {}, [], {}
    windows = [("w%d" % k, now - (k + 1) * 7 * 86400, now + 86400 if k == 0 else now - k * 7 * 86400)
               for k in range(4)]
    for sid in SECTOR_IDS:
        for name, a, b in windows:
            q = '%s (startup OR company) (raises OR funding OR "seed round" OR "series a" OR "series b") ' \
                'after:%s before:%s' % (SECTOR[sid]["q"], _day(a), _day(b))
            rows, err = fetch_gnews(q)
            if err:
                errors.append("%s/%s: %s" % (sid, name, err))
                if err.startswith("HTTP 429") or err.startswith("HTTP 503"):
                    break
                continue
            stats[(sid, name)] = {"raw": len(rows)}
            for title, url, outlet, ts in rows:
                if not add_deal(conn, title, url, outlet, ts, now, sid=sid):
                    if name in ("w0", "w1") and ANALYSIS.search(title) and (ts or 0) >= now - 21 * 86400:
                        analysis.setdefault(sid, []).append(
                            {"title": _clean_title(title)[:200], "url": url, "outlet": outlet, "ts": ts})
            time.sleep(0.8)
    for q in GENERAL_QUERIES:
        rows, err = fetch_gnews(q)
        if err:
            errors.append("общий: %s" % err)
            continue
        for title, url, outlet, ts in rows:
            add_deal(conn, title, url, outlet, ts, now)
        time.sleep(0.8)
    for outlet, url in FEEDS:
        rows, err = fetch_feed(url, full=True)
        if err:
            errors.append("%s: %s" % (outlet, err))
            continue
        for title, link, ts, text, cats in rows:
            is_deal = add_deal(conn, title, link, outlet, ts, now)
            store_article(conn, outlet, title, link, ts or now, text, cats, is_deal, now)
    conn.commit()
    db.kv_set(conn, "market_deal_raw", json.dumps({"%s|%s" % k: v["raw"] for k, v in stats.items()}))
    # Обзоры по сектору («Sector Snapshot: Robotics Startups On Fire…») —
    # это и есть аналитические сводки: их показывает карточка сектора.
    db.kv_set(conn, "market_analysis", json.dumps(
        {sid: sorted(v, key=lambda d: -(d["ts"] or 0))[:4] for sid, v in analysis.items()},
        ensure_ascii=False))
    if verbose:
        n = conn.execute("SELECT COUNT(*) n FROM deals WHERE ts >= ?", (now - 28 * 86400,)).fetchone()["n"]
        print("  рынок: раундов за 28 дней в базе %d%s" % (n, (" — " + "; ".join(errors[:2])) if errors else ""))
    return not errors or len(errors) < len(SECTOR_IDS)


# Приставки и хвосты в названиях, которые не отличают одну компанию от
# другой: «Dextr AI» и «Dextr», «Foo Labs Inc.» и «Foo» — одна сделка.
_CO_NOISE = re.compile(r"\b(inc|ltd|llc|gmbh|corp|co|labs?|technologies|technology|hq|the)\b\.?", re.I)


def company_norm(name):
    n = _CO_NOISE.sub(" ", (name or "").lower())
    n = re.sub(r"\bai\b$", "", n.strip())
    return re.sub(r"[^a-z0-9]", "", n)[:32]


def known_niches(conn, now, days=60, limit=80):
    """Ниши последних двух месяцев, самые частые первыми — словарь для ИИ."""
    rows = conn.execute("SELECT niche, COUNT(*) n FROM deals WHERE niche IS NOT NULL AND niche != '' "
                        "AND is_round = 1 AND ts >= ? GROUP BY niche ORDER BY n DESC LIMIT ?",
                        (now - days * 86400, limit)).fetchall()
    return [r["niche"] for r in rows]


def niche_names(conn):
    try:
        return json.loads(db.kv_get(conn, "niche_names", "{}") or "{}")
    except ValueError:
        return {}


def niche_label(niche, lang, names=None):
    """Ниша на языке читателя: русское название из разбора, иначе английское."""
    if lang == "en":
        return niche
    return (names or {}).get(niche) or niche


def enrich_deals(conn, now, verbose=True):
    """
    Очередь разбора раундов: ИИ читает заголовок и отвечает, раунд ли это,
    чей, на сколько, какая стадия, сектор и ниша. Возвращает число разобранных.

    Зачем. Регулярка по заголовку ошибалась ровно там, где это важно: одна
    сделка Amaani в пяти изданиях давала пять «разных» компаний («startup
    Amaani», «parent Amaani»…), суммы в рупиях терялись, обзоры сходили за
    сделки (живая выдача 2026-09-28). А ниша — то, ради чего стартапер вообще
    открывает отчёт, — регуляркой не определяется в принципе.
    """
    import ai
    _ensure(conn)
    # Свежие первыми, затем история за полгода: с ключом OpenRouter за
    # прогон уходит 200 заголовков, на Groq — 40 (его минутный лимит).
    per_run = ai.DEAL_BATCH * (ai.OR_BATCHES_PER_RUN if ai.openrouter_key() else ai.DEAL_BATCHES_PER_RUN)
    rows = conn.execute(
        "SELECT key, title, outlet, src FROM deals WHERE ai = 0 AND ts >= ? ORDER BY ts DESC LIMIT ?",
        (now - (HISTORY_WEEKS * 7 + 3) * 86400, per_run)).fetchall()
    if not rows:
        return 0
    ids = {str(i): r["key"] for i, r in enumerate(rows)}
    items = [{"id": str(i), "text": r["title"] + ((" [%s]" % r["outlet"]) if r["outlet"] else "")}
             for i, r in enumerate(rows)]
    answers, answered, err = ai.extract_deals(
        conn, now, items, [(sid, SECTOR[sid]["names"]["en"]) for sid in SECTOR_IDS], known_niches(conn, now))
    names, done = niche_names(conn), set()
    for e in answers:
        key = ids.get(str(e.get("id")))
        if not key or key in done:
            continue
        done.add(key)
        if not e.get("is_round"):
            conn.execute("UPDATE deals SET ai = 1, is_round = 0 WHERE key = ?", (key,))
            continue
        sector = e.get("sector") if e.get("sector") in SECTOR else None
        nch = e.get("niche") if isinstance(e.get("niche"), dict) else {"en": e.get("niche")}
        niche = re.sub(r"\s+", " ", str(nch.get("en") or "").lower()).strip()[:48] or None
        if niche and nch.get("ru"):
            names.setdefault(niche, str(nch["ru"]).strip()[:60])
        try:
            usd = float(e.get("usd")) if e.get("usd") not in (None, "") else None
        except (TypeError, ValueError):
            usd = None
        usd = usd if usd and 1e4 <= usd <= 2e10 else None
        stage = e.get("stage") if e.get("stage") in STAGES else None
        what = e.get("what") if isinstance(e.get("what"), dict) else {}
        what = {k: str(what[k]).strip()[:140] for k in ("ru", "en") if what.get(k)}
        raw_inv = e.get("investors") if isinstance(e.get("investors"), list) else []
        inv = [str(x).strip()[:40] for x in raw_inv if str(x).strip()][:3]
        conn.execute(
            "UPDATE deals SET ai = 1, is_round = 1, company = COALESCE(?, company), "
            "amount_usd = COALESCE(?, amount_usd), stage = COALESCE(?, stage), "
            "sectors = COALESCE(?, sectors), niche = ?, what = ?, country = ?, investors = ? WHERE key = ?",
            ((e.get("company") or "").strip()[:80] or None, usd, stage,
             json.dumps([sector]) if sector else None, niche,
             json.dumps(what, ensure_ascii=False) if what else None,
             str(e.get("country") or "")[:2].upper(), json.dumps(inv) if inv else None, key))
    # Строки из отвеченных пачек, которые модель пропустила, — не повторять
    # каждый прогон: помечаем разобранными как есть.
    for i in answered:
        if ids.get(i) and ids[i] not in done:
            conn.execute("UPDATE deals SET ai = 1 WHERE key = ?", (ids[i],))
    db.kv_set(conn, "niche_names", json.dumps(names, ensure_ascii=False))
    conn.commit()
    if verbose and (done or err):
        print("  раунды разобраны ИИ: %d из %d%s" % (len(done), len(rows), (" — " + err) if err else ""))
    return len(done)


def rounds(conn, since, until=None):
    """
    Раунды за период — по одному на компанию, а не по заголовку.

    Одна сделка в пяти изданиях — пять строк в deals; здесь они склеиваются
    по названию компании. Число изданий сохраняется: сделку, о которой
    написали пятеро, видно лучше той, что нашлась в одной заметке.
    """
    _ensure(conn)
    rows = conn.execute("SELECT * FROM deals WHERE ts >= ? AND ts < ? AND is_round = 1 ORDER BY ts",
                        (since, until or 2 ** 40)).fetchall()
    groups = {}
    for r in rows:
        k = company_norm(r["company"]) or r["key"]
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"company": r["company"], "usd": None, "stage": None, "niche": None, "what": {},
                             "sectors": [], "ts": r["ts"], "url": r["url"], "title": r["title"],
                             "outlets": set(), "keys": [], "sent": False, "ai": False, "country": "", "investors": []}
        g["keys"].append(r["key"])
        g["outlets"].add(r["outlet"] or r["url"])
        g["sent"] = g["sent"] or bool(r["sent"])
        g["usd"] = max(g["usd"] or 0, r["amount_usd"] or 0) or None
        secs = json.loads(r["sectors"] or "[]")
        if r["ai"]:
            # Разобранная ИИ строка главнее: у неё чистое название, сектор и ниша.
            if not g["ai"]:
                g.update(company=r["company"], sectors=[], url=r["url"], title=r["title"])
            g["ai"] = True
            g["stage"] = g["stage"] or r["stage"]
            g["niche"] = g["niche"] or r["niche"]
            g["country"] = g["country"] or (r["country"] or "")
            for inv in json.loads(r["investors"] or "[]"):
                if inv not in g["investors"]:
                    g["investors"].append(inv)
            if r["what"] and not g["what"]:
                try:
                    g["what"] = json.loads(r["what"])
                except ValueError:
                    pass
        elif not g["ai"]:
            g["stage"] = g["stage"] or r["stage"]
        if r["ai"] or not g["ai"]:
            g["sectors"] += [s for s in secs if s in SECTOR and s not in g["sectors"]]
    out = []
    for g in groups.values():
        g["outlets"] = len(g["outlets"])
        g["sectors"] = g["sectors"][:2]
        out.append(g)
    return out


def money(conn, now):
    """{сектор: {cur_n, prev_n, cur_usd, prev_usd, cur_early, prev_early, sat, top}} за два окна."""
    _ensure(conn)
    raw = {}
    try:
        raw = json.loads(db.kv_get(conn, "market_deal_raw", "{}") or "{}")
    except ValueError:
        pass
    cut_cur, cut_prev = now - WINDOW_DAYS * 86400, now - 2 * WINDOW_DAYS * 86400
    out = {sid: {"cur_n": 0, "prev_n": 0, "cur_usd": 0.0, "prev_usd": 0.0, "cur_early": 0, "prev_early": 0,
                 "top": [], "sat": max(raw.get("%s|w0" % sid, 0), raw.get("%s|w1" % sid, 0),
                                       raw.get("%s|cur" % sid, 0)) >= 95} for sid in SECTOR_IDS}
    for r in rounds(conn, cut_prev):
        side = "cur" if r["ts"] >= cut_cur else "prev"
        for sid in r["sectors"]:
            o = out[sid]
            o[side + "_n"] += 1
            o[side + "_usd"] += r["usd"] or 0.0
            o[side + "_early"] += 1 if r["stage"] in EARLY else 0
            if side == "cur":
                o["top"].append(r)
    for sid, o in out.items():
        o["top"] = [_round_brief(r) for r in sorted(o["top"], key=lambda r: -(r["usd"] or 0))[:3]]
    return out


def _round_brief(r):
    return {"company": r["company"], "title": r["title"], "url": r["url"], "usd": r["usd"],
            "stage": r["stage"], "what": r["what"], "niche": r["niche"], "ts": r["ts"],
            "outlets": r["outlets"], "investors": r.get("investors") or []}


NICHE_DAYS = 28
NICHE_MIN = 3            # раундов в нише за окно, чтобы о ней говорить
HISTORY_WEEKS = 26       # история за полгода — для кривых ниш и секторов
# Мегараунд (от $1 млрд) не складывается в сумму ниши: один Prometheus на
# $12 млрд делал «ИИ-агентов для предприятий» нишей на $15 млрд и прятал,
# сколько получили остальные 30 компаний (2026-09-29). Он показывается отдельно.
MEGA_USD = 1e9


def week_index(ts, now):
    """Номер недели от текущей: 0 — последние 7 дней, 1 — неделя до них…"""
    return int((now - ts) // (7 * 86400))


def niches(conn, now, days=NICHE_DAYS, limit=12):
    """
    Ниши, в которые за окно пришло несколько раундов, — ответ на вопрос
    «какая идея сейчас залетает». Считаются разные компании, а не заметки.

    Ранний раунд (pre-seed, seed, A) весит больше позднего: три seed-раунда
    в одной нише за месяц — это ниша, которая только открывается, а три
    раунда C — ниша, где места уже поделены.

    К каждой нише: кто в неё вкладывает, кривая раундов по неделям за
    полгода, «боль» — посты людей, которые просят такой продукт, — и метка,
    свободна ли ниша в Казахстане и СНГ (gap_step).
    """
    hist = {}
    for r in rounds(conn, now - HISTORY_WEEKS * 7 * 86400):
        if r["niche"]:
            hist.setdefault(r["niche"], []).append(r)
    try:
        gaps = json.loads(db.kv_get(conn, "niche_gaps", "{}") or "{}")
    except ValueError:
        gaps = {}
    out = []
    for niche, allr in hist.items():
        lst = [r for r in allr if r["ts"] >= now - days * 86400]
        if len(lst) < 2:
            continue
        secs, inv = {}, {}
        for r in lst:
            for x in r["sectors"][:1]:
                secs[x] = secs.get(x, 0) + 1
            for i in r.get("investors") or []:
                inv[i] = inv.get(i, 0) + 1
        weekly = [0] * HISTORY_WEEKS
        for r in allr:
            w = week_index(r["ts"], now)
            if 0 <= w < HISTORY_WEEKS:
                weekly[HISTORY_WEEKS - 1 - w] += 1
        mega = [r for r in lst if (r["usd"] or 0) >= MEGA_USD]
        pain = conn.execute("SELECT text, url, likes, ts FROM demand WHERE niche = ? AND ts >= ? "
                            "ORDER BY likes DESC LIMIT 2", (niche, now - 60 * 86400)).fetchall()
        early = sum(1 for r in lst if r["stage"] in EARLY)
        prev = sum(1 for r in allr if now - 2 * days * 86400 <= r["ts"] < now - days * 86400)
        out.append({
            "niche": niche, "n": len(lst), "early": early,
            "usd": round(sum(r["usd"] or 0 for r in lst if (r["usd"] or 0) < MEGA_USD)),
            "mega": [_round_brief(r) for r in mega[:2]],
            "prev": prev,
            "sector": max(secs, key=secs.get) if secs else None,
            "last7": sum(1 for r in lst if r["ts"] >= now - 7 * 86400),
            "investors": [k for k, _v in sorted(inv.items(), key=lambda kv: -kv[1])[:4]],
            "weekly": weekly,
            "pain": [{"text": p["text"][:200], "url": p["url"], "likes": p["likes"], "ts": p["ts"]} for p in pain],
            "gap": gaps.get(niche),
            "companies": [_round_brief(r) for r in sorted(lst, key=lambda r: -(r["usd"] or 0))[:5]],
            "companies_6m": len(allr),
            "late": sum(1 for r in lst if r["stage"] in ("b", "c+", "growth")),
        })
        out[-1]["opp"] = opportunity(out[-1])
    out.sort(key=lambda d: (-d["early"], -d["n"], -d["usd"]))
    return out[:limit]


# ---------------------------------------------------------------------------
# 🎯 Тип возможности и прозрачный скор
# ---------------------------------------------------------------------------
# «Рынок растёт» ≠ «туда стоит идти» (разбор концепции 2026-09-30). Ниша
# получает ОДИН тип и скор, который складывается из видимых частей — у
# каждой своя цифра и источник, чтобы стартапер видел, почему так.
OPP_TYPES = ("window", "forming", "overheated", "local_gap", "watch")


def opportunity(n):
    """{type, score 0-100, parts: [(ключ, баллы, факт)]} по данным ниши."""
    w = n.get("weekly") or []
    recent, before = sum(w[-8:]), sum(w[-16:-8])
    momentum = (recent + 1.0) / (before + 1.0)
    early_share = n["early"] / max(n["n"], 1)
    crowd = n.get("companies_6m", n["n"])
    gap = n.get("gap") or {}
    pain = len(n.get("pain") or [])
    mega = bool(n.get("mega"))
    parts = []
    # Деньги: ранние раунды за окно — главный сигнал, что ниша открывается.
    money = min(30, n["early"] * 6)
    parts.append(("money", money, {"early": n["early"], "n": n["n"], "usd": n["usd"]}))
    # Динамика: последние 8 недель против 8 до них.
    # Порог 3 раунда: «2 против 0» — это не рост ×3, а шум мелких чисел.
    mom = 0 if recent < 3 else 20 if momentum >= 2 else 12 if momentum >= 1.3 else 5 if momentum >= 0.9 else 0
    parts.append(("momentum", mom, {"recent": recent, "before": before}))
    # Спрос людей: посты «сделайте кто-нибудь…» по этой нише.
    dem = min(15, pain * 8)
    parts.append(("demand", dem, {"posts": pain}))
    # Конкуренция: сколько разных компаний подняли деньги за полгода.
    comp = 0 if crowd <= 5 else -5 if crowd <= 12 else -12 if crowd <= 25 else -20
    if mega:
        comp -= 5
    parts.append(("competition", comp, {"companies_6m": crowd, "mega": mega}))
    # Локальный рынок: свободна ли ниша в Казахстане и СНГ.
    loc = ({"free": 20, "partly": 10, "crowded": 0}.get(gap.get("kz"), 0)
           + {"free": 5, "partly": 2, "crowded": 0}.get(gap.get("cis"), 0))
    parts.append(("local", min(loc, 25), {"kz": gap.get("kz"), "cis": gap.get("cis")}))
    score = max(0, min(100, 30 + sum(p[1] for p in parts)))
    # Тип — по правилам, а не по скору: у каждого своя причина.
    if mega or (crowd > 25) or (n.get("late", 0) >= max(3, n["n"] * 0.6)):
        t = "overheated"
    elif gap.get("kz") == "free" and n["n"] >= NICHE_MIN:
        t = "local_gap"
    elif n["early"] >= 2 and crowd <= 12 and momentum >= 1.0 and recent >= 3:
        t = "window"
    elif n["early"] >= 4 or crowd > 12:
        t = "forming"
    else:
        t = "watch"
    return {"type": t, "score": round(score), "parts": [{"k": k, "pts": v, "fact": f} for k, v, f in parts],
            "momentum": round(momentum, 2), "early_share": round(early_share, 2)}


# ---------------------------------------------------------------------------
# 🎓 Отбор YC
# ---------------------------------------------------------------------------
YC_META = "https://yc-oss.github.io/api/meta.json"
YC_BATCH = "https://yc-oss.github.io/api/batches/%s.json"
_SEASON = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def _yc_sectors(c):
    sub = c.get("subindustry") or ""
    base = YC_SUB.get(sub) or YC_INDUSTRY.get(c.get("industry") or "")
    text = " ".join([c.get("one_liner") or "", " ".join(c.get("tags") or [])])
    out = [base] if base else []
    for s in classify(text, limit=2):
        if s not in out:
            out.append(s)
    return out[:2] or ["b2b_saas"]


def refresh_yc(conn, now, n_batches=5, min_size=80, verbose=True):
    """
    Доли секторов в последних полных батчах YC. Раз в сутки — каталог
    обновляется ежедневно, а батч весит сотни килобайт.

    Батч считается полным от 80 компаний: YC заводит будущие батчи заранее,
    и в Winter 2027 на 2026-09-28 лежит ОДНА компания.
    """
    meta = None
    try:
        meta = requests.get(YC_META, headers={"User-Agent": UA}, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        if verbose:
            print("  рынок: каталог YC недоступен (%s)" % str(e)[:80])
        return False
    batches = [(k, v.get("count", 0)) for k, v in (meta.get("batches") or {}).items()
               if v.get("count", 0) >= min_size]

    def key(slug):
        s, _, y = slug.rpartition("-")
        return (int(y) if y.isdigit() else 0, _SEASON.get(s.lower(), 0))

    batches.sort(key=lambda kv: key(kv[0]), reverse=True)
    mix = []
    for slug, _n in batches[:n_batches]:
        try:
            comps = requests.get(YC_BATCH % slug, headers={"User-Agent": UA}, timeout=30).json()
        except (requests.RequestException, ValueError):
            continue
        cnt = {}
        for c in comps:
            for s in _yc_sectors(c):
                cnt[s] = cnt.get(s, 0) + 1
        phys = sum(1 for c in comps if set(_yc_sectors(c)) & set(PHYSICAL))
        mix.append({"slug": slug, "name": (meta["batches"][slug].get("name") or slug),
                    "n": len(comps), "share": {s: round(100.0 * v / len(comps), 1) for s, v in cnt.items()},
                    "physical": round(100.0 * phys / max(len(comps), 1), 1)})
        time.sleep(0.3)
    if mix:
        db.kv_set(conn, "market_yc", json.dumps(mix, ensure_ascii=False))
        db.kv_set(conn, "market_yc_ts", now)
        conn.commit()
    return bool(mix)


def yc_mix(conn):
    try:
        return json.loads(db.kv_get(conn, "market_yc", "[]") or "[]")
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# 🙋 Спрос людей: запросы в X
# ---------------------------------------------------------------------------
def store_demand(conn, posts, now):
    """Посты-запросы из X (sources/x.py, запросы с пометкой demand:)."""
    _ensure(conn)
    n = 0
    for p in posts:
        secs = classify(p.get("text"))
        cur = conn.execute("INSERT OR IGNORE INTO demand (ext_id, ts, text, url, likes, sectors) "
                           "VALUES (?,?,?,?,?,?)",
                           (p["id"], p.get("ts") or now, (p.get("text") or "")[:500], p.get("url"),
                            p.get("likes"), json.dumps(secs)))
        n += cur.rowcount or 0
    conn.commit()
    return n


def demand(conn, now, days=14):
    _ensure(conn)
    rows = conn.execute("SELECT ts, sectors, text, url, likes FROM demand WHERE ts >= ?",
                        (now - 2 * days * 86400,)).fetchall()
    out = {sid: {"cur": 0, "prev": 0, "top": []} for sid in SECTOR_IDS}
    for r in rows:
        for s in json.loads(r["sectors"] or "[]"):
            if s not in out:
                continue
            if r["ts"] >= now - days * 86400:
                out[s]["cur"] += 1
                out[s]["top"].append({"text": (r["text"] or "")[:160], "url": r["url"], "likes": r["likes"]})
            else:
                out[s]["prev"] += 1
    for o in out.values():
        o["top"] = sorted(o["top"], key=lambda d: -(d["likes"] or 0))[:2]
    return out


# ---------------------------------------------------------------------------
# 📰 Аналитика: заголовки обзоров рынка с цифрами
# ---------------------------------------------------------------------------
ANALYSIS_QUERIES = (
    '"venture funding" (report OR data OR quarter OR record OR surge OR falls) when:10d',
    '"startup funding" (report OR data OR "first half" OR quarter) when:10d',
    '"Sector Snapshot" OR "biggest funding rounds" when:14d',
)
# Заголовок должен быть про деньги в стартапах, а не про акционеров банка.
ABOUT_VC = re.compile(r"(venture|startup|funding|\bvc\b|seed|investors?|unicorn)", re.I)


def refresh_headlines(conn, now):
    """Свежие аналитические заголовки — только с цифрой внутри: без неё это мнение."""
    seen, out = set(), []
    for q in ANALYSIS_QUERIES:
        rows, err = fetch_gnews(q)
        if err:
            continue
        for title, url, outlet, ts in rows:
            t = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
            k = t.lower()[:60]
            if k in seen or not re.search(r"\d", t) or parse_deal(t) or not ABOUT_VC.search(t):
                continue
            seen.add(k)
            out.append({"title": t[:200], "url": url, "outlet": outlet, "ts": ts})
        time.sleep(0.8)
    out.sort(key=lambda d: -(d["ts"] or 0))
    db.kv_set(conn, "market_headlines", json.dumps(out[:12], ensure_ascii=False))
    conn.commit()
    return out[:12]


# ---------------------------------------------------------------------------
# Сводка: импульс по секторам
# ---------------------------------------------------------------------------
def _ratio(cur, prev):
    return (cur + 1.0) / (prev + 1.0)


# Веса опор импульса. Все три — ВНЕШНИЕ данные: деньги инвесторов, отбор
# YC, запросы людей в X. Наши собственные находки в расчёт больше не входят
# (решение владельца 2026-09-28): доля сектора среди того, что прошло наши
# же фильтры, — зеркало фильтров, а не рынка.
WEIGHTS = {"money": 0.45, "early": 0.2, "yc": 0.25, "demand": 0.1}


def compute(conn, now):
    """Отчёт в цифрах — словарь без текста. Его же получает бот в срезе."""
    mon = money(conn, now)
    yc = yc_mix(conn)
    dem = demand(conn, now)
    latest = yc[0] if yc else None
    prior = yc[1:4]
    tot_cur = sum(m["cur_n"] for m in mon.values()) or 1
    tot_prev = sum(m["prev_n"] for m in mon.values()) or 1
    early_cur = sum(m["cur_early"] for m in mon.values())
    early_prev = sum(m["prev_early"] for m in mon.values())
    sectors = []
    for sid in SECTOR_IDS:
        m, d = mon[sid], dem[sid]
        yc_now = latest["share"].get(sid, 0.0) if latest else None
        yc_prev = (sum(x["share"].get(sid, 0.0) for x in prior) / len(prior)) if prior else None
        parts = {}
        # Деньги: изменение ДОЛИ сектора в общем потоке сделок, а не числа
        # сделок. Google News полнее индексирует свежие статьи, и в прошлом
        # окне заголовков всегда меньше: по сырому числу «рос» каждый второй
        # сектор (8 из 16 на пробе 2026-09-28). Доля этот перекос снимает и
        # отвечает ровно на вопрос «куда смещаются деньги».
        if m["cur_n"] + m["prev_n"] >= 4:
            share_now = (m["cur_n"] + 1.0) / (tot_cur + 5.0)
            share_was = (m["prev_n"] + 1.0) / (tot_prev + 5.0)
            parts["money"] = max(-1.5, min(1.5, math.log2(share_now / share_was)))
        # Ранние раунды отдельно: сектор, куда пошли seed и pre-seed, —
        # это место, где сейчас открываются ниши для новых команд.
        if m["cur_early"] + m["prev_early"] >= 4:
            e_now = (m["cur_early"] + 1.0) / (early_cur + 5.0)
            e_was = (m["prev_early"] + 1.0) / (early_prev + 5.0)
            parts["early"] = max(-1.5, min(1.5, math.log2(e_now / e_was)))
        if yc_now is not None and yc_prev is not None:
            parts["yc"] = max(-1.5, min(1.5, (yc_now - yc_prev) / 5.0))
        if d["cur"] + d["prev"] >= 5:
            parts["demand"] = max(-1.0, min(1.0, math.log2(_ratio(d["cur"], d["prev"]))))
        wsum = sum(WEIGHTS[k] for k in parts) or 1.0
        momentum = sum(WEIGHTS[k] * v for k, v in parts.items()) / wsum if parts else 0.0
        trend = "up" if momentum >= 0.35 else ("down" if momentum <= -0.3 else "flat")
        sectors.append({
            "id": sid, "momentum": round(momentum, 2), "trend": trend, "signals": len(parts),
            "money": {k: (round(v) if isinstance(v, float) else v) for k, v in m.items()},
            "yc": {"share": yc_now, "prev": round(yc_prev, 1) if yc_prev is not None else None,
                   "batch": latest["name"] if latest else None},
            "demand": d,
        })
    sectors.sort(key=lambda s: -s["momentum"])
    physical = None
    if latest and prior:
        physical = {"share": latest["physical"], "batch": latest["name"],
                    "prev": round(sum(x["physical"] for x in prior) / len(prior), 1),
                    "history": [{"batch": x["name"], "share": x["physical"]} for x in yc[::-1]]}
    phys_money = {"cur_n": sum(mon[s]["cur_n"] for s in PHYSICAL),
                  "prev_n": sum(mon[s]["prev_n"] for s in PHYSICAL),
                  "cur_usd": round(sum(mon[s]["cur_usd"] for s in PHYSICAL)),
                  "prev_usd": round(sum(mon[s]["prev_usd"] for s in PHYSICAL))}
    all_money = {"cur_n": sum(m["cur_n"] for m in mon.values()),
                 "prev_n": sum(m["prev_n"] for m in mon.values()),
                 "cur_early": early_cur, "prev_early": early_prev,
                 "cur_usd": round(sum(m["cur_usd"] for m in mon.values())),
                 "prev_usd": round(sum(m["prev_usd"] for m in mon.values()))}
    try:
        heads = json.loads(db.kv_get(conn, "market_headlines", "[]") or "[]")
    except ValueError:
        heads = []
    # Аналитика изданий (Crunchbase News, TechCrunch) — первой: это обзоры
    # на данных Crunchbase, а не отдельные заголовки из поиска.
    arts = analysis_articles(conn, now)
    seen = {a["title"].lower()[:60] for a in arts}
    heads = arts + [h for h in heads if h["title"].lower()[:60] not in seen]
    # Для графиков мини-приложения: раунды по дням за 28 дней и по стадиям
    # за текущее окно — одна компания = один раунд.
    daily, stages = {}, {}
    for r in rounds(conn, now - 28 * 86400):
        d = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
        cell = daily.setdefault(d, [0, 0.0])
        cell[0] += 1
        cell[1] += r["usd"] or 0
        if r["ts"] >= now - WINDOW_DAYS * 86400:
            st = r["stage"] or "unknown"
            stages[st] = stages.get(st, 0) + 1
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - (27 - i) * 86400)) for i in range(28)]
    all_money["daily"] = [{"d": d, "n": daily.get(d, [0, 0])[0], "usd": round(daily.get(d, [0, 0])[1])} for d in days]
    all_money["stages"] = stages
    # Полгода по неделям: всего раундов, ранних и по секторам — для кривых.
    wk_total, wk_early = [0] * HISTORY_WEEKS, [0] * HISTORY_WEEKS
    wk_sec = {sid: [0] * HISTORY_WEEKS for sid in SECTOR_IDS}
    for r in rounds(conn, now - HISTORY_WEEKS * 7 * 86400):
        w = week_index(r["ts"], now)
        if not 0 <= w < HISTORY_WEEKS:
            continue
        i = HISTORY_WEEKS - 1 - w
        wk_total[i] += 1
        wk_early[i] += 1 if r["stage"] in EARLY else 0
        for sid in r["sectors"][:1]:
            wk_sec[sid][i] += 1
    all_money["weekly"] = {"total": wk_total, "early": wk_early, "sectors": wk_sec,
                           "start": time.strftime("%Y-%m-%d", time.gmtime(now - HISTORY_WEEKS * 7 * 86400))}
    return {"generated": now, "formd": formd_stats(conn, now), "window_days": WINDOW_DAYS, "sectors": sectors,
            "physical": physical, "physical_money": phys_money, "all_money": all_money,
            "yc_batches": [{"name": x["name"], "n": x["n"]} for x in yc],
            "niches": niches(conn, now, limit=30), "niche_days": NICHE_DAYS,
            "niche_names": niche_names(conn), "headlines": heads[:6]}


# ---------------------------------------------------------------------------
# Текст для Telegram
# ---------------------------------------------------------------------------
T = {
    "ru": {"head": "🧭 <b>Куда движется рынок</b>",
           "sub": "по раундам инвесторов, отбору YC и запросам людей · окна по %d дней · «растёт» и «остывает» — по доле сектора в общем потоке раундов, а не по их числу",
           "total": "💰 Всего раундов: %d за %d дн. (ранних — %d), %s · было %d, %s",
           "phys": "🦾 <b>Физический продукт</b> (железо, роботы, оборона, энергетика, логистика)",
           "phys_yc": "доля в батче YC %s: <b>%s%%</b> (в среднем за 3 прошлых — %s%%)",
           "phys_money": "раунды: %d за %d дн., %s (было %d, %s)",
           "niches": "💡 <b>Ниши, куда пошли деньги</b> — %d дн.",
           "niche": "<b>%s</b> — раундов: %d, ранних %d, %s",
           "up": "▲ Растёт", "down": "▼ Остывает", "flat": "Больше всего денег сейчас",
           "deals": "💰 раундов: %s (ранних %d), %s · было %d, %s", "yc": "🎓 YC %s: %s%% (было %s%%)",
           "dem": "🙋 запросы людей в X: %d (было %d)",
           "sec": "🏛 Официально, SEC Form D (США): %d компаний моложе 6 лет подняли %s за %d дн. · было %d, %s",
           "news": "📰 <b>Из аналитики</b>", "story": "🧠 <b>Вывод</b>", "more": "Разбор по сектору и все ниши — кнопками ниже.",
           "empty": "Данных о рынке пока нет: первый сбор раундов и батчей YC идёт в ближайшем прогоне.",
           "b": "млрд", "m": "млн", "none": "сумм нет"},
    "kk": {"head": "🧭 <b>Нарық қайда бет алды</b>",
           "sub": "инвесторлар раундтары, YC іріктеуі және адамдардың сұраулары бойынша · %d күндік терезелер · «өсіп келеді» мен «суып барады» — раундтардың жалпы ағынындағы сала үлесі бойынша",
           "total": "💰 Барлық раундтар: %d (%d күн, ерте — %d), %s · бұрын %d, %s",
           "phys": "🦾 <b>Физикалық өнім</b> (құрылғылар, роботтар, қорғаныс, энергетика, логистика)",
           "phys_yc": "YC %s батчындағы үлесі: <b>%s%%</b> (алдыңғы 3 батчта орта есеппен — %s%%)",
           "phys_money": "раундтар: %d (%d күн), %s (бұрын %d, %s)",
           "niches": "💡 <b>Ақша келген тауашалар</b> — %d күн",
           "niche": "<b>%s</b> — раунд: %d, ерте %d, %s",
           "up": "▲ Өсіп келеді", "down": "▼ Суып барады", "flat": "Қазір ақша ең көп",
           "deals": "💰 %s раунд (ерте %d), %s · бұрын %d, %s", "yc": "🎓 YC %s: %s%% (бұрын %s%%)",
           "dem": "🙋 X-тегі адамдардың сұраулары: %d (бұрын %d)",
           "sec": "🏛 Ресми, SEC Form D (АҚШ): 6 жастан кіші %d компания %s тартты (%d күн) · бұрын %d, %s",
           "news": "📰 <b>Аналитикадан</b>", "story": "🧠 <b>Қорытынды</b>", "more": "Сала бойынша талдау және тауашалар — төмендегі батырмалар.",
           "empty": "Нарық туралы деректер әзірге жоқ: раундтар мен YC батчтарын алғашқы жинау келесі іске қосуда.",
           "b": "млрд", "m": "млн", "none": "сомасы жоқ"},
    "en": {"head": "🧭 <b>Where the market is heading</b>",
           "sub": "by investor rounds, YC selection and what people ask for · %d-day windows · rising and cooling mean the sector's share of all rounds, not the raw count",
           "total": "💰 All rounds: %d in %d days (%d early), %s · was %d, %s",
           "phys": "🦾 <b>Physical products</b> (hardware, robotics, defense, energy, logistics)",
           "phys_yc": "share of YC %s: <b>%s%%</b> (3 previous batches averaged %s%%)",
           "phys_money": "rounds: %d in %d days, %s (was %d, %s)",
           "niches": "💡 <b>Niches the money went into</b> — %d days",
           "niche": "<b>%s</b> — %d rounds, %d early, %s",
           "up": "▲ Rising", "down": "▼ Cooling", "flat": "Where the money is now",
           "deals": "💰 %s rounds (%d early), %s · was %d, %s", "yc": "🎓 YC %s: %s%% (was %s%%)",
           "dem": "🙋 people asking on X: %d (was %d)",
           "sec": "🏛 Official, SEC Form D (US): %d companies under 6 years old raised %s in %d days · was %d, %s",
           "news": "📰 <b>From the analysts</b>", "story": "🧠 <b>Takeaway</b>", "more": "Sector breakdowns and all niches — buttons below.",
           "empty": "No market data yet: the first pass over funding rounds and YC batches runs on the next cycle.",
           "b": "B", "m": "M", "none": "no amounts"},
}
STAGE_NAME = {"pre-seed": "pre-seed", "seed": "seed", "a": "Series A", "b": "Series B",
              "c+": "Series C+", "growth": "growth"}


def usd(v, lang="ru"):
    tx = T.get(lang, T["ru"])
    if not v:
        return tx["none"]
    if v >= 1e9:
        return "$%.1f %s" % (v / 1e9, tx["b"])
    if v < 1e7:
        return "$%.1f %s" % (v / 1e6, tx["m"])
    return "$%.0f %s" % (v / 1e6, tx["m"])


def sector_lines(s, lang="ru"):
    """Строки с цифрами по одному сектору — только по опорам, где есть данные."""
    tx = T.get(lang, T["ru"])
    m, y, d = s["money"], s["yc"], s["demand"]
    out = []
    if m["cur_n"] or m["prev_n"]:
        out.append(tx["deals"] % (("%d+" % m["cur_n"]) if m.get("sat") else str(m["cur_n"]),
                                  m.get("cur_early", 0), usd(m["cur_usd"], lang),
                                  m["prev_n"], usd(m["prev_usd"], lang)))
    if y.get("share") is not None and y.get("prev") is not None:
        out.append(tx["yc"] % (y["batch"], y["share"], y["prev"]))
    if d["cur"] + d["prev"] >= 5:
        out.append(tx["dem"] % (d["cur"], d["prev"]))
    return out


def round_line(r, lang="ru"):
    """«Baselayer — $35 млн, Series A: что делают» — одна строка раунда."""
    e = lambda s: html.escape(s or "", quote=True)  # noqa: E731
    bits = [usd(r.get("usd"), lang)] if r.get("usd") else []
    if r.get("stage"):
        bits.append(STAGE_NAME.get(r["stage"], r["stage"]))
    what = (r.get("what") or {}).get("en" if lang == "en" else "ru") or ""
    name = e(r.get("company") or "—")
    head = '<a href="%s">%s</a>' % (e(r["url"]), name) if r.get("url") else name
    return "%s%s%s" % (head, (" — " + ", ".join(bits)) if bits else "", (": " + e(what)) if what else "")


def niche_block(rep, lang="ru", limit=4, per=2):
    """Ниши, куда пришло несколько раундов, с компаниями и суммами."""
    tx = T.get(lang, T["ru"])
    e = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    items = [n for n in (rep.get("niches") or []) if n["n"] >= NICHE_MIN][:limit]
    if not items:
        return []
    names = rep.get("niche_names") or {}
    lines = [tx["niches"] % rep.get("niche_days", NICHE_DAYS)]
    for n in items:
        emoji = SECTOR[n["sector"]]["emoji"] + " " if n.get("sector") in SECTOR else ""
        lines.append(emoji + tx["niche"] % (e(niche_label(n["niche"], lang, names)), n["n"], n["early"],
                                            usd(n["usd"], lang)))
        lines += ["   • " + round_line(r, lang) for r in n["companies"][:per]]
    return lines


def render(rep, lang="ru", story_text=None):
    tx = T.get(lang, T["ru"])
    e = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    if not rep or not any(s["signals"] for s in rep.get("sectors", [])):
        return tx["empty"]
    lines = [tx["head"], "<i>%s</i>" % (tx["sub"] % rep["window_days"])]
    am = rep.get("all_money") or {}
    if am.get("cur_n"):
        lines += ["", tx["total"] % (am["cur_n"], rep["window_days"], am.get("cur_early", 0),
                                     usd(am.get("cur_usd"), lang), am.get("prev_n", 0), usd(am.get("prev_usd"), lang))]
    fd = rep.get("formd")
    if fd and fd.get("cur_n"):
        lines.append(tx["sec"] % (fd["cur_n"], usd(fd["cur_usd"], lang), fd.get("days", WINDOW_DAYS),
                                  fd.get("prev_n", 0), usd(fd.get("prev_usd"), lang)))
    nb = niche_block(rep, lang)
    if nb:
        lines += [""] + nb
    ph, pm = rep.get("physical"), rep.get("physical_money") or {}
    if ph:
        lines += ["", tx["phys"], "• " + tx["phys_yc"] % (ph["batch"], ph["share"], ph["prev"])]
        if pm.get("cur_n") or pm.get("prev_n"):
            lines.append("• " + tx["phys_money"] % (pm["cur_n"], rep["window_days"], usd(pm["cur_usd"], lang),
                                                   pm["prev_n"], usd(pm["prev_usd"], lang)))
    up = [s for s in rep["sectors"] if s["trend"] == "up"][:4]
    down = [s for s in rep["sectors"] if s["trend"] == "down"][-3:]
    for title, group in ((tx["up"], up), (tx["down"], down)):
        if not group:
            continue
        lines += ["", "<b>%s</b>" % title]
        for s in group:
            lines.append("%s <b>%s</b>" % (SECTOR[s["id"]]["emoji"], e(sector_name(s["id"], lang))))
            lines += ["   " + x for x in sector_lines(s, lang)[:3]]
    rich = sorted(rep["sectors"], key=lambda s: -s["money"]["cur_usd"])[:3]
    if rich and rich[0]["money"]["cur_usd"]:
        lines += ["", "<b>%s</b>" % tx["flat"]]
        lines.append(" · ".join("%s %s %s" % (SECTOR[s["id"]]["emoji"], e(sector_name(s["id"], lang)),
                                              usd(s["money"]["cur_usd"], lang)) for s in rich))
    heads = rep.get("headlines") or []
    if heads:
        lines += ["", tx["news"]]
        for h in heads[:3]:
            lines.append('• <a href="%s">%s</a>' % (html.escape(h["url"] or ""), e(h["title"][:140])))
    if story_text:
        lines += ["", tx["story"], e(story_text)]
    lines += ["", "<i>%s</i>" % tx["more"]]
    out = "\n".join(lines)
    # Telegram режет сообщение на 4096 символах, а обрезка посреди тега
    # ломает разметку целиком — убираем строки из середины, а не символы.
    while len(out) > 3900 and len(lines) > 6:
        lines.pop(len(lines) // 2)
        out = "\n".join(lines)
    return out


def story_input(rep):
    """Цифры для модели — только посчитанное, без свободы придумывать."""
    am = rep.get("all_money") or {}
    lines = ["Windows: last %d days vs the %d days before. Sources: venture rounds found in news (one per "
             "company), YC batch composition, posts on X where people ask for a product."
             % (rep["window_days"], rep["window_days"])]
    if am.get("cur_n"):
        lines.append("All rounds: %d now (%d early-stage: pre-seed/seed/A), $%.0fM vs %d, $%.0fM before."
                     % (am["cur_n"], am.get("cur_early", 0), (am.get("cur_usd") or 0) / 1e6,
                        am.get("prev_n", 0), (am.get("prev_usd") or 0) / 1e6))
    ph = rep.get("physical")
    if ph:
        lines.append("Physical-world startups (hardware, robotics, defense, energy, logistics): %s%% of YC %s "
                     "vs %s%% average of the 3 previous batches." % (ph["share"], ph["batch"], ph["prev"]))
    for s in rep["sectors"]:
        if not s["signals"]:
            continue
        m, y = s["money"], s["yc"]
        lines.append("- %s: trend %s (momentum %.2f); rounds %d (%d early) vs %d (%d early), $%.0fM vs $%.0fM; "
                     "YC share %s%% vs %s%%"
                     % (SECTOR[s["id"]]["names"]["en"], s["trend"], s["momentum"], m["cur_n"],
                        m.get("cur_early", 0), m["prev_n"], m.get("prev_early", 0),
                        m["cur_usd"] / 1e6, m["prev_usd"] / 1e6, y.get("share"), y.get("prev")))
    for n in (rep.get("niches") or [])[:8]:
        comps = "; ".join("%s %s%s - %s" % (r["company"], ("$%.1fM " % (r["usd"] / 1e6)) if r.get("usd") else "",
                                           STAGE_NAME.get(r.get("stage"), ""), (r.get("what") or {}).get("en", ""))
                          for r in n["companies"][:4])
        lines.append("Niche \"%s\" (%s): %d rounds in %d days, %d early, $%.0fM. Companies: %s"
                     % (n["niche"], SECTOR[n["sector"]]["names"]["en"] if n.get("sector") in SECTOR else "?",
                        n["n"], rep.get("niche_days", NICHE_DAYS), n["early"], n["usd"] / 1e6, comps))
    fd = rep.get("formd")
    if fd and fd.get("cur_n"):
        lines.append("SEC Form D (official US filings, companies under 6 years): %d raised $%.0fM now vs %d, "
                     "$%.0fM before; by industry: %s" % (fd["cur_n"], fd["cur_usd"] / 1e6, fd.get("prev_n", 0),
                                                         (fd.get("prev_usd") or 0) / 1e6,
                                                         ", ".join("%s %d" % (i[0], i[1]) for i in fd["industries"])))
    for h in (rep.get("headlines") or [])[:6]:
        lines.append("Analyst article: %s (%s). %s" % (h["title"], h.get("outlet") or "", (h.get("excerpt") or "")[:500]))
    return "\n".join(lines)


def story(conn, rep, now, lang="ru", max_age_h=12):
    """Вывод от модели — не чаще раза в 12 часов на язык; без ключа — None."""
    import ai
    cached = db.kv_get(conn, "market_story_" + lang)
    ts = int(db.kv_get(conn, "market_story_ts_" + lang, 0) or 0)
    if cached and now - ts < max_age_h * 3600:
        return cached
    if not any(s["signals"] for s in rep["sectors"]):
        return None
    text, _err = ai.write_market_story(story_input(rep), lang=lang)
    if text:
        db.kv_set(conn, "market_story_" + lang, text)
        db.kv_set(conn, "market_story_ts_" + lang, now)
        conn.commit()
        return text
    return cached


def render_all(conn, rep, now):
    import ai
    return {lang: render(rep, lang, story(conn, rep, now, lang)) for lang in ai.LANGS}


# ---------------------------------------------------------------------------
# Расписание и рассылки
# ---------------------------------------------------------------------------
# Раунды — раз в 6 часов: рассылка «раунды за сутки» должна видеть
# вчерашние сделки, а 40 запросов к RSS раз в 6 часов — вежливо.
REFRESH_H = 6


def maybe_refresh(conn, now, force=False, verbose=True):
    """
    Обновить данные рынка, если пора. Возвращает свежий отчёт или None.

    Раунды и заголовки — раз в 12 часов, каталог YC — раз в сутки: чаще
    данные там не меняются, а каждый прогон идёт раз в 10 минут.
    """
    last = int(db.kv_get(conn, "market_ts", 0) or 0)
    if not force and now - last < REFRESH_H * 3600:
        return None
    if force or now - int(db.kv_get(conn, "market_yc_ts", 0) or 0) >= 86400:
        refresh_yc(conn, now, verbose=verbose)
    refresh_deals(conn, now, verbose=verbose)
    refresh_headlines(conn, now)
    rep = compute(conn, now)
    prev = db.kv_get(conn, "market_report")
    db.kv_set(conn, "market_prev_report", prev or "")
    db.kv_set(conn, "market_report", json.dumps(rep, ensure_ascii=False))
    db.kv_set(conn, "market_ts", now)
    conn.commit()
    return rep


def update_report(conn, now):
    """
    Пересчитать отчёт по уже собранным данным, без сети. Нужен после
    разбора очереди раундов ИИ: ниши и стадии появляются постепенно, между
    пересчётами по расписанию. Прошлый отчёт для «сдвигов» не трогаем.
    """
    rep = compute(conn, now)
    db.kv_set(conn, "market_report", json.dumps(rep, ensure_ascii=False))
    conn.commit()
    return rep


def last_report(conn):
    try:
        return json.loads(db.kv_get(conn, "market_report", "") or "null")
    except ValueError:
        return None


def shift_alerts(conn, rep):
    """
    Секторы, которые в этом пересчёте впервые перешли в «растёт».

    Сравнение с прошлым пересчётом, а не с порогом: сектор, который растёт
    месяц, не должен будить подписчика каждые 12 часов.
    """
    try:
        prev = json.loads(db.kv_get(conn, "market_prev_report", "") or "null")
    except ValueError:
        prev = None
    if not prev:
        return []
    was = {s["id"]: s for s in prev.get("sectors", [])}
    out = []
    for s in rep["sectors"]:
        p = was.get(s["id"])
        if s["trend"] == "up" and s["signals"] >= 2 and p and p["trend"] != "up" and s["momentum"] - p["momentum"] >= 0.2:
            out.append(s)
    return out


A = {
    "ru": {"alert": "🚀 <b>Сдвиг рынка: %s</b>\nСектор перешёл в рост.", "funding": "💰 <b>Раунды за сутки</b>",
           "niche": "💡 <b>Ниша набирает раунды: %s</b>\n%d раунда за %d дн., из них ранних %d, всего %s.",
           "niche_tail": "Несколько ранних раундов в одной нише за месяц — инвесторы начали на неё ставить."},
    "kk": {"alert": "🚀 <b>Нарықтағы өзгеріс: %s</b>\nСала өсуге көшті.", "funding": "💰 <b>Тәуліктегі раундтар</b>",
           "niche": "💡 <b>Тауаша раундтар жинап жатыр: %s</b>\n%d күнде %d раунд, оның ішінде ерте %d, барлығы %s.",
           "niche_tail": "Бір айда бір тауашада бірнеше ерте раунд — инвесторлар оған бәс тіге бастады."},
    "en": {"alert": "🚀 <b>Market shift: %s</b>\nThe sector has turned upward.", "funding": "💰 <b>Rounds in the last 24h</b>",
           "niche": "💡 <b>A niche is attracting rounds: %s</b>\n%d rounds in %d days, %d of them early, %s in total.",
           "niche_tail": "Several early rounds in one niche within a month — investors have started betting on it."},
}


def alert_payloads(rep, sectors):
    import ai
    out = []
    for s in sectors:
        texts = {}
        for lang in ai.LANGS:
            body = [A[lang]["alert"] % html.escape(sector_name(s["id"], lang))]
            body += ["• " + x for x in sector_lines(s, lang)]
            for dl in s["money"]["top"][:3]:
                body.append("   " + round_line(dl, lang))
            texts[lang] = "\n".join(body)
        out.append({"kind": "alert", "sectors": [s["id"]], "texts": texts, "text": texts["ru"]})
    return out


NICHE_ALERT_EARLY = 2      # ранних раундов, чтобы ниша считалась «открывающейся»


def niche_alerts(conn, rep, now, max_per_run=2):
    """
    Ниши, которые впервые набрали NICHE_MIN раундов, из них не меньше двух
    ранних. Одна ниша — одно уведомление в месяц.

    Первый расчёт только запоминает текущие ниши и ничего не шлёт: иначе
    включение функции разослало бы разом всё, что накопилось.
    """
    import ai
    raw = db.kv_get(conn, "niche_alerted")
    try:
        seen = json.loads(raw) if raw else None
    except ValueError:
        seen = {}
    fresh = [n for n in (rep.get("niches") or [])
             if n["n"] >= NICHE_MIN and n["early"] >= NICHE_ALERT_EARLY
             and now - int((seen or {}).get(n["niche"], 0)) > 30 * 86400]
    if seen is None:
        db.kv_set(conn, "niche_alerted", json.dumps({n["niche"]: now for n in fresh}))
        conn.commit()
        return []
    out, names = [], rep.get("niche_names") or {}
    for n in fresh[:max_per_run]:
        texts = {}
        for lang in ai.LANGS:
            body = [A[lang]["niche"] % (html.escape(niche_label(n["niche"], lang, names)), n["n"],
                                        rep.get("niche_days", NICHE_DAYS), n["early"], usd(n["usd"], lang))]
            body += ["• " + round_line(r, lang) for r in n["companies"][:4]]
            body += ["", "<i>%s</i>" % A[lang]["niche_tail"]]
            texts[lang] = "\n".join(body)
        out.append({"kind": "alert", "sectors": [n["sector"]] if n.get("sector") else [],
                    "texts": texts, "text": texts["ru"]})
        seen[n["niche"]] = now
    db.kv_set(conn, "niche_alerted", json.dumps(seen))
    conn.commit()
    return out


def funding_digest(conn, now, min_usd=1e6):
    """
    Новые раунды с прошлой рассылки, по секторам: [{kind, sectors, texts}].
    Worker раздаёт каждому только его секторы.

    По одному разу на компанию: сделку, о которой через три дня написало
    ещё одно издание, второй раз не присылаем.
    """
    import ai
    _ensure(conn)
    sent_before = {company_norm(r["company"]) for r in conn.execute(
        "SELECT company FROM deals WHERE sent = 1 AND ts >= ?", (now - 60 * 86400,)).fetchall()}
    fresh = [r for r in rounds(conn, now - 2 * 86400)
             if not r["sent"] and (r["usd"] or 0) >= min_usd
             and company_norm(r["company"]) not in sent_before]
    if not fresh:
        return [], []
    fresh.sort(key=lambda r: -(r["usd"] or 0))
    by = {}
    for r in fresh:
        for s in r["sectors"][:1] or ["b2b_saas"]:
            by.setdefault(s, []).append(r)
    out = []
    for sid, deals in by.items():
        texts = {}
        for lang in ai.LANGS:
            lines = ["%s %s — %s" % (A[lang]["funding"], SECTOR[sid]["emoji"],
                                     html.escape(sector_name(sid, lang)))]
            lines += ["• " + round_line(_round_brief(r), lang) for r in deals[:6]]
            texts[lang] = "\n".join(lines)
        out.append({"kind": "funding", "sectors": [sid], "texts": texts, "text": texts["ru"]})
    return out, [k for r in fresh for k in r["keys"]]


def mark_deals_sent(conn, keys):
    conn.executemany("UPDATE deals SET sent = 1 WHERE key = ?", [(k,) for k in keys])
    conn.commit()


# ---------------------------------------------------------------------------
# Связка с находками: у этого продукта есть раунд?
# ---------------------------------------------------------------------------
# Хостинги и соцсети: домен такого адреса — не имя компании.
GENERIC_HOSTS = {"github", "gitlab", "x", "twitter", "youtube", "youtu", "medium", "substack", "notion",
                 "vercel", "netlify", "linkedin", "producthunt", "apple", "google", "play", "tiktok",
                 "instagram", "facebook", "reddit", "discord", "t", "bit", "huggingface", "kickstarter",
                 "ycombinator", "news", "docs", "app", "apps", "chrome", "microsoft", "amazon"}
_IDX = {}


def funding_index(conn, now, days=120):
    """{нормализованное имя компании: раунд} — кэш на прогон. Новости главнее формы D."""
    key = now // 600
    if _IDX.get("key") != key:
        idx = dict(formd_index(conn, now, days))
        for r in rounds(conn, now - days * 86400):
            k = company_norm(r["company"])
            if len(k) >= 4 and (k not in idx or idx[k].get("source") == "SEC Form D"
                                or (r["usd"] or 0) > (idx[k]["usd"] or 0)):
                idx[k] = r
        _IDX.clear()
        _IDX.update(key=key, idx=idx)
    return _IDX["idx"]


def _names_of(item):
    """
    [(имя, минимальная длина)]: точное название компании (каталог YC,
    Launch HN) сверяется от 4 букв, метка домена — от 5: у доменов чаще
    случайные совпадения с чужими компаниями.
    """
    names = []
    title = item["title"] or ""
    if item["source"] in ("yc", "ph"):
        names.append((title.split(" — ")[0].split(":")[0], 4))
    m = re.match(r"^(?:Launch|Show) HN:\s*([^(–—\-:]{2,40})", title)
    if m:
        names.append((m.group(1), 4))
    dom = (item["domain"] or "").lower()
    parts = [p for p in dom.split(".") if p and p != "www"]
    if len(parts) >= 2:
        label = parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com") else parts[-2]
        if label not in GENERIC_HOSTS:
            names.append((label, 5))
    return names


def deal_for_item(conn, item, now):
    """Раунд компании за последние 4 месяца или None. Совпадение — по имени."""
    try:
        idx = funding_index(conn, now)
    except Exception:
        return None
    for n, min_len in _names_of(item):
        k = company_norm(n)
        if len(k) >= min_len and k in idx:
            return idx[k]
    return None


# ---------------------------------------------------------------------------
# Раунды из X: основатели пишут о раунде раньше, чем выходит заметка
# ---------------------------------------------------------------------------
def store_x_raises(conn, posts, now):
    """Посты вида «we raised $5M seed» -> в поток сделок (src = x). Сколько записано."""
    _ensure(conn)
    n = 0
    for p in posts:
        text = re.sub(r"https?://\S+", "", p.get("text") or "").strip()
        amount, stage = _amount(text), parse_stage(text)
        if not amount and not stage:
            continue
        who = p.get("screen_name") or ""
        label = None
        dom = (p.get("domain") or "").split(".")
        if len(dom) >= 2 and dom[-2] not in GENERIC_HOSTS:
            label = dom[-2]
        n += add_deal(conn, text[:220], p.get("url"), "X @%s" % who, p.get("ts") or now, now,
                      src="x", company=label or who, amount=amount)
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 📰 Статьи изданий: аналитика и «топ раундов недели»
# ---------------------------------------------------------------------------
def _ensure_intel(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS articles ("
        " url TEXT PRIMARY KEY, ts INTEGER, outlet TEXT, title TEXT, text TEXT, cats TEXT, is_deal INTEGER)")
    # Факты о выручке: предложения статей и посты основателей с MRR/ARR.
    # Выручка — единственное, что отличает «подняли деньги» от «продают».
    conn.execute(
        "CREATE TABLE IF NOT EXISTS facts ("
        " key TEXT PRIMARY KEY, ts INTEGER, kind TEXT, company TEXT, value_usd REAL, text TEXT,"
        " url TEXT, source TEXT, likes INTEGER)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS formd ("
        " acc TEXT PRIMARY KEY, cik TEXT, company TEXT, filed INTEGER, industry TEXT, sector TEXT,"
        " sold REAL, state TEXT, year_inc INTEGER, url TEXT, done INTEGER DEFAULT 0, keep INTEGER DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS formd_filed ON formd (filed)")


# «1. Island , $400M, cybersecurity: Island, a developer of …» — строка
# еженедельного топа Crunchbase News (формат стабилен с 2023 года).
TOP_ITEM = re.compile(r"(?:^| )(\d{1,2})\. ([A-Z0-9][^,:]{1,60}?) ?, \$([\d.]+) ?([MB]), ([^:]{2,60}): "
                      r"(.{20,700}?)(?= \d{1,2}\. [A-Z0-9][^,:]{1,60}? ?, \$|$)")
REVENUE = re.compile(r"(?:[^.]|\.(?=\d)){0,200}\$\s?[\d.,]+\s?(?:k|m|b|million|billion)?\s?(?:in )?(?:annual(?:ized)? recurring "
                     r"revenue|ARR|MRR|monthly recurring revenue|revenue run[- ]rate|annual revenue)(?:[^.]|\.(?=\d)){0,120}\.",
                     re.I)
_MONEY = re.compile(r"\$\s?([\d.,]+)\s?(k|m|b|million|billion)?", re.I)
# Сумма, стоящая прямо перед словами о выручке: в одном предложении часто
# есть и оценка компании («valuing it at $3.2 billion»), и выручка.
_REV_AMOUNT = re.compile(r"\$\s?([\d.,]+)\s?(k|m|b|million|billion)?\s?(?:in )?(?:annual(?:ized)? recurring revenue|"
                         r"ARR|MRR|monthly recurring revenue|revenue run[- ]rate|annual revenue)", re.I)


def _money(t):
    m = _MONEY.search(t or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}.get((m.group(2) or "").lower(), 1)
    return v * mult


def store_article(conn, outlet, title, url, ts, text, cats, is_deal, now):
    """Статья: в архив, «топ раундов» — в поток сделок, фразы о выручке — в факты."""
    _ensure_intel(conn)
    if not url:
        return
    conn.execute("INSERT OR IGNORE INTO articles (url, ts, outlet, title, text, cats, is_deal) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (url, ts, outlet, _clean_title(title)[:220], (text or "")[:4000],
                  json.dumps(cats[:6], ensure_ascii=False), 1 if is_deal else 0))
    if re.search(r"biggest funding rounds", title, re.I):
        for m in TOP_ITEM.finditer(text or ""):
            company, amount = m.group(2).strip(), float(m.group(3)) * (1e9 if m.group(4) == "B" else 1e6)
            desc = m.group(6).strip()
            add_deal(conn, "%s raises $%s%s (%s): %s" % (company, m.group(3), m.group(4), m.group(5), desc[:160]),
                     url, outlet, ts, now, src="crunchbase", company=company, amount=amount)
    for sent in REVENUE.findall(text or "")[:6]:
        sent = sent.strip()
        key = "a|" + hashlib.sha1(sent.encode("utf-8")).hexdigest()[:16]
        conn.execute("INSERT OR IGNORE INTO facts (key, ts, kind, company, value_usd, text, url, source) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (key, ts, "revenue", None, _money(_REV_AMOUNT.search(sent).group(0)), sent[:300], url, outlet))


def analysis_articles(conn, now, days=14, limit=8):
    """Аналитика изданий (не заметки о сделке) — свежие первыми."""
    _ensure_intel(conn)
    rows = conn.execute("SELECT * FROM articles WHERE is_deal = 0 AND ts >= ? ORDER BY ts DESC",
                        (now - days * 86400,)).fetchall()
    out = []
    for r in rows:
        if not (ANALYSIS.search(r["title"]) or re.search(r"\d", r["title"])
                or re.search(r"(funding|venture|startups?|rounds?|investors?)", r["title"], re.I)):
            continue
        out.append({"title": r["title"], "url": r["url"], "outlet": r["outlet"], "ts": r["ts"],
                    "excerpt": (r["text"] or "")[:600]})
    return out[:limit]


# ---------------------------------------------------------------------------
# 💵 Выручка из X: основатели публикуют MRR/ARR (#buildinpublic)
# ---------------------------------------------------------------------------
MRR_POST = re.compile(r"\$\s?([\d.,]+)\s?(k|m)?\s?(?:in )?(mrr|arr)\b|\b(mrr|arr)\b[^$\n]{0,20}\$\s?([\d.,]+)\s?(k|m)?",
                      re.I)


def store_x_traction(conn, posts, now):
    """Посты с цифрой выручки -> факты (kind = mrr). Сколько записано."""
    _ensure_intel(conn)
    n = 0
    for p in posts:
        text = p.get("text") or ""
        m = MRR_POST.search(text)
        if not m:
            continue
        num, suf, kind = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(6), m.group(4))
        try:
            v = float(num.replace(",", "")) * {"k": 1e3, "m": 1e6}.get((suf or "").lower(), 1)
        except ValueError:
            continue
        cur = conn.execute("INSERT OR IGNORE INTO facts (key, ts, kind, company, value_usd, text, url, source, likes) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           ("x|" + p["id"], p.get("ts") or now, kind.lower(), p.get("screen_name"), v,
                            re.sub(r"https?://\S+", "", text)[:300], p.get("url"), "X @%s" % (p.get("screen_name") or ""),
                            p.get("likes")))
        n += cur.rowcount or 0
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 🏛 SEC Form D: официальные заявки о раундах в США
# ---------------------------------------------------------------------------
def refresh_formd(conn, now, days=5, verbose=True):
    """Дневные индексы EDGAR за прошлые дни -> очередь заявок. Каждый день — один раз."""
    from sources import sec
    _ensure_intel(conn)
    added, errs = 0, []
    for back in range(1, days + 1):
        day = now - back * 86400
        if time.gmtime(day).tm_wday >= 5:
            continue                                   # по выходным EDGAR не публикует индексы
        k = "formd_day_" + time.strftime("%Y%m%d", time.gmtime(day))
        if db.kv_get(conn, k):
            continue
        rows, err = sec.daily_filings(day)
        if err:
            errs.append("%s: %s" % (k[-8:], err))
            if err.startswith("HTTP 403") or err.startswith("HTTP 429"):
                break
            continue
        filed = day - day % 86400                        # полночь UTC дня подачи
        for cik, acc, name in rows:
            cur = conn.execute("INSERT OR IGNORE INTO formd (acc, cik, company, filed, url) VALUES (?,?,?,?,?)",
                               (acc, cik, name[:100], filed, sec.filing_url(cik, acc)))
            added += cur.rowcount or 0
        db.kv_set(conn, k, len(rows))
        time.sleep(0.3)
    conn.commit()
    if verbose and (added or errs):
        print("  SEC Form D: в очередь +%d%s" % (added, (" — " + "; ".join(errs[:2])) if errs else ""))
    return added, errs


def process_formd(conn, now, verbose=True):
    """Разобрать очередь заявок: цифры из primary_doc.xml. Сколько стартапов найдено."""
    from sources import sec
    _ensure_intel(conn)
    rows = conn.execute("SELECT acc, cik FROM formd WHERE done = 0 ORDER BY filed DESC LIMIT ?",
                        (sec.MAX_DOCS,)).fetchall()
    kept, err = 0, None
    for r in rows:
        doc, err = sec.fetch_doc(r["cik"], r["acc"], now)
        if err:
            if err.startswith("HTTP 404"):
                conn.execute("UPDATE formd SET done = 1 WHERE acc = ?", (r["acc"],))
                err = None
                continue
            break
        if doc:
            conn.execute("UPDATE formd SET done = 1, keep = 1, company = ?, industry = ?, sector = ?, sold = ?, "
                         "state = ?, year_inc = ? WHERE acc = ?",
                         (doc["company"] or None, doc["industry"], doc["sector"], doc["sold"], doc["state"],
                          doc["year_inc"], r["acc"]))
            kept += 1
        else:
            conn.execute("UPDATE formd SET done = 1 WHERE acc = ?", (r["acc"],))
        time.sleep(0.15)                               # SEC: не больше 10 запросов в секунду
    conn.commit()
    if verbose and (rows or err):
        print("  SEC Form D: разобрано %d, стартапов %d%s" % (len(rows), kept, (" — " + err) if err else ""))
    return kept


def formd_stats(conn, now, days=WINDOW_DAYS):
    """Заявки молодых компаний за два окна: число, сумма, по отраслям."""
    _ensure_intel(conn)
    rows = conn.execute("SELECT filed, industry, sold, company, url, state FROM formd "
                        "WHERE keep = 1 AND filed >= ?", (now - 2 * days * 86400,)).fetchall()
    if not rows:
        return None
    out = {"cur_n": 0, "prev_n": 0, "cur_usd": 0.0, "prev_usd": 0.0, "industries": {}, "top": []}
    for r in rows:
        side = "cur" if r["filed"] >= now - days * 86400 else "prev"
        out[side + "_n"] += 1
        out[side + "_usd"] += r["sold"] or 0
        if side == "cur":
            ind = out["industries"].setdefault(r["industry"] or "?", [0, 0])
            ind[0] += 1
            ind[1] += r["sold"] or 0
            out["top"].append({"company": r["company"], "usd": r["sold"], "industry": r["industry"],
                               "url": r["url"], "ts": r["filed"], "state": r["state"]})
    out["top"] = sorted(out["top"], key=lambda d: -(d["usd"] or 0))[:8]
    out["industries"] = sorted(([k, v[0], round(v[1])] for k, v in out["industries"].items()),
                               key=lambda x: -x[1])[:8]
    out["cur_usd"], out["prev_usd"] = round(out["cur_usd"]), round(out["prev_usd"])
    out["days"] = days
    return out


def formd_index(conn, now, days=120):
    """{имя компании: заявка} — для сверки находок с официальными раундами."""
    _ensure_intel(conn)
    idx = {}
    for r in conn.execute("SELECT company, sold, filed, url FROM formd WHERE keep = 1 AND filed >= ?",
                          (now - days * 86400,)).fetchall():
        k = company_norm(r["company"])
        if len(k) >= 4:
            idx[k] = {"company": r["company"], "usd": r["sold"], "stage": None, "url": r["url"],
                      "ts": r["filed"], "source": "SEC Form D"}
    return idx


# ---------------------------------------------------------------------------
# 💬 Факты для чата: только свежее и только с источником
# ---------------------------------------------------------------------------
def dataset_rows(conn, now):
    """
    Раунды за полгода — по строке на компанию — для таблицы rounds в боте.
    По ней ИИ-собеседник ищет инструментами (search_rounds): модели нужен
    не срез последних дней, а весь датасет, иначе про нишу вне топа ей
    нечего сказать (вопрос владельца 2026-09-30).
    """
    out = []
    for r in rounds(conn, now - HISTORY_WEEKS * 7 * 86400):
        k = company_norm(r["company"])
        if not k:
            continue
        what = r.get("what") or {}
        out.append({"key": k, "ts": r["ts"], "company": (r["company"] or "")[:80], "usd": r["usd"],
                    "stage": r["stage"], "niche": r["niche"], "sector": (r["sectors"] or [None])[0],
                    "country": r.get("country") or "", "investors": ", ".join(r.get("investors") or [])[:120],
                    "what_ru": (what.get("ru") or "")[:140], "what_en": (what.get("en") or "")[:140],
                    "url": (r["url"] or "")[:400], "outlets": r["outlets"]})
    return out


def chat_facts(conn, now):
    """
    Компактный набор фактов для ИИ-собеседника в боте: раунды последних
    трёх суток (по одному на компанию), ниши, выручка, аналитика, форма D.
    Всё с датой и ссылкой: собеседник обязан ссылаться, а не вспоминать.
    """
    _ensure_intel(conn)
    rs = sorted(rounds(conn, now - 3 * 86400), key=lambda r: -r["ts"])[:70]
    rev = conn.execute("SELECT ts, kind, company, value_usd, text, url, source, likes FROM facts "
                       "WHERE ts >= ? ORDER BY ts DESC LIMIT 40", (now - 7 * 86400,)).fetchall()
    return {
        "rounds": [{"company": r["company"], "usd": r["usd"], "stage": r["stage"], "niche": r["niche"],
                    "sector": (r["sectors"] or [None])[0], "what": r["what"], "url": r["url"], "ts": r["ts"],
                    "country": r["country"], "outlets": r["outlets"]} for r in rs],
        "revenue": [{"ts": r["ts"], "kind": r["kind"], "who": r["company"], "usd": r["value_usd"],
                     "text": r["text"], "url": r["url"], "source": r["source"]} for r in rev],
        "articles": analysis_articles(conn, now, days=10, limit=6),
        # «Боль» за 60 дней — для инструмента search_pain в чате.
        "pain": [{"ts": p["ts"], "likes": p["likes"], "niche": p["niche"] or "", "text": (p["text"] or "")[:220],
                  "url": p["url"]} for p in conn.execute(
            "SELECT ts, likes, niche, text, url FROM demand WHERE ts >= ? AND likes >= 10 "
            "ORDER BY likes DESC LIMIT 300", (now - 60 * 86400,)).fetchall()],
    }


# ---------------------------------------------------------------------------
# 📜 История за полгода: те же запросы к Google News, по неделям назад
# ---------------------------------------------------------------------------
BACKFILL_QUERIES_PER_RUN = 60


def backfill_deals(conn, now, verbose=True):
    """
    Догрузить раунды за полгода — по неделе на запрос, как и живой сбор.

    Google News отдаёт старые заголовки по after:/before: (проверено
    2026-09-29: 20–30 заголовков о раундах на сектор в неделю даже в марте).
    Запросов ~500, поэтому по BACKFILL_QUERIES_PER_RUN за прогон: вся история
    набирается за полтора часа прогонов, а каждый прогон дольше на минуту.
    Разбор заголовков ИИ идёт общей очередью (enrich_deals), свежие первыми.
    """
    _ensure(conn)
    done = json.loads(db.kv_get(conn, "backfill_done", "{}") or "{}")
    base = now - now % 86400
    todo = []
    for k in range(4, HISTORY_WEEKS):                 # последние 4 недели собирает refresh_deals
        a, b = base - (k + 1) * 7 * 86400, base - k * 7 * 86400
        for sid in SECTOR_IDS + ["_general"]:
            key = "%s|%s" % (_day(a), sid)
            if key not in done:
                todo.append((key, sid, a, b))
    if not todo:
        return 0
    added, errs = 0, []
    for key, sid, a, b in todo[:BACKFILL_QUERIES_PER_RUN]:
        if sid == "_general":
            q = '(raises OR raised OR secures) ("pre-seed" OR "seed round" OR "Series A" OR "Series B" OR ' \
                '"Series C") startup after:%s before:%s' % (_day(a), _day(b))
        else:
            q = '%s (startup OR company) (raises OR funding OR "seed round" OR "series a" OR "series b") ' \
                'after:%s before:%s' % (SECTOR[sid]["q"], _day(a), _day(b))
        rows, err = fetch_gnews(q)
        if err:
            errs.append(err)
            if err.startswith("HTTP 429") or err.startswith("HTTP 503"):
                break
            continue
        for title, url, outlet, ts in rows:
            added += 1 if add_deal(conn, title, url, outlet, ts, now, sid=None if sid == "_general" else sid) else 0
        done[key] = len(rows)
        time.sleep(0.8)
    db.kv_set(conn, "backfill_done", json.dumps(done))
    conn.commit()
    if verbose:
        print("  история: запросов %d из %d, заголовков о раундах %d%s"
              % (len(done), len(done) + len(todo) - min(len(todo), BACKFILL_QUERIES_PER_RUN), added,
                 (" — " + errs[0]) if errs else ""))
    return added


# ---------------------------------------------------------------------------
# 🙋 «Боль» -> ниша
# ---------------------------------------------------------------------------
def tag_demand_step(conn, now, verbose=True):
    """Посты «сделайте кто-нибудь…» с откликом — привязать к нишам. Одна пачка за прогон."""
    import ai
    _ensure(conn)
    rows = conn.execute("SELECT ext_id, text FROM demand WHERE niche IS NULL AND likes >= 20 AND ts >= ? "
                        "ORDER BY likes DESC LIMIT 30", (now - 60 * 86400,)).fetchall()
    known = known_niches(conn, now, days=HISTORY_WEEKS * 7)
    if not rows or not known:
        return 0
    got, err = ai.tag_demand([{"id": r["ext_id"], "text": (r["text"] or "")[:280]} for r in rows], known)
    if err and not got:
        return 0
    found = dict(got)
    for r in rows:
        conn.execute("UPDATE demand SET niche = ? WHERE ext_id = ?", (found.get(r["ext_id"], ""), r["ext_id"]))
    conn.commit()
    if verbose and found:
        print("  «боль» привязана к нишам: %d из %d" % (len(found), len(rows)))
    return len(found)


# ---------------------------------------------------------------------------
# 🇰🇿 Свободна ли ниша в Казахстане и СНГ
# ---------------------------------------------------------------------------
GAP_PER_RUN = 1
GAP_MAX_AGE = 14 * 86400


def gap_step(conn, now, rep, verbose=True):
    """
    Проверить на аналоги в Казахстане и СНГ верхние ниши отчёта — по одной
    за прогон, каждую не чаще раза в две недели. Поиск в сети дорог и
    медленен, поэтому результат хранится (kv niche_gaps).
    """
    import ai
    gaps = json.loads(db.kv_get(conn, "niche_gaps", "{}") or "{}")
    cands = [n for n in (rep.get("niches") or []) if n["n"] >= NICHE_MIN][:8]
    todo = [n for n in cands if now - int((gaps.get(n["niche"]) or {}).get("ts", 0)) > GAP_MAX_AGE]
    done = 0
    for n in todo[:GAP_PER_RUN]:
        examples = ["%s (%s)" % (c["company"], (c.get("what") or {}).get("en", "")) for c in n["companies"]]
        data, err = ai.gap_check(n["niche"], examples)
        if err or not isinstance(data, dict):
            if verbose:
                print("  аналоги в СНГ: %s — %s" % (n["niche"], err))
            continue
        ok = ("free", "partly", "crowded")
        analogs = [a for a in (data.get("analogs") or []) if isinstance(a, dict) and a.get("name")
                   and str(a.get("url") or "").startswith("http")][:5]
        gaps[n["niche"]] = {"kz": data.get("kz") if data.get("kz") in ok else None,
                            "cis": data.get("cis") if data.get("cis") in ok else None,
                            "analogs": [{"name": str(a["name"])[:60], "url": str(a["url"])[:300],
                                         "country": str(a.get("country") or "")[:3]} for a in analogs],
                            "note": data.get("note") if isinstance(data.get("note"), dict) else {}, "ts": now}
        done += 1
        if verbose:
            print("  аналоги в СНГ: %s — КЗ %s, СНГ %s, найдено %d"
                  % (n["niche"], gaps[n["niche"]]["kz"], gaps[n["niche"]]["cis"], len(analogs)))
    if done:
        db.kv_set(conn, "niche_gaps", json.dumps(gaps, ensure_ascii=False))
        conn.commit()
    return done


# ---------------------------------------------------------------------------
# 🔔 Раунды в нишах, за которыми следят
# ---------------------------------------------------------------------------
NR = {
    "ru": "🔔 <b>Новый раунд в нише «%s»</b>",
    "kk": "🔔 <b>«%s» тауашасында жаңа раунд</b>",
    "en": "🔔 <b>New round in “%s”</b>",
}


def niche_round_payloads(conn, now):
    """
    Свежие разобранные раунды (за 3 суток), о которых ещё не сообщали, — по
    одному блоку на нишу. Worker шлёт блок только тем, кто следит за нишей.
    Возвращает (блоки, ключи строк для отметки).
    """
    import ai
    _ensure(conn)
    sent = set(json.loads(db.kv_get(conn, "niche_round_sent", "[]") or "[]"))
    first = not sent and db.kv_get(conn, "niche_round_sent") is None
    by, keys = {}, []
    names = niche_names(conn)
    for r in rounds(conn, now - 3 * 86400):
        k = company_norm(r["company"])
        if not r["niche"] or not r["ai"] or k in sent:
            continue
        by.setdefault(r["niche"], []).append(r)
        keys.append(k)
    if first:
        # Первый запуск только запоминает: иначе ушёл бы залп за трое суток.
        db.kv_set(conn, "niche_round_sent", json.dumps(keys))
        conn.commit()
        return [], []
    out = []
    for niche, lst in by.items():
        texts = {}
        for lang in ai.LANGS:
            lines = [NR[lang] % html.escape(niche_label(niche, lang, names))]
            lines += ["• " + round_line(_round_brief(r), lang) +
                      ((" · " + ", ".join(html.escape(i) for i in r["investors"][:2])) if r.get("investors") else "")
                      for r in lst[:4]]
            texts[lang] = "\n".join(lines)
        out.append({"kind": "niche", "niche": niche, "sectors": lst[0]["sectors"][:1], "texts": texts,
                    "text": texts["ru"]})
    return out, keys


def mark_niche_rounds(conn, keys):
    sent = json.loads(db.kv_get(conn, "niche_round_sent", "[]") or "[]")
    db.kv_set(conn, "niche_round_sent", json.dumps((sent + keys)[-3000:]))
    conn.commit()


def main():
    setup_logging("market")
    ap = argparse.ArgumentParser(description="рынок по секторам")
    ap.add_argument("--refresh", action="store_true", help="обновить раунды и батчи YC")
    ap.add_argument("--lang", default="ru")
    args = ap.parse_args()
    load_env()
    conn = db.connect()
    now = int(time.time())
    rep = maybe_refresh(conn, now, force=True) if args.refresh else (last_report(conn) or compute(conn, now))
    print(render(rep, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
