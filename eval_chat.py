# -*- coding: utf-8 -*-
"""
Эталонный набор вопросов к ИИ-аналитику: python eval_chat.py [--out report.json]

ЗАЧЕМ. «Подкрутить точность» без замера — это на глаз. Здесь 30 типичных
вопросов стартапера и для каждого — что должно найтись. После каждой правки
прогон показывает цифры «было -> стало»:
  * поиск: нашлась ли нужная ниша или компания (retrieval);
  * выдумки: имена компаний в ответе, которых нет в переданных фактах;
  * опора: сколько строк с цифрами пришлось вырезать без ссылки на факт;
  * скорость: первые слова и весь ответ, медиана и 90-й процентиль.

КАК. Нужен бот в режиме разработки с отладочным маршрутом (в бою его нет):
    cd worker && npx wrangler@4 dev --remote --port 8799 --var LS_DEBUG:1
Отладочный маршрут отвечает тем же путём, что и бот в Telegram, но без
отправки сообщений.
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = "http://127.0.0.1:8799/debug-ask"

# (вопрос, что должно найтись: подстроки в названиях найденных ниш или в
# тексте фактов — хватает любой из списка)
CASES = [
    ("Сколько денег зашло в страхование с ИИ за полгода и в какие ниши?", ["insurance"]),
    ("Что происходит с ИИ для юристов? Есть ли окно?", ["legal"]),
    ("Кто финансирует гуманоидных роботов и сколько?", ["humanoid", "robot"]),
    ("Оборонные стартапы: сколько раундов и кто инвесторы?", ["defense", "military"]),
    ("Платежи в стейблкоинах — рынок перегрет или формируется?", ["stablecoin"]),
    ("ИИ-агенты для поддержки клиентов: сколько конкурентов и денег?", ["support", "customer service", "contact center"]),
    ("Куда идут деньги в кибербезопасности для ИИ-агентов?", ["security", "identity", "agent"]),
    ("Есть ли деньги в edtech сейчас?", ["educat", "learning", "tutor"]),
    ("Что с энергетикой и сетями? Оптимизация энергосетей растёт?", ["grid", "energy"]),
    ("Логистика и склады: какие ниши получили раунды?", ["logistic", "warehouse", "freight", "supply"]),
    ("Медицина: где ранние раунды за последний месяц?", ["health", "clinic", "patient", "medical"]),
    ("Инструменты для разработчиков с ИИ — сколько игроков?", ["developer", "coding", "code", "devtool"]),
    ("HR и найм с ИИ: окно или перегрев?", ["recruit", "hiring", "hr "]),
    ("Недвижимость и стройка: что финансируют?", ["real estate", "construction", "property", "housing"]),
    ("Кредитование малого бизнеса — что с деньгами?", ["lending", "loan", "credit"]),
    ("Сколько привлекла компания Baselayer и что она делает?", ["baselayer"]),
    ("Что за компания Temporal и сколько она подняла?", ["temporal"]),
    ("Instinct — какой раунд и в какой нише?", ["instinct"]),
    ("Космос и спутники: сколько денег за полгода?", ["satellite", "space"]),
    ("Голосовые ИИ-агенты: сколько раундов по месяцам?", ["voice"]),
    ("ИИ для бухгалтерии и финансов компаний — что с рынком?", ["account", "finance", "bookkeep", "cfo"]),
    ("Маркетинг и продажи с ИИ: есть ли свободные ниши?", ["marketing", "sales", "crm"]),
    ("E-commerce: что сейчас финансируют?", ["commerce", "retail", "shop", "marketplace"]),
    ("Агротех: есть ли раунды?", ["agri", "farm", "crop"]),
    ("Что с дронами?", ["drone", "unmanned"]),
    ("Крипто-инфраструктура: сколько денег?", ["crypto", "blockchain", "web3", "stablecoin"]),
    ("Я делаю CRM для клиник в Казахстане. Что рядом получает деньги?", ["clinic", "health", "crm", "patient"]),
    ("У меня $10 000 и я умею делать ИИ-продукты. Куда идти?", []),
    ("Какие ниши сейчас в Казахстане свободны?", []),
    ("Какие рынки перегреты прямо сейчас?", []),
]

# Слова с заглавной, которые не названия компаний.
NOT_NAMES = {"ИИ", "AI", "B2B", "B2C", "SaaS", "CRM", "MVP", "ICP", "CIS", "KZ", "US", "EU", "MENA", "API",
             "Series", "Seed", "Pre", "The", "And", "For", "LLM", "GPU", "HR", "IT", "ROI", "CAC", "LTV", "TAM",
             "WhatsApp", "Telegram", "Kaspi", "Google", "OpenAI", "YC", "Y", "Combinator", "USD", "Q1", "Q2", "Q3",
             "Q4", "PROFILE", "TOTAL", "FACTS", "NICHE", "ROUND", "OPPORTUNITY", "COMPETITOR", "SECTOR", "TRENDS"}


def ask(q, timeout=120):
    req = urllib.request.Request(BASE + "?q=" + urllib.parse.quote(q), headers={"User-Agent": "curl/8"})
    t = time.time()
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    d["wall_ms"] = int((time.time() - t) * 1000)
    return d


def unknown_names(answer, facts):
    """Латинские «имена» из ответа, которых нет ни в одном факте — кандидаты в выдумки."""
    blob = " ".join(f.get("text", "") for f in facts).lower()
    # Номера фактов [F10] и сокращения (IAM, FDIC, B2B-) — не имена компаний.
    text = re.sub(r"\[F\d+(?:[,\s-]+F?\d+)*\]", " ", answer or "")
    names = set(re.findall(r"\b[A-Z][A-Za-z0-9.&'-]{2,}(?:\s[A-Z][A-Za-z0-9.&'-]{2,})?", text))
    out = []
    for n in names:
        n = n.rstrip("-.")
        first = n.split()[0]
        if (first in NOT_NAMES or n in NOT_NAMES or re.fullmatch(r"[A-Z0-9]{2,5}", first)
                or re.fullmatch(r"F\d+(?:-F?\d+)?", first)):
            continue
        if n.lower() not in blob and first.lower() not in blob:
            out.append(n)
    return sorted(out)


def run():
    rows = []
    for q, expect in CASES:
        try:
            d = ask(q)
        except Exception as e:           # noqa: BLE001
            rows.append({"q": q, "error": str(e)[:120]})
            print("ERR  %s — %s" % (q[:60], str(e)[:80]))
            continue
        hay = " ".join(d.get("niches") or []).lower() + " " + " ".join(f.get("text", "") for f in d.get("facts") or []).lower()
        hit = (not expect) or any(e in hay for e in expect)
        niche_hit = (not expect) or any(e in " ".join(d.get("niches") or []).lower() for e in expect)
        bad = unknown_names(d.get("answer"), d.get("facts") or [])
        row = {"q": q, "hit": hit, "niche_hit": niche_hit, "invented": bad, "dropped": d.get("dropped_lines", 0),
               "first_ms": d.get("first_token_ms"), "total_ms": d.get("total_ms"), "facts": d.get("n_facts"),
               "niches": d.get("niches"), "empty": not (d.get("answer") or "").strip(), "answer": d.get("answer")}
        rows.append(row)
        print("%s %s %5s/%5s ms  facts %2s  выдумки %d  вырезано %d  %s"
              % ("OK " if hit else "MISS", "н" if niche_hit else "-", row["first_ms"], row["total_ms"],
                 row["facts"], len(bad), row["dropped"], q[:58]))
    good = [r for r in rows if "error" not in r]
    firsts = [r["first_ms"] for r in good if r["first_ms"]]
    totals = [r["total_ms"] for r in good if r["total_ms"]]
    pct = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(len(xs) * p))] if xs else None  # noqa: E731
    summary = {
        "cases": len(rows), "errors": len(rows) - len(good),
        "retrieval_hit": round(sum(r["hit"] for r in good) / max(len(good), 1), 2),
        "niche_hit": round(sum(r["niche_hit"] for r in good) / max(len(good), 1), 2),
        "answers_with_invented_names": sum(1 for r in good if r["invented"]),
        "invented_names_total": sum(len(r["invented"]) for r in good),
        "dropped_lines_total": sum(r["dropped"] for r in good),
        "empty_answers": sum(1 for r in good if r["empty"]),
        "first_token_p50_ms": statistics.median(firsts) if firsts else None, "first_token_p90_ms": pct(firsts, 0.9),
        "total_p50_ms": statistics.median(totals) if totals else None, "total_p90_ms": pct(totals, 0.9),
    }
    print("\nИТОГ:", json.dumps(summary, ensure_ascii=False))
    return summary, rows


def main():
    ap = argparse.ArgumentParser(description="эталонный прогон ИИ-аналитика")
    ap.add_argument("--out", help="сохранить отчёт в JSON")
    args = ap.parse_args()
    summary, rows = run()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
