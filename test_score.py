# -*- coding: utf-8 -*-
"""
Проверка скоринга на подставных данных: python test_score.py

Зачем синтетика, а не живая база: главное свойство формулы — что ОДНИ И ТЕ ЖЕ
абсолютные числа дают разный балл в зависимости от того, за какое время они
набраны и кто их набрал. На живом потоке такую пару не поймать по заказу,
а ошибиться в знаке или в делителе — легко, и заметно это станет через
неделю молчащих уведомлений.

Каждый случай проверяет ровно одно утверждение и печатает результат.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db          # noqa: E402
import score       # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NOW = int(time.time())
HOUR = 3600
FAILED = []


def check(name, got, want, explain=""):
    ok = bool(got)
    mark = "PASS" if ok == want else "FAIL"
    if ok != want:
        FAILED.append(name)
    print("%-4s %-46s %s" % (mark, name, explain))


def make_item(conn, **kw):
    """Кандидат с разумными значениями по умолчанию."""
    item = {
        "source": kw.get("source", "x"),
        "ext_id": kw.get("ext_id", str(time.time_ns())),
        "url": "https://x.com/u/status/1",
        "product_url": "https://example-product.com",
        "domain": kw.get("domain"),
        "title": kw.get("title", "Introducing Thing"),
        "body": kw.get("body", "we built a thing"),
        "author": kw.get("author"),
        "author_followers": kw.get("followers"),
        "posted_at": kw.get("posted_at", NOW - 3 * HOUR),
        "first_seen": kw.get("first_seen", NOW - 3 * HOUR),
        "tags": kw.get("tags", "launch"),
        "bootstrap": kw.get("bootstrap", 0),
    }
    item_id, _ = db.upsert_item(conn, item)
    for offset, m in kw.get("series", []):
        db.add_metrics(conn, item_id, NOW - offset, m)
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return row


def main():
    tmp = Path(tempfile.mkdtemp()) / "test.sqlite"
    conn = db.connect(tmp)
    print("проверка скоринга, база: %s\n" % tmp)

    # 1. Один и тот же итог, разное время набора.
    fast = make_item(
        conn, ext_id="fast", author="fastguy", followers=3000,
        series=[(3 * HOUR, {"likes": 20, "replies": 2, "bookmarks": 4}),
                (2 * HOUR, {"likes": 150, "replies": 9, "bookmarks": 30}),
                (1 * HOUR, {"likes": 420, "replies": 18, "bookmarks": 95})])
    slow = make_item(
        conn, ext_id="slow", author="slowguy", followers=3000,
        posted_at=NOW - 70 * HOUR, first_seen=NOW - 70 * HOUR,
        series=[(70 * HOUR, {"likes": 300, "replies": 14, "bookmarks": 60}),
                (36 * HOUR, {"likes": 380, "replies": 17, "bookmarks": 80}),
                (1 * HOUR, {"likes": 420, "replies": 18, "bookmarks": 95})])
    s_fast, t_fast, b_fast = score.score_item(conn, fast, NOW)
    s_slow, t_slow, b_slow = score.score_item(conn, slow, NOW)
    check("быстрый набор ценится выше медленного", s_fast > s_slow + 20, True,
          "быстрый %.1f против медленного %.1f при равных 420 лайках" % (s_fast, s_slow))
    check("быстрый доходит до мгновенного уведомления", t_fast == score.HOT, True,
          "уровень=%s, разбор: %s" % (t_fast, list(b_fast)[:3]))

    # 2. Ускорение засчитывается отдельно.
    check("ускорение попало в разбор",
          any("ускоряется" in k for k in b_fast), True,
          "; ".join(k for k in b_fast if "ускор" in k or "темп" in k))

    # 3. Нормировка на аудиторию: те же лайки у большого аккаунта.
    big = make_item(
        conn, ext_id="big", author="bigguy", followers=900000,
        series=[(3 * HOUR, {"likes": 20}), (2 * HOUR, {"likes": 150}),
                (1 * HOUR, {"likes": 420})])
    s_big, _, _ = score.score_item(conn, big, NOW)
    check("те же лайки у большого аккаунта весят меньше", s_big < s_fast, True,
          "900k подписчиков: %.1f против %.1f у 3k" % (s_big, s_fast))

    # 4. Аномалия относительно автора.
    score.update_baseline(conn, "x", "quietguy", [12, 15, 9, 11, 14, 10], NOW)
    quiet = make_item(
        conn, ext_id="quiet", author="quietguy", followers=2000,
        series=[(2 * HOUR, {"likes": 40}), (1 * HOUR, {"likes": 180})])
    s_quiet, _, b_quiet = score.score_item(conn, quiet, NOW)
    check("выстрел у тихого автора замечен",
          any("выше своей нормы" in k for k in b_quiet), True,
          "; ".join(k for k in b_quiet if "норм" in k))

    # 5. Закладки как признак полезности.
    saved = make_item(
        conn, ext_id="saved", author="a1", followers=5000,
        series=[(2 * HOUR, {"likes": 100, "bookmarks": 40}),
                (1 * HOUR, {"likes": 200, "bookmarks": 80})])
    plain = make_item(
        conn, ext_id="plain", author="a2", followers=5000,
        series=[(2 * HOUR, {"likes": 100, "bookmarks": 2}),
                (1 * HOUR, {"likes": 200, "bookmarks": 4})])
    s_saved, _, _ = score.score_item(conn, saved, NOW)
    s_plain, _, _ = score.score_item(conn, plain, NOW)
    check("высокая доля закладок повышает балл", s_saved > s_plain, True,
          "%.1f против %.1f при равных лайках" % (s_saved, s_plain))

    # 6. Холивар штрафуется.
    fight = make_item(
        conn, ext_id="fight", author="a3", followers=5000,
        series=[(2 * HOUR, {"likes": 100, "replies": 60}),
                (1 * HOUR, {"likes": 200, "replies": 130})])
    _, _, b_fight = score.score_item(conn, fight, NOW)
    check("спор в комментариях штрафуется",
          any("спор" in k for k in b_fight), True,
          "; ".join(k for k in b_fight if "спор" in k))

    # 7. Стоп-ниша по правилам владельца.
    banned = make_item(conn, ext_id="banned", author="a4", followers=5000,
                       title="Introducing a new casino app",
                       series=[(1 * HOUR, {"likes": 5000})])
    s_ban, t_ban, b_ban = score.score_item(conn, banned, NOW)
    check("запретная ниша отсекается", s_ban == 0 and t_ban == score.ARCHIVE, True,
          "балл %.1f, причина: %s" % (s_ban, list(b_ban)))

    lending = make_item(conn, ext_id="lend", author="a5", followers=5000,
                        title="Introducing instant loan approvals",
                        series=[(2 * HOUR, {"likes": 100}), (1 * HOUR, {"likes": 400})])
    _, _, b_lend = score.score_item(conn, lending, NOW)
    check("пограничная ниша помечается, но не режется",
          any("под вопросом" in k for k in b_lend), True,
          "; ".join(k for k in b_lend if "вопрос" in k))

    # 8. YC: факт отбора вместо реакции публики.
    yc = make_item(conn, source="yc", ext_id="yc1", title="Async",
                   body="AI agents that run small businesses",
                   posted_at=None, first_seen=NOW - HOUR, tags="Summer 2026")
    s_yc, t_yc, b_yc = score.score_item(conn, yc, NOW)
    check("новая компания YC попадает в сводку", t_yc == score.DIGEST, True,
          "балл %.1f, уровень %s" % (s_yc, t_yc))

    # 9. Посевные записи молчат.
    seeded = make_item(conn, source="yc", ext_id="yc2", title="Old YC co",
                       first_seen=NOW - HOUR, bootstrap=1)
    _, t_seed, _ = score.score_item(conn, seeded, NOW)
    check("посевная запись не уведомляет", t_seed == score.ARCHIVE, True,
          "уровень %s" % t_seed)

    # 10. Свежий домен против старого.
    conn.execute("UPDATE items SET domain = 'newthing.com', domain_age_days = 12 "
                 "WHERE ext_id = 'fast'")
    conn.execute("UPDATE items SET domain = 'oldthing.com', domain_age_days = 2000 "
                 "WHERE ext_id = 'slow'")
    conn.commit()
    f2 = conn.execute("SELECT * FROM items WHERE ext_id='fast'").fetchone()
    s2, _, b2 = score.score_item(conn, f2, NOW)
    check("свежий домен добавляет балл", s2 > s_fast, True,
          "%.1f против %.1f без возраста домена" % (s2, s_fast))

    print()
    if FAILED:
        print("ПРОВАЛЕНО %d: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
