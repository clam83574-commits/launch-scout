# -*- coding: utf-8 -*-
"""
Глубокий поиск по запросу из чата (2026-10-01): бот кладёт задание, GitHub
Actions запускает этот скрипт, он ищет по фразам там, куда из Cloudflare не
достать, — X (куки аккаунта есть только тут) и Google Trends (из Cloudflare
отвечает 429), — и отдаёт находки боту, а тот присылает дополнение в чат.

Репозиторий публичный, логи Actions видны всем: в запуск передаётся только
номер задания, а сам вопрос и находки в лог не пишутся.

    python research.py --job <id>
"""
import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKER = os.environ.get("WORKER_URL", "https://launch-scout-bot.clam83574.workers.dev")


def _hdr():
    return {"x-ingest-secret": os.environ.get("LS_INGEST_SECRET", ""), "content-type": "application/json"}


def x_search(queries):
    from sources import x
    sess, err = x.session_from_env()
    if not sess:
        return [], err
    out, seen, errs = [], set(), []
    for q in queries[:3]:
        for product in ("Top", "Latest"):
            tweets, e = sess.search(q + " -filter:replies", limit=20, product=product, pages=1)
            if e:
                errs.append(e[:60])
                if "429" in e:
                    return out, "429"
                continue
            for t in tweets:
                if t["id"] in seen or t.get("is_retweet") or x.is_noise(t.get("text")):
                    continue
                seen.add(t["id"])
                out.append({"text": (t.get("text") or "")[:400], "likes": t.get("likes") or 0,
                            "replies": t.get("replies") or 0, "views": t.get("views"),
                            "followers": t.get("followers"), "who": t.get("screen_name"),
                            "ts": x._parse_twitter_time(t.get("created_at")),
                            "url": "https://x.com/%s/status/%s" % (t.get("screen_name") or "i", t["id"])})
    out.sort(key=lambda t: -(t["likes"] + 3 * t["replies"]))
    return out[:25], ("; ".join(errs[:2]) or None)


def trends(terms):
    from sources import gtrends
    t, out = gtrends.Trends(pause=2.5), []
    for term in terms[:3]:
        row = {"term": term}
        for geo in ("", "KZ"):
            try:
                series = t.weekly(term, geo=geo)
            except gtrends.Blocked:
                return out + [row], "429"
            except Exception as e:      # сеть, формат
                row["err_" + (geo or "world")] = str(e)[:60]
                continue
            s = gtrends.summarize(series) or {}
            row[geo or "world"] = {k: s.get(k) for k in ("g3m", "g1y", "peak_ago", "low")}
            row[(geo or "world") + "_weekly"] = [v for _ts, v in series][-26:]
        out.append(row)
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    a = ap.parse_args()
    r = requests.get(WORKER + "/research-job", params={"id": a.job}, headers=_hdr(), timeout=30)
    if r.status_code != 200:
        print("задание не получено: HTTP %d" % r.status_code)
        return 1
    job = r.json()
    queries = [q for q in job.get("queries") or [] if q][:3]
    t0 = time.time()
    tweets, xerr = x_search(queries)
    tr, terr = trends(job.get("terms") or queries)
    res = {"id": a.job, "x": tweets, "x_err": xerr, "trends": tr, "trends_err": terr}
    # В лог — только счётчики, без текста вопроса и находок (публичный репозиторий).
    print("X: %d постов%s; Google Trends: %d фраз%s; %.0f с" % (
        len(tweets), " (ошибка)" if xerr else "", len(tr), " (429)" if terr else "", time.time() - t0))
    p = requests.post(WORKER + "/research-result", data=json.dumps(res, ensure_ascii=False).encode("utf-8"),
                      headers=_hdr(), timeout=60)
    print("бот: HTTP %d" % p.status_code)
    return 0 if p.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
