"""Слайды Скаута (Threads и Instagram): задание от Worker'а → JPEG → обратно в Worker.

Worker на бесплатном тарифе не может рисовать картинки (10 мс процессора на
запрос), поэтому слайды рисует этот скрипт в GitHub Actions: берёт задание
GET /th/job?id=N, рендерит каждый слайд шаблоном threads/template.html в
Chromium (Playwright) и отправляет JPEG на POST /th/media?post=N&ix=K.
JPEG, а не PNG: Instagram принимает только JPEG, Threads — оба.
Оба запроса подписаны x-ingest-secret. Текст поста в запуск не передаётся —
логи публичного репозитория видны всем.

Запуск:  python threads_render.py --job 12
Проверка дизайна без Worker'а:  python threads_render.py --demo  → threads/preview/*.jpg
"""
import argparse
import base64
import json
import os
import pathlib
import sys

import requests
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent / "threads"
WORKER = os.environ.get("WORKER_URL", "https://launch-scout-bot.clam83574.workers.dev")
SECRET = os.environ.get("LS_INGEST_SECRET", "")

DEMO = [
    {"kind": "hook", "rubric": "Деньги недели", "number": "$2,1 млрд", "title": "ИИ-агенты съели треть денег недели", "body": "А финтех впервые за месяц ушёл из топ-3."},
    {"kind": "bars", "rubric": "Деньги недели", "title": "Куда ушли деньги за 7 дней", "bars": [
        {"label": "ИИ-агенты", "value": 710, "display": "$710 млн"}, {"label": "ИИ-инфраструктура", "value": 520, "display": "$520 млн"},
        {"label": "Кибербезопасность", "value": 300, "display": "$300 млн"}, {"label": "Финтех", "value": 180, "display": "$180 млн"},
        {"label": "Медицина", "value": 120, "display": "$120 млн"}], "body": "Без мегараундов от $1 млрд."},
    {"kind": "text", "rubric": "Деньги недели", "title": "Что это значит для маленькой команды", "body": "Агентов для **больших компаний** уже строят с миллионами. А вот агент для ==бухгалтера в Алматы== пока никому не интересен. И зря."},
    {"kind": "list", "rubric": "Деньги недели", "title": "Где искать дыру", "items": ["Узкая профессия, а не «бизнес вообще»", "Местный язык и законы", "Оплата в тенге и Kaspi"]},
    {"kind": "quote", "rubric": "Боль недели", "quote": "Я трачу два часа в день, чтобы сверить выписки руками", "source": "Reddit · r/smallbusiness"},
    {"kind": "stat", "rubric": "Цифра дня", "number": "×3", "unit": "раунда за месяц", "title": "ИИ-CRM для B2B снова в моде", "body": "Раньше — один раунд в месяц."},
    {"kind": "cta", "rubric": "Деньги недели", "title": "Проверь свою нишу за 10 секунд", "body": "Спроси бота текстом или голосом. 300 LS на старте — бесплатно."},
]


def avatar_uri():
    return "data:image/svg+xml;base64," + base64.b64encode((ROOT / "avatar.svg").read_bytes()).decode()


def render(slides):
    """Список спецификаций → список JPEG (bytes)."""
    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("{{AVATAR}}", avatar_uri())
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1440}, device_scale_factor=1)
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        n = len(slides)
        for i, spec in enumerate(slides, 1):
            page.evaluate("(s) => render(s)", {**spec, "i": i, "n": n})
            page.evaluate("Promise.all([document.fonts.ready, ...[...document.images].map((im) => im.decode().catch(() => 0))])")
            out.append(page.screenshot(type="jpeg", quality=93, clip={"x": 0, "y": 0, "width": 1080, "height": 1440}))
        browser.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if a.demo:
        dst = ROOT / "preview"
        dst.mkdir(exist_ok=True)
        for i, jpg in enumerate(render(DEMO), 1):
            (dst / f"slide_{i}.jpg").write_bytes(jpg)
        print("готово:", dst)
        return
    if not (a.job and SECRET):
        sys.exit("нужны --job и LS_INGEST_SECRET")
    h = {"x-ingest-secret": SECRET}
    r = requests.get(f"{WORKER}/th/job", params={"id": a.job}, headers=h, timeout=30)
    r.raise_for_status()
    job = r.json()
    slides = job.get("slides") or []
    if not slides:
        sys.exit("в задании нет слайдов")
    for i, jpg in enumerate(render(slides)):
        rr = requests.post(f"{WORKER}/th/media", params={"post": a.job, "ix": i, "n": len(slides)}, headers={**h, "content-type": "image/jpeg"}, data=jpg, timeout=60)
        print("слайд", i + 1, rr.status_code, rr.text[:120])
        rr.raise_for_status()


if __name__ == "__main__":
    main()
