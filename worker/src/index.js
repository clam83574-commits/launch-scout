/**
 * launch-scout bot на Cloudflare Workers.
 *
 * Зачем он отдельно от bot.py: кнопки должны работать, когда ноутбук
 * закрыт. Long polling для этого не годится — ему нужен живой процесс,
 * а Actions отрабатывают и умирают. Здесь вебхук: Telegram сам стучится
 * в Worker, тот читает D1 и отвечает. Ноутбук не участвует.
 *
 * Три роли:
 *   1. Бот. Вебхук Telegram, кнопки, доступ по коду.
 *   2. Приёмник. GitHub Actions после каждого прогона присылает сюда срез
 *      находок одним JSON (POST /ingest), Worker кладёт его в D1 одной
 *      записью. Так в GitHub не нужен токен Cloudflare вовсе.
 *   3. Часы. Cron каждые 10 минут запускает прогон через workflow_dispatch.
 *      Собственное расписание GitHub для этого не годится: за 115 часов
 *      при cron «каждые 10 минут» оно выполнило 31 прогон из 688, медиана —
 *      раз в четыре часа (замерено 2026-09-25). Заодно cron — сторож: если срез
 *      не обновлялся дольше 45 минут, владелец получает предупреждение.
 *      Пять дней полной тишины из-за ровно такой поломки больше не
 *      должны выглядеть как «интересного не было».
 *
 * Worker НИЧЕГО не собирает сам: сбор и оценка живут в Python (scout.py).
 *
 * Маршруты:
 *   POST /tg      — вебхук Telegram, подписан заголовком secret_token
 *   POST /ingest  — срез находок из Actions, подписан x-ingest-secret
 *   GET  /        — проверка живости
 */

import APP_HTML from "./app.html";

const PAGE = 10;

const SRC_RU = {
  x: "X",
  hn: "Hacker News",
  yc: "Y Combinator",
  gh: "GitHub",
  ph: "Product Hunt",
};

// Адрес мини-приложения — этот же Worker. Кнопки «Обновить» и «Статус»
// ушли из клавиатуры в команды /refresh и /status: это машинерия, в
// обычный день их не трогают, а место на экране нужно категориям и
// трендам (правило PLAYBOOK «считать поверхность»).
const APP_URL = "https://launch-scout-bot.clam83574.workers.dev/app";

const KEYBOARD = {
  inline_keyboard: [
    [
      { text: "🔥 Топ-10", callback_data: "top:0" },
      { text: "🆕 За сутки", callback_data: "fresh:0" },
    ],
    [
      { text: "📈 Тренды", callback_data: "trends" },
      { text: "🗂 Категории", callback_data: "cats" },
    ],
    [{ text: "📱 Открыть приложение", web_app: { url: APP_URL } }],
  ],
};

const REPO = "clam83574-commits/launch-scout";
const WORKFLOW = "scout.yml";
// Сторож: срез старше этого — значит, сбор встал. Два пропущенных тика
// по 10 минут плюс запас на сам прогон.
const STALE_SECONDS = 45 * 60;
// Не чаще раза в шесть часов, иначе ночной сбой разбудил бы владельца
// тридцатью одинаковыми сообщениями.
const ALERT_EVERY_SECONDS = 6 * 3600;

async function meta(env, k) {
  const r = await env.DB.prepare("SELECT v FROM kv WHERE k = ?1").bind(k).first().catch(() => null);
  return r ? r.v : null;
}

async function setMeta(env, k, v) {
  await env.DB.prepare("INSERT OR REPLACE INTO kv (k, v) VALUES (?1, ?2)")
    .bind(k, String(v))
    .run();
}

/** Срез находок из D1: {updated, findings[]} или null. */
async function loadSnapshot(env) {
  const row = await env.DB.prepare("SELECT data FROM snapshot WHERE k = 'findings'")
    .first()
    .catch(() => null);
  if (!row) return null;
  try {
    return JSON.parse(row.data);
  } catch {
    return null;
  }
}

/**
 * Запустить прогон в GitHub Actions. Возвращает текст ошибки или null.
 *
 * User-Agent обязателен: без него API GitHub отвечает 403 «Request
 * forbidden by administrative rules», а fetch в Workers сам его не ставит.
 */
async function dispatchRun(env, inputs = {}) {
  if (!env.LS_GH_TOKEN) return "нет LS_GH_TOKEN";
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.LS_GH_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "launch-scout-bot",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    }
  );
  // GitHub сам сообщает срок токена в каждом ответе — запоминаем, чтобы
  // предупредить владельца заранее, а не узнать постфактум от сторожа.
  const exp = r.headers.get("github-authentication-token-expiration");
  if (exp) await setMeta(env, "gh_token_expires", exp);
  if (r.status === 204) return null;
  return `GitHub ${r.status}: ${(await r.text()).slice(0, 200)}`;
}

/** Дата истечения токена часов в unix-секундах или null. */
async function tokenExpiry(env) {
  const exp = await meta(env, "gh_token_expires");
  if (!exp) return null;
  // Формат GitHub: «2026-10-25 14:50:15 UTC».
  const t = Date.parse(exp.replace(" UTC", "Z").replace(" ", "T"));
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

/**
 * За три дня до истечения токена — напомнить владельцу, раз в сутки.
 *
 * Токен часов выдан на 30 дней (2026-09-25 → 2026-10-25). Без напоминания
 * в день истечения прогоны встали бы, и об этом рассказал бы только
 * сторож — через 45 минут тишины, то есть уже после поломки.
 */
async function tokenReminder(env, now) {
  const t = await tokenExpiry(env);
  if (!t) return;
  const daysLeft = (t - now) / 86400;
  if (daysLeft > 3) return;
  const last = Number((await meta(env, "last_token_warn")) || 0);
  if (now - last < 86400) return;
  const owner = (env.LS_BOT_ALLOW || "").split(",")[0].trim();
  if (!owner) return;
  const when = new Date(t * 1000).toISOString().slice(0, 10);
  await tg(env, "sendMessage", {
    chat_id: owner,
    text:
      `⏳ <b>Токен часов истекает ${daysLeft > 0 ? `через ${Math.ceil(daysLeft)} дн.` : "сегодня"} (${when}).</b>\n\n` +
      "После этого прогоны перестанут запускаться каждые 10 минут.\n" +
      "Перевыпустить: github.com/settings/personal-access-tokens → " +
      "launch-scout-clock → Regenerate token. Новый — в .env, строка LS_GH_TOKEN.",
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
  await setMeta(env, "last_token_warn", now);
}

// Тексты бота на трёх языках. Казахстан — первый рынок (решение владельца
// 2026-09-26), поэтому русский и казахский, затем английский. Казахские
// строки — машинный перевод: до продажи в РК показать носителю языка.
const LANGS = ["ru", "kk", "en"];
const I18N = {
  ru: {
    hello: "<b>launch-scout</b>\n\nИщет продукты в первые часы после выхода. Считает не лайки, а темп их набора, ускорение, отклонение от нормы автора и долю закладок.\n\nНаходки приходят сами. Кнопками — когда захотите сами.",
    kb_top: "🔥 Топ-10", kb_fresh: "🆕 За сутки", kb_trends: "📈 Тренды", kb_cats: "🗂 Категории", kb_app: "📱 Открыть приложение",
    empty: "Пока пусто: за это окно источники ничего не принесли.", no_more: "Больше нет.",
    next: "Дальше?", more10: "➡️ Ещё 10", start_over: "🔄 В начало",
    top_title: "Топ находок", fresh_title: "За сутки",
    cats_title: "<b>Категории за трое суток</b>", cats_empty: "Категорий пока нет: темы проставляются новым находкам с каждым прогоном.",
    trends_empty: "Трендов пока нет: нужно, чтобы темы проставились хотя бы нескольким десяткам находок.",
    fav_ok: "⭐ Взято в работу — во вкладке «В работе» приложения.", gone: "Эта находка уже выпала из свежего среза.",
    commands: "Команды: /top, /new, /trends, /lang",
    subs: "подписчиков", post: "пост", product: "продукт", clone: "Повторить", domain_age: "домену %d дн.",
    effort: { days: "дни", weeks: "недели", months: "месяцы", unclear: "неясно" }, none: ["не видно", "not visible"],
    btn_idea: "🧩 Карточка идеи", btn_fav: "⭐ В работу", lang_set: "Готово — язык: Русский.",
    btn_post: "↗ Открыть пост", btn_site: "🌐 Сайт проекта",
    idea_title: "🧩 <b>Карточка идеи</b>", why_now: "⏱ <b>Почему сейчас.</b>", who_pays: "💳 <b>Кто платит.</b>",
    analogs: "🇰🇿 <b>Аналоги в Казахстане и СНГ.</b>", mvp: "🛠 <b>MVP за две недели</b>", risk: "⚠️ <b>Главный риск.</b>",
    loc: "🌍 <b>Под Казахстан.</b>", orig: "исходный пост",
    e_day: "На сегодня лимит карточек исчерпан — завтра снова можно.", e_user: "У вас на сегодня уже %d карточек — это предел, завтра снова можно.",
    e_noai: "ИИ не подключён.", e_time: "Поиск аналогов не уложился по времени — нажмите ещё раз.",
    e_quota: "Бесплатная квота ИИ на сейчас исчерпана — попробуйте через несколько минут.", e_bad: "Карточка не собралась — нажмите ещё раз.",
    e_quota_in: "Бесплатная квота ИИ исчерпана — попробуйте через ~%d мин.",
    e_http: "ИИ ответил %d — попробуйте позже.",
    refresh_busy: "Сбор уже запущен только что. Через пару минут нажмите «🆕 За сутки».",
    refresh_ok: "Пошёл в источники. Горячее придёт само, остальное — через пару минут по кнопке «🆕 За сутки».",
    refresh_err: "Запустить сбор не удалось: %s",
    digest: "📋 <b>Сводка</b>",
  },
  kk: {
    hello: "<b>launch-scout</b>\n\nӨнімдерді шыққаннан кейінгі алғашқы сағаттарда табады. Лайктарды емес, олардың жиналу қарқынын, үдеуін, автордың әдеттегі деңгейінен ауытқуын және бетбелгілер үлесін есептейді.\n\nТабылымдар өздігінен келеді. Қаласаңыз — батырмалармен.",
    kb_top: "🔥 Үздік 10", kb_fresh: "🆕 Тәулік ішінде", kb_trends: "📈 Трендтер", kb_cats: "🗂 Санаттар", kb_app: "📱 Қосымшаны ашу",
    empty: "Әзірге бос: осы уақыт аралығында көздер ештеңе әкелмеді.", no_more: "Басқа жоқ.",
    next: "Әрі қарай?", more10: "➡️ Тағы 10", start_over: "🔄 Басына",
    top_title: "Үздік табылымдар", fresh_title: "Тәулік ішінде",
    cats_title: "<b>Үш тәуліктегі санаттар</b>", cats_empty: "Санаттар әзірге жоқ: тақырыптар әр іске қосуда жаңа табылымдарға қойылады.",
    trends_empty: "Трендтер әзірге жоқ: тақырыптар кемінде бірнеше ондаған табылымға қойылуы керек.",
    fav_ok: "⭐ Жұмысқа алынды — қосымшаның «Жұмыста» бөлімінде.", gone: "Бұл табылым жаңа тізімнен шығып кетті.",
    commands: "Командалар: /top, /new, /trends, /lang",
    subs: "жазылушы", post: "пост", product: "өнім", clone: "Қайталау", domain_age: "доменге %d күн",
    effort: { days: "күндер", weeks: "апталар", months: "айлар", unclear: "белгісіз" }, none: ["көрінбейді", "not visible", "не видно"],
    btn_idea: "🧩 Идея картасы", btn_fav: "⭐ Жұмысқа", lang_set: "Дайын — тіл: Қазақша.",
    btn_post: "↗ Постты ашу", btn_site: "🌐 Жоба сайты",
    idea_title: "🧩 <b>Идея картасы</b>", why_now: "⏱ <b>Неге қазір.</b>", who_pays: "💳 <b>Кім төлейді.</b>",
    analogs: "🇰🇿 <b>Қазақстан мен ТМД-дағы аналогтар.</b>", mvp: "🛠 <b>Екі аптадағы MVP</b>", risk: "⚠️ <b>Басты тәуекел.</b>",
    loc: "🌍 <b>Қазақстанға бейімдеу.</b>", orig: "бастапқы пост",
    e_day: "Бүгінгі карталар лимиті таусылды — ертең қайта болады.", e_user: "Бүгін сізде %d карта бар — бұл шек, ертең қайта болады.",
    e_noai: "ЖИ қосылмаған.", e_time: "Аналогтарды іздеу уақытқа сыймады — қайта басыңыз.",
    e_quota: "ЖИ-дің тегін квотасы әзірге таусылды — бірнеше минуттан кейін көріңіз.", e_bad: "Карта жиналмады — қайта басыңыз.",
    e_quota_in: "ЖИ-дің тегін квотасы таусылды — ~%d минуттан кейін көріңіз.",
    e_http: "ЖИ %d деп жауап берді — кейінірек көріңіз.",
    refresh_busy: "Жинау жаңа ғана басталды. Бірнеше минуттан кейін «🆕 Тәулік ішінде» басыңыз.",
    refresh_ok: "Көздерге кеттім. Ыстықтары өздігінен келеді, қалғандары — бірнеше минуттан кейін «🆕 Тәулік ішінде» батырмасымен.",
    refresh_err: "Жинауды іске қосу мүмкін болмады: %s",
    digest: "📋 <b>Шолу</b>",
  },
  en: {
    hello: "<b>launch-scout</b>\n\nFinds products in the first hours after launch. It scores not likes but how fast they come in, acceleration, deviation from the author’s usual level and the bookmark share.\n\nFindings arrive on their own. Use the buttons whenever you want.",
    kb_top: "🔥 Top 10", kb_fresh: "🆕 Last 24h", kb_trends: "📈 Trends", kb_cats: "🗂 Categories", kb_app: "📱 Open the app",
    empty: "Nothing yet: the sources brought nothing in this window.", no_more: "No more.",
    next: "More?", more10: "➡️ 10 more", start_over: "🔄 From the top",
    top_title: "Top findings", fresh_title: "Last 24 hours",
    cats_title: "<b>Categories, last 3 days</b>", cats_empty: "No categories yet: topics are assigned to new findings on every run.",
    trends_empty: "No trends yet: topics need to be assigned to at least a few dozen findings.",
    fav_ok: "⭐ Saved — see the “In progress” tab in the app.", gone: "This finding has dropped out of the fresh feed.",
    commands: "Commands: /top, /new, /trends, /lang",
    subs: "followers", post: "post", product: "product", clone: "To replicate", domain_age: "domain %d days old",
    effort: { days: "days", weeks: "weeks", months: "months", unclear: "unclear" }, none: ["not visible", "не видно"],
    btn_idea: "🧩 Idea card", btn_fav: "⭐ Save", lang_set: "Done — language: English.",
    btn_post: "↗ Open post", btn_site: "🌐 Project site",
    idea_title: "🧩 <b>Idea card</b>", why_now: "⏱ <b>Why now.</b>", who_pays: "💳 <b>Who pays.</b>",
    analogs: "🇰🇿 <b>Analogs in Kazakhstan and the CIS.</b>", mvp: "🛠 <b>Two-week MVP</b>", risk: "⚠️ <b>Main risk.</b>",
    loc: "🌍 <b>For Kazakhstan.</b>", orig: "original post",
    e_day: "Today’s card limit is used up — try again tomorrow.", e_user: "You already have %d cards today — that is the limit, try again tomorrow.",
    e_noai: "AI is not connected.", e_time: "The analog search ran out of time — tap again.",
    e_quota: "The free AI quota is used up for now — try again in a few minutes.", e_bad: "The card did not come together — tap again.",
    e_quota_in: "The free AI quota is used up — try again in ~%d min.",
    e_http: "The AI answered %d — try again later.",
    refresh_busy: "A run has just started. Tap “🆕 Last 24h” in a couple of minutes.",
    refresh_ok: "Checking the sources. Hot findings arrive on their own, the rest via “🆕 Last 24h” in a couple of minutes.",
    refresh_err: "Could not start a run: %s",
    digest: "📋 <b>Digest</b>",
  },
};
const L = (lang) => I18N[lang] || I18N.ru;
const fmt = (s, ...a) => a.reduce((acc, v) => acc.replace(/%[ds]/, String(v)), s);

// Пост и сайт проекта — кнопками под сообщением: ссылки текстом в конце
// карточки на телефоне не замечали (отзыв владельца 2026-09-27).
const okUrl = (u) => typeof u === "string" && u.length < 1500 && /^https?:\/\/[^\s"<>]+$/.test(u);

function linkRow(row, s) {
  const out = [];
  if (okUrl(row.url)) out.push({ text: s.btn_post, url: row.url });
  if (okUrl(row.product_url) && row.product_url !== row.url) out.push({ text: s.btn_site, url: row.product_url });
  return out;
}

/**
 * Сообщение с кнопками-ссылками. Редкий адрес Telegram может отвергнуть
 * как кнопку (BUTTON_URL_INVALID) — тогда шлём без ссылок-кнопок, но с
 * остальными: терять находку из-за кнопки нельзя, ссылки есть и в тексте.
 */
async function sendWithLinks(env, payload, links, rows = []) {
  if (links.length) {
    const r = await tg(env, "sendMessage", { ...payload, reply_markup: { inline_keyboard: [links, ...rows] } });
    if (r && r.ok) return r;
  }
  return tg(env, "sendMessage", rows.length ? { ...payload, reply_markup: { inline_keyboard: rows } } : payload);
}

function keyboardFor(lang) {
  const s = L(lang);
  return {
    inline_keyboard: [
      [{ text: s.kb_top, callback_data: "top:0" }, { text: s.kb_fresh, callback_data: "fresh:0" }],
      [{ text: s.kb_trends, callback_data: "trends" }, { text: s.kb_cats, callback_data: "cats" }],
      [{ text: s.kb_app, web_app: { url: APP_URL } }],
    ],
  };
}

const LANG_PICKER = {
  inline_keyboard: [[
    { text: "Русский", callback_data: "lang:ru" },
    { text: "Қазақша", callback_data: "lang:kk" },
    { text: "English", callback_data: "lang:en" },
  ]],
};

async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.LS_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}

const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

function num(n) {
  if (n === null || n === undefined) return "—";
  n = Number(n);
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

/** Полный ИИ-разбор ровно на этом языке (разборы до 2026-09-27 — только ru). */
function txExact(row, lang) {
  const ai = row.ai || null;
  if (!ai) return null;
  const i = ai.i18n || (ai.summary ? { ru: ai } : null);
  return (i && i[lang]) || null;
}

/**
 * Текст находки на языке читателя: разбор, иначе выжимка-фраза. Перевода
 * нет — казахскому читателю русский (в Казахстане его читают почти все),
 * английскому — пустая строка: карточка покажет исходный пост.
 */
function textFor(row, lang) {
  const own = txExact(row, lang), g = row.gist || {};
  if (own && own.summary) return own.summary;
  if (g[lang]) return g[lang];
  if (lang === "kk") {
    const ru = txExact(row, "ru");
    return (ru && ru.summary) || g.ru || "";
  }
  return "";
}

/**
 * Карточка находки. Порядок строк повторяет порядок решения:
 * что это -> насколько горячо и почему -> куда идти смотреть.
 */
function card(row, lang = "ru") {
  const s = L(lang);
  const head = row.tier === "hot" ? "🔥" : "•";
  const lines = [`${head} <b>${esc(row.title || "—")}</b>  <code>${row.score}</code>`];
  let sub = SRC_RU[row.source] || row.source;
  if (row.author) {
    sub += ` · @${esc(row.author)}`;
    if (row.author_followers) sub += ` (${num(row.author_followers)} ${s.subs})`;
  }
  lines.push(`<i>${sub}</i>`);
  const own = txExact(row, lang), text = textFor(row, lang);
  if (text) {
    lines.push("", "🧠 " + esc(text));
    // Срок повтора — слово из словаря, есть на любом языке; пояснение и
    // монетизация — только из разбора на языке читателя.
    const eff = s.effort[(row.ai || {}).clone_effort];
    if (eff) lines.push(`🛠 ${s.clone}: ${eff}` + (own && own.clone_note ? " — " + esc(own.clone_note) : ""));
    const money = ((own && own.monetization) || "").trim();
    if (money && !s.none.includes(money.toLowerCase())) lines.push("💰 " + esc(money));
  } else {
    const body = (row.body || "").trim();
    if (body && body !== (row.title || "").trim()) lines.push("", esc(body.slice(0, 420)));
  }
  const nums = [["♥", row.likes], ["💬", row.replies], ["🔁", row.reposts], ["🔖", row.bookmarks], ["👁", row.views]]
    .filter(([, v]) => v).map(([ic, v]) => `${ic} ${num(v)}`);
  if (nums.length) lines.push("", nums.join(" · "));
  let tail = `<a href="${esc(row.url || "")}">${s.post}</a>`;
  if (row.product_url && row.product_url !== row.url) {
    tail += ` · <a href="${esc(row.product_url)}">${esc(row.domain || s.product)}</a>`;
  }
  if (row.domain_age_days !== null && row.domain_age_days !== undefined) tail += " · " + fmt(s.domain_age, row.domain_age_days);
  lines.push("", tail);
  return lines.join("\n");
}

async function listTop(env, chatId, offset, windowHours, title, topic = null, lang = "ru") {
  const s = L(lang);
  const cutoff = Math.floor(Date.now() / 1000) - windowHours * 3600;
  const snap = await loadSnapshot(env);
  const all = (snap && snap.findings) || [];
  // Один продукт — одна карточка: иначе десятка уходит на то, что всплыло
  // сразу в трёх источниках.
  const seen = new Set();
  const pool = [];
  for (const f of all) {
    if (f.first_seen < cutoff || !(f.score > 0)) continue;
    if (topic && !(f.topics || []).includes(topic)) continue;
    if (f.domain) {
      if (seen.has(f.domain)) continue;
      seen.add(f.domain);
    }
    pool.push(f);
  }
  pool.sort((a, b) => b.score - a.score);
  const results = pool.slice(offset, offset + PAGE);
  if (!results.length) {
    await tg(env, "sendMessage", { chat_id: chatId, text: offset === 0 ? s.empty : s.no_more, reply_markup: keyboardFor(lang) });
    return;
  }
  await tg(env, "sendMessage", { chat_id: chatId, text: `<b>${esc(title)}</b> — ${results.length}`, parse_mode: "HTML" });
  for (const row of results) {
    await sendWithLinks(env, { chat_id: chatId, text: card(row, lang), parse_mode: "HTML", disable_web_page_preview: true },
      linkRow(row, s), [[{ text: s.btn_idea, callback_data: `idea:${row.id}` }, { text: s.btn_fav, callback_data: `fav:${row.id}` }]]);
  }
  const more = offset + PAGE;
  const key = topic ? `cat:${topic}` : windowHours === 24 ? "fresh" : "top";
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: s.next,
    reply_markup: {
      inline_keyboard: [
        [{ text: s.more10, callback_data: `${key}:${more}` }, { text: s.start_over, callback_data: `${key}:0` }],
        [{ text: s.kb_app, web_app: { url: APP_URL } }],
      ],
    },
  });
}

function topicName(snap, t, lang) {
  const tr = (snap && snap.trends) || {};
  return ((tr.topic_names || {})[lang] || tr.topic_ru || {})[t] || t;
}

/** Темы за трое суток с числом находок — кнопками. */
async function categories(env, chatId, lang = "ru") {
  const s = L(lang);
  const snap = await loadSnapshot(env);
  const all = (snap && snap.findings) || [];
  const cutoff = Math.floor(Date.now() / 1000) - 72 * 3600;
  const count = {};
  for (const f of all) {
    if (f.first_seen < cutoff) continue;
    for (const tp of f.topics || []) count[tp] = (count[tp] || 0) + 1;
  }
  const top = Object.entries(count).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (!top.length) {
    await tg(env, "sendMessage", { chat_id: chatId, text: s.cats_empty, reply_markup: keyboardFor(lang) });
    return;
  }
  const rows = [];
  for (let i = 0; i < top.length; i += 2) {
    rows.push(top.slice(i, i + 2).map(([tp, n]) => ({ text: `${topicName(snap, tp, lang)} · ${n}`, callback_data: `cat:${tp}:0`.slice(0, 64) })));
  }
  await tg(env, "sendMessage", { chat_id: chatId, text: s.cats_title, parse_mode: "HTML", reply_markup: { inline_keyboard: rows } });
}

async function trendsMsg(env, chatId, lang = "ru") {
  const snap = await loadSnapshot(env);
  const tt = snap && snap.trends && snap.trends.text;
  const text = typeof tt === "string" ? tt : tt ? tt[lang] || tt.ru : null;
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: text || L(lang).trends_empty,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    reply_markup: keyboardFor(lang),
  });
}

async function status(env, chatId) {
  const now = Math.floor(Date.now() / 1000);
  const snap = await loadSnapshot(env);
  const all = (snap && snap.findings) || [];
  const dayN = all.filter((f) => f.first_seen >= now - 86400).length;
  const bySrc = {};
  for (const f of all) {
    const s = (bySrc[f.source] = bySrc[f.source] || { n: 0, last: 0 });
    s.n += 1;
    s.last = Math.max(s.last, f.first_seen || 0);
  }

  const lines = ["<b>Состояние</b>", "", `в срезе: ${all.length}, за сутки: ${dayN}`, "", "<b>Источники</b>"];
  for (const [src, s] of Object.entries(bySrc).sort((a, b) => b[1].n - a[1].n)) {
    const ago = Math.round((now - (s.last || now)) / 60);
    lines.push(`${SRC_RU[src] || src}: ${s.n}, свежайшая ${ago} мин назад`);
  }
  if (snap && snap.updated) {
    const mins = Math.round((now - snap.updated) / 60);
    const warn = now - snap.updated > STALE_SECONDS ? " ⚠️ сбор отстаёт" : "";
    lines.push("", `последний сбор: ${mins} мин назад${warn}`);
  } else {
    lines.push("", "срез ещё ни разу не приходил");
  }
  const lastDispatch = await meta(env, "last_dispatch_error");
  if (lastDispatch) lines.push(`запуск сбора: <i>${esc(lastDispatch).slice(0, 160)}</i>`);
  const exp = await tokenExpiry(env);
  if (exp) {
    const days = Math.floor((exp - now) / 86400);
    lines.push(`часы: токен GitHub действует ещё ${days} дн. (до ${new Date(exp * 1000).toISOString().slice(0, 10)})`);
  }
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: lines.join("\n"),
    parse_mode: "HTML",
    reply_markup: KEYBOARD,
  });
}

/**
 * Доступ: владелец из LS_BOT_ALLOW плюс все, кто ввёл код.
 *
 * Код, а не список id: чтобы добавить человека, не надо заранее узнавать
 * его Telegram id — достаточно переслать ему ссылку на бота и код. Он
 * вводит, бот запоминает его id в таблице access, дальше код не нужен.
 *
 * Открытым «для всех» бот не делается сознательно: найти его в поиске
 * Telegram может кто угодно, а внутри — находки, ради которых всё считается.
 */
function isOwner(env, chatId) {
  return (env.LS_BOT_ALLOW || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .includes(String(chatId));
}

async function hasAccess(env, chatId) {
  if (isOwner(env, chatId)) return true;
  const row = await env.DB.prepare("SELECT 1 FROM access WHERE chat_id = ?1")
    .bind(String(chatId))
    .first()
    .catch(() => null);
  return !!row;
}

const MAX_TRIES = 5;
const LOCK_SECONDS = 3600;

/**
 * Попытка входа по коду. Возвращает текст ответа.
 *
 * Перебор шестизначного кода машиной — минуты, поэтому счётчик попыток
 * обязателен: пять промахов запирают этот чат на час. Счётчик живёт в той
 * же таблице, так что переживает перезапуск Worker.
 */
async function tryCode(env, chatId, text, who) {
  const now = Math.floor(Date.now() / 1000);
  const id = String(chatId);
  const row = await env.DB.prepare(
    "SELECT tries, locked_until FROM access_tries WHERE chat_id = ?1"
  )
    .bind(id)
    .first()
    .catch(() => null);

  if (row && row.locked_until && row.locked_until > now) {
    const mins = Math.ceil((row.locked_until - now) / 60);
    return `Слишком много попыток — попробуйте через ${mins} мин.\nТым көп әрекет — ${mins} минуттан кейін көріңіз.\nToo many attempts — try again in ${mins} min.`;
  }

  const code = (env.LS_ACCESS_CODE || "").trim();
  const given = text.replace(/^\/code\s*/i, "").trim();
  if (code && given && given.toLowerCase() === code.toLowerCase()) {
    await env.DB.prepare(
      "INSERT OR REPLACE INTO access (chat_id, who, granted_at) VALUES (?1, ?2, ?3)"
    )
      .bind(id, who || "", now)
      .run();
    await env.DB.prepare("DELETE FROM access_tries WHERE chat_id = ?1").bind(id).run();
    return null; // null = пустить
  }

  const tries = (row ? row.tries : 0) + 1;
  const locked = tries >= MAX_TRIES ? now + LOCK_SECONDS : 0;
  await env.DB.prepare(
    "INSERT OR REPLACE INTO access_tries (chat_id, tries, locked_until) VALUES (?1, ?2, ?3)"
  )
    .bind(id, tries, locked)
    .run();
  if (locked) return "Слишком много попыток — ввод закрыт на час.\nТым көп әрекет — енгізу бір сағатқа жабылды.\nToo many attempts — entry locked for an hour.";
  const left = MAX_TRIES - tries;
  return `Код не подошёл, осталось попыток: ${left}.\nКод сәйкес келмеді, қалған әрекет: ${left}.\nWrong code, attempts left: ${left}.`;
}

async function handleUpdate(env, update) {
  const msg = update.message || update.edited_message;
  const cb = update.callback_query;
  const chatId = msg ? msg.chat.id : cb ? cb.message.chat.id : null;
  if (!chatId) return;
  if (cb) {
    // Ответить Telegram надо сразу, иначе кнопка «крутится» до таймаута.
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
  }
  const data = cb ? cb.data || "" : "";
  const raw = msg ? (msg.text || "").trim() : "";
  const text = raw.toLowerCase();

  // /whoami отвечает всем: человеку без доступа надо чем-то представиться владельцу.
  if (text.startsWith("/whoami")) {
    await tg(env, "sendMessage", { chat_id: chatId, text: `Telegram id: <code>${chatId}</code>`, parse_mode: "HTML" });
    return;
  }

  await ensureTables(env);
  if (!(await hasAccess(env, chatId))) {
    const from = msg && msg.from ? msg.from : cb && cb.from ? cb.from : {};
    const who = from.username ? "@" + from.username : from.first_name || "";
    // Любое сообщение незнакомца — попытка ввести код. Язык он ещё не
    // выбрал, поэтому отвечаем сразу на трёх.
    if (raw && !raw.startsWith("/start") && !raw.startsWith("/help")) {
      const err = await tryCode(env, chatId, raw, who);
      if (err) {
        await tg(env, "sendMessage", { chat_id: chatId, text: err });
        return;
      }
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: "✅ Доступ открыт · Қолжетімділік ашылды · Access granted\n\nВыберите язык · Тілді таңдаңыз · Choose your language",
        reply_markup: LANG_PICKER,
      });
      return;
    }
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "🔒 Бот закрытый — пришлите код доступа одним сообщением.\n🔒 Бот жабық — қолжетімділік кодын бір хабарламамен жіберіңіз.\n🔒 Private bot — send your access code in one message.",
    });
    return;
  }

  const prefs = await getPrefs(env, chatId);
  if (data.startsWith("lang:")) {
    const lang = LANGS.includes(data.slice(5)) ? data.slice(5) : "ru";
    await setPrefs(env, chatId, { lang });
    await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).lang_set + "\n\n" + L(lang).hello, parse_mode: "HTML", reply_markup: keyboardFor(lang) });
    return;
  }
  // Язык спрашиваем при первом входе и по /lang. Нажатие кнопки под
  // уведомлением не перехватываем: человек ждёт карточку, а не вопрос —
  // до выбора языка такие ответы идут по-русски.
  if (text.startsWith("/lang") || (!prefs.lang && !data)) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Выберите язык · Тілді таңдаңыз · Choose your language", reply_markup: LANG_PICKER });
    return;
  }
  const lang = prefs.lang || "ru";
  const s = L(lang);

  if (data.startsWith("idea:")) {
    await ideaForChat(env, chatId, data.slice(5), lang);
  } else if (data.startsWith("fav:")) {
    const f = await findFinding(env, data.slice(4));
    if (f) await saveFav(env, chatId, f);
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: f ? s.fav_ok : s.gone,
      reply_markup: f ? { inline_keyboard: [[{ text: s.kb_app, web_app: { url: APP_URL } }]] } : undefined,
    });
  } else if (data === "cats") {
    await categories(env, chatId, lang);
  } else if (data.startsWith("cat:")) {
    const parts = data.split(":");
    const off = Number(parts.pop()) || 0;
    const topic = parts.slice(1).join(":");
    const snap = await loadSnapshot(env);
    await listTop(env, chatId, off, 72, topicName(snap, topic, lang), topic, lang);
  } else if (data === "trends" || text.startsWith("/trends")) {
    await trendsMsg(env, chatId, lang);
  } else if (data.startsWith("top:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 72, s.top_title, null, lang);
  } else if (data.startsWith("fresh:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 24, s.fresh_title, null, lang);
  } else if (data === "status") {
    await status(env, chatId);
  } else if (data === "refresh" || text.startsWith("/refresh")) {
    // Не чаще раза в две минуты на всех: серия нажатий иначе выстроила бы
    // очередь одинаковых прогонов.
    const now = Math.floor(Date.now() / 1000);
    const last = Number((await meta(env, "last_manual_refresh")) || 0);
    if (now - last < 120) {
      await tg(env, "sendMessage", { chat_id: chatId, text: s.refresh_busy, reply_markup: keyboardFor(lang) });
      return;
    }
    const err = await dispatchRun(env, { digest: "no" });
    if (err) {
      await tg(env, "sendMessage", { chat_id: chatId, text: fmt(s.refresh_err, err), reply_markup: keyboardFor(lang) });
      return;
    }
    await setMeta(env, "last_manual_refresh", now);
    await tg(env, "sendMessage", { chat_id: chatId, text: s.refresh_ok, reply_markup: keyboardFor(lang) });
  } else if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(env, "sendMessage", { chat_id: chatId, text: s.hello, parse_mode: "HTML", reply_markup: keyboardFor(lang) });
  } else if (text.startsWith("/top")) {
    await listTop(env, chatId, 0, 72, s.top_title, null, lang);
  } else if (text.startsWith("/new")) {
    await listTop(env, chatId, 0, 24, s.fresh_title, null, lang);
  } else if (text.startsWith("/status")) {
    await status(env, chatId);
  } else if (text) {
    await tg(env, "sendMessage", { chat_id: chatId, text: s.commands, reply_markup: keyboardFor(lang) });
  }
}

/**
 * Сторож: если срез давно не обновлялся — написать владельцу.
 *
 * Сбор умирает молча: пять дней GitHub выполнял 5% прогонов, бот не
 * прислал ни одного сообщения, и это выглядело как «интересного не было».
 * Теперь тишина дольше 45 минут сама превращается в сообщение.
 */
async function watchdog(env, now) {
  const snap = await loadSnapshot(env);
  const updated = snap && snap.updated ? snap.updated : 0;
  if (updated && now - updated <= STALE_SECONDS) return;
  const lastAlert = Number((await meta(env, "last_stale_alert")) || 0);
  if (now - lastAlert < ALERT_EVERY_SECONDS) return;
  const owner = (env.LS_BOT_ALLOW || "").split(",")[0].trim();
  if (!owner) return;
  const mins = updated ? Math.round((now - updated) / 60) : null;
  const why = (await meta(env, "last_dispatch_error")) || "запуск проходит, а срез не приходит — смотреть лог прогона в Actions";
  await tg(env, "sendMessage", {
    chat_id: owner,
    text:
      `⚠️ <b>Сбор молчит${mins !== null ? ` ${mins} мин` : ""}.</b>\n\n` +
      `Находки не обновляются, уведомлений не будет, пока это не починить.\n` +
      `<i>${esc(why).slice(0, 300)}</i>`,
    parse_mode: "HTML",
  });
  await setMeta(env, "last_stale_alert", now);
}

// ---------------------------------------------------------------------------
// Карточка идеи: «почему сейчас», кто платит, аналоги в Казахстане и СНГ,
// MVP на две недели, главный риск. Рынок — Казахстан первым (решение
// владельца 2026-09-26), затем англоязычный.
//
// Аналоги ищет сама модель встроенным веб-поиском Groq (browser_search):
// память модели для этого не годится — она придумала бы правдоподобные
// названия. В карточку попадают только найденные с адресом.
//
// Один запрос с поиском — около 24 тыс. токенов (замерено 2026-09-26), а у
// бесплатного Groq на модель 200 тыс. токенов в СУТКИ, общих с разборами
// находок (PLAYBOOK, урок 2026-09-27): это считанные карточки в день на всех.
// Поэтому карточка собирается только по нажатию, одна на находку, общая для
// всех и сразу на трёх языках — поиск (основная часть токенов) делается
// один раз, а не под каждый язык.
// ---------------------------------------------------------------------------
const IDEA_MAX_PER_DAY = 40;
const IDEA_MAX_PER_USER = 10;

const IDEA_SYSTEM = `You prepare an "idea card" for a founder in Kazakhstan who builds or localizes products that just launched abroad. Market priority: Kazakhstan first, then Russia and the CIS, then the English-speaking world.
Use web search to check whether analogs already exist in Kazakhstan, then in Russia/CIS. Search in Russian and Kazakh as well as English.
Reply with JSON only, no markdown:
{"analogs": [{"name": "...", "url": "...", "market": "KZ|RU|CIS|global", "note": {"ru": "...", "kk": "...", "en": "..."}}], "ru": {"why_now": "...", "who_pays": "...", "analog_verdict": "...", "mvp": ["...", "..."], "main_risk": "...", "localization": "..."}, "kk": {...the same fields...}, "en": {...the same fields...}}
Rules: the ru, kk and en blocks say the same thing in Russian, Kazakh (Cyrillic script) and English — plain and short. why_now: 1-2 sentences grounded ONLY in the traction numbers given and the product itself — never invent numbers. who_pays: who exactly pays and roughly how. analogs: only services you actually found, each with a real URL; at most 4; an empty list if none. analog_verdict: one line — is the Kazakhstan/CIS niche free, partly taken or crowded. mvp: 3-5 bullets a small team can ship in two weeks. main_risk: one line. localization: one line on what to adapt for Kazakhstan (payments such as Kaspi, Russian/Kazakh language, local rules).`;

/**
 * Карточка на одном языке из сохранённой трёхъязычной. Карточки до
 * 2026-09-27 — плоские и русские: русскому читателю отдаём как есть,
 * остальным — null (соберётся новая, сразу на трёх языках).
 */
function ideaFor(data, lang) {
  if (!data) return null;
  if (!data.ru && typeof data.why_now === "string") return lang === "ru" ? data : null;
  const tx = data[lang] || data.ru || data.en;
  if (!tx || typeof tx.why_now !== "string") return null;
  const note = (n) => (n && typeof n === "object" ? n[lang] || n.ru || n.en || "" : n || "");
  return { ...tx, analogs: (data.analogs || []).map((a) => ({ name: a.name, url: a.url, market: a.market, note: note(a.note) })) };
}

/** «Please try again in 11m7.44s» из ответа 429 — в минутах, вверх. */
function waitMinutes(text) {
  const m = /try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?/.exec(text || "");
  if (!m) return null;
  return Math.max(1, Math.ceil(Number(m[1] || 0) * 60 + Number(m[2] || 0) + Number(m[3] || 0) / 60));
}

async function ideaCard(env, f, uid, lang = "ru") {
  const s = L(lang);
  const row = await env.DB.prepare("SELECT data FROM idea_cards WHERE item_id = ?1")
    .bind(Number(f.id)).first().catch(() => null);
  if (row && row.data) {
    let saved = null;
    try { saved = JSON.parse(row.data); } catch { saved = null; }
    const card = ideaFor(saved, lang);
    if (card) return { card, cached: true };
  }
  const day = new Date().toISOString().slice(0, 10);
  const total = Number((await meta(env, "idea_calls_" + day)) || 0);
  const mine = Number((await meta(env, `idea_user_${uid}_${day}`)) || 0);
  if (total >= IDEA_MAX_PER_DAY) return { error: s.e_day };
  if (mine >= IDEA_MAX_PER_USER) return { error: fmt(s.e_user, mine) };
  if (!env.LS_GROQ_KEY) return { error: s.e_noai };

  const ai = f.ai || {};
  const tx = ai.i18n ? ai.i18n.en || ai.i18n.ru : ai;
  const user = [
    `Product: ${f.title}`,
    f.product_url ? `URL: ${f.product_url}` : "",
    tx && tx.summary ? `What it is: ${tx.summary}` : `Post: ${(f.body || "").slice(0, 800)}`,
    `Source: ${f.source}; traction numbers: likes/points ${f.likes ?? "?"}, replies ${f.replies ?? "?"}, bookmarks ${f.bookmarks ?? "?"}; score ${f.score} because: ${f.breakdown || "n/a"}`,
  ].filter(Boolean).join("\n");

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 28000);
  let r;
  try {
    r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      signal: ctrl.signal,
      headers: { authorization: `Bearer ${env.LS_GROQ_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({
        model: "openai/gpt-oss-120b",
        messages: [{ role: "system", content: IDEA_SYSTEM }, { role: "user", content: user }],
        tools: [{ type: "browser_search" }],
        tool_choice: "auto",
        reasoning_effort: "low",
        max_completion_tokens: 4000,
        temperature: 0.2,
      }),
    });
  } catch (e) {
    return { error: s.e_time };
  } finally {
    clearTimeout(timer);
  }
  await setMeta(env, "idea_calls_" + day, total + 1);
  await setMeta(env, `idea_user_${uid}_${day}`, mine + 1);
  if (r.status === 429) {
    const mins = waitMinutes(await r.text().catch(() => ""));
    return { error: mins ? fmt(s.e_quota_in, mins) : s.e_quota };
  }
  if (!r.ok) return { error: fmt(s.e_http, r.status) };
  let data;
  try {
    const content = ((await r.json()).choices[0].message.content || "").trim()
      .replace(/^```(?:json)?/, "").replace(/```$/, "").trim();
    data = JSON.parse(content.slice(content.indexOf("{"), content.lastIndexOf("}") + 1));
  } catch {
    return { error: s.e_bad };
  }
  if (!data || !data.ru || typeof data.ru.why_now !== "string") return { error: s.e_bad };
  data.analogs = (Array.isArray(data.analogs) ? data.analogs : [])
    .filter((a) => a && a.name && /^https?:\/\//.test(a.url || "")).slice(0, 4);
  await env.DB.prepare("INSERT OR REPLACE INTO idea_cards (item_id, data, ts) VALUES (?1, ?2, ?3)")
    .bind(Number(f.id), JSON.stringify(data), Math.floor(Date.now() / 1000)).run();
  return { card: ideaFor(data, lang), cached: false };
}

function ideaCardHtml(f, card, lang = "ru") {
  const s = L(lang);
  const lines = [`${s.idea_title} — ${esc(f.title)}`, ""];
  lines.push(`${s.why_now} ${esc(card.why_now)}`);
  if (card.who_pays) lines.push(`${s.who_pays} ${esc(card.who_pays)}`);
  lines.push("", `${s.analogs} ${esc(card.analog_verdict || "")}`);
  for (const a of card.analogs || []) {
    lines.push(`• <a href="${esc(a.url)}">${esc(a.name)}</a>${a.market ? ` (${esc(a.market)})` : ""}${a.note ? " — " + esc(a.note) : ""}`);
  }
  if ((card.mvp || []).length) {
    lines.push("", s.mvp);
    for (const m of card.mvp.slice(0, 5)) lines.push("• " + esc(m));
  }
  if (card.main_risk) lines.push("", `${s.risk} ${esc(card.main_risk)}`);
  if (card.localization) lines.push(`${s.loc} ${esc(card.localization)}`);
  if (f.product_url) lines.push("", `<a href="${esc(f.product_url)}">${esc(f.domain || s.product)}</a> · <a href="${esc(f.url || "")}">${s.orig}</a>`);
  return lines.join("\n").slice(0, 4000);
}

// Один раз на экземпляр Worker: иначе каждое нажатие кнопки стоило бы
// пяти лишних запросов к D1.
let tablesReady = false;

async function ensureTables(env) {
  if (tablesReady) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS idea_cards (item_id INTEGER PRIMARY KEY, data TEXT, ts INTEGER)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS prefs (user_id TEXT PRIMARY KEY, topics TEXT, ts INTEGER, lang TEXT, audience TEXT, notify TEXT)"),
  ]);
  // Таблица prefs создавалась раньше без языка и фильтров — дополняем на
  // месте. Повторное добавление колонки D1 отклоняет, это ожидаемо.
  for (const col of ["lang", "audience", "notify"]) {
    await env.DB.prepare(`ALTER TABLE prefs ADD COLUMN ${col} TEXT`).run().catch(() => null);
  }
  tablesReady = true;
}

async function findFinding(env, id) {
  const snap = await loadSnapshot(env);
  return ((snap && snap.findings) || []).find((x) => Number(x.id) === Number(id)) || null;
}

async function ideaForChat(env, chatId, id, lang = "ru") {
  await ensureTables(env);
  const f = await findFinding(env, id);
  if (!f) {
    await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).gone });
    return;
  }
  await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });
  const res = await ideaCard(env, f, chatId, lang);
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: res.error ? res.error : ideaCardHtml(f, res.card, lang),
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
}

// --- избранное из чата ------------------------------------------------------
async function saveFav(env, uid, f) {
  await env.DB.prepare(
    "CREATE TABLE IF NOT EXISTS favorites (user_id TEXT, item_id INTEGER, card TEXT, ts INTEGER, " +
      "PRIMARY KEY (user_id, item_id))"
  ).run();
  const card = { id: f.id, title: f.title, url: f.url, product_url: f.product_url, domain: f.domain,
    source: f.source, author: f.author, score: f.score, tier: f.tier, topics: f.topics, ai: f.ai,
    gist: f.gist, audience: f.audience,
    body: (f.body || "").slice(0, 400), first_seen: f.first_seen };
  await env.DB.prepare(
    "INSERT OR REPLACE INTO favorites (user_id, item_id, card, ts) VALUES (?1, ?2, ?3, ?4)"
  ).bind(String(uid), Number(f.id), JSON.stringify(card), Math.floor(Date.now() / 1000)).run();
}

// --- личные категории и рассылка ---------------------------------------------
const NOTIFY_DEFAULT = { hot: true, digest: true, trends: true };

async function getPrefs(env, uid) {
  const row = await env.DB.prepare("SELECT topics, lang, audience, notify FROM prefs WHERE user_id = ?1")
    .bind(String(uid)).first().catch(() => null);
  const j = (v, d) => { try { return v ? JSON.parse(v) : d; } catch { return d; } };
  const arr = (v) => (Array.isArray(v) && v.length ? v : null);
  return {
    lang: row && LANGS.includes(row.lang) ? row.lang : null,
    topics: arr(j(row && row.topics, null)),
    audience: arr(j(row && row.audience, null)),
    notify: { ...NOTIFY_DEFAULT, ...j(row && row.notify, {}) },
  };
}

async function setPrefs(env, uid, patch) {
  const cur = await getPrefs(env, uid);
  const next = { ...cur, ...patch };
  await env.DB.prepare(
    "INSERT OR REPLACE INTO prefs (user_id, topics, ts, lang, audience, notify) VALUES (?1, ?2, ?3, ?4, ?5, ?6)"
  ).bind(String(uid), JSON.stringify(next.topics || []), Math.floor(Date.now() / 1000), next.lang || null,
    JSON.stringify(next.audience || []), JSON.stringify(next.notify || NOTIFY_DEFAULT)).run();
  return next;
}

async function subscribers(env) {
  const owners = (env.LS_BOT_ALLOW || "").split(",").map((s) => s.trim()).filter(Boolean);
  const { results } = await env.DB.prepare("SELECT chat_id FROM access").all().catch(() => ({ results: [] }));
  return [...new Set([...owners, ...(results || []).map((r) => String(r.chat_id))])];
}

const wants = (topics, filter) => !filter || (topics || []).some((t) => filter.includes(t));
// Аудитория неизвестна (ИИ ещё не разметил) — не отсекаем: лучше лишняя
// находка, чем пропущенная.
const wantsAud = (aud, filter) => !filter || !(aud || []).length || aud.some((a) => filter.includes(a));

/**
 * Рассылка находок подписчикам по их настройкам.
 *
 * Раньше прогон слал уведомления сам и только владельцу: друг с кодом
 * доступа видел кнопки, но ни одного уведомления не получал. Теперь прогон
 * отдаёт горячее сюда уже на трёх языках, а Worker, который знает
 * подписчиков, раздаёт каждому своё: на его языке, по его категориям и
 * аудитории (B2B/B2C/B2G) и только те виды уведомлений, что он не выключил.
 */
async function notifyAll(env, body) {
  const subs = await subscribers(env);
  let sent = 0;
  for (const uid of subs) {
    const p = await getPrefs(env, uid);
    const lang = p.lang || "ru";
    const s = L(lang);
    if (p.notify.hot !== false) {
      for (const h of body.hot || []) {
        if (!wants(h.topics, p.topics) || !wantsAud(h.audience, p.audience)) continue;
        const r = await sendWithLinks(env, {
          chat_id: uid,
          text: (h.texts && h.texts[lang]) || h.text,
          parse_mode: "HTML",
        }, linkRow(h, s), [[
          { text: s.btn_idea, callback_data: `idea:${h.id}` },
          { text: s.btn_fav, callback_data: `fav:${h.id}` },
        ]]);
        if (r && r.ok) sent++;
      }
    }
    if (p.notify.digest !== false && body.digest) {
      const lines = (body.digest.items || []).filter((d) => wants(d.topics, p.topics) && wantsAud(d.audience, p.audience));
      if (lines.length) {
        const head = (body.digest.heads && body.digest.heads[lang]) || body.digest.head || s.digest;
        const r = await tg(env, "sendMessage", {
          chat_id: uid,
          text: `${head} — ${lines.length}\n\n` + lines.map((d) => (d.lines && d.lines[lang]) || d.line).join("\n"),
          parse_mode: "HTML",
          disable_web_page_preview: true,
        });
        if (r && r.ok) sent++;
      }
    }
    for (const b of body.broadcast || []) {
      if (typeof b !== "string" && b.kind === "trends" && p.notify.trends === false) continue;
      const text = typeof b === "string" ? b : (b.texts && b.texts[lang]) || b.text;
      if (!text) continue;
      const r = await tg(env, "sendMessage", { chat_id: uid, text, parse_mode: "HTML", disable_web_page_preview: true });
      if (r && r.ok) sent++;
    }
  }
  return { subscribers: subs.length, sent };
}

/**
 * Проверка входа в мини-приложение по подписи Telegram (initData).
 *
 * Страница приложения открыта всем, а данные — только своим: Telegram
 * подписывает параметры запуска ключом, производным от токена бота, и
 * подделать подпись без токена нельзя. Схема из документации Telegram:
 * secret = HMAC_SHA256(key="WebAppData", msg=bot_token),
 * hash   = hex(HMAC_SHA256(key=secret, msg=data_check_string)),
 * где data_check_string — все поля, кроме hash, по алфавиту через \n.
 * Подпись старше суток не принимается: иначе перехваченная ссылка
 * работала бы вечно.
 */
async function hmac(keyBytes, msg) {
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg)));
}

async function appUser(env, request) {
  const init = request.headers.get("x-init-data") || "";
  if (!init) return null;
  const params = new URLSearchParams(init);
  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");
  const check = [...params.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`).join("\n");
  const secret = await hmac(new TextEncoder().encode("WebAppData"), env.LS_BOT_TOKEN);
  const sig = await hmac(secret, check);
  const hex = [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (hex !== hash) return null;
  if (Date.now() / 1000 - Number(params.get("auth_date") || 0) > 86400) return null;
  let user = null;
  try {
    user = JSON.parse(params.get("user") || "null");
  } catch {
    return null;
  }
  if (!user || !user.id) return null;
  return (await hasAccess(env, user.id)) ? user : null;
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

async function favorites(env, request, user, url) {
  await env.DB.prepare(
    "CREATE TABLE IF NOT EXISTS favorites (user_id TEXT, item_id INTEGER, card TEXT, ts INTEGER, " +
      "PRIMARY KEY (user_id, item_id))"
  ).run();
  const uid = String(user.id);
  if (request.method === "GET") {
    const { results } = await env.DB.prepare(
      "SELECT item_id, card, ts FROM favorites WHERE user_id = ?1 ORDER BY ts DESC LIMIT 200"
    ).bind(uid).all();
    return json({ items: (results || []).map((r) => ({ ...JSON.parse(r.card || "{}"), saved: r.ts })) });
  }
  if (request.method === "POST") {
    const body = await request.json().catch(() => null);
    if (!body || !body.id) return json({ error: "нет id" }, 400);
    // Копия карточки, а не ссылка на срез: срез хранит неделю, а идея
    // «в работе» должна жить, пока её не уберут руками.
    await env.DB.prepare(
      "INSERT OR REPLACE INTO favorites (user_id, item_id, card, ts) VALUES (?1, ?2, ?3, ?4)"
    ).bind(uid, Number(body.id), JSON.stringify(body).slice(0, 8000), Math.floor(Date.now() / 1000)).run();
    return json({ ok: true });
  }
  if (request.method === "DELETE") {
    await env.DB.prepare("DELETE FROM favorites WHERE user_id = ?1 AND item_id = ?2")
      .bind(uid, Number(url.searchParams.get("id") || 0)).run();
    return json({ ok: true });
  }
  return json({ error: "метод" }, 405);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/app") {
      return new Response(APP_HTML, {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" },
      });
    }

    if (url.pathname.startsWith("/api/")) {
      const user = await appUser(env, request);
      if (!user) return json({ error: "нет доступа" }, 401);
      if (url.pathname === "/api/data" && request.method === "GET") {
        const snap = await loadSnapshot(env);
        return json(snap || { findings: [] });
      }
      if (url.pathname === "/api/fav") return favorites(env, request, user, url);
      if (url.pathname === "/api/idea" && request.method === "GET") {
        await ensureTables(env);
        const f = await findFinding(env, url.searchParams.get("id"));
        if (!f) return json({ error: "Находка уже выпала из свежего среза." }, 404);
        const lang = (await getPrefs(env, user.id)).lang || "ru";
        const res = await ideaCard(env, f, user.id, lang);
        return json(res.error ? { error: res.error } : { card: res.card });
      }
      if (url.pathname === "/api/prefs") {
        await ensureTables(env);
        if (request.method === "POST") {
          const body = (await request.json().catch(() => null)) || {};
          const patch = {};
          if (LANGS.includes(body.lang)) patch.lang = body.lang;
          if (Array.isArray(body.topics)) patch.topics = body.topics.map(String).slice(0, 60);
          if (Array.isArray(body.audience)) patch.audience = body.audience.filter((a) => ["b2b", "b2c", "b2g"].includes(a));
          if (body.notify && typeof body.notify === "object") {
            patch.notify = { hot: body.notify.hot !== false, digest: body.notify.digest !== false, trends: body.notify.trends !== false };
          }
          const next = await setPrefs(env, user.id, patch);
          return json({ ok: true, prefs: next });
        }
        const p = await getPrefs(env, user.id);
        return json({ lang: p.lang, topics: p.topics || [], audience: p.audience || [], notify: p.notify });
      }
      return json({ error: "не найдено" }, 404);
    }

    if (request.method === "POST" && url.pathname === "/notify") {
      // Рассылка от прогона в Actions — тем же секретом, что и срез.
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      await ensureTables(env);
      const body = await request.json().catch(() => null);
      if (!body) return json({ error: "не JSON" }, 400);
      return json(await notifyAll(env, body));
    }

    if (request.method === "GET" && url.pathname === "/") {
      const snap = await loadSnapshot(env);
      const n = snap && snap.findings ? snap.findings.length : 0;
      const age = snap && snap.updated ? Math.round((Date.now() / 1000 - snap.updated) / 60) : "—";
      return new Response(`launch-scout-bot жив. в срезе: ${n}, обновлён ${age} мин назад`, {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    if (request.method === "POST" && url.pathname === "/ingest") {
      // Срез находок из GitHub Actions. Секрет обязателен: иначе кто угодно
      // мог бы подложить владельцу свои «находки».
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      const raw = await request.text();
      let data;
      try {
        data = JSON.parse(raw);
      } catch {
        return new Response("не JSON", { status: 400 });
      }
      if (!data || !Array.isArray(data.findings)) {
        return new Response("нет findings", { status: 400 });
      }
      await env.DB.prepare(
        "INSERT OR REPLACE INTO snapshot (k, data, updated) VALUES ('findings', ?1, ?2)"
      )
        .bind(raw, Number(data.updated) || Math.floor(Date.now() / 1000))
        .run();
      return new Response(`принято: ${data.findings.length}`);
    }

    if (request.method === "POST" && url.pathname === "/tg") {
      // Без этой проверки в Worker может постучаться кто угодно и выдать
      // себя за Telegram.
      if (
        request.headers.get("x-telegram-bot-api-secret-token") !== env.LS_WEBHOOK_SECRET
      ) {
        return new Response("нет", { status: 403 });
      }
      const update = await request.json();
      // Telegram считает доставку успешной по коду ответа и повторяет
      // апдейт, если ждать долго. Отвечаем сразу, работу доделываем следом.
      ctx.waitUntil(handleUpdate(env, update).catch((e) => console.log("ошибка:", e)));
      return new Response("ok");
    }

    return new Response("не найдено", { status: 404 });
  },

  /**
   * Каждые 10 минут: запустить прогон и проверить, что прошлые доходят.
   * Ошибку запуска запоминаем — её увидят «📊 Статус» и сторож.
   */
  async scheduled(event, env, ctx) {
    const now = Math.floor(Date.now() / 1000);
    const err = await dispatchRun(env, { digest: "auto" });
    await setMeta(env, "last_dispatch_error", err || "");
    await setMeta(env, "last_dispatch", now);
    await watchdog(env, now);
    await tokenReminder(env, now);
  },
};
