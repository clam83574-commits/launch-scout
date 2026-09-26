# -*- coding: utf-8 -*-
"""
X (Twitter) — собственный парсер. Единственный источник здесь, который
требует аккаунта, и единственный, который ломается сам по себе.

ПОЧЕМУ ЧЕРЕЗ КУКИ, А НЕ ПО-ХОРОШЕМУ
Проверено 2026-09-20 с рабочей машины:
  * cdn.syndication.twimg.com/tweet-result — отдаёт метрики ОДНОГО твита
    по его id без всякой авторизации. 200 OK. Это наш запасной канал.
  * POST /1.1/guest/activate.json — гостевой токен всё ещё выдаётся (200),
    но ленты и поиска под ним больше нет: /2/search/adaptive.json = 404.
  * Nitter мёртв: nitter.net и privacydev не отвечают, xcancel отдаёт 451
    «service is suspended».
Анонимного доступа к поиску не осталось. Поэтому лента берётся сессией
обычного веб-клиента: куки auth_token + ct0 из браузера.

ЧЕМ ЭТО ГРОЗИТ. Автоматизация против ToS X. Аккаунт могут ограничить или
заблокировать. Поэтому: ОТДЕЛЬНЫЙ аккаунт, не основной; вежливый темп
(пауза между запросами); никаких действий записи — только чтение.

ПОЧЕМУ ЭТО НЕ РАЗВАЛИТСЯ ПРИ ПЕРВОМ ЖЕ ОБНОВЛЕНИИ X
Две вещи в этом API меняются постоянно и ломают все наивные парсеры:
  1. queryId в пути GraphQL — свой у каждой операции, меняется с релизом
     фронтенда. Мы не держим его в коде: достаём из бандла main.*.js,
     кладём в x_queries.json и обновляем, когда операция отвалилась.
  2. features — набор обязательных флагов. Если его состав разошёлся,
     X отвечает 400 с текстом «The following features cannot be null: a, b».
     Мы этот текст разбираем, дописываем недостающие флаги и повторяем
     запрос. Так парсер сам догоняет изменения, вместо того чтобы молча
     умереть до следующего ручного визита.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import UA, clean_text, domain_of, first_external_url  # noqa: E402

SOURCE = "x"
ROOT = Path(__file__).resolve().parent.parent
QUERY_CACHE = ROOT / "data" / "x_queries.json"

# Публичный Bearer веб-клиента X — он один и тот же для всех браузеров,
# вшит в их фронтенд и секретом не является.
WEB_BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4"
              "puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
GQL = "https://x.com/i/api/graphql/%s/%s"

# Стартовый набор флагов. Он заведомо неполный и устареет — это нормально,
# недостающие дописываются автоматически из текста ошибки X.
BASE_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

# Стартовые методы. Поиск — POST: с 2026-09-26 на GET он отвечает 404
# (проверено: GET 404, POST 200 с тем же queryId и теми же куками).
# Остальное пока принимает GET. Если X переставит методы снова, парсер
# сам попробует другой и запомнит — эта таблица только экономит первый
# промах после чистого старта.
DEFAULT_METHODS = {"SearchTimeline": "POST"}

# Маркеры запуска. Пост без них — это мнение или тред-подборка, а не выход
# продукта. Список намеренно узкий: широкий («AI», «startup») превращает
# выдачу в ленту болтовни.
LAUNCH_MARKERS = (
    "introducing", "launching", "just launched", "we launched", "just shipped",
    "we shipped", "now live", "we built", "i built", "launch day",
    "out now", "public beta", "早期", "go live", "v1.0", "1.0 is out",
    "today we're launching", "excited to launch", "excited to announce",
)
# Явный мусор: розыгрыши, подборки чужих продуктов, запуски криптотокенов,
# художественные заказы. Криптотокены — отдельно важны: первая же живая
# выдача (2026-09-26) принесла «just launched $BAGM» — мемкоин, а не
# стартап, и по правилам владельца (майсир, спекуляция) мимо в любом
# случае. Тикер ловится регуляркой ниже, слова — здесь.
NOISE_MARKERS = (
    "giveaway", "retweet to win", "rt to win", "follow + rt", "airdrop",
    "tools you should know", "tools you need", "thread 🧵 of", "top 10 ai",
    "best ai tools", "mega thread",
    "memecoin", "meme coin", "pump.fun", "presale", "pre-sale", "stealth launch",
    "fair launch", "contract address", "ca:", "dexscreener", "1000x", "100x",
    "commission", "commissions open",
)
# Тикер криптотокена: «$BAGM», «$PEPE2». Требуем 2-8 заглавных после $,
# чтобы не резать цены вроде «$20/month» — у них за $ идёт цифра.
CASHTAG = re.compile(r"(?<![\w$])\$[A-Z][A-Z0-9]{1,7}\b")


class XSession:
    """Живая сессия веб-клиента X на куках из .env."""

    def __init__(self, auth_token, ct0, delay=1.6):
        self.s = requests.Session()
        self.ct0 = ct0
        self.delay = delay
        self.s.headers.update({
            "Authorization": WEB_BEARER,
            "User-Agent": UA,
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
        })
        self.s.cookies.set("auth_token", auth_token, domain=".x.com")
        self.s.cookies.set("ct0", ct0, domain=".x.com")
        self.features = dict(BASE_FEATURES)
        self.queries = _load_queries()
        # HTTP-метод каждой операции. Меняется так же без предупреждения, как
        # queryId: 2026-09-26 поиск перестал отвечать на GET (404) и отвечает
        # только на POST. Выученное значение хранится рядом с queryId.
        self.methods = dict(DEFAULT_METHODS)
        self.methods.update(self.queries.get("__methods__") or {})
        self._last_call = 0.0

    # --- вежливость ----------------------------------------------------
    def _wait(self):
        """Пауза между запросами. Чем ровнее темп, тем дольше живёт аккаунт."""
        gap = time.time() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.time()

    # --- добыча queryId ------------------------------------------------
    def discover_query_ids(self):
        """
        Вытащить актуальные queryId из фронтенда X.

        Бандлы перечислены в HTML авторизованной страницы; в каждом лежат
        пары {queryId:"...", operationName:"..."}. Забираем все, какие нашли,
        и складываем в data/x_queries.json.
        """
        # Страницу и бандлы запрашиваем КАК БРАУЗЕР: только куки, без
        # заголовков API. С `Authorization: Bearer` и `x-csrf-token` X
        # принимает загрузку страницы за вызов API и отвечает 401 с пустым
        # телом — при полностью живых куках (поймано 2026-09-26 первой же
        # проверкой с настоящим аккаунтом). Браузер эти заголовки шлёт
        # только в запросах к API, не при открытии страницы.
        page_headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9"}
        self._wait()
        try:
            r = requests.get("https://x.com/home", cookies=self.s.cookies,
                             headers=page_headers, timeout=30)
        except requests.RequestException as e:
            return {}, "главная не открылась: %s" % str(e)[:120]
        if r.status_code != 200 or '"screen_name"' not in r.text:
            # С адресов дата-центров (раннер GitHub) главная X отвечает 403
            # при живых куках — замерено 2026-09-26: с ноутбука 200 и вход,
            # из облака 403. API при этом может работать, ему нужны только
            # queryId. Берём их из открытого справочника.
            page_err = ("главная ответила %d без признаков входа (из облака это "
                        "норма — X закрывает страницу для дата-центров)" % r.status_code)
            found, cerr = _community_query_ids()
            if found:
                self.queries.update(found)
                _save_queries(self.queries)
                return found, None
            return {}, page_err + "; справочник тоже недоступен: %s" % cerr
        html = r.text
        bundles = sorted(set(re.findall(
            r"https://abs\.twimg\.com/responsive-web/client-web[a-z-]*/"
            r"(?:main|api|bundle\.[A-Za-z]+)\.[0-9a-f]+[a-z]?\.js", html)))
        if not bundles:
            # Резерв: список модулей отдаётся отдельным манифестом.
            bundles = sorted(set(re.findall(
                r"https://abs\.twimg\.com/responsive-web/client-web[^\"']+\.js", html)))[:12]
        found = {}
        for url in bundles[:12]:
            try:
                js = requests.get(url, headers=page_headers, timeout=30).text
            except requests.RequestException:
                continue
            for qid, op in re.findall(
                    r'queryId:"([A-Za-z0-9_-]{16,})",operationName:"([A-Za-z]+)"', js):
                found[op] = qid
            for op, qid in re.findall(
                    r'operationName:"([A-Za-z]+)",queryId:"([A-Za-z0-9_-]{16,})"', js):
                found[op] = qid
        if found:
            self.queries.update(found)
            _save_queries(self.queries)
            return found, None
        return {}, "в бандлах не нашлось ни одной пары queryId/operationName"

    # --- собственно запрос ---------------------------------------------
    def graphql(self, op, variables, tries=2):
        """
        GraphQL-запрос с самопочинкой набора features.

        Возвращает (данные, ошибка). Ошибку не бросаем: молчащий X не должен
        ронять прогон остальных источников.
        """
        qid = self.queries.get(op)
        if not qid:
            found, err = self.discover_query_ids()
            qid = found.get(op) or self.queries.get(op)
            if not qid:
                return None, "нет queryId для %s (%s)" % (op, err or "не найден")

        method = self.methods.get(op, "GET")
        flipped = rediscovered = False
        for attempt in range(tries + 4):
            self._wait()
            try:
                if method == "POST":
                    r = self.s.post(GQL % (qid, op), timeout=30, json={
                        "variables": variables, "features": self.features, "queryId": qid})
                else:
                    params = {"variables": json.dumps(variables, separators=(",", ":")),
                              "features": json.dumps(self.features, separators=(",", ":"))}
                    r = self.s.get(GQL % (qid, op), params=params, timeout=30)
            except requests.RequestException as e:
                return None, "сеть: %s" % str(e)[:140]

            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    return None, "ответ не JSON (вероятно, капча или разлогин)"
                errs = data.get("errors") or []
                if errs and not data.get("data"):
                    return None, "X вернул ошибку: %s" % str(errs[0].get("message"))[:160]
                self._remember_method(op, method)
                return data, None

            body = r.text[:500]
            if r.status_code == 400 and "features cannot be null" in body:
                # Главный механизм выживания: X сам называет недостающие флаги.
                missing = re.findall(r"[a-z0-9_]{6,}", body.split("null:", 1)[-1])
                added = 0
                for name in missing:
                    if name not in self.features:
                        self.features[name] = True
                        added += 1
                if added:
                    continue
                return None, "400, но новых флагов в тексте нет: %s" % body[:200]

            if r.status_code in (401, 403):
                return None, ("%d — куки не приняты. Протух auth_token/ct0 "
                              "или аккаунт ограничен. Обновите .env" % r.status_code)
            if r.status_code == 429:
                return None, "429 — упёрлись в лимит, ждём следующего прогона"
            if r.status_code == 404:
                # 404 у X значит одно из двух, и чинится в этом порядке.
                # 1) Сменился HTTP-метод операции (поиск 2026-09-26: GET
                #    стал 404, POST — 200). Пробуем другой метод.
                if not flipped:
                    flipped = True
                    method = "POST" if method == "GET" else "GET"
                    continue
                # 2) Сменился queryId. Достаём свежий из бандла и снова
                #    пробуем оба метода.
                if not rediscovered:
                    rediscovered = True
                    found, _ = self.discover_query_ids()
                    if found.get(op) and found[op] != qid:
                        qid = found[op]
                        flipped = False
                        method = self.methods.get(op, "GET")
                        continue
                return None, ("404 на %s — не помогли ни другой метод, ни свежий "
                              "queryId: операция переименована или снята" % op)
            return None, "HTTP %d: %s" % (r.status_code, body[:160])
        return None, "не удалось после повторов"

    def _remember_method(self, op, method):
        """Запомнить сработавший метод, если он отличается от сохранённого."""
        if self.methods.get(op) != method:
            self.methods[op] = method
            self.queries["__methods__"] = {k: v for k, v in self.methods.items()
                                           if DEFAULT_METHODS.get(k) != v or k in DEFAULT_METHODS}
            _save_queries(self.queries)

    # --- высокоуровневое ------------------------------------------------
    def search(self, query, limit=40, product="Latest"):
        """Поиск. product=Latest — хронология, она и нужна, чтобы ловить рано."""
        variables = {"rawQuery": query, "count": min(limit, 50),
                     "querySource": "typed_query", "product": product}
        data, err = self.graphql("SearchTimeline", variables)
        if err:
            return [], err
        return _extract_tweets(data), None

    def user_tweets(self, user_id, limit=20):
        """Последние посты конкретного аккаунта по его числовому id."""
        variables = {"userId": str(user_id), "count": min(limit, 40),
                     "includePromotedContent": False,
                     "withQuickPromoteEligibilityTweetFields": False,
                     "withVoice": False, "withV2Timeline": True}
        data, err = self.graphql("UserTweets", variables)
        if err:
            return [], err
        return _extract_tweets(data), None

    def user_id(self, screen_name):
        """Числовой id по @нику. Нужен один раз на аккаунт, потом кэшируется."""
        data, err = self.graphql("UserByScreenName",
                                 {"screen_name": screen_name.lstrip("@"),
                                  "withSafetyModeUserFields": True})
        if err:
            return None, err
        try:
            return data["data"]["user"]["result"]["rest_id"], None
        except (KeyError, TypeError):
            return None, "нет rest_id в ответе для @%s" % screen_name


# Открытый справочник внутренних адресов X: проект обновляет queryId
# автоматически, сверено 2026-09-26 — все три нужные операции совпали с
# добытыми из бандла на ноутбуке. Это запасной путь, основной — бандл.
COMMUNITY_QIDS = ("https://raw.githubusercontent.com/fa0311/"
                  "TwitterInternalAPIDocument/master/docs/json/API.json")


def _community_query_ids():
    """queryId всех операций из открытого справочника. (словарь, ошибка)."""
    try:
        r = requests.get(COMMUNITY_QIDS, timeout=30, headers={"User-Agent": UA})
        if r.status_code != 200:
            return {}, "HTTP %d" % r.status_code
        g = (r.json() or {}).get("graphql") or {}
    except (requests.RequestException, ValueError) as e:
        return {}, str(e)[:120]
    found = {op: e["queryId"] for op, e in g.items()
             if isinstance(e, dict) and isinstance(e.get("queryId"), str)}
    return found, (None if found else "в справочнике нет операций")


def _load_queries():
    if QUERY_CACHE.exists():
        try:
            return json.loads(QUERY_CACHE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _save_queries(q):
    QUERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    QUERY_CACHE.write_text(json.dumps(q, indent=2, ensure_ascii=False),
                           encoding="utf-8")


# Вложенные твиты: цитата внутри поста и оригинал внутри ретвита. Они
# лежат в ответе как полноценные объекты Tweet, и обход «по признаку»
# принимал их за самостоятельные посты — в выдачу попадали чужие люди,
# которых процитировал кто-то из отслеживаемых (замечено 2026-09-26:
# @Babygravy9 без единого совпадения с запросами и списком).
NESTED_TWEET_KEYS = ("quoted_status_result", "retweeted_status_result")


def _walk_entries(node, out):
    """
    Рекурсивно собрать объекты твитов из ответа любой формы.

    Форму ответа (instructions -> entries -> itemContent -> ...) X меняет
    чаще, чем хотелось бы, и жёсткий путь по ключам ломается на каждом
    редизайне ленты. Поэтому ищем по признаку: словарь с __typename
    'Tweet' и полем legacy. Это переживает перестановки обёрток.
    Внутрь цитат и ретвитов не спускаемся — берём только верхний уровень.
    """
    if isinstance(node, dict):
        if node.get("__typename") == "Tweet" and "legacy" in node:
            out.append(node)
        elif node.get("__typename") == "TweetWithVisibilityResults" and "tweet" in node:
            out.append(node["tweet"])
        for k, v in node.items():
            if k in NESTED_TWEET_KEYS:
                continue
            _walk_entries(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_entries(v, out)


def _extract_tweets(data):
    """Ответ GraphQL -> список нормализованных твитов."""
    raw = []
    _walk_entries(data, raw)
    seen, out = set(), []
    for t in raw:
        lg = t.get("legacy") or {}
        tid = t.get("rest_id") or lg.get("id_str")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        user = (((t.get("core") or {}).get("user_results") or {}).get("result") or {})
        # 2026-09-26: у пользователя больше нет `legacy`. Ник переехал в
        # `core.screen_name`, подписчики — в `relationship_counts.followers`.
        # Без подписчиков молча отключалась нормировка «отклик к размеру
        # аудитории» (18 баллов бюджета X) — у всех 115 записей первого
        # сбора их не было. Старые места оставлены запасными.
        ulg = user.get("legacy") or {}
        screen = (user.get("core") or {}).get("screen_name") or ulg.get("screen_name")
        followers = (user.get("relationship_counts") or {}).get("followers")
        if followers is None:
            followers = ulg.get("followers_count")
        text = lg.get("full_text") or ""
        # Ссылку берём развёрнутую: в тексте она всегда в виде t.co/...
        expanded = None
        for u in ((lg.get("entities") or {}).get("urls") or []):
            cand = u.get("expanded_url")
            if cand and "x.com" not in cand and "twitter.com" not in cand:
                expanded = cand
                break
        out.append({
            "id": str(tid),
            "text": text,
            "screen_name": screen,
            "followers": followers,
            "created_at": lg.get("created_at"),
            "product_url": expanded or first_external_url(text),
            "likes": lg.get("favorite_count"),
            "replies": lg.get("reply_count"),
            "reposts": lg.get("retweet_count"),
            "quotes": lg.get("quote_count"),
            "bookmarks": lg.get("bookmark_count"),
            "views": _int_or_none((t.get("views") or {}).get("count")),
            "is_retweet": bool(lg.get("retweeted_status_result")),
        })
    return out


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_twitter_time(s):
    """'Wed Sep 16 21:30:12 +0000 2026' -> unix."""
    if not s:
        return None
    try:
        import email.utils
        return int(email.utils.parsedate_to_datetime(s).timestamp())
    except Exception:
        return None


def is_noise(text):
    """Розыгрыш, подборка, криптотокен или заказ — не запуск продукта."""
    low = (text or "").lower()
    return any(n in low for n in NOISE_MARKERS) or bool(CASHTAG.search(text or ""))


def looks_like_launch(text):
    """Пост о выходе продукта, а не мнение и не подборка чужого."""
    if is_noise(text):
        return False
    low = (text or "").lower()
    return any(m in low for m in LAUNCH_MARKERS)


def to_item(tw, now):
    """Твит -> пара (кандидат, метрики) нашей схемы."""
    url = "https://x.com/%s/status/%s" % (tw.get("screen_name") or "i", tw["id"])
    return {
        "source": SOURCE,
        "ext_id": tw["id"],
        "url": url,
        "product_url": tw.get("product_url"),
        "domain": domain_of(tw.get("product_url")),
        "title": clean_text(tw.get("text"), 200),
        "body": clean_text(tw.get("text"), 900),
        "author": tw.get("screen_name"),
        "author_followers": tw.get("followers"),
        "posted_at": _parse_twitter_time(tw.get("created_at")),
        "first_seen": now,
        "tags": "launch",
        "raw": None,
    }, {
        "likes": tw.get("likes"), "replies": tw.get("replies"),
        "reposts": tw.get("reposts"), "quotes": tw.get("quotes"),
        "bookmarks": tw.get("bookmarks"), "views": tw.get("views"),
    }


# --- запасной канал: метрики без авторизации ----------------------------
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result"


def syndication_metrics(tweet_id, timeout=20):
    """
    Метрики твита через встраиваемый виджет — без кук и без аккаунта.

    Проверено 2026-09-20: отдаёт favorite_count и conversation_count.
    Закладок и просмотров здесь нет. Канал узкий (нужен готовый id твита),
    зато не зависит от состояния аккаунта — им добираем замеры по уже
    найденным постам, когда основная сессия отвалилась.
    """
    try:
        r = requests.get(SYNDICATION,
                         params={"id": str(tweet_id), "lang": "en", "token": "a"},
                         headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return None, "HTTP %d" % r.status_code
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        return None, str(e)[:120]
    return {"likes": d.get("favorite_count"),
            "replies": d.get("conversation_count")}, None


def session_from_env():
    """Сессия из .env. Возвращает (сессия, ошибка) — None, если кук нет."""
    auth = os.environ.get("X_AUTH_TOKEN", "").strip()
    ct0 = os.environ.get("X_CT0", "").strip()
    if not auth or not ct0:
        return None, ("нет X_AUTH_TOKEN/X_CT0 в .env — твиттер-слой выключен, "
                      "остальные источники работают")
    delay = float(os.environ.get("X_DELAY", "1.6"))
    return XSession(auth, ct0, delay=delay), None


def fetch(session, queries, accounts_ids=None, per_query=40, window_hours=24):
    """
    Основной сбор: поисковые запросы + ленты избранных аккаунтов.

    Возвращает (список пар, ошибка). Частичный успех — нормальный исход:
    один запрос мог упереться в лимит, остальные принесли данные.
    """
    now = int(time.time())
    cutoff = now - window_hours * 3600
    out, errors, seen = [], [], set()

    # Ответ «слишком часто» (429) — сигнал остановиться сразу, а не
    # добивать оставшиеся запросы: каждый следующий только приближает
    # аккаунт к ограничению. Вызывающий код по «429» в тексте ошибки
    # ставит паузу на следующие прогоны.
    for q in queries:
        tweets, err = session.search(q, limit=per_query)
        if err:
            errors.append("поиск «%s»: %s" % (q[:40], err))
            if "429" in err:
                return out, "429: " + "; ".join(errors[:3])
            continue
        for tw in tweets:
            if tw["id"] in seen or tw.get("is_retweet"):
                continue
            ts = _parse_twitter_time(tw.get("created_at"))
            if ts and ts < cutoff:
                continue
            if not looks_like_launch(tw.get("text")):
                continue
            seen.add(tw["id"])
            out.append(to_item(tw, now))

    for uid in (accounts_ids or []):
        tweets, err = session.user_tweets(uid, limit=20)
        if err:
            errors.append("лента %s: %s" % (uid, err))
            if "429" in err:
                return out, "429: " + "; ".join(errors[:3])
            continue
        for tw in tweets:
            if tw["id"] in seen or tw.get("is_retweet"):
                continue
            ts = _parse_twitter_time(tw.get("created_at"))
            if ts and ts < cutoff:
                continue
            # Для отобранных вручную аккаунтов строгих маркеров запуска не
            # требуем — формулировки у них свои. Но нужен ХОТЯ БЫ один
            # признак продукта: маркер или ссылка наружу. Иначе в выдачу
            # шли просто мнения («Legalize personalized education» от
            # партнёра YC, 2026-09-26). Мусор режем и здесь.
            if is_noise(tw.get("text")):
                continue
            if not looks_like_launch(tw.get("text")) and not domain_of(tw.get("product_url")):
                continue
            seen.add(tw["id"])
            out.append(to_item(tw, now))

    return out, ("; ".join(errors[:4]) if errors else None)
