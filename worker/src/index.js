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

const HELLO =
  "<b>launch-scout</b>\n\n" +
  "Ищет продукты в первые часы после выхода. Считает не лайки, а темп их " +
  "набора, ускорение, отклонение от нормы автора и долю закладок.\n\n" +
  "Находки приходят сами. Кнопками — когда захотите сами.";

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

/**
 * Карточка находки. Порядок строк повторяет порядок решения:
 * что это -> насколько горячо и почему -> куда идти смотреть.
 */
function card(row) {
  const head = row.tier === "hot" ? "🔥" : "•";
  const lines = [
    `${head} <b>${esc(row.title || "без названия")}</b>  <code>${row.score}</code>`,
  ];
  let sub = SRC_RU[row.source] || row.source;
  if (row.author) {
    sub += ` · @${esc(row.author)}`;
    if (row.author_followers) sub += ` (${num(row.author_followers)} подписчиков)`;
  }
  lines.push(`<i>${sub}</i>`);

  // С ИИ-разбором выжимка идёт первой, пост — строкой контекста.
  const ai = row.ai || null;
  if (ai && ai.summary) {
    lines.push("", "🧠 " + esc(ai.summary));
    const eff = { days: "дни", weeks: "недели", months: "месяцы", unclear: "неясно" }[ai.clone_effort];
    if (eff) lines.push("🛠 Повторить: " + eff + (ai.clone_note ? " — " + esc(ai.clone_note) : ""));
    const money = (ai.monetization || "").trim();
    if (money && !/^(not visible|не видно|не указано)$/i.test(money)) lines.push("💰 " + esc(money));
  }
  const body = (row.body || "").trim();
  if (body && body !== (row.title || "").trim()) {
    lines.push("", ai ? esc(body.slice(0, 160)) + (body.length > 160 ? "…" : "") : esc(body.slice(0, 420)));
  }

  const nums = [
    ["♥", row.likes],
    ["💬", row.replies],
    ["🔁", row.reposts],
    ["🔖", row.bookmarks],
    ["👁", row.views],
  ]
    .filter(([, v]) => v)
    .map(([ic, v]) => `${ic} ${num(v)}`);
  if (nums.length) lines.push("", nums.join(" · "));

  if (row.breakdown) {
    // Разбор балла приходит готовой строкой из scout.py: порог должен быть
    // объясним, иначе непонятно, что крутить, когда выдача поедет.
    lines.push("", `<i>${esc(row.breakdown).slice(0, 600)}</i>`);
  }

  let tail = `<a href="${esc(row.url || "")}">пост</a>`;
  if (row.product_url && row.product_url !== row.url) {
    tail += ` · <a href="${esc(row.product_url)}">${esc(row.domain || "продукт")}</a>`;
  }
  if (row.domain_age_days !== null && row.domain_age_days !== undefined) {
    tail += ` · домену ${row.domain_age_days} дн.`;
  }
  lines.push("", tail);
  return lines.join("\n");
}

async function listTop(env, chatId, offset, windowHours, title, topic = null) {
  const cutoff = Math.floor(Date.now() / 1000) - windowHours * 3600;
  // Свежесть считается по first_seen: у части источников времени публикации
  // нет вовсе, а момент, когда запись увидели, есть всегда.
  const snap = await loadSnapshot(env);
  const all = (snap && snap.findings) || [];
  // Один продукт — одна карточка: иначе десятка уходит на то, что всплыло
  // сразу в трёх источниках. Срез уже отсортирован по баллу.
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
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        offset === 0
          ? "Пока пусто: за это окно источники ничего не принесли."
          : "Больше нет.",
      reply_markup: KEYBOARD,
    });
    return;
  }

  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `<b>${title}</b> — ${results.length} шт.`,
    parse_mode: "HTML",
  });
  for (const row of results) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: card(row),
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
  }
  const more = offset + PAGE;
  // Внутри категории листаем ту же категорию, иначе «Ещё 10» выбрасывало
  // бы из неё в общий список.
  const key = topic ? `cat:${topic}` : windowHours === 24 ? "fresh" : "top";
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: "Дальше?",
    reply_markup: {
      inline_keyboard: [
        [
          { text: "➡️ Ещё 10", callback_data: `${key}:${more}` },
          { text: "🔄 В начало", callback_data: `${key}:0` },
        ],
        [{ text: "📱 Открыть приложение", web_app: { url: APP_URL } }],
      ],
    },
  });
}

/** Темы за трое суток с числом находок — кнопками. */
async function categories(env, chatId) {
  const snap = await loadSnapshot(env);
  const all = (snap && snap.findings) || [];
  const ru = (snap && snap.trends && snap.trends.topic_ru) || {};
  const cutoff = Math.floor(Date.now() / 1000) - 72 * 3600;
  const count = {};
  for (const f of all) {
    if (f.first_seen < cutoff) continue;
    for (const t of f.topics || []) count[t] = (count[t] || 0) + 1;
  }
  const top = Object.entries(count).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (!top.length) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "Категорий пока нет: темы проставляются новым находкам с каждым прогоном.",
      reply_markup: KEYBOARD,
    });
    return;
  }
  const rows = [];
  for (let i = 0; i < top.length; i += 2) {
    rows.push(top.slice(i, i + 2).map(([t, n]) => ({
      text: `${ru[t] || t} · ${n}`,
      callback_data: `cat:${t}:0`.slice(0, 64),
    })));
  }
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: "<b>Категории за трое суток</b>",
    parse_mode: "HTML",
    reply_markup: { inline_keyboard: rows },
  });
}

async function trendsMsg(env, chatId) {
  const snap = await loadSnapshot(env);
  const text = snap && snap.trends && snap.trends.text;
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: text || "Трендов пока нет: нужно, чтобы темы проставились хотя бы нескольким десяткам находок.",
    parse_mode: "HTML",
    disable_web_page_preview: true,
    reply_markup: KEYBOARD,
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
    return `Слишком много попыток. Попробуйте через ${mins} мин.`;
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
  if (locked) return "Слишком много попыток. Доступ к вводу закрыт на час.";
  return `Код не подошёл. Осталось попыток: ${MAX_TRIES - tries}`;
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

  // /whoami отвечает всем: человеку, которому доступ ещё не выдан, нужно
  // чем-то представиться владельцу.
  if (text.startsWith("/whoami")) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `Ваш Telegram id: <code>${chatId}</code>`,
      parse_mode: "HTML",
    });
    return;
  }

  if (!(await hasAccess(env, chatId))) {
    const from = msg && msg.from ? msg.from : cb && cb.from ? cb.from : {};
    const who = from.username ? "@" + from.username : from.first_name || "";
    // Любое сообщение от незнакомца считается попыткой ввести код: просить
    // писать «/code XXXX» — лишний шаг там, где человеку и так прислали код.
    if (raw && !raw.startsWith("/start") && !raw.startsWith("/help")) {
      const err = await tryCode(env, chatId, raw, who);
      if (err) {
        await tg(env, "sendMessage", { chat_id: chatId, text: err });
        return;
      }
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: "Доступ открыт.\n\n" + HELLO,
        parse_mode: "HTML",
        reply_markup: KEYBOARD,
      });
      return;
    }
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "Бот закрытый. Пришлите код доступа одним сообщением.\n\n" +
        "Кода нет — попросите у владельца.",
    });
    return;
  }

  if (data === "cats") {
    await categories(env, chatId);
  } else if (data.startsWith("cat:")) {
    const parts = data.split(":");
    const off = Number(parts.pop()) || 0;
    const topic = parts.slice(1).join(":");
    const snap = await loadSnapshot(env);
    const ru = (snap && snap.trends && snap.trends.topic_ru) || {};
    await listTop(env, chatId, off, 72, ru[topic] || topic, topic);
  } else if (data === "trends" || text.startsWith("/trends")) {
    await trendsMsg(env, chatId);
  } else if (data.startsWith("top:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 72, "Топ находок");
  } else if (data.startsWith("fresh:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 24, "За сутки");
  } else if (data === "status") {
    await status(env, chatId);
  } else if (data === "refresh" || text.startsWith("/refresh")) {
    // Не чаще раза в две минуты на всех: прогон длится около минуты, а
    // серия нажатий иначе выстроила бы очередь одинаковых прогонов.
    const now = Math.floor(Date.now() / 1000);
    const last = Number((await meta(env, "last_manual_refresh")) || 0);
    if (now - last < 120) {
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: "Сбор уже запущен только что. Через пару минут нажмите «🆕 За сутки».",
        reply_markup: KEYBOARD,
      });
      return;
    }
    const err = await dispatchRun(env, { digest: "no" });
    if (err) {
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: `Запустить сбор не удалось: ${err}`,
        reply_markup: KEYBOARD,
      });
      return;
    }
    await setMeta(env, "last_manual_refresh", now);
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "Пошёл в источники. Горячее придёт само, остальное — через пару минут " +
        "по кнопке «🆕 За сутки».",
      reply_markup: KEYBOARD,
    });
  } else if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: HELLO,
      parse_mode: "HTML",
      reply_markup: KEYBOARD,
    });
  } else if (text.startsWith("/top")) {
    await listTop(env, chatId, 0, 72, "Топ находок");
  } else if (text.startsWith("/new")) {
    await listTop(env, chatId, 0, 24, "За сутки");
  } else if (text.startsWith("/status")) {
    await status(env, chatId);
  } else if (text) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "Команды: /top, /new, /status",
      reply_markup: KEYBOARD,
    });
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
      return json({ error: "не найдено" }, 404);
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
