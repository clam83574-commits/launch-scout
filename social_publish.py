"""Публикация постов Скаута через веб-версии Threads, Instagram и X по куки.

Аккаунта разработчика Meta нет (верификация не проходит), а API X платный,
поэтому пост публикует Chromium в GitHub Actions как обычный пользователь:
открывает сайт с куки аккаунта Скаута, прикладывает слайды, вставляет текст,
жмёт «Опубликовать». Worker запускает это в слот (threads.yml, mode=publish)
и получает отчёт на POST /th/pub: ссылку на пост или причину со скриншотом.

Куки — секреты репозитория TH_COOKIES, IG_COOKIES, X_SCOUT_COOKIES: экспорт
расширения Cookie-Editor (JSON) или строка заголовка «name=value; name2=…».
X — отдельный аккаунт Скаута, не парсер (у того X_AUTH_TOKEN / X_CT0).
Имена профилей (для ссылки на пост, если сайт её не показал) — TH_USER,
IG_USER, X_USER, необязательно.

Текст поста в лог не пишется — логи публичного репозитория видны всем.

Запуск:  python social_publish.py --job 12 --nets th,ig,x
Проверка без публикации (всё, кроме последней кнопки, скриншот — владельцу):
         python social_publish.py --job 12 --nets ig --dry
Промо-видео (promo.yml): пост из файла, ролик по ссылке из него, без Worker'а —
ссылки на посты в лог, скриншоты в папку shots/:
         python social_publish.py --promo threads/promo_video_ru.json --nets th,ig,x --dry
"""
import argparse
import json
import os
import pathlib
import random
import re
import sys
import tempfile
import time

import requests
from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

WORKER = os.environ.get("WORKER_URL", "https://launch-scout-bot.clam83574.workers.dev")
SECRET = os.environ.get("LS_INGEST_SECRET", "")
H = {"x-ingest-secret": SECRET}

SITES = {
    "th": {"name": "Threads", "env": "TH_COOKIES", "domain": ".threads.com", "home": "https://www.threads.com/", "user": "TH_USER"},
    "ig": {"name": "Instagram", "env": "IG_COOKIES", "domain": ".instagram.com", "home": "https://www.instagram.com/", "user": "IG_USER"},
    "x": {"name": "X", "env": "X_SCOUT_COOKIES", "domain": ".x.com", "home": "https://x.com/home", "user": "X_USER"},
}
# Обычный десктопный Chrome, один и тот же в каждом запуске: сайты привыкают к «устройству».
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"


class Fail(Exception):
    """Публикация не удалась; текст — владельцу."""


def pause(a=0.6, b=1.6):
    time.sleep(random.uniform(a, b))


def cookies_for(net):
    raw = os.environ.get(SITES[net]["env"], "").strip()
    if not raw:
        raise Fail(f"нет секрета {SITES[net]['env']} в GitHub")
    dom = SITES[net]["domain"]
    out = []
    if raw.startswith("["):
        same = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
        for c in json.loads(raw):
            d = c.get("domain") or dom
            if not d.endswith(dom.lstrip(".")):
                continue
            ck = {"name": c["name"], "value": c["value"], "domain": d, "path": c.get("path") or "/",
                  "secure": bool(c.get("secure", True)), "httpOnly": bool(c.get("httpOnly", False))}
            ss = same.get(str(c.get("sameSite") or "").lower())
            if ss:
                ck["sameSite"] = ss
            if c.get("expirationDate"):
                ck["expires"] = int(c["expirationDate"])
            out.append(ck)
    else:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out.append({"name": k, "value": v, "domain": dom, "path": "/", "secure": True})
    if not out:
        raise Fail(f"в {SITES[net]['env']} нет куки для {dom}")
    return out


def btn(scope, *names):
    """Кнопка по тексту на любом из языков интерфейса."""
    rx = re.compile("^(" + "|".join(re.escape(n) for n in names) + ")$", re.I)
    return scope.get_by_role("button", name=rx).or_(scope.locator("div[role=button], button").filter(has_text=rx)).first


def dismiss(page):
    """Всплывашки «Включить уведомления», «Сохранить данные входа» и т. п."""
    for name in ("Not Now", "Не сейчас", "Not now", "Сейчас не надо"):
        try:
            b = page.get_by_role("button", name=name).first
            if b.is_visible(timeout=800):
                b.click()
                pause()
        except Exception:
            pass


def absolute(base, href):
    """Ссылка из href: сайт отдаёт то относительную, то полную."""
    href = (href or "").split("?")[0]
    return href if href.startswith("http") else base + href


def is_video(files):
    return any(f.lower().endswith((".mp4", ".mov")) for f in files)


def need_login(page):
    u = page.url
    return "/login" in u or "accounts/login" in u or "/i/flow/login" in u


# ---- Threads ---------------------------------------------------------------

def set_topic(page, topic):
    """Тема поста («Сообщество или тема»): по ней Threads показывает пост читателям темы.
    Не вышло — пост уходит без темы, это не ошибка."""
    if not topic:
        return
    try:
        page.get_by_text(re.compile("^(Сообщество или тема|Добавить тему|Add a topic|Community or topic)$")).last.click(timeout=5000)
        pause()
        # Фокус должен быть в поле темы, а не в тексте поста — иначе тема допишется в пост.
        if page.evaluate("() => !!document.activeElement && document.activeElement.isContentEditable"):
            print("тема не поставлена: фокус в тексте поста")
            return
        page.keyboard.insert_text(topic)
        pause(1.5, 2.5)
        opt = page.get_by_role("option").or_(page.locator("[role=listbox] [role=button], [role=menu] [role=menuitem]")).first
        if opt.is_visible(timeout=4000):
            opt.click()
        else:
            page.keyboard.press("Enter")
        pause()
    except Exception as e:
        print("тема не поставлена:", type(e).__name__)

def post_threads(page, job, files, dry):
    text = job["text"]
    if job.get("link") and not files:
        text = f"{text}\n\n{job['link']}"
    page.goto("https://www.threads.com/", wait_until="domcontentloaded")
    pause(2, 4)
    if need_login(page) or page.locator("a[href*='/login']").count() and not page.locator("[aria-label='Create'], [aria-label='Создать']").count():
        raise Fail("куки протухли — войдите в Threads заново и обновите секрет TH_COOKIES")
    dismiss(page)
    # Окно нового поста: кнопка «Создать» в боковой панели.
    page.locator("[aria-label='Create'], [aria-label='Создать'], [aria-label='New thread'], [aria-label='Новая ветка']").first.click()
    pause(1.5, 2.5)
    # Окно «Новая публикация» всплывает сбоку и не всегда помечено как dialog:
    # ищем по заголовку, поле ввода — последнее видимое (в ленте сверху своё «Что нового?»).
    head = page.get_by_text(re.compile("^(Новая публикация|New thread|Новая ветка)$")).last
    box = page.locator("[contenteditable=true]:visible, [role=textbox]:visible, textarea:visible").last
    try:
        box.wait_for(timeout=10000)
    except PwTimeout:
        page.get_by_text(re.compile("^(Что нового\\?|What's new\\?)$")).last.click()
        box.wait_for(timeout=10000)
    box.click()
    pause()
    page.keyboard.insert_text(text)
    pause()
    set_topic(page, job.get("topic"))
    # Контейнер окна — ближайший предок заголовка, где есть кнопка «Опубликовать».
    dlg = head.locator("xpath=ancestor::div[.//*[normalize-space(text())='Опубликовать' or normalize-space(text())='Post']][1]")
    if not dlg.count():
        dlg = page
    if files:
        inp = dlg.locator("input[type=file]")
        (inp.last if inp.count() else page.locator("input[type=file]").last).set_input_files(files)
        # Превью всех картинок (у ролика — <video>).
        if is_video(files):
            page.locator("video").first.wait_for(timeout=120000)
            pause(3, 5)
        else:
            page.locator("img[src^='blob:']").nth(len(files) - 1).wait_for(timeout=60000)
            pause(1, 2)
    post = page.locator("div[role=button], button").filter(has_text=re.compile("^(Post|Опубликовать)$")).last
    if dlg is not page:
        post = dlg.locator("div[role=button], button").filter(has_text=re.compile("^(Post|Опубликовать)$")).last
    if dry:
        return None
    post.click()
    # Тост «Опубликовано» со ссылкой «Посмотреть».
    try:
        a = page.locator("a[href*='/post/']").filter(has_text=re.compile("View|Посмотреть|Смотреть", re.I)).first
        a.wait_for(timeout=300000 if is_video(files) else 90000)   # ролик грузится после нажатия
        return absolute("https://www.threads.com", a.get_attribute("href"))
    except PwTimeout:
        return profile_last(page, "th")


# ---- Instagram -------------------------------------------------------------

def post_instagram(page, job, files, dry):
    if not files:
        raise Fail("в Instagram без картинки нельзя")
    # Последний пост в профиле до публикации: если Instagram не покажет «опубликовано»,
    # по нему поймём, вышел ли пост на самом деле.
    before = "" if dry else profile_last(page, "ig")
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    pause(2, 4)
    if need_login(page) or page.locator("input[name=username]").count():
        raise Fail("куки протухли — войдите в Instagram заново и обновите секрет IG_COOKIES")
    dismiss(page)
    page.locator("svg[aria-label='New post'], svg[aria-label='Новая публикация'], svg[aria-label='Create'], svg[aria-label='Создать']").first.click()
    pause()
    # В новых версиях «Создать» открывает меню: «Публикация» / «Post».
    try:
        sub = page.get_by_role("link", name=re.compile("^(Post|Публикация)$", re.I)).or_(page.locator("a, div[role=button], span").filter(has_text=re.compile("^(Post|Публикация)$"))).first
        if sub.is_visible(timeout=2000):
            sub.click()
            pause()
    except Exception:
        pass
    dlg = page.get_by_role("dialog").last
    dlg.locator("input[type=file]").first.set_input_files(files)
    pause(2, 3)
    video = is_video(files)
    if video:
        # «Видеопубликации теперь публикуются как Reels» — подтверждаем.
        try:
            ok = btn(page, "OK", "ОК")
            if ok.is_visible(timeout=8000):
                ok.click()
                pause()
        except Exception:
            pass
        dlg = page.get_by_role("dialog").last
    # Слайды 3:4 — без обрезки до квадрата: «Оригинал». Без него Instagram режет
    # карусель в квадрат, а сетка профиля — ещё и края (2026-10-09: текст обрезан).
    # Подпись кнопки — в aria-label или во вложенном <title>, ищем по смыслу.
    crop = page.evaluate("""() => {
      const t = [...document.querySelectorAll('[role=dialog] svg[aria-label], [role=dialog] svg title')].find((x) =>
        /обрез|crop|размер/i.test(x.getAttribute && x.getAttribute('aria-label') || x.textContent || ''));
      if (!t) return false;
      const svg = t.tagName.toLowerCase() === 'svg' ? t : t.closest('svg');
      (svg.closest('[role=button], button') || svg.parentElement).click();
      return true;
    }""")
    pause()
    original = dlg.locator("div[role=button], button, span").filter(has_text=re.compile("^(Original|Оригинал)$")).first
    try:
        if not crop:
            raise PwTimeout("нет кнопки обрезки")
        original.click(timeout=6000)
        pause()
    except Exception:
        # Reels и так 9:16, как наш ролик, — без «Оригинала» ничего не обрежется.
        if not video:
            raise Fail("не удалось выбрать формат «Оригинал» — Instagram обрезал бы слайды в квадрат, пост в Instagram пропущен")
        print("Instagram: формат «Оригинал» не выбран, у Reels и так 9:16")
    for _ in range(2):   # обрезка → фильтры → подпись
        btn(dlg, "Next", "Далее").click(timeout=20000)
        pause(1.5, 2.5)
    cap = dlg.locator("div[contenteditable=true][role=textbox], div[aria-label*='caption' i], div[aria-label*='подпись' i]").first
    cap.click(timeout=20000)
    page.keyboard.insert_text(job["ig_text"])
    pause()
    share = btn(dlg, "Share", "Поделиться")
    if dry:
        return None
    share.click()
    try:
        # Карусель Instagram иногда грузит минутами (2026-10-09: 3 минут не хватило).
        page.get_by_text(re.compile("(post has been shared|reel has been shared|публикация опубликована|публикация размещена|вы поделились публикацией|reels опубликовано|reels размещено|вы поделились (видео )?reels)", re.I)).first.wait_for(timeout=360000)
    except PwTimeout:
        # Подтверждения нет — смотрим профиль во второй вкладке (первую не трогаем:
        # уход со страницы оборвал бы загрузку). Новый пост наверху — значит, вышел.
        chk = page.context.new_page()
        try:
            after = profile_last(chk, "ig")
        finally:
            chk.close()
        if after and after != before:
            return after
        raise Fail("Instagram не подтвердил публикацию за 6 минут, и в профиле нового поста нет")
    pause(2, 3)
    return profile_last(page, "ig")


# ---- X ---------------------------------------------------------------------

def post_x(page, job, files, dry):
    page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
    pause(2, 4)
    if need_login(page):
        raise Fail("куки протухли — войдите в X (аккаунт Скаута) и обновите секрет X_SCOUT_COOKIES")
    box = page.locator("[data-testid='tweetTextarea_0']").first
    box.wait_for(timeout=30000)
    box.click()
    # Ссылки режем (в обычных постах их нет по правилам), промо-пост может их оставить.
    text = job["x_text"].strip() if job.get("x_links") else re.sub(r"https?://\S+", "", job["x_text"]).strip()
    page.keyboard.insert_text(text)
    pause()
    if files:
        page.locator("input[data-testid='fileInput']").first.set_input_files(files[:4])
        if is_video(files):
            page.locator("[data-testid='attachments'] video").first.wait_for(timeout=120000)
        else:
            page.locator("[data-testid='attachments'] img").nth(min(len(files), 4) - 1).wait_for(timeout=60000)
        pause(1, 2)
    send = page.locator("[data-testid='tweetButton']").first
    if dry:
        if is_video(files):   # для проверки дождёмся, что X дообработал ролик и кнопка активна
            page.wait_for_function("() => { const b = document.querySelector(\"[data-testid='tweetButton']\"); return b && !b.disabled && b.getAttribute('aria-disabled') !== 'true'; }", timeout=300000)
        return None
    send.click(timeout=300000)   # пока ролик загружается, кнопка неактивна
    try:
        a = page.locator("[data-testid='toast'] a[href*='/status/']").first
        a.wait_for(timeout=60000)
        return absolute("https://x.com", a.get_attribute("href"))
    except PwTimeout:
        return profile_last(page, "x")


def profile_last(page, net):
    """Ссылка на свежий пост из профиля (если сайт не показал её сам)."""
    user = os.environ.get(SITES[net]["user"], "").strip().lstrip("@")
    if not user:
        return ""
    url = {"th": f"https://www.threads.com/@{user}", "ig": f"https://www.instagram.com/{user}/", "x": f"https://x.com/{user}"}[net]
    sel = {"th": "a[href*='/post/']", "ig": "a[href*='/p/'], a[href*='/reel/']", "x": "article a[href*='/status/']"}[net]
    try:
        page.goto(url, wait_until="domcontentloaded")
        a = page.locator(sel).first
        a.wait_for(timeout=20000)
        href = a.get_attribute("href").split("?")[0]
        return href if href.startswith("http") else url.split("/@")[0].rstrip("/").split(f"/{user}")[0] + href
    except Exception:
        return ""


POSTERS = {"th": post_threads, "ig": post_instagram, "x": post_x}


def report(job_id, net, url="", err="", shot=b"", dry=False):
    params = {"post": job_id, "net": net}
    if url:
        params["url"] = url
    if err:
        params["err"] = err
    if dry:
        params["dry"] = "1"
    r = requests.post(f"{WORKER}/th/pub", params=params, headers={**H, "content-type": "image/jpeg"}, data=shot or b"", timeout=60)
    print(SITES[net]["name"], "→ отчёт", r.status_code)


def promo_report(_job, net, url="", err="", shot=b"", dry=False):
    """Промо — не пост из плана: отчёт не Worker'у, а в лог и в папку shots/ (артефакт запуска)."""
    print(SITES[net]["name"], "→", url or err or "готово")
    if shot:
        pathlib.Path("shots").mkdir(exist_ok=True)
        (pathlib.Path("shots") / f"{net}.jpg").write_bytes(shot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--promo", help="JSON промо-поста: text, ig_text, x_text, video (ссылка на mp4)")
    ap.add_argument("--nets", default="th,ig")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    tmp = pathlib.Path(tempfile.mkdtemp())
    files = []
    if a.promo:
        job = json.loads(pathlib.Path(a.promo).read_text(encoding="utf-8"))
        vid = requests.get(job["video"], timeout=300)
        vid.raise_for_status()
        f = tmp / "video.mp4"
        f.write_bytes(vid.content)
        files.append(str(f))
        print("ролик:", len(vid.content) // 1024, "КБ")
    else:
        if not a.job:
            sys.exit("нужен --job или --promo")
        if not SECRET:
            sys.exit("нужен LS_INGEST_SECRET")
        r = requests.get(f"{WORKER}/th/job", params={"id": a.job}, headers=H, timeout=30)
        r.raise_for_status()
        job = r.json()
        for i in range(job.get("n_media") or 0):
            img = requests.get(f"{WORKER}/th/m/{a.job}/{i}.jpg", timeout=30)
            img.raise_for_status()
            f = tmp / f"slide{i + 1}.jpg"
            f.write_bytes(img.content)
            files.append(str(f))
    send = promo_report if a.promo else report
    nets = [n for n in a.nets.split(",") if n in POSTERS]
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for net in nets:
            ctx = browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                ctx.add_cookies(cookies_for(net))
                url = POSTERS[net](page, job, files, a.dry)
                if a.dry:
                    send(a.job, net, err="проверка: всё готово, публикацию не нажимал", shot=page.screenshot(type="jpeg", quality=70), dry=True)
                else:
                    send(a.job, net, url=url or "")
            except Exception as e:
                failed += 1
                why = str(e) if isinstance(e, Fail) else f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                print(SITES[net]["name"], "ошибка:", type(e).__name__)
                try:
                    shot = page.screenshot(type="jpeg", quality=70)
                except Exception:
                    shot = b""
                send(a.job, net, err=why, shot=shot, dry=a.dry)
            finally:
                ctx.close()
            pause(5, 15)   # между сетями — как человек, а не залпом
        browser.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
