# -*- coding: utf-8 -*-
"""
Оценка кандидата, 0-100.

ГЛАВНЫЙ ПРИНЦИП: абсолютные числа не значат ничего. «500 лайков» — это
шум у аккаунта с сотней тысяч подписчиков и событие у аккаунта с восемью
сотнями. «300 очков на HN» за сутки — обычный день, за час — первая полоса.
Поэтому считаем три вещи, которых не видно глазами:

  1. СКОРОСТЬ и УСКОРЕНИЕ — сколько набирает в час и растёт ли этот темп.
     Ради этого db.py и хранит историю замеров. Пост, который ещё
     разгоняется, ценнее поста, который уже собрал больше, но встал.
  2. АНОМАЛИЯ ОТНОСИТЕЛЬНО АВТОРА — отклонение от его собственной нормы
     (медиана последних постов, разброс через MAD). Ловит момент, когда
     у человека, которого обычно не замечают, вдруг выстрелило.
  3. КАЧЕСТВО ВНИМАНИЯ — доля закладок. Лайк ставят за остроумие, в
     закладки кладут то, к чему собираются вернуться. Высокая доля
     закладок отличает инструмент от развлечения лучше любой другой
     бесплатной метрики.

Отдельно стоит YC и акселераторы: там подтверждением служит не реакция
публики, а сам факт отбора, поэтому такие кандидаты получают высокий
балл сразу, минуя расчёт скорости.

MAD, а не стандартное отклонение: у вовлечённости хвост тяжёлый, один
вирусный пост раздувает sigma так, что следующий вирусный выглядит нормой.
"""
import json
import math
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOT, DIGEST, ARCHIVE = "hot", "digest", "archive"
# Пороги выставлены по потолку РЕАЛЬНО достижимого балла, а не на глаз.
# Первая версия (72 и 52) была непригодна: сумма всех слагаемых, доступных
# свежему твиту, давала максимум 52, и мгновенное уведомление не могло
# сработать в принципе, пока за автором не накопится история в несколько
# недель. Поймано test_score.py до запуска, а не месяцем тишины после.
HOT_MIN = 58.0        # мгновенное сообщение
DIGEST_MIN = 42.0     # в сводку дважды в день

# Ниши, которые владелец не берёт по своим правилам. Жёсткий отсев —
# только очевидное; пограничное помечается флагом и теряет часть балла,
# решение остаётся за человеком.
HARD_BLOCK = ("casino", "gambling", "betting odds", "sportsbook", "lottery",
              "porn", "onlyfans", "adult content", "alcohol delivery",
              "wine club", "brewery")
SOFT_FLAG = ("loan", "lending", "mortgage", "interest rate", "payday",
             "credit score", "bnpl", "buy now pay later", "trading bot",
             "crypto trading", "forex", "leverage trading", "dating app",
             "insurance premium")


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _mad(xs, med):
    dev = [abs(x - med) for x in xs if x is not None]
    m = _median(dev)
    return m if m else None


def series(conn, item_id):
    """Замеры по кандидату, по возрастанию времени."""
    return conn.execute(
        "SELECT ts, likes, replies, reposts, quotes, bookmarks, views "
        "FROM metrics WHERE item_id = ? ORDER BY ts", (item_id,)).fetchall()


def velocity(rows, field="likes"):
    """
    (скорость в час, ускорение). Ускорение — отношение темпа во второй
    половине наблюдений к темпу в первой; больше 1 = ещё разгоняется.

    Меньше двух замеров — скорость посчитать не из чего, возвращаем (None, None).
    Это НЕ ноль: ноль означал бы «стоит на месте», а мы просто ещё не знаем.
    """
    pts = [(r["ts"], r[field]) for r in rows if r[field] is not None]
    if len(pts) < 2:
        return None, None
    t0, v0 = pts[0]
    t1, v1 = pts[-1]
    hours = max((t1 - t0) / 3600.0, 1 / 60.0)
    v = (v1 - v0) / hours
    if len(pts) < 3:
        return v, None
    mid = len(pts) // 2
    ta, va = pts[0]
    tb, vb = pts[mid]
    tc, vc = pts[-1]
    h1 = max((tb - ta) / 3600.0, 1 / 60.0)
    h2 = max((tc - tb) / 3600.0, 1 / 60.0)
    v1h = (vb - va) / h1
    v2h = (vc - vb) / h2
    if v1h <= 0:
        return v, (2.0 if v2h > 0 else None)
    return v, v2h / v1h


def update_baseline(conn, source, author, values, now=None):
    """Пересчитать норму автора по его последним постам."""
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return None
    med = _median(vals)
    mad = _mad(vals, med)
    conn.execute(
        "INSERT OR REPLACE INTO baseline (source, author, median_likes, mad, n, updated) "
        "VALUES (?,?,?,?,?,?)",
        (source, author, med, mad, len(vals), now or int(time.time())))
    return med, mad


def author_z(conn, source, author, value):
    """
    Насколько пост выбивается из нормы автора. None, если нормы ещё нет.

    Порог 0.1 в знаменателе — от деления на ноль у аккаунтов, где все посты
    собирают одинаково.
    """
    if not author or value is None:
        return None
    row = conn.execute(
        "SELECT median_likes, mad, n FROM baseline WHERE source = ? AND author = ?",
        (source, author)).fetchone()
    if not row or row["median_likes"] is None or (row["n"] or 0) < 5:
        return None
    mad = row["mad"] or max(row["median_likes"] * 0.3, 1.0)
    return (value - row["median_likes"]) / max(mad, 0.1)


def cross_source_hits(conn, domain, item_id):
    """
    Сколько РАЗНЫХ источников уже говорят про этот же домен.

    Самый дешёвый способ отличить настоящий запуск от одиночного всплеска:
    если продукт всплыл и в твиттере, и на HN, и на GitHub — это не
    случайность и не накрутка.
    """
    if not domain:
        return 0
    row = conn.execute(
        "SELECT COUNT(DISTINCT source) n FROM items "
        "WHERE domain = ? AND item_id != ?", (domain, item_id)).fetchone()
    return int(row["n"] or 0)


def crowded(conn, domain, item_id, days=14):
    """
    Сколько раз про этот домен уже писали за последние N дней.

    Нужно не для силы сигнала, а против неё: если про продукт говорят
    вторую неделю, «сделать первым» уже не получится, окно закрылось.
    """
    if not domain:
        return 0
    cutoff = int(time.time()) - days * 86400
    row = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE domain = ? AND item_id != ? "
        "AND first_seen >= ?", (domain, item_id, cutoff)).fetchone()
    return int(row["n"] or 0)


def niche_flags(text):
    """(жёсткий стоп, мягкая пометка) по правилам владельца."""
    low = (text or "").lower()
    hard = next((w for w in HARD_BLOCK if w in low), None)
    soft = next((w for w in SOFT_FLAG if w in low), None)
    return hard, soft


def score_item(conn, item, now=None):
    """
    Посчитать балл. Возвращает (балл, уровень, разбор).

    Разбор пишется в базу и уходит в сообщение: порог должен быть объясним,
    иначе им нельзя пользоваться — непонятно, что крутить, когда выдача
    поедет.
    """
    now = now or int(time.time())
    src = item["source"]
    rows = series(conn, item["item_id"])
    last = rows[-1] if rows else None
    breakdown, total = {}, 0.0

    text = " ".join(filter(None, [item["title"], item["body"], item["tags"]]))
    hard, soft = niche_flags(text)
    if hard:
        return 0.0, ARCHIVE, {"стоп-ниша": hard}

    age_h = None
    if item["posted_at"]:
        age_h = max((now - item["posted_at"]) / 3600.0, 0.05)

    # --- 1. Отбор акселератора: факт вместо реакции публики (0-40) -------
    if src == "yc":
        add = 40.0
        breakdown["батч YC"] = add
        total += add
        # Свежесть здесь — это когда мы увидели компанию впервые.
        # Вес подобран так, чтобы новая компания батча сама по себе доходила
        # до сводки (54 при пороге 52), но в мгновенное уведомление попадала
        # только с подтверждением: всплыла в другом источнике или домен
        # зарегистрирован на днях. Иначе каждое обновление каталога YC
        # разрывало бы телефон полусотней сообщений.
        if now - item["first_seen"] < 3 * 86400:
            breakdown["новичок в каталоге"] = 14.0
            total += 14.0

    # --- 2. Аномалия относительно автора (0-25) -------------------------
    z = author_z(conn, src, item["author"], last["likes"] if last else None)
    if z is not None:
        add = max(0.0, min(25.0, 25.0 * (z / 6.0)))
        if add >= 1:
            breakdown["выше своей нормы (z=%.1f)" % z] = round(add, 1)
            total += add

    # --- 3. Скорость и ускорение (0-30) ---------------------------------
    v, acc = velocity(rows)
    if v is not None and v > 0:
        # Логарифм: разница между 5 и 50 в час важна, между 500 и 5000 — нет.
        add = min(20.0, 7.0 * math.log10(1 + v))
        breakdown["темп %.0f/час" % v] = round(add, 1)
        total += add
        if acc and acc > 1.15:
            a = min(10.0, 4.0 * acc)
            breakdown["ускоряется (x%.1f)" % acc] = round(a, 1)
            total += a

    # --- 4. Нормировка на аудиторию (0-18) ------------------------------
    if last and last["likes"] and item["author_followers"]:
        er = last["likes"] / max(item["author_followers"], 1)
        # Планка мягче для крупных аккаунтов: у них охват режется самой
        # платформой, и 0.5% там значит столько же, сколько 2% у малого.
        need = 0.005 if item["author_followers"] > 100000 else 0.02
        if er >= need:
            add = min(18.0, 18.0 * (er / (need * 3)))
            breakdown["отклик %.1f%% аудитории" % (er * 100)] = round(add, 1)
            total += add

    # --- 5. Качество внимания: закладки (0-18) --------------------------
    if last and last["bookmarks"] and last["likes"]:
        ratio = last["bookmarks"] / max(last["likes"], 1)
        if ratio >= 0.08:
            add = min(18.0, 18.0 * (ratio / 0.25))
            breakdown["в закладки %.0f%% от лайков" % (ratio * 100)] = round(add, 1)
            total += add

    # --- 6. Свежесть продукта по RDAP (0-10) ----------------------------
    d = item["domain_age_days"]
    if d is not None:
        if d <= 90:
            add = 10.0 - min(9.0, d / 12.0)
            breakdown["домену %d дн." % d] = round(add, 1)
            total += add
        elif d > 1095:
            breakdown["домену %d дн. — не новьё" % d] = -6.0
            total -= 6.0

    # --- 7. Подтверждение из другого источника (0-12) -------------------
    hits = cross_source_hits(conn, item["domain"], item["item_id"])
    if hits:
        add = min(12.0, 7.0 * hits)
        breakdown["подтверждено источниками: %d" % hits] = add
        total += add

    # --- 8. Штрафы ------------------------------------------------------
    if last and last["replies"] and last["likes"]:
        r = last["replies"] / max(last["likes"], 1)
        if r > 0.4:
            breakdown["спор в комментариях (%.2f)" % r] = -8.0
            total -= 8.0
    n_crowd = crowded(conn, item["domain"], item["item_id"])
    if n_crowd >= 4:
        breakdown["окно закрывается: %d упоминаний за 2 нед." % n_crowd] = -10.0
        total -= 10.0
    if age_h and age_h > 72 and src in ("x", "hn"):
        breakdown["старше 3 суток"] = -6.0
        total -= 6.0
    if soft:
        breakdown["ниша под вопросом: %s" % soft] = -12.0
        total -= 12.0

    total = max(0.0, min(100.0, total))
    tier = HOT if total >= HOT_MIN else (DIGEST if total >= DIGEST_MIN else ARCHIVE)
    # Посевная запись остаётся в базе со своим баллом — она нужна как фон
    # для дедупликации и для норм авторов, — но уведомления не порождает.
    if _col(item, "bootstrap"):
        tier = ARCHIVE
    return round(total, 1), tier, breakdown


def _col(row, name, default=None):
    """Достать поле из sqlite3.Row, которого может не быть в старой базе."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def save_score(conn, item_id, ts, total, tier, breakdown):
    conn.execute(
        "INSERT OR REPLACE INTO scores (item_id, ts, score, tier, breakdown) "
        "VALUES (?,?,?,?,?)",
        (item_id, ts, total, tier, json.dumps(breakdown, ensure_ascii=False)))
