# -*- coding: utf-8 -*-
"""
Аналитика трендов: какие темы запусков растут, какие самые массовые, что
появилось нового.

    python trends.py            посчитать и показать в консоли
    python trends.py --send     отправить отчёт в Telegram

ОТКУДА ТЕМЫ. Каждой находке ИИ-слой (ai.tag_items) ставит одну-две темы
из закрытого словаря — закрытого нарочно: свободные теги дробят одну тему
на десяток написаний, и ни одна не набирает веса. То, чего в словаре нет,
модель пишет в new_topic: так видно действительно новое.

КАК СЧИТАЕТСЯ РОСТ. Последние 7 дней против предыдущих 7, с поправкой на
длину окна. Тема попадает в «растут», только если за неделю у неё не
меньше 5 находок: рост с одной до трёх — это шум, а не тренд.

ЧЕСТНО ПРО ИСТОРИЮ. Сравнение с прошлой неделей имеет смысл, только когда
прошлая неделя целиком есть в базе. Пока истории меньше 14 дней, отчёт
прямо так и пишет и делает упор на объём и новые темы, а цифры роста
помечены как предварительные.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db                       # noqa: E402
import notify                   # noqa: E402
from common import load_env, setup_logging  # noqa: E402

WINDOW_DAYS = 7
MIN_SUPPORT = 5        # находок за неделю, чтобы тема могла «расти»
MIN_NEW = 3            # упоминаний, чтобы новая тема попала в отчёт
MIN_GROWTH = 1.5

TOPIC_RU = {
    "ai agents": "ИИ-агенты", "coding assistants": "ИИ для программистов",
    "voice ai": "голосовой ИИ", "video generation": "генерация видео",
    "image generation": "генерация картинок", "chatbots & support": "чат-боты и поддержка",
    "open-source models": "открытые модели", "local & on-device ai": "локальный ИИ",
    "devtools": "инструменты разработчика", "testing & qa": "тестирование",
    "observability": "мониторинг", "security": "безопасность", "databases": "базы данных",
    "infrastructure & cloud": "инфраструктура и облако", "data & analytics": "данные и аналитика",
    "browser automation": "автоматизация браузера", "no-code": "no-code",
    "design tools": "дизайн", "creator tools": "для авторов", "productivity": "продуктивность",
    "notes & knowledge": "заметки и знания", "email & calendar": "почта и календарь",
    "sales & crm": "продажи и CRM", "marketing & seo": "маркетинг и SEO",
    "e-commerce": "e-commerce", "payments": "платежи",
    "accounting & invoicing": "учёт и счета", "hr & recruiting": "HR и найм",
    "legal": "юристам", "health & fitness": "здоровье и фитнес",
    "mental health": "ментальное здоровье", "education": "образование",
    "language learning": "изучение языков", "real estate": "недвижимость",
    "travel": "путешествия", "food & delivery": "еда и доставка",
    "social & community": "сообщества", "dating": "знакомства", "gaming": "игры",
    "robotics": "робототехника", "hardware": "железо", "climate & energy": "климат и энергия",
    "crypto infrastructure": "крипто-инфраструктура", "privacy": "приватность",
    "other": "прочее",
}


def compute(conn, now, days=WINDOW_DAYS):
    """Посчитать тренды. Возвращает словарь с цифрами — без текста."""
    cur_start = now - days * 86400
    prev_start = now - 2 * days * 86400
    oldest = conn.execute(
        "SELECT MIN(first_seen) m FROM items WHERE bootstrap = 0").fetchone()["m"]
    history_days = (now - oldest) / 86400.0 if oldest else 0.0
    # Посевные записи не участвуют: у них first_seen — момент запуска
    # системы, а не появления продукта, и они исказили бы обе недели.
    rows = conn.execute(
        "SELECT i.item_id, i.source, i.first_seen, i.title, i.url, t.topics, t.new_topic, "
        "       (SELECT score FROM scores s WHERE s.item_id = i.item_id "
        "         ORDER BY ts DESC LIMIT 1) score "
        "  FROM items i JOIN item_topics t ON t.item_id = i.item_id "
        " WHERE i.first_seen >= ? AND i.bootstrap = 0", (prev_start,)).fetchall()

    cur, prev, new_topics = {}, {}, {}
    examples = {}
    total_cur = total_prev = 0
    for r in rows:
        try:
            topics = json.loads(r["topics"] or "[]")
        except ValueError:
            topics = []
        is_cur = r["first_seen"] >= cur_start
        if is_cur:
            total_cur += 1
        else:
            total_prev += 1
        for tp in topics:
            if tp == "other":
                continue
            bucket = cur if is_cur else prev
            bucket[tp] = bucket.get(tp, 0) + 1
            if is_cur:
                examples.setdefault(tp, []).append(r)
        if is_cur and r["new_topic"]:
            nt = r["new_topic"]
            new_topics.setdefault(nt, []).append(r)

    # Предыдущее окно может быть неполным, пока база молодая: темп считаем
    # на сутки реально покрытой истории, иначе рост раздувается.
    prev_cover = max(min(days, history_days - days), 0.5)
    rising = []
    for tp, n in cur.items():
        if n < MIN_SUPPORT:
            continue
        p = prev.get(tp, 0)
        growth = (n / float(days)) / ((p + 1) / prev_cover)
        if growth >= MIN_GROWTH:
            rising.append({"topic": tp, "cur": n, "prev": p, "growth": round(growth, 1),
                           "examples": _top_examples(examples.get(tp, []))})
    rising.sort(key=lambda d: -(d["growth"] * math.log2(d["cur"] + 1)))

    volume = sorted(({"topic": tp, "cur": n,
                      "share": round(100.0 * n / max(total_cur, 1), 1)}
                     for tp, n in cur.items()), key=lambda d: -d["cur"])

    fresh = sorted(({"name": nt, "cur": len(rs), "examples": _top_examples(rs)}
                    for nt, rs in new_topics.items() if len(rs) >= MIN_NEW),
                   key=lambda d: -d["cur"])

    return {
        "generated": now, "days": days, "history_days": round(history_days, 1),
        "growth_ready": history_days >= 2 * days,
        "total_cur": total_cur, "total_prev": total_prev,
        "rising": rising[:5], "volume": volume[:8], "new_topics": fresh[:5],
    }


def _top_examples(rows, n=2):
    rows = sorted(rows, key=lambda r: -(r["score"] or 0))[:n]
    return [{"title": (r["title"] or "")[:90], "url": r["url"]} for r in rows]


def stats_for_story(st):
    """Цифры в виде текста для модели — только то, что посчитано."""
    lines = ["Window: last %d days, %d launches (previous window: %d)."
             % (st["days"], st["total_cur"], st["total_prev"])]
    if not st["growth_ready"]:
        lines.append("History is only %.0f days: growth figures are preliminary." % st["history_days"])
    if st["rising"]:
        lines.append("Rising topics:")
        for r in st["rising"]:
            ex = "; ".join(e["title"] for e in r["examples"])
            lines.append("- %s: %d this week vs %d before (x%.1f). Examples: %s"
                         % (r["topic"], r["cur"], r["prev"], r["growth"], ex))
    lines.append("Largest topics: " + ", ".join("%s %d" % (v["topic"], v["cur"])
                                                for v in st["volume"]))
    if st["new_topics"]:
        lines.append("New themes outside the usual list: " + ", ".join(
            "%s (%d)" % (n["name"], n["cur"]) for n in st["new_topics"]))
    return "\n".join(lines)


def story(conn, st, now, lang="ru", max_age_h=12):
    """
    Связные выводы от модели — не чаще раза в 12 часов.

    Цифры считаются каждый прогон бесплатно, а текст стоит запрос к Groq:
    пересчитывать его каждые 10 минут — тратить квоту на одно и то же.
    """
    import ai
    cached = db.kv_get(conn, "trend_story_" + lang)
    ts = int(db.kv_get(conn, "trend_story_ts_" + lang, 0) or 0)
    if cached and now - ts < max_age_h * 3600:
        return cached
    if st["total_cur"] < 20:
        return None
    text, err = ai.write_trend_story(stats_for_story(st), lang=lang)
    if text:
        db.kv_set(conn, "trend_story_" + lang, text)
        db.kv_set(conn, "trend_story_ts_" + lang, now)
        conn.commit()
        return text
    return cached


def _ru(tp):
    return TOPIC_RU.get(tp, tp)


def render(st, story_text=None):
    """Отчёт для Telegram (HTML)."""
    e = notify._esc
    lines = ["📈 <b>Тренды за %d дней</b> — %d находок" % (st["days"], st["total_cur"])]
    if not st["growth_ready"]:
        lines.append("<i>История пока %.0f дн.: рост к прошлой неделе предварительный, "
                     "надёжным станет через %d дн.</i>"
                     % (st["history_days"], max(1, int(round(2 * st["days"] - st["history_days"])))))
    if st["rising"]:
        lines += ["", "<b>Растут</b>"]
        for r in st["rising"]:
            lines.append("• %s — %d (было %d), ×%.1f" % (e(_ru(r["topic"])), r["cur"], r["prev"], r["growth"]))
            for ex in r["examples"][:1]:
                lines.append('   <a href="%s">%s</a>' % (e(ex["url"] or ""), e(ex["title"])))
    if st["volume"]:
        lines += ["", "<b>Больше всего запусков</b>"]
        lines.append(" · ".join("%s %d%%" % (e(_ru(v["topic"])), round(v["share"]))
                                for v in st["volume"][:6]))
    if st["new_topics"]:
        lines += ["", "<b>Новое, чего нет в привычных темах</b>"]
        for n in st["new_topics"]:
            ex = n["examples"][0] if n["examples"] else None
            tail = ' — <a href="%s">%s</a>' % (e(ex["url"] or ""), e(ex["title"][:60])) if ex else ""
            lines.append("• %s (%d)%s" % (e(n["name"]), n["cur"], tail))
    if story_text:
        lines += ["", "🧠 <b>Выводы</b>", e(story_text)]
    if not (st["rising"] or st["volume"]):
        lines += ["", "Данных пока мало: темы проставляются новым находкам с каждым прогоном."]
    return "\n".join(lines)


# Еженедельный отчёт: понедельник, окно 07-09 UTC (10-12 по Москве), не
# чаще раза в 6 дней. Та же схема «окно + память», что и у сводки: при
# любом разнобое в расписании отчёт уйдёт один раз.
def weekly_due(conn, now):
    g = time.gmtime(now)
    if g.tm_wday != 0 or not (7 <= g.tm_hour < 9):
        return False
    last = int(db.kv_get(conn, "last_trends", 0) or 0)
    return now - last >= 6 * 86400


def maybe_send_weekly(conn, now, dry=False, lang="ru"):
    if not weekly_due(conn, now):
        return False
    st = compute(conn, now)
    text = render(st, story(conn, st, now, lang))
    if dry:
        print(text)
        return False
    # Всем подписчикам через Worker; без него — владельцу напрямую.
    ok, err = notify.deliver(broadcast=[text])
    if ok:
        db.kv_set(conn, "last_trends", now)
        conn.commit()
    print("  тренды недели: %s" % ("отправлены (%d)" % ok if ok else "не ушли — %s" % err))
    return ok


def main():
    setup_logging("trends")
    ap = argparse.ArgumentParser(description="аналитика трендов запусков")
    ap.add_argument("--send", action="store_true", help="отправить отчёт в Telegram")
    args = ap.parse_args()
    load_env()
    conn = db.connect()
    now = int(time.time())
    st = compute(conn, now)
    text = render(st, story(conn, st, now))
    if args.send:
        ok, err = notify.send(text, preview=False)
        print("отправлено" if ok else "не ушло: %s" % err)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
