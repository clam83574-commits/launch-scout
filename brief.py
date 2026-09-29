# -*- coding: utf-8 -*-
"""
☀️ Сводка дня — одно короткое сообщение вместо потока уведомлений.

    python brief.py            показать сводку в консоли
    python brief.py --send     разослать сейчас

ЗАЧЕМ (решение владельца 2026-09-29). Бот присылал много сообщений: каждую
горячую находку, сводку дважды в день, раунды по сектору на сообщение. По
умолчанию теперь приходит только это: раз в день, коротко — какие ниши
получают деньги и какие новые стартапы вышли. Дальше человек спрашивает
сам («расскажи подробнее про эту нишу»), и отвечает чат в боте.

КАК. Факты собирает код: ниши и раунды из market.py, новые продукты — лучшие
находки суток, прошедшие ИИ-проверку «это запуск и бизнес». Модель только
объясняет простыми словами, что это за ниши и продукты, и пишет вывод в одну
фразу. Цифры, названия и ссылки модели не доверяются — их вставляет код.
"""
import argparse
import html
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai                                   # noqa: E402
import db                                   # noqa: E402
import market                               # noqa: E402
import notify                               # noqa: E402
import score as scoring                     # noqa: E402
from common import load_env, setup_logging  # noqa: E402

SRC = {"x": "X", "hn": "Hacker News", "gh": "GitHub", "ph": "Product Hunt", "yc": "Y Combinator"}
UNIT = {"ru": {"x": "лайков", "hn": "очков", "gh": "звёзд", "ph": "голосов"},
        "kk": {"x": "лайк", "hn": "ұпай", "gh": "жұлдыз", "ph": "дауыс"},
        "en": {"x": "likes", "hn": "points", "gh": "stars", "ph": "votes"}}

T = {
    "ru": {"head": "☀️ <b>Коротко за %s</b>", "niches": "💡 <b>Куда пошли деньги</b>",
           "niche": "%d раундов за %d дн., ранних %d, %s", "rounds": "💰 <b>Раунды за сутки:</b> %d на %s",
           "new": "🆕 <b>Новые стартапы</b>", "ask": "💬 Спросите: «расскажи подробнее про %s»",
           "empty": "За сутки заметного не было — ни новых ниш, ни запусков с откликом."},
    "kk": {"head": "☀️ <b>%s — қысқаша</b>", "niches": "💡 <b>Ақша қайда кетті</b>",
           "niche": "%d раунд (%d күн), ерте %d, %s", "rounds": "💰 <b>Тәуліктегі раундтар:</b> %d, %s",
           "new": "🆕 <b>Жаңа стартаптар</b>", "ask": "💬 Сұраңыз: «%s туралы толығырақ айтып бер»",
           "empty": "Тәулік ішінде елеулі ештеңе болмады."},
    "en": {"head": "☀️ <b>In short, %s</b>", "niches": "💡 <b>Where the money went</b>",
           "niche": "%d rounds in %d days, %d early, %s", "rounds": "💰 <b>Rounds in 24h:</b> %d, %s",
           "new": "🆕 <b>New startups</b>", "ask": "💬 Ask: “tell me more about %s”",
           "empty": "Nothing notable in the last 24 hours — no new niches or launches with traction."},
}

EXPLAIN = """You explain a daily startup-market brief to founders in plain words.
Input: niches (where several companies raised venture rounds) and new products (launched in the last 24 hours).
For every niche write what the companies in it actually sell and to whom — max 14 words, no jargon.
For every product write what it does and for whom — max 12 words.
takeaway: ONE sentence on what this means for a small team deciding what to build (grounded only in the input, no numbers you were not given).
Answer in Russian (ru), Kazakh in Cyrillic (kk) and English (en).
Reply JSON only: {"ru": {"niches": ["..."], "products": ["..."], "takeaway": "..."}, "kk": {...}, "en": {...}} — the lists in the same order as the input."""


def _new_startups(conn, now, limit=4):
    """
    Лучшие запуски суток: с настоящим откликом, прошедшие ИИ-проверку
    «запуск и бизнес». Product Hunt и Launch HN — продукты по определению.
    Один домен — одна строка.
    """
    rows = conn.execute("SELECT * FROM items WHERE first_seen >= ? AND source IN ('x','hn','gh','ph') "
                        "AND bootstrap = 0", (now - 26 * 3600,)).fetchall()
    out, seen = [], set()
    scored = []
    for it in rows:
        total, tier, b = scoring.score_item(conn, it, now)
        if tier == scoring.ARCHIVE:
            continue
        note = ai.get_note(conn, it["item_id"])
        if note is not None and (ai.verdict(note, it["source"])[0] or ai.not_business(note)):
            continue
        if note is None and it["source"] == "x":
            continue                # твит без проверки — слишком много шума
        last = conn.execute("SELECT likes FROM metrics WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
                            (it["item_id"],)).fetchone()
        scored.append((total, it, (last["likes"] if last else None), note))
    for total, it, likes, note in sorted(scored, key=lambda t: -t[0]):
        key = it["domain"] or it["item_id"]
        if key in seen:
            continue
        seen.add(key)
        name = (it["title"] or "").split(" — ")[0]
        name = name.replace("Launch HN: ", "").replace("Show HN: ", "")[:60]
        out.append({"name": name, "source": it["source"], "likes": likes, "url": it["url"],
                    "text": ((note or {}).get("i18n", {}).get("en", {}) or {}).get("summary")
                    or (it["body"] or it["title"] or "")[:300]})
        if len(out) >= limit:
            break
    return out


def build(conn, now):
    """{lang: html} или None, если сказать нечего."""
    rep = market.last_report(conn) or {}
    names = rep.get("niche_names") or {}
    niches = [n for n in (rep.get("niches") or []) if n["n"] >= market.NICHE_MIN][:3]
    day_rounds = sorted(market.rounds(conn, now - 86400), key=lambda r: -(r["usd"] or 0))
    startups = _new_startups(conn, now)
    if not (niches or startups or day_rounds):
        return None
    payload = {"niches": [{"niche": n["niche"], "companies": [
                   {"name": r["company"], "what": (r.get("what") or {}).get("en", "")} for r in n["companies"][:4]]}
                          for n in niches],
               "products": [{"name": s["name"], "about": s["text"][:300]} for s in startups]}
    expl = {}
    if ai.available()[0] and (niches or startups):
        try:
            data, _err = ai._chat(ai.DEFAULT_MODEL, EXPLAIN, json.dumps(payload, ensure_ascii=False),
                                  max_tokens=2500)
            expl = data if isinstance(data, dict) else {}
        except ai.RateLimited:
            expl = {}
    e = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    out = {}
    date = time.strftime("%d.%m", time.gmtime(now))
    for lang in ai.LANGS:
        tx, x = T[lang], (expl.get(lang) or {})
        nx, px = x.get("niches") or [], x.get("products") or []
        lines = [tx["head"] % date]
        if niches:
            lines += ["", tx["niches"]]
            for i, n in enumerate(niches):
                label = market.niche_label(n["niche"], lang, names)
                lines.append("%d. <b>%s</b> — %s" % (i + 1, e(label), tx["niche"] % (
                    n["n"], rep.get("niche_days", market.NICHE_DAYS), n["early"], market.usd(n["usd"], lang))))
                if i < len(nx) and nx[i]:
                    lines.append("   " + e(str(nx[i])[:160]))
                lines.append("   " + ", ".join(
                    '<a href="%s">%s</a>%s' % (html.escape(r.get("url") or "", quote=True), e(r["company"]),
                                               (" " + market.usd(r["usd"], lang)) if r.get("usd") else "")
                    for r in n["companies"][:3]))
        if day_rounds:
            total = sum(r["usd"] or 0 for r in day_rounds)
            lines += ["", tx["rounds"] % (len(day_rounds), market.usd(total, lang))]
            lines += ["• " + market.round_line(market._round_brief(r), lang) for r in day_rounds[:3]]
        if startups:
            lines += ["", tx["new"]]
            for i, s in enumerate(startups):
                what = px[i] if i < len(px) and px[i] else ""
                num = (" · %s %s" % (s["likes"], UNIT[lang].get(s["source"], ""))) if s["likes"] else ""
                lines.append('• <a href="%s">%s</a>%s <i>(%s%s)</i>' % (
                    html.escape(s["url"] or "", quote=True), e(s["name"]),
                    (" — " + e(str(what)[:140])) if what else "", SRC[s["source"]], num))
        if x.get("takeaway"):
            lines += ["", "🧠 " + e(str(x["takeaway"])[:300])]
        if niches:
            lines += ["", tx["ask"] % e(market.niche_label(niches[0]["niche"], lang, names))]
        out[lang] = "\n".join(lines)[:3900]
    return out


# Раз в сутки, окно 04–06 UTC (9–11 по Астане, 7–9 по Москве): к началу дня.
WINDOW_UTC = (4, 6)


def due(conn, now):
    h = time.gmtime(now).tm_hour
    if not (WINDOW_UTC[0] <= h < WINDOW_UTC[1]):
        return False
    return now - int(db.kv_get(conn, "last_brief", 0) or 0) >= 20 * 3600


def maybe_send(conn, now, dry=False, force=False):
    if not (force or due(conn, now)):
        return 0
    texts = build(conn, now)
    if not texts:
        return 0
    if dry:
        print(texts["ru"])
        return 0
    ok, err = notify.deliver(broadcast=[{"kind": "brief", "texts": texts, "text": texts["ru"]}])
    if ok or not err:
        db.kv_set(conn, "last_brief", now)
        conn.commit()
    print("  сводка дня: %s" % ("разослана (%d)" % ok if ok else "не ушла — %s" % err))
    return ok


def main():
    setup_logging("brief")
    ap = argparse.ArgumentParser(description="сводка дня")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    load_env()
    conn = db.connect()
    now = int(time.time())
    if args.send:
        maybe_send(conn, now, force=True)
    else:
        texts = build(conn, now)
        print(texts["ru"] if texts else "сказать нечего")
    return 0


if __name__ == "__main__":
    sys.exit(main())
