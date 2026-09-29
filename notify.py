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


# Подписи уведомлений на трёх языках. Казахские — машинный перевод,
# до продажи в РК показать носителю языка.
L = {
    "ru": {"subs": "подписчиков", "clone": "Повторить", "post": "пост", "product": "продукт",
           "early": "⏳ Отклика пока нет (%d) — это сигнал о запуске, а не о взлёте.",
           "effort": {"days": "дни", "weeks": "недели", "months": "месяцы", "unclear": "неясно"},
           "none": ("не видно", "not visible")},
    "kk": {"subs": "жазылушы", "clone": "Қайталау", "post": "пост", "product": "өнім",
           "early": "⏳ Әзірге үн қату жоқ (%d) — бұл өсу емес, іске қосу туралы белгі.",
           "effort": {"days": "күндер", "weeks": "апталар", "months": "айлар", "unclear": "белгісіз"},
           "none": ("көрінбейді", "not visible", "не видно")},
    "en": {"subs": "followers", "clone": "To replicate", "post": "post", "product": "product",
           "early": "⏳ No traction yet (%d) — a launch signal, not a breakout.",
           "effort": {"days": "days", "weeks": "weeks", "months": "months", "unclear": "unclear"},
           "none": ("not visible", "не видно")},
}
EFFORT_RU = L["ru"]["effort"]


def format_item(item, metrics, total, tier, breakdown, note=None, gist=None, lang="ru",
                sectors=None):
    """
    Одно уведомление в HTML для Telegram, на языке читателя.

    Текст находки идёт на языке пользователя: полный ИИ-разбор, если он
    есть, иначе выжимка в одну фразу из пакетной разметки. Исходный пост
    на английском показывается, только когда ни того, ни другого нет, —
    ссылка на оригинал остаётся всегда.
    """
    tx = L.get(lang, L["ru"])
    head = "🔥" if tier == "hot" else "•"
    src = SRC_RU.get(item["source"], item["source"])
    lines = ["%s <b>%s</b>  <code>%s</code>" % (head, _esc(item["title"] or "—"), total)]

    who = item["author"]
    sub = src
    if who:
        sub += " · @%s" % _esc(who)
        if item["author_followers"]:
            sub += " (%s %s)" % (_num(item["author_followers"]), tx["subs"])
    if sectors:
        import market
        sub += " · " + ", ".join("%s %s" % (market.SECTOR[x]["emoji"], market.sector_name(x, lang))
                                 for x in sectors if x in market.SECTOR)
    lines.append("<i>%s</i>" % sub)

    if note and note.get("summary"):
        lines.append("")
        lines.append("🧠 " + _esc(note["summary"]))
        effort = tx["effort"].get(note.get("clone_effort"), "")
        if effort:
            extra = " — " + _esc(note["clone_note"]) if note.get("clone_note") else ""
            lines.append("🛠 %s: %s%s" % (tx["clone"], effort, extra))
        money = (note.get("monetization") or "").strip()
        if money and money.lower() not in tx["none"]:
            lines.append("💰 " + _esc(money))
    elif gist:
        lines.append("")
        lines.append("🧠 " + _esc(gist))
    else:
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

    # Почему это пришло — одной строкой, с честной пометкой, если отклика
    # ещё нет: Launch HN приходит в день запуска и с тремя очками, и
    # читатель должен видеть, что это «компания YC вышла», а не «взлетело».
    why = [k for k, v in sorted((breakdown or {}).items(), key=lambda kv: -abs(kv[1]) if isinstance(kv[1], (int, float)) else 0)
           if isinstance(v, (int, float)) and v > 0][:2]
    if why:
        lines.append("📊 " + _esc("; ".join(why)))
    likes = (metrics or {}).get("likes")
    floor = {"hn": 20, "x": 30}.get(item["source"])
    if floor and (likes or 0) < floor:
        lines.append(tx["early"] % (likes or 0))

    lines.append("")
    lines.append('<a href="%s">%s</a>' % (_esc(item["url"] or ""), tx["post"]))
    if item["product_url"] and item["product_url"] != item["url"]:
        lines[-1] += ' · <a href="%s">%s</a>' % (
            _esc(item["product_url"]), _esc(item["domain"] or tx["product"]))
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


def deliver(hot=None, digest=None, broadcast=None):
    """
    Разослать подписчикам через Worker; без него — напрямую владельцу.

    Worker знает подписчиков и их личные категории и сам раздаёт каждому
    своё, с кнопками «Карточка идеи» и «В работу». Если Worker недоступен
    или ответил ошибкой, горячее уходит владельцу напрямую, как раньше:
    потерять находку из-за сбоя рассылки хуже, чем получить её без кнопок.
    Возвращает (сколько сообщений ушло, ошибка).
    """
    hot, broadcast = hot or [], broadcast or []
    url = os.environ.get("WORKER_URL", "").strip()
    secret = os.environ.get("LS_INGEST_SECRET", "").strip()
    worker_err = None
    if url and secret:
        try:
            r = requests.post(url.rstrip("/") + "/notify", timeout=90,
                              headers={"x-ingest-secret": secret},
                              json={"hot": hot, "digest": digest, "broadcast": broadcast})
            if r.status_code == 200:
                return int(r.json().get("sent", 0)), None
            worker_err = "Worker ответил %d" % r.status_code
        except (requests.RequestException, ValueError) as e:
            worker_err = "Worker недоступен: %s" % str(e)[:100]
    msgs = [h.get("text") or (h.get("texts") or {}).get("ru", "") for h in hot]
    if digest and digest.get("items"):
        msgs.append("%s — %d\n\n%s" % (digest.get("head", "📋 <b>Сводка</b>"), len(digest["items"]),
                                       "\n".join(d["line"] for d in digest["items"])))
    msgs += [b if isinstance(b, str) else (b.get("text") or "") for b in broadcast]
    ok, errs = send_batch(msgs)
    err = "; ".join(filter(None, [worker_err] + errs[:1])) or None
    return ok, err


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
