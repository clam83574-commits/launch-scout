"""Автокомментарии Скаута в Threads и X: ответы под постами стартап-аудитории.

Зачем: новому аккаунту охват даёт не частота постов, а то, с кем он общается.
Полезный ответ с цифрами под постом фаундера видят его читатели, а алгоритм
начинает показывать посты Скаута той же аудитории.

Один заход (engage.yml, 5 раз в день) — по одному ответу в каждой сети:
1. кандидаты: Threads — поиск в веб-версии с куки Скаута; X — через парсер
   (sources/x.py, аккаунт X_AUTH_TOKEN / X_CT0): поиск и ленты стартап-аккаунтов
   из x_account_ids.json (веб-поиск X из Actions упирается в проверку «вы не бот»);
2. модель выбирает 1–3 поста, где Скауту есть что сказать по сути;
3. под выбранный пост Worker достаёт факты из нашей базы (POST /th/factsfor —
   тот же поиск по нишам, что в чате бота: раунды, инвесторы, конкуренты, жалобы);
4. модель пишет ответ только с этими фактами; отдельный проверяющий вызов ставит
   оценку релевантности, ниже 8 из 10 — не публикуем (лучше пропустить заход);
5. ответ от аккаунта Скаута, подписка на автора, отчёт Worker'у (POST /th/engaged).
Лимит в сутки, пауза и кому уже отвечали — у Worker'а (GET /th/engage).

Тексты постов и ответов в лог не пишутся — логи публичного репозитория видны всем.

Запуск:  python social_engage.py            (по одному ответу в Threads и X)
Проверка: python social_engage.py --dry      (подбор и тексты владельцу, без публикации)
"""
import argparse
import calendar
import json
import os
import pathlib
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
MIN_PICK = 7      # пост подходит
MIN_SCORE = 7     # ответ по сути этого поста (проверяющий вызов; выдуманные числа — не выше 3)

# Запросы поиска: живые разговоры фаундеров, а не новости. Каждый заход — несколько случайных.
QUERIES = {
    # Фразы, которые пишут только те, кто сам строит бизнес: «стартап» в поиске ловит что угодно.
    "th": ["мой стартап", "наш стартап", "мы запустили", "ищу кофаундера", "первые платящие клиенты", "MRR",
           "юнит-экономика", "привлекли инвестиции", "кастдев", "product market fit", "pre-seed раунд",
           "резидент Astana Hub", "пилю SaaS", "запустил MVP", "фаундер стартапа", "build in public"],
    "x": ["\"мой стартап\" -filter:replies", "\"мы запустили\" lang:ru -filter:replies", "\"ищу кофаундера\"",
          "MRR lang:ru -filter:replies", "\"привлекли инвестиции\" -filter:replies", "Astana Hub", "startup Kazakhstan",
          "\"first paying customers\" -filter:replies min_faves:5", "\"pre-seed\" raised -filter:replies min_faves:10",
          "\"building in public\" MRR -filter:replies min_faves:10"],
}

SELECT_RULES = """Ты выбираешь, под какими постами Скауту стоит оставить ответ.

Строго: Скауту нужны фаундеры, а не все подряд. 8–10 — только если АВТОР САМ строит стартап/продукт/бизнес
(«мы запустили», «наш MRR», «ищу кофаундера», «привлекли раунд», кастдев, первые клиенты) или инвестирует в стартапы.
Посты «вообще» про ИИ, деньги, работу, мотивацию, карьеру, учёбу, новости — не выше 4, даже если там есть слово «стартап».

★ ПЛОЩАДКА — пост медийного фаундера, акселератора, фонда или стартап-медиа: под ним собирается аудитория фаундеров,
поэтому такие посты в приоритете (мнение о рынке, разбор, анонс, вопрос подписчикам — подходит), если на него можно
ответить по существу с данными о рынке. Анонсы мероприятий и чистая реклама площадки — не выше 5.

ПОДХОДИТ (score 0–10)
+ автор сам делает стартап, продукт, бизнес или выбирает идею для своего стартапа; спрашивает совета; делится цифрами, болью, запуском;
+ тема: стартапы, инвестиции, рынок, ниши, ИИ-продукты, SaaS, e-commerce, бизнес в Казахстане и СНГ;
+ на пост можно ответить по сути с данными о рынке (раунды, ниши, конкуренты, спрос);
+ свежий (часы, не дни), живое обсуждение, но не тысячи ответов, где ответ утонет.
− новости и перепосты без мнения автора, реклама, курсы, розыгрыши, крипто-сигналы, вакансии;
− политика, религия, трагедии, личные драмы, споры с переходом на личности;
− сам Скаут (launch_scout, Launc_Scout);
− пост, на который можно ответить только общими словами, — ниже 6.

Ответь только JSON: {"picks": [{"i": номер поста, "score": 0-10, "why": "одна фраза"}]} — 1–3 лучших, по убыванию score."""

REPLY_RULES = """Ты пишешь ответ от имени Скаута под чужим постом.

- Отвечай на мысль ЭТОГО поста: автор должен понять, что ты прочитал именно его, а не тему вообще.
- Опирайся на FACTS — это данные из нашей базы по теме поста (раунды, ниши, инвесторы, конкуренты, жалобы клиентов). Возьми 1–2 факта, которые правда отвечают на мысль поста, и скажи, что из них следует для автора. Числа и названия — ровно как в FACTS, ничего не выдумывай и не округляй по-своему. Если в FACTS нет ничего про тему поста — ответь без цифр: точное наблюдение или вопрос.
- Факт годится, только если он про ТО ЖЕ, о чём пост (та же ниша, тот же тип продукта, та же проблема). Компания из соседней ниши «для примера» — хуже, чем ответ без цифр: читатель увидит натяжку.
- 1–3 предложения, до 260 знаков. На языке поста (английский пост — по-английски).
- Без рекламы: не называй бота и сервис, без ссылок, без «подписывайся». Максимум — очень аккуратно и не всегда: «смотрю раунды каждый день — в этой нише за полгода …».
- Без лести и пустоты: никаких «отличный пост», «полностью согласен», «интересная мысль».
- Без хэштегов, эмодзи максимум одно. Не притворяйся человеком, но и не объявляй, что ты ИИ.

Ответь только текстом ответа — без кавычек, пояснений, вариантов и JSON."""

CHECK_RULES = """Ты строгий редактор. Тебе дают чужой пост, данные FACTS и ответ на пост от аккаунта Скаута.
Оцени ответ по шкале 0–10:
- 10: отвечает именно на мысль этого поста, добавляет пользу (факт из FACTS, наблюдение, точный вопрос), звучит как живой умный собеседник;
- 5: по теме, но мог бы стоять под любым похожим постом;
- 0: мимо поста, реклама, лесть, вода, неуместно (трагедия, политика).
Если в ответе есть число или название компании, которых нет в FACTS, — не больше 3.
Ответь только JSON: {"score": 0-10, "why": "одна фраза"}"""


def llm(system, user, tries=2):
    """JSON-ответ модели; не JSON — ещё одна попытка."""
    for k in range(tries):
        try:
            return _llm(system, user)
        except (ValueError, json.JSONDecodeError):
            if k == tries - 1:
                raise


def llm_text(system, user):
    """Ответ модели обычным текстом (сам комментарий): без обёрток, кавычек и подписи."""
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=120, headers={
        "authorization": f"Bearer {OR_KEY}", "content-type": "application/json",
        "HTTP-Referer": WORKER, "X-Title": "launch-scout-engage"},
        json={"model": MODEL, "max_tokens": 1500, "temperature": 0.6,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    t = (r.json()["choices"][0]["message"]["content"] or "").strip()
    try:   # всё же прислала JSON — достаём поле
        j = json.loads(re.search(r"\{[\s\S]*\}", t).group(0))
        t = j.get("reply") or j.get("text") or t
    except Exception:
        pass
    return t.strip().strip('"«»').strip()


def _llm(system, user):
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


# ---- Кандидаты: Threads (веб) --------------------------------------------------

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
    const ts = Date.parse(t.getAttribute('datetime')) / 1000;
    out.push({ url: href, author: m ? m[1] : '', ts, text: (box.innerText || '').slice(0, 900) });
  }
  return out;
}"""


def hubs():
    """Площадки — аккаунты, где собирается стартап-аудитория: threads/hubs.json + найденные сами (у Worker'а)."""
    base = json.loads((pathlib.Path(__file__).parent / "threads" / "hubs.json").read_text(encoding="utf-8"))
    return base


def threads_candidates(page, n, hub_list):
    found = {}
    # Сначала площадки: свежие посты с их профилей — там и аудитория, и ранний ответ заметен.
    for h in random.sample(hub_list, min(8, len(hub_list))):
        page.goto(f"https://www.threads.com/@{h}", wait_until="domcontentloaded")
        pause(3, 5)
        if need_login(page):
            raise Fail("куки протухли — обновите секрет TH_COOKIES")
        dismiss(page)
        got = [x for x in page.evaluate(COLLECT_TH) if x["url"].startswith(f"/@{h}/") or f"/@{h}/" in x["url"]]
        print("Threads площадка → постов:", len(got))
        for p in got:
            p["url"] = absolute("https://www.threads.com", p["url"])
            p["hub"] = True
            found.setdefault(p["url"], p)
        pause(2, 4)
    for k, q in enumerate(random.sample(QUERIES["th"], n), 1):
        page.goto(f"https://www.threads.com/search?q={requests.utils.quote(q)}&serp_type=default&filter=recent", wait_until="domcontentloaded")
        pause(3, 5)
        if need_login(page):
            raise Fail("куки протухли — обновите секрет TH_COOKIES")
        dismiss(page)
        for _ in range(2):
            page.mouse.wheel(0, 2500)
            pause(1.5, 2.5)
        got = page.evaluate(COLLECT_TH)
        print("Threads запрос", k, "→ постов:", len(got))
        for p in got:
            p["url"] = absolute("https://www.threads.com", p["url"])
            found.setdefault(p["url"], p)
        pause(2, 4)
    return list(found.values())


# ---- Кандидаты: X (через парсер) -----------------------------------------------

def x_candidates(n, hub_list):
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from sources import x as xs
    sess, err = xs.session_from_env()
    if not sess:
        raise Fail("нет куки парсера X (X_AUTH_TOKEN / X_CT0)")
    raw = []
    for k, q in enumerate(random.sample(QUERIES["x"], n), 1):
        got, err = sess.search(q, limit=30, product="Latest")
        print("X запрос", k, "→ постов:", len(got or []), "· ошибка" if err else "")
        raw += got or []
    ids = json.loads((pathlib.Path(__file__).parent / "x_account_ids.json").read_text(encoding="utf-8"))
    names = list(dict.fromkeys([*hub_list, *ids]))
    hub_set = {x.lower() for x in names}
    for name in random.sample(names, min(7, len(names))):
        uid = ids.get(name)
        if not uid:
            uid, err = sess.user_id(name)
            if not uid:
                print("X площадка не найдена")
                continue
        got, err = sess.user_tweets(uid, limit=15)
        print("X площадка → постов:", len(got or []), "· ошибка" if err else "")
        raw += got or []
    out = {}
    for t in raw:
        if t.get("is_retweet") or not t.get("screen_name"):
            continue
        txt = t.get("text") or ""
        if txt.startswith("@"):   # ответ кому-то, а не свой пост
            continue
        url = f"https://x.com/{t['screen_name']}/status/{t['id']}"
        stats = f"{t.get('likes') or 0} лайков, {t.get('replies') or 0} ответов, {t.get('views') or '?'} просмотров, {t.get('followers') or '?'} подписчиков"
        out[url] = {"url": url, "author": t["screen_name"], "ts": xs._parse_twitter_time(t.get("created_at")) or 0,
                    "text": txt[:700], "stats": stats, "replies": t.get("replies") or 0, "hub": t["screen_name"].lower() in hub_set}
    return list(out.values())


def fresh(posts, recent, me):
    """Только свежие (до 36 ч), не свои, не те, кому уже отвечали, не утонувшие в ответах."""
    done_urls = {r["url"] for r in recent}
    done_auth = {(r.get("author") or "").lower() for r in recent}
    now = time.time()
    out = []
    for p in posts:
        a = (p.get("author") or "").lower()
        if not a or a == me.lower() or a in done_auth or p["url"] in done_urls or len(p.get("text") or "") < 40:
            continue
        if not p.get("ts") or now - p["ts"] > (24 if p.get("hub") else 36) * 3600 or (p.get("replies") or 0) > 400:
            continue
        p["age_h"] = round((now - p["ts"]) / 3600, 1)
        out.append(p)
    random.shuffle(out)
    out.sort(key=lambda p: not p.get("hub"))   # площадки первыми
    return out[:25]


def choose(net, cands, cfg, page=None):
    """Лучший пост и ответ на основе нашей базы, прошедший проверку; или None."""
    listing = "\n\n".join(f"[{i}] {'★ ПЛОЩАДКА · ' if p.get('hub') else ''}@{p['author']} · {p['age_h']} ч назад{(' · ' + p['stats']) if p.get('stats') else ''}\n{p['text']}" for i, p in enumerate(cands))
    sel = llm(SELECT_RULES, f"СЕТЬ: {'Threads' if net == 'th' else 'X'}\n\nПОСТЫ:\n{listing}")
    picks = sorted(sel.get("picks") or [], key=lambda x: -(x.get("score") or 0))
    print(net, "оценки выбора:", [pk.get("score") for pk in picks])
    for pk in picks:
        i = pk.get("i")
        if not isinstance(i, int) or not (0 <= i < len(cands)) or (pk.get("score") or 0) < MIN_PICK:
            continue
        post = cands[i]
        if net == "th" and page is not None and not post.get("hub") and not builder(page, post):
            continue
        fr = requests.post(f"{WORKER}/th/factsfor", headers=H, json={"text": post["text"]}, timeout=90).json()
        facts = fr.get("facts") or []
        print(net, "фактов из базы:", len(facts), "· ниш:", len(fr.get("niches") or []))
        ftxt = "\n".join(facts) or "(в базе ничего по теме поста — отвечай без цифр)"
        reply = llm_text(cfg["persona"] + "\n\n" + REPLY_RULES, f"FACTS (наша база по теме поста):\n{ftxt}\n\nПОСТ (@{post['author']}):\n{post['text']}")
        reply = re.sub(r"https?://\S+", "", reply).strip()
        if not reply or len(reply) > 280:
            print(net, "ответ пустой или длиннее 280 знаков:", len(reply))
            continue
        # Проверка; не прошёл — одна доработка по замечанию редактора и повторная проверка.
        for attempt in range(2):
            chk = llm(CHECK_RULES, f"FACTS:\n{ftxt}\n\nПОСТ (@{post['author']}):\n{post['text']}\n\nОТВЕТ СКАУТА:\n{reply}")
            if (chk.get("score") or 0) >= MIN_SCORE:
                return {**post, "reply": reply, "score": chk.get("score"), "n_facts": len(facts)}
            print(net, "ответ не прошёл проверку:", chk.get("score"), "·", str(chk.get("why") or "")[:140])
            if attempt == 1:
                break
            reply = llm_text(cfg["persona"] + "\n\n" + REPLY_RULES,
                             f"FACTS (наша база по теме поста):\n{ftxt}\n\nПОСТ (@{post['author']}):\n{post['text']}\n\n"
                             f"ТВОЙ ПРОШЛЫЙ ОТВЕТ:\n{reply}\n\nЗАМЕЧАНИЕ РЕДАКТОРА: {chk.get('why')}\nПерепиши ответ с учётом замечания.")
            reply = re.sub(r"https?://\S+", "", reply).strip()
            if not reply or len(reply) > 280:
                break
    return None


DISCOVERED = []

BIO_RULES = """По описанию профиля в Threads реши: автор строит стартап, продукт или бизнес (фаундер, CEO, со-основатель,
продакт, разработчик своего продукта, инди-хакер) или инвестирует в стартапы (VC, бизнес-ангел, акселератор)?
Блогеры «про деньги», коучи, курсы, HR, вакансии, личные блоги — нет.
Ответь только JSON: {"builder": true|false, "why": "одна фраза"}"""


def builder(page, post):
    """Автор — фаундер или инвестор? Смотрим шапку его профиля."""
    try:
        page.goto(f"https://www.threads.com/@{post['author']}", wait_until="domcontentloaded")
        pause(2, 3.5)
        head = re.sub(r"\s+", " ", page.locator("main, body").first.inner_text()[:700])
        v = llm(BIO_RULES, f"ПРОФИЛЬ @{post['author']}:\n{head}\n\nЕГО ПОСТ:\n{post['text'][:400]}")
        ok = bool(v.get("builder"))
        print("th автор подходит:", ok)
        # Фаундер с большой аудиторией — в площадки: под ним стоит отвечать и дальше.
        m = re.search(r"([\d.,]+)\s*(тыс\.?|K|M|млн)?\s*(подписчик|followers)", head, re.I)
        if ok and m:
            n = float(m.group(1).replace(",", "."))
            n *= {"тыс": 1e3, "тыс.": 1e3, "k": 1e3, "m": 1e6, "млн": 1e6}.get((m.group(2) or "").lower(), 1)
            if n >= 3000:
                DISCOVERED.append({"net": "th", "handle": post["author"]})
        return ok
    except Exception as e:
        print("th профиль не прочитан:", type(e).__name__)
        return False


# ---- Ответ и подписка ----------------------------------------------------------

def reply_threads(page, post, dry):
    page.goto(post["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    dismiss(page)
    # Иконка ответа без aria-label — подпись во вложенном <title> («Комментировать»); первая — у самого поста.
    # Клик принимает обёртка role=button, а не сама иконка.
    clicked = False
    for _ in range(8):
        clicked = page.evaluate("""() => {
          const t = [...document.querySelectorAll('svg title, svg[aria-label]')].find((x) =>
            /комментир|ответить|reply|comment/i.test(x.textContent || x.getAttribute('aria-label') || ''));
          if (!t) return false;
          const svg = t.tagName.toLowerCase() === 'svg' ? t : t.closest('svg');
          (svg.closest('[role=button]') || svg.parentElement).click();
          return true;
        }""")
        if clicked:
            break
        pause(1.5, 2)
    if not clicked:
        names = page.evaluate("() => [...new Set([...document.querySelectorAll('svg title, svg[aria-label]')].map((x) => x.textContent || x.getAttribute('aria-label')))].slice(0, 20)")
        raise Fail("Threads: нет кнопки ответа, иконки: " + ", ".join(str(n) for n in names) + " · адрес " + page.url.split("?")[0][-60:])
    pause(1.5, 2.5)
    box = page.locator("[contenteditable=true]:visible").last
    box.wait_for(timeout=15000)
    if dry:
        return
    box.click()
    page.keyboard.insert_text(post["reply"])
    pause(1, 2)
    page.locator("div[role=button], button").filter(has_text=re.compile("^(Post|Опубликовать)$")).last.click()
    pause(4, 6)


def reply_x(page, post, dry):
    page.goto(post["url"], wait_until="domcontentloaded")
    pause(2.5, 4)
    box = page.locator("[data-testid='tweetTextarea_0']").first
    try:
        box.wait_for(timeout=20000)
    except PwTimeout:
        body = re.sub(r"\s+", " ", page.locator("body").inner_text()[:160])
        raise Fail("страница поста X не открылась: " + body)
    if dry:
        return
    box.click()
    page.keyboard.insert_text(post["reply"])
    pause(1, 2)
    page.locator("[data-testid='tweetButtonInline']").first.click()
    pause(4, 6)


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
    hub_base = hubs()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for net in nets[:max(left, 1) if not a.dry else 2]:
            ctx = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                ctx.add_cookies(cookies_for(net))
                me = os.environ.get(SITES[net]["user"], "")
                hl = list(dict.fromkeys([*hub_base.get(net, []), *((cfg.get("hubs") or {}).get(net) or [])]))
                raw = threads_candidates(page, 2, hl) if net == "th" else x_candidates(2, hl)
                cands = fresh(raw, cfg.get("recent") or [], me)
                print(SITES[net]["name"], "найдено:", len(raw), "· свежих кандидатов:", len(cands))
                pick = choose(net, cands, cfg, page) if cands else None
                if not pick:
                    print(SITES[net]["name"], "подходящего поста нет — пропуск")
                    continue
                (reply_threads if net == "th" else reply_x)(page, pick, a.dry)
                if not a.dry:
                    pick["followed"] = follow(page, net, pick["author"])
                items.append({"net": net, "url": pick["url"], "author": pick["author"], "post_text": pick["text"][:600],
                              "reply": pick["reply"], "score": pick["score"], "followed": pick.get("followed", False)})
                print(SITES[net]["name"], "ответ", "подобран" if a.dry else "опубликован", "· оценка", pick["score"], "· фактов", pick["n_facts"])
            except Exception as e:
                why = str(e) if isinstance(e, Fail) else f"{type(e).__name__}: {str(e).splitlines()[0][:150]}"
                errors.append(f"{SITES[net]['name']}: {why}")
                print(SITES[net]["name"], "ошибка:", type(e).__name__, (str(e).splitlines() or [""])[0][:200])
            finally:
                ctx.close()
            pause(20, 60)
        browser.close()
    requests.post(f"{WORKER}/th/engaged", headers=H, json={"items": items, "errors": errors, "dry": a.dry, "discovered": DISCOVERED}, timeout=60)


if __name__ == "__main__":
    main()
