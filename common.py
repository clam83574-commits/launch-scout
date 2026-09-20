# -*- coding: utf-8 -*-
"""Общее для всех источников: HTTP с ретраями, домены, RDAP, чтение .env."""
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Домены, которые НЕ являются продуктом: соцсети, витрины и — отдельно
# важное — хостинги чужих страниц.
#
# Про хостинги. Пропустить их нельзя не из аккуратности, а потому что они
# ломают сразу две механики. Домен здесь служит ключом, по которому
# считается «подтверждено другими источниками» и «про это уже писали» —
# и если у сотни разных проектов домен один и тот же github.io, они
# начинают подтверждать друг друга и одновременно глушить друг друга как
# «окно закрылось». Поймано 2026-09-20 на живой выдаче: незнакомый
# проект получил +12 за подтверждение двумя источниками, которых не было.
# Вторая механика — возраст по RDAP: у github.io он 2013 год, и любой
# сегодняшний проект на нём получал бы штраф «не новьё».
NON_PRODUCT = {
    "github.com", "x.com", "twitter.com", "youtube.com", "youtu.be",
    "news.ycombinator.com", "producthunt.com", "medium.com", "substack.com",
    "notion.so", "notion.site", "docs.google.com", "linkedin.com",
    "reddit.com", "discord.gg", "discord.com", "t.me", "apps.apple.com",
    "play.google.com", "figma.com", "loom.com", "huggingface.co", "arxiv.org",
    # хостинги страниц и превью-деплоев
    "github.io", "gitlab.io", "pages.dev", "vercel.app", "netlify.app",
    "netlify.com", "herokuapp.com", "web.app", "firebaseapp.com",
    "streamlit.app", "replit.app", "replit.dev", "repl.co", "glitch.me",
    "surge.sh", "onrender.com", "fly.dev", "railway.app", "up.railway.app",
    "readthedocs.io", "gitbook.io", "bubbleapps.io", "softr.app",
    "framer.website", "webflow.io", "carrd.co", "lovable.app", "base44.app",
}


def setup_logging(name, keep_bytes=512 * 1024):
    """
    Направить вывод в файл, когда консоли нет.

    Под `pythonw.exe` (а именно им всё запускается по расписанию, чтобы не
    мигало окно терминала) `sys.stdout` равен None — и ЛЮБОЙ `print`
    роняет процесс с AttributeError на NoneType. Поэтому здесь не просто
    удобство логов, а условие работоспособности: без подмены поток
    уведомлений молча остановился бы на первой же строке вывода.

    Файл подрезается по размеру, чтобы за месяцы работы не разросся.
    """
    log_path = ROOT / "data" / ("%s.log" % name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.exists() and log_path.stat().st_size > keep_bytes:
            tail = log_path.read_bytes()[-keep_bytes // 2:]
            log_path.write_bytes(tail)
    except OSError:
        pass

    no_console = sys.stdout is None or sys.stderr is None
    if no_console:
        stream = open(str(log_path), "a", encoding="utf-8", errors="replace",
                      buffering=1)
        sys.stdout = stream
        sys.stderr = stream
        return log_path

    # Консоль есть: держим UTF-8 и дублируем в файл, чтобы разбор
    # ночного сбоя не зависел от того, скроллится ли ещё то окно.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdout = _Tee(sys.stdout, open(str(log_path), "a",
                                           encoding="utf-8", errors="replace",
                                           buffering=1))
    except OSError:
        pass
    return log_path


class _Tee:
    """Пишет сразу в консоль и в файл."""

    def __init__(self, console, file_obj):
        self.console = console
        self.file = file_obj

    def write(self, data):
        try:
            self.console.write(data)
        except Exception:
            pass
        try:
            self.file.write(data)
        except Exception:
            pass

    def flush(self):
        for s in (self.console, self.file):
            try:
                s.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self.console, name)


def load_env(path=None):
    """
    Прочитать .env рядом со скриптом в os.environ.

    Свой парсер вместо python-dotenv намеренно: одна зависимость меньше,
    а формат здесь — десяток строк KEY=VALUE.
    """
    p = Path(path) if path else (ROOT / ".env")
    if not p.exists():
        return {}
    got = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            got[k] = v
            os.environ.setdefault(k, v)
    return got


def http_json(url, headers=None, params=None, method="GET", data=None,
              tries=3, timeout=25, pause=1.5):
    """
    JSON по HTTP с ретраями. Возвращает (данные, ошибка).

    Ошибку возвращаем, а не бросаем: у скаута пять независимых источников,
    и падение одного не должно ронять прогон остальных. Молчащий источник
    попадает в таблицу runs, и это видно в /status бота.
    """
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    last = "не пробовали"
    for attempt in range(tries):
        try:
            r = requests.request(method, url, headers=h, params=params,
                                 data=data, timeout=timeout)
            if r.status_code == 429:
                last = "429 rate limit"
                time.sleep(pause * (attempt + 2) * 2)
                continue
            if r.status_code >= 400:
                # 4xx кроме 429 повторять бессмысленно — это не сбой связи,
                # а отказ: закрытый endpoint, протухшая кука, битый запрос.
                if r.status_code < 500:
                    return None, "HTTP %d: %s" % (r.status_code, r.text[:160])
                last = "HTTP %d" % r.status_code
                time.sleep(pause * (attempt + 1))
                continue
            return r.json(), None
        except json.JSONDecodeError as e:
            return None, "не JSON: %s" % e
        except requests.RequestException as e:
            last = str(e)[:160]
            time.sleep(pause * (attempt + 1))
    return None, last


def domain_of(url):
    """Домен второго уровня из URL. None для агрегаторов и мусора."""
    if not url:
        return None
    try:
        host = urllib.parse.urlparse(url if "//" in url else "http://" + url).netloc
    except ValueError:
        return None
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    parts = host.split(".")
    # co.uk, com.br и подобные: берём три уровня, иначе второй уровень пустой
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        base = ".".join(parts[-3:])
    else:
        base = ".".join(parts[-2:])
    if base in NON_PRODUCT or host in NON_PRODUCT:
        return None
    return base


RDAP_BASE = "https://rdap.org/domain/"


def domain_age_days(conn, domain, now=None):
    """
    Возраст регистрации домена в днях, через RDAP, с кэшем в базе.

    Зачем: отсекает пересказы старых продуктов. Домен, зарегистрированный
    три года назад, — это не «только что вышло», сколько бы лайков ни собрал
    пост. RDAP бесплатный и без ключей, но отвечает не по всем зонам —
    поэтому None здесь штатное значение, а не ошибка.
    """
    if not domain:
        return None
    now = now or int(time.time())
    row = conn.execute(
        "SELECT created_at, checked_at FROM domain_cache WHERE domain = ?",
        (domain,)).fetchone()
    if row and row["checked_at"] and now - row["checked_at"] < 30 * 86400:
        if row["created_at"] is None:
            return None
        return int((now - row["created_at"]) / 86400)

    data, err = http_json(RDAP_BASE + domain, tries=2, timeout=15,
                          headers={"Accept": "application/rdap+json"})
    created = None
    if data and not err:
        for ev in (data.get("events") or []):
            if ev.get("eventAction") == "registration":
                created = _parse_iso(ev.get("eventDate"))
                break
    conn.execute(
        "INSERT OR REPLACE INTO domain_cache (domain, created_at, checked_at) "
        "VALUES (?,?,?)", (domain, created, now))
    if created is None:
        return None
    return int((now - created) / 86400)


def _parse_iso(s):
    """ISO-8601 в unix. RDAP отдаёт и 'Z', и смещение, и без зоны."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def clean_text(s, limit=600):
    """HTML-теги, сущности и лишние пробелы вон — в Telegram уходит простой текст."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
          .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&#x2F;", "/").replace("&nbsp;", " "))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def first_external_url(text):
    """Первая ссылка в тексте, не ведущая на сам твиттер."""
    if not text:
        return None
    for m in re.findall(r"https?://[^\s\)\]\"'<>]+", text):
        host = m.split("//", 1)[-1].split("/", 1)[0].lower()
        if not any(host.endswith(b) for b in ("x.com", "twitter.com", "t.co")):
            return m.rstrip(".,;")
    return None
