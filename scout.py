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
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import ai                      # noqa: E402
import brief                   # noqa: E402
import db                      # noqa: E402
import market                  # noqa: E402
import notify                  # noqa: E402
import os                      # noqa: E402
import score as scoring        # noqa: E402
from common import domain_age_days, load_env, setup_logging  # noqa: E402
from sources import github as gh_src          # noqa: E402
from sources import hn as hn_src              # noqa: E402
from sources import ph as ph_src              # noqa: E402
from sources import x as x_src                # noqa: E402
from sources import yc as yc_src              # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ALL_SOURCES = ("x", "hn", "yc", "gh", "ph")
ACCOUNTS_FILE = ROOT / "accounts.txt"
QUERIES_FILE = ROOT / "queries.txt"
# В корне, а не в data/: файл лежит в репозитории, иначе облачный прогон
# его не увидит (data/ — это кэш Actions, туда ничего не кладут руками).
# Числовые id аккаунтов публичны, секрета в них нет.
IDS_FILE = ROOT / "x_account_ids.json"

# Бюджет запросов к X с одного аккаунта. При прогоне раз в 10 минут все
# поиски и все 33 ленты разом давали бы ~6000 запросов в сутки — аккаунт
# ограничили бы в первый же день. Поиски идут каждый прогон (из них и
# приходят свежие запуски), ленты — по кругу срезами. Итого около 20
# запросов в 10 минут: темп активного живого пользователя.
#
# 2026-09-28: X — самая массовая площадка, а находок из неё приходило меньше,
# чем из HN. Поиски теперь идут по кругу из большого пула (queries.txt):
# «ядро» каждый прогон на две страницы, остальное — срезом. Лимит поиска у
# веб-клиента X около 50 запросов в 15 минут, поэтому за прогон (раз в 10
# минут) — не больше ~20 поисков: 4 ядра × 2 страницы + 12 по кругу.
X_ACCOUNTS_PER_RUN = 16
X_SEARCHES_PER_RUN = 12
X_COOLDOWN_SECONDS = 30 * 60


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

    if "ph" in sources:
        # Голоса Product Hunt приходят свежими при каждом сборе (с PH_TOKEN) —
        # это и есть замер; отдельного доизмерения не нужно.
        seeds["ph"] = not db.source_seeded(conn, "ph")
        pairs, err = ph_src.fetch()
        total, new, _ = _store(conn, pairs, now, bootstrap=seeds["ph"])
        db.log_run(conn, now, "ph", total, new, not err, err or "")
        report["ph"] = (total, new, err)

    if "x" in sources:
        session, err = x_src.session_from_env()
        if not session:
            db.log_run(conn, now, "x", 0, 0, False, err)
            report["x"] = (0, 0, err)
        elif int(db.kv_get(conn, "x_cooldown_until", 0) or 0) > now:
            left = (int(db.kv_get(conn, "x_cooldown_until", 0)) - now) // 60
            note = "пауза после 429 ещё %d мин — аккаунт бережём" % left
            db.log_run(conn, now, "x", 0, 0, True, note)
            report["x"] = (0, 0, None)
            if verbose:
                print("  x   " + note)
        else:
            seeds["x"] = not db.source_seeded(conn, "x")
            queries = _pick_queries(conn, _read_list(QUERIES_FILE))
            ids = _load_account_ids()
            # Ленты — по кругу: каждый прогон следующий срез списка.
            off = int(db.kv_get(conn, "x_acc_offset", 0) or 0) % max(len(ids), 1)
            part = (ids[off:] + ids[:off])[:X_ACCOUNTS_PER_RUN]
            db.kv_set(conn, "x_acc_offset", off + len(part))
            pairs, demand, raises, err = x_src.fetch(session, queries, accounts_ids=part)
            n_mrr = market.store_x_traction(conn, raises + (x_src.fetch.last_mrr or []), now)
            if err and err.startswith("429"):
                db.kv_set(conn, "x_cooldown_until", now + X_COOLDOWN_SECONDS)
            total, new, _ = _store(conn, pairs, now, bootstrap=seeds["x"])
            n_dem = market.store_demand(conn, demand, now) if demand else 0
            n_raise = market.store_x_raises(conn, raises, now) if raises else 0
            st = x_src.fetch.last_stats or {}
            note = "запросов %s, постов %s, запусков %s, спрос +%d, раундов %d, выручка +%d" % (
                st.get("calls", "?"), st.get("tweets", "?"), st.get("launches", "?"), n_dem, n_raise, n_mrr)
            db.log_run(conn, now, "x", total, new, not err, err or note)
            report["x"] = (total, new, err)
            if verbose:
                print("    x: " + note)

    conn.commit()
    if verbose:
        for src, (total, new, err) in report.items():
            mark = "!" if err else " "
            seed = " (посевной прогон, без уведомлений)" if seeds.get(src) else ""
            print("%s %-3s собрано %4d, новых %3d%s %s"
                  % (mark, src, total, new, seed, ("— " + err) if err else ""))
    return report


def _pick_queries(conn, lines):
    """
    Поиски X на этот прогон: всё «ядро» (строки с !) плюс следующий срез
    остальных по кругу. Смещение хранится в базе, как у лент аккаунтов.
    """
    core = [q for q in lines if x_src.parse_query(q)["core"]]
    rest = [q for q in lines if not x_src.parse_query(q)["core"]]
    if not rest:
        return core
    off = int(db.kv_get(conn, "x_q_offset", 0) or 0) % len(rest)
    part = (rest[off:] + rest[:off])[:X_SEARCHES_PER_RUN]
    db.kv_set(conn, "x_q_offset", off + len(part))
    return core + part


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
    # У X окно короче и квота меньше: скорость твита важна в первые часы,
    # а каждый замер — отдельный запрос с адреса раннера. 120 замеров раз в
    # 10 минут — это 17 тысяч запросов в сутки ради хвоста, который уже
    # ничего не решает.
    #
    # 2026-09-28: X поднят до 120 замеров за сутки. Замер идёт через
    # syndication — без кук и без лимитов аккаунта, — а без повторных
    # замеров у твита нет ни темпа, ни ускорения: из 38 постов X в срезе
    # 2026-09-27 у большинства был один-единственный замер.
    caps = {"hn": (200, cutoff), "x": (120, now - 24 * 3600)}
    by_src = {}
    # Уже измеренное в ЭТОМ прогоне (сбор только что принёс свежие цифры)
    # повторно не замеряем: это лишние запросы, а раньше ещё и затирание
    # полного замера урезанным.
    for src, (cap, since) in caps.items():
        by_src[src] = conn.execute(
            "SELECT item_id, source, ext_id, title FROM items i "
            "WHERE first_seen >= ? AND source = ? "
            "  AND NOT EXISTS (SELECT 1 FROM metrics m "
            "                  WHERE m.item_id = i.item_id AND m.ts = ?) "
            "ORDER BY first_seen DESC LIMIT ?", (since, src, now, cap)).fetchall()

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
            time.sleep(0.25)

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


def _meta_of(conn, item_id):
    """
    Темы, аудитория и выжимка находки: (topics, audience, gist_by_lang).
    Нужны Worker'у для фильтра по личным категориям и типу аудитории.
    """
    try:
        row = conn.execute("SELECT topics, audience, gist FROM item_topics WHERE item_id = ?",
                           (item_id,)).fetchone()
    except Exception:
        row = None
    if not row:
        return [], [], {}
    def _j(v, default):
        try:
            return json.loads(v) if v else default
        except ValueError:
            return default
    topics = [x for x in _j(row["topics"], []) if x != "other"]
    return topics, _j(row["audience"], []), _j(row["gist"], {})


def _topics_of(conn, item_id):
    return _meta_of(conn, item_id)[0]


def _sectors_of(conn, item):
    """Секторы находки (market.SECTORS) — для личных фильтров подписчиков."""
    topics = _meta_of(conn, item["item_id"])[0]
    return market.sectors_for(topics, "%s %s" % (item["title"] or "", item["body"] or ""))


# Порог «заметного»: ниже горячего, но выше сводки. Такие находки уходят
# сразу тем, кто выбрал чувствительность «всё заметное» (Worker решает по
# личному порогу подписчика: 70 / 58 / 48).
WARM_MIN = 48.0
TOY = "ИИ: скорее игрушка, чем бизнес"


def _pushable(e):
    item, _m, total, tier, breakdown = e
    if TOY in breakdown:
        return False       # ИИ счёл игрушкой — только в сводку, не в пуш
    # Каталог YC «заметным» не пушится: у каждого новичка батча ровно 54, и
    # обновление каталога прислало бы полсотни сообщений разом (на пробном
    # прогоне 2026-09-28 — 16 штук за раз). Им место в сводке.
    # «Заметное» тоже только с живым откликом: без этого оно обходило бы
    # порог пуша, и три лайка снова прилетали бы уведомлением.
    likes = (_m or {}).get("likes") or 0
    funded = any(k.startswith("раунд инвесторов") for k in breakdown)
    real = funded or likes >= scoring.TRACTION_FULL.get(item["source"], 10 ** 9)
    warm = item["source"] != "yc" and tier == scoring.DIGEST and total >= WARM_MIN and real
    return tier == scoring.HOT or warm


DIGEST_SIZE = 12
DIGEST_PER_SOURCE = 4


def _diverse(cands, size=DIGEST_SIZE, per_source=DIGEST_PER_SOURCE):
    """
    Лучшие по баллу, но не больше per_source строк от одного источника.

    Без этого сводку забивал каталог YC: у новичка батча ровно 54 балла, и
    десяток таких выдавливал любой пост X с 42–53 — в живом срезе 2026-09-27
    X было 79 находок из 328, а в сводку не попадало ни одной. Если другим
    источникам нечего дать, места добираются лучшими из оставшихся.
    """
    picked, rest, count = [], [], {}
    for e in cands:
        src = e[0]["source"]
        if count.get(src, 0) < per_source:
            picked.append(e)
            count[src] = count.get(src, 0) + 1
        else:
            rest.append(e)
    picked = picked[:size]
    picked += rest[:size - len(picked)]
    return sorted(picked, key=lambda e: -e[2])


def dispatch(conn, evaluated, now, dry=False, digest=False, verbose=True):
    """
    Разослать горячее сразу; сводку — когда пришло её время.

    Рассылает Worker (notify.deliver): он знает подписчиков и их личные
    категории, поэтому к каждой находке прикладываются её темы. Без Worker
    всё уходит владельцу напрямую, как раньше.
    """
    # Пост из X или HN уходит в пуш только после ИИ-проверки «запуск ли это
    # вообще»: первый же прогон с широкими запросами X (2026-09-29) разослал
    # патч игры, пост «к нам присоединился такой-то» и цитату-мнение — у всех
    # по сотне лайков, но разбор до них ещё не дошёл. Непроверенное ждёт
    # следующего прогона, разбор идёт по горячим первыми.
    ai_on = ai.available()[0]
    hot = [e for e in evaluated if _pushable(e)
           and not db.already_sent(conn, e[0]["item_id"], scoring.HOT)
           and not (ai_on and e[0]["source"] in ("x", "hn") and ai.get_note(conn, e[0]["item_id"]) is None)]
    hot_payload, ids = [], []
    for item, metrics, total, tier, breakdown in hot:
        topics, audience, gist = _meta_of(conn, item["item_id"])
        raw = ai.get_note(conn, item["item_id"])
        secs = _sectors_of(conn, item)
        texts = {lang: notify.format_item(item, metrics, total, tier, breakdown,
                                          note=ai.note_for(raw, lang), gist=gist.get(lang),
                                          lang=lang, sectors=secs)
                 for lang in ai.LANGS}
        # Адреса отдельно от текста: Worker ставит их кнопками под
        # уведомлением — ссылки в конце текста на телефоне не замечали.
        # Балл, источник и секторы — для личных фильтров: Worker шлёт
        # каждому только то, что проходит его порог, секторы и источники.
        hot_payload.append({"id": item["item_id"], "texts": texts, "text": texts["ru"],
                            "topics": topics, "audience": audience,
                            "sectors": secs, "source": item["source"],
                            "score": total, "tier": tier,
                            "url": item["url"], "product_url": item["product_url"]})
        ids.append((item["item_id"], scoring.HOT))

    digest_payload = None
    if digest:
        pool = _diverse([e for e in evaluated if e[3] == scoring.DIGEST
                         and not db.already_sent(conn, e[0]["item_id"], scoring.DIGEST)])
        if pool:
            lines = []
            for item, metrics, total, tier, _b in pool:
                topics, audience, gist = _meta_of(conn, item["item_id"])
                src = notify.SRC_RU.get(item["source"], item["source"])
                by_lang = {}
                for lang in ai.LANGS:
                    text = gist.get(lang) or (item["title"] or "")
                    by_lang[lang] = "<code>%s</code> <b>%s</b> — %s — <a href=\"%s\">%s</a>" % (
                        total, notify._esc((item["title"] or "")[:60]),
                        notify._esc(text[:140]), notify._esc(item["url"] or ""), src)
                lines.append({"id": item["item_id"], "lines": by_lang, "line": by_lang["ru"],
                              "topics": topics, "audience": audience, "score": total,
                              "sectors": _sectors_of(conn, item), "source": item["source"],
                              "pushed": _pushable((item, metrics, total, tier, _b))})
                ids.append((item["item_id"], scoring.DIGEST))
            digest_payload = {"head": "📋 <b>Сводка</b>",
                              "heads": {"ru": "📋 <b>Сводка</b>", "kk": "📋 <b>Шолу</b>",
                                        "en": "📋 <b>Digest</b>"},
                              "items": lines}

    if not hot_payload and not digest_payload:
        if verbose:
            print("  отправлять нечего")
        return 0

    if dry:
        if verbose:
            print("  --dry: не отправлено; горячих %d, в сводке %d"
                  % (len(hot_payload), len((digest_payload or {}).get("items", []))))
            for h in hot_payload[:2]:
                print("  " + "-" * 60)
                print(h["texts"]["ru"][:700])
        return 0

    # Что именно уходит — в лог прогона: на вопрос «почему не было постов
    # из X» иначе нечем ответить, кроме догадок (2026-09-27).
    if verbose:
        for item, _m, total, _t, _b in hot:
            print("  горячее: [%s] %.1f %s" % (item["source"], total, (item["title"] or "")[:80]))
        if digest_payload:
            by_src = {}
            for e in pool:
                by_src[e[0]["source"]] = by_src.get(e[0]["source"], 0) + 1
            print("  в сводке: " + ", ".join("%s %d" % kv for kv in sorted(by_src.items())))
    ok, err = notify.deliver(hot=hot_payload, digest=digest_payload)
    if ok:
        for item_id, tier in ids:
            db.mark_sent(conn, item_id, tier, now)
        if any(t == scoring.DIGEST for _i, t in ids):
            db.kv_set(conn, "last_digest", now)
        conn.commit()
    if verbose:
        print("  отправлено сообщений: %d %s" % (ok, ("— " + err) if err else ""))
    return ok


# Окна сводки по UTC: 07-09 и 19-21 — это 10-12 и 22-00 по Москве.
DIGEST_WINDOWS_UTC = ((7, 9), (19, 21))
DIGEST_MIN_GAP_H = 10


def digest_due(conn, now):
    """
    Пора ли слать сводку: сейчас внутри окна И с прошлой сводки прошло
    не меньше 10 часов.

    Именно так, а не «прогон попал в минуты 00-15 нужного часа». Прежнее
    условие молча предполагало, что прогоны идут плотно; GitHub же
    выполнял их раз в четыре часа, и за пять дней окно не поймал ни один
    прогон — сводка не ушла ни разу (lesson 2026-09-20, RECURRED 2026-09-25).
    Окно в два часа плюс память о прошлой отправке переживают любой
    разнобой в расписании: сводка уйдёт с первым же прогоном внутри окна
    и не уйдёт второй раз за то же окно.
    """
    h = time.gmtime(now).tm_hour
    if not any(a <= h < b for a, b in DIGEST_WINDOWS_UTC):
        return False
    last = int(db.kv_get(conn, "last_digest", 0) or 0)
    return now - last >= DIGEST_MIN_GAP_H * 3600


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
    messages = [notify.format_item(it, m, total, tier, b, lang=_lang(),
                                   note=ai.note_for(ai.get_note(conn, it["item_id"]), _lang()),
                                   gist=_meta_of(conn, it["item_id"])[2].get(_lang()))
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


def run(sources, dry=False, digest=False, digest_auto=False):
    load_env()
    conn = db.connect()
    now = int(time.time())
    print("прогон %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)))
    if digest_auto and not digest and digest_due(conn, now):
        digest = True
        print("  время сводки")
    collect(conn, sources, now)
    remeasure(conn, now)
    n = refresh_baselines(conn, now)
    if n:
        print("  норм автора обновлено: %d" % n)
    d = enrich_domains(conn, now)
    if d:
        print("  возраст домена уточнён: %d" % d)
    evaluated = evaluate(conn, now)
    evaluated = apply_ai(conn, evaluated, now)
    hot = sum(1 for e in evaluated if e[3] == scoring.HOT)
    dig = sum(1 for e in evaluated if e[3] == scoring.DIGEST)
    print("  оценено %d: горячих %d, в сводку %d" % (len(evaluated), hot, dig))
    dispatch(conn, evaluated, now, dry=dry, digest=digest)
    try:
        market_step(conn, now, dry=dry)
    except Exception as e:          # рынок — надстройка: его сбой не должен ронять прогон
        print("  рынок: ошибка %s" % e)
    try:
        brief.maybe_send(conn, now, dry=dry)
    except Exception as e:          # сводка — тоже надстройка
        print("  сводка дня: ошибка %s" % e)
    conn.close()
    return 0


def _due(conn, key, now, hours, windows=((7, 9),), weekday=None):
    """Окно по UTC + память о прошлой отправке: та же схема, что у сводки."""
    g = time.gmtime(now)
    if weekday is not None and g.tm_wday != weekday:
        return False
    if not any(a <= g.tm_hour < b for a, b in windows):
        return False
    return now - int(db.kv_get(conn, key, 0) or 0) >= hours * 3600


def market_step(conn, now, dry=False):
    """
    Рынок: пересчёт раз в 12 часов и три рассылки — каждая со своим
    переключателем в настройках подписчика и фильтром по его секторам.

      🚀 сдвиг рынка — сектор впервые перешёл в «растёт» (сразу);
      💰 раунды за сутки — новые сделки от $1 млн, по секторам (ежедневно, 10:00 МСК);
      🧭 рынок недели — полный отчёт (понедельник, 10:00 МСК).
    """
    rep = market.maybe_refresh(conn, now)
    broadcast = []
    if rep:
        print("  рынок пересчитан: растут %s" % (", ".join(
            s["id"] for s in rep["sectors"] if s["trend"] == "up") or "—"))
        broadcast += market.alert_payloads(rep, market.shift_alerts(conn, rep))
    # SEC Form D: индексы EDGAR — раз в сутки, заявки — очередью каждый
    # прогон (их сотни в день, SEC пускает не больше 10 запросов в секунду).
    fd = 0
    try:
        # Отметка — только при успехе: после 403 от SEC (2026-09-29) она
        # стояла на сутки, и исправленный контакт не проверялся до завтра.
        # Неудача — повтор не чаще раза в час.
        key = "formd_ok_ts"
        last_try = int(db.kv_get(conn, "formd_try_ts", 0) or 0)
        if now - int(db.kv_get(conn, key, 0) or 0) >= 86400 and now - last_try >= 3600:
            db.kv_set(conn, "formd_try_ts", now)
            _added, errs = market.refresh_formd(conn, now)
            if not errs:
                db.kv_set(conn, key, now)
        fd = market.process_formd(conn, now)
    except Exception as e:              # SEC — надстройка: его сбой не роняет рынок
        print("  SEC Form D: ошибка %s" % e)
    # История за полгода — понемногу каждый прогон, пока не наберётся.
    try:
        market.backfill_deals(conn, now)
    except Exception as e:
        print("  история: ошибка %s" % e)
    # Очередь разбора раундов — каждый прогон понемногу: ниши и стадии
    # появляются по мере разбора, и отчёт пересчитывается без сети.
    try:
        market.signals_step(conn, now)
    except Exception as e:          # сигналы — надстройка
        print("  сигналы: ошибка %s" % e)
    enriched = market.enrich_deals(conn, now)
    try:
        if market.split_step(conn, now):
            enriched = True
    except Exception as e:
        print("  дробление ниш: ошибка %s" % e)
    try:
        market.tag_demand_step(conn, now)
    except Exception as e:
        print("  «боль»: ошибка %s" % e)
    if enriched or fd:
        rep = market.update_report(conn, now)
    rep = rep or market.last_report(conn)
    if rep:
        try:
            if market.gap_step(conn, now, rep):
                rep = market.update_report(conn, now)
        except Exception as e:
            print("  аналоги в СНГ: ошибка %s" % e)
        try:
            market.web_step(conn, now, rep)
        except Exception as e:
            print("  сеть по нише: ошибка %s" % e)
        broadcast += market.niche_alerts(conn, rep, now)
        # Вывод модели — не чаще раза в 12 часов на язык (кэш в story).
        market.render_all(conn, rep, now)
    niche_items, niche_keys = market.niche_round_payloads(conn, now)
    broadcast += niche_items
    fund_keys = []
    if _due(conn, "last_funding", now, 20):
        items, fund_keys = market.funding_digest(conn, now)
        broadcast += items
    weekly = rep and _due(conn, "last_market_weekly", now, 6 * 24, weekday=0)
    if weekly:
        texts = market.render_all(conn, rep, now)
        broadcast.append({"kind": "market", "texts": texts, "text": texts["ru"]})
    if not broadcast:
        return
    if dry:
        print("  --dry: рыночных рассылок %d: %s" % (len(broadcast), ", ".join(b["kind"] for b in broadcast)))
        return
    ok, err = notify.deliver(broadcast=broadcast)
    if ok or not err:
        if niche_keys:
            market.mark_niche_rounds(conn, niche_keys)
        if fund_keys:
            market.mark_deals_sent(conn, fund_keys)
            db.kv_set(conn, "last_funding", now)
        if weekly:
            db.kv_set(conn, "last_market_weekly", now)
        conn.commit()
    print("  рынок: разослано %d %s" % (ok, ("— " + err) if err else ""))


AI_PER_RUN = 20


def _lang():
    """Язык выжимок: LS_LANG из окружения, по умолчанию русский."""
    return os.environ.get("LS_LANG", "ru").strip() or "ru"


def apply_ai(conn, evaluated, now):
    """
    ИИ-разбор кандидатов в уведомление и его вердикт.

    Порядок важен: сначала цифры решают, кто вообще кандидат (так модель
    читает десятки записей в сутки, а не тысячи), потом модель снимает
    то, что по цифрам похоже на запуск, а по смыслу им не является.
    Без ключа шаг ничего не делает — система работает как раньше.
    """
    lang = os.environ.get("LS_LANG", "ru").strip() or "ru"
    ok, why = ai.available()
    if not ok:
        return evaluated
    # Первыми — то, что уйдёт в пуш: без разбора оно не отправится.
    cands = [e[0] for e in evaluated if _pushable(e)]
    cands += [e[0] for e in evaluated if e[3] in (scoring.HOT, scoring.DIGEST)]
    ai.annotate(conn, cands, now, lang=lang)
    ai.tag_items(conn, now)
    out = []
    for item, metrics, total, tier, breakdown in evaluated:
        note = ai.get_note(conn, item["item_id"], lang)
        demote, flag = ai.verdict(note, item["source"])
        if flag:
            breakdown = dict(breakdown)
            breakdown[flag] = 0.0
        if demote and tier != scoring.ARCHIVE:
            tier = scoring.ARCHIVE
            scoring.save_score(conn, item["item_id"], now, total, tier, breakdown)
        elif tier == scoring.HOT and ai.not_business(note):
            breakdown = dict(breakdown)
            breakdown["ИИ: скорее игрушка, чем бизнес"] = 0.0
            tier = scoring.DIGEST
            scoring.save_score(conn, item["item_id"], now, total, tier, breakdown)
        out.append((item, metrics, total, tier, breakdown))
    conn.commit()
    return out


def main():
    # ПЕРВЫМ делом, до любого print: по расписанию нас запускает pythonw,
    # у которого stdout равен None.
    setup_logging("scout")
    ap = argparse.ArgumentParser(description="launch-scout — поиск свежих запусков")
    ap.add_argument("--sources", default=",".join(ALL_SOURCES),
                    help="источники через запятую: x,hn,yc,gh")
    ap.add_argument("--dry", action="store_true", help="не отправлять в Telegram")
    ap.add_argument("--digest", action="store_true", help="отправить сводку")
    ap.add_argument("--digest-auto", action="store_true",
                    help="отправить сводку, если пришло её время (окно + 10 ч с прошлой)")
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
    return run(srcs, dry=args.dry, digest=args.digest, digest_auto=args.digest_auto)


if __name__ == "__main__":
    sys.exit(main())
