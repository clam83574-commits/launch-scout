# -*- coding: utf-8 -*-
"""
Hacker News через Algolia API — бесплатно, без ключа, без лимитов на практике.

Два потока, и первый важнее второго:

  «Launch HN» — так на HN запускаются компании Y Combinator, причём в день
  запуска и с основателями в комментариях. Это ровно то, что заказано:
  проект акселератора в момент выхода. Поток редкий (единицы в неделю),
  поэтому берём его целиком, без порогов.

  «Show HN» — всё остальное, что человек сделал и показывает. Поток
  большой и шумный, тут уже нужны пороги по скорости набора очков.

Сигнал HN: очки (points) и комментарии. Абсолютные значения обманчивы —
пост, висящий сутки, наберёт больше, чем взлетающий за час. Скорость
считает score.py по истории замеров, здесь только честный сбор.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import clean_text, domain_of, http_json  # noqa: E402

API = "https://hn.algolia.com/api/v1"
SOURCE = "hn"


def _to_item(hit, now):
    """Строка Algolia -> кандидат нашей схемы."""
    url = hit.get("url")
    title = clean_text(hit.get("title") or hit.get("story_title") or "", 300)
    body = clean_text(hit.get("story_text") or hit.get("comment_text") or "", 700)
    # У Launch HN ссылки в поле url нет — она в тексте поста.
    if not url and body:
        from common import first_external_url
        url = first_external_url(body)
    kind = "launch" if "launch hn" in title.lower() else "show"
    return {
        "source": SOURCE,
        "ext_id": hit.get("objectID"),
        "url": "https://news.ycombinator.com/item?id=%s" % hit.get("objectID"),
        "product_url": url,
        "domain": domain_of(url),
        "title": title,
        "body": body,
        "author": hit.get("author"),
        "author_followers": None,
        "posted_at": hit.get("created_at_i"),
        "first_seen": now,
        "tags": kind,
        "raw": None,
    }, {
        "likes": hit.get("points"),
        "replies": hit.get("num_comments"),
    }


def fetch(window_hours=48, limit=100):
    """
    Свежие Launch HN и Show HN за окно. Возвращает (список пар, ошибка).

    Окно шире интервала прогона намеренно: пост, опубликованный час назад,
    ещё не показал, разгонится он или нет, и должен попадаться нам несколько
    раз подряд — иначе не из чего считать ускорение.
    """
    now = int(time.time())
    cutoff = now - window_hours * 3600
    out, errors = [], []

    # Launch HN: ищем по фразе, без порогов.
    data, err = http_json(
        API + "/search_by_date",
        params={"query": '"Launch HN"', "tags": "story",
                "numericFilters": "created_at_i>%d" % cutoff,
                "hitsPerPage": 50})
    if err:
        errors.append("launch: " + err)
    else:
        for hit in data.get("hits", []):
            title = (hit.get("title") or "").lower()
            if "launch hn" not in title:
                continue          # Algolia отдаёт и просто похожие тексты
            out.append(_to_item(hit, now))

    # Show HN: весь поток за окно, фильтрация — дальше по скорости.
    data, err = http_json(
        API + "/search_by_date",
        params={"tags": "show_hn",
                "numericFilters": "created_at_i>%d" % cutoff,
                "hitsPerPage": limit})
    if err:
        errors.append("show: " + err)
    else:
        for hit in data.get("hits", []):
            out.append(_to_item(hit, now))

    return out, ("; ".join(errors) if errors else None)


def refresh(ext_ids):
    """
    Повторный замер уже известных постов — ради скорости и ускорения.

    Algolia позволяет спросить пачку одним запросом через tags=(story_1,story_2),
    поэтому сотня кандидатов стоит одного обращения, а не сотни.
    """
    if not ext_ids:
        return {}, None
    got, errors = {}, []
    ids = [str(i) for i in ext_ids]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        tags = "(%s)" % ",".join("story_%s" % x for x in chunk)
        data, err = http_json(API + "/search",
                              params={"tags": tags, "hitsPerPage": len(chunk)})
        if err:
            errors.append(err)
            continue
        for hit in data.get("hits", []):
            got[str(hit.get("objectID"))] = {
                "likes": hit.get("points"),
                "replies": hit.get("num_comments"),
            }
    return got, ("; ".join(errors) if errors else None)
