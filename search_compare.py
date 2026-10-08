"""Временное сравнение поиска в сети: Perplexity sonar против бесплатного Groq. Удаляется после прогона."""
import json
import os
import re
import time

import requests

import ai

GROQ = "https://api.groq.com/openai/v1/chat/completions"

NICHES = [
    ("ai bookkeeping for small business", ["Pilot", "Digits", "Puzzle"]),
    ("employee mental health benefits", ["Spring Health", "Lyra Health", "Modern Health"]),
    ("restaurant inventory management", ["MarketMan", "Restaurant365", "Apicbase"]),
]
QUESTIONS = [
    "Сколько привлёк Nace.AI, кто инвесторы и кто конкуренты?",
    "Есть ли в Казахстане сервисы онлайн-бухгалтерии для ИП с ИИ и сколько они стоят?",
]
FACTS_SYSTEM = """You search the web to answer a startup founder's question. Reply JSON only:
{"facts": [{"text": "one factual sentence with names, numbers and dates", "url": "https://exact source page", "date": "YYYY-MM-DD or empty"}]}
6-10 facts, newest first, only from pages you actually opened. Never invent.
Use only the browser_search tool to search; write the final JSON as plain text in your answer (it is not a tool)."""


def groq_call(model, system, user, tools=None):
    keys = [k for k in (os.environ.get(n, "").strip() for n in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4")) if k]
    body = {"model": model, "temperature": 0.2, "max_completion_tokens": 3000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if tools:
        body.update({"tools": tools, "tool_choice": "auto", "reasoning_effort": "low"})
    last = ""
    for k in keys:
        r = requests.post(GROQ, json=body, timeout=150, headers={"Authorization": "Bearer " + k})
        if r.status_code == 429:
            last = "429"
            continue
        if r.status_code != 200:
            return None, f"Groq {r.status_code}: {r.text[:150]}"
        c = r.json()["choices"][0]["message"]["content"] or ""
        return ai._loose_json(c), None if ai._loose_json(c) is not None else "не JSON"
    return None, last or "нет ключей"


def or_call(system, user):
    return ai._chat_or("perplexity/sonar", system, user, max_tokens=3000, timeout=150)


def live(url):
    try:
        r = requests.get(url, timeout=12, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        return r.status_code < 400
    except Exception:
        return False


ENGINES = [
    ("perplexity/sonar", lambda s, u: or_call(s, u)),
    ("groq gpt-oss-120b+browser", lambda s, u: groq_call("openai/gpt-oss-120b", s, u, [{"type": "browser_search"}])),
    ("groq compound-beta", lambda s, u: groq_call("compound-beta", s, u)),
    ("groq compound-beta-mini", lambda s, u: groq_call("compound-beta-mini", s, u)),
]


def report(kind, data, err, secs):
    if err or not isinstance(data, dict):
        print(f"    ✗ {err or 'пусто'} · {secs:.0f} с")
        return
    if kind == "dossier":
        comp = [c for c in data.get("competitors") or [] if isinstance(c, dict)]
        urls = [c.get("url") for c in comp if str(c.get("url") or "").startswith("http")]
        ok = sum(live(u) for u in urls)
        kz = [c.get("name") for c in comp if str(c.get("market", "")).upper() in ("KZ", "CIS", "RU")]
        print(f"    конкурентов {len(comp)}, ссылки живые {ok}/{len(urls)}, жалоб {len(data.get('complaints') or [])}, КЗ/СНГ: {kz} · {secs:.0f} с")
        print("      " + "; ".join(f"{c.get('name')} [{c.get('price', '')}]" for c in comp[:8]))
    else:
        facts = [f for f in data.get("facts") or [] if isinstance(f, dict)]
        urls = [f.get("url") for f in facts if str(f.get("url") or "").startswith("http")]
        ok = sum(live(u) for u in urls)
        print(f"    фактов {len(facts)}, ссылки живые {ok}/{len(urls)} · {secs:.0f} с")
        for f in facts[:4]:
            print("      - " + str(f.get("text"))[:170])


NICHES = NICHES[:1]
for niche, ex in NICHES:
    print(f"\n=== Досье: {niche}")
    user = "Niche: %s\nFunded companies in this niche: %s" % (niche, "; ".join(ex))
    for name, fn in ENGINES:
        print("  " + name)
        t = time.time()
        d, e = fn(ai.WEB_SYSTEM, user)
        report("dossier", d, e, time.time() - t)

for q in QUESTIONS:
    print(f"\n=== Вопрос: {q}")
    for name, fn in ENGINES:
        print("  " + name)
        t = time.time()
        d, e = fn(FACTS_SYSTEM, f"Question: {q}")
        report("facts", d, e, time.time() - t)
print("\nтокены OpenRouter:", ai.USAGE)
