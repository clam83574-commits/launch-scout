# -*- coding: utf-8 -*-
"""
Отправка в Telegram. Без ИИ-пересказа — владелец выбрал сырые данные:
текст поста как есть, цифры как есть, разбор балла и обе ссылки.

Формат считан за пять секунд с телефона, потому что порядок строк
повторяет порядок решения: что это -> насколько горячо и почему ->
куда идти смотреть.
"""
import html
import os
import sys
import time

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://api.telegram.org/bot%s/%s"
SRC_RU = {"x": "X", "hn": "Hacker News", "yc": "Y Combinator",
          "gh": "GitHub", "ph": "Product Hunt"}


def _esc(s):
    return html.escape(s or "", quote=False)


def _num(n):
    """1234 -> 1,2k. В уведомлении важен порядок величины, не точность."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def format_item(item, metrics, total, tier, breakdown):
    """Одно уведомление в HTML для Telegram."""
    head = "🔥" if tier == "hot" else "•"
    src = SRC_RU.get(item["source"], item["source"])
    lines = ["%s <b>%s</b>  <code>%s</code>" % (head, _esc(item["title"] or "без названия"), total)]

    who = item["author"]
    sub = src
    if who:
        sub += " · @%s" % _esc(who)
        if item["author_followers"]:
            sub += " (%s подписчиков)" % _num(item["author_followers"])
    lines.append("<i>%s</i>" % sub)

    body = (item["body"] or "").strip()
    if body and body != (item["title"] or "").strip():
        lines.append("")
        lines.append(_esc(body[:420]))

    # Цифры: только то, что реально измерено, без прочерков-заглушек.
    nums = []
    if metrics:
        pairs = [("♥", metrics.get("likes")), ("💬", metrics.get("replies")),
                 ("🔁", metrics.get("reposts")), ("🔖", metrics.get("bookmarks")),
                 ("👁", metrics.get("views"))]
        nums = ["%s %s" % (ic, _num(v)) for ic, v in pairs if v]
    if nums:
        lines.append("")
        lines.append(" · ".join(nums))

    if breakdown:
        parts = []
        for k, v in sorted(breakdown.items(), key=lambda kv: -abs(kv[1] if isinstance(kv[1], (int, float)) else 0)):
            sign = "+" if isinstance(v, (int, float)) and v >= 0 else ""
            parts.append("%s %s%s" % (_esc(k), sign, v))
        lines.append("")
        lines.append("<i>%s</i>" % _esc(" · ".join(parts))[:600])

    lines.append("")
    lines.append('<a href="%s">пост</a>' % _esc(item["url"] or ""))
    if item["product_url"] and item["product_url"] != item["url"]:
        lines[-1] += ' · <a href="%s">%s</a>' % (
            _esc(item["product_url"]), _esc(item["domain"] or "продукт"))
    if item["domain_age_days"] is not None:
        lines[-1] += " · домену %d дн." % item["domain_age_days"]
    return "\n".join(lines)


def send(text, token=None, chat_id=None, preview=True):
    """Отправить сообщение. Возвращает (ок, ошибка)."""
    token = token or os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "нет TG_BOT_TOKEN/TG_CHAT_ID в .env"
    try:
        r = requests.post(API % (token, "sendMessage"), timeout=25, data={
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
        })
        if r.status_code != 200:
            return False, "HTTP %d: %s" % (r.status_code, r.text[:200])
        if not r.json().get("ok"):
            return False, str(r.json())[:200]
        return True, None
    except requests.RequestException as e:
        return False, str(e)[:160]


def send_batch(messages, pause=1.2, **kw):
    """
    Несколько сообщений подряд. Пауза обязательна: Telegram режет
    отправку чаще примерно одного сообщения в секунду в один чат.
    """
    ok, errs = 0, []
    for m in messages:
        good, err = send(m, **kw)
        if good:
            ok += 1
        else:
            errs.append(err)
        time.sleep(pause)
    return ok, errs
