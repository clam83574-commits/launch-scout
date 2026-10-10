# -*- coding: utf-8 -*-
"""
PDF-досье по разговору с ботом (2026-10-10). Бот кладёт задание после оплаты
(подарок к первой покупке или /pdf за LS), GitHub Actions запускает этот
скрипт: он забирает у Worker'а оглавление и факты из матрицы, пишущая модель
(Gemini через OpenRouter, как весь бот) отдаёт текст разделов JSON-ом со ссылками [F12]
на факты, а шаблон dossier/template.html превращает их в PDF в Chromium — со
сносками на источники внизу каждого раздела. Готовый файл уходит обратно
Worker'у, тот присылает его человеку.

Репозиторий публичный, логи Actions видны всем: в запуск передаётся только
номер задания, в лог — только счётчики.

    python dossier.py --job <id>
    python dossier.py --sample job.json --out test.pdf   # локально, без Worker'а
"""
import argparse
import html
import json
import os
import pathlib
import re
import sys
import time
from urllib.parse import urlparse

import requests

HERE = pathlib.Path(__file__).resolve().parent
WORKER = os.environ.get("WORKER_URL", "https://launch-scout-bot.clam83574.workers.dev")
OR_API = "https://openrouter.ai/api/v1/chat/completions"
# Gemini, как весь бот: PDF — хук для конверсии, а не заработок на генерации.
MODELS = [os.environ.get("DZ_MODEL") or "google/gemini-3.8-flash", "google/gemini-3.1-flash-lite"]
COST = {"usd": 0.0}   # фактическая стоимость генерации — боту, в уведомление владельцу

UI = {
    "ru": {"for": "Подготовлено Launch Scout по вашему разговору с ботом", "summary": "Главное", "kpis": "Ключевые цифры",
           "toc": "Содержание", "key": "Главный раздел", "sources": "Источники", "next": "Что сделать на этой неделе",
           "base": "База Launch Scout", "method": "Цифры — из базы раундов и ниш Launch Scout и открытых источников; у каждой — сноска. "
           "Оценки помечены словом «оценка». Это аналитика, а не инвестиционная рекомендация.", "page": "стр."},
    "kk": {"for": "Launch Scout ботпен әңгімеңіз бойынша дайындады", "summary": "Басты", "kpis": "Негізгі сандар",
           "toc": "Мазмұны", "key": "Басты бөлім", "sources": "Дереккөздер", "next": "Осы аптада не істеу керек",
           "base": "Launch Scout базасы", "method": "Сандар — Launch Scout раундтар мен нишалар базасынан және ашық дереккөздерден; әрқайсысында сілтеме бар. "
           "Бағалаулар «бағалау» сөзімен белгіленген. Бұл аналитика, инвестициялық кеңес емес.", "page": "бет"},
    "en": {"for": "Prepared by Launch Scout from your conversation with the bot", "summary": "Key takeaways", "kpis": "Key numbers",
           "toc": "Contents", "key": "Key section", "sources": "Sources", "next": "What to do this week",
           "base": "Launch Scout database", "method": "Numbers come from the Launch Scout database of rounds and niches and from open sources; each has a footnote. "
           "Estimates are marked as such. This is analysis, not investment advice.", "page": "p."},
}

SYSTEM = """You are a senior startup analyst. You write a paid PDF dossier for one founder, in %(lang)s.
The dossier must feel personal: it answers what THIS founder asked about (their questions and profile are given) and stays practical.

WHAT TO WRITE: %(brief)s
The sections are fixed — write exactly one section per CONTENTS item, in the same order, using the item text as the section title.
The LAST section is the key one the founder paid for: make it the longest and most concrete (named steps, numbers, owners, metrics).

FACTS RULES (strict):
- Every number, company, investor, round, price or date must come from FACTS and be followed by its citation like [F12] or [F3, F7].
- Never invent companies, investors, rounds, prices or URLs. If facts are thin, say so plainly and give reasoning instead of numbers.
- Your own estimates are allowed only for plans and unit economics; mark each with the word "%(est)s" and no citation.
- Do not cite a fact for something it does not say.

OUTPUT: one JSON object, no prose around it:
{
 "subtitle": "one line: what this dossier gives this founder",
 "summary": ["3-5 one-sentence takeaways, with citations"],
 "kpis": [{"value": "$120M", "label": "short label", "cite": [3]}],   // 3-4 items, numbers only from facts
 "sections": [
   {"title": "...", "lead": "1-2 sentences", "blocks": [
     {"type": "text", "text": "paragraph"},
     {"type": "bullets", "items": ["..."]},
     {"type": "table", "head": ["...", "..."], "rows": [["...", "..."]]},
     {"type": "steps", "items": [{"when": "Weeks 1-2", "what": "...", "metric": "..."}]},
     {"type": "callout", "text": "the one thing not to miss"}
   ]}
 ],
 "next": ["3 concrete actions for this week"]
}
Use tables for comparisons (competitors, investors, ideas), steps for any plan. Keep sentences short. **bold** is allowed."""


def _hdr():
    return {"x-ingest-secret": os.environ.get("LS_INGEST_SECRET", "")}


def _json(text):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        return None


def write(job):
    """Текст досье от модели: (dict, ошибка). Сначала основная модель, при сбое — запасная."""
    est = {"ru": "оценка", "kk": "бағалау"}.get(job["lang"], "estimate")
    system = SYSTEM % {"lang": job.get("lang_name") or "Russian", "brief": job["brief"], "est": est}
    facts = "\n".join("[F%d] %s" % (f["id"], f["text"]) for f in job.get("facts") or [])[:30000]
    user = "TODAY: %s\nTITLE: %s\nNICHE: %s\nFOUNDER'S MARKET: %s\nPROFILE: %s\n\nFOUNDER'S QUESTIONS (newest first):\n%s\n\nCONTENTS:\n%s\n\nFACTS:\n%s" % (
        job.get("date"), job["title"], job.get("niche") or "-", job.get("country") or "-", job.get("profile") or "unknown",
        "\n".join("- " + q for q in job.get("questions") or []) or "-",
        "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(job["toc"])), facts or "(no facts)")
    err = None
    for model in MODELS:
        body = {"model": model, "max_tokens": 9000, "temperature": 0.3, "response_format": {"type": "json_object"},
                "usage": {"include": True},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = requests.post(OR_API, json=body, timeout=300, headers={
                "Authorization": "Bearer " + (os.environ.get("OPENROUTER_API_KEY") or "").strip(),
                "HTTP-Referer": "https://github.com/clam83574-commits/launch-scout", "X-Title": "launch-scout-dossier"})
        except requests.RequestException as e:
            err = "%s: сеть %s" % (model, str(e)[:100])
            continue
        if r.status_code != 200:
            err = "%s: HTTP %d %s" % (model, r.status_code, r.text[:120])
            continue
        try:
            payload = r.json()
            COST["usd"] += float((payload.get("usage") or {}).get("cost") or 0)
            data = _json(payload["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError):
            data = None
        if data and data.get("sections"):
            return data, None
        err = "%s: ответ не JSON" % model
    return None, err


# ---------------------------------------------------------------------------
# Вёрстка: экранируем текст модели, потом [F12] -> сноска. Номера сносок —
# сквозные, по первому упоминанию; список — внизу своего раздела.
# ---------------------------------------------------------------------------
class Notes:
    def __init__(self, facts, ui):
        self.facts = {int(f["id"]): f for f in facts or []}
        self.ui = ui
        self.num = {}        # id факта -> номер сноски
        self.section = []    # факты, на которые ссылается текущий раздел (и уже встречавшиеся раньше)

    def ref(self, fid):
        if fid not in self.facts:
            return ""
        if fid not in self.num:
            self.num[fid] = len(self.num) + 1
        if fid not in self.section:
            self.section.append(fid)
        n = self.num[fid]
        return '<sup class="fn"><a href="#n%d">%d</a></sup>' % (n, n)

    def text(self, s):
        """Строка модели -> безопасный HTML со сносками и **жирным**."""
        out = html.escape(str(s or ""))
        out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)

        def cite(m):
            ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
            return "".join(self.ref(i) for i in ids)
        out = re.sub(r"\s*[\[【]\s*(F\d+(?:\s*[,;]\s*F?\d+)*)\s*[\]】]", cite, out)
        return out

    def cites(self, ids):
        return "".join(self.ref(int(i)) for i in ids or [] if str(i).isdigit())

    def flush(self):
        """Сноски раздела — список внизу раздела."""
        if not self.section:
            return ""
        items = []
        for fid in sorted(self.section, key=lambda f: self.num[f]):
            f = self.facts[fid]
            label = re.sub(r"\s+", " ", f.get("text") or "")
            label = label[:150] + ("…" if len(label) > 150 else "")
            url = f.get("url") or ""
            if url.startswith("http"):
                host = urlparse(url).netloc.replace("www.", "")
                src = '<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(host))
            else:
                src = html.escape(self.ui["base"])
            items.append('<li id="n%d" value="%d">%s — <span>%s</span></li>' % (self.num[fid], self.num[fid], src, html.escape(label)))
        self.section = []
        return '<div class="notes"><b>%s</b><ol>%s</ol></div>' % (html.escape(self.ui["sources"]), "".join(items))


def block(b, n):
    t = (b or {}).get("type")
    if t == "text":
        return "<p>%s</p>" % n.text(b.get("text"))
    if t == "bullets":
        return "<ul>%s</ul>" % "".join("<li>%s</li>" % n.text(x) for x in b.get("items") or [])
    if t == "callout":
        return '<div class="callout">%s</div>' % n.text(b.get("text"))
    if t == "table":
        head = "".join("<th>%s</th>" % n.text(x) for x in b.get("head") or [])
        rows = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % n.text(c) for c in r) for r in b.get("rows") or [] if isinstance(r, list))
        return '<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (head, rows)
    if t == "steps":
        out = []
        for i, x in enumerate(b.get("items") or []):
            x = x if isinstance(x, dict) else {"what": x}
            out.append('<div class="step"><div class="when">%s</div><div class="what">%s%s</div></div>' % (
                n.text(x.get("when") or str(i + 1)), n.text(x.get("what")),
                '<div class="metric">↳ %s</div>' % n.text(x["metric"]) if x.get("metric") else ""))
        return '<div class="steps">%s</div>' % "".join(out)
    return ""


def render_html(job, d):
    ui = UI.get(job["lang"], UI["ru"])
    n = Notes(job.get("facts"), ui)
    esc = html.escape
    secs = (d.get("sections") or [])[:len(job["toc"])]
    # Подзаголовок, сводка и цифры — до разделов: их сноски идут первыми.
    subtitle = n.text(d.get("subtitle"))
    summary = "".join("<li>%s</li>" % n.text(x) for x in d.get("summary") or [])
    kpis = "".join('<div class="kpi"><b>%s%s</b><span>%s</span></div>' % (
        esc(str(k.get("value") or "")), n.cites(k.get("cite")), n.text(k.get("label")))
        for k in (d.get("kpis") or [])[:4] if isinstance(k, dict))
    intro_notes = n.flush()
    toc = "".join('<li%s>%s</li>' % (' class="key"' if i == len(job["toc"]) - 1 else "", esc(t)) for i, t in enumerate(job["toc"]))
    body = []
    for i, s in enumerate(secs):
        key = i == len(job["toc"]) - 1
        title = job["toc"][i] if i < len(job["toc"]) else s.get("title")
        inner = "".join(block(b, n) for b in s.get("blocks") or [])
        body.append('<section class="sec%s"><div class="num">%s</div><h2>%s</h2>%s<p class="lead">%s</p>%s%s</section>' % (
            " key" if key else "", "%02d" % (i + 1), esc(title), '<div class="badge">🔑 %s</div>' % esc(ui["key"]) if key else "",
            n.text(s.get("lead")), inner, n.flush()))
    nxt = "".join("<li>%s</li>" % n.text(x) for x in d.get("next") or [])
    tpl = (HERE / "dossier" / "template.html").read_text(encoding="utf-8")
    repl = {
        "__LANG__": job["lang"], "__TITLE__": esc(job["title"]), "__SUBTITLE__": subtitle,
        "__FOR__": esc(ui["for"]), "__DATE__": esc(".".join(reversed(job.get("date", "").split("-")))),
        "__MARKET__": esc(" · ".join(x for x in [job.get("niche"), job.get("country")] if x)),
        "__L_SUMMARY__": esc(ui["summary"]), "__L_KPIS__": esc(ui["kpis"]), "__L_TOC__": esc(ui["toc"]), "__L_NEXT__": esc(ui["next"]),
        "__SUMMARY__": summary, "__KPIS__": kpis, "__INTRO_NOTES__": intro_notes, "__TOC__": toc,
        "__SECTIONS__": "".join(body), "__NEXT__": nxt, "__NEXT_NOTES__": n.flush(), "__METHOD__": esc(ui["method"]),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl, len(n.num)


def to_pdf(page_html, out, lang):
    from playwright.sync_api import sync_playwright
    page_lbl = UI.get(lang, UI["ru"])["page"]
    tmp = pathlib.Path(out).with_suffix(".html")
    tmp.write_text(page_html, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(tmp.resolve().as_uri(), wait_until="networkidle", timeout=60000)
        pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
               header_template="<span></span>",
               footer_template='<div style="width:100%%;font:7.5px Arial,sans-serif;color:#8a96ad;padding:0 16mm;display:flex;justify-content:space-between">'
                               '<span>Launch Scout</span><span>%s <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>' % page_lbl,
               prefer_css_page_size=True)   # поля — из @page шаблона: обложка под обрез
        b.close()
    return pathlib.Path(out).read_bytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job")
    ap.add_argument("--sample", help="задание из файла (локальная проверка)")
    ap.add_argument("--text", help="готовый текст модели из файла — без вызова модели")
    ap.add_argument("--out", default="dossier.pdf")
    a = ap.parse_args()
    if a.sample:
        job = json.loads(pathlib.Path(a.sample).read_text(encoding="utf-8"))
    else:
        r = requests.get(WORKER + "/dossier-job", params={"id": a.job}, headers=_hdr(), timeout=60)
        if r.status_code != 200:
            print("задание не получено: HTTP %d" % r.status_code)
            return 1
        job = r.json()
    t0 = time.time()

    def fail(msg):
        print("сбой: " + msg[:200])
        if not a.sample:
            requests.post(WORKER + "/dossier-result", params={"id": a.job}, json={"error": msg[:500]}, headers=_hdr(), timeout=60)
        return 1

    if a.text:
        d, err = json.loads(pathlib.Path(a.text).read_text(encoding="utf-8")), None
    else:
        d, err = write(job)
    if not d:
        return fail("текст: %s" % err)
    try:
        page_html, notes = render_html(job, d)
        pdf = to_pdf(page_html, a.out, job["lang"])
    except Exception as e:  # noqa: BLE001 — любой сбой вёрстки сообщаем боту
        return fail("вёрстка: %s" % e)
    # В лог — только счётчики (публичный репозиторий).
    print("формат %s: %d фактов, %d разделов, %d сносок, %d КБ, %.0f с" % (
        job.get("fmt"), len(job.get("facts") or []), len(d.get("sections") or []), notes, len(pdf) // 1024, time.time() - t0))
    if a.sample:
        print("PDF: " + str(pathlib.Path(a.out).resolve()))
        return 0
    p = requests.post(WORKER + "/dossier-result", params={"id": a.job}, data=pdf,
                      headers={**_hdr(), "content-type": "application/pdf", "x-cost-usd": "%.5f" % COST["usd"]}, timeout=120)
    print("бот: HTTP %d" % p.status_code)
    return 0 if p.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
