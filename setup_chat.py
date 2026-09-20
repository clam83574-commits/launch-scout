# -*- coding: utf-8 -*-
"""
Записать TG_CHAT_ID в .env, не копируя его руками: python setup_chat.py

Скрипт ждёт любое сообщение боту, берёт из него id чата, дописывает в .env
и тут же шлёт туда подтверждение — так сразу видно, что канал рабочий,
а не только что число куда-то записалось.

Годится и для группы: добавьте бота в группу и напишите там — id группы
отрицательный, это нормально, скрипт с ним работает так же.
"""
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ENV = Path(__file__).resolve().parent / ".env"
API = "https://api.telegram.org/bot%s/%s"


def write_chat_id(chat_id):
    """Подставить значение в .env, сохранив остальные строки как есть."""
    text = ENV.read_text(encoding="utf-8")
    line = "TG_CHAT_ID=%s" % chat_id
    if re.search(r"^TG_CHAT_ID=.*$", text, re.M):
        text = re.sub(r"^TG_CHAT_ID=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV.write_text(text, encoding="utf-8")


def main(wait_seconds=None):
    # Окно ожидания аргументом: python setup_chat.py 600
    if wait_seconds is None:
        wait_seconds = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 120
    load_env()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        print("в .env нет TG_BOT_TOKEN")
        return 1

    me = requests.get(API % (token, "getMe"), timeout=20).json()
    if not me.get("ok"):
        print("токен не принят Telegram: %s" % str(me)[:200])
        return 1
    username = me["result"]["username"]

    print("Откройте https://t.me/%s и отправьте боту любое сообщение." % username)
    print("Жду до %d секунд..." % wait_seconds)

    deadline = time.time() + wait_seconds
    offset = None
    while time.time() < deadline:
        params = {"timeout": 20}
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(API % (token, "getUpdates"), params=params, timeout=30).json()
        except requests.RequestException as e:
            print("сеть: %s" % str(e)[:120])
            time.sleep(2)
            continue
        for upd in (r.get("result") or []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if not chat.get("id"):
                continue
            chat_id = chat["id"]
            who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            write_chat_id(chat_id)
            print("нашёл: chat_id=%s (%s, %s) — записан в .env"
                  % (chat_id, chat.get("type"), who))
            ok = requests.post(API % (token, "sendMessage"), timeout=20, data={
                "chat_id": chat_id, "parse_mode": "HTML",
                "text": ("✅ <b>launch-scout подключён</b>\n\nСюда будут приходить "
                         "находки. Мгновенно — то, что набрало 58+ баллов, "
                         "сводка — дважды в день.")}).json()
            print("подтверждение отправлено" if ok.get("ok")
                  else "сообщение не ушло: %s" % str(ok)[:160])
            return 0
        time.sleep(1)

    print("сообщений не дождался. Запустите снова и напишите боту.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
