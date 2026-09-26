# -*- coding: utf-8 -*-
"""
Проверка твиттер-слоя по шагам: python x_check.py

Зачем отдельный инструмент. Парсер X ломается не целиком, а по звеньям:
протухли куки, сменился queryId, X добавил обязательный флаг, аккаунт
ограничили. В общем прогоне всё это выглядит одинаково — «X: 0 записей».
Здесь каждое звено проверяется отдельно и называется то, которое
порвалось, — чтобы чинить сразу нужное место.

Запросов делает немного (пять-шесть) и с паузой, аккаунт не нагружает.
Значения кук не печатает никогда.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_env, setup_logging  # noqa: E402
from sources import x as X                  # noqa: E402


def step(n, title):
    print("\n%d. %s" % (n, title))


def main():
    setup_logging("x_check")
    load_env()
    session, err = X.session_from_env()
    if not session:
        print("куки не найдены: %s" % err)
        return 1

    step(1, "адреса запросов (queryId) из фронтенда X")
    found, err = session.discover_query_ids()
    need = ("SearchTimeline", "UserByScreenName", "UserTweets")
    if err and not found:
        print("   НЕ НАЙДЕНЫ: %s" % err)
        print("   значит: либо куки не приняты и X отдал страницу входа, "
              "либо изменилась разметка страницы")
    else:
        print("   найдено операций: %d" % len(found))
        for op in need:
            print("   %-18s %s" % (op, "есть" if session.queries.get(op) else "НЕТ"))

    step(2, "сессия принята: профиль по нику")
    uid, err = session.user_id("paulg")
    if uid:
        print("   ок: @paulg -> %s (куки живые, аккаунт читает)" % uid)
    else:
        print("   НЕ ВЫШЛО: %s" % err)

    step(3, "поиск (самое важное: на нём держится весь сбор)")
    t0 = time.time()
    tweets, err = session.search('"just launched" filter:links -filter:replies min_faves:20 lang:en',
                                 limit=20)
    if err:
        print("   НЕ ВЫШЛО: %s" % err)
    else:
        launches = [t for t in tweets if X.looks_like_launch(t.get("text"))]
        with_bm = [t for t in tweets if t.get("bookmarks") is not None]
        print("   твитов: %d за %.1f с, из них похожих на запуск: %d"
              % (len(tweets), time.time() - t0, len(launches)))
        print("   закладки в ответе есть у %d из %d (без них не считается "
              "доля закладок)" % (len(with_bm), len(tweets)))
        for t in tweets[:3]:
            print("   · @%s ♥%s 🔖%s  %s" % (t.get("screen_name"), t.get("likes"),
                                          t.get("bookmarks"),
                                          (t.get("text") or "").replace("\n", " ")[:70]))

    step(4, "лента аккаунта (для списка accounts.txt)")
    if uid:
        tweets, err = session.user_tweets(uid, limit=10)
        print("   НЕ ВЫШЛО: %s" % err if err else "   ок: %d постов" % len(tweets))
    else:
        print("   пропущено: нет id из шага 2")

    step(5, "добавленные на лету флаги")
    added = sorted(set(session.features) - set(X.BASE_FEATURES))
    print("   X потребовал новых флагов: %d%s"
          % (len(added), (" — " + ", ".join(added[:6])) if added else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
