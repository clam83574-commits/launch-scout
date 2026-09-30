# -*- coding: utf-8 -*-
"""
Сигналы спроса и найма помимо денег (2026-10-01):

  * Astana Hub, «Технологические задачи» — компании Казахстана публикуют,
    что им нужно разработать (ИИ-ассистент для приёмной комиссии, прогноз
    отклонений на рынке электроэнергии, ПО против БПЛА…). Прямой местный
    B2B-спрос с названием заказчика и числом откликов команд.
  * HN «Ask HN: Who is hiring?» — ежемесячный тред вакансий. Совпадение с
    компаниями, поднявшими раунды, показывает, что деньги превращаются в найм.
  * Devpost — открытые хакатоны: какие задачи и темы ставят организаторы.

Всё — открытые страницы и API без ключей.
"""
import html
import re
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36"}
AH = "https://astanahub.com"


def _text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def astanahub_tasks(pages=3, timeout=30):
    """[{url, title, desc, company, area, deadline, bids}], ошибка."""
    out, err = [], None
    for page in range(1, pages + 1):
        try:
            r = requests.get(AH + "/ru/tech_task/", params={"page": page} if page > 1 else None,
                             headers=UA, timeout=timeout)
        except requests.RequestException as e:
            err = str(e)[:120]
            break
        if r.status_code != 200:
            err = "HTTP %d" % r.status_code
            break
        cards = re.findall(r'<a href="(/ru/tech_task/[^"]+)" class="techtask-card[^"]*">(.*?)</a>', r.text, re.S)
        if not cards:
            break
        for href, body in cards:
            title = re.search(r"<h2>(.*?)</h2>", body, re.S)
            desc = re.search(r'<div class="left">.*?<p>(.*?)</p>', body, re.S)
            comp = re.search(r"Заказчик</h3>\s*<h4>(.*?)</h4>", body, re.S)
            area = re.search(r"Область задачи\s*</p>\s*<span>(.*?)</span>", body, re.S)
            dead = re.search(r"Прием решений до</p>\s*<p><b>(.*?)</b>", body, re.S)
            bids = re.search(r"Количество заявок</p>\s*<p>(\d+)</p>", body, re.S)
            out.append({"url": AH + href, "title": _text(title.group(1)) if title else "",
                        "desc": _text(desc.group(1))[:600] if desc else "",
                        "company": _text(comp.group(1)) if comp else "",
                        "area": _text(area.group(1)) if area else "",
                        "deadline": _text(dead.group(1)) if dead else "",
                        "bids": int(bids.group(1)) if bids else None})
        time.sleep(0.8)
    return out, err


def hn_hiring(timeout=40):
    """Последний тред «Who is hiring»: (месяц, [{id, company, text, url}]), ошибка."""
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search_by_date",
                         params={"tags": "story,author_whoishiring", "hitsPerPage": 6}, timeout=timeout)
        hits = [h for h in r.json().get("hits", []) if "Who is hiring" in (h.get("title") or "")]
        if not hits:
            return None, [], "нет треда"
        story = hits[0]
        month = re.search(r"\(([^)]+)\)", story["title"])
        item = requests.get("https://hn.algolia.com/api/v1/items/%s" % story["objectID"], timeout=timeout).json()
    except (requests.RequestException, ValueError, KeyError) as e:
        return None, [], str(e)[:120]
    out = []
    for c in item.get("children") or []:
        text = _text(c.get("text"))
        if not text or "|" not in text[:200]:
            continue
        company = re.sub(r"https?://\S+|\([^)]*\)", "", text.split("|")[0]).strip(" -–:")[:80]
        if not company:
            continue
        out.append({"id": str(c.get("id")), "company": company, "text": text[:500],
                    "url": "https://news.ycombinator.com/item?id=%s" % c.get("id")})
    return (month.group(1) if month else story["title"]), out, None


def devpost_hackathons(pages=6, timeout=30):
    """Открытые хакатоны Devpost: [{url, title, org, themes, prize, dates, location}], ошибка."""
    out, err = [], None
    for page in range(1, pages + 1):
        try:
            r = requests.get("https://devpost.com/api/hackathons",
                             params={"status[]": "open", "order_by": "recently-added", "page": page},
                             headers=UA, timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            err = str(e)[:120]
            break
        items = data.get("hackathons") or []
        if not items:
            break
        for h in items:
            out.append({"url": h.get("url") or "", "title": (h.get("title") or "")[:160],
                        "org": (h.get("organization_name") or "")[:100],
                        "themes": [t.get("name") for t in (h.get("themes") or []) if t.get("name")][:5],
                        "prize": _text(h.get("prize_amount") or ""),
                        "dates": h.get("submission_period_dates") or "",
                        "location": ((h.get("displayed_location") or {}).get("location") or "")[:60]})
        time.sleep(0.5)
    return out, err
