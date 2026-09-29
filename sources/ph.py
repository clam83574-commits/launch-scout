# -*- coding: utf-8 -*-
"""
Product Hunt — витрина, куда стартапы сами приносят запуски в день выхода.

ДВА РЕЖИМА (проверено 2026-09-29):
  * без ключа — Atom-лента producthunt.com/feed: ~50 продуктов из подборки
    редакции, с описанием, но БЕЗ числа голосов. Такие записи — сигнал
    «вышел и попал в подборку», а не «взлетел»: до уведомления они не
    дотягивают, в сводку идут только с другим подтверждением;
  * с PH_TOKEN (developer token: producthunt.com/v2/oauth/applications ->
    Create Token) — GraphQL API v2: голоса и комментарии, лучшие за сутки.
    Тогда голоса считаются как лайки, и продукт оценивается по отклику
    наравне с HN и X.
"""
import html
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import clean_text, domain_of  # noqa: E402

SOURCE = "ph"
FEED = "https://www.producthunt.com/feed"
API = "https://api.producthunt.com/v2/api/graphql"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")

QUERY = """query($after: DateTime!) {
  posts(first: 40, order: VOTES, postedAfter: $after) {
    edges { node {
      id name tagline description url website votesCount commentsCount createdAt featuredAt
      topics(first: 3) { edges { node { name } } }
      makers { username }
    } }
  }
}"""


def _iso_ts(s):
    """'2026-09-28T06:17:05-07:00' / '…Z' -> unix."""
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError):
        return None


def _item(now, ext_id, name, tagline, url, website, posted, votes=None, comments=None, topics="", maker=None):
    title = "%s — %s" % (name, tagline) if tagline else name
    return {
        "source": SOURCE, "ext_id": str(ext_id), "url": url,
        "product_url": website or url, "domain": domain_of(website) if website and "producthunt.com" not in website else None,
        "title": clean_text(title, 200), "body": clean_text(tagline, 900),
        "author": maker, "author_followers": None, "posted_at": posted,
        "first_seen": now, "tags": topics or "producthunt", "raw": None,
    }, ({"likes": votes, "replies": comments} if votes is not None else None)


def fetch_api(token, now, window_hours=30):
    after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_hours * 3600))
    try:
        r = requests.post(API, json={"query": QUERY, "variables": {"after": after}}, timeout=30,
                          headers={"Authorization": "Bearer " + token, "User-Agent": UA,
                                   "Content-Type": "application/json", "Accept": "application/json"})
    except requests.RequestException as e:
        return [], "сеть: %s" % str(e)[:120]
    if r.status_code != 200:
        return [], "API %d: %s" % (r.status_code, r.text[:120])
    try:
        edges = r.json()["data"]["posts"]["edges"]
    except (ValueError, KeyError, TypeError):
        return [], "непонятный ответ API: %s" % r.text[:120]
    out = []
    for e in edges:
        n = e.get("node") or {}
        topics = ", ".join(t["node"]["name"] for t in ((n.get("topics") or {}).get("edges") or []))
        maker = ((n.get("makers") or [{}])[0] or {}).get("username")
        out.append(_item(now, n.get("id"), n.get("name") or "", n.get("tagline") or "", n.get("url"),
                         n.get("website"), _iso_ts(n.get("featuredAt") or n.get("createdAt")),
                         n.get("votesCount") or 0, n.get("commentsCount") or 0, topics, maker))
    return out, None


def fetch_feed(now, window_hours=48):
    try:
        r = requests.get(FEED, headers={"User-Agent": UA}, timeout=30)
    except requests.RequestException as e:
        return [], "сеть: %s" % str(e)[:120]
    if r.status_code != 200:
        return [], "лента %d" % r.status_code
    out = []
    for ent in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        pid = re.search(r"Post/(\d+)</id>", ent)
        link = re.search(r'<link rel="alternate" type="text/html" href="([^"]+)"', ent)
        title = re.search(r"<title>(.*?)</title>", ent, re.S)
        pub = re.search(r"<published>(.*?)</published>", ent)
        body = re.search(r"<content type=\"html\">(.*?)</content>", ent, re.S)
        if not (pid and link and title):
            continue
        posted = _iso_ts(pub.group(1)) if pub else None
        if posted and posted < now - window_hours * 3600:
            continue
        text = re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(body.group(1)))) if body else ""
        tagline = re.sub(r"\s+", " ", text.split("Discussion")[0]).strip()
        out.append(_item(now, pid.group(1), html.unescape(title.group(1)).strip(), tagline,
                         link.group(1), None, posted))
    return out, None


def client_token():
    """
    Токен по паре API Key + API Secret (grant_type=client_credentials): так
    Product Hunt выдаёт доступ к публичным данным без входа пользователя.
    Нужен, когда вместо Developer Token есть только пара ключей приложения.
    """
    cid = (os.environ.get("PH_CLIENT_ID") or "").strip()
    secret = (os.environ.get("PH_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        return None
    try:
        r = requests.post("https://api.producthunt.com/v2/oauth/token", timeout=30,
                          json={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
                          headers={"User-Agent": UA, "Accept": "application/json"})
        return (r.json() or {}).get("access_token") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def fetch():
    """
    [(кандидат, метрики)], ошибка. С PH_TOKEN (или парой PH_CLIENT_ID +
    PH_CLIENT_SECRET) — API с голосами, без — лента.
    """
    now = int(time.time())
    token = (os.environ.get("PH_TOKEN") or "").strip() or client_token()
    if token:
        got, err = fetch_api(token, now)
        if not err:
            return got, None
        feed, ferr = fetch_feed(now)
        return feed, "API не ответил (%s), взята лента без голосов" % err if not ferr else err
    return fetch_feed(now)
