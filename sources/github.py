# -*- coding: utf-8 -*-
"""
GitHub — репозитории, созданные недавно и быстро набирающие звёзды.

Для AI-инструментов это самый честный из бесплатных сигналов: звезда стоит
человеку одного клика, но ставят её разработчики, а не боты за розыгрыш,
и растёт она ровно тогда, когда инструментом реально начали пользоваться.

Фильтр по дате создания обязателен. Без него выдача — вечный список
старых знаменитых репозиториев: у них абсолютных звёзд больше всегда.

Токен не обязателен, но желателен: без него GitHub даёт 10 поисков в
минуту на IP, с ним — 30. В .env: GITHUB_TOKEN=ghp_...
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import clean_text, domain_of, http_json  # noqa: E402

API = "https://api.github.com/search/repositories"
SOURCE = "gh"


def _headers():
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def fetch(max_age_days=45, min_stars=150, limit=50):
    """Молодые репозитории выше порога звёзд. Возвращает (список пар, ошибка)."""
    now = int(time.time())
    since = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    data, err = http_json(
        API, headers=_headers(),
        params={"q": "created:>%s stars:>%d" % (since, min_stars),
                "sort": "stars", "order": "desc", "per_page": min(limit, 100)})
    if err:
        return [], err

    out = []
    for r in (data.get("items") or []):
        created = r.get("created_at")
        ts = None
        if created:
            try:
                ts = int(datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        # homepage — сайт продукта, если автор его указал; это он нам и нужен
        # для дедупликации с твиттером и HN, где ссылаются именно на сайт.
        site = (r.get("homepage") or "").strip() or None
        out.append(({
            "source": SOURCE,
            "ext_id": r.get("id"),
            "url": r.get("html_url"),
            "product_url": site or r.get("html_url"),
            "domain": domain_of(site),
            "title": clean_text(r.get("full_name"), 140),
            "body": clean_text(r.get("description"), 500),
            "author": ((r.get("owner") or {}).get("login")),
            "author_followers": None,
            "posted_at": ts,
            "first_seen": now,
            "tags": " ".join(filter(None, [r.get("language") or ""] +
                                    (r.get("topics") or [])))[:400],
            "raw": None,
        }, {
            "likes": r.get("stargazers_count"),
            "replies": r.get("open_issues_count"),
            "reposts": r.get("forks_count"),
        }))
    return out, None


def refresh(full_names):
    """
    Повторный замер звёзд. Один запрос на репозиторий — дорого по лимитам,
    поэтому зовётся только для кандидатов, уже прошедших первичный отбор.
    """
    got, errors = {}, []
    for name in full_names:
        data, err = http_json("https://api.github.com/repos/" + name,
                              headers=_headers(), tries=2)
        if err:
            errors.append("%s: %s" % (name, err))
            continue
        got[str(data.get("id"))] = {
            "likes": data.get("stargazers_count"),
            "replies": data.get("open_issues_count"),
            "reposts": data.get("forks_count"),
        }
        time.sleep(0.4)
    return got, ("; ".join(errors[:3]) if errors else None)
