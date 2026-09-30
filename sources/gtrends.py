# -*- coding: utf-8 -*-
"""
Google Trends — интерес поиска к нише по неделям за год (2026-10-01).

Официального открытого API у Google Trends нет; это тот же внутренний
эндпоинт, который вызывает сайт trends.google.com (explore -> токен ->
widgetdata/multiline). Проверено: из GitHub Actions отвечает, из Cloudflare
Workers — 429, поэтому сбор только в пайплайне Actions.

Значения — относительные (0-100, максимум ряда = 100), поэтому сравниваем
ряд сам с собой: последние недели против предыдущих, а не ниши между собой.
Каждый термин — отдельный запрос, чтобы редкие термины не сплющивались в 0
рядом с популярными.
"""
import json
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
BASE = "https://trends.google.com"


class Blocked(Exception):
    """Google ответил 429 — на этот прогон хватит."""


class Trends:
    def __init__(self, timeout=25, pause=3.0):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.timeout, self.pause = timeout, pause
        self._warm = False

    def _get(self, path, params):
        for attempt in range(2):
            r = self.s.get(BASE + path, params=params, timeout=self.timeout)
            if r.status_code == 429:
                if attempt == 0:
                    time.sleep(30)
                    continue
                raise Blocked("429")
            if r.status_code != 200:
                raise RuntimeError("HTTP %d" % r.status_code)
            # Ответ начинается с защитного префикса )]}' — JSON после первой строки.
            return json.loads(r.text[r.text.find("\n") + 1:] if r.text.startswith(")]}'") else r.text)
        raise Blocked("429")

    def weekly(self, term, geo="", timeframe="today 12-m"):
        """[(ts, 0-100)] по неделям для одного термина; [] если данных нет."""
        if not self._warm:
            self.s.get(BASE + "/?geo=US", timeout=self.timeout)   # кука NID
            self._warm = True
        req = {"comparisonItem": [{"keyword": term, "geo": geo, "time": timeframe}], "category": 0, "property": ""}
        ex = self._get("/trends/api/explore", {"hl": "en-US", "tz": "0", "req": json.dumps(req)})
        w = next((w for w in ex.get("widgets") or [] if w.get("id") == "TIMESERIES"), None)
        if not w:
            return []
        time.sleep(self.pause)
        data = self._get("/trends/api/widgetdata/multiline",
                         {"hl": "en-US", "tz": "0", "req": json.dumps(w["request"]), "token": w["token"]})
        time.sleep(self.pause)
        return [(int(p["time"]), int((p.get("value") or [0])[0]))
                for p in (data.get("default") or {}).get("timelineData") or [] if not p.get("isPartial")]


def summarize(series):
    """
    Сводка ряда: рост за 3 месяца (последние 12 недель к 12 до них) и за год
    (последние 8 недель к первым 8). None, если поиск почти нулевой — там
    отношение даёт шум («×3» из 1 в 3).
    """
    v = [x for _t, x in series]
    if len(v) < 30:
        return None
    last12, prev12 = v[-12:], v[-24:-12]
    first8, last8 = v[:8], v[-8:]
    mean = sum(v) / len(v)
    if mean < 5:
        return {"weekly": v, "low": True}

    def ratio(a, b):
        return round((sum(a) / len(a) + 1.0) / (sum(b) / len(b) + 1.0), 2)
    return {"weekly": v, "g3m": ratio(last12, prev12), "g1y": ratio(last8, first8),
            "peak_ago": len(v) - 1 - v.index(max(v)), "low": False}
