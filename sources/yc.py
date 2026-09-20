# -*- coding: utf-8 -*-
"""
Y Combinator — открытое зеркало каталога компаний (yc-oss.github.io/api).

Зачем зеркало, а не сайт YC: каталог на ycombinator.com рисуется джаваскриптом
через закрытый индекс Algolia, а зеркало отдаёт тот же набор готовым JSON,
обновляется ежедневно и не требует ключей.

Сигнал здесь не «много лайков», а сам факт появления: компания, которой
вчера в каталоге не было, а сегодня есть, — это компания, только что
принятая в батч. Метрик вовлечённости у источника нет и не нужно:
отбор YC и есть подтверждённый интерес инвесторов, ради которого
всё затевалось. Такие кандидаты получают фиксированный высокий балл
в score.py, минуя расчёт скорости.

Берём два последних батча, а не весь каталог: в нём 6000+ компаний,
качать их каждые полчаса — бессмысленный трафик.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import clean_text, domain_of, http_json  # noqa: E402

META = "https://yc-oss.github.io/api/meta.json"
BATCH = "https://yc-oss.github.io/api/batches/%s.json"
SOURCE = "yc"

_SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def _batch_key(slug):
    """'summer-2026' -> (2026, 2). Для сортировки батчей по свежести."""
    parts = slug.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return (0, 0)
    return (int(parts[1]), _SEASON_ORDER.get(parts[0].lower(), 0))


def latest_batches(min_year=None, fallback_n=3):
    """
    Слаги батчей текущего года и новее.

    Просто «взять два самых свежих» здесь не работает: YC заводит будущие
    батчи заранее, и в summer-2027 на 2026-09-20 лежит ОДНА компания.
    Такая выборка честно отдаёт самое свежее и при этом пропускает
    Summer 2026 с её 232 компаниями — то есть ровно то, за чем пришли.
    Поэтому берём год целиком: полупустые будущие батчи не мешают, а
    заполненные текущие не теряются.
    """
    data, err = http_json(META, timeout=20)
    if err or not data:
        return [], err or "пустой meta.json"
    slugs = list((data.get("batches") or {}).keys())
    if not slugs:
        return [], "в meta.json нет списка батчей"
    slugs.sort(key=_batch_key, reverse=True)
    year = min_year or time.gmtime().tm_year
    picked = [s for s in slugs if _batch_key(s)[0] >= year]
    return (picked or slugs[:fallback_n]), None


def fetch(batches=None, min_year=None):
    """Компании свежих батчей. Возвращает (список пар, ошибка)."""
    now = int(time.time())
    if not batches:
        batches, err = latest_batches(min_year)
        if err:
            return [], err
    out, errors = [], []
    for slug in batches:
        data, err = http_json(BATCH % slug, timeout=30)
        if err or not isinstance(data, list):
            errors.append("%s: %s" % (slug, err or "не список"))
            continue
        for c in data:
            site = c.get("website")
            out.append(({
                "source": SOURCE,
                "ext_id": c.get("id") or c.get("slug"),
                "url": c.get("url"),
                "product_url": site,
                "domain": domain_of(site),
                "title": clean_text(c.get("name"), 120),
                "body": clean_text(c.get("one_liner") or c.get("long_description"), 700),
                "author": None,
                "author_followers": None,
                # posted_at не ставим: launched_at в каталоге — это дата
                # запуска продукта на YC-платформе, она бывает старше батча
                # на годы и врёт про свежесть. Свежесть тут даёт first_seen.
                "posted_at": None,
                "first_seen": now,
                "tags": " ".join(filter(None, [
                    c.get("batch"), c.get("stage"),
                    " ".join(c.get("tags") or [])])).strip()[:400],
                "raw": None,
            }, {}))
    return out, ("; ".join(errors) if errors else None)
