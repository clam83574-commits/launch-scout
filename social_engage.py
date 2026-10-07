"""Автокомментарии Скаута в Threads и X: ответы под постами стартап-аудитории.

Зачем: новому аккаунту охват даёт не частота постов, а то, с кем он общается.
Полезный ответ с цифрами под постом фаундера видят его читатели, а алгоритм
начинает показывать посты Скаута той же аудитории.

Один заход (engage.yml, 5 раз в день) — по одному ответу в каждой сети:
1. поиск свежих постов по стартап-запросам (веб-версия с куки, как в social_publish.py);
2. модель выбирает пост, где Скаут может сказать что-то по делу, и пишет ответ;
3. отдельный проверяющий вызов ставит оценку релевантности; ниже 8 из 10 — не публикуем
   (лучше пропустить заход, чем ответить невпопад);
4. ответ, подписка на автора, отчёт Worker'у (POST /th/engaged → владельцу).
Лимит в сутки, пауза и кому уже отвечали — у Worker'а (GET /th/engage).

Тексты постов и ответов в лог не пишутся — логи публичного репозитория видны всем.

Запуск:  python social_engage.py            (по одному ответу в Threads и X)
Проверка: python social_engage.py --dry      (подбор и тексты владельцу, без публикации)
"""
import argparse
import calendar
import json
import os
import random
import re
import sys
import time

import requests
from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

from social_publish import H, SECRET, SITES, UA, WORKER, Fail, absolute, cookies_for, dismiss, need_login, pause

OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("LS_TH_MODEL", "anthropic/claude-sonnet-5.5")
MIN_SCORE = 8

# Запросы поиска: живые разговоры фаундеров, а не новости. Каждый заход — 3 случайных.
QUERIES = {
    "th": ["стартап", "фаундер", "MVP", "запустил продукт", "ищу инвестора", "бизнес идея", "Astana Hub", "венчур",
           "SaaS", "первые клиенты", "pre-seed", "стартап Казахстан", "питч", "акселератор", "свой продукт"],
    "x": ["стартап lang:ru", "фаундер lang:ru", "MVP lang:ru", "инвестиции стартап lang:ru", "Astana Hub",
          "startup Kazakhstan", "стартап Казахстан", "бизнес идея lang:ru", "SaaS lang:ru", "венчур lang:ru"],
}

PICK_RULES = """Ты выбираешь, под каким постом оставить ответ, и пишешь ответ от имени Скаута.

КАКОЙ ПОСТ ПОДХОДИТ (score 0–10)
+ автор сам делает стартап, продукт, бизнес или выбирает идею; спрашивает совета; делится цифрами, болью, запуском;
+ тема: стартапы, инвестиции, рынок, ниши, ИИ-продукты, SaaS, e-commerce, бизнес в Казахстане и СНГ;
+ Скауту есть что сказать ПО СУТИ ЭТОГО ПОСТА: цифра из FACTS, наблюдение о рынке, конкретный вопрос;
+ свежий (часы, не дни), живое обсуждение, но не тысячи ответов, где ответ утонет.
− новости и перепосты без мнения автора, реклама, курсы, розыгрыши, крипто-сигналы, вакансии;
− политика, религия, трагедии, личные драмы, споры с переходом на личности;
− сам Скаут (launch_scout, Launc_Scout) и авторы из списка «уже отвечали»;
− пост, на который можно ответить только общими словами — ставь ниже 6.

КАК ОТВЕЧАТЬ
- Отвечай на мысль ЭТОГО поста: человек должен понять, что ты прочитал именно его, а не тему вообще.
- 1–3 предложения, до 260 знаков. На языке поста.
- Цифры можно и нужно, но ТОЛЬКО из FACTS и только если они правда про тему поста. Нет подходящей — без цифр: наблюдение, опыт рынка, точный вопрос.
- Без рекламы: не называй бота, без ссылок, без «подписывайся», без «у нас есть сервис». Максимум — очень аккуратно, не чаще чем в одном ответе из пяти: «смотрю раунды каждый день — в этой нише за месяц …».
- Без лести и пустоты: никаких «отличный пост», «полностью согласен», «интересная мысль».
- Без хэштегов и эмодзи (максимум одно). Не притворяйся человеком, но и не объявляй, что ты ИИ.

Ответь только JSON: {"picks": [{"i": номер поста, "score": 0-10, "why": "почему подходит, 1 фраза", "reply": "текст ответа"}]} — 1–3 лучших, по убыванию score."""

CHECK_RULES = """Ты строгий редактор. Тебе дают чужой пост и ответ на него от аккаунта Скаута.
Оцени ответ по шкале 0–10:
- 10: отвечает именно на мысль этого поста, добавляет пользу (факт, наблюдение, точный вопрос), звучит как живой умный собеседник;
- 5: по теме, но мог бы стоять под любым похожим постом;
- 0: мимо поста, реклама, лесть, вода, выдуманные цифры, неуместно (трагедия, политика).
Если в ответе есть число, которого нет в FACTS, — не больше 3.
Ответь только JSON: {"score": 0-10, "why": "одна фраза"}"""


def llm(system, user):
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=120, headers={
        "authorization": f"Bearer {OR_KEY}", "content-type": "application/json",
        "HTTP-Referer": WORKER, "X-Title": "launch-scout-engage"},
        json={"model": MODEL, "max_tokens": 2000, "temperature": 0.6,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    t = (r.json()["choices"][0]["message"]["content"] or "").replace("```json", "").replace("```", "")
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        raise ValueError("модель ответила не JSON")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))


# ---- Поиск постов --------------------------------------------------------------

COLLECT_TH = """() => {
  const out = [], seen = new Set();
  for (const t of document.querySelectorAll('time[datetime]')) {
    const a = t.closest('a[href*="/post/"]');
    if (!a) continue;
    const href = a.getAttribute('href').split('?')[0];
    if (seen.has(href)) continue;
    seen.add(href);
    let box = a;
    for (let k = 0; k < 8 && box.parentElement; k++) { box = box.parentElement; if (box.getAttribute('data-pressable-container')) break; }
    const m = href.match(/\\/@([^/]+)\\/post\\//);
    out.push({ url: href, author: m ? m[1] : '', time: t.getAttribute('datetime'), text: (box.innerText || '').slice(0, 900) });
  }
  return out;
}"""

COLLECT_X = """() => [...document.querySelectorAll('article[data-testid="tweet"]')].map((ar) => {
  const t = ar.querySelector('time');
  const a = t && t.closest('a[href*="/status/"]');
  if (!a) return null;
  const href = a.getAttribute('href').split('?')[0];
  return { url: href, author: href.split('/')[1] || '', time: t.getAttribute('datetime'),
    text: ((ar.querySelector('[data-testid="tweetText"]') || {}).innerText || '').slice(0, 700),
    stats: ar.querySelector('[role="group"]') ? ar.querySelector('[role="group"]').getAttribute('aria-label') || '' : '' };
}).filter(Boolean)"""


def search(page, net, queries):
    found = {}
    for q in queries:
        if net == "th":
            page.goto(f"https://www.threads.com/search?q={requests.utils.quote(q)}&serp_type=default&filter=recent", wait_until="domcontentloaded")
        else:
            page.goto(f"https://x.com/search?q={requests.utils.quote(q)}&src=typed_query&f=live", wait_until="domcontentloaded")
        pause(3, 5)
        if need_login(page):
            raise Fail(f"куки протухли — обновите секрет {SITES[net]['env']}")
        dismiss(page)
        for _ in range(2):
            page.mouse.wheel(0, 2500)
            pause(1.5, 2.5)
        for p in page.evaluate(COLLECT_TH if net == "th" else COLLECT_X):
            p["url"] = absolute("https://www.threads.com" if net == "th" else "https://x.com", p["url"])
            found.setdefault(p["url"], p)
        pause(2, 4)
    return list(found.values())


def fresh(posts, recent, me):
    """Только свежие (до 36 ч), не свои и не те, кому уже отвечали."""
    done_urls = {r["url"] for r in recent}
    done_auth = {(r.get("author") or "").lower() for r in recent}
    now = time.time()
    out = []
    for p in posts:
        a = (p.get("author") or "").lower()
        if not a or a == me.lower() or a in done_auth or p["url"] in done_urls or len(p.get("text") or "") < 40:
            continue
        try:
            ts = calendar.timegm(time.strptime(p["time"][:19], "%Y-%m-%dT%H:%M:%S"))   # время сайта — UTC
        except Exception:
            ts = now
        if now - ts > 36 * 3600:
            continue
        p["age_h"] = round((now - ts) / 3600, 1)
        out.append(p)
    return out[:25]


def choose(net, cands, cfg):
    """Лучший пост и ответ, прошедший проверку на релевантность; или None."""
    facts = "\n".join(cfg.get("facts") or []) or "(фактов нет — без цифр)"
    listing = "\n\n".join(f"[{i}] @{p['author']} · {p['age_h']} ч назад{(' · ' + p['stats']) if p.get('stats') else ''}\n{p['text']}" for i, p in enumerate(cands))
    res = llm(cfg["persona"] + "\n\n" + PICK_RULES,
              f"СЕТЬ: {'Threads' if net == 'th' else 'X'}\n\nFACTS (свежие данные Скаута):\n{facts}\n\nПОСТЫ:\n{listing}")
    for pk in sorted(res.get("picks") or [], key=lambda x: -(x.get("score") or 0)):
        i, reply = pk.get("i"), (pk.get("reply") or "").strip()
        if not isinstance(i, int) or not (0 <= i < len(cands)) or (pk.get("score") or 0) < MIN_SCORE or not reply:
            continue
        reply = re.sub(r"https?://\S+", "", reply).strip()
        if len(reply) > 280:
            continue
        post = cands[i]
        chk = llm(CHECK_RULES, f"FACTS:\n{facts}\n\nПОСТ (@{post['author']}):\n{post['text']}\n\nОТВЕТ СКАУТА:\n{reply}")
        if (chk.get("score") or 0) >= MIN_SCORE:
            return {**post, "reply": reply, "score": chk.get("score")}
        print(net, "ответ не прошёл проверку:", chk.get("score"))
    return None


# ---- Ответ и подписка ----------------------------------------------------------

def reply_threads(page, post):
    page.goto(post["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    dismiss(page)
    page.locator("svg[aria-label='Ответить'], svg[aria-label='Reply']").first.click(timeout=15000)
    pause(1.5, 2.5)
    box = page.locator("[contenteditable=true]:visible").last
    box.wait_for(timeout=15000)
    box.click()
    page.keyboard.insert_text(post["reply"])
    pause(1, 2)
    page.locator("div[role=button], button").filter(has_text=re.compile("^(Post|Опубликовать)$")).last.click()
    pause(4, 6)
    return post["url"]


def reply_x(page, post):
    page.goto(post["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    box = page.locator("[data-testid='tweetTextarea_0']").first
    box.wait_for(timeout=20000)
    box.click()
    page.keyboard.insert_text(post["reply"])
    pause(1, 2)
    page.locator("[data-testid='tweetButtonInline']").first.click()
    pause(4, 6)
    return post["url"]


def follow(page, net, author):
    try:
        if net == "th":
            page.goto(f"https://www.threads.com/@{author}", wait_until="domcontentloaded")
            pause(2, 3)
            b = page.locator("div[role=button], button").filter(has_text=re.compile("^(Подписаться|Follow)$")).first
        else:
            page.goto(f"https://x.com/{author}", wait_until="domcontentloaded")
            pause(2, 3)
            b = page.locator("[data-testid$='-follow']").first
        if b.is_visible(timeout=6000):
            b.click()
            pause(1, 2)
            return True
    except Exception as e:
        print(net, "подписка не вышла:", type(e).__name__)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--nets", default="")
    a = ap.parse_args()
    if not (SECRET and OR_KEY):
        sys.exit("нужны LS_INGEST_SECRET и OPENROUTER_API_KEY")
    cfg = requests.get(f"{WORKER}/th/engage", headers=H, timeout=60).json()
    if cfg.get("off") and not a.dry:
        print("автокомментарии на паузе")
        return
    left = cfg.get("limit", 10) - cfg.get("today", 0)
    if left <= 0 and not a.dry:
        print("дневной лимит исчерпан")
        return
    nets = [n for n in (a.nets.split(",") if a.nets else cfg.get("nets") or []) if n in ("th", "x")]
    random.shuffle(nets)
    if not a.dry:
        time.sleep(random.uniform(0, 600))   # не ровно по расписанию
    items, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for net in nets[:max(left, 1) if not a.dry else 2]:
            ctx = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                ctx.add_cookies(cookies_for(net))
                me = os.environ.get(SITES[net]["user"], "")
                cands = fresh(search(page, net, random.sample(QUERIES[net], 3)), cfg.get("recent") or [], me)
                print(SITES[net]["name"], "кандидатов:", len(cands))
                pick = choose(net, cands, cfg) if cands else None
                if not pick:
                    print(SITES[net]["name"], "подходящего поста нет — пропуск")
                    continue
                if not a.dry:
                    (reply_threads if net == "th" else reply_x)(page, pick)
                    pick["followed"] = follow(page, net, pick["author"])
                items.append({"net": net, "url": pick["url"], "author": pick["author"], "post_text": pick["text"][:600],
                              "reply": pick["reply"], "score": pick["score"], "followed": pick.get("followed", False)})
                print(SITES[net]["name"], "ответ", "подобран" if a.dry else "опубликован", "· оценка", pick["score"])
            except Exception as e:
                why = str(e) if isinstance(e, Fail) else f"{type(e).__name__}: {str(e).splitlines()[0][:150]}"
                errors.append(f"{SITES[net]['name']}: {why}")
                print(SITES[net]["name"], "ошибка:", type(e).__name__)
            finally:
                ctx.close()
            pause(20, 60)
        browser.close()
    requests.post(f"{WORKER}/th/engaged", headers=H, json={"items": items, "errors": errors, "dry": a.dry}, timeout=60)


if __name__ == "__main__":
    main()
