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

    per_source_ceilings(conn)

    print()
    if FAILED:
        print("ПРОВАЛЕНО %d: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("все проверки пройдены")
    return 0


def seed_distribution(conn, source, rates, age_h):
    """
    Фон площадки: записи с заданными темпами, как на реальной неделе.

    Без фона сравнивать не с чем: слагаемое «быстрее N% площадки»
    требует хотя бы PCT_MIN_SAMPLE записей того же источника.
    """
    for i, r in enumerate(rates):
        likes = r * (age_h / 24.0 if source == "gh" else age_h)
        make_item(conn, source=source, ext_id="bg-%s-%d" % (source, i),
                  author="bg%d" % i, posted_at=NOW - int(age_h * HOUR),
                  first_seen=NOW - int(age_h * HOUR), tags="show",
                  series=[(0, {"likes": int(round(likes))})])


def per_source_ceilings(conn):
    """
    ГЕЙТ (lesson 2026-09-20, RECURRED 2026-09-25): для КАЖДОГО источника —
    сильный реалистичный пример доходит до мгновенного уведомления, средний
    не доходит даже до сводки.

    Первая версия теста проверяла потолок только на твитах. У HN и GitHub
    нет ни размера аудитории, ни закладок — и пять дней боевой работы их
    лучший балл был 3.4 и 22 при пороге 58: ни одного уведомления. Источник
    без своей пары примеров здесь = порог для него не проверен.

    Фоновые распределения повторяют реальные квантили, снятые 2026-09-25:
    HN — медиана 0.17 очка/час, p90 0.96, p97 2.7, p99 3.5;
    GitHub — медиана 163 звезды/сутки, p90 740, p97 2900, p99 6075.
    """
    print("\n--- потолок по каждому источнику ---")
    hn_bg = ([0.05] * 40 + [0.17] * 30 + [0.5] * 15 + [0.96] * 8
             + [2.0] * 3 + [2.7] * 2 + [3.5] * 2)
    gh_bg = ([40] * 20 + [163] * 15 + [400] * 8 + [740] * 4 + [2900] * 2 + [6075])
    seed_distribution(conn, "hn", hn_bg, age_h=6)
    seed_distribution(conn, "gh", gh_bg, age_h=240)

    # HN: Show HN на первой полосе — 45 очков за 2 часа и растёт.
    hn_hot = make_item(
        conn, source="hn", ext_id="hn-hot", author="maker1", tags="show",
        posted_at=NOW - 2 * HOUR, first_seen=NOW - 2 * HOUR,
        series=[(90 * 60, {"likes": 9}), (60 * 60, {"likes": 18}),
                (30 * 60, {"likes": 31}), (5 * 60, {"likes": 45})])
    s, tier, b = score.score_item(conn, hn_hot, NOW)
    check("HN: взлетающий Show HN — мгновенно", tier == score.HOT, True,
          "%.1f %s" % (s, "; ".join(list(b)[:3])))

    # HN: обычный Show HN — 3 очка за 3 часа.
    hn_mid = make_item(
        conn, source="hn", ext_id="hn-mid", author="maker2", tags="show",
        posted_at=NOW - 3 * HOUR, first_seen=NOW - 3 * HOUR,
        series=[(2 * HOUR, {"likes": 1}), (10 * 60, {"likes": 3})])
    s, tier, _ = score.score_item(conn, hn_mid, NOW)
    check("HN: рядовой Show HN — не дальше архива", tier == score.ARCHIVE, True,
          "%.1f" % s)

    # HN: Launch HN — компания YC выходит на публику. В день запуска это
    # мгновенное уведомление даже без отклика: прямой ответ на заказ.
    launch_quiet = make_item(
        conn, source="hn", ext_id="hn-launch", author="yc-founder", tags="launch",
        title="Launch HN: Acme (YC S26) — invoices for plumbers",
        posted_at=NOW - 2 * HOUR, first_seen=NOW - 2 * HOUR,
        series=[(60 * 60, {"likes": 2}), (5 * 60, {"likes": 3})])
    s, tier, _ = score.score_item(conn, launch_quiet, NOW)
    check("HN: Launch HN в день запуска — мгновенно", tier == score.HOT, True, "%.1f" % s)

    # HN: тот же Launch HN, но четырёхдневной давности и тихий — в архив,
    # иначе старые запуски висели бы в выдаче вечно.
    launch_old = make_item(
        conn, source="hn", ext_id="hn-launch-old", author="yc-founder3", tags="launch",
        title="Launch HN: Oldco (YC W26)",
        posted_at=NOW - 96 * HOUR, first_seen=NOW - 96 * HOUR,
        series=[(90 * HOUR, {"likes": 3}), (5 * 60, {"likes": 4})])
    s, tier, _ = score.score_item(conn, launch_old, NOW)
    check("HN: старый тихий Launch HN — не мгновенно", tier != score.HOT, True,
          "%.1f %s" % (s, tier))

    # GitHub: репозиторию двое суток, 12 тысяч звёзд, набирает 250 в час.
    gh_hot = make_item(
        conn, source="gh", ext_id="gh-hot", author="org1", tags="python",
        posted_at=NOW - 48 * HOUR, first_seen=NOW - 3 * HOUR,
        series=[(3 * HOUR, {"likes": 11250}), (2 * HOUR, {"likes": 11500}),
                (1 * HOUR, {"likes": 11750}), (5 * 60, {"likes": 12000})])
    s, tier, b = score.score_item(conn, gh_hot, NOW)
    check("GitHub: взлетающий репозиторий — мгновенно", tier == score.HOT, True,
          "%.1f %s" % (s, "; ".join(list(b)[:3])))

    # GitHub: 20 дней, 3 тысячи звёзд — середина выдачи.
    gh_mid = make_item(
        conn, source="gh", ext_id="gh-mid", author="org2", tags="go",
        posted_at=NOW - 20 * 24 * HOUR, first_seen=NOW - 3 * HOUR,
        series=[(3 * HOUR, {"likes": 2990}), (5 * 60, {"likes": 3000})])
    s, tier, _ = score.score_item(conn, gh_mid, NOW)
    check("GitHub: рядовой репозиторий — не дальше архива", tier == score.ARCHIVE, True,
          "%.1f" % s)

    # YC: новичок каталога — сводка; он же с подтверждением — мгновенно.
    yc_new = make_item(conn, source="yc", ext_id="yc-new", title="Acme",
                       domain="acme-invoices.com", posted_at=None,
                       first_seen=NOW - HOUR, tags="Summer 2026")
    s, tier, _ = score.score_item(conn, yc_new, NOW)
    check("YC: новичок батча — сводка", tier == score.DIGEST, True, "%.1f" % s)

    make_item(conn, source="hn", ext_id="hn-launch-acme", tags="launch",
              domain="acme-invoices.com", author="yc-founder2",
              title="Launch HN: Acme (YC S26)", posted_at=NOW - HOUR,
              first_seen=NOW - HOUR, series=[(5 * 60, {"likes": 4})])
    yc_new = conn.execute("SELECT * FROM items WHERE ext_id = 'yc-new'").fetchone()
    s, tier, b = score.score_item(conn, yc_new, NOW)
    check("YC: новичок, подтверждённый Launch HN — мгновенно", tier == score.HOT, True,
          "%.1f %s" % (s, "; ".join(list(b)[:3])))


if __name__ == "__main__":
    sys.exit(main())
