# -*- coding: utf-8 -*-
"""
Бот с кнопками: выдача по требованию, поверх того же потока уведомлений.

    python bot.py

Зачем отдельный процесс, а не всё в scout.py: у них разные роли. scout.py
запускается по расписанию, делает работу и умирает — так и должно быть для
задачи планировщика. Бот же должен ЖДАТЬ нажатия, то есть висеть постоянно.
Смешивать их в одном процессе значит потерять обе гарантии сразу.

Оба пишут в одну базу, это безопасно: в db.py включён WAL, он и рассчитан
на «один пишет, другой читает».

ДОСТУП. Бот отвечает ТОЛЬКО владельцу — chat_id из .env. Найти бота в поиске
Telegram может кто угодно, и без этой проверки он показывал бы чужим людям
находки, ради которых всё считается. В tm-scout ровно это место осталось
открытым (`TM_BOT_ALLOW` пуст) — здесь закрыто с первого дня.
"""
import os
import sys
import time
import traceback
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import db            # noqa: E402
import notify        # noqa: E402
import scout         # noqa: E402
from common import load_env, setup_logging  # noqa: E402

# В автозагрузке бот стартует под pythonw (иначе на рабочем столе висело бы
# окно консоли), а там sys.stdout равен None и первый же print убил бы
# процесс. Подменяем поток ДО любого вывода.
setup_logging("bot")

API = "https://api.telegram.org/bot%s/%s"

KEYBOARD = {"inline_keyboard": [
    [{"text": "🔥 Топ-10", "callback_data": "top:10"},
     {"text": "🔄 Обновить сейчас", "callback_data": "refresh"}],
    [{"text": "🆕 За сутки", "callback_data": "fresh:10"},
     {"text": "📊 Статус", "callback_data": "status"}],
]}

HELLO = ("<b>launch-scout</b>\n\n"
         "Ищет продукты в первые часы после выхода. Считает не лайки, а темп "
         "их набора, ускорение, отклонение от нормы автора и долю закладок.\n\n"
         "Находки приходят сюда сами. Кнопками — когда захотите сами.")


class Bot:
    def __init__(self, token, owner_chat_id):
        self.token = token
        self.owner = str(owner_chat_id)
        self.offset = None
        self.s = requests.Session()

    def call(self, method, **data):
        try:
            r = self.s.post(API % (self.token, method), data=data, timeout=40)
            return r.json()
        except requests.RequestException as e:
            return {"ok": False, "error": str(e)[:160]}

    def say(self, chat_id, text, keyboard=None, preview=False):
        payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": "false" if preview else "true"}
        if keyboard:
            import json as _json
            payload["reply_markup"] = _json.dumps(keyboard)
        return self.call("sendMessage", **payload)

    def allowed(self, chat_id):
        return str(chat_id) == self.owner

    # --- действия ------------------------------------------------------
    def action_top(self, chat_id, n=10, window_hours=72, title="Топ находок"):
        conn = db.connect()
        now = int(time.time())
        rows = scout.top_items(conn, n=n, window_hours=window_hours, now=now)
        if not rows:
            self.say(chat_id, "Пока пусто. Источники ещё не принесли ничего "
                              "за это окно.", KEYBOARD)
            conn.close()
            return
        self.say(chat_id, "<b>%s</b> — %d шт." % (title, len(rows)))
        for total, item, metrics, tier, breakdown in rows:
            self.say(chat_id, notify.format_item(item, metrics, total, tier, breakdown),
                     preview=False)
            time.sleep(0.4)
        self.say(chat_id, "Готово.", KEYBOARD)
        conn.close()

    def action_refresh(self, chat_id):
        """Сходить в источники прямо сейчас и показать, что изменилось."""
        self.say(chat_id, "Иду в источники…")
        conn = db.connect()
        now = int(time.time())
        try:
            report = scout.collect(conn, scout.ALL_SOURCES, now, verbose=False)
            scout.remeasure(conn, now, verbose=False)
            scout.refresh_baselines(conn, now)
            scout.enrich_domains(conn, now)
        except Exception:
            self.say(chat_id, "Сбор упал:\n<code>%s</code>"
                     % traceback.format_exc()[-700:], KEYBOARD)
            conn.close()
            return

        lines, total_new = [], 0
        for src, (found, new, err) in report.items():
            total_new += new or 0
            mark = "⚠️" if err else "✓"
            lines.append("%s %s: найдено %s, новых %s%s"
                         % (mark, notify.SRC_RU.get(src, src), found, new,
                            ("\n   <i>%s</i>" % notify._esc(err[:160])) if err else ""))
        self.say(chat_id, "<b>Сбор закончен</b>\n\n" + "\n".join(lines))

        if total_new:
            self.action_top(chat_id, n=min(10, total_new + 3), window_hours=6,
                            title="Самое свежее")
        else:
            self.say(chat_id, "Новых записей нет — с прошлого раза источники "
                              "ничего не добавили.", KEYBOARD)
        conn.close()

    def action_status(self, chat_id):
        conn = db.connect()
        now = int(time.time())
        total = conn.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
        day = conn.execute("SELECT COUNT(*) n FROM items WHERE first_seen >= ?",
                           (now - 86400,)).fetchone()["n"]
        lines = ["<b>Состояние</b>", "",
                 "кандидатов: %d, за сутки: %d" % (total, day), "", "<b>Источники</b>"]
        for r in conn.execute(
                "SELECT source, MAX(ts) ts, found, ok, note FROM runs "
                "GROUP BY source ORDER BY source"):
            ago = (now - r["ts"]) / 60.0
            mark = "✓" if r["ok"] else "⚠️"
            lines.append("%s %s — %.0f мин назад, найдено %s"
                         % (mark, notify.SRC_RU.get(r["source"], r["source"]),
                            ago, r["found"]))
            if not r["ok"] and r["note"]:
                lines.append("   <i>%s</i>" % notify._esc(r["note"][:160]))
        conn.close()
        self.say(chat_id, "\n".join(lines), KEYBOARD)

    # --- цикл ----------------------------------------------------------
    def handle_message(self, msg):
        chat_id = (msg.get("chat") or {}).get("id")
        if not self.allowed(chat_id):
            self.say(chat_id, "Это личный бот, доступа нет.")
            return
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/start") or text.startswith("/help"):
            self.say(chat_id, HELLO, KEYBOARD)
        elif text.startswith("/top"):
            parts = text.split()
            n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            self.action_top(chat_id, n=min(n, 25))
        elif text.startswith("/new"):
            self.action_top(chat_id, n=10, window_hours=24, title="За сутки")
        elif text.startswith("/refresh"):
            self.action_refresh(chat_id)
        elif text.startswith("/status"):
            self.action_status(chat_id)
        else:
            self.say(chat_id, "Команды: /top, /new, /refresh, /status", KEYBOARD)

    def handle_callback(self, cb):
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        data = cb.get("data") or ""
        # Ответить Telegram НАДО сразу, иначе кнопка «крутится» до таймаута,
        # а работа у нас занимает десятки секунд.
        self.call("answerCallbackQuery", callback_query_id=cb.get("id"))
        if not self.allowed(chat_id):
            return
        if data.startswith("top:"):
            self.action_top(chat_id, n=int(data.split(":")[1]))
        elif data.startswith("fresh:"):
            self.action_top(chat_id, n=int(data.split(":")[1]), window_hours=24,
                            title="За сутки")
        elif data == "refresh":
            self.action_refresh(chat_id)
        elif data == "status":
            self.action_status(chat_id)

    def run(self):
        print("бот запущен, владелец: %s. Ctrl+C чтобы остановить." % self.owner)
        me = self.call("getMe")
        if not me.get("ok"):
            print("токен не принят: %s" % str(me)[:200])
            return 1
        print("это @%s" % me["result"]["username"])
        while True:
            params = {"timeout": 25}
            if self.offset:
                params["offset"] = self.offset
            try:
                r = self.s.get(API % (self.token, "getUpdates"),
                               params=params, timeout=40).json()
            except requests.RequestException as e:
                print("сеть: %s" % str(e)[:120])
                time.sleep(3)
                continue
            for upd in (r.get("result") or []):
                self.offset = upd["update_id"] + 1
                try:
                    if upd.get("message"):
                        self.handle_message(upd["message"])
                    elif upd.get("callback_query"):
                        self.handle_callback(upd["callback_query"])
                except Exception:
                    # Одно упавшее нажатие не должно ронять бота целиком.
                    print(traceback.format_exc()[-600:])


def main():
    load_env()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    owner = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not owner:
        print("в .env нужны TG_BOT_TOKEN и TG_CHAT_ID (см. setup_chat.py)")
        return 1
    return Bot(token, owner).run() or 0


if __name__ == "__main__":
    sys.exit(main())
