# -*- coding: utf-8 -*-
"""
Проверка разбора рынка на подставных данных: python test_market.py

Главный риск модуля — не сеть, а разбор заголовков: оценка компании
(«at a $10B valuation»), выданная за сумму раунда, раздувает сектор в
десятки раз, а «raises guidance» у публичной компании превращается в
«раунд». Каждый случай взят из живой выдачи Google News 2026-09-28.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db          # noqa: E402
import market      # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FAILED = []


def check(name, ok, explain=""):
    if not ok:
        FAILED.append(name)
    print("%-4s %-52s %s" % ("PASS" if ok else "FAIL", name, explain))


def deals():
    print("--- заголовки раундов ---")
    cases = [
        ("Observability startup Groundcover bags $35M in new funding to take on Datadog - SiliconANGLE",
         35e6, "«new funding» — не «new fund»"),
        ("AI agent startup Instinct raises $1B Series C at $10B valuation", 1e9, "сумма раунда, не оценка"),
        ("Exclusive: Manufacturing AI startup CADDi valued at $1.2 billion following $114 million Series D funding round - Fortune",
         114e6, "раунд без глагола, оценка пропущена"),
        ("Israeli AI robotics startup Enigma emerges from stealth with $71 million Seed round", 71e6, "выход из тени"),
        ("Jaipur Robotics raises EUR 4.3 million - Startupticker", 4.3e6 * 1.1, "евро словом"),
        ("Database maker ClickHouse raises $400M, acquires AI observability startup Langfuse", 400e6,
         "покупка в хвосте не отменяет раунд"),
    ]
    for title, want, why in cases:
        d = market.parse_deal(title)
        got = d[1] if d else None
        check("раунд: %s" % why, d is not None and got is not None and abs(got - want) < 1e5,
              "%s -> %s" % (title[:40], got))
    not_rounds = [
        ("Taiwan Semiconductor Manufacturing Company (TSM) Raises Guidance as Demand for Advanced Chips Soars",
         "отчётность публичной компании"),
        ("Robotics startup Generalist AI is in talks to raise a new funding round at a $3 billion valuation",
         "переговоры — ещё не сделка"),
        ("Lightspeed targets $250M for new India fund, focusing on early-stage AI", "фонд собирает на себя"),
        ("Viral video of new robot released by Chinese company raises questions about its abilities", "не деньги"),
        ("The Week’s 10 Biggest Funding Rounds: Physical AI Startup Atoms Leads", "обзор, а не сделка"),
    ]
    for title, why in not_rounds:
        check("не раунд: %s" % why, market.parse_deal(title) is None, title[:50])
    check("обзор распознан как аналитика",
          bool(market.ANALYSIS.search("Sector Snapshot: Robotics Startups On Fire As Venture Funding Surges")), "")


def classify():
    print("\n--- секторы ---")
    cases = [
        ("Humanoid robots for warehouse picking", "hardware"),
        ("Open-source database for vector search", "devtools"),
        ("Payroll for small clinics in Kazakhstan", "fintech"),
        ("AI agents that answer your support tickets", "ai_agents"),
    ]
    for text, want in cases:
        got = market.classify(text)
        check("сектор: %s" % want, bool(got) and got[0] == want, "%s -> %s" % (text[:40], got))
    check("темы ИИ-разметки переводятся в секторы",
          market.sectors_for(["robotics", "ai agents"]) == ["hardware", "ai_agents"], "")


def momentum():
    """Рост денег и доли в YC даёт «растёт»; падение — «остывает»."""
    print("\n--- импульс сектора ---")
    conn = db.connect(Path(tempfile.mkdtemp()) / "m.sqlite")
    market._ensure(conn)
    now = int(time.time())
    day = 86400
    def put(key, ts, company, sectors, stage=None, niche=None, ai=0):
        conn.execute("INSERT INTO deals (key, ts, seen, title, url, outlet, company, amount_usd, sectors, "
                     "stage, niche, ai) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (key, ts, now, "%s raises $10M" % company, "u", "o", company, 1e7, sectors,
                      stage, niche, ai))
    for i in range(12):   # железо: 12 компаний сейчас против 3 раньше, почти все ранние
        put("hw%d" % i, now - (i % 10) * day, "Robo%d" % i, '["hardware"]', "seed", "warehouse robots", 1)
    for i in range(3):
        put("hwp%d" % i, now - 20 * day, "Oldrobo%d" % i, '["hardware"]', "c+")
    for i in range(10):   # стройка: 2 против 10
        put("pp%d" % i, now - 20 * day, "Build%d" % i, '["proptech"]', "seed")
    for i in range(2):
        put("pc%d" % i, now - 3 * day, "Newbuild%d" % i, '["proptech"]', "b")
    import json
    db.kv_set(conn, "market_yc", json.dumps([
        {"name": "S26", "n": 200, "share": {"hardware": 24.0, "proptech": 1.0}, "physical": 40.0},
        {"name": "W26", "n": 200, "share": {"hardware": 12.0, "proptech": 3.0}, "physical": 22.0},
        {"name": "F25", "n": 150, "share": {"hardware": 10.0, "proptech": 3.0}, "physical": 20.0},
        {"name": "S25", "n": 160, "share": {"hardware": 9.0, "proptech": 3.0}, "physical": 18.0}]))
    conn.commit()
    rep = market.compute(conn, now)
    by = {s["id"]: s for s in rep["sectors"]}
    check("железо растёт", by["hardware"]["trend"] == "up", "импульс %.2f" % by["hardware"]["momentum"])
    check("стройка остывает", by["proptech"]["trend"] == "down", "импульс %.2f" % by["proptech"]["momentum"])
    check("сектор без данных не «растёт»", by["crypto"]["trend"] == "flat", "")
    text = market.render(rep, "ru")
    check("в отчёте есть доля физического продукта в YC", "40.0%" in text, "")
    check("в отчёте есть число раундов", "раундов: 12" in text, "")
    check("ниша с ранними раундами в отчёте", "warehouse robots" in text, "")
    n = rep["niches"][0] if rep["niches"] else {}
    check("ниша: 12 раундов, все ранние", n.get("n") == 12 and n.get("early") == 12, str(n.get("n")))
    check("наших находок в отчёте нет", "находок" not in text, "")


def dedupe():
    """Одна сделка в пяти изданиях — один раунд; суммы в рупиях; стадии."""
    print("\n--- склейка дублей ---")
    conn = db.connect(Path(tempfile.mkdtemp()) / "d.sqlite")
    market._ensure(conn)
    now = int(time.time())
    for t in ("Amaani raises $5M in Series A - YourStory.com",
              "Beauty and wellness startup Amaani raises $5 Mn led by BECO Capital - Entrackr",
              "AÏZA parent Amaani raises $5M Series A led by BECO to expand beauty brand - fwdstart.me"):
        market.add_deal(conn, t, "u", "o", now - 86400, now)
    # ИИ разобрал одну из строк: чистое имя и ниша — главнее регулярки.
    conn.execute("UPDATE deals SET ai = 1, company = 'Amaani', niche = 'beauty brands', stage = 'a' "
                 "WHERE company = 'startup Amaani'")
    conn.commit()
    rs = market.rounds(conn, now - 5 * 86400)
    check("три заметки об Amaani — один раунд", len(rs) == 1, str([r["company"] for r in rs]))
    check("у раунда три издания", rs and rs[0]["outlets"] >= 1, "")
    check("сумма в крорах", abs((market._amount("Balwaan Krishi raises Rs 100 Cr in Series B") or 0) - 12e6) < 1,
          str(market._amount("Balwaan Krishi raises Rs 100 Cr in Series B")))
    check("стадия pre-seed", market.parse_stage("secures $2M pre-seed round") == "pre-seed", "")
    check("стадия Series C+", market.parse_stage("raises $80M Series D") == "c+", "")
    check("компания: хвост AI не мешает", market.company_norm("Dextr AI") == market.company_norm("Dextr"), "")


def niche_extras():
    """Мегараунд не раздувает сумму ниши; кривая за полгода; инвесторы."""
    print("\n--- ниши: мегараунд, кривая, инвесторы ---")
    conn = db.connect(Path(tempfile.mkdtemp()) / "n.sqlite")
    market._ensure(conn)
    now = int(time.time())
    rows = [("Alpha", 12e9, 2, "c+", '["a16z"]'), ("Beta", 5e6, 3, "seed", '["a16z", "YC"]'),
            ("Gamma", 3e6, 5, "seed", '["YC"]'), ("Delta", 2e6, 60, "seed", None)]
    for name, usd, days, stage, inv in rows:
        conn.execute("INSERT INTO deals (key, ts, seen, title, url, outlet, company, amount_usd, sectors, stage, "
                     "niche, ai, investors) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                     (name, now - days * 86400, now, name + " raises", "u", "o", name, usd, '["ai_agents"]',
                      stage, "ai agents for sales", inv))
    conn.commit()
    n = market.niches(conn, now)[0]
    check("мегараунд не в сумме ниши", n["usd"] == 8e6, str(n["usd"]))
    check("мегараунд показан отдельно", n["mega"] and n["mega"][0]["company"] == "Alpha", "")
    check("инвесторы по частоте", set(n["investors"][:2]) == {"a16z", "YC"}, str(n["investors"]))
    check("кривая за полгода: 26 недель", len(n["weekly"]) == market.HISTORY_WEEKS, "")
    check("раунд 60 дней назад — в кривой, но не в окне", n["n"] == 3 and sum(n["weekly"]) == 4, str(n["weekly"]))
    check("мегараунд в нише -> перегрев", n["opp"]["type"] == "overheated", n["opp"]["type"])
    small = market.opportunity({"n": 2, "early": 2, "usd": 1e6, "weekly": [0] * 24 + [1, 1], "companies_6m": 2})
    mom = [p["pts"] for p in small["parts"] if p["k"] == "momentum"][0]
    check("2 раунда против 0 — не «рост»", mom == 0 and small["type"] != "window", "%s %s" % (mom, small["type"]))
    win = market.opportunity({"n": 4, "early": 3, "usd": 9e6, "weekly": [0] * 16 + [0] * 6 + [1, 1, 1, 1],
                              "companies_6m": 5, "pain": [{"text": "x"}]})
    check("ранние раунды, мало игроков, рост — окно", win["type"] == "window", win["type"])


def imports():
    """Модули прогона импортируются: 2026-09-29 сломанный отступ в scout.py
    прошёл мимо тестов (они его не импортировали) и уронил прогон в Actions."""
    print("\n--- модули прогона ---")
    import importlib
    for name in ("scout", "brief", "export_d1", "trends"):
        try:
            importlib.import_module(name)
            check("импортируется " + name, True, "")
        except Exception as e:           # noqa: BLE001
            check("импортируется " + name, False, str(e)[:80])


def main():
    imports()
    deals()
    classify()
    momentum()
    dedupe()
    niche_extras()
    print()
    if FAILED:
        print("ПРОВАЛЕНО %d: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
