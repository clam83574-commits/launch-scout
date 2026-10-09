"""Директ, комментарии под постами Скаута и ответы на его комментарии — во всех сетях.

Дважды в день (inbox.yml запускает Worker в 11:00 и 20:00 по Астане):
- X: ответы и упоминания Скаута — поиском через аккаунт парсера (sources/x.py:
  веб-поиск X из Actions упирается в проверку «вы не бот»); отвечает аккаунт Скаута.
  Личные сообщения X зашифрованы и требуют отдельного кода — их не трогаем.
- Threads: страница активности — ответы на посты и комментарии Скаута, упоминания.
- Instagram: комментарии под последними постами и непрочитанные диалоги в директе.

Каждое сообщение сначала разбирает Groq (бесплатно): ответить / переслать владельцу
(оплата, сотрудничество, жалобы, личное, неясное) / пропустить (спам, реакции без
вопроса, оскорбления, политика). Ответ пишет Gemini — по сути сообщения, цифры
только из нашей базы (POST /th/factsfor). Что разобрано — у Worker'а (th_inbox),
повторно не отвечаем. Сводка — владельцу в служебный бот.

Тексты сообщений в лог не пишутся — логи публичного репозитория видны всем.

Запуск:  python social_inbox.py         Проверка без отправки: python social_inbox.py --dry
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

from social_engage import COLLECT_TH, llm, llm_text, reply_threads, reply_x
from social_publish import H, SECRET, SITES, UA, WORKER, Fail, absolute, cookies_for, dismiss, need_login, pause

MAX_REPLIES = 12          # ответов за заход на все сети
FRESH_DAYS = 3            # старше — не отвечаем: разговор уже остыл

CLASSIFY = """You triage messages that people sent to "Скаут" — an AI analyst account about startups and venture money (Threads, Instagram, X).
For each item decide:
- "reply": a question or remark Скаут can answer helpfully (startups, niches, market, ideas, how the bot works) or thanks/praise (short warm reply).
- "forward": payments, refunds, partnership/ads/collab/press offers, complaints about the service, personal requests to the owner, anything risky or unclear — a human must answer.
- "skip": spam, bots, giveaways, emoji-only or one-word reactions with nothing to answer, insults, politics, or a direct-message thread where the LAST message is already from Скаут.
Also "needs_data": true if a good answer needs startup-market numbers (rounds, niches, competitors).
Reply JSON only: {"items": [{"i": 0, "action": "reply|forward|skip", "needs_data": false, "why": "short reason in Russian"}]}"""

REPLY = """Ты отвечаешь от имени Скаута на сообщение человека. Вид сообщения: %s.

- Отвечай по сути ЭТОГО сообщения: человек должен видеть, что его прочитали.
- Комментарий или ответ — 1–3 предложения, до 270 знаков. Директ — до 600 знаков, можно короткий список.
- Числа, компании, суммы — только из FACTS и только если они правда про тему сообщения; нет подходящих — без цифр.
- В директе, если человек спрашивает, где посмотреть или как попробовать, дай ссылку на бота: %s. В публичных ответах ссылок нет — максимум «бот по ссылке в профиле», и только если прямо спросили.
- Благодарность или похвала — тёплый короткий ответ и вопрос, что человек строит.
- На языке сообщения. Без лести, канцелярита и хэштегов. Не притворяйся человеком.

Ответь только текстом ответа."""


def key_of(*parts):
    return ":".join(parts)[:180]


def h(text):
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


# ---- Сбор: X (аккаунт парсера) ---------------------------------------------------

def x_items(me):
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from sources import x as xs
    sess, err = xs.session_from_env()
    if not sess:
        raise Fail("нет куки парсера X (X_AUTH_TOKEN / X_CT0)")
    out, now = {}, time.time()
    for kind, q in (("reply", f"to:{me}"), ("mention", f"@{me} -from:{me}")):
        got, err = sess.search(q, limit=40, product="Latest")
        print("X", kind, "→", len(got or []), "· ошибка" if err else "")
        for t in got or []:
            ts = xs._parse_twitter_time(t.get("created_at")) or 0
            if t.get("is_retweet") or (t.get("screen_name") or "").lower() == me.lower() or now - ts > FRESH_DAYS * 86400:
                continue
            url = f"https://x.com/{t['screen_name']}/status/{t['id']}"
            out.setdefault(url, {"net": "x", "kind": kind, "key": key_of("x", t["id"]), "author": t["screen_name"], "url": url,
                                 "text": (t.get("text") or "")[:900]})
    return list(out.values())


# ---- Сбор: Threads (активность) ---------------------------------------------------

def threads_items(page, me):
    page.goto("https://www.threads.com/activity", wait_until="domcontentloaded")
    pause(3, 5)
    if need_login(page):
        raise Fail("куки протухли — обновите секрет TH_COOKIES")
    dismiss(page)
    for _ in range(2):
        page.mouse.wheel(0, 2500)
        pause(1.5, 2.5)
    got, now, out = page.evaluate(COLLECT_TH), time.time(), []
    print("Threads активность → постов:", len(got))
    for p in got:
        if not p.get("author") or p["author"].lower() == me.lower() or not p.get("ts") or now - p["ts"] > FRESH_DAYS * 86400:
            continue
        url = absolute("https://www.threads.com", p["url"])
        out.append({"net": "th", "kind": "reply", "key": key_of("th", url.rsplit("/", 1)[-1]), "author": p["author"], "url": url,
                    "text": (p.get("text") or "")[:900]})
    return out


# ---- Сбор: Instagram (комментарии и директ) ---------------------------------------

COLLECT_IG_COMMENTS = """(me) => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/c/"]')) {
    const m = (a.getAttribute('href') || '').match(/\\/c\\/(\\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    let box = a, author = '';
    for (let k = 0; k < 8 && box.parentElement; k++) {
      box = box.parentElement;
      const u = [...box.querySelectorAll('a[href]')].map((x) => x.getAttribute('href')).find((x) => /^\\/[\\w.]+\\/$/.test(x));
      if (u && (box.innerText || '').length > 15) { author = u.slice(1, -1); break; }
    }
    if (!author || author.toLowerCase() === me.toLowerCase()) continue;
    const t = a.querySelector('time');
    out.push({ id: m[1], author, ts: t ? Date.parse(t.getAttribute('datetime')) / 1000 : 0, text: (box.innerText || '').slice(0, 700) });
  }
  return out;
}"""


def ig_comment_items(page, me):
    page.goto(f"https://www.instagram.com/{me}/", wait_until="domcontentloaded")
    pause(3, 5)
    if need_login(page) or page.locator("input[name=username]").count():
        raise Fail("куки протухли — обновите секрет IG_COOKIES")
    dismiss(page)
    posts = []
    for a in page.locator("a[href*='/p/']").all()[:5]:
        href = (a.get_attribute("href") or "").split("?")[0]
        if href and href not in posts:
            posts.append(href)
    out, now = [], time.time()
    for href in posts:
        url = absolute("https://www.instagram.com", href)
        page.goto(url, wait_until="domcontentloaded")
        pause(2.5, 4)
        got = page.evaluate(COLLECT_IG_COMMENTS, me)
        print("Instagram пост → комментариев:", len(got))
        for c in got:
            if c.get("ts") and now - c["ts"] > FRESH_DAYS * 86400:
                continue
            out.append({"net": "ig", "kind": "comment", "key": key_of("ig", "c", c["id"]), "author": c["author"], "url": url,
                        "text": c["text"], "cid": c["id"]})
    return out


def ig_dm_items(page, our_replies):
    page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
    pause(3, 5)
    dismiss(page)
    links = []
    for a in page.locator("a[href*='/direct/t/']").all()[:6]:
        href = (a.get_attribute("href") or "").split("?")[0]
        if href and href not in links:
            links.append(href)
    print("Instagram директ → диалогов:", len(links))
    out = []
    for href in links:
        url = absolute("https://www.instagram.com", href)
        page.goto(url, wait_until="domcontentloaded")
        pause(3, 4.5)
        tail = re.sub(r"\s+", " ", page.locator("main, [role=main], body").first.inner_text())[-1500:]
        # Последнее в переписке — наш же ответ: собеседник ещё не написал ничего нового.
        if any(r[:60] and r[:60] in tail[-700:] for r in our_replies):
            continue
        tid = href.rstrip("/").rsplit("/", 1)[-1]
        out.append({"net": "ig", "kind": "dm", "key": key_of("ig", "dm", tid, h(tail[-300:])), "author": tid, "url": url, "text": tail})
    return out


# ---- Ответ --------------------------------------------------------------------------

def reply_ig_comment(page, it):
    page.goto(it["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    a = page.locator(f"a[href*='/c/{it['cid']}']").first
    a.wait_for(timeout=15000)
    box = a.locator("xpath=ancestor::*[.//*[normalize-space(text())='Ответить' or normalize-space(text())='Reply']][1]")
    box.locator("xpath=.//*[normalize-space(text())='Ответить' or normalize-space(text())='Reply']").first.click(timeout=10000)
    pause(1, 2)
    ta = page.locator("textarea").first
    ta.wait_for(timeout=10000)
    ta.click()
    page.keyboard.press("End")
    page.keyboard.insert_text(" " + it["reply"])
    pause(1, 2)
    page.keyboard.press("Enter")
    pause(3, 5)


def reply_ig_dm(page, it):
    page.goto(it["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    box = page.locator("div[role=textbox][contenteditable=true], textarea").last
    box.wait_for(timeout=15000)
    box.click()
    page.keyboard.insert_text(it["reply"])
    pause(1, 2)
    page.keyboard.press("Enter")
    pause(3, 5)


def facts_for(text):
    try:
        return requests.post(f"{WORKER}/th/factsfor", headers=H, json={"text": text[:1200]}, timeout=90).json().get("facts") or []
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if not SECRET:
        sys.exit("нужен LS_INGEST_SECRET")
    cfg = requests.get(f"{WORKER}/th/inbox", headers=H, timeout=60).json()
    if cfg.get("off") and not a.dry:
        print("на паузе")
        return
    seen, nets = set(cfg.get("seen") or []), cfg.get("nets") or []
    our_replies = cfg.get("our_dm") or []
    items, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for net in [n for n in ("x", "th", "ig") if n in nets]:
            ctx = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                me = os.environ.get(SITES[net]["user"], "").lstrip("@")
                if net == "x":
                    got = x_items(me)
                else:
                    ctx.add_cookies(cookies_for(net))
                    got = threads_items(page, me) if net == "th" else ig_comment_items(page, me) + ig_dm_items(page, our_replies)
                new = [g for g in got if g["key"] not in seen]
                print(SITES[net]["name"], "найдено:", len(got), "· новых:", len(new))
                items += new[:12]   # за заход — не больше 12 новых на сеть: разбор Groq и лимиты сайтов
            except Exception as e:
                errors.append(f"{SITES[net]['name']}: {e if isinstance(e, Fail) else type(e).__name__ + ': ' + str(e).splitlines()[0][:150]}")
                print(SITES[net]["name"], "ошибка сбора:", type(e).__name__)
            finally:
                ctx.close()

        # Разбор пачкой (Groq), потом ответы.
        if items:
            listing = "\n\n".join(f"[{i}] {it['net']} {it['kind']} от @{it['author']}:\n{it['text'][:700]}" for i, it in enumerate(items))
            try:
                dec = {d.get("i"): d for d in (llm(CLASSIFY, listing).get("items") or []) if isinstance(d, dict)}
            except Exception as e:
                dec = {}
                errors.append("разбор: " + type(e).__name__)
            for i, it in enumerate(items):
                d = dec.get(i) or {"action": "forward", "why": "разбор не ответил"}
                it["action"], it["why"] = d.get("action") if d.get("action") in ("reply", "forward", "skip") else "forward", d.get("why", "")
                it["needs_data"] = bool(d.get("needs_data"))
        sent = 0
        ctxs = {}
        for it in items:
            if it["action"] != "reply":
                continue
            if sent >= MAX_REPLIES:
                it["action"], it["why"] = "forward", "лимит ответов за заход"
                continue
            facts = facts_for(it["text"]) if it.get("needs_data") else []
            kind = {"dm": "личное сообщение (директ)", "comment": "комментарий под постом Скаута", "reply": "ответ в ветке", "mention": "упоминание"}[it["kind"]]
            try:
                text = llm_text(cfg["persona"] + "\n\n" + REPLY % (kind, cfg.get("bot", "")),
                                f"FACTS (наша база):\n{chr(10).join(facts) or '(нет)'}\n\nСООБЩЕНИЕ от @{it['author']}:\n{it['text'][-1200:]}")
                text = text if it["kind"] == "dm" else re.sub(r"https?://\S+", "", text).strip()
                if not text or len(text) > (700 if it["kind"] == "dm" else 280):
                    it["action"], it["why"] = "forward", "ответ не получился"
                    continue
                it["reply"] = text
                if not a.dry:
                    net = it["net"]
                    if net not in ctxs:
                        c = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1366, "height": 900})
                        c.add_cookies(cookies_for(net))
                        ctxs[net] = c.new_page()
                    pg = ctxs[net]
                    if net == "x":
                        reply_x(pg, {"url": it["url"], "reply": text}, False)
                    elif net == "th":
                        reply_threads(pg, {"url": it["url"], "reply": text}, False)
                    elif it["kind"] == "dm":
                        reply_ig_dm(pg, it)
                    else:
                        reply_ig_comment(pg, it)
                    pause(20, 50)
                sent += 1
            except Exception as e:
                it["action"], it["why"] = "forward", "не отправилось: " + type(e).__name__
                print(it["net"], "ответ не отправлен:", type(e).__name__, (str(e).splitlines() or [""])[0][:200])
        browser.close()
    print("итог: ответов", sent, "· переслано", sum(1 for i in items if i["action"] == "forward"), "· пропущено", sum(1 for i in items if i["action"] == "skip"))
    payload = [{k: it.get(k) for k in ("net", "kind", "key", "author", "url", "text", "reply", "action", "why")} for it in items]
    requests.post(f"{WORKER}/th/inboxed", headers=H, json={"items": payload, "errors": errors, "dry": a.dry}, timeout=60)


if __name__ == "__main__":
    main()
