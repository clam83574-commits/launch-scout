/**
 * launch-scout bot на Cloudflare Workers.
 *
 * Зачем он отдельно от bot.py: кнопки должны работать, когда ноутбук
 * закрыт. Long polling для этого не годится — ему нужен живой процесс,
 * а Actions отрабатывают и умирают. Здесь вебхук: Telegram сам стучится
 * в Worker, тот читает D1 и отвечает. Ноутбук не участвует.
 *
 * Данные сюда заливает GitHub Actions после каждого прогона (шаг
 * «Залить находки в D1»): scout.py генерирует SQL, wrangler его исполняет.
 * Worker НИЧЕГО не собирает сам — он только показывает уже собранное.
 *
 * Маршруты:
 *   POST /tg  — вебхук Telegram, подписан заголовком secret_token
 *   GET  /    — проверка живости
 */

const PAGE = 10;

const SRC_RU = {
  x: "X",
  hn: "Hacker News",
  yc: "Y Combinator",
  gh: "GitHub",
  ph: "Product Hunt",
};

const KEYBOARD = {
  inline_keyboard: [
    [
      { text: "🔥 Топ-10", callback_data: "top:0" },
      { text: "🆕 За сутки", callback_data: "fresh:0" },
    ],
    [{ text: "📊 Статус", callback_data: "status" }],
  ],
};

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

  const body = (row.body || "").trim();
  if (body && body !== (row.title || "").trim()) {
    lines.push("", esc(body.slice(0, 420)));
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

async function listTop(env, chatId, offset, windowHours, title) {
  const cutoff = Math.floor(Date.now() / 1000) - windowHours * 3600;
  // Свежесть считается по first_seen: у части источников времени публикации
  // нет вовсе, а момент, когда запись увидели, есть всегда.
  const { results } = await env.DB.prepare(
    `SELECT * FROM findings
      WHERE first_seen >= ?1 AND score > 0
      ORDER BY score DESC
      LIMIT ${PAGE} OFFSET ?2`
  )
    .bind(cutoff, offset)
    .all();

  if (!results || !results.length) {
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
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: "Дальше?",
    reply_markup: {
      inline_keyboard: [
        [
          { text: "➡️ Ещё 10", callback_data: `${windowHours === 24 ? "fresh" : "top"}:${more}` },
          { text: "🔄 В начало", callback_data: `${windowHours === 24 ? "fresh" : "top"}:0` },
        ],
        [{ text: "📊 Статус", callback_data: "status" }],
      ],
    },
  });
}

async function status(env, chatId) {
  const now = Math.floor(Date.now() / 1000);
  const total = await env.DB.prepare("SELECT COUNT(*) n FROM findings").first("n");
  const dayN = await env.DB.prepare(
    "SELECT COUNT(*) n FROM findings WHERE first_seen >= ?1"
  )
    .bind(now - 86400)
    .first("n");
  const { results } = await env.DB.prepare(
    "SELECT source, COUNT(*) n, MAX(first_seen) last FROM findings GROUP BY source ORDER BY n DESC"
  ).all();
  const upd = await env.DB.prepare("SELECT MAX(updated) u FROM meta").first("u").catch(() => null);

  const lines = ["<b>Состояние</b>", "", `находок: ${total}, за сутки: ${dayN}`, "", "<b>Источники</b>"];
  for (const r of results || []) {
    const ago = Math.round((now - (r.last || now)) / 60);
    lines.push(`${SRC_RU[r.source] || r.source}: ${r.n}, свежайшая ${ago} мин назад`);
  }
  if (upd) {
    lines.push("", `последняя заливка из Actions: ${Math.round((now - upd) / 60)} мин назад`);
  }
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: lines.join("\n"),
    parse_mode: "HTML",
    reply_markup: KEYBOARD,
  });
}

function allowed(env, chatId) {
  const list = (env.LS_BOT_ALLOW || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  // Пустой список НЕ означает «пускать всех»: бота может найти в поиске
  // Telegram кто угодно, и открытый по умолчанию доступ показывал бы чужим
  // людям находки, ради которых всё считается.
  if (!list.length) return false;
  return list.includes(String(chatId));
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
  if (!allowed(env, chatId)) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Это личный бот, доступа нет." });
    return;
  }

  const data = cb ? cb.data || "" : "";
  const text = msg ? (msg.text || "").trim().toLowerCase() : "";

  if (data.startsWith("top:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 72, "Топ находок");
  } else if (data.startsWith("fresh:")) {
    await listTop(env, chatId, Number(data.split(":")[1]) || 0, 24, "За сутки");
  } else if (data === "status") {
    await status(env, chatId);
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      const n = await env.DB.prepare("SELECT COUNT(*) n FROM findings")
        .first("n")
        .catch(() => "нет таблицы");
      return new Response(`launch-scout-bot жив. находок: ${n}`, {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
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
      const work = handleUpdate(env, update).catch((e) => console.log("ошибка:", e));
      if (typeof globalThis.ctx?.waitUntil === "function") globalThis.ctx.waitUntil(work);
      else await work;
      return new Response("ok");
    }

    return new Response("не найдено", { status: 404 });
  },
};
