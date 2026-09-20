# -*- coding: utf-8 -*-
"""
launch-scout — прогон: собрать источники, доизмерить старое, оценить, разослать.

    python scout.py                    обычный прогон (ставится на расписание)
    python scout.py --dry              всё то же, но без отправки в Telegram
    python scout.py --sources hn,yc    только эти источники
    python scout.py --digest           отправить сводку по накопленному
    python scout.py --status           что в базе и кто из источников молчит
    python scout.py --init-accounts    разрешить @ники из accounts.txt в id

ПОРЯДОК ШАГОВ ВАЖЕН и повторяет урок tm-scout: сначала сохранить и
измерить, и только потом рассылать. Если разослать раньше записи,
упавшая отправка заставит следующий прогон посчитать всё новым заново.

Повторный замер — не побочная работа, а половина смысла: по одному
снимку нельзя отличить взлетающий пост от лежалого. Поэтому кандидат
моложе суток измеряется на КАЖДОМ прогоне, даже если он уже в базе.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import db                      # noqa: E402
import notify                  # noqa: E402
import score as scoring        # noqa: E402
from common import domain_age_days, load_env, setup_logging  # noqa: E402
from sources import github as gh_src          # noqa: E402
from sources import hn as hn_src              # noqa: E402
from sources import x as x_src                # noqa: E402
from sources import yc as yc_src              # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ALL_SOURCES = ("x", "hn", "yc", "gh")
ACCOUNTS_FILE = ROOT / "accounts.txt"
QUERIES_FILE = ROOT / "queries.txt"
IDS_FILE = ROOT / "data" / "x_account_ids.json"


def _read_list(path):
    """Строки файла без комментариев и пустых."""
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _store(conn, pairs, now, bootstrap=False):
    """Записать кандидатов и их замеры. Возвращает (всего, новых, id новых)."""
    new_ids, total = [], 0
    for item, metrics in pairs:
        if not item.get("ext_id"):
            continue
        total += 1
        if bootstrap:
            item["bootstrap"] = 1
        item_id, is_new = db.upsert_item(conn, item)
        if metrics:
            db.add_metrics(conn, item_id, now, metrics)
        if is_new:
            new_ids.append(item_id)
    conn.commit()
    return total, len(new_ids), new_ids


def collect(conn, sources, now, verbose=True):
    """
    Обойти источники. Падение одного не трогает остальные.

    Первый успешный прогон источника — посевной: всё, что он принёс,
    помечается bootstrap и уведомлений не порождает. Иначе запуск системы
    означал бы разовый залп из сотен сообщений про то, что копилось годами.
    """
    report, seeds = {}, {}

    if "hn" in sources:
        seeds["hn"] = not db.source_seeded(conn, "hn")
        pairs, err = hn_src.fetch(window_hours=48)
        total, new, _ = _store(conn, pairs, now, bootstrap=seeds["hn"])
        db.log_run(conn, now, "hn", total, new, not err, err or "")
        report["hn"] = (total, new, err)

    if "yc" in sources:
        seeds["yc"] = not db.source_seeded(conn, "yc")
        pairs, err = yc_src.fetch()
        total, new, _ = _store(conn, pairs, now, bootstrap=seeds["yc"])
        db.log_run(conn, now, "yc", total, new, not err, err or "")
        report["yc"] = (total, new, err)

    if "gh" in sources:
        seeds["gh"] = not db.source_seeded(conn, "gh")
        pairs, err = gh_src.fetch()
        total, new, _ = _store(conn, pairs, now, bootstrap=seeds["gh"])
        db.log_run(conn, now, "gh", total, new, not err, err or "")
        report["gh"] = (total, new, err)

    if "x" in sources:
        session, err = x_src.session_from_env()
        if not session:
            db.log_run(conn, now, "x", 0, 0, False, err)
            report["x"] = (0, 0, err)
        else:
            seeds["x"] = not db.source_seeded(conn, "x")
            queries = _read_list(QUERIES_FILE)
            ids = _load_account_ids()
            pairs, err = x_src.fetch(session, queries, accounts_ids=ids)
            total, new, _ = _store(conn, pairs, now, bootstrap=seeds["x"])
            db.log_run(conn, now, "x", total, new, not err, err or "")
            report["x"] = (total, new, err)

    conn.commit()
    if verbose:
        for src, (total, new, err) in report.items():
            mark = "!" if err else " "
            seed = " (посевной прогон, без уведомлений)" if seeds.get(src) else ""
            print("%s %-3s собрано %4d, новых %3d%s %s"
                  % (mark, src, total, new, seed, ("— " + err) if err else ""))
    return report


def remeasure(conn, now, max_age_hours=36, verbose=True):
    """
    Доизмерить кандидатов моложе max_age_hours — из этого берётся скорость.

    Считаем от first_seen, а не от posted_at: у части источников времени
    публикации нет вовсе, а момент, когда мы увидели запись, есть всегда.

    Выборка идёт ПО ИСТОЧНИКАМ, каждый со своим лимитом, и YC в неё не
    входит вовсе. Общий LIMIT здесь был ошибкой: у YC записей на порядок
    больше всех прочих, каталог занимал всю квоту целиком, и посты HN и
    GitHub не доизмерялись ни разу — то есть скорость, ради которой всё
    и затевалось, не считалась ни у одного кандидата.
    """
    cutoff = now - max_age_hours * 3600
    # GitHub здесь нет намеренно: его поиск на КАЖДОМ сборе возвращает
    # свежее число звёзд сразу по всем полусотне репозиториев, и это уже
    # готовый замер. Поштучный опрос /repos ничего не добавлял, зато
    # выжигал весь неавторизованный лимит (60 запросов в час на IP) —
    # замерено 2026-09-20, 403 на четвёртом десятке.
    caps = {"hn": 200, "x": 120}
    by_src = {}
    for src, cap in caps.items():
        by_src[src] = conn.execute(
            "SELECT item_id, source, ext_id, title FROM items "
            "WHERE first_seen >= ? AND source = ? "
            "ORDER BY first_seen DESC LIMIT ?", (cutoff, src, cap)).fetchall()

    done, problems = 0, []

    if by_src.get("hn"):
        got, err = hn_src.refresh([r["ext_id"] for r in by_src["hn"]])
        if err:
            problems.append("hn: " + err)
        for r in by_src["hn"]:
            m = got.get(str(r["ext_id"]))
            if m:
                db.add_metrics(conn, r["item_id"], now, m)
                done += 1

    if by_src.get("x"):
        for r in by_src["x"]:
            # Запасной канал (syndication) идёт первым намеренно: он не
            # тратит лимиты аккаунта и не приближает его к блокировке,
            # а для замера скорости лайков и ответов его достаточно.
            m, err = x_src.syndication_metrics(r["ext_id"])
            if m:
                db.add_metrics(conn, r["item_id"], now, m)
                done += 1
            elif err and len(problems) < 4:
                problems.append("x/%s: %s" % (r["ext_id"], err))
            time.sleep(0.35)

    conn.commit()
    if verbose:
        note = ("  — " + "; ".join(problems[:3])) if problems else ""
        print("  доизмерено записей: %d%s" % (done, note))
    return done


def refresh_baselines(conn, now, min_posts=5):
    """
    Пересчитать норму авторов по тому, что уже накоплено в базе.

    Первые дни норм не будет почти ни у кого — это ожидаемо: слой
    «аномалия автора» включается сам, когда наберётся история.
    """
    rows = conn.execute(
        "SELECT i.source, i.author, "
        "       (SELECT MAX(m.likes) FROM metrics m WHERE m.item_id = i.item_id) mx "
        "  FROM items i "
        " WHERE i.author IS NOT NULL AND i.first_seen >= ? ",
        (now - 90 * 86400,)).fetchall()
    grouped = {}
    for r in rows:
        if r["mx"] is None:
            continue
        grouped.setdefault((r["source"], r["author"]), []).append(r["mx"])
    n = 0
    for (src, author), values in grouped.items():
        if len(values) >= min_posts:
            if scoring.update_baseline(conn, src, author, values, now):
                n += 1
    conn.commit()
    return n


def enrich_domains(conn, now, limit=40):
    """
    Возраст домена по RDAP — только для кандидатов, у которых уже есть
    хоть какой-то отклик. RDAP бесплатный, но медленный, и тратить его
    на весь поток незачем.
    """
    rows = conn.execute(
        "SELECT i.item_id, i.domain FROM items i "
        " WHERE i.domain IS NOT NULL AND i.domain_age_days IS NULL "
        "   AND i.first_seen >= ? "
        " ORDER BY i.first_seen DESC LIMIT ?", (now - 7 * 86400, limit)).fetchall()
    n = 0
    for r in rows:
        age = domain_age_days(conn, r["domain"], now)
        if age is not None:
            conn.execute("UPDATE items SET domain_age_days = ? WHERE item_id = ?",
                         (age, r["item_id"]))
            n += 1
    conn.commit()
    return n


def evaluate(conn, now, window_hours=72):
    """Оценить всё свежее. Возвращает список (item, метрики, балл, уровень, разбор)."""
    rows = conn.execute(
        "SELECT * FROM items WHERE first_seen >= ? ORDER BY first_seen DESC",
        (now - window_hours * 3600,)).fetchall()
    out = []
    for item in rows:
        total, tier, breakdown = scoring.score_item(conn, item, now)
        scoring.save_score(conn, item["item_id"], now, total, tier, breakdown)
        last = conn.execute(
            "SELECT * FROM metrics WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
            (item["item_id"],)).fetchone()
        out.append((item, dict(last) if last else {}, total, tier, breakdown))
    conn.commit()
    out.sort(key=lambda t: -t[2])
    return out


def dispatch(conn, evaluated, now, dry=False, digest=False, verbose=True):
    """Разослать горячее сразу; сводку — когда позвали с --digest."""
    hot = [e for e in evaluated if e[3] == scoring.HOT
           and not db.already_sent(conn, e[0]["item_id"], scoring.HOT)]
    messages, ids = [], []
    for item, metrics, total, tier, breakdown in hot:
        messages.append(notify.format_item(item, metrics, total, tier, breakdown))
        ids.append((item["item_id"], scoring.HOT))

    if digest:
        pool = [e for e in evaluated if e[3] == scoring.DIGEST
                and not db.already_sent(conn, e[0]["item_id"], scoring.DIGEST)][:12]
        if pool:
            head = "📋 <b>Сводка: %d кандидатов</b>" % len(pool)
            body = []
            for item, metrics, total, tier, _b in pool:
                body.append("<code>%s</code> %s — <a href=\"%s\">%s</a>"
                            % (total, notify._esc((item["title"] or "")[:70]),
                               item["url"], notify.SRC_RU.get(item["source"], item["source"])))
                ids.append((item["item_id"], scoring.DIGEST))
            messages.append(head + "\n\n" + "\n".join(body))

    if not messages:
        if verbose:
            print("  отправлять нечего")
        return 0

    if dry:
        if verbose:
            print("  --dry: не отправлено, %d сообщений готово" % len(messages))
            for m in messages[:3]:
                print("  " + "-" * 60)
                print(m[:700])
        return 0

    ok, errs = notify.send_batch(messages)
    if ok:
        for item_id, tier in ids:
            db.mark_sent(conn, item_id, tier, now)
        conn.commit()
    if verbose:
        print("  отправлено: %d/%d %s" % (ok, len(messages),
                                          ("— " + "; ".join(errs[:2])) if errs else ""))
    return ok


def top_items(conn, n=10, window_hours=72, now=None, skip_sent=False):
    """
    Лучшее из накопленного прямо сейчас, без ожидания порога.

    Нужно для выдачи по требованию — кнопкой в боте или `--top`. Порог тут
    намеренно не применяется: владелец сам спросил «покажи, что есть»,
    и ответ «ничего не дотянуло до 58» на такой вопрос бесполезен.

    Один домен встречается в выдаче один раз: без этого десятка мест
    уходит на один и тот же продукт, замеченный в трёх источниках.
    """
    now = now or int(time.time())
    rows = conn.execute(
        "SELECT * FROM items WHERE first_seen >= ? ORDER BY first_seen DESC",
        (now - window_hours * 3600,)).fetchall()
    scored = []
    for item in rows:
        if skip_sent and db.already_sent(conn, item["item_id"], "ondemand"):
            continue
        total, tier, breakdown = scoring.score_item(conn, item, now)
        if total <= 0:
            continue
        last = conn.execute(
            "SELECT * FROM metrics WHERE item_id = ? ORDER BY ts DESC LIMIT 1",
            (item["item_id"],)).fetchone()
        scored.append((total, item, dict(last) if last else {}, tier, breakdown))
    scored.sort(key=lambda t: -t[0])

    out, seen_domains = [], set()
    for row in scored:
        d = row[1]["domain"]
        if d and d in seen_domains:
            continue
        if d:
            seen_domains.add(d)
        out.append(row)
        if len(out) >= n:
            break
    return out


def send_top(conn, n=10, now=None, dry=False, mark=False):
    """Отправить выдачу по требованию. Возвращает (сколько ушло, ошибки)."""
    now = now or int(time.time())
    rows = top_items(conn, n=n, now=now)
    if not rows:
        return 0, ["в базе пока нечего показывать"]
    messages = [notify.format_item(it, m, total, tier, b)
                for total, it, m, tier, b in rows]
    if dry:
        for msg in messages[:2]:
            print("-" * 60)
            print(msg)
        return 0, []
    ok, errs = notify.send_batch(messages)
    if mark and ok:
        for _total, it, _m, _tier, _b in rows:
            db.mark_sent(conn, it["item_id"], "ondemand", now)
        conn.commit()
    return ok, errs


def _load_account_ids():
    import json
    if IDS_FILE.exists():
        try:
            return list(json.loads(IDS_FILE.read_text(encoding="utf-8")).values())
        except ValueError:
            return []
    return []


def init_accounts():
    """Разрешить @ники из accounts.txt в числовые id и запомнить их."""
    import json
    load_env()
    session, err = x_src.session_from_env()
    if not session:
        print("нельзя: " + err)
        return 1
    names = [n.lstrip("@") for n in _read_list(ACCOUNTS_FILE)]
    known = {}
    if IDS_FILE.exists():
        try:
            known = json.loads(IDS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            known = {}
    added, failed = 0, []
    for n in names:
        if n in known:
            continue
        uid, err = session.user_id(n)
        if uid:
            known[n] = uid
            added += 1
            print("  @%-22s -> %s" % (n, uid))
        else:
            failed.append("%s (%s)" % (n, err))
        time.sleep(1.2)
    IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    IDS_FILE.write_text(json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8")
    print("готово: +%d, всего %d" % (added, len(known)))
    if failed:
        print("не разрешились: " + ", ".join(failed[:6]))
    return 0


def status():
    """Что в базе и кто из источников молчал в последний раз."""
    load_env()
    conn = db.connect()
    now = int(time.time())
    total = conn.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    day = conn.execute("SELECT COUNT(*) n FROM items WHERE first_seen >= ?",
                       (now - 86400,)).fetchone()["n"]
    print("кандидатов всего: %d, за сутки: %d" % (total, day))
    print("по источникам:")
    for r in conn.execute(
            "SELECT source, COUNT(*) n FROM items GROUP BY source ORDER BY n DESC"):
        print("  %-3s %6d" % (r["source"], r["n"]))
    print("последний прогон каждого источника:")
    for r in conn.execute(
            "SELECT source, MAX(ts) ts, found, new_rows, ok, note FROM runs "
            "GROUP BY source ORDER BY source"):
        ago = (now - r["ts"]) / 60.0
        mark = "ок " if r["ok"] else "СБОЙ"
        print("  %-3s %s %5.0f мин назад, найдено %s %s"
              % (r["source"], mark, ago, r["found"], (r["note"] or "")[:90]))
    top = conn.execute(
        "SELECT i.title, s.score, i.url FROM scores s JOIN items i USING (item_id) "
        "WHERE s.ts >= ? ORDER BY s.score DESC LIMIT 5", (now - 86400,)).fetchall()
    if top:
        print("топ за сутки:")
        for r in top:
            print("  %5.1f  %s" % (r["score"], (r["title"] or "")[:70]))
    conn.close()
    return 0


def run(sources, dry=False, digest=False):
    load_env()
    conn = db.connect()
    now = int(time.time())
    print("прогон %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    collect(conn, sources, now)
    remeasure(conn, now)
    n = refresh_baselines(conn, now)
    if n:
        print("  норм автора обновлено: %d" % n)
    d = enrich_domains(conn, now)
    if d:
        print("  возраст домена уточнён: %d" % d)
    evaluated = evaluate(conn, now)
    hot = sum(1 for e in evaluated if e[3] == scoring.HOT)
    dig = sum(1 for e in evaluated if e[3] == scoring.DIGEST)
    print("  оценено %d: горячих %d, в сводку %d" % (len(evaluated), hot, dig))
    dispatch(conn, evaluated, now, dry=dry, digest=digest)
    conn.close()
    return 0


def main():
    # ПЕРВЫМ делом, до любого print: по расписанию нас запускает pythonw,
    # у которого stdout равен None.
    setup_logging("scout")
    ap = argparse.ArgumentParser(description="launch-scout — поиск свежих запусков")
    ap.add_argument("--sources", default=",".join(ALL_SOURCES),
                    help="источники через запятую: x,hn,yc,gh")
    ap.add_argument("--dry", action="store_true", help="не отправлять в Telegram")
    ap.add_argument("--digest", action="store_true", help="отправить сводку")
    ap.add_argument("--status", action="store_true", help="состояние базы и источников")
    ap.add_argument("--init-accounts", action="store_true",
                    help="разрешить @ники из accounts.txt в id")
    ap.add_argument("--top", type=int, metavar="N",
                    help="прислать N лучших из накопленного, минуя пороги")
    args = ap.parse_args()

    if args.top:
        load_env()
        conn = db.connect()
        ok, errs = send_top(conn, n=args.top, dry=args.dry)
        print("отправлено: %d %s" % (ok, ("— " + "; ".join(errs[:2])) if errs else ""))
        conn.close()
        return 0
    if args.status:
        return status()
    if args.init_accounts:
        return init_accounts()
    srcs = [s.strip() for s in args.sources.split(",") if s.strip() in ALL_SOURCES]
    if not srcs:
        print("не указан ни один известный источник")
        return 2
    return run(srcs, dry=args.dry, digest=args.digest)


if __name__ == "__main__":
    sys.exit(main())
