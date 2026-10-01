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

/**
 * Записать мелкое состояние — только если значение изменилось.
 *
 * Раньше каждый тик часов (раз в 10 минут) переписывал две-три строки kv
 * без нужды. Сами по себе это ~1000 записей в сутки, но D1 на бесплатном
 * тарифе делит суточный лимит записи (100 тыс. строк) на ВЕСЬ аккаунт, и
 * 2026-09-28 его выжег соседний tm-scout — после чего у бота падали и
 * приём среза, и сохранение настроек. Лишних записей здесь быть не должно.
 */
async function setMeta(env, k, v) {
  const cur = await meta(env, k);
  if (cur === String(v)) return;
  await env.DB.prepare("INSERT OR REPLACE INTO kv (k, v) VALUES (?1, ?2)")
    .bind(k, String(v))
    .run()
    .catch((e) => console.log("kv не записан:", k, String(e).slice(0, 120)));
}

// Разобранный срез держим в памяти экземпляра минуту: разбор JSON в
// полмегабайта на каждое нажатие кнопки — это заметная доля бесплатных
// 10 мс процессора на вызов.
let snapCache = { at: 0, data: null };

/**
 * Срез находок: {updated, findings[], trends, market} или null.
 *
 * Основное место — Workers KV (привязка SNAP), запасное — D1. KV выбран,
 * потому что его лимиты не общие с D1: 2026-09-28 чужой проект на том же
 * аккаунте выжег суточный лимит записи D1, и срез перестал приниматься
 * (Worker отвечал 1101 на каждый /ingest). Без привязки SNAP всё работает
 * по-старому, через D1.
 */
async function loadSnapshot(env) {
  const now = Date.now();
  if (snapCache.data && now - snapCache.at < 60000) return snapCache.data;
  let raw = null;
  if (env.SNAP) raw = await env.SNAP.get("findings").catch(() => null);
  if (!raw) {
    const row = await env.DB.prepare("SELECT data FROM snapshot WHERE k = 'findings'")
      .first()
      .catch(() => null);
    raw = row ? row.data : null;
  }
  if (!raw) return null;
  try {
    const data = JSON.parse(raw);
    snapCache = { at: now, data };
    return data;
  } catch {
    return null;
  }
}

/** Сохранить срез. Возвращает текст ошибки или null. */
async function saveSnapshot(env, raw, updated) {
  if (env.SNAP) {
    try {
      await env.SNAP.put("findings", raw);
      snapCache = { at: 0, data: null };
      return null;
    } catch (e) {
      console.log("KV не принял срез:", String(e).slice(0, 200));
    }
  }
  try {
    await env.DB.prepare("INSERT OR REPLACE INTO snapshot (k, data, updated) VALUES ('findings', ?1, ?2)")
      .bind(raw, updated)
      .run();
    snapCache = { at: 0, data: null };
    return null;
  } catch (e) {
    return String(e).slice(0, 300);
  }
}

/**
 * Запустить прогон в GitHub Actions. Возвращает текст ошибки или null.
 *
 * User-Agent обязателен: без него API GitHub отвечает 403 «Request
 * forbidden by administrative rules», а fetch в Workers сам его не ставит.
 */
async function dispatchRun(env, inputs = {}, workflow = WORKFLOW) {
  if (!env.LS_GH_TOKEN) return "нет LS_GH_TOKEN";
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
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
  await tg(adminEnv(env), "sendMessage", {
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
    hello: "<b>Launch Scout</b> — радар рынка для фаундеров.\n\n💰 Куда идут деньги: раунды, инвесторы, SEC.\n💡 Какие ниши открываются и где уже перегрев.\n🇰🇿 Что свободно в Казахстане и СНГ.\n🔎 Живой поиск по сети, X и Google Trends.\n\nСпросите что угодно — текстом или голосом.",
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
    hello: "<b>Launch Scout</b> — фаундерлерге арналған нарық радары.\n\n💰 Ақша қайда барады: раундтар, инвесторлар, SEC.\n💡 Қай тауашалар ашылып жатыр, қайда қызып кетті.\n🇰🇿 Қазақстан мен ТМД-да не бос.\n🔎 Желіден, X пен Google Trends-тен тікелей іздеу.\n\nКез келген сұрақ қойыңыз — мәтінмен немесе дауыспен.",
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
    hello: "<b>Launch Scout</b> — a market radar for founders.\n\n💰 Where the money goes: rounds, investors, SEC.\n💡 Which niches are opening and which are overheated.\n🇰🇿 What is still free in Kazakhstan and CIS.\n🔎 Live search across the web, X and Google Trends.\n\nAsk anything — by text or voice.",
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
// Рынок, секторы и настройки (2026-09-28). Отдельным словарём, чтобы не
// перекраивать основной; казахский — машинный перевод, как и остальной.
const EXTRA = {
  ru: {
    hello: "<b>launch-scout</b> — радар рынка для стартаперов.\n\n🧭 <b>Рынок</b> — куда идут деньги инвесторов, по секторам и в цифрах: раунды и их стадии, доля в батчах YC, запросы людей.\n💡 <b>Ниши</b> — где за месяц прошло несколько ранних раундов: туда инвесторы только начали ставить.\n🔥 <b>Находки</b> — продукты с реальным откликом или свежим раундом, а не три лайка.\n💬 <b>Спросите</b> — напишите, чем занимаетесь, и ИИ-аналитик ответит по раундам за полгода и свежим запускам, с датами и ссылками.\n🧪 <b>/check идея</b> — бот сначала попробует её опровергнуть фактами и скажет, стоит ли делать.\n⚙️ <b>Настройки</b> — какие секторы, источники и какие уведомления присылать.",
    kb_market: "🧭 Рынок", kb_sectors: "🗂 Секторы", kb_settings: "⚙️ Настройки", kb_niches: "💡 Ниши",
    commands: "Команды: /market, /niches, /top, /new, /sectors, /settings, /lang, /reset\n\nИли просто напишите вопрос — например: «я делаю CRM для клиник, что рядом со мной сейчас получает деньги?»",
    niches_title: "💡 <b>Ниши, куда пошли деньги</b> — %d дн.\nНиша — где несколько компаний подняли раунды. Ранние раунды (pre-seed, seed, A) значат, что ниша только открывается.",
    niches_empty: "Ниш пока нет: раунды разбираются ИИ по мере сбора, первые появятся в течение суток.",
    niche_line: "раундов: %d, ранних %d, %s", sec_niches: "💡 <b>Ниши сектора</b>",
    mega: "+ мегараунд: %s", investors: "💼 Инвесторы:", pain: "🙋 Просят:", gap_kz: "Казахстан", gap_cis: "СНГ",
    gap_free: "свободно", gap_partly: "частично", gap_crowded: "занято",
    follow_on: "🔔 Слежу: новые раунды в нише придут сообщением", follow_off: "Больше не слежу за нишей",
    e_busy: "ИИ сейчас не ответил — попробуйте через минуту.",
    digging: "🔍 Копаю: раунды за полгода, конкуренты, жалобы, аналоги в СНГ — до минуты.",
    dig_btn: "🔍 Глубже",
    radar_title: "🎯 <b>Возможности под вас</b>", radar_fit: "совпадение", radar_why: "почему:", radar_show: "🎯 Показать радар",
    founder_title: "🎯 <b>Ваш профиль</b>\nОтметьте ответы — по ним бот подбирает возможности и отвечает в чате.",
    founder_edit: "✏️ Профиль",
    fq_budget: "💰 Бюджет", fa_budget_5k: "до $5k", fa_budget_20k: "до $20k", fa_budget_100k: "$100k+",
    fq_skills: "🧠 Навыки", fa_skills_dev: "разработка", fa_skills_sales: "продажи", fa_skills_marketing: "маркетинг", fa_skills_industry: "опыт в отрасли",
    fq_markets: "🌍 Рынки", fa_markets_kz: "Казахстан", fa_markets_cis: "СНГ", fa_markets_mena: "MENA", fa_markets_us: "США", fa_markets_global: "весь мир",
    fq_models: "🧩 Модель", fa_models_saas: "SaaS", fa_models_marketplace: "маркетплейс", fa_models_agent: "ИИ-агент", fa_models_fintech: "финтех", fa_models_hardware: "железо",
    fq_horizon: "⏱ Срок до запуска", fa_horizon_1m: "1 месяц", fa_horizon_3m: "3 месяца", fa_horizon_1y: "год",
    why_fit_model: "ваша модель", why_heavy: "капиталоёмко для бюджета", why_overheated: "перегрев", why_kz_free: "в КЗ свободно",
    why_kz_crowded: "в КЗ занято", why_kz_tasks: "компании КЗ просят такое", why_fast: "можно быстро", why_hiring: "компании ниши нанимают", search_fast: "🔎 Google «%s»: интерес растёт быстрее, чем у %d%% ниш", search_slow: "🔎 Google «%s»: интерес растёт медленнее, чем у %d%% ниш",
    searching_niche: "🔎 Ищу в интернете конкурентов, цены и жалобы клиентов в нише «%s»…",
    searching_idea: "🔎 Ищу в интернете конкурентов для вашей идеи…",
    ab_live_p: "📡 Искать везде · +%d LS", ab_xtrends: "📡 X и Google Trends · %d LS",
    st_title_sub: "Launch Scout %s", st_title_pack: "%d LS", st_descr_sub: "%d LS в месяц на вопросы, поиск и разборы. Продление каждые 30 дней, отмена в любой момент.", st_descr_pack: "Докупленные LS не сгорают и тратятся после подписочных.", st_pack_btn: "⭐ +1000 LS — %d", st_pay_hint: "Счёт готов — оплата в один клик звёздами Telegram.", st_pay_btn: "Оплатить %d ⭐", st_ok_sub: "✅ Тариф <b>%s</b> подключён: %d LS до %s.", st_ok_pack: "✅ +%d LS. Докупленных теперь %d — они не сгорают.", st_support_hint: "Опишите проблему одним сообщением: /paysupport <текст>. Передам владельцу.", st_support_ok: "Передал — ответим здесь же.", ls_pay_soon: "Оплата — звёздами Telegram, кнопки ниже. Условия — /terms.", st_terms: "<b>Условия Launch Scout</b>\n\n1. Сервис даёт аналитику рынка и ответы ИИ по открытым данным. Это не инвестиционная рекомендация; решения вы принимаете сами.\n2. Действия оплачиваются внутренними единицами LS по фиксированному прайсу (/balance): вопрос — 10 LS; если для ответа нужен живой поиск в интернете (вопрос о конкретной компании, продукте или новостях) — 40 LS, и бот сообщает об этом в начале поиска. Подписочные LS действуют один расчётный месяц и не переносятся; докупленные LS не сгорают.\n3. Подписка оплачивается Telegram Stars и продлевается каждые 30 дней, пока вы её не отмените в настройках Telegram. Сервис не начисляет проценты, штрафы и скрытые платежи.\n4. Оплата за оказанный период не возвращается. Исключение — технический сбой: звёзды списаны, а тариф или LS не начислены, или списание прошло дважды. Напишите /paysupport, исправим или вернём звёзды.\n5. Вопросы об оплате — /paysupport.",
    ls_footer: "−%d LS · осталось %d LS", ls_low: "⚠️ Осталось %d LS — меньше 10% месячного лимита.", ls_short: "💳 На это нужно %d LS, а у вас %d.", ls_balance: "💳 <b>Тариф: %s</b>\nПодписочные LS: <b>%d</b> · докупленные: <b>%d</b>\nПодписочные обновятся %s; докупленные не сгорают.", ls_tariffs_title: "<b>Тарифы</b>", ls_plan_free: "Free — $%s · %s LS в месяц", ls_plan_pro: "Pro — $%s · %s LS в месяц", ls_plan_max: "Max — $%s · %s LS в месяц", ls_plan_promax: "Pro Max — $%s · %s LS в месяц", ls_pack: "Докупить: %s LS за $%s — не сгорают", ls_prices: "Цена действий: вопрос 10 · глубже / карта идеи / конкуренты 20 · проверка идеи 30 · живой поиск 40 · X и Google Trends 15 · карточка идеи 10 LS. Сводка, лента и радар — бесплатно.", ls_name_free: "Free", ls_name_pro: "Pro", ls_name_max: "Max", ls_name_promax: "Pro Max",
    src_db: "База раундов", src_web: "Веб и новости", live_head: "🔎 Живой поиск в интернете (40 LS): %s", research_started: "📡 Запустил глубокий поиск по X и Google Trends — дополнение придёт отдельным сообщением через 1–2 минуты.", research_limit: "📡 Глубокий поиск — до %d раз в сутки, лимит на сегодня исчерпан.", research_err: "📡 Глубокий поиск сейчас не запустился — попробуйте позже.", research_head: "📡 <b>Дополнение: X и Google Trends</b>", research_empty: "📡 В X и Google Trends по этому запросу ничего заметного.", ab_deep: "🔬 Глубже", ab_map: "🗺 Карта идеи", ab_wide: "🧭 Смежные ниши", ab_comp: "⚔️ Конкуренты", ab_follow: "🔔 Следить", ab_unfollow: "✅ Слежу", ab_live: "📡 Искать везде", ctx_gone: "Контекст устарел — задайте вопрос заново", adj_title: "🧭 <b>Рядом с «%s»</b>", adj_line: "компаний за 6 мес: %d, ранних %d, %s", onboard: "👋 Пять быстрых вопросов — и я буду подбирать ниши и инсайты под вас. Отметьте, что подходит, и нажмите «Показать радар».",
    opp_window: "🔥 Окно: спрос есть, игроков мало", opp_forming: "🧭 Формируется: ищите незакрытую вертикаль",
    opp_overheated: "⚠️ Перегрев: вход только с сильным отличием", opp_local_gap: "🕳 Пусто у нас: доказано деньгами, в КЗ свободно",
    opp_watch: "👀 Наблюдать: сигналов пока мало", opp_score: "скор",
    check_help: "Напишите идею после команды, например:\n/check CRM для частных клиник в Казахстане с записью через WhatsApp",
    voice_fail: "Не получилось распознать голосовое (до 3 минут). Попробуйте ещё раз или напишите текстом.",
    chat_limit: "Сегодня уже %d вопросов — это предел, завтра снова можно.", chat_reset: "Разговор и профиль очищены — начнём заново.",
    set_title: "⚙️ <b>Настройки</b>\nНажмите, чтобы включить или выключить.",
    n_brief: "☀️ Сводка дня — одно короткое сообщение", n_hot: "🔥 Горячие находки — сразу", n_digest: "📋 Сводка — 2 раза в день", n_market: "🧭 Рынок недели — по понедельникам",
    n_funding: "💰 Раунды за сутки в моих секторах", n_alerts: "🚀 Сдвиги рынка — сектор пошёл в рост",
    sens: "🎚 Порог находок:", sens_strict: "только сильные", sens_normal: "обычный", sens_wide: "всё заметное",
    my_sectors: "🗂 Секторы: %s", my_sources: "📡 Источники: %s", my_aud: "👥 Кто платит: %s", all: "все",
    lang_btn: "🌐 Язык", done: "✅ Готово", back: "← Назад",
    pick_sectors: "🗂 <b>Секторы</b>\nПо ним фильтруются находки, раунды и сдвиги рынка. Ничего не выбрано — присылаем всё.",
    pick_sources: "📡 <b>Источники находок</b>\nНичего не выбрано — все.",
    pick_aud: "👥 <b>Кто платит</b>\nB2B — бизнес, B2C — люди, B2G — государство. Ничего не выбрано — все.",
    onboard: "Последний шаг: какие секторы вам интересны? Можно пропустить — тогда будет всё.",
    follow: "🔔 Следить", following: "✅ Слежу", sec_findings: "📰 Находки сектора", back_market: "← Рынок",
    trend: { up: "▲ растёт", down: "▼ остывает", flat: "→ ровно" },
    deals: "💰 раундов: %s за %d дн. (ранних %d), %s · было %d, %s", yc: "🎓 доля в YC %s: %s%% (было %s%%)",
    dem: "🙋 запросы людей в X: %d (было %d)",
    big_deals: "💰 <b>Крупные раунды</b>", analysis: "📰 <b>Аналитика</b>", launches: "🛠 <b>Свежие запуски</b>",
    market_empty: "Данных о рынке пока нет: первый сбор раундов и батчей YC идёт в ближайшем прогоне.",
    sec_empty: "По этому сектору пока нет цифр.", no_amount: "сумм нет", bn: "млрд", mn: "млн",
    sectors_title: "🗂 <b>Секторы</b> — находки за трое суток. Нажмите, чтобы открыть карточку сектора.",
    aud_b2b: "B2B", aud_b2c: "B2C", aud_b2g: "B2G",
  },
  kk: {
    hello: "<b>launch-scout</b> — стартаперларға арналған нарық радары.\n\n🧭 <b>Нарық</b> — инвесторлардың ақшасы қайда бара жатыр, салалар бойынша және сандармен: раундтар мен олардың кезеңдері, YC батчтарындағы үлес, адамдардың сұраулары.\n💡 <b>Тауашалар</b> — бір айда бірнеше ерте раунд өткен жерлер.\n🔥 <b>Табылымдар</b> — нақты үн қатуы немесе жаңа раунды бар өнімдер.\n💬 <b>Сұраңыз</b> — немен айналысатыныңызды жазыңыз, ЖИ-талдаушы жарты жылдағы раундтар мен жаңа іске қосулар бойынша күні мен сілтемесімен жауап береді.\n🧪 <b>/check идея</b> — бот алдымен оны фактілермен жоққа шығаруға тырысады және жасау керек пе, айтады.\n⚙️ <b>Баптаулар</b> — қандай салалар, көздер және хабарламалар.",
    kb_market: "🧭 Нарық", kb_sectors: "🗂 Салалар", kb_settings: "⚙️ Баптаулар", kb_niches: "💡 Тауашалар",
    commands: "Командалар: /market, /niches, /top, /new, /sectors, /settings, /lang, /reset\n\nНемесе сұрағыңызды жай жазыңыз — мысалы: «мен клиникаларға CRM жасаймын, қазір маған жақын не ақша алып жатыр?»",
    niches_title: "💡 <b>Ақша келген тауашалар</b> — %d күн\nТауаша — бірнеше компания раунд тартқан жер. Ерте раундтар (pre-seed, seed, A) тауашаның енді ашылып жатқанын білдіреді.",
    niches_empty: "Тауашалар әзірге жоқ: раундтарды ЖИ жинау барысында талдайды, алғашқылары бір тәулік ішінде шығады.",
    niche_line: "раунд: %d, ерте %d, %s", sec_niches: "💡 <b>Сала тауашалары</b>",
    mega: "+ мега-раунд: %s", investors: "💼 Инвесторлар:", pain: "🙋 Сұрайды:", gap_kz: "Қазақстан", gap_cis: "ТМД",
    gap_free: "бос", gap_partly: "ішінара", gap_crowded: "бос емес",
    follow_on: "🔔 Бақылаймын: тауашадағы жаңа раундтар хабарламамен келеді", follow_off: "Тауашаны бақылау тоқтатылды",
    e_busy: "ЖИ қазір жауап бермеді — бір минуттан кейін көріңіз.",
    digging: "🔍 Зерттеп жатырмын: жарты жылдағы раундтар, бәсекелестер, шағымдар, ТМД-дағы аналогтар — бір минутқа дейін.",
    dig_btn: "🔍 Тереңірек",
    radar_title: "🎯 <b>Сізге арналған мүмкіндіктер</b>", radar_fit: "сәйкестік", radar_why: "неге:", radar_show: "🎯 Радарды көрсету",
    founder_title: "🎯 <b>Сіздің профиліңіз</b>\nЖауаптарды белгілеңіз — бот соған қарай мүмкіндіктер іріктейді.",
    founder_edit: "✏️ Профиль",
    fq_budget: "💰 Бюджет", fa_budget_5k: "$5k дейін", fa_budget_20k: "$20k дейін", fa_budget_100k: "$100k+",
    fq_skills: "🧠 Дағдылар", fa_skills_dev: "әзірлеу", fa_skills_sales: "сату", fa_skills_marketing: "маркетинг", fa_skills_industry: "сала тәжірибесі",
    fq_markets: "🌍 Нарықтар", fa_markets_kz: "Қазақстан", fa_markets_cis: "ТМД", fa_markets_mena: "MENA", fa_markets_us: "АҚШ", fa_markets_global: "бүкіл әлем",
    fq_models: "🧩 Модель", fa_models_saas: "SaaS", fa_models_marketplace: "маркетплейс", fa_models_agent: "ЖИ-агент", fa_models_fintech: "финтех", fa_models_hardware: "құрылғы",
    fq_horizon: "⏱ Іске қосу мерзімі", fa_horizon_1m: "1 ай", fa_horizon_3m: "3 ай", fa_horizon_1y: "жыл",
    why_fit_model: "сіздің модель", why_heavy: "бюджетке ауыр", why_overheated: "қызып кеткен", why_kz_free: "ҚЗ-да бос",
    why_kz_crowded: "ҚЗ-да бос емес", why_kz_tasks: "ҚЗ компаниялары сұрайды", why_fast: "тез жасауға болады", why_hiring: "компаниялар жалдап жатыр", search_fast: "🔎 Google «%s»: қызығушылық тауашалардың %d%%-нан жылдам өсуде", search_slow: "🔎 Google «%s»: қызығушылық тауашалардың %d%%-нан баяу өсуде",
    searching_niche: "🔎 «%s» тауашасындағы бәсекелестерді, бағаларды және шағымдарды интернеттен іздеп жатырмын…",
    searching_idea: "🔎 Идеяңыздың бәсекелестерін интернеттен іздеп жатырмын…",
    ab_live_p: "📡 Барлық жерден іздеу · +%d LS", ab_xtrends: "📡 X және Google Trends · %d LS",
    st_title_sub: "Launch Scout %s", st_title_pack: "%d LS", st_descr_sub: "Айына %d LS: сұрақтар, іздеу және талдаулар. 30 күн сайын ұзартылады, кез келген уақытта тоқтатуға болады.", st_descr_pack: "Сатып алынған LS күймейді және жазылым LS-тен кейін жұмсалады.", st_pack_btn: "⭐ +1000 LS — %d", st_pay_hint: "Шот дайын — Telegram жұлдыздарымен бір рет басып төлеңіз.", st_pay_btn: "%d ⭐ төлеу", st_ok_sub: "✅ <b>%s</b> тарифі қосылды: %d LS, %s дейін.", st_ok_pack: "✅ +%d LS. Сатып алынғандары енді %d — күймейді.", st_support_hint: "Мәселені бір хабарламамен жазыңыз: /paysupport <мәтін>.", st_support_ok: "Жіберілді — осында жауап береміз.", ls_pay_soon: "Төлем — Telegram жұлдыздарымен, батырмалар төменде. Шарттар — /terms.", st_terms: "<b>Launch Scout шарттары</b>\n\n1. Сервис ашық деректер бойынша нарық аналитикасын және ЖИ жауаптарын береді. Бұл инвестициялық кеңес емес.\n2. Әрекеттер LS бірліктерімен тұрақты баға бойынша төленеді (/balance): сұрақ — 10 LS; жауапқа интернеттен тікелей іздеу керек болса (нақты компания, өнім не жаңалық туралы сұрақ) — 40 LS, бот бұл туралы іздеу басында хабарлайды. Жазылым LS бір есеп айы жарамды және келесі айға ауыспайды; сатып алынған LS күймейді.\n3. Жазылым Telegram Stars арқылы төленеді және 30 күн сайын ұзартылады. Сервис пайыз, айыппұл және жасырын төлем алмайды.\n4. Көрсетілген кезең үшін төлем қайтарылмайды. Ерекшелік — техникалық ақау: жұлдыздар алынды, бірақ тариф не LS берілмеді, не екі рет алынды. /paysupport жазыңыз.\n5. Төлем сұрақтары — /paysupport.",
    ls_footer: "−%d LS · қалды %d LS", ls_low: "⚠️ %d LS қалды — айлық лимиттің 10%-нан аз.", ls_short: "💳 Бұған %d LS керек, сізде %d.", ls_balance: "💳 <b>Тариф: %s</b>\nЖазылым LS: <b>%d</b> · сатып алынған: <b>%d</b>\nЖазылым LS %s жаңарады; сатып алынғандары күймейді.", ls_tariffs_title: "<b>Тарифтер</b>", ls_plan_free: "Free — $%s · айына %s LS", ls_plan_pro: "Pro — $%s · айына %s LS", ls_plan_max: "Max — $%s · айына %s LS", ls_plan_promax: "Pro Max — $%s · айына %s LS", ls_pack: "Қосымша: %s LS — $%s, күймейді", ls_prices: "Әрекет бағасы: сұрақ 10 · тереңірек / идея картасы / бәсекелестер 20 · идеяны тексеру 30 · тікелей іздеу 40 · X және Google Trends 15 · идея картасы 10 LS. Шолу, лента және радар — тегін.", ls_name_free: "Free", ls_name_pro: "Pro", ls_name_max: "Max", ls_name_promax: "Pro Max",
    src_db: "Раундтар базасы", src_web: "Веб және жаңалықтар", live_head: "🔎 Интернеттен тікелей іздеу (40 LS): %s", research_started: "📡 X және Google Trends бойынша терең іздеу басталды — толықтыру 1–2 минуттан кейін бөлек хабарламамен келеді.", research_limit: "📡 Терең іздеу — тәулігіне %d рет, бүгінгі лимит бітті.", research_err: "📡 Терең іздеу қазір басталмады — кейінірек көріңіз.", research_head: "📡 <b>Толықтыру: X және Google Trends</b>", research_empty: "📡 Бұл сұрау бойынша X пен Google Trends-те елеулі ештеңе жоқ.", ab_deep: "🔬 Тереңірек", ab_map: "🗺 Идея картасы", ab_wide: "🧭 Көршілес тауашалар", ab_comp: "⚔️ Бәсекелестер", ab_follow: "🔔 Бақылау", ab_unfollow: "✅ Бақылаудамын", ab_live: "📡 Барлық жерден іздеу", ctx_gone: "Контекст ескірді — сұрақты қайта қойыңыз", adj_title: "🧭 <b>«%s» маңында</b>", adj_line: "6 айда компаниялар: %d, ерте %d, %s", onboard: "👋 Бес жылдам сұрақ — сонда тауашалар мен инсайттарды сізге қарай таңдаймын. Сәйкесін белгілеп, «Радарды көрсету» батырмасын басыңыз.",
    opp_window: "🔥 Терезе: сұраныс бар, ойыншы аз", opp_forming: "🧭 Қалыптасуда: бос вертикаль іздеңіз",
    opp_overheated: "⚠️ Қызып кеткен: кіру тек күшті ерекшелікпен", opp_local_gap: "🕳 Бізде бос: ақшамен дәлелденген, ҚЗ-да бос",
    opp_watch: "👀 Бақылау: сигнал әзірге аз", opp_score: "балл",
    check_help: "Идеяны командадан кейін жазыңыз, мысалы:\n/check Қазақстандағы жеке клиникаларға WhatsApp арқылы жазылатын CRM",
    voice_fail: "Дауыстық хабарламаны тану мүмкін болмады (3 минутқа дейін). Қайталаңыз немесе мәтінмен жазыңыз.",
    chat_limit: "Бүгін %d сұрақ қойылды — бұл шек, ертең қайта болады.", chat_reset: "Әңгіме мен профиль тазартылды — қайта бастаймыз.",
    set_title: "⚙️ <b>Баптаулар</b>\nҚосу немесе өшіру үшін басыңыз.",
    n_brief: "☀️ Күн шолуы — бір қысқа хабарлама", n_hot: "🔥 Ыстық табылымдар — бірден", n_digest: "📋 Шолу — күніне 2 рет", n_market: "🧭 Апта нарығы — дүйсенбі сайын",
    n_funding: "💰 Менің салаларымдағы тәуліктік раундтар", n_alerts: "🚀 Нарықтағы өзгерістер — сала өсуге көшті",
    sens: "🎚 Табылымдар шегі:", sens_strict: "тек күштілер", sens_normal: "қалыпты", sens_wide: "бәрі елеулі",
    my_sectors: "🗂 Салалар: %s", my_sources: "📡 Көздер: %s", my_aud: "👥 Кім төлейді: %s", all: "бәрі",
    lang_btn: "🌐 Тіл", done: "✅ Дайын", back: "← Артқа",
    pick_sectors: "🗂 <b>Салалар</b>\nТабылымдар, раундтар және нарық өзгерістері осы бойынша сүзіледі. Ештеңе таңдалмаса — бәрі.",
    pick_sources: "📡 <b>Табылым көздері</b>\nЕштеңе таңдалмаса — бәрі.",
    pick_aud: "👥 <b>Кім төлейді</b>\nB2B — бизнес, B2C — адамдар, B2G — мемлекет. Ештеңе таңдалмаса — бәрі.",
    onboard: "Соңғы қадам: қандай салалар қызық? Өткізіп жіберуге болады — онда бәрі келеді.",
    follow: "🔔 Бақылау", following: "✅ Бақылап отырмын", sec_findings: "📰 Сала табылымдары", back_market: "← Нарық",
    trend: { up: "▲ өсіп келеді", down: "▼ суып барады", flat: "→ бірқалыпты" },
    deals: "💰 раунд: %s (%d күн, ерте %d), %s · бұрын %d, %s", yc: "🎓 YC %s үлесі: %s%% (бұрын %s%%)",
    dem: "🙋 X-тегі адамдардың сұраулары: %d (бұрын %d)",
    big_deals: "💰 <b>Ірі раундтар</b>", analysis: "📰 <b>Аналитика</b>", launches: "🛠 <b>Жаңа іске қосулар</b>",
    market_empty: "Нарық туралы деректер әзірге жоқ: алғашқы жинау келесі іске қосуда.",
    sec_empty: "Бұл сала бойынша әзірге сандар жоқ.", no_amount: "сомасы жоқ", bn: "млрд", mn: "млн",
    sectors_title: "🗂 <b>Салалар</b> — үш тәуліктегі табылымдар. Сала картасын ашу үшін басыңыз.",
    aud_b2b: "B2B", aud_b2c: "B2C", aud_b2g: "B2G",
  },
  en: {
    hello: "<b>launch-scout</b> — a market radar for founders.\n\n🧭 <b>Market</b> — where investor money is moving, by sector and in numbers: rounds and their stages, share of YC batches, what people ask for.\n💡 <b>Niches</b> — where several early rounds closed within a month: investors have just started betting there.\n🔥 <b>Findings</b> — products with real traction or a fresh round, not three likes.\n💬 <b>Ask</b> — tell it what you build, and the AI analyst answers from six months of rounds and fresh launches, with dates and links.\n🧪 <b>/check idea</b> — the bot first tries to refute it with facts and tells you whether it is worth doing.\n⚙️ <b>Settings</b> — which sectors, sources and notifications you get.",
    kb_market: "🧭 Market", kb_sectors: "🗂 Sectors", kb_settings: "⚙️ Settings", kb_niches: "💡 Niches",
    commands: "Commands: /market, /niches, /top, /new, /sectors, /settings, /lang, /reset\n\nOr just type a question — e.g. “I build a CRM for clinics, what near me is getting funded right now?”",
    niches_title: "💡 <b>Niches the money went into</b> — %d days\nA niche is where several companies raised rounds. Early rounds (pre-seed, seed, A) mean the niche is only opening up.",
    niches_empty: "No niches yet: rounds are parsed by AI as they come in, the first ones appear within a day.",
    niche_line: "%d rounds, %d early, %s", sec_niches: "💡 <b>Sector niches</b>",
    mega: "+ mega-round: %s", investors: "💼 Investors:", pain: "🙋 People ask:", gap_kz: "Kazakhstan", gap_cis: "CIS",
    gap_free: "free", gap_partly: "partly taken", gap_crowded: "crowded",
    follow_on: "🔔 Following: new rounds in this niche will arrive as a message", follow_off: "No longer following this niche",
    e_busy: "The AI did not answer just now — try again in a minute.",
    digging: "🔍 Digging: six months of rounds, competitors, complaints, CIS analogs — up to a minute.",
    dig_btn: "🔍 Deeper",
    radar_title: "🎯 <b>Opportunities for you</b>", radar_fit: "fit", radar_why: "why:", radar_show: "🎯 Show radar",
    founder_title: "🎯 <b>Your profile</b>\nTick your answers — the bot picks opportunities and answers in chat accordingly.",
    founder_edit: "✏️ Profile",
    fq_budget: "💰 Budget", fa_budget_5k: "up to $5k", fa_budget_20k: "up to $20k", fa_budget_100k: "$100k+",
    fq_skills: "🧠 Skills", fa_skills_dev: "engineering", fa_skills_sales: "sales", fa_skills_marketing: "marketing", fa_skills_industry: "industry know-how",
    fq_markets: "🌍 Markets", fa_markets_kz: "Kazakhstan", fa_markets_cis: "CIS", fa_markets_mena: "MENA", fa_markets_us: "US", fa_markets_global: "global",
    fq_models: "🧩 Model", fa_models_saas: "SaaS", fa_models_marketplace: "marketplace", fa_models_agent: "AI agent", fa_models_fintech: "fintech", fa_models_hardware: "hardware",
    fq_horizon: "⏱ Time to launch", fa_horizon_1m: "1 month", fa_horizon_3m: "3 months", fa_horizon_1y: "a year",
    why_fit_model: "your model", why_heavy: "capital-heavy for the budget", why_overheated: "overheated", why_kz_free: "free in KZ",
    why_kz_crowded: "crowded in KZ", why_kz_tasks: "KZ companies ask for it", why_fast: "quick to launch", why_hiring: "niche companies are hiring", search_fast: "🔎 Google “%s”: interest growing faster than %d%% of niches", search_slow: "🔎 Google “%s”: interest growing slower than %d%% of niches",
    searching_niche: "🔎 Searching the web for competitors, prices and complaints in “%s”…",
    searching_idea: "🔎 Searching the web for competitors of your idea…",
    ab_live_p: "📡 Search everywhere · +%d LS", ab_xtrends: "📡 X and Google Trends · %d LS",
    st_title_sub: "Launch Scout %s", st_title_pack: "%d LS", st_descr_sub: "%d LS a month for questions, search and deep dives. Renews every 30 days, cancel anytime.", st_descr_pack: "Purchased LS never expire and are spent after plan LS.", st_pack_btn: "⭐ +1000 LS — %d", st_pay_hint: "Invoice ready — one-tap payment with Telegram Stars.", st_pay_btn: "Pay %d ⭐", st_ok_sub: "✅ Plan <b>%s</b> is on: %d LS until %s.", st_ok_pack: "✅ +%d LS. Purchased LS now %d — they never expire.", st_support_hint: "Describe the problem in one message: /paysupport <text>.", st_support_ok: "Sent — we will reply here.", ls_pay_soon: "Pay with Telegram Stars — buttons below. Terms — /terms.", st_terms: "<b>Launch Scout terms</b>\n\n1. The service provides market analytics and AI answers based on public data. It is not investment advice.\n2. Actions are paid in internal LS units at a fixed price list (/balance): a question is 10 LS; if the answer needs a live web search (a question about a specific company, product or news) it is 40 LS, and the bot says so when the search starts. Plan LS are valid for one billing month and do not roll over; purchased LS never expire.\n3. Plans are paid in Telegram Stars and renew every 30 days until you cancel in Telegram settings. No interest, penalties or hidden fees.\n4. Payments for a period already provided are not refunded. Exception — a technical failure: Stars were charged but the plan or LS were not credited, or you were charged twice. Write /paysupport and we will fix it or return the Stars.\n5. Payment questions — /paysupport.",
    ls_footer: "−%d LS · %d LS left", ls_low: "⚠️ %d LS left — under 10% of the monthly allowance.", ls_short: "💳 This needs %d LS, you have %d.", ls_balance: "💳 <b>Plan: %s</b>\nPlan LS: <b>%d</b> · purchased: <b>%d</b>\nPlan LS renew on %s; purchased LS never expire.", ls_tariffs_title: "<b>Plans</b>", ls_plan_free: "Free — $%s · %s LS a month", ls_plan_pro: "Pro — $%s · %s LS a month", ls_plan_max: "Max — $%s · %s LS a month", ls_plan_promax: "Pro Max — $%s · %s LS a month", ls_pack: "Top up: %s LS for $%s — never expire", ls_prices: "Prices: question 10 · deeper / idea map / competitors 20 · idea check 30 · live search 40 · X and Google Trends 15 · idea card 10 LS. Brief, feed and radar are free.", ls_name_free: "Free", ls_name_pro: "Pro", ls_name_max: "Max", ls_name_promax: "Pro Max",
    src_db: "Rounds database", src_web: "Web and news", live_head: "🔎 Live web search (40 LS): %s", research_started: "📡 Started a deep search on X and Google Trends — the follow-up arrives as a separate message in 1–2 minutes.", research_limit: "📡 Deep search is limited to %d a day — today's limit is used up.", research_err: "📡 Deep search did not start — try again later.", research_head: "📡 <b>Follow-up: X and Google Trends</b>", research_empty: "📡 Nothing notable on X or Google Trends for this query.", ab_deep: "🔬 Deeper", ab_map: "🗺 Idea map", ab_wide: "🧭 Adjacent niches", ab_comp: "⚔️ Competitors", ab_follow: "🔔 Follow", ab_unfollow: "✅ Following", ab_live: "📡 Search everywhere", ctx_gone: "Context expired — ask again", adj_title: "🧭 <b>Next to “%s”</b>", adj_line: "companies in 6 months: %d, early %d, %s", onboard: "👋 Five quick questions — and I'll pick niches and insights for you. Tick what fits and tap “Show radar”.",
    opp_window: "🔥 Window: demand exists, few players", opp_forming: "🧭 Forming: look for an unserved vertical",
    opp_overheated: "⚠️ Overheated: enter only with strong differentiation", opp_local_gap: "🕳 Empty here: proven by money, free in KZ",
    opp_watch: "👀 Watch: few signals yet", opp_score: "score",
    check_help: "Type your idea after the command, e.g.:\n/check CRM for private clinics in Kazakhstan with booking via WhatsApp",
    voice_fail: "Could not transcribe the voice message (up to 3 minutes). Try again or type it.",
    chat_limit: "You have asked %d questions today — that is the limit, try again tomorrow.", chat_reset: "Conversation and profile cleared — let's start over.",
    set_title: "⚙️ <b>Settings</b>\nTap to switch on or off.",
    n_brief: "☀️ Daily brief — one short message", n_hot: "🔥 Hot findings — right away", n_digest: "📋 Digest — twice a day", n_market: "🧭 Weekly market — Mondays",
    n_funding: "💰 Daily funding rounds in my sectors", n_alerts: "🚀 Market shifts — a sector turns upward",
    sens: "🎚 Findings threshold:", sens_strict: "strong only", sens_normal: "normal", sens_wide: "everything notable",
    my_sectors: "🗂 Sectors: %s", my_sources: "📡 Sources: %s", my_aud: "👥 Who pays: %s", all: "all",
    lang_btn: "🌐 Language", done: "✅ Done", back: "← Back",
    pick_sectors: "🗂 <b>Sectors</b>\nFindings, funding rounds and market shifts are filtered by these. None selected — everything.",
    pick_sources: "📡 <b>Finding sources</b>\nNone selected — all.",
    pick_aud: "👥 <b>Who pays</b>\nB2B — businesses, B2C — consumers, B2G — government. None selected — all.",
    onboard: "Last step: which sectors interest you? You can skip it and get everything.",
    follow: "🔔 Follow", following: "✅ Following", sec_findings: "📰 Sector findings", back_market: "← Market",
    trend: { up: "▲ rising", down: "▼ cooling", flat: "→ flat" },
    deals: "💰 %s rounds in %d days (%d early), %s · was %d, %s", yc: "🎓 share of YC %s: %s%% (was %s%%)",
    dem: "🙋 people asking on X: %d (was %d)",
    big_deals: "💰 <b>Largest rounds</b>", analysis: "📰 <b>Analysis</b>", launches: "🛠 <b>Fresh launches</b>",
    market_empty: "No market data yet: the first pass over funding rounds and YC batches runs on the next cycle.",
    sec_empty: "No numbers for this sector yet.", no_amount: "no amounts", bn: "B", mn: "M",
    sectors_title: "🗂 <b>Sectors</b> — findings over the last 3 days. Tap to open a sector card.",
    aud_b2b: "B2B", aud_b2c: "B2C", aud_b2g: "B2G",
  },
};
for (const l of LANGS) Object.assign(I18N[l], EXTRA[l]);

const L = (lang) => I18N[lang] || I18N.ru;
const fmt = (s, ...a) => a.reduce((acc, v) => acc.replace(/%[ds]/, String(v)), s).replace(/%%/g, "%");

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
      [{ text: s.kb_market, callback_data: "market" }, { text: s.kb_niches, callback_data: "niches" }],
      [{ text: s.radar_show, callback_data: "radar" }, { text: s.kb_sectors, callback_data: "sectors" }],
      [{ text: s.kb_top, callback_data: "top:0" }],
      [{ text: s.kb_settings, callback_data: "set" }, { text: s.kb_app, web_app: { url: APP_URL } }],
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
  // Отладка (/debug-chat при LS_DEBUG=1): вызовы Telegram пишутся в список, а не уходят.
  if (globalThis.__tgCap) {
    globalThis.__tgCap.push({ t: Date.now(), method, text: payload.text, kb: payload.reply_markup });
    return { ok: true, result: { message_id: globalThis.__tgCap.length } };
  }
  // LS_TG_API — только для локальной проверки (wrangler dev): заглушка
  // вместо Telegram записывает, что бот отправил бы. В бою не задаётся.
  const base = env.LS_TG_API || "https://api.telegram.org";
  const r = await fetch(`${base}/bot${env.LS_BOT_TOKEN}/${method}`, {
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

async function listTop(env, chatId, offset, windowHours, title, topic = null, lang = "ru", sector = null) {
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
    if (sector && !(f.sectors || []).includes(sector)) continue;
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
  const key = sector ? `sf:${sector}` : topic ? `cat:${topic}` : windowHours === 24 ? "fresh" : "top";
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

// ---------------------------------------------------------------------------
// 🧭 Рынок и секторы
// ---------------------------------------------------------------------------
function marketOf(snap) {
  return (snap && snap.market) || null;
}

function sectorMeta(snap, sid) {
  const m = marketOf(snap);
  return ((m && m.sectors) || []).find((x) => x.id === sid) || null;
}

function sectorLabel(snap, sid, lang) {
  const x = sectorMeta(snap, sid);
  return x ? `${x.emoji} ${x.names[lang] || x.names.ru}` : sid;
}

function usd(v, s) {
  if (!v) return s.no_amount;
  return v >= 1e9 ? `$${(v / 1e9).toFixed(1)} ${s.bn}` : `$${Math.round(v / 1e6)} ${s.mn}`;
}

/** Строки с цифрами по сектору — зеркало market.sector_lines в Python. */
function pillarLines(sec, rep, s) {
  const out = [];
  const m = sec.money || {}, y = sec.yc || {}, d = sec.demand || {};
  if (m.cur_n || m.prev_n) {
    out.push(fmt(s.deals, m.sat ? `${m.cur_n}+` : m.cur_n, rep.window_days || 14, m.cur_early || 0, usd(m.cur_usd, s), m.prev_n, usd(m.prev_usd, s)));
  }
  if (y.share !== null && y.share !== undefined && y.prev !== null && y.prev !== undefined) out.push(fmt(s.yc, y.batch, y.share, y.prev));
  if ((d.cur || 0) + (d.prev || 0) >= 5) out.push(fmt(s.dem, d.cur, d.prev));
  return out;
}

const STAGE = { "pre-seed": "pre-seed", seed: "seed", a: "Series A", b: "Series B", "c+": "Series C+", growth: "growth" };

/** «Baselayer — $35 млн, Series A: что делают» — зеркало market.round_line. */
function roundLine(r, lang, s) {
  const bits = [];
  if (r.usd) bits.push(usd(r.usd, s));
  if (r.stage) bits.push(STAGE[r.stage] || r.stage);
  const what = (r.what || {})[lang === "en" ? "en" : "ru"] || "";
  const name = esc(r.company || "—");
  const head = okUrl(r.url) ? `<a href="${esc(r.url)}">${name}</a>` : name;
  return `${head}${bits.length ? " — " + bits.join(", ") : ""}${what ? ": " + esc(what) : ""}`;
}

function nicheName(rep, n, lang) {
  return lang === "en" ? n.niche : ((rep.niche_names || {})[n.niche] || n.niche);
}

/** Ниши, куда за окно пришло несколько раундов, — главный ответ «что залетает». */
const GAP_ICON = { free: "🟢", partly: "🟡", crowded: "🔴" };

/** Строки ниши: цифры, мегараунд, инвесторы, «боль», свобода в КЗ/СНГ. */
function nicheLines(rep, n, lang, s, snap) {
  const emo = sectorMeta(snap, n.sector);
  const o = n.opp || {};
  const out = [`${emo ? emo.emoji + " " : ""}<b>${esc(nicheName(rep, n, lang))}</b> — ${fmt(s.niche_line, n.n, n.early, usd(n.usd, s))}`];
  if (o.type) out.push(`   ${s["opp_" + o.type] || o.type} · ${s.opp_score} <b>${o.score}</b>/100`);
  for (const r of (n.companies || []).filter((r) => !(r.usd >= 1e9)).slice(0, 3)) out.push("   • " + roundLine(r, lang, s));
  for (const r of (n.mega || []).slice(0, 1)) out.push("   " + fmt(s.mega, roundLine(r, lang, s)));
  if ((n.investors || []).length) out.push("   " + s.investors + " " + esc(n.investors.join(", ")));
  const sr = n.search;
  if (sr && sr.rel) out.push("   " + (sr.pct >= 50 ? fmt(s.search_fast, esc(sr.term), sr.pct) : fmt(s.search_slow, esc(sr.term), 100 - sr.pct)));
  const p = (n.pain || [])[0];
  if (p) out.push(`   ${s.pain} <a href="${esc(p.url || "")}">«${esc(String(p.text || "").slice(0, 120))}»</a>${p.likes ? " · ♥ " + p.likes : ""}`);
  const g = n.gap;
  if (g && (g.kz || g.cis)) {
    const note = (g.note || {})[lang] || (g.note || {}).ru || "";
    out.push(`   ${GAP_ICON[g.kz] || "⚪"} ${s.gap_kz} ${s["gap_" + g.kz] || "?"} · ${GAP_ICON[g.cis] || "⚪"} ${s.gap_cis} ${s["gap_" + g.cis] || "?"}` +
      ((g.analogs || []).length ? " — " + g.analogs.slice(0, 3).map((a) => `<a href="${esc(a.url)}">${esc(a.name)}</a>`).join(", ") : "") +
      (note ? `\n   <i>${esc(note)}</i>` : ""));
  }
  return out;
}

/** Ниши, куда за окно пришло несколько раундов, — главный ответ «что залетает». */
async function nichesMsg(env, chatId, lang, sid = null, prefs = null, editMsg = null) {
  const s = L(lang);
  const snap = await loadSnapshot(env);
  const m = marketOf(snap);
  const rep = m && m.report;
  const list = ((rep && rep.niches) || []).filter((n) => n.n >= 3 && (!sid || n.sector === sid)).slice(0, 6);
  if (!list.length) {
    await tg(env, "sendMessage", { chat_id: chatId, text: s.niches_empty, reply_markup: keyboardFor(lang) });
    return;
  }
  const lines = [fmt(s.niches_title, rep.niche_days || 28)];
  for (const n of list) lines.push("", ...nicheLines(rep, n, lang, s, snap));
  let text = lines.join("\n");
  while (text.length > 3900 && lines.length > 3) { lines.pop(); text = lines.join("\n"); }
  // Кнопка «Следить» на каждую нишу: номер в списке отчёта, а не имя —
  // callback_data у Telegram не длиннее 64 байт.
  const mine = new Set((prefs && prefs.niches) || []);
  const all = (rep.niches || []);
  const kb = list.map((n) => [
    { text: `${mine.has(n.niche) ? "✅" : "🔔"} ${nicheName(rep, n, lang)}`.slice(0, 40), callback_data: `nf:${all.indexOf(n)}` },
    { text: s.dig_btn, callback_data: `dd:${all.indexOf(n)}` },
  ]);
  kb.push([{ text: s.kb_market, callback_data: "market" }, { text: s.kb_sectors, callback_data: "sectors" }]);
  const payload = { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true, reply_markup: { inline_keyboard: kb } };
  if (editMsg) {
    const r = await tg(env, "editMessageReplyMarkup", { chat_id: chatId, message_id: editMsg, reply_markup: payload.reply_markup });
    if (r && r.ok) return;
  }
  await tg(env, "sendMessage", payload);
}

/** Кнопки секторов: по две в ряд, самые растущие первыми. */
function sectorButtons(snap, lang, prefix = "sec:") {
  const m = marketOf(snap);
  const rep = m && m.report;
  const order = rep ? rep.sectors.map((x) => x.id) : ((m && m.sectors) || []).map((x) => x.id);
  const rows = [];
  for (let i = 0; i < order.length; i += 2) {
    rows.push(order.slice(i, i + 2).map((sid) => ({ text: sectorLabel(snap, sid, lang).slice(0, 40), callback_data: prefix + sid })));
  }
  return rows;
}

async function marketMsg(env, chatId, lang = "ru") {
  const s = L(lang);
  const snap = await loadSnapshot(env);
  const m = marketOf(snap);
  const text = m && m.texts ? m.texts[lang] || m.texts.ru : null;
  if (!text) {
    // Рынка в срезе ещё нет — показываем прежние тренды находок, если есть.
    const tt = snap && snap.trends && snap.trends.text;
    const old = typeof tt === "string" ? tt : tt ? tt[lang] || tt.ru : null;
    await tg(env, "sendMessage", { chat_id: chatId, text: old || s.market_empty, parse_mode: "HTML",
      disable_web_page_preview: true, reply_markup: keyboardFor(lang) });
    return;
  }
  const rows = sectorButtons(snap, lang).slice(0, 4);
  rows.push([{ text: s.kb_niches, callback_data: "niches" }, { text: s.kb_sectors, callback_data: "sectors" }]);
  rows.push([{ text: s.kb_settings, callback_data: "set" }]);
  await tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true,
    reply_markup: { inline_keyboard: rows } });
}

/** Карточка сектора: тренд и цифры, крупные раунды, аналитика, свежие запуски. */
async function sectorCard(env, chatId, sid, lang, prefs, editMsg = null) {
  const s = L(lang);
  const snap = await loadSnapshot(env);
  const m = marketOf(snap);
  const rep = m && m.report;
  const sec = rep && rep.sectors.find((x) => x.id === sid);
  const lines = [`<b>${esc(sectorLabel(snap, sid, lang))}</b>`];
  if (sec) {
    lines.push(`<i>${s.trend[sec.trend] || ""}</i>`, "");
    const pl = pillarLines(sec, rep, s);
    lines.push(...(pl.length ? pl : [s.sec_empty]));
    const deals = (sec.money && sec.money.top) || [];
    if (deals.length) {
      lines.push("", s.big_deals);
      for (const dl of deals.slice(0, 3)) lines.push("• " + roundLine(dl, lang, s));
    }
  } else {
    lines.push("", s.sec_empty);
  }
  const secNiches = ((rep && rep.niches) || []).filter((n) => n.sector === sid && n.n >= 2).slice(0, 3);
  if (secNiches.length) {
    lines.push("", s.sec_niches);
    for (const n of secNiches) {
      lines.push(`• <b>${esc(nicheName(rep, n, lang))}</b> — ${fmt(s.niche_line, n.n, n.early, usd(n.usd, s))}: ` +
        (n.companies || []).slice(0, 3).map((r) => esc(r.company)).join(", "));
    }
  }
  const an = ((m && m.analysis) || {})[sid] || [];
  if (an.length) {
    lines.push("", s.analysis);
    for (const a of an.slice(0, 2)) lines.push(`• <a href="${esc(a.url)}">${esc((a.title || "").slice(0, 120))}</a>`);
  }
  const cutoff = Math.floor(Date.now() / 1000) - 72 * 3600;
  const fresh = ((snap && snap.findings) || []).filter((f) => f.first_seen >= cutoff && (f.sectors || []).includes(sid) && f.score > 0)
    .sort((a, b) => b.score - a.score).slice(0, 3);
  if (fresh.length) {
    lines.push("", s.launches);
    for (const f of fresh) lines.push(`• <a href="${esc(f.url || "")}">${esc((f.title || "").slice(0, 80))}</a> <code>${f.score}</code>`);
  }
  const mine = (prefs.sectors || []).includes(sid);
  const kb = { inline_keyboard: [
    [{ text: mine ? s.following : s.follow, callback_data: `follow:${sid}` }, { text: s.sec_findings, callback_data: `sf:${sid}:0` }],
    [{ text: s.back_market, callback_data: "market" }],
  ] };
  const payload = { chat_id: chatId, text: lines.join("\n").slice(0, 4000), parse_mode: "HTML", disable_web_page_preview: true, reply_markup: kb };
  if (editMsg) {
    const r = await tg(env, "editMessageText", { ...payload, message_id: editMsg });
    if (r && r.ok) return;
  }
  await tg(env, "sendMessage", payload);
}

/** Секторы с числом находок за трое суток — кнопками. */
async function sectorsMsg(env, chatId, lang) {
  const s = L(lang);
  const snap = await loadSnapshot(env);
  const cutoff = Math.floor(Date.now() / 1000) - 72 * 3600;
  const count = {};
  for (const f of (snap && snap.findings) || []) {
    if (f.first_seen < cutoff) continue;
    for (const x of f.sectors || []) count[x] = (count[x] || 0) + 1;
  }
  const m = marketOf(snap);
  const ids = ((m && m.sectors) || []).map((x) => x.id);
  if (!ids.length) {
    await categories(env, chatId, lang);   // срез без рынка: прежние категории
    return;
  }
  const rows = [];
  for (let i = 0; i < ids.length; i += 2) {
    rows.push(ids.slice(i, i + 2).map((sid) => ({ text: `${sectorLabel(snap, sid, lang)} · ${count[sid] || 0}`.slice(0, 48), callback_data: `sec:${sid}` })));
  }
  await tg(env, "sendMessage", { chat_id: chatId, text: s.sectors_title, parse_mode: "HTML", reply_markup: { inline_keyboard: rows } });
}

// ---------------------------------------------------------------------------
// ⚙️ Настройки: одно сообщение, которое правится на месте
// ---------------------------------------------------------------------------
function settingsView(p, lang, snap) {
  const s = L(lang);
  const on = (k) => (p.notify[k] !== false ? "✅ " : "▫️ ");
  const secs = p.sectors ? `${p.sectors.length}` : s.all;
  const srcs = p.sources ? p.sources.map((x) => SRC_RU[x] || x).join(", ") : s.all;
  const aud = p.audience ? p.audience.map((a) => a.toUpperCase()).join(", ") : s.all;
  const sensBtn = (k) => ({ text: (p.sens === k ? "🔘 " : "⚪ ") + s["sens_" + k], callback_data: `sens:${k}` });
  return {
    text: s.set_title,
    reply_markup: { inline_keyboard: [
      ...NOTIFY_KEYS.map((k) => [{ text: on(k) + s["n_" + k], callback_data: `nt:${k}` }]),
      [{ text: s.sens, callback_data: "noop" }],
      [sensBtn("strict"), sensBtn("normal"), sensBtn("wide")],
      [{ text: fmt(s.my_sectors, secs), callback_data: "ps" }],
      [{ text: fmt(s.my_sources, srcs), callback_data: "psrc" }, { text: fmt(s.my_aud, aud), callback_data: "paud" }],
      [{ text: s.lang_btn, callback_data: "lang" }, { text: s.done, callback_data: "home" }],
    ] },
  };
}

function sectorsPicker(p, lang, snap, back = "set") {
  const s = L(lang);
  const m = marketOf(snap);
  const ids = ((m && m.sectors) || []).map((x) => x.id);
  const mine = new Set(p.sectors || []);
  const rows = [];
  for (let i = 0; i < ids.length; i += 2) {
    rows.push(ids.slice(i, i + 2).map((sid) => ({ text: (mine.has(sid) ? "✅ " : "") + sectorLabel(snap, sid, lang).slice(0, 36), callback_data: `ts:${sid}:${back}` })));
  }
  rows.push([{ text: s.done, callback_data: back }]);
  return { text: back === "home" ? s.onboard + "\n\n" + s.pick_sectors : s.pick_sectors, reply_markup: { inline_keyboard: rows } };
}

function sourcesPicker(p, lang) {
  const s = L(lang);
  const mine = new Set(p.sources || []);
  return { text: s.pick_sources, reply_markup: { inline_keyboard: [
    SOURCES.map((x) => ({ text: (mine.has(x) ? "✅ " : "") + (SRC_RU[x] || x), callback_data: `tsrc:${x}` })),
    [{ text: s.back, callback_data: "set" }],
  ] } };
}

function audPicker(p, lang) {
  const s = L(lang);
  const mine = new Set(p.audience || []);
  return { text: s.pick_aud, reply_markup: { inline_keyboard: [
    ["b2b", "b2c", "b2g"].map((a) => ({ text: (mine.has(a) ? "✅ " : "") + s["aud_" + a], callback_data: `taud:${a}` })),
    [{ text: s.back, callback_data: "set" }],
  ] } };
}

/** Показать экран: правим то же сообщение, а если нельзя — шлём новое. */
async function show(env, chatId, msgId, view) {
  const payload = { chat_id: chatId, text: view.text, parse_mode: "HTML", reply_markup: view.reply_markup, disable_web_page_preview: true };
  if (msgId) {
    const r = await tg(env, "editMessageText", { ...payload, message_id: msgId });
    if (r && (r.ok || /not modified/.test(r.description || ""))) return;
  }
  await tg(env, "sendMessage", payload);
}

const toggle = (list, x) => {
  const set = new Set(list || []);
  if (set.has(x)) set.delete(x); else set.add(x);
  return set.size ? [...set] : null;
};

/** Кнопки настроек. Возвращает true, если нажатие было про настройки. */
async function settingsAction(env, chatId, msgId, data, prefs, lang) {
  const snap = await loadSnapshot(env);
  let p = prefs;
  if (data === "set") return show(env, chatId, msgId, settingsView(p, lang, snap)), true;
  if (data.startsWith("nt:")) {
    const k = data.slice(3);
    if (!NOTIFY_KEYS.includes(k)) return true;
    p = await setPrefs(env, chatId, { notify: { ...p.notify, [k]: p.notify[k] === false } });
    await show(env, chatId, msgId, settingsView(p, lang, snap));
    return true;
  }
  if (data.startsWith("sens:")) {
    const k = data.slice(5);
    if (SENS[k] && k !== p.sens) p = await setPrefs(env, chatId, { sens: k });
    await show(env, chatId, msgId, settingsView(p, lang, snap));
    return true;
  }
  if (data === "ps") return show(env, chatId, msgId, sectorsPicker(p, lang, snap)), true;
  if (data.startsWith("ts:")) {
    const [, sid, back] = data.split(":");
    p = await setPrefs(env, chatId, { sectors: toggle(p.sectors, sid) });
    await show(env, chatId, msgId, sectorsPicker(p, lang, snap, back || "set"));
    return true;
  }
  if (data === "psrc") return show(env, chatId, msgId, sourcesPicker(p, lang)), true;
  if (data.startsWith("tsrc:")) {
    p = await setPrefs(env, chatId, { sources: toggle(p.sources, data.slice(5)) });
    await show(env, chatId, msgId, sourcesPicker(p, lang));
    return true;
  }
  if (data === "paud") return show(env, chatId, msgId, audPicker(p, lang)), true;
  if (data.startsWith("taud:")) {
    p = await setPrefs(env, chatId, { audience: toggle(p.audience, data.slice(5)) });
    await show(env, chatId, msgId, audPicker(p, lang));
    return true;
  }
  if (data.startsWith("follow:")) {
    const sid = data.slice(7);
    p = await setPrefs(env, chatId, { sectors: toggle(p.sectors, sid) });
    await sectorCard(env, chatId, sid, lang, p, msgId);
    return true;
  }
  return false;
}

/**
 * Меню команд в Telegram — один раз на версию (помечается в kv). Раньше
 * команд в меню не было вовсе: /trends, /lang знали только те, кому сказали.
 */
const COMMANDS_VERSION = "2026-10-02";
async function setupCommands(env) {
  if ((await meta(env, "commands_version")) === COMMANDS_VERSION) return;
  const list = {
    ru: [["market", "🧭 Куда движется рынок"], ["radar", "🎯 Возможности под меня"], ["profile", "✏️ Мой профиль"],  ["niches", "💡 Ниши, куда пошли деньги"], ["check", "🧪 Проверь мою идею"], ["balance", "💳 Баланс LS и тарифы"], ["terms", "📄 Условия"], ["paysupport", "🆘 Помощь с оплатой"], ["top", "🔥 Лучшие находки"], ["new", "🆕 За сутки"],
      ["sectors", "🗂 Секторы"], ["settings", "⚙️ Настройки уведомлений"], ["lang", "🌐 Язык"]],
    en: [["market", "🧭 Where the market is heading"], ["radar", "🎯 Opportunities for me"], ["profile", "✏️ My profile"],  ["niches", "💡 Niches the money went into"], ["check", "🧪 Check my idea"], ["balance", "💳 LS balance and plans"], ["terms", "📄 Terms"], ["paysupport", "🆘 Payment support"], ["top", "🔥 Top findings"], ["new", "🆕 Last 24h"],
      ["sectors", "🗂 Sectors"], ["settings", "⚙️ Notification settings"], ["lang", "🌐 Language"]],
    kk: [["market", "🧭 Нарық қайда бет алды"], ["radar", "🎯 Маған арналған мүмкіндіктер"], ["profile", "✏️ Профилім"],  ["niches", "💡 Ақша келген тауашалар"], ["check", "🧪 Идеямды тексер"], ["balance", "💳 LS балансы мен тарифтер"], ["terms", "📄 Шарттар"], ["paysupport", "🆘 Төлем бойынша көмек"], ["top", "🔥 Үздік табылымдар"], ["new", "🆕 Тәулік ішінде"],
      ["sectors", "🗂 Салалар"], ["settings", "⚙️ Хабарлама баптаулары"], ["lang", "🌐 Тіл"]],
  };
  const cmd = (arr) => arr.map(([command, description]) => ({ command, description }));
  const r = await tg(env, "setMyCommands", { commands: cmd(list.ru) });
  await tg(env, "setMyCommands", { commands: cmd(list.en), language_code: "en" });
  await tg(env, "setMyCommands", { commands: cmd(list.kk), language_code: "kk" });
  if (r && r.ok) await setMeta(env, "commands_version", COMMANDS_VERSION);
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
  if (update.pre_checkout_query) {
    await starsPreCheckout(env, update.pre_checkout_query);
    return;
  }
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

  const seen = await env.DB.prepare("INSERT OR IGNORE INTO users_seen (user_id, ts) VALUES (?1, ?2)").bind(String(chatId), Math.floor(Date.now() / 1000)).run().catch(() => null);
  if (seen && seen.meta && seen.meta.changes && !isOwner(env, chatId)) {
    const from = (msg && msg.from) || (cb && cb.from) || {};
    await ownerNotify(env, { text: `👤 Новый пользователь: ${from.first_name || ""}${from.username ? " @" + from.username : ""} (${chatId})` });
  }
  const prefs = await getPrefs(env, chatId);
  const msgId = cb && cb.message ? cb.message.message_id : null;
  if (data.startsWith("lang:")) {
    const lang = LANGS.includes(data.slice(5)) ? data.slice(5) : "ru";
    const first = !prefs.lang;
    const p = await setPrefs(env, chatId, { lang });
    if (first) {
      // Первый вход: сразу предложить секторы — без этого шага новичок
      // получал бы всё подряд и не знал бы, что можно настроить.
      const snap = await loadSnapshot(env);
      if (marketOf(snap)) {
        await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).lang_set + "\n\n" + L(lang).hello, parse_mode: "HTML", reply_markup: keyboardFor(lang) });
        // Первый вход: анкета основателя вместо выбора секторов — по ней
        // подбираются ниши, радар и ответы чата (просьба владельца 2026-10-01).
        await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).onboard });
        await show(env, chatId, null, founderView(p, lang));
        return;
      }
    }
    await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).lang_set + "\n\n" + L(lang).hello, parse_mode: "HTML", reply_markup: keyboardFor(lang) });
    return;
  }
  // Язык спрашиваем при первом входе и по /lang. Нажатие кнопки под
  // уведомлением не перехватываем: человек ждёт карточку, а не вопрос —
  // до выбора языка такие ответы идут по-русски.
  if (text.startsWith("/lang") || data === "lang" || (!prefs.lang && !data)) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Выберите язык · Тілді таңдаңыз · Choose your language", reply_markup: LANG_PICKER });
    return;
  }
  const lang = prefs.lang || "ru";
  const s = L(lang);

  if (data === "noop") return;
  if (data && (await settingsAction(env, chatId, msgId, data, prefs, lang))) return;
  if (data === "home") {
    await show(env, chatId, msgId, { text: s.hello, reply_markup: keyboardFor(lang) });
    return;
  }
  if (data === "market" || data === "trends" || text.startsWith("/market") || text.startsWith("/trends")) {
    await marketMsg(env, chatId, lang);
    return;
  }
  if (data === "niches" || text.startsWith("/niches")) {
    await nichesMsg(env, chatId, lang, null, prefs);
    return;
  }
  if (msg && msg.successful_payment) {
    await starsPaid(env, chatId, msg, lang);
    return;
  }
  if (data.startsWith("buy:")) {
    const link = await starsLink(env, chatId, data.slice(4), lang);
    const it = STAR_ITEMS[data.slice(4)];
    await tg(env, "sendMessage", link
      ? { chat_id: chatId, text: L(lang).st_pay_hint, reply_markup: { inline_keyboard: [[{ text: fmt(L(lang).st_pay_btn, it.stars), url: link }]] } }
      : { chat_id: chatId, text: L(lang).research_err });
    return;
  }
  if (text.startsWith("/terms")) {
    await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).st_terms, parse_mode: "HTML" });
    return;
  }
  if (text.startsWith("/paysupport")) {
    // Вопрос об оплате — владельцу; без текста — подсказка, как написать.
    const body = raw.replace(/^\/paysupport(@\S+)?\s*/i, "").trim();
    if (!body) {
      await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).st_support_hint });
    } else {
      await ownerNotify(env, { text: `💳 /paysupport от ${chatId}:\n${body.slice(0, 2000)}` });
      await tg(env, "sendMessage", { chat_id: chatId, text: L(lang).st_support_ok });
    }
    return;
  }
  if (isOwner(env, chatId) && /^\/refund\b/.test(text)) {
    const [, uid, charge] = raw.split(/\s+/);
    const r = await tg(env, "refundStarPayment", { user_id: Number(uid), telegram_payment_charge_id: charge });
    await tg(env, "sendMessage", { chat_id: chatId, text: r && r.ok ? `✅ возврат ${uid} ${charge}` : `не вышло: ${JSON.stringify(r).slice(0, 200)}` });
    return;
  }
  if (data === "ls" || text.startsWith("/balance")) {
    await lsBalanceMsg(env, chatId, lang);
    return;
  }
  if (isOwner(env, chatId) && /^\/(grant|credit|costs)\b/.test(text)) {
    await lsAdmin(env, chatId, text);
    return;
  }
  if (data === "radar" || text.startsWith("/radar")) {
    await radarMsg(env, chatId, lang, prefs);
    return;
  }
  if (data === "fprof" || text.startsWith("/profile")) {
    await show(env, chatId, data ? msgId : null, founderView(prefs, lang));
    return;
  }
  if (data.startsWith("fp:")) {
    const [, k, v] = data.split(":");
    if (FOUNDER_Q[k] && FOUNDER_Q[k].includes(v)) {
      const f = { ...(prefs.founder || {}) };
      if (FOUNDER_MULTI.has(k)) {
        const set = new Set(f[k] || []);
        if (set.has(v)) set.delete(v); else set.add(v);
        f[k] = [...set];
      } else f[k] = f[k] === v ? null : v;
      const p = await setPrefs(env, chatId, { founder: f });
      // Профиль видит и ИИ-собеседник: подбирает ответы под человека.
      const sum = founderSummary(f);
      if (sum) await env.DB.prepare("INSERT OR REPLACE INTO chat_profile (user_id, about, ts) VALUES (?1, ?2, ?3)")
        .bind(String(chatId), sum, Math.floor(Date.now() / 1000)).run().catch(() => null);
      await show(env, chatId, msgId, founderView(p, lang));
    }
    return;
  }
  if (data.startsWith("ca:") || data.startsWith("cn:")) {
    await answerAction(env, chatId, lang, data, prefs, cb, msgId);
    return;
  }
  if (data.startsWith("dd:")) {
    // «Копай глубже» по нише: исследование с инструментами и поиском в сети.
    const rep = (marketOf(await loadSnapshot(env)) || {}).report || {};
    const n = (rep.niches || [])[Number(data.slice(3))];
    if (n) await chatReply(env, chatId, `Dig deep into the niche "${n.niche}" (sector ${n.sector}).`, lang, "deep", { niche: n.niche });
    return;
  }
  if (data.startsWith("nf:")) {
    // Следить / не следить за нишей: раунды в ней придут отдельным сообщением.
    const rep = (marketOf(await loadSnapshot(env)) || {}).report || {};
    const n = (rep.niches || [])[Number(data.slice(3))];
    if (n) {
      const p = await setPrefs(env, chatId, { niches: toggle(prefs.niches, n.niche) });
      const on = (p.niches || []).includes(n.niche);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: on ? s.follow_on : s.follow_off });
      await nichesMsg(env, chatId, lang, null, p, msgId);
    }
    return;
  }
  if (data === "sectors" || text.startsWith("/sectors")) {
    await sectorsMsg(env, chatId, lang);
    return;
  }
  if (data.startsWith("sec:")) {
    await sectorCard(env, chatId, data.slice(4), lang, prefs);
    return;
  }
  if (data.startsWith("sf:")) {
    const [, sid, off] = data.split(":");
    const snap = await loadSnapshot(env);
    await listTop(env, chatId, Number(off) || 0, 72, sectorLabel(snap, sid, lang), null, lang, sid);
    return;
  }
  if (data === "set" || text.startsWith("/settings")) {
    await show(env, chatId, msgId, settingsView(prefs, lang, await loadSnapshot(env)));
    return;
  }

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
    // Первое знакомство: пять вопросов о человеке — под них подбираются ниши и ответы.
    if (!prefs.founder || !Object.keys(prefs.founder).length) {
      await tg(env, "sendMessage", { chat_id: chatId, text: s.onboard });
      await show(env, chatId, null, founderView(prefs, lang));
    }
  } else if (text.startsWith("/top")) {
    await listTop(env, chatId, 0, 72, s.top_title, null, lang);
  } else if (text.startsWith("/new")) {
    await listTop(env, chatId, 0, 24, s.fresh_title, null, lang);
  } else if (text.startsWith("/status")) {
    await status(env, chatId);
  } else if (text.startsWith("/reset")) {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM chat_log WHERE user_id = ?1").bind(String(chatId)),
      env.DB.prepare("DELETE FROM chat_profile WHERE user_id = ?1").bind(String(chatId)),
    ]);
    await tg(env, "sendMessage", { chat_id: chatId, text: s.chat_reset });
  } else if (msg && msg.voice) {
    // Голос: распознать и ответить как на текст, показав, что услышали.
    await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });
    const heard = await transcribe(env, msg.voice, lang);
    if (!heard) {
      await tg(env, "sendMessage", { chat_id: chatId, text: s.voice_fail });
    } else {
      // Расшифровку не показываем — сразу ответ (просьба владельца 2026-09-29).
      await chatReply(env, chatId, heard, lang);
    }
  } else if (text.startsWith("/check")) {
    // «Проверь мою идею»: сначала пытаемся её опровергнуть фактами.
    const idea = raw.replace(/^\/check(@\w+)?/i, "").trim();
    if (idea.length < 10) await tg(env, "sendMessage", { chat_id: chatId, text: s.check_help });
    else await chatReply(env, chatId, idea, lang, "check");
  } else if (raw && !raw.startsWith("/")) {
    await chatReply(env, chatId, raw, lang);
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
  await tg(adminEnv(env), "sendMessage", {
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
Rules: the ru, kk and en blocks say the same thing in Russian, Kazakh (Cyrillic script) and English — plain and short. why_now: 1-2 sentences grounded in the INVESTOR DATA given (rounds in this sector and niche, their stage and sums, YC share) and only then in the product's own traction — cite the numbers, never invent any; if the investor data shows no money in this direction, say so plainly. who_pays: who exactly pays and roughly how. analogs: only services you actually found, each with a real URL; at most 4; an empty list if none. analog_verdict: one line — is the Kazakhstan/CIS niche free, partly taken or crowded. mvp: 3-5 bullets a small team can ship in two weeks. main_risk: one line. localization: one line on what to adapt for Kazakhstan (payments such as Kaspi, Russian/Kazakh language, local rules).`;

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

/**
 * Ключи Groq: LS_GROQ_KEY (можно несколько через запятую) и LS_GROQ_KEY_2 … _3.
 * Ротация спасает, только если ключи из разных организаций Groq — лимиты
 * считаются на организацию, а не на ключ.
 */
function groqKeys(env) {
  const raw = [env.LS_GROQ_KEY, env.LS_GROQ_KEY_2, env.LS_GROQ_KEY_3].filter(Boolean).join(",");
  return [...new Set(raw.split(",").map((k) => k.trim()).filter(Boolean))];
}

/**
 * Запрос к Groq с ротацией ключей и моделей: на 429 — следующий ключ, когда
 * ключи кончились — следующая модель. Возвращает последний ответ (или null
 * при таймауте); 429 наружу — только если на пределе всё.
 */
async function groqFetch(env, body, models, timeoutMs = 27000) {
  const started = Date.now();
  let last = null;
  for (const model of models) {
    for (const key of groqKeys(env)) {
      const left = timeoutMs - (Date.now() - started);
      if (left < 3000) return last;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), left);
      try {
        last = await fetch("https://api.groq.com/openai/v1/chat/completions", {
          method: "POST", signal: ctrl.signal,
          headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
          body: JSON.stringify({ ...body, model }),
        });
      } catch (e) {
        return last;
      } finally {
        clearTimeout(timer);
      }
      if (last.status !== 429) return last;
    }
  }
  return last;
}

/**
 * «Умная» модель для диалога и карточки идеи — через OpenRouter, если задан
 * секрет LS_OPENROUTER_KEY; иначе Groq. Разделение владельца (2026-09-29):
 * массовый разбор постов — бесплатный Groq, разговор и советы — модель
 * сильнее. Модель меняется секретом/переменной без правки кода.
 */
const OR_CHAT_MODEL = "google/gemini-3.8-flash";

/**
 * Ошибка ИИ-провайдера — в лог Worker'а и в базу (последние 10): без этого
 * «ИИ ответил 403» (2026-09-29) нельзя было понять, кто именно отказал.
 */
async function noteAiError(env, provider, status, text) {
  const e = { ts: Math.floor(Date.now() / 1000), provider, status, text: String(text || "").slice(0, 300) };
  console.log("ai error", JSON.stringify(e));
  try {
    const cur = JSON.parse((await meta(env, "ai_errors")) || "[]");
    await setMeta(env, "ai_errors", JSON.stringify([e, ...cur].slice(0, 10)));
  } catch { /* лог — не повод ронять ответ */ }
}

const OR_FALLBACK_MODEL = "openai/gpt-6-luna";

async function smartFetch(env, body, groqModels, { web = false, timeoutMs = 27000 } = {}) {
  const started = Date.now();
  if (env.LS_OPENROUTER_KEY) {
    const { tools, tool_choice, reasoning_effort, max_completion_tokens, ...rest } = body;
    // С поиском — сначала Perplexity sonar (~11 с, первоисточники), запасная —
    // модель чата с :online (20-30 с на замере 2026-10-01).
    const models = web ? [env.LS_WEB_MODEL || WEB_SOURCE_MODEL, (env.LS_CHAT_MODEL || OR_CHAT_MODEL) + ":online"]
      : [env.LS_CHAT_MODEL || OR_CHAT_MODEL, OR_FALLBACK_MODEL];
    for (const model of models) {
      const isPplx = model.startsWith("perplexity/");
      const left = timeoutMs - (Date.now() - started);
      if (left < 4000) break;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), left);
      try {
        const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
          method: "POST", signal: ctrl.signal,
          headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
            "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
          body: JSON.stringify({ ...rest, ...(isPplx ? { response_format: undefined } : {}), max_tokens: max_completion_tokens || 2000, model }),
        });
        if (r.ok) return r;
        await noteAiError(env, "openrouter " + model, r.status, await r.text().catch(() => ""));
        // Ключ или оплата — другая модель не поможет.
        if (r.status === 401 || r.status === 402) break;
      } catch (e) {
        await noteAiError(env, "openrouter " + model, 0, String(e));
      } finally {
        clearTimeout(timer);
      }
    }
  }
  if (!groqKeys(env).length) return null;
  const left = Math.max(timeoutMs - (Date.now() - started), 8000);
  const r = await groqFetch(env, body, groqModels, left);
  if (r && !r.ok) {
    const t = await r.clone().text().catch(() => "");
    await noteAiError(env, "groq", r.status, t);
  }
  return r;
}

/**
 * Голосовое сообщение -> текст: Groq Whisper, при сбое — OpenRouter.
 * Русский и казахский распознаются оба. Возвращает текст или null.
 */
const VOICE_MAX_SECONDS = 180;

async function transcribe(env, voice, lang) {
  if (!voice || (!groqKeys(env).length && !env.LS_OPENROUTER_KEY) || (voice.duration || 0) > VOICE_MAX_SECONDS) return null;
  const f = await tg(env, "getFile", { file_id: voice.file_id });
  if (!f || !f.ok || !f.result.file_path) return null;
  const audio = await fetch(`https://api.telegram.org/file/bot${env.LS_BOT_TOKEN}/${f.result.file_path}`);
  if (!audio.ok) return null;
  const blob = await audio.blob();
  // Groq Whisper первым (решение владельца 2026-09-29): быстрый, точный и
  // бесплатный. OpenRouter (Gemini слушает ogg напрямую, ~$0,001 за минуту) —
  // только если у Groq ошибка или кончился лимит на всех ключах.
  for (const key of groqKeys(env)) {
    const form = new FormData();
    form.append("file", blob, "voice.ogg");
    form.append("model", "whisper-large-v3-turbo");
    form.append("language", lang === "en" ? "en" : lang === "kk" ? "kk" : "ru");
    form.append("response_format", "json");
    const r = await fetch("https://api.groq.com/openai/v1/audio/transcriptions", {
      method: "POST", headers: { authorization: `Bearer ${key}` }, body: form,
    }).catch(() => null);
    if (r && r.ok) {
      const j = await r.json().catch(() => null);
      if (j && j.text && String(j.text).trim()) return String(j.text).trim();
      break;
    }
    await noteAiError(env, "groq whisper", r ? r.status : 0, r ? await r.text().catch(() => "") : "сеть");
    if (!r || r.status !== 429) break;             // 429 — пробуем следующий ключ, иное — сразу OpenRouter
  }
  if (!env.LS_OPENROUTER_KEY) return null;
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
      "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
    body: JSON.stringify({
      model: env.LS_VOICE_MODEL || "google/gemini-3.1-flash-lite", temperature: 0,
      messages: [{ role: "user", content: [
        { type: "text", text: "Transcribe this voice message verbatim in its original language (Russian, Kazakh or English). Output only the transcript." },
        { type: "input_audio", input_audio: { data: btoa(bin), format: "ogg" } },
      ] }],
    }),
  }).catch(() => null);
  if (!r || !r.ok) {
    await noteAiError(env, "openrouter voice", r ? r.status : 0, r ? await r.text().catch(() => "") : "сеть");
    return null;
  }
  const j = await r.json().catch(() => null);
  const t = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
  return t && String(t).trim() ? String(t).trim() : null;
}

/** «Please try again in 11m7.44s» из ответа 429 — в минутах, вверх. */
function waitMinutes(text) {
  const m = /try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?/.exec(text || "");
  if (!m) return null;
  return Math.max(1, Math.ceil(Number(m[1] || 0) * 60 + Number(m[2] || 0) + Number(m[3] || 0) / 60));
}

/**
 * Рыночный фон для карточки идеи: раунды в секторах находки и ниши там же.
 * «Почему сейчас» должно опираться на деньги инвесторов, а не на лайки
 * одного поста (решение владельца 2026-09-28).
 */
async function marketContext(env, f) {
  const snap = await loadSnapshot(env);
  const rep = marketOf(snap) && marketOf(snap).report;
  if (!rep) return "";
  const w = rep.window_days || 14;
  const out = [`INVESTOR DATA (last ${w} days vs the ${w} before):`];
  for (const sid of (f.sectors || []).slice(0, 2)) {
    const sec = (rep.sectors || []).find((x) => x.id === sid);
    if (!sec) continue;
    const m = sec.money || {}, y = sec.yc || {};
    out.push(`- sector ${sid}: trend ${sec.trend}; ${m.cur_n || 0} rounds (${m.cur_early || 0} early), $${Math.round((m.cur_usd || 0) / 1e6)}M vs ${m.prev_n || 0} rounds, $${Math.round((m.prev_usd || 0) / 1e6)}M before; YC share ${y.share ?? "?"}% vs ${y.prev ?? "?"}%`);
    for (const n of (rep.niches || []).filter((x) => x.sector === sid).slice(0, 4)) {
      out.push(`  niche "${n.niche}": ${n.n} rounds in ${rep.niche_days || 28} days (${n.early} early), $${Math.round(n.usd / 1e6)}M: ` +
        (n.companies || []).slice(0, 3).map((r) => `${r.company}${r.stage ? " " + r.stage : ""}${r.usd ? " $" + (r.usd / 1e6).toFixed(1) + "M" : ""}`).join(", "));
    }
  }
  return out.length > 1 ? out.join("\n") : "";
}

async function ideaCard(env, f, uid, lang = "ru", { allowNew = true } = {}) {
  const s = L(lang);
  const row = await env.DB.prepare("SELECT data FROM idea_cards WHERE item_id = ?1")
    .bind(Number(f.id)).first().catch(() => null);
  if (row && row.data) {
    let saved = null;
    try { saved = JSON.parse(row.data); } catch { saved = null; }
    const card = ideaFor(saved, lang);
    if (card) return { card, cached: true };
  }
  if (!allowNew) return { error: fmt(s.ls_short, LS_PRICE.idea, (await lsGet(env, uid)).sub_ls + (await lsGet(env, uid)).credits) };
  const day = new Date().toISOString().slice(0, 10);
  const total = Number((await meta(env, "idea_calls_" + day)) || 0);
  const mine = Number((await meta(env, `idea_user_${uid}_${day}`)) || 0);
  if (total >= IDEA_MAX_PER_DAY) return { error: s.e_day };
  if (mine >= IDEA_MAX_PER_USER) return { error: fmt(s.e_user, mine) };
  if (!groqKeys(env).length && !env.LS_OPENROUTER_KEY) return { error: s.e_noai };

  const ai = f.ai || {};
  const tx = ai.i18n ? ai.i18n.en || ai.i18n.ru : ai;
  const user = [
    `Product: ${f.title}`,
    f.product_url ? `URL: ${f.product_url}` : "",
    tx && tx.summary ? `What it is: ${tx.summary}` : `Post: ${(f.body || "").slice(0, 800)}`,
    `Source: ${f.source}; traction numbers: likes/points ${f.likes ?? "?"}, replies ${f.replies ?? "?"}, bookmarks ${f.bookmarks ?? "?"}; score ${f.score} because: ${f.breakdown || "n/a"}`,
    await marketContext(env, f),
  ].filter(Boolean).join("\n");

  // Только 120b: поиск аналогов (browser_search) есть не у всех моделей.
  const r = await smartFetch(env, {
    messages: [{ role: "system", content: IDEA_SYSTEM }, { role: "user", content: user }],
    tools: [{ type: "browser_search" }],
    tool_choice: "auto",
    reasoning_effort: "low",
    max_completion_tokens: 4000,
    temperature: 0.2,
  }, ["openai/gpt-oss-120b"], { web: true, timeoutMs: 28000 });
  if (!r) return { error: s.e_time };
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

// ---------------------------------------------------------------------------
// 💬 Чат с аналитиком: вопросы свободным текстом
//
// Отвечает ТОЛЬКО по фактам среза: раунды последних трёх суток, выручка из
// статей и постов основателей, аналитика изданий, ниши и находки с откликом.
// У каждого факта есть дата и ссылка, и модель обязана их приводить: чат,
// который «вспоминает» стартапы годичной давности из обучения, бесполезен —
// стартаперу нужно то, что произошло вчера (требование владельца 2026-09-29).
// Помнит, чем занимается пользователь (коротко, в chat_profile), и последние
// реплики разговора.
// ---------------------------------------------------------------------------
// С LS суточные лимиты — только защита от злоупотреблений, тариф — баланс LS.
const CHAT_MAX_PER_USER = 120;
const CHAT_MAX_PER_DAY = 5000;
const CHAT_TURNS = 6;

const CHAT_SYSTEM = `You are the analyst inside launch-scout, a Telegram market radar for startup founders (Kazakhstan first, then the CIS and the English-speaking world).
You answer ONLY from the DATA block: venture rounds of the last 72 hours, revenue facts, analyst articles, niches with several rounds, fresh product launches with measured traction. Every fact there has a date and a link.
Rules:
- Every claim about a company must carry its date, the number (round size, stage, revenue, likes/points) and the link from DATA. Never use companies, numbers or events from your own memory; if DATA has nothing relevant, say so plainly and suggest what to follow instead.
- Freshness first: prefer items from the last 24 hours, then 72 hours; say how old each item is.
- Revenue (MRR/ARR) is rarely public. Mention it only when DATA has it; otherwise write that revenue is not disclosed.
- Tailor the answer to the user's PROFILE (what they build, their skills, market). End with 1-3 concrete next steps a small team can take this week (a product, a customer segment, a test), grounded in the facts you cited.
- Short paragraphs or bullets, no markdown headers, no tables, no hype. Plain text; put links as bare URLs.
- Answer in %LANG%.
Reply with JSON only: {"answer": "...", "profile": "..."} — profile is the user's updated profile in one or two English sentences (what they do, their market, interests) merged from the old PROFILE and this message; keep the old one if nothing new.`;

const LANG_EN = { ru: "Russian", kk: "Kazakh (Cyrillic)", en: "English" };

function ago(ts) {
  const h = Math.max(0, Math.round((Date.now() / 1000 - ts) / 3600));
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

const day = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

/** Факты среза одним текстом, сначала то, что ближе к вопросу и профилю. */
function chatData(snap, question, profile, limit = 14000) {
  const m = marketOf(snap) || {};
  const c = m.chat || {};
  const rep = m.report || {};
  const words = new Set(`${question} ${profile}`.toLowerCase().match(/[a-zа-яё0-9]{4,}/g) || []);
  const rel = (t) => { let n = 0; for (const w of String(t).toLowerCase().match(/[a-zа-яё0-9]{4,}/g) || []) if (words.has(w)) n++; return n; };
  const out = [];
  const rounds = (c.rounds || []).map((r) => {
    const what = (r.what || {}).en || (r.what || {}).ru || "";
    const line = `${day(r.ts)} (${ago(r.ts)}) | ${r.company} | ${r.usd ? "$" + (r.usd / 1e6).toFixed(1) + "M" : "amount n/a"}${r.stage ? " " + (STAGE[r.stage] || r.stage) : ""} | sector ${r.sector || "?"} | niche: ${r.niche || "?"} | ${what} | ${r.url || ""}`;
    return { line, score: rel(line) * 10 + (r.ts / 1e9) };
  }).sort((a, b) => b.score - a.score).slice(0, 45);
  if (rounds.length) out.push("ROUNDS (last 72h, one per company):", ...rounds.map((x) => "- " + x.line));
  const rev = (c.revenue || []).slice(0, 20).map((r) => `- ${day(r.ts)} (${ago(r.ts)}) | ${r.kind.toUpperCase()} ${r.usd ? "$" + Math.round(r.usd).toLocaleString("en-US") : ""} | ${r.who || ""} | ${String(r.text || "").slice(0, 220)} | ${r.source || ""} ${r.url || ""}`);
  if (rev.length) out.push("", "REVENUE FACTS (last 7 days):", ...rev);
  const niches = (rep.niches || []).filter((n) => n.n >= 2).slice(0, 10).map((n) => `- ${n.niche} (${n.sector}): ${n.n} rounds in ${rep.niche_days || 28}d, ${n.early} early, $${Math.round(n.usd / 1e6)}M — ` +
    (n.companies || []).slice(0, 4).map((r) => `${r.company} ${day(r.ts)}${r.usd ? " $" + (r.usd / 1e6).toFixed(1) + "M" : ""}${r.stage ? " " + r.stage : ""}${(r.what || {}).en ? " (" + r.what.en + ")" : ""}`).join(", ") +
    ((n.investors || []).length ? ` | investors: ${n.investors.join(", ")}` : "") +
    (n.gap ? ` | Kazakhstan: ${n.gap.kz || "?"}, CIS: ${n.gap.cis || "?"}${(n.gap.analogs || []).length ? " (local analogs: " + n.gap.analogs.map((a) => a.name + " " + a.country).join(", ") + ")" : ""}` : "") +
    ((n.pain || []).length ? ` | people ask: "${String(n.pain[0].text).slice(0, 140)}" ${n.pain[0].url}` : "") +
    ` | weekly rounds over 26 weeks: ${(n.weekly || []).join(",")}`);
  if (niches.length) out.push("", "NICHES WITH SEVERAL ROUNDS:", ...niches);
  const secs = (rep.sectors || []).filter((s) => s.signals).slice(0, 16).map((s) => `${s.id} ${s.trend} (${(s.money || {}).cur_n || 0} rounds/${rep.window_days || 14}d, ${(s.money || {}).cur_early || 0} early)`);
  if (secs.length) out.push("", "SECTOR TRENDS: " + secs.join("; "));
  const arts = (c.articles || []).slice(0, 4).map((a) => `- ${day(a.ts)} ${a.outlet}: ${a.title} — ${String(a.excerpt || "").slice(0, 350)} ${a.url}`);
  if (arts.length) out.push("", "ANALYST ARTICLES:", ...arts);
  const cutoff = Date.now() / 1000 - 48 * 3600;
  const launches = ((snap && snap.findings) || []).filter((f) => f.first_seen >= cutoff && f.score >= 42)
    .map((f) => ({ f, score: rel(`${f.title} ${f.body}`) * 10 + f.score / 100 })).sort((a, b) => b.score - a.score).slice(0, 12)
    .map(({ f }) => `- ${day(f.first_seen)} (${ago(f.first_seen)}) | ${f.source} | ${String(f.title || "").slice(0, 100)} | traction: ${f.likes ?? "?"} likes/points, ${f.replies ?? "?"} replies | ${(f.gist || {}).en || ""} | ${f.url}`);
  if (launches.length) out.push("", "FRESH LAUNCHES WITH TRACTION (last 48h):", ...launches);
  return out.join("\n").slice(0, limit);
}

async function chatHistory(env, uid) {
  const { results } = await env.DB.prepare("SELECT role, text FROM chat_log WHERE user_id = ?1 ORDER BY ts DESC LIMIT ?2")
    .bind(String(uid), CHAT_TURNS).all().catch(() => ({ results: [] }));
  return (results || []).reverse();
}

// Инструкции для режима с инструментами: модель сама запрашивает датасет.
const TOOL_RULES = `
You have tools over the bot's full dataset: search_rounds (all venture rounds of the last 6 months), list_opportunities (niches with opportunity type and transparent score), search_pain (people asking for products on X), search_launches (launches of the last 7 days with traction). Before answering any question about a market, niche, company, trend or idea, CALL THE TOOLS — never answer from memory. Search in English keywords even if the user writes in Russian or Kazakh; try synonyms if a search returns little.
Interpret signals, do not just list them. Tell apart: growing demand + few funded players = an open window; many early rounds + many similar products = a forming market (look for an unserved vertical); mega-rounds, late stages, dozens of players = overheated (entry needs strong differentiation). Always say which of these the evidence shows and why, citing the numbers the tools returned.`;

const CHECK_SYSTEM = `You are the analyst inside launch-scout. The user gives a startup idea. Your job is to try to KILL it with evidence first, then say honestly whether it survives. Market priority: Kazakhstan, then CIS and MENA, then global.
${"%TOOLS%"}
Use the tools (search_rounds with several keyword sets, search_pain, search_launches, list_opportunities) and, when needed, web search for competitors in Kazakhstan/CIS and their prices.
Answer in %LANG%, plain text, no markdown headers, compact:
❌ / ⚠️ lines — the strongest reasons NOT to do it (funded competitors with names, sums and dates; crowding; unproven willingness to pay; CAC; regulation).
🟢 lines — evidence FOR it (early rounds, growing requests, weak local competition, a technology window).
Verdict: one of «делать», «делать узко (какой сегмент)», «не делать» — with one sentence why.
What to verify in 7 days: 3 concrete steps (whom to interview, what landing/price test, what pilot).
Every company or number must come from the tools or web search, with its date and link. If data is thin, say so instead of guessing.`;

const DEEP_SYSTEM = `You are the analyst inside launch-scout. Dig deep into ONE niche for a founder (Kazakhstan first, then CIS/MENA, then global).
${"%TOOLS%"}
Use search_rounds (several keyword sets, 6 months), search_pain, search_launches, list_opportunities, and web search for competitors, their prices and customer complaints.
Answer in %LANG%, plain text, compact, sections as short lines starting with an emoji:
💰 Money: who raised, how much, stage, when (numbers, dates, links) and what the trend over months shows.
🏁 Players: who already does this globally and in Kazakhstan/CIS, with prices where found.
😡 Complaints: what customers dislike about existing products (only what you found).
🎯 First customer (ICP) and where to find the first 10.
💳 Monetization that works in this niche.
🧩 Technology/API that makes it cheaper to build now.
⚠️ Why this might NOT be worth doing.
✅ 7-day validation plan: 3 steps.
Every fact must have a date and a link. Say plainly where data is missing.`;

/**
 * Ответ ИИ-аналитика. mode: "chat" — разговор (JSON с обновлением профиля),
 * "check" — «Проверь мою идею», "deep" — «Копай глубже» по нише.
 * С OpenRouter модель работает с инструментами по полному датасету; без
 * него (или если OpenRouter не ответил) — обычный чат на срезе через Groq.
 */
async function chatReply(env, chatId, question, lang, mode = "chat", opts = {}) {
  const s = L(lang);
  const today = new Date().toISOString().slice(0, 10);
  const total = Number((await meta(env, "chat_calls_" + today)) || 0);
  const mine = Number((await meta(env, `chat_user_${chatId}_${today}`)) || 0);
  if (!groqKeys(env).length && !env.LS_OPENROUTER_KEY) return tg(env, "sendMessage", { chat_id: chatId, text: s.e_noai });
  if (total >= CHAT_MAX_PER_DAY) return tg(env, "sendMessage", { chat_id: chatId, text: s.e_day });
  if (mine >= CHAT_MAX_PER_USER) return tg(env, "sendMessage", { chat_id: chatId, text: fmt(s.chat_limit, mine) });
  const act = opts.upgrade ? "live_up" : opts.live ? "live" : (LS_PRICE[mode] ? mode : "chat");
  if (!(await lsCanAfford(env, chatId, act))) return lsShortMsg(env, chatId, lang, act);
  await tg(env, "sendChatAction", { chat_id: chatId, action: "typing" });

  const prof = await env.DB.prepare("SELECT about FROM chat_profile WHERE user_id = ?1").bind(String(chatId)).first().catch(() => null);
  const profile = (prof && prof.about) || "";
  const hist = await chatHistory(env, chatId);
  // Быстрый путь: матрица ниш + один вызов модели с потоком (секунды).
  // Запасные ниже — цепочка инструментов и срез через Groq — только если он
  // недоступен (нет матрицы, индекса или OpenRouter не ответил).
  if (await fastAnswer(env, chatId, question, lang, mode, { niche: opts.niche || null, profile, hist, live: !!opts.live, research: !!opts.research, upgrade: !!opts.upgrade })) {
    await setMeta(env, "chat_calls_" + today, total + 1);
    await setMeta(env, `chat_user_${chatId}_${today}`, mine + 1);
    return;
  }
  if (mode !== "chat") await tg(env, "sendMessage", { chat_id: chatId, text: s.digging });
  const snap = await loadSnapshot(env);
  const langName = LANG_EN[lang] || "Russian";
  let answer = "", about = "";

  if (env.LS_OPENROUTER_KEY) {
    const sys = mode === "check" ? CHECK_SYSTEM : mode === "deep" ? DEEP_SYSTEM : CHAT_SYSTEM + "\n" + TOOL_RULES;
    const messages = [
      { role: "system", content: sys.replace("%TOOLS%", TOOL_RULES).replace("%LANG%", langName) },
      // Короткий срез — ориентир; точные цифры модель берёт инструментами.
      { role: "user", content: `TODAY: ${today}\nPROFILE: ${profile || "unknown"}\n\nFRESH CONTEXT (last days; use tools for anything else):\n${chatData(snap, question, profile, 5000)}` },
      ...(mode === "chat" ? hist : []).map((h) => ({ role: h.role === "user" ? "user" : "assistant", content: String(h.text).slice(0, 1500) })),
      { role: "user", content: question.slice(0, 1500) },
    ];
    const res = await toolChat(env, messages, { web: mode !== "chat", maxSteps: mode === "chat" ? 3 : 4 });
    if (res.text) {
      if (mode === "chat") {
        const t = res.text, d = (() => { try { return JSON.parse(t.slice(t.indexOf("{"), t.lastIndexOf("}") + 1)); } catch { return null; } })();
        answer = String((d && d.answer) || (d ? "" : t)).trim();
        about = String((d && d.profile) || "").trim();
      } else {
        answer = res.text;
      }
    }
  }

  if (!answer && mode === "chat") {
    // Запасной путь: срез в промпте через Groq (без инструментов). Запрос
    // вместе с ответом должен уложиться в 8000 токенов в минуту: 2026-09-29
    // он весил 8557 и получал отказ — поэтому меньше фактов и короче история.
    const messages = [
      { role: "system", content: CHAT_SYSTEM.replace("%LANG%", langName) },
      { role: "user", content: `TODAY: ${today}\nPROFILE: ${profile || "unknown"}\n\nDATA:\n${chatData(snap, question, profile, 7000)}` },
      ...hist.slice(-4).map((h) => ({ role: h.role === "user" ? "user" : "assistant", content: String(h.text).slice(0, 700) })),
      { role: "user", content: question.slice(0, 1500) },
    ];
    const r = await groqFetch(env, { messages, reasoning_effort: "low", max_completion_tokens: 1300, temperature: 0.3,
      response_format: { type: "json_object" } }, ["openai/gpt-oss-120b", "openai/gpt-oss-20b"], 25000);
    if (r && r.ok) {
      try {
        const content = ((await r.json()).choices[0].message.content || "").trim();
        const d = JSON.parse(content.slice(content.indexOf("{"), content.lastIndexOf("}") + 1));
        answer = String((d && d.answer) || "").trim();
        about = String((d && d.profile) || "").trim();
      } catch { /* ниже — сообщение об ошибке */ }
    } else if (r) {
      await noteAiError(env, "groq chat", r.status, await r.text().catch(() => ""));
    }
  }
  await setMeta(env, "chat_calls_" + today, total + 1);
  await setMeta(env, `chat_user_${chatId}_${today}`, mine + 1);
  if (!answer) return tg(env, "sendMessage", { chat_id: chatId, text: s.e_busy });

  const now = Math.floor(Date.now() / 1000);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO chat_log (user_id, ts, role, text) VALUES (?1, ?2, 'user', ?3)").bind(String(chatId), now, question.slice(0, 1500)),
    env.DB.prepare("INSERT INTO chat_log (user_id, ts, role, text) VALUES (?1, ?2, 'assistant', ?3)").bind(String(chatId), now + 1, answer.slice(0, 3000)),
    env.DB.prepare("DELETE FROM chat_log WHERE user_id = ?1 AND ts < ?2").bind(String(chatId), now - 3 * 86400),
  ]).catch(() => null);
  if (about && about !== profile) {
    await env.DB.prepare("INSERT OR REPLACE INTO chat_profile (user_id, about, ts) VALUES (?1, ?2, ?3)")
      .bind(String(chatId), about.slice(0, 400), now).run().catch(() => null);
  }
  // Telegram: не больше 4096 символов в сообщении — длинный ответ частями.
  for (let i = 0; i < answer.length; i += 3800) {
    await tg(env, "sendMessage", { chat_id: chatId, text: esc(answer.slice(i, i + 3800)), parse_mode: "HTML", disable_web_page_preview: true });
  }
}

// ---------------------------------------------------------------------------
// 🧰 Инструменты ИИ: запросы к полному датасету, а не срез в промпте
//
// Раньше модель получала в промпте отобранные факты последних 72 часов и
// топ-10 ниш. Про нишу вне топа или про то, что было три месяца назад, ей
// было нечего сказать (вопрос владельца 2026-09-30). Теперь она сама
// спрашивает у бота: ищет раунды за полгода по ключевым словам, получает
// сводку ниши, жалобы людей, свежие запуски. Искать и считать — дело D1
// (таблица rounds), у Worker лимит ~10 мс процессора на запрос.
// ---------------------------------------------------------------------------
const TOOLS = [
  { type: "function", function: { name: "search_rounds",
    description: "Search ALL venture rounds of the last 6 months (one row per company) by English keywords over company, niche and description. Returns aggregates (count, sum, early-stage count, rounds per month, top investors) and up to 25 rows with dates and links.",
    parameters: { type: "object", properties: {
      keywords: { type: "array", items: { type: "string" }, description: "2-6 English keywords or short phrases, e.g. [\"insurance\", \"claims\"]" },
      sector: { type: "string", description: "optional sector id: hardware, defense_space, energy, mobility, ai_agents, ai_infra, devtools, security, fintech, health, consumer, commerce, b2b_saas, edu, proptech, crypto" },
      stage: { type: "string", enum: ["any", "early", "late"] },
      days: { type: "integer", description: "look back this many days, max 183" } },
      required: ["keywords"] } } },
  { type: "function", function: { name: "list_opportunities",
    description: "Niches where several companies raised rounds in the last 28 days, each with an opportunity type (window, forming, overheated, local_gap, watch), a transparent score with its parts, investors, weekly trend over 26 weeks, people's requests and whether Kazakhstan/CIS already has analogs.",
    parameters: { type: "object", properties: { type: { type: "string", enum: ["any", "window", "forming", "overheated", "local_gap", "watch"] } } } } },
  { type: "function", function: { name: "search_pain",
    description: "Posts on X where people ask for a product or complain (\"someone should build…\", \"I'd pay for…\") in the last 60 days, by English keywords.",
    parameters: { type: "object", properties: { keywords: { type: "array", items: { type: "string" } } }, required: ["keywords"] } } },
  { type: "function", function: { name: "search_launches",
    description: "Product launches of the last 7 days (Product Hunt, Hacker News, X, GitHub) with measured traction, by English keywords.",
    parameters: { type: "object", properties: { keywords: { type: "array", items: { type: "string" } } }, required: ["keywords"] } } },
];

const EARLY_STAGES = ["pre-seed", "seed", "a"];
const LATE_STAGES = ["b", "c+", "growth"];

function kwList(a) {
  return (Array.isArray(a) ? a : [a]).map((x) => String(x || "").toLowerCase().trim()).filter((x) => x.length >= 3).slice(0, 6);
}

/** Совпадения ключевых слов с текстом: число совпавших слов. */
function kwHits(text, kws) {
  const t = String(text || "").toLowerCase();
  let n = 0;
  for (const k of kws) if (t.includes(k.length > 6 ? k.slice(0, k.length - 2) : k)) n++;
  return n;
}

async function toolSearchRounds(env, a) {
  const kws = kwList(a.keywords);
  if (!kws.length) return { error: "no keywords" };
  const days = Math.min(Math.max(Number(a.days) || 183, 7), 183);
  const since = Math.floor(Date.now() / 1000) - days * 86400;
  const where = ["ts >= ?1"], binds = [since];
  // Любое из слов — в SQL (быстро), порядок по числу совпадений — здесь.
  where.push("(" + kws.map((k, i) => { binds.push("%" + (k.length > 6 ? k.slice(0, k.length - 2) : k) + "%"); return `doc LIKE ?${binds.length}`; }).join(" OR ") + ")");
  if (a.sector) { binds.push(String(a.sector)); where.push(`sector = ?${binds.length}`); }
  if (a.stage === "early") where.push("stage IN ('pre-seed','seed','a')");
  if (a.stage === "late") where.push("stage IN ('b','c+','growth')");
  const { results } = await env.DB.prepare(`SELECT * FROM rounds WHERE ${where.join(" AND ")} LIMIT 600`)
    .bind(...binds).all().catch((e) => ({ results: [], err: String(e) }));
  const rows = (results || []).map((r) => ({ ...r, hits: kwHits(r.doc, kws) }))
    .filter((r) => r.hits >= Math.min(2, kws.length)).sort((x, y) => y.hits - x.hits || y.ts - x.ts);
  const byMonth = {}, inv = {};
  let sum = 0, early = 0;
  for (const r of rows) {
    const m = new Date(r.ts * 1000).toISOString().slice(0, 7);
    byMonth[m] = (byMonth[m] || 0) + 1;
    sum += r.usd || 0;
    if (EARLY_STAGES.includes(r.stage)) early++;
    for (const i of String(r.investors || "").split(",").map((x) => x.trim()).filter(Boolean)) inv[i] = (inv[i] || 0) + 1;
  }
  return {
    matched: rows.length, days, total_usd_m: Math.round(sum / 1e6), early_stage: early,
    per_month: byMonth,
    top_investors: Object.entries(inv).sort((x, y) => y[1] - x[1]).slice(0, 8).map(([k, v]) => `${k} (${v})`),
    rounds: rows.slice(0, 25).map((r) => ({ date: new Date(r.ts * 1000).toISOString().slice(0, 10), company: r.company,
      usd_m: r.usd ? Math.round(r.usd / 1e5) / 10 : null, stage: r.stage, niche: r.niche, sector: r.sector,
      country: r.country, investors: r.investors, what: r.what_en || r.what_ru, url: r.url })),
  };
}

async function toolOpportunities(env, a) {
  const rep = (marketOf(await loadSnapshot(env)) || {}).report || {};
  return (rep.niches || []).filter((n) => !a.type || a.type === "any" || (n.opp || {}).type === a.type).slice(0, 15).map((n) => ({
    niche: n.niche, sector: n.sector, type: (n.opp || {}).type, score: (n.opp || {}).score,
    score_parts: ((n.opp || {}).parts || []).map((p) => `${p.k} ${p.pts > 0 ? "+" : ""}${p.pts} ${JSON.stringify(p.fact)}`),
    rounds_28d: n.n, early: n.early, usd_m: Math.round(n.usd / 1e6), companies_6m: n.companies_6m,
    mega: (n.mega || []).map((r) => `${r.company} $${Math.round((r.usd || 0) / 1e6)}M`),
    weekly_26w: (n.weekly || []).join(","), investors: n.investors,
    companies: (n.companies || []).map((r) => `${r.company} ${r.usd ? "$" + (r.usd / 1e6).toFixed(1) + "M" : ""} ${r.stage || ""} — ${(r.what || {}).en || ""} ${r.url || ""}`),
    people_ask: (n.pain || []).map((p) => `${p.text} ${p.url}`),
    kazakhstan: n.gap ? n.gap.kz : "not checked", cis: n.gap ? n.gap.cis : "not checked",
    local_analogs: n.gap ? (n.gap.analogs || []).map((x) => `${x.name} ${x.country} ${x.url}`) : [],
  }));
}

async function toolPain(env, a) {
  const kws = kwList(a.keywords);
  const c = (marketOf(await loadSnapshot(env)) || {}).chat || {};
  return (c.pain || []).map((p) => ({ ...p, hits: kwHits(`${p.text} ${p.niche}`, kws) })).filter((p) => p.hits)
    .sort((x, y) => y.hits - x.hits || (y.likes || 0) - (x.likes || 0)).slice(0, 15)
    .map((p) => ({ date: new Date(p.ts * 1000).toISOString().slice(0, 10), likes: p.likes, niche: p.niche, text: p.text, url: p.url }));
}

async function toolLaunches(env, a) {
  const kws = kwList(a.keywords);
  const snap = await loadSnapshot(env);
  return ((snap && snap.findings) || []).map((f) => ({ f, hits: kwHits(`${f.title} ${f.body} ${(f.gist || {}).en || ""}`, kws) }))
    .filter((x) => x.hits).sort((x, y) => y.hits - x.hits || y.f.score - x.f.score).slice(0, 12)
    .map(({ f }) => ({ date: new Date(f.first_seen * 1000).toISOString().slice(0, 10), source: f.source, title: String(f.title || "").slice(0, 120),
      traction: `${f.likes ?? "?"} likes/points/votes, ${f.replies ?? "?"} replies`, about: (f.gist || {}).en || "", url: f.url, site: f.product_url }));
}

async function runTool(env, name, args) {
  try {
    if (name === "search_rounds") return await toolSearchRounds(env, args || {});
    if (name === "list_opportunities") return await toolOpportunities(env, args || {});
    if (name === "search_pain") return await toolPain(env, args || {});
    if (name === "search_launches") return await toolLaunches(env, args || {});
  } catch (e) {
    return { error: String(e).slice(0, 200) };
  }
  return { error: "unknown tool" };
}

/**
 * Разговор с инструментами: модель запрашивает данные, Worker отвечает,
 * пока модель не даст итог (не больше 4 шагов и ~26 секунд — у фоновой
 * работы вебхука 30 секунд). Только OpenRouter: у бесплатного Groq на
 * такие диалоги не хватает 8000 токенов в минуту. Возвращает текст
 * последнего ответа модели или { error }.
 */
async function toolChat(env, messages, { maxSteps = 4, budgetMs = 26000, web = false } = {}) {
  if (!env.LS_OPENROUTER_KEY) return { error: "no-openrouter" };
  const started = Date.now();
  let model = env.LS_CHAT_MODEL || OR_CHAT_MODEL;
  if (web && !model.endsWith(":online")) model += ":online";
  const msgs = [...messages];
  for (let step = 0; step <= maxSteps; step++) {
    const left = budgetMs - (Date.now() - started);
    if (left < 5000) break;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), left);
    let r;
    try {
      r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
        method: "POST", signal: ctrl.signal,
        headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
          "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
        body: JSON.stringify({ model, messages: msgs, max_tokens: 2500, temperature: 0.3,
          ...(step < maxSteps ? { tools: TOOLS, tool_choice: "auto" } : {}) }),
      });
    } catch (e) {
      await noteAiError(env, "openrouter tools " + model, 0, String(e));
      return { error: "timeout" };
    } finally {
      clearTimeout(timer);
    }
    if (!r.ok) {
      await noteAiError(env, "openrouter tools " + model, r.status, await r.text().catch(() => ""));
      return { error: "http", status: r.status };
    }
    const j = await r.json().catch(() => null);
    const m = j && j.choices && j.choices[0] && j.choices[0].message;
    if (!m) return { error: "bad" };
    const calls = m.tool_calls || [];
    if (!calls.length) return { text: String(m.content || "").trim() };
    msgs.push({ role: "assistant", content: m.content || "", tool_calls: calls });
    for (const c of calls.slice(0, 4)) {
      let args = {};
      try { args = JSON.parse((c.function && c.function.arguments) || "{}"); } catch { args = {}; }
      const res = await runTool(env, c.function && c.function.name, args);
      msgs.push({ role: "tool", tool_call_id: c.id, content: JSON.stringify(res).slice(0, 12000) });
    }
  }
  return { error: "steps" };
}

/** Раунды за полгода из прогона — в таблицу rounds (POST /ingest-rounds). */
async function ingestRounds(env, rows) {
  const cols = ["key", "ts", "company", "usd", "stage", "niche", "sector", "country", "investors", "what_ru", "what_en", "url", "outlets", "doc"];
  const per = Math.floor(99 / cols.length);           // D1: не больше 100 параметров на запрос
  const stmts = [];
  for (let i = 0; i < rows.length; i += per) {
    const chunk = rows.slice(i, i + per), binds = [];
    const ph = chunk.map((r) => {
      const doc = [r.company, r.niche, r.what_en, r.what_ru, r.sector, r.investors, r.country].join(" ").toLowerCase();
      const vals = [r.key, r.ts, r.company, r.usd, r.stage, r.niche, r.sector, r.country, r.investors, r.what_ru, r.what_en, r.url, r.outlets, doc];
      return "(" + vals.map((v) => { binds.push(v === undefined ? null : v); return `?${binds.length}`; }).join(",") + ")";
    }).join(",");
    stmts.push(env.DB.prepare(`INSERT OR REPLACE INTO rounds (${cols.join(",")}) VALUES ${ph}`).bind(...binds));
  }
  if (stmts.length) await env.DB.batch(stmts);
  return rows.length;
}

// ---------------------------------------------------------------------------
// ⚡ Матрица ниш: ответ за секунды и только по датасету (2026-09-30)
//
// Цепочка «модель спрашивает базу по шагам» занимала 10–30 секунд. Теперь
// всё посчитано заранее (niche_matrix: по строке на нишу — деньги по
// месяцам, стадии, компании, инвесторы, «боль», КЗ/СНГ, тип и скор,
// конкуренты и жалобы из сети), а на вопрос уходит:
//   эмбеддинг вопроса (Workers AI, bge-m3) -> ближайшие ниши (Vectorize)
//   -> строки матрицы и раунды из D1 -> ОДИН вызов модели с потоковым
//   выводом. Каждый факт пронумерован [F7]; строки с суммами без ссылки на
//   факт вырезаются, ссылки превращаются в источники.
// Поиск в сети — только когда конкурентов в матрице нет: на это время в
// чате висит «🔎 Ищу…», и сообщение удаляется, когда приходит ответ.
// ---------------------------------------------------------------------------
const EMB_MODEL = "@cf/baai/bge-m3";
const FAST_MODEL = "google/gemini-3.8-flash";
const NICHE_SIM_MIN = 0.35;
let lastMatches = [];
let lastKept = [];
const JUNK = new Set(["unknown", "other", "n/a", "none", "misc", "various"]);         // ниши после фильтра релевантности — для замера      // сырые совпадения последнего поиска — для замера /debug-ask

async function embed(env, texts) {
  const r = await env.AI.run(EMB_MODEL, { text: texts });
  return (r && r.data) || [];
}

async function vecId(niche) {
  const h = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(niche));
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Строки матрицы из прогона: в D1 и в векторный индекс. */
async function ingestMatrix(env, rows) {
  const keep = {};
  const names = rows.map((r) => r.niche);
  // Сведения из сети, найденные ботом по запросу («Глубже»), прогон не знает:
  // не затираем их пустыми при обновлении строки.
  for (let i = 0; i < names.length; i += 50) {
    const part = names.slice(i, i + 50);
    const { results } = await env.DB.prepare(`SELECT niche, data FROM niche_matrix WHERE niche IN (${part.map((_, k) => "?" + (k + 1)).join(",")})`)
      .bind(...part).all().catch(() => ({ results: [] }));
    for (const r of results || []) {
      try { const d = JSON.parse(r.data); if (d.web) keep[r.niche] = d.web; } catch { /* битая строка — перезапишется */ }
    }
  }
  const now = Math.floor(Date.now() / 1000);
  const stmts = rows.map((r) => {
    const d = r.data || {};
    if (!d.web && keep[r.niche]) d.web = keep[r.niche];
    return env.DB.prepare("INSERT OR REPLACE INTO niche_matrix (niche, name_ru, sector, data, ts) VALUES (?1, ?2, ?3, ?4, ?5)")
      .bind(r.niche, r.name_ru || "", r.sector || "", JSON.stringify(d), now);
  });
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return indexNiches(env, rows.map((r) => ({ niche: r.niche, doc: r.doc || r.niche })));
}

/**
 * Эмбеддинги ниш — пачками по 20: на пачке из 60 длинных описаний Workers AI
 * молча возвращал пустой ответ, и 497 ниш из 557 не попали в индекс
 * (2026-09-30). Бросает ошибку, если хоть одна пачка не посчиталась, —
 * тогда прогон повторит эти строки.
 */
async function indexNiches(env, items) {
  let n = 0;
  for (let i = 0; i < items.length; i += 20) {
    const part = items.slice(i, i + 20);
    const vecs = await embed(env, part.map((x) => x.doc.slice(0, 1500)));
    if (vecs.length !== part.length) throw new Error(`эмбеддинги: ${vecs.length} из ${part.length}`);
    const out = [];
    for (let k = 0; k < part.length; k++) out.push({ id: await vecId(part[k].niche), values: vecs[k], metadata: { niche: part[k].niche } });
    await env.VEC.upsert(out);
    n += out.length;
  }
  return n;
}

/**
 * Фильтр релевантности: из ниш, найденных по смыслу, быстрая модель Groq
 * оставляет относящиеся к вопросу. Векторный поиск на вопрос о страховании
 * тянул «baby monitoring» и «loan origination» (замер 2026-09-30), а итоговые
 * суммы по нишам должны считаться только по релевантным. Английский
 * ранжировщик Workers AI русские вопросы не понимает, поэтому — LLM.
 * Не успела за 2,5 с или ошиблась — берём найденное как есть.
 */
const GATE_MODEL = "openai/gpt-oss-20b";

async function gateNiches(env, question, cands) {
  const keys = groqKeys(env);
  if (!keys.length || cands.length <= 1) return cands.map((c) => c.niche);
  const list = cands.map((c, i) => `${i}: ${c.niche}${c.name_ru ? " / " + c.name_ru : ""}`).join("\n");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    const r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST", signal: ctrl.signal,
      headers: { authorization: `Bearer ${keys[0]}`, "content-type": "application/json" },
      body: JSON.stringify({ model: env.LS_GATE_MODEL || GATE_MODEL, temperature: 0, max_completion_tokens: 300,
        reasoning_effort: "low", response_format: { type: "json_object" },
        messages: [
          { role: "system", content: "You pick which startup niches are relevant to a founder's question. A niche is relevant if its companies are what the question asks about (same industry and problem), not merely adjacent. Reply JSON only: {\"keep\": [indices]}. If the question is general (where to go, what is overheated) keep all." },
          { role: "user", content: `Question: ${question.slice(0, 600)}\nNiches:\n${list}` }] }),
    });
    if (!r.ok) {
      await noteAiError(env, "groq gate", r.status, await r.text().catch(() => ""));
      return cands.map((c) => c.niche);
    }
    const t = ((await r.json()).choices[0].message.content || "").trim();
    const keep = (JSON.parse(t.slice(t.indexOf("{"), t.lastIndexOf("}") + 1)).keep || []).map(Number);
    const out = keep.filter((i) => cands[i]).map((i) => cands[i].niche);
    return out.length ? out : cands.map((c) => c.niche);
  } catch (e) {
    return cands.map((c) => c.niche);
  } finally {
    clearTimeout(timer);
  }
}

// Общие слова вопроса: по ним вектор тянуло к финансовым нишам («есть ли
// деньги в edtech» находил платежи, а не образование — замер 2026-09-30).
// \b в JavaScript не видит границ кириллических слов — поэтому юникод-классы.
const STOP_WORDS = /(?<!\p{L})(сколько|деньги|денег|деньгах|инвестиц\p{L}*|раунд\p{L}*|сейчас|есть|ли|что|какие|какой|куда|где|кто|рынок|рынке|рынка|ниш\p{L}*|полгода|месяц\p{L}*|за|в|на|и|с|по|для|money|funding|rounds?|how|much|what|which|market|now|is|are|there|any)(?!\p{L})/giu;

// Словарь «слово вопроса -> основа в названиях ниш» для поиска по словам.
const SYNONYMS = [
  [/edtech|образован|обучен|школ|студент|учёб|учеб|репетит/i, ["educat", "learning", "school", "student", "tutor"]],
  [/страхов|insur/i, ["insurance"]], [/юрист|юрид|legal|право/i, ["legal", "law"]],
  [/медицин|клиник|здоров|врач|пациент|health/i, ["health", "clinic", "patient", "medical"]],
  [/финтех|платеж|платёж|банк|fintech|payment/i, ["payment", "bank", "fintech"]],
  [/кредит|займ|lend|loan/i, ["lending", "loan", "credit"]], [/стейбл|stablecoin/i, ["stablecoin"]],
  [/крипт|блокчейн|web3|crypto/i, ["crypto", "blockchain", "onchain"]],
  [/логист|склад|достав|грузо|logist|warehouse/i, ["logistic", "warehouse", "freight", "delivery", "supply chain"]],
  [/недвиж|строит|стройк|real estate/i, ["real estate", "construction", "property", "housing"]],
  [/агро|сельск|ферм|agri/i, ["agri", "farm", "crop"]], [/дрон|беспилот|drone/i, ["drone", "unmanned", "uas"]],
  [/робот|robot|гуманоид/i, ["robot", "humanoid"]], [/оборон|военн|defen/i, ["defense", "military"]],
  [/космос|спутник|space|satellit/i, ["space", "satellite", "orbit"]],
  [/энерг|электросет|солнеч|батар|energy|grid/i, ["energy", "grid", "solar", "battery", "power"]],
  [/кибер|безопасн|security/i, ["security", "identity", "fraud"]],
  [/бухгалт|учёт|финансов|accounting/i, ["accounting", "bookkeeping", "finance", "tax"]],
  [/маркетинг|реклам|marketing/i, ["marketing", "advertis"]], [/продаж|sales|crm/i, ["sales", "crm"]],
  [/найм|рекрут|hr\b|кадр|recruit|hiring/i, ["recruit", "hiring", "talent", "hr "]],
  [/голос|voice/i, ["voice"]], [/поддержк|support/i, ["support", "customer service"]],
  [/разработ|программ|devtool|developer|coding/i, ["developer", "coding", "code"]],
  [/e-?commerce|ecommerce|магазин|маркетплейс|ритейл|retail/i, ["commerce", "retail", "marketplace", "shop"]],
  [/путешеств|туризм|travel/i, ["travel"]], [/еда|ресторан|food/i, ["food", "restaurant"]],
];

function lexicalStems(question) {
  const out = new Set();
  for (const [rx, stems] of SYNONYMS) if (rx.test(question)) stems.forEach((x) => out.add(x));
  for (const w of question.toLowerCase().match(/[a-z][a-z-]{4,}/g) || []) out.add(w.slice(0, 7));
  return [...out].slice(0, 8);
}

const usdM = (v) => (v ? (v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : `$${(v / 1e6).toFixed(1)}M`) : "amount n/a");

/**
 * Факты под вопрос: ниши по смыслу, их раунды, компании по имени, общий
 * итог по найденным нишам, а для общих вопросов — верхние возможности.
 * Возвращает { lines, facts: [{id, url}], niches }.
 */
async function matrixFacts(env, question, { niche = null, snap = null } = {}) {
  const facts = [];
  const add = (text, url = null) => { facts.push({ id: facts.length + 1, text, url }); return facts.length; };
  let names = [];
  if (niche) names = [niche];
  else {
    const clean = question.replace(STOP_WORDS, " ").replace(/\s+/g, " ").trim() || question;
    const stems = lexicalStems(question);
    // Поиск по словам в названиях ниш — параллельно с векторным.
    const lexP = stems.length
      ? env.DB.prepare(`SELECT niche FROM niche_matrix WHERE ${stems.map((_, k) => `(lower(niche) LIKE ?${k + 1})`).join(" OR ")} LIMIT 40`)
        .bind(...stems.map((x) => `%${x}%`)).all().catch(() => ({ results: [] }))
      : Promise.resolve({ results: [] });
    const [[qv], lex] = await Promise.all([embed(env, [clean.slice(0, 1000)]), lexP]);
    const lexNames = (lex.results || []).map((r) => r.niche);
    if (qv) {
      const res = await env.VEC.query(qv, { topK: 12, returnMetadata: "all" });
      lastMatches = ((res && res.matches) || []).map((m) => ({ niche: m.metadata && m.metadata.niche, score: Math.round(m.score * 1000) / 1000 }));
      // Порог относительный: у bge-m3 близкие ниши дают 0,40–0,60. Берём
      // не дальше 0,08 от лучшего совпадения, дальше решает фильтр.
      const ms = (res && res.matches) || [];
      const best = ms.length ? ms[0].score : 0;
      names = ms.filter((m) => m.score >= NICHE_SIM_MIN && m.score >= best - 0.08).map((m) => m.metadata && m.metadata.niche).filter(Boolean);
    }
    // Объединение: сначала совпавшие по словам (их точность выше), затем
    // по смыслу; всего не больше 16 кандидатов — дальше решает фильтр.
    names = [...new Set([...lexNames.slice(0, 10), ...names])].filter((n) => !JUNK.has(n)).slice(0, 16);
  }
  // Строки матрицы и прямые совпадения по компаниям — параллельно.
  const tokens = [...new Set((question.match(/[A-Za-z][A-Za-z0-9.&-]{3,}/g) || []).map((t) => t.toLowerCase()))].slice(0, 3);
  // Фильтр релевантности — параллельно с загрузкой строк (по английским
  // названиям ниш): экономит ~0,3 с до первого слова.
  const gateP = (!niche && names.length > 1) ? gateNiches(env, question, names.map((n) => ({ niche: n }))) : Promise.resolve(null);
  const [keepList, matrixRes, ...companyRes] = await Promise.all([
    gateP,
    names.length
      ? env.DB.prepare(`SELECT niche, name_ru, data FROM niche_matrix WHERE niche IN (${names.map((_, k) => "?" + (k + 1)).join(",")})`)
        .bind(...names).all().catch(() => ({ results: [] }))
      : Promise.resolve({ results: [] }),
    ...tokens.map((t) => env.DB.prepare("SELECT * FROM rounds WHERE lower(company) LIKE ?1 ORDER BY ts DESC LIMIT 3")
      .bind(`%${t}%`).all().catch(() => ({ results: [] }))),
  ]);
  const by = Object.fromEntries((matrixRes.results || []).map((r) => [r.niche, r]));
  let found = names.filter((n) => by[n]).map((n) => ({ niche: n, name_ru: by[n].name_ru }));
  if (keepList) {
    const keep = new Set(keepList);
    const kept = found.filter((c) => keep.has(c.niche));
    if (kept.length) found = kept;
  }
  lastKept = found.map((c) => c.niche);
  const rows = [];
  for (const c of found) { try { rows.push({ ...JSON.parse(by[c.niche].data), name_ru: by[c.niche].name_ru }); } catch { /* пропуск */ } }
  // Итог по всем релевантным нишам — считает код, не модель (модель сама
  // складывала суммы — замер 2026-09-30).
  if (rows.length) {
    const tot = { rounds: 0, early: 0, usd: 0, monthly: {} };
    for (const d of rows) {
      tot.rounds += d.companies_6m || 0; tot.early += d.early_6m || 0; tot.usd += d.usd_6m || 0;
      for (const [m, v] of Object.entries(d.monthly || {})) tot.monthly[m] = (tot.monthly[m] || 0) + v;
    }
    add(`TOTAL (computed, use it for any sum or count across niches) over the ${rows.length} relevant niches (${rows.map((d) => d.niche).join("; ")}), last 6 months: ${tot.rounds} companies raised rounds (${tot.early} early-stage), ${usdM(tot.usd)} excluding $1B+ mega-rounds; rounds per month ${JSON.stringify(tot.monthly)}`);
  }
  for (const d of rows.slice(0, 6)) {
    const o = d.opp || {};
    add(`NICHE "${d.niche}"${d.name_ru ? " / " + d.name_ru : ""} (sector ${d.sector}): opportunity type ${o.type}, score ${o.score}/100 = ${(o.parts || []).map((p) => `${p.k} ${p.pts > 0 ? "+" : ""}${p.pts}`).join(", ")}; ` +
      `6 months: ${d.companies_6m} companies raised, ${d.early_6m} early, ${usdM(d.usd_6m)}; by month ${JSON.stringify(d.monthly || {})}; stages ${JSON.stringify(d.stages_6m || {})}; ` +
      `last 28 days: ${d.n} rounds (${d.early} early), ${usdM(d.usd)}` +
      ((d.mega || []).length ? `; mega-rounds: ${d.mega.map((r) => `${r.company} ${usdM(r.usd)}`).join(", ")}` : "") +
      ((d.investors || []).length ? `; investors: ${d.investors.join(", ")}` : "") +
      (d.gap ? `; Kazakhstan: ${d.gap.kz || "?"}, CIS: ${d.gap.cis || "?"}${(d.gap.analogs || []).length ? " (local analogs: " + d.gap.analogs.map((a) => `${a.name} ${a.country}`).join(", ") + ")" : ""}` : "; Kazakhstan/CIS: not checked"));
    for (const r of (d.top_6m || []).slice(0, 5)) {
      add(`ROUND ${new Date(r.ts * 1000).toISOString().slice(0, 10)}: ${r.company} — ${usdM(r.usd)}${r.stage ? " " + r.stage : ""}, niche "${d.niche}"${(r.investors || []).length ? ", investors " + r.investors.join(", ") : ""}: ${(r.what || {}).en || ""}`, r.url);
    }
    for (const t of (d.local_tasks || []).slice(0, 3)) add(`KAZAKHSTAN COMPANY TASK in "${d.niche}" (Astana Hub, ${t.bids ?? "?"} team bids): ${t.company || "?"} needs: ${t.title}`, t.url);
    if ((d.hiring || []).length) add(`HIRING in "${d.niche}": ${d.hiring_n} funded companies posted jobs in the latest HN "Who is hiring": ${d.hiring.map((h) => h.company).join(", ")}`, d.hiring[0].url);
    if (d.search && d.search.rel) add(`GOOGLE SEARCH INTEREST in "${d.niche}" (Google Trends, query "${d.search.term}", worldwide): over the last 3 months it grew ${d.search.rel}x relative to the median niche, faster than ${d.search.pct}% of niches; weekly index for 26 weeks (0-100, relative) ${JSON.stringify(d.search.weekly || [])}. Google changed how it counts searches in mid-2026, so compare niches, not absolute values.`, `https://trends.google.com/trends/explore?q=${encodeURIComponent(d.search.term)}`);
    for (const p of (d.pain || []).slice(0, 2)) add(`PEOPLE ASK (${new Date(p.ts * 1000).toISOString().slice(0, 10)}, ${p.likes || 0} likes) about "${d.niche}": ${p.text}`, p.url);
    const w = d.web;
    if (w) {
      for (const c of (w.competitors || []).slice(0, 6)) add(`COMPETITOR in "${d.niche}": ${c.name} (${c.market || "?"}), price ${c.price || "not published"} — ${c.note || ""}`, c.url);
      for (const c of (w.complaints || []).slice(0, 3)) add(`CUSTOMER COMPLAINT in "${d.niche}": ${c.text}`, c.source);
      if (w.pricing) add(`PRICING in "${d.niche}": ${w.pricing}`);
      if (w.icp) add(`FIRST CUSTOMERS in "${d.niche}": ${w.icp}`);
    }
  }
  // Компании, названные в вопросе прямо (латиница от 4 букв).
  for (const res of companyRes) {
    for (const r of res.results || []) add(`ROUND ${new Date(r.ts * 1000).toISOString().slice(0, 10)}: ${r.company} — ${usdM(r.usd)}${r.stage ? " " + r.stage : ""}, niche "${r.niche}": ${r.what_en || r.what_ru || ""}`, r.url);
  }
  // Вопрос про регион — раунды компаний из Казахстана и СНГ (страна из
  // разбора ИИ; русскоязычные источники добавлены 2026-09-30).
  if (/казах|kazakh|\bkz\b|снг|\bcis\b|центральн\p{L}* ази|узбек|кыргыз|армен|грузи|азербайдж|алмат|астан/iu.test(question)) {
    const { results } = await env.DB.prepare("SELECT * FROM rounds WHERE country IN ('KZ','UZ','KG','TJ','AM','GE','AZ','BY','RU') ORDER BY ts DESC LIMIT 15")
      .all().catch(() => ({ results: [] }));
    for (const r of results || []) add(`ROUND IN THE REGION (${r.country}) ${new Date(r.ts * 1000).toISOString().slice(0, 10)}: ${r.company} — ${usdM(r.usd)}${r.stage ? " " + r.stage : ""}, niche "${r.niche}"${r.investors ? ", investors " + r.investors : ""}: ${r.what_en || r.what_ru || ""}`, r.url);
  }
  const chatSnap = ((snap && marketOf(snap)) || {}).chat || {};
  if (/казах|kazakh|\bkz\b|снг|\bcis\b|заказчик|задач\p{L}* компан|astana ?hub/iu.test(question)) {
    for (const t of (chatSnap.local_tasks || []).slice(0, 10)) add(`KAZAKHSTAN COMPANY TASK (Astana Hub, area "${t.area}", ${t.bids ?? "?"} team bids, deadline ${t.deadline}): ${t.company || "?"} needs: ${t.title}`, t.url);
  }
  if (/хакатон|hackathon|devpost/iu.test(question)) {
    for (const h of (chatSnap.hackathons || []).slice(0, 12)) add(`HACKATHON (Devpost, ${h.dates}, ${h.location}): ${h.title} by ${h.org}, prize ${h.prize}, themes ${h.themes}`, h.url);
  }
  // Общий вопрос («куда идти?») или мало совпадений — верхние возможности.
  const rep = ((snap && marketOf(snap)) || {}).report || {};
  if (rows.length < 2) {
    for (const n of (rep.niches || []).slice(0, 8)) {
      const o = n.opp || {};
      add(`OPPORTUNITY "${n.niche}": type ${o.type}, score ${o.score}/100; last 28 days ${n.n} rounds (${n.early} early), ${usdM(n.usd)}; ${n.companies_6m} companies in 6 months; Kazakhstan ${n.gap ? n.gap.kz : "not checked"}`);
    }
  }
  // Кто вкладывает: рейтинг посчитан кодом по всем раундам за полгода.
  if (INVESTOR_Q.test(question)) {
    const inv = rep.investors_top || [];
    const secsQ = [...new Set(rows.map((d) => d.sector).filter(Boolean))];
    const pick = secsQ.length ? inv.filter((x) => secsQ.some((sx) => (x.sectors || {})[sx])).slice(0, 8) : [];
    const list = [...pick, ...inv.filter((x) => !pick.includes(x))].slice(0, 15);
    list.forEach((x, i) => add(`INVESTOR LEADERBOARD (computed from our dataset of rounds, last 6 months)${pick.includes(x) ? " in sectors " + secsQ.join("/") : ""} #${i + 1}: ${x.name} — ${x.n} rounds (${x.early} early-stage), ${usdM(x.usd)} raised in those rounds; sectors ${JSON.stringify(x.sectors)}; niches ${(x.niches || []).join(", ")}; recent: ${(x.companies || []).map((c) => `${c.company} ${usdM(c.usd)}${c.stage ? " " + c.stage : ""}`).join("; ")}`,
      ((x.companies || [])[0] || {}).url || null));
  }
  const secs = (rep.sectors || []).filter((x) => x.signals).map((x) => `${x.id} ${x.trend} (${(x.money || {}).cur_n || 0} rounds/${rep.window_days || 14}d, ${(x.money || {}).cur_early || 0} early)`);
  if (secs.length) add(`SECTOR TRENDS (share of all rounds): ${secs.join("; ")}`);
  return { facts, niches: rows };
}

const FAST_RULES = `Never add up or compute numbers yourself: for any sum or count across niches quote the TOTAL fact. Answer ONLY from the FACTS list. After every claim put the fact number in square brackets, e.g. [F3]; a claim with a company, a sum or a count MUST carry one. Never use companies, numbers or events from your own memory. If FACTS do not answer the question, say so in one line and say what the data does show.
Never recommend building or investing in businesses based on interest-bearing lending (riba), gambling or betting, alcohol, cannabis, pork or adult content: you may state their numbers neutrally as market facts, but do not present them as opportunities, next steps or ideas for the user.
Interpret, do not just list: say whether the evidence shows an open window (demand, few funded players), a forming market (many early rounds and similar products — look for an unserved vertical) or an overheated one (mega-rounds, late stages, dozens of players).
Market numbers (money, rounds, investors, niches) come from our dataset facts (TOTAL, NICHE, ROUND, INVESTOR LEADERBOARD); WEB, HACKER NEWS and GITHUB facts describe specific companies and products or complement the dataset. Ignore facts that do not answer the question (other countries' corporate spending, unrelated companies).
Never open with what is missing (no "В данных нет…", "No data on…"): the first line answers the question with what the evidence shows; if something asked is not in FACTS, say it in one short line at the end.
FORMAT for a phone screen: the first line is the verdict in one sentence wrapped in **double asterisks**. Then blocks, one per line, each like: "<ONE emoji> **Short title, 2-4 words** — 1-2 short sentences". Emojis: 💰 money · 📈 growth · 🔎 search interest · 🏁 competitors · 🇰🇿 Kazakhstan/CIS · 💼 investors · 🙋 demand · ⚠️ risk · 💡 idea. Key numbers and names in **bold**. A blank line between blocks. No # headers, no tables, no other markdown. Answer in %LANG%.`;

const FAST_SYSTEM = {
  chat: `You are the analyst inside launch-scout, a market radar for founders (Kazakhstan first, then CIS/MENA, then global).
${FAST_RULES}
Tailor the answer to the user's PROFILE. 3-5 bullets, then a blank line and a line like "👉 **Что сделать:**" (in the answer language) with 1-2 numbered concrete steps. At most 10 short lines — the user has buttons for a deep dive, an idea map and competitors.
If the user's message tells something new about them (what they build, skills, budget, market), add a last line "PROFILE: <their updated profile in one English sentence>"; otherwise do not add it.`,
  deep: `You are the analyst inside launch-scout. Give a deep dive into ONE niche for a founder (Kazakhstan first).
${FAST_RULES}
Sections as short lines starting with an emoji: 💰 money and trend by month · 🏁 players and prices (global and KZ/CIS) · 😡 what customers complain about · 🎯 first customers · 💳 how money is made · ⚠️ why this might NOT be worth doing · ✅ 7-day validation plan (3 steps). At most 22 lines.`,
  map: `You are the analyst inside launch-scout. Build a one-screen IDEA MAP for a founder (Kazakhstan first, then CIS/MENA, then global).
${FAST_RULES}
Exactly these blocks, each a bold emoji title line and 1-2 short lines: 🎯 **Problem and who pays** · 📊 **Market signals** (money, rounds, search interest, demand) · 🏁 **Competitors and prices** · 🇰🇿 **Kazakhstan/CIS** · 🛠 **MVP in 2 weeks** (3 features max) · 👥 **First 10 customers** (where exactly to find them) · 💳 **Business model and price** · ⚠️ **Main risk** · ✅ **7-day test** (3 steps). Titles in the answer language. At most 26 lines.`,
  comp: `You are the analyst inside launch-scout. Show a founder who already does this and where a newcomer can win (Kazakhstan first).
${FAST_RULES}
Blocks: 🏁 leaders with prices (one line each, name in bold) · 🇰🇿 local players · 😡 what customers complain about · 🕳 gaps a small team can take · 👉 first move. At most 18 lines.`,
  research: `You are the analyst inside launch-scout. FACTS come from a live search on X (posts with likes and author audience) and Google Trends. Summarise what people actually say and how search interest moves, for the founder's QUESTION.
${FAST_RULES}
Weigh posts by engagement and author audience; ignore spam and giveaways. Blocks: 🗣 what people say (2-4 bullets) · 🔎 search interest (worldwide and Kazakhstan) · 💡 what it means for the founder. At most 14 lines.`,
  check: `You are the analyst inside launch-scout. The user gives a startup idea. First try to KILL it with evidence, then say honestly whether it survives. Market priority: Kazakhstan, then CIS/MENA, then global.
${FAST_RULES}
Format: ❌/⚠️ lines — strongest reasons not to do it; 🟢 lines — evidence for it; "Вердикт:" one of делать / делать узко (which segment) / не делать, with one sentence why; then 3 steps to verify in 7 days. At most 18 lines.`,
};

/** Потоковый ответ OpenRouter: onDelta(текст до сих пор). Возвращает полный текст или null. */
// Первое слово не пришло за это время — обрываем и идём к запасной модели:
// на замере 2026-09-30 провайдер Gemini изредка молчал 13–15 секунд.
const FIRST_TOKEN_MS = 4500;
const FALLBACK_FAST_MODEL = "openai/gpt-oss-120b";

async function streamOpenRouter(env, model, messages, onDelta, timeoutMs = 25000, maxTokens = 1800, firstMs = FIRST_TOKEN_MS, meter = null) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let gotFirst = false;
  const firstTimer = setTimeout(() => { if (!gotFirst) ctrl.abort(); }, firstMs);
  try {
    const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST", signal: ctrl.signal,
      headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
        "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
      body: JSON.stringify({ model, messages, stream: true, max_tokens: maxTokens, temperature: 0.3, usage: { include: true },
        reasoning: { effort: "low", exclude: true },
        // gpt-oss — на серверах Groq через OpenRouter: первое слово за доли
        // секунды и сотни токенов в секунду, без минутного лимита бесплатного Groq.
        ...(model.startsWith("openai/gpt-oss") ? { provider: { order: ["groq"], allow_fallbacks: true } } : {}) }),
    });
    if (!r.ok || !r.body) {
      await noteAiError(env, "openrouter fast " + model, r.status, await r.text().catch(() => ""));
      return null;
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", text = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") continue;
        try {
          const obj = JSON.parse(payload);
          // Последний кусок потока несёт usage.cost — фактическую цену вызова.
          if (meter && obj.usage && obj.usage.cost) meter.usd += Number(obj.usage.cost) || 0;
          const d = obj.choices && obj.choices[0] && obj.choices[0].delta && obj.choices[0].delta.content;
          if (d) { gotFirst = true; text += d; await onDelta(text); }
        } catch { /* служебные строки потока */ }
      }
    }
    return text;
  } catch (e) {
    await noteAiError(env, "openrouter fast " + model, 0, gotFirst ? String(e) : `нет первого слова за ${firstMs} мс`);
    return null;
  } finally {
    clearTimeout(timer);
    clearTimeout(firstTimer);
  }
}

/**
 * Итоговый текст: ссылки [F7] -> источники, строки с суммами и числами без
 * ссылки на факт — вон (так модель не может «вспомнить» свои цифры).
 */
// ---------------------------------------------------------------------------
// 🧷 Проверка ответа по фактам (защита от выдумок, 2026-10-01)
//
// Ссылка [F7] ещё не значит, что число взято из F7: модель могла сослаться и
// написать своё. Каждое число в строке со ссылкой ищем среди чисел фактов
// ($21,5 млн = $21.5M = 21.5 million, допуск 1,5% на округление); выделенное
// название компании латиницей — в тексте фактов. Не нашлось — строку вон.
// ---------------------------------------------------------------------------
const UNIT = { "трлн": 1e12, "trillion": 1e12, "tn": 1e12, "млрд": 1e9, "bn": 1e9, "billion": 1e9, "b": 1e9, "млн": 1e6, "mn": 1e6, "million": 1e6, "m": 1e6,
  "тыс": 1e3, "k": 1e3, "thousand": 1e3 };
const NUM_RE = /(?<![\p{L}\d.,])([$€£]\s?)?(\d{1,3}(?:[ \u00a0\u202f,]\d{3})+|\d+(?:[.,]\d+)?)\s?(трлн|trillion|tn|млрд|млн|тыс|bn|billion|mn|million|thousand|[bmk](?![\p{L}]))?\.?(%)?/giu;

/** Числа текста: [{v, money}] — суммы приведены к единицам, годы и номера пунктов пропущены. */
function numbersIn(text) {
  const out = [];
  const t = String(text || "").replace(/<[^>]+>/g, " ").replace(/^\s*\d+[.)]\s/, " ");
  for (const m of t.matchAll(NUM_RE)) {
    let raw = m[2];
    // «1,420» и «1 420» — тысячи; «4,7» — десятичная запятая.
    // С единицей («2,357 трлн») запятая — десятичная.
    if (/^\d{1,3}([ \u00a0\u202f,]\d{3})+$/.test(raw) && !(m[3] && /^\d{1,3},\d{3}$/.test(raw))) raw = raw.replace(/[ \u00a0\u202f,]/g, "");
    else raw = raw.replace(",", ".");
    let v = Number(raw);
    if (!Number.isFinite(v)) continue;
    const unit = (m[3] || "").toLowerCase();
    if (!m[1] && !unit && !m[4] && v >= 1990 && v <= 2035 && Number.isInteger(v)) continue;   // год
    if (!m[1] && !unit && !m[4] && v < 2) continue;                                            // «1» — шум
    if (unit) v *= UNIT[unit] || 1;
    out.push({ v, money: !!(m[1] || unit) });
  }
  return out;
}

const GENERIC_EN = /^(saas|paas|api|ai|llm|mvp|crm|erp|b2b|b2c|seed|pre-seed|series [a-e]\+?|growth|devops|fintech|edtech|healthtech|proptech|insurtech|legaltech|agentic ai|ai agents?|machine learning|open source|enterprise|startup|no-code|low-code|vertical saas|physical ai)$/i;

function makeChecker(facts, extra = "") {
  // Числа из вопроса и профиля тоже свои: «у меня $10 000» — не выдумка.
  const pool = [...facts.flatMap((f) => numbersIn(f.text)), ...numbersIn(extra)];
  const vals = pool.map((x) => x.v);
  const squash = (x) => String(x || "").toLowerCase().replace(/[^a-z0-9а-яё]/g, "");
  const corpus = squash(facts.map((f) => f.text).join(" ") + " " + extra);
  const pcts = new Set(pool.filter((x) => x.v > 0 && x.v < 100).map((x) => Math.round(x.v)));
  // «медленнее 87% ниш» при факте «быстрее 13%» — то же утверждение.
  const has = (v) => vals.some((x) => x === v || (x > 0 && Math.abs(x - v) / x <= 0.015)) || (v > 0 && v < 100 && pcts.has(Math.round(100 - v)));
  return {
    /** Числа строки, которых нет ни в одном факте. */
    badNumbers: (line) => numbersIn(line).filter((n) => !has(n.v)).map((n) => n.v),
    /** Выделенные названия латиницей, которых нет в фактах. */
    badNames: (htmlLine) => [...htmlLine.matchAll(/<b>([^<]{2,60})<\/b>/g)].map((m) => m[1])
      // Аббревиатуры (MVP, CRM) и общие слова (SaaS) — не названия компаний.
      .filter((b) => /[A-Z][A-Za-z0-9.]{2,}/.test(b) && !/[$€£%]|\d/.test(b) && !/^[A-Z]{2,5}$/.test(b) && !GENERIC_EN.test(b))
      .filter((b) => !corpus.includes(squash(b)) && !b.split(/\s+/).some((w) => w.length >= 4 && /^[A-Z]/.test(w) && corpus.includes(squash(w)))),
  };
}

/**
 * Ответ «постами»: каждый блок, начинающийся с эмодзи, — в цитатную плашку
 * Telegram (<blockquote>: подложка и полоса слева), заголовок блока — отдельной
 * жирной строкой. Карточек внутри одного сообщения Telegram не умеет, плашки —
 * самое близкое к ленте мини-приложения (просьба владельца 2026-10-01).
 */
const EMOJI_START = /^\s*(?:\p{Extended_Pictographic}|\p{Regional_Indicator})/u;
function cardify(html) {
  const out = [], block = [];
  const flush = () => {
    if (!block.length) return;
    // «💰 <b>Деньги</b> — текст» -> заголовок строкой, текст под ним.
    const first = block[0].replace(/^(\s*\S+\s+<b>[^<]{1,60}<\/b>)\s*[—–:-]\s*/u, "$1\n");
    out.push(`<blockquote>${[first, ...block.slice(1)].join("\n").trim()}</blockquote>`);
    block.length = 0;
  };
  for (const line of String(html || "").split("\n")) {
    if (!line.trim()) { flush(); continue; }
    if (EMOJI_START.test(line)) { flush(); block.push(line); continue; }
    if (block.length) block.push(line);
    else out.push(line);
  }
  flush();
  return out.join("\n\n").replace(/\n{3,}/g, "\n\n");
}

const SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹";
const sup = (n) => String(n).split("").map((d) => SUP[Number(d)]).join("");

function groundAnswer(text, facts, extra = "") {
  const byId = Object.fromEntries(facts.map((f) => [f.id, f]));
  const order = new Map();
  const chk = makeChecker(facts, extra);
  const unsupported = [];
  let dropped = 0;
  const lines = [];
  // Ссылка — одиночная [F6] или группа [F6, F7] / [F6-F8]: групповые раньше
  // не распознавались, и строки с опорой вырезались как непроверенные.
  const GROUP = /\[(F\d+(?:\s*[,;–-]\s*F?\d+)*)\]/g;
  const idsOf = (g) => {
    const out = [];
    for (const part of g.split(/\s*[,;]\s*/)) {
      const m = /F?(\d+)(?:\s*[–-]\s*F?(\d+))?/.exec(part);
      if (!m) continue;
      const a = Number(m[1]), b = m[2] ? Number(m[2]) : a;
      for (let i = a; i <= Math.min(b, a + 20); i++) out.push(i);
    }
    return out;
  };
  for (const raw of String(text || "").split("\n")) {
    const cites = [...raw.matchAll(GROUP)].flatMap((m) => idsOf(m[1]));
    const valid = cites.filter((c) => byId[c]);
    const hasNumbers = /\$\s?\d|\d+\s?(млн|млрд|million|billion|M\b|B\b|%|раунд|round)/i.test(raw);
    if (hasNumbers && !valid.length) { dropped++; continue; }
    let line = esc(raw).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*\*/g, "");
    // Строка со ссылками: числа и названия должны найтись в фактах.
    if (valid.length) {
      const plain = raw.replace(GROUP, " ");
      const nums = chk.badNumbers(plain), names = chk.badNames(line.replace(GROUP, " "));
      if (nums.length || names.length) { dropped++; unsupported.push({ line: plain.slice(0, 200), nums, names }); continue; }
    }
    // Сноска — маленький номер по порядку появления (¹ ² ³), а не [17]:
    // в Telegram крупные «[4]» среди текста читались хуже самого текста.
    line = line.replace(/\s*\[(F\d+(?:\s*[,;–-]\s*F?\d+)*)\]/g, (m) => {
      const g = /\[(.+)\]/.exec(m)[1];
      const links = [...new Set(idsOf(g))].map((n) => {
        const f = byId[n];
        if (!(f && f.url && okUrl(f.url))) return "";
        if (!order.has(f.url)) order.set(f.url, order.size + 1);
        return `<a href="${esc(f.url)}">${sup(order.get(f.url))}</a>`;
      }).filter(Boolean);
      return links.length ? " " + links.join(" ") : "";
    });
    // Больше трёх сносок подряд — шум («¹ ² … ¹⁶» в одной строке, 2026-10-01).
    line = line.replace(/((?:\s*<a href="[^"]+">[^<]+<\/a>){3})(?:\s*<a href="[^"]+">[^<]+<\/a>)+/g, "$1");
    // Одна и та же сноска подряд («³ ³») — один раз.
    line = line.replace(/(<a href="([^"]+)">[^<]+<\/a>)(?:\s*<a href="\2">[^<]+<\/a>)+/g, "$1");
    lines.push(line.replace(/\s+$/, ""));
  }
  return { html: lines.join("\n").replace(/\n{3,}/g, "\n\n").trim(), dropped, unsupported };
}

/** Поиск в сети: статус в чате на время поиска, потом он удаляется. */
async function webSearchWithStatus(env, chatId, statusText, subject, examples, meter = null) {
  const st = await tg(env, "sendMessage", { chat_id: chatId, text: statusText });
  let data = null;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 22000);
  try {
    // Perplexity sonar: ~11 с и ссылки на первоисточники; gemini с поиском
    // висел 20-33 с и обрывал JSON (замер 2026-10-01).
    const model = env.LS_WEB_MODEL || WEB_SOURCE_MODEL;
    const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST", signal: ctrl.signal,
      headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
        "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
      body: JSON.stringify({ model, max_tokens: 2500, temperature: 0.2, usage: { include: true }, messages: [
        { role: "system", content: WEB_SYSTEM },
        { role: "user", content: `Niche or idea: ${subject}\nKnown funded companies: ${examples.join("; ")}` }] }),
    });
    if (r.ok) {
      const j = await r.json();
      if (meter && j.usage && j.usage.cost) meter.usd += Number(j.usage.cost) || 0;
      const t = (j.choices[0].message.content || "").trim();
      data = JSON.parse(t.slice(t.indexOf("{"), t.lastIndexOf("}") + 1));
    } else {
      await noteAiError(env, "openrouter web", r.status, await r.text().catch(() => ""));
    }
  } catch (e) {
    await noteAiError(env, "openrouter web", 0, String(e));
  } finally {
    clearTimeout(timer);
  }
  if (st && st.ok) await tg(env, "deleteMessage", { chat_id: chatId, message_id: st.result.message_id });
  return data && typeof data === "object" ? data : null;
}

const WEB_SYSTEM = `You research a startup niche or idea on the web for founders (Kazakhstan and CIS first, then global).
Find real products that already do this: global leaders and players in Kazakhstan/CIS, with the price if published, and what customers complain about.
Reply with JSON only: {"competitors": [{"name": "...", "url": "https://...", "market": "global|US|EU|KZ|RU|CIS|MENA", "price": "...", "note": "one line"}], "complaints": [{"text": "...", "source": "https://..."}], "pricing": "one line", "icp": "one line"}
At most 8 competitors and 5 complaints, only ones you actually found with real URLs. Never invent.`;

// ---------------------------------------------------------------------------
// 💳 LS — внутренняя валюта запросов (решение владельца 2026-10-01)
//
// Наружу — фиксированная цена действия в LS; внутри 1 LS ≈ $0,001 нашей
// себестоимости, курс не показывается. Подписочные LS живут один расчётный
// месяц и сгорают при продлении; докупленные credits не сгорают и тратятся
// после подписочных, в том числе на Free. Фактическая стоимость каждого
// действия (OpenRouter отдаёт cost) пишется в ls_log — по ней сверяем прайс.
// ---------------------------------------------------------------------------
const PLANS = {
  free: { ls: 300, usd: 0 },
  pro: { ls: 1500, usd: 3.99 },
  max: { ls: 3500, usd: 9.99 },
  promax: { ls: 7000, usd: 19.99 },
};
const LS_PRICE = { chat: 10, live: 40, live_up: 30, deep: 20, map: 20, comp: 20, check: 30, research: 15, idea: 10 };
const LS_PERIOD = 30 * 86400;
const CREDIT_PACK = { ls: 1000, usd: 3.49 };

async function lsGet(env, uid) {
  const now = Math.floor(Date.now() / 1000);
  let r = await env.DB.prepare("SELECT * FROM ls_balance WHERE user_id = ?1").bind(String(uid)).first().catch(() => null);
  r = r ? { ...r } : { user_id: String(uid), plan: "free", paid_until: 0, period_end: 0, sub_ls: 0, credits: 0, warned: 0 };
  if (now >= (r.period_end || 0)) {
    // Новый расчётный месяц: подписочные LS — заново, credits остаются.
    if (r.plan !== "free" && now >= (r.paid_until || 0)) r.plan = "free";
    r.sub_ls = (PLANS[r.plan] || PLANS.free).ls;
    r.period_end = now + LS_PERIOD;
    r.warned = 0;
    await lsSave(env, r);
  }
  return r;
}

async function lsSave(env, r) {
  await env.DB.prepare("INSERT OR REPLACE INTO ls_balance (user_id, plan, paid_until, period_end, sub_ls, credits, warned, ts) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)")
    .bind(String(r.user_id), r.plan, r.paid_until || 0, r.period_end, r.sub_ls, r.credits, r.warned || 0, Math.floor(Date.now() / 1000)).run();
}

const lsFree = (env, uid) => isOwner(env, uid) || String(uid) === "debug";   // владелец и отладка — без списаний

async function lsCanAfford(env, uid, action) {
  if (lsFree(env, uid)) return true;
  const r = await lsGet(env, uid);
  return r.sub_ls + r.credits >= (LS_PRICE[action] || 10);
}

/** Списать за действие: сначала подписочные, потом credits. {spent, left, warn}. */
async function lsSpend(env, uid, action, costUsd = 0) {
  const price = LS_PRICE[action] || 10;
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("INSERT INTO ls_log (user_id, ts, action, ls, cost_usd) VALUES (?1, ?2, ?3, ?4, ?5)")
    .bind(String(uid), now, action, price, Math.round((costUsd || 0) * 1e6) / 1e6).run().catch(() => null);
  if (lsFree(env, uid)) return { spent: price, left: null, owner: true };
  const r = await lsGet(env, uid);
  const fromSub = Math.min(r.sub_ls, price);
  r.sub_ls -= fromSub;
  r.credits = Math.max(0, r.credits - (price - fromSub));
  const left = r.sub_ls + r.credits;
  const plan = PLANS[r.plan] || PLANS.free;
  const warn = !r.warned && left < plan.ls * 0.1;
  if (warn) r.warned = 1;
  await lsSave(env, r);
  return { spent: price, left, warn };
}

/** Строка под ответом: сколько списано и сколько осталось. */
function lsFooter(s, res) {
  if (!res) return "";
  return res.owner ? `\n\n<i>−${res.spent} LS</i>` : `\n\n<i>${fmt(s.ls_footer, res.spent, res.left)}</i>`;
}

function lsTariffs(s) {
  const row = (k) => fmt(s["ls_plan_" + k], PLANS[k].usd.toFixed(2), PLANS[k].ls.toLocaleString("ru-RU").replace(/,/g, " ")) + (STAR_ITEMS[k] ? ` · ${STAR_ITEMS[k].stars} ⭐` : "");
  return [s.ls_tariffs_title, row("free"), row("pro"), row("max"), row("promax"), "",
    fmt(s.ls_pack, CREDIT_PACK.ls.toLocaleString("ru-RU").replace(/,/g, " "), CREDIT_PACK.usd.toFixed(2)) + ` · ${STAR_ITEMS.pack.stars} ⭐`, "",
    s.ls_prices, s.ls_pay_soon].join("\n");
}

async function lsBalanceMsg(env, chatId, lang) {
  const s = L(lang);
  const r = await lsGet(env, chatId);
  const text = [fmt(s.ls_balance, s["ls_name_" + r.plan] || r.plan, r.sub_ls, r.credits,
    new Date(r.period_end * 1000).toISOString().slice(0, 10)), "", lsTariffs(s)].join("\n");
  await tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", reply_markup: starsButtons(s) });
}

async function lsShortMsg(env, chatId, lang, action) {
  const s = L(lang);
  const r = await lsGet(env, chatId);
  await tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML",
    text: fmt(s.ls_short, LS_PRICE[action] || 10, r.sub_ls + r.credits) + "\n\n" + lsTariffs(s), reply_markup: starsButtons(s) });
}

/** Владелец: /grant <id> <free|pro|max|promax>, /credit <id> <LS>, /costs — сверка прайса с фактом. */
async function lsAdmin(env, chatId, text) {
  const [cmd, uid, arg] = text.trim().split(/\s+/);
  if (cmd === "/grant" && uid && PLANS[arg]) {
    const r = await lsGet(env, uid);
    const now = Math.floor(Date.now() / 1000);
    Object.assign(r, { plan: arg, paid_until: arg === "free" ? 0 : now + LS_PERIOD, period_end: now + LS_PERIOD, sub_ls: PLANS[arg].ls, warned: 0 });
    await lsSave(env, r);
    return tg(env, "sendMessage", { chat_id: chatId, text: `✅ ${uid}: ${arg}, ${PLANS[arg].ls} LS до ${new Date(r.period_end * 1000).toISOString().slice(0, 10)}` });
  }
  if (cmd === "/credit" && uid && Number(arg)) {
    const r = await lsGet(env, uid);
    r.credits = Math.max(0, r.credits + Math.round(Number(arg)));
    await lsSave(env, r);
    return tg(env, "sendMessage", { chat_id: chatId, text: `✅ ${uid}: credits ${r.credits}` });
  }
  if (cmd === "/costs") {
    const since = Math.floor(Date.now() / 1000) - 7 * 86400;
    const { results } = await env.DB.prepare("SELECT action, COUNT(*) n, SUM(ls) ls, SUM(cost_usd) usd, COUNT(DISTINCT user_id) users FROM ls_log WHERE ts >= ?1 GROUP BY action ORDER BY usd DESC")
      .bind(since).all().catch(() => ({ results: [] }));
    const lines = ["💳 Факт за 7 дней (цена LS против себестоимости; 1 LS = $0,001):"];
    let tl = 0, tu = 0;
    for (const x of results || []) {
      tl += x.ls || 0; tu += x.usd || 0;
      lines.push(`${x.action}: ${x.n} шт., ${x.users} чел. — ${x.ls} LS, факт $${(x.usd || 0).toFixed(3)} (в среднем $${((x.usd || 0) / x.n).toFixed(4)} при цене $${(LS_PRICE[x.action] / 1000).toFixed(3)})`);
    }
    lines.push(`Итого: ${tl} LS ($${(tl / 1000).toFixed(2)} по прайсу), факт $${tu.toFixed(2)}`);
    return tg(env, "sendMessage", { chat_id: chatId, text: lines.join("\n") });
  }
  return tg(env, "sendMessage", { chat_id: chatId, text: "/grant <id> <free|pro|max|promax> · /credit <id> <LS> · /costs" });
}

// ---------------------------------------------------------------------------
// ⭐ Оплата Telegram Stars (2026-10-01)
//
// Подписки — createInvoiceLink с subscription_period 30 дней (Telegram сам
// продлевает и присылает successful_payment каждый месяц), пакет LS — разовый
// счёт. Цена в звёздах ≈ цене в $ при выплате ~$0,013 за звезду через Fragment;
// покупатель с телефона платит за звёзды дороже из-за комиссии App Store / Google.
// ---------------------------------------------------------------------------
const STAR_ITEMS = {
  pro: { stars: 300, plan: "pro", sub: true },
  max: { stars: 750, plan: "max", sub: true },
  promax: { stars: 1500, plan: "promax", sub: true },
  pack: { stars: 270, ls: 1000, sub: false },
};

async function starsLink(env, uid, item, lang) {
  const s = L(lang);
  const it = STAR_ITEMS[item];
  if (!it) return null;
  const title = it.sub ? fmt(s.st_title_sub, s["ls_name_" + it.plan]) : fmt(s.st_title_pack, it.ls);
  const descr = it.sub ? fmt(s.st_descr_sub, PLANS[it.plan].ls) : s.st_descr_pack;
  const r = await tg(env, "createInvoiceLink", {
    title: title.slice(0, 32), description: descr.slice(0, 255), payload: `${item}:${uid}`, currency: "XTR", provider_token: "",
    prices: [{ label: title.slice(0, 32), amount: it.stars }], ...(it.sub ? { subscription_period: 2592000 } : {}),
  });
  return r && r.ok ? r.result : null;
}

function starsButtons(s) {
  return { inline_keyboard: [
    [{ text: `⭐ Pro — ${STAR_ITEMS.pro.stars}`, callback_data: "buy:pro" }, { text: `⭐ Max — ${STAR_ITEMS.max.stars}`, callback_data: "buy:max" }],
    [{ text: `⭐ Pro Max — ${STAR_ITEMS.promax.stars}`, callback_data: "buy:promax" }, { text: fmt(s.st_pack_btn, STAR_ITEMS.pack.stars), callback_data: "buy:pack" }],
  ] };
}

/** Проверка перед списанием звёзд: товар существует и счёт выставлен этому человеку. */
async function starsPreCheckout(env, q) {
  const [item, uid] = String(q.invoice_payload || "").split(":");
  const ok = !!STAR_ITEMS[item] && String(uid) === String(q.from.id) && q.currency === "XTR" && q.total_amount === STAR_ITEMS[item].stars;
  await tg(env, "answerPreCheckoutQuery", { pre_checkout_query_id: q.id, ok, ...(ok ? {} : { error_message: "Счёт устарел — откройте /balance и оплатите заново." }) });
}

/** Оплата прошла: тариф или пакет LS, запись в payments. */
async function starsPaid(env, chatId, msg, lang) {
  const p = msg.successful_payment;
  const s = L(lang);
  const [item] = String(p.invoice_payload || "").split(":");
  const it = STAR_ITEMS[item];
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("INSERT OR IGNORE INTO payments (charge_id, user_id, ts, item, stars, sub_exp, recurring) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
    .bind(p.telegram_payment_charge_id, String(chatId), now, item || "?", p.total_amount, p.subscription_expiration_date || 0, p.is_recurring ? 1 : 0).run();
  await ownerNotify(env, { text: `💰 Оплата: ${item} — ${p.total_amount} ⭐${p.is_recurring && !p.is_first_recurring ? " (продление)" : ""} от ${chatId}` });
  if (!it) return;
  const r = await lsGet(env, chatId);
  if (it.sub) {
    // Новый месяц подписки: лимит тарифа заново, докупленные LS не трогаем.
    Object.assign(r, { plan: it.plan, paid_until: p.subscription_expiration_date || now + LS_PERIOD, period_end: p.subscription_expiration_date || now + LS_PERIOD,
      sub_ls: PLANS[it.plan].ls, warned: 0 });
    await lsSave(env, r);
    // Перешёл на другой тариф — старую подписку не продлеваем, чтобы не платил дважды.
    if (!p.is_recurring || p.is_first_recurring) {
      const { results } = await env.DB.prepare("SELECT charge_id FROM payments WHERE user_id = ?1 AND item != ?2 AND item IN ('pro','max','promax') AND sub_exp > ?3")
        .bind(String(chatId), item, now).all().catch(() => ({ results: [] }));
      for (const old of results || []) {
        await tg(env, "editUserStarSubscription", { user_id: Number(chatId), telegram_payment_charge_id: old.charge_id, is_canceled: true });
      }
    }
    await tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", text: fmt(s.st_ok_sub, s["ls_name_" + it.plan], PLANS[it.plan].ls,
      new Date(r.period_end * 1000).toISOString().slice(0, 10)) });
  } else {
    r.credits += it.ls;
    await lsSave(env, r);
    await tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", text: fmt(s.st_ok_pack, it.ls, r.credits) });
  }
}

// ---------------------------------------------------------------------------
// 📊 Дашборд владельца (мини-приложение, вкладка «Админ»)
//
// Выручка — по оплатам звёздами (выплата ≈ STAR_USD за звезду через
// Fragment), затраты — реальный расход OpenRouter по его API (бот и пайплайн
// на одном аккаунте) плюс комиссия пополнения: OpenRouter 5–5,5% и карта
// Bybit — у владельца $10 кредитов стоили $11,02 (2026-10-01).
// ---------------------------------------------------------------------------
const STAR_USD = 0.013;          // примерная выплата за звезду; уточнять по факту вывода
const TOPUP_FEE = 0.102;         // наценка пополнения OpenRouter: 11,02 / 10 − 1
const FIXED_USD_MONTH = 0;       // Cloudflare и Actions сейчас бесплатны
const NOT_USERS = new Set(["debug", "lstest1"]);

async function orCredits(env) {
  if (!env.LS_OPENROUTER_KEY) return null;
  const r = await fetch("https://openrouter.ai/api/v1/credits", { headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}` } }).catch(() => null);
  if (!r || !r.ok) return null;
  const d = ((await r.json().catch(() => ({}))) || {}).data || {};
  return { total: Number(d.total_credits) || 0, used: Number(d.total_usage) || 0 };
}

/** Отметка расхода OpenRouter на начало месяца — от неё считается расход месяца. */
async function orMonthMark(env, credits) {
  const month = new Date().toISOString().slice(0, 7);
  const key = "or_used_" + month;
  let mark = await meta(env, key);
  if (!mark && credits) { mark = JSON.stringify({ used: credits.used, ts: Math.floor(Date.now() / 1000) }); await setMeta(env, key, mark); }
  try { return JSON.parse(mark || "null"); } catch { return null; }
}

async function adminStats(env) {
  const now = Math.floor(Date.now() / 1000);
  const d0 = new Date(); d0.setUTCDate(1); d0.setUTCHours(0, 0, 0, 0);
  const monthStart = Math.floor(d0.getTime() / 1000);
  const owners = (env.LS_BOT_ALLOW || "").split(",").map((x) => x.trim()).filter(Boolean);
  const skip = [...NOT_USERS, ...owners];
  const notIn = `user_id NOT IN (${skip.map((_, i) => "?" + (i + 1)).join(",")})`;
  const q = (sql, ...extra) => env.DB.prepare(sql).bind(...skip, ...extra);
  const one = async (sql, ...extra) => (await q(sql, ...extra).first().catch(() => null)) || {};
  const all = async (sql, ...extra) => ((await q(sql, ...extra).all().catch(() => ({ results: [] }))).results) || [];
  const n = skip.length;

  const users = await one(`SELECT COUNT(*) n FROM users_seen WHERE ${notIn}`);
  const new7 = await one(`SELECT COUNT(*) n FROM users_seen WHERE ${notIn} AND ts >= ?${n + 1}`, now - 7 * 86400);
  const act7 = await one(`SELECT COUNT(DISTINCT user_id) n FROM ls_log WHERE ${notIn} AND ts >= ?${n + 1}`, now - 7 * 86400);
  const act30 = await one(`SELECT COUNT(DISTINCT user_id) n FROM ls_log WHERE ${notIn} AND ts >= ?${n + 1}`, now - 30 * 86400);
  const plans = await all(`SELECT plan, COUNT(*) n FROM ls_balance WHERE ${notIn} AND plan != 'free' AND paid_until > ?${n + 1} GROUP BY plan`, now);
  const paid = plans.reduce((a, x) => a + x.n, 0);
  const payM = await one(`SELECT COUNT(*) n, COALESCE(SUM(stars),0) stars FROM payments WHERE ${notIn} AND ts >= ?${n + 1}`, monthStart);
  const payAll = await one(`SELECT COUNT(*) n, COALESCE(SUM(stars),0) stars, COUNT(DISTINCT user_id) payers FROM payments WHERE ${notIn}`);
  const lsM = await all(`SELECT action, COUNT(*) n, SUM(ls) ls, SUM(cost_usd) usd FROM ls_log WHERE ${notIn} AND ts >= ?${n + 1} GROUP BY action ORDER BY n DESC`, monthStart);
  const daily = await all(`SELECT CAST((ts - ?${n + 1}) / 86400 AS INTEGER) d, COUNT(DISTINCT user_id) users, COUNT(*) actions, SUM(cost_usd) usd FROM ls_log WHERE ${notIn} AND ts >= ?${n + 1} GROUP BY d`, now - 30 * 86400);
  const dailyNew = await all(`SELECT CAST((ts - ?${n + 1}) / 86400 AS INTEGER) d, COUNT(*) n FROM users_seen WHERE ${notIn} AND ts >= ?${n + 1} GROUP BY d`, now - 30 * 86400);
  const dailyPay = await all(`SELECT CAST((ts - ?${n + 1}) / 86400 AS INTEGER) d, SUM(stars) stars FROM payments WHERE ${notIn} AND ts >= ?${n + 1} GROUP BY d`, now - 30 * 86400);
  // Подписки, не продлённые после окончания, за 30 дней — отток.
  const churn = await one(`SELECT COUNT(DISTINCT p.user_id) n FROM payments p WHERE p.${notIn.replace("user_id", "user_id")} AND p.sub_exp > 0 AND p.sub_exp BETWEEN ?${n + 1} AND ?${n + 2}
    AND NOT EXISTS (SELECT 1 FROM payments x WHERE x.user_id = p.user_id AND x.ts > p.ts)`, now - 30 * 86400, now);

  const credits = await orCredits(env);
  const mark = await orMonthMark(env, credits);
  const aiMonth = credits && mark ? Math.max(0, credits.used - mark.used) : lsM.reduce((a, x) => a + (x.usd || 0), 0);
  const costMonth = aiMonth * (1 + TOPUP_FEE) + FIXED_USD_MONTH;
  const revenueMonth = payM.stars * STAR_USD;
  const bal = await tg(env, "getMyStarBalance", {});
  const series = (rows, key) => { const a = Array(30).fill(0); for (const r of rows) if (r.d >= 0 && r.d < 30) a[r.d] = Math.round((r[key] || 0) * 1000) / 1000; return a; };
  return {
    users: users.n || 0, new7: new7.n || 0, active7: act7.n || 0, active30: act30.n || 0,
    paid, free: Math.max(0, (users.n || 0) - paid), plans: Object.fromEntries(plans.map((x) => [x.plan, x.n])),
    conversion: users.n ? Math.round(paid / users.n * 1000) / 10 : 0,
    revenue_month: Math.round(revenueMonth * 100) / 100, stars_month: payM.stars, payments_month: payM.n,
    revenue_all: Math.round(payAll.stars * STAR_USD * 100) / 100, payers_all: payAll.payers || 0,
    cost_month: Math.round(costMonth * 100) / 100, ai_month: Math.round(aiMonth * 100) / 100, cost_since: mark ? mark.ts : null,
    profit_month: Math.round((revenueMonth - costMonth) * 100) / 100,
    arppu: paid ? Math.round(revenueMonth / paid * 100) / 100 : 0, churn30: churn.n || 0,
    or_balance: credits ? Math.round((credits.total - credits.used) * 100) / 100 : null,
    stars_balance: bal && bal.ok ? bal.result.amount : null,
    actions: lsM.map((x) => ({ action: x.action, n: x.n, ls: x.ls, usd: Math.round((x.usd || 0) * 10000) / 10000 })),
    daily: { users: series(daily, "users"), actions: series(daily, "actions"), cost: series(daily, "usd"), new: series(dailyNew, "n"), stars: series(dailyPay, "stars") },
    assumptions: { star_usd: STAR_USD, topup_fee: TOPUP_FEE, fixed: FIXED_USD_MONTH },
  };
}

// ---------------------------------------------------------------------------
// 🛠 Два бота (решение владельца 2026-10-01)
//
// Launch Scout — публичный бот (секрет LS_PUBLIC_BOT_TOKEN). Прежний токен
// (LS_BOT_TOKEN) становится служебным «Dashboard Launch Scout»: только
// владелец, уведомления, отчёты, вопросы ИИ о делах и мини-приложение с
// дашбордом. Пока LS_PUBLIC_BOT_TOKEN не задан, всё работает как раньше —
// одним ботом.
// ---------------------------------------------------------------------------
function botEnv(env) {
  if (!env.LS_PUBLIC_BOT_TOKEN || env.LS_ADMIN_TOKEN) return env;
  // Object.create: привязки (DB, AI, VEC, SNAP) остаются доступны через прототип.
  return Object.create(env, {
    LS_BOT_TOKEN: { value: env.LS_PUBLIC_BOT_TOKEN, enumerable: true },
    LS_ADMIN_TOKEN: { value: env.LS_BOT_TOKEN, enumerable: true },
  });
}
const adminEnv = (env) => (env.LS_ADMIN_TOKEN ? Object.create(env, { LS_BOT_TOKEN: { value: env.LS_ADMIN_TOKEN } }) : env);
const owners = (env) => (env.LS_BOT_ALLOW || "").split(",").map((x) => x.trim()).filter(Boolean);

/** Служебное сообщение владельцу — в служебного бота, если он есть. */
async function ownerNotify(env, payload) {
  for (const o of owners(env)) await tg(adminEnv(env), "sendMessage", { chat_id: o, disable_web_page_preview: true, ...payload });
}

function adminReport(d) {
  const usd = (v) => (v < 0 ? "−$" : "$") + Math.abs(v).toFixed(2);
  const plans = Object.entries(d.plans || {}).map(([k, v]) => `${k} ${v}`).join(", ") || "—";
  return [
    "📊 <b>Launch Scout — сводка</b>",
    "",
    `👥 Пользователей: <b>${d.users}</b> (новых за 7 дн: ${d.new7})`,
    `🔥 Активных: ${d.active7} за 7 дн · ${d.active30} за 30 дн`,
    `💳 Платных: <b>${d.paid}</b> (${plans}) · доля ${d.conversion}% · не продлили за 30 дн: ${d.churn30}`,
    "",
    `💰 Выручка за месяц: <b>${usd(d.revenue_month)}</b> (${d.stars_month} ⭐, оплат ${d.payments_month})`,
    `🧾 Затраты: ${usd(d.cost_month)} (ИИ ${usd(d.ai_month)} + комиссии)`,
    `📈 Чистая прибыль: <b>${usd(d.profit_month)}</b>`,
    `⭐ Баланс звёзд: ${d.stars_balance ?? "—"} · OpenRouter: ${d.or_balance == null ? "—" : usd(d.or_balance)}${d.or_balance != null && d.or_balance < 5 ? " ⚠️" : ""}`,
  ].join("\n");
}

const ADMIN_SYSTEM = `You are the business analyst for the owner of Launch Scout, a paid Telegram market-radar bot. Answer the owner's question about how the business is doing using ONLY the STATS JSON (users, activity, plans, revenue in USD and Stars, costs, profit, balances, daily series for the last 30 days — index 0 is 30 days ago, the last item is today) and the RECENT lists. Never invent numbers. Be brief: 3-8 lines, key numbers in <b>bold</b> (Telegram HTML, no markdown), then one practical suggestion if it is useful. Answer in Russian unless asked otherwise.`;

async function adminAsk(env, chatId, question) {
  const aenv = adminEnv(env);
  await tg(aenv, "sendChatAction", { chat_id: chatId, action: "typing" });
  const d = await adminStats(env);
  const owns = owners(env);
  const skip = owns.length ? ` WHERE user_id NOT IN (${owns.map((_, i) => "?" + (i + 1)).join(",")})` : "";
  const recentUsers = ((await env.DB.prepare(`SELECT user_id, ts FROM users_seen${skip} ORDER BY ts DESC LIMIT 15`).bind(...owns).all().catch(() => ({}))).results) || [];
  const recentPays = ((await env.DB.prepare(`SELECT user_id, ts, item, stars, recurring FROM payments${skip} ORDER BY ts DESC LIMIT 15`).bind(...owns).all().catch(() => ({}))).results) || [];
  const iso = (t) => new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ");
  const ctx = `TODAY: ${new Date().toISOString().slice(0, 10)}\nSTATS: ${JSON.stringify(d)}\nRECENT USERS (first seen): ${recentUsers.map((u) => `${u.user_id} ${iso(u.ts)}`).join("; ") || "none"}\nRECENT PAYMENTS: ${recentPays.map((p) => `${p.user_id} ${iso(p.ts)} ${p.item} ${p.stars}⭐${p.recurring ? " renewal" : ""}`).join("; ") || "none"}`;
  const messages = [{ role: "system", content: ADMIN_SYSTEM }, { role: "user", content: `${ctx}\n\nQUESTION: ${question.slice(0, 1000)}` }];
  let text = await streamOpenRouter(env, env.LS_FAST_MODEL || FAST_MODEL, messages, async () => {}, 25000, 900, 8000);
  if (!text) text = await streamOpenRouter(env, FALLBACK_FAST_MODEL, messages, async () => {}, 20000, 900, 8000);
  const html = (text || "").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").trim() || "ИИ сейчас не ответил — вот сводка:\n\n" + adminReport(d);
  const r = await tg(aenv, "sendMessage", { chat_id: chatId, text: html.slice(0, 3900), parse_mode: "HTML", reply_markup: adminKb() });
  if (!(r && r.ok)) await tg(aenv, "sendMessage", { chat_id: chatId, text: html.replace(/<[^>]+>/g, "").slice(0, 3900), reply_markup: adminKb() });
}

const adminKb = () => ({ inline_keyboard: [[{ text: "📊 Дашборд", web_app: { url: APP_URL + "#admin" } }, { text: "📋 Сводка", callback_data: "a:report" }]] });

/** Апдейты служебного бота: только владелец. */
async function handleAdminUpdate(env, update) {
  const aenv = adminEnv(env);
  const msg = update.message;
  const cb = update.callback_query;
  const chatId = msg ? msg.chat.id : cb ? cb.message.chat.id : null;
  if (!chatId) return;
  if (cb) await tg(aenv, "answerCallbackQuery", { callback_query_id: cb.id });
  if (!isOwner(env, chatId)) {
    if (msg) await tg(aenv, "sendMessage", { chat_id: chatId, text: "Это служебный бот Launch Scout." });
    return;
  }
  await ensureTables(env);
  const raw = msg ? (msg.text || "").trim() : "";
  const text = raw.toLowerCase();
  const data = cb ? cb.data || "" : "";
  if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(aenv, "sendMessage", { chat_id: chatId, parse_mode: "HTML", reply_markup: adminKb(),
      text: "🛠 <b>Dashboard Launch Scout</b>\n\nСюда приходят служебные уведомления: сбор молчит, токены, баланс OpenRouter, оплаты, новые пользователи, /paysupport.\n\n" +
        "/report — сводка · /costs — себестоимость действий за неделю\n/grant &lt;id&gt; &lt;free|pro|max|promax&gt; · /credit &lt;id&gt; &lt;LS&gt; · /refund &lt;id&gt; &lt;charge_id&gt;\n\nИли спросите текстом/голосом: «как дела за неделю?», «сколько новых пользователей?»." });
    return;
  }
  if (data === "a:report" || text.startsWith("/report")) {
    await tg(aenv, "sendMessage", { chat_id: chatId, text: adminReport(await adminStats(env)), parse_mode: "HTML", reply_markup: adminKb() });
    return;
  }
  if (/^\/(grant|credit|costs)\b/.test(text)) {
    await lsAdmin(aenv, chatId, text);
    return;
  }
  if (/^\/refund\b/.test(text)) {
    const [, uid, charge] = raw.split(/\s+/);
    // Оплата была в публичном боте — возвращает он, ответ — сюда.
    const r = await tg(env, "refundStarPayment", { user_id: Number(uid), telegram_payment_charge_id: charge });
    await tg(aenv, "sendMessage", { chat_id: chatId, text: r && r.ok ? `✅ возврат ${uid} ${charge}` : `не вышло: ${JSON.stringify(r).slice(0, 200)}` });
    return;
  }
  let question = raw;
  if (msg && msg.voice) {
    question = (await transcribe(aenv, msg.voice)) || "";
    if (!question) { await tg(aenv, "sendMessage", { chat_id: chatId, text: "Не расслышал — попробуйте ещё раз." }); return; }
  }
  if (question) await adminAsk(env, chatId, question);
}

/** Настройка ботов (имя, аватар, вебхук, меню) — из отладки, токены берутся из секретов. */
async function setupBot(env, which, avatar) {
  const isAdmin = which === "admin";
  const benv = isAdmin ? adminEnv(env) : env;
  const base = "https://launch-scout-bot.clam83574.workers.dev";
  const out = {};
  out.webhook = await tg(benv, "setWebhook", { url: base + (isAdmin ? "/tg-admin" : "/tg"), secret_token: env.LS_WEBHOOK_SECRET,
    allowed_updates: ["message", "callback_query", "pre_checkout_query", "edited_message"] });
  out.name = await tg(benv, "setMyName", { name: isAdmin ? "Dashboard Launch Scout" : "Launch Scout" });
  out.menu = await tg(benv, "setChatMenuButton", { menu_button: { type: "web_app", text: isAdmin ? "Дашборд" : "Приложение", web_app: { url: APP_URL + (isAdmin ? "#admin" : "") } } });
  if (isAdmin) {
    out.commands = await tg(benv, "setMyCommands", { commands: [
      { command: "report", description: "📋 Сводка" }, { command: "costs", description: "🧾 Себестоимость за неделю" },
      { command: "grant", description: "Выдать тариф: /grant id pro" }, { command: "credit", description: "Начислить LS: /credit id 1000" },
      { command: "refund", description: "Возврат: /refund id charge_id" }] });
    for (const l of ["en", "kk"]) await tg(benv, "deleteMyCommands", { language_code: l });
  }
  if (avatar && avatar.byteLength) {
    const fd = new FormData();
    fd.append("photo", JSON.stringify({ type: "static", photo: "attach://avatar" }));
    fd.append("avatar", new Blob([avatar], { type: "image/png" }), "avatar.png");
    out.photo = await fetch(`https://api.telegram.org/bot${benv.LS_BOT_TOKEN}/setMyProfilePhoto`, { method: "POST", body: fd }).then((r) => r.json()).catch((e) => ({ error: String(e) }));
  }
  const me = await tg(benv, "getMe", {});
  out.me = me && me.result ? { username: me.result.username, name: me.result.first_name } : me;
  return out;
}

let lastUnsupported = null;   // для /debug-chat
const INVESTOR_Q = /инвест|инвестор|фонд|венчур|ангел|\bvc\b|investor|\bfunds?\b|backer|кто вкладыва|кто финансир/iu;

// ---------------------------------------------------------------------------
// 🔎 Живой поиск по запросу
//
// «Нет данных» на вопрос о конкретной компании или фонде — провал (журнал
// чата 2026-09-30: NACE AI, топ инвесторов). Быстрая модель по вопросу решает,
// нужен ли живой поиск, и даёт фразы; источники идут параллельно, статус в
// чате показывает ход и удаляется, когда ответ готов. Из Cloudflare доступны
// HN, GitHub и веб-поиск OpenRouter; Google News отвечает 503, Reddit — 403,
// а RSS Bing разрешён только для некоммерческого использования. X и Google
// Trends — через глубокий поиск в GitHub Actions (researchDispatch).
// ---------------------------------------------------------------------------
const PLAN_SYSTEM = `You plan a live search for a market radar for startup founders. Reply JSON only:
{"live": true|false, "queries": ["English search phrase, 2-5 words", "... at most 3"], "terms": ["1-3 word phrase people type into Google about this topic, at most 3"], "entities": ["company, product, fund or person names from the question"]}
live = true if the question names a specific company, product, fund, person or event, asks for news or anything recent, or asks something a database of startup funding rounds and niches would not answer (e.g. how a product works, user numbers, pricing). Otherwise false.
Questions like "which investors are most active", "where does the money go", "which niches are hot" are answered by the rounds database: live = false. Search phrases must not contain years unless the user gave one.`;

/**
 * Названия из вопроса без модели: «проект с названием Канго», «про Kango»,
 * «"Nace"», латиница с заглавной. Планировщик на «с названием Канго» название
 * не выделял, на «проект канго» — выделял (замер 2026-10-01).
 */
const NAME_STOP = new Set(["ai", "ии", "saas", "b2b", "b2c", "mvp", "crm", "api", "kz", "usa", "сша", "снг", "казахстан", "казахстане", "узбекистан", "рынок", "рынке", "ниша", "нише", "стартап", "стартапы", "идея", "идею", "для", "под", "над", "без", "при", "или", "что", "как", "это", "где", "который", "которая", "которые", "the", "for", "with", "and", "that", "which"]);
function nameHints(q) {
  const out = new Set();
  const add = (x) => {
    const v = String(x || "").replace(/[?!.,;:]+$/, "").trim();
    if (v.length >= 3 && v.length <= 40 && !NAME_STOP.has(v.toLowerCase())) out.add(v);
  };
  for (const m of q.matchAll(/[«"“]([^»"”]{2,40})[»"”]/g)) add(m[1]);
  for (const m of q.matchAll(/(?:названием|называется|компани[яиюей]|стартап[а-я]*|проект[а-я]*|сервис[а-я]*|продукт[а-я]*|приложени[еяю]|\bпро|\babout|called|named)\s+([\p{L}\p{N}][\p{L}\p{N}.\-]{2,30}(?:\s[\p{Lu}][\p{L}\p{N}.\-]{1,20})?)/gu)) {
    if (/^(какой|какие|какую|который|этот|эту|это|сейчас|рынок|нишу|идею|себя|него|них|startup|project|company)/i.test(m[1])) continue;
    add(m[1]);
  }
  for (const m of q.matchAll(/(?<![\p{L}])([A-Z][a-z0-9]+(?:[.\-][A-Za-z]+)?(?:\s[A-Z][a-z0-9]+)?)/gu)) add(m[1]);
  return [...out].slice(0, 3);
}

async function planSearch(env, question) {
  const fallback = { live: false, queries: [question.slice(0, 80)], terms: [], entities: [] };
  try {
    const r = await groqFetch(env, { temperature: 0, max_completion_tokens: 400, reasoning_effort: "low",
      response_format: { type: "json_object" },
      messages: [{ role: "system", content: PLAN_SYSTEM }, { role: "user", content: `TODAY: ${new Date().toISOString().slice(0, 10)}
QUESTION: ${question.slice(0, 800)}` }] },
    ["openai/gpt-oss-20b"], 5000);
    if (!r || !r.ok) return fallback;
    const d = JSON.parse((await r.json()).choices[0].message.content || "{}");
    const arr = (x, n) => (Array.isArray(x) ? x.map((v) => String(v).trim()).filter(Boolean).slice(0, n) : []);
    const queries = arr(d.queries, 3);
    return { live: !!d.live, queries: queries.length ? queries : fallback.queries, terms: arr(d.terms, 3), entities: arr(d.entities, 4) };
  } catch (e) {
    return fallback;
  }
}

const WEB_FACTS_SYSTEM = `You search the web to answer a startup founder's question. Reply JSON only:
{"facts": [{"text": "one factual sentence with names, numbers and dates", "url": "https://exact source page", "date": "YYYY-MM-DD or empty"}]}
6-10 facts, newest first, only from pages you actually opened. Cover what the question needs: what the company/product does, funding and investors, revenue or users, pricing, competitors, Kazakhstan/CIS presence. Never invent.`;

async function webFacts(env, question, queries, { model = null, timeoutMs = 16000, meter = null } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST", signal: ctrl.signal,
      headers: { authorization: `Bearer ${env.LS_OPENROUTER_KEY}`, "content-type": "application/json",
        "HTTP-Referer": "https://launch-scout-bot.clam83574.workers.dev", "X-Title": "launch-scout" },
      body: JSON.stringify({ model: model || env.LS_WEB_MODEL || WEB_FAST_MODEL, max_tokens: 1500, temperature: 0.1, usage: { include: true },
        // У Perplexity поиск свой; остальным — плагин поиска OpenRouter.
        ...(String(model || "").startsWith("perplexity/") ? {} : { plugins: [{ id: "web", max_results: 6 }] }),
        messages: [{ role: "system", content: WEB_FACTS_SYSTEM }, { role: "user", content: `Question: ${question.slice(0, 800)}\nSearch phrases: ${queries.join("; ")}` }] }),
    });
    if (!r.ok) { await noteAiError(env, "openrouter live web", r.status, await r.text().catch(() => "")); return []; }
    const j = await r.json();
    if (meter && j.usage && j.usage.cost) meter.usd += Number(j.usage.cost) || 0;
    const t = (j.choices[0].message.content || "").trim();
    const d = JSON.parse(t.slice(t.indexOf("{"), t.lastIndexOf("}") + 1));
    return (d.facts || []).filter((f) => f && f.text).slice(0, 10).map((f) => ({
      text: `WEB${f.date ? " " + f.date : ""}: ${String(f.text).slice(0, 300)}`,
      // Редирект Google-поиска живёт недолго — такую ссылку не показываем.
      url: okUrl(f.url) && !/vertexaisearch|grounding-api-redirect/.test(f.url) ? String(f.url).replace(/[?&]utm_source=openai/, "") : null }))
      // Факт без страницы-источника проверить нельзя — в ответ он не идёт.
      .filter((f) => f.url);
  } catch (e) {
    await noteAiError(env, "openrouter live web", 0, String(e));
    return [];
  } finally {
    clearTimeout(timer);
  }
}

// Замер 2026-10-01 на вопросе о Nace.AI: gemini-3.8-flash с поиском — 21 с и
// оборванный JSON; flash-lite — 6,5 с, но ссылки — временные редиректы Google;
// perplexity/sonar — 11 с, первоисточники. Идут параллельно, факты сливаются.
const WEB_FAST_MODEL = "google/gemini-3.1-flash-lite";
const WEB_SOURCE_MODEL = "perplexity/sonar";

async function hnFacts(queries) {
  const since = Math.floor(Date.now() / 1000) - 365 * 86400;
  const res = await Promise.all(queries.slice(0, 2).map((q) =>
    fetch(`https://hn.algolia.com/api/v1/search?query=${encodeURIComponent(q)}&tags=story&numericFilters=created_at_i>${since},points>4&hitsPerPage=6`)
      .then((r) => (r.ok ? r.json() : { hits: [] })).catch(() => ({ hits: [] }))));
  const seen = new Set(), out = [];
  for (const h of res.flatMap((x) => x.hits || []).sort((a, b) => (b.points || 0) - (a.points || 0))) {
    if (seen.has(h.objectID)) continue;
    seen.add(h.objectID);
    out.push({ text: `HACKER NEWS ${String(h.created_at || "").slice(0, 10)} (${h.points || 0} points, ${h.num_comments || 0} comments): ${h.title}`,
      url: `https://news.ycombinator.com/item?id=${h.objectID}` });
  }
  return out.slice(0, 6);
}

async function ghFacts(env, queries) {
  const h = { "user-agent": "launch-scout-bot", accept: "application/vnd.github+json" };
  if (env.LS_GH_TOKEN) h.authorization = `Bearer ${env.LS_GH_TOKEN}`;
  const r = await fetch(`https://api.github.com/search/repositories?q=${encodeURIComponent(queries[0])}&sort=stars&per_page=5`, { headers: h })
    .then((x) => (x.ok ? x.json() : { items: [] })).catch(() => ({ items: [] }));
  return (r.items || []).filter((x) => x.stargazers_count >= 20).map((x) => ({
    text: `GITHUB open-source ${x.full_name} (${x.stargazers_count} stars, updated ${String(x.pushed_at || "").slice(0, 10)}): ${String(x.description || "").slice(0, 200)}`,
    url: x.html_url }));
}

async function dbFacts(env, plan) {
  // По базе ищем только названия (сущности): фраза «NACE AI pricing» целиком в названии не встретится.
  const words = (plan.entities.length ? plan.entities : plan.queries.slice(0, 1)).map((w) => w.toLowerCase().trim()).filter((w) => w.length >= 3).slice(0, 4);
  if (!words.length) return [];
  // «NACE AI» в вопросе и «Nace.AI» в базе: сравниваем без точек, пробелов и дефисов.
  const squash = (col) => `replace(replace(replace(lower(${col}), '.', ''), ' ', ''), '-', '')`;
  const cond = words.map((_, k) => `(${squash("company")} LIKE ?${k + 1} OR ${squash("key")} LIKE ?${k + 1})`).join(" OR ");
  const { results } = await env.DB.prepare(`SELECT * FROM rounds WHERE ${cond} ORDER BY ts DESC LIMIT 10`)
    .bind(...words.map((w) => `%${w.replace(/[.\s-]+/g, "")}%`)).all().catch(() => ({ results: [] }));
  let rows = results || [];
  // Опечатка или другое написание («Nase AI», «Anthropik»): кандидаты с тем же
  // началом названия, расстояние Левенштейна до 1-2 букв.
  if (!rows.length && plan.entities.length) {
    for (const e of plan.entities.map((w) => w.toLowerCase().replace(/[^a-z0-9а-яё]/g, "")).filter((w) => w.length >= 5).slice(0, 3)) {
      const { results: cand } = await env.DB.prepare(`SELECT * FROM rounds WHERE ${squash("company")} LIKE ?1 LIMIT 200`)
        .bind(e.slice(0, 2) + "%").all().catch(() => ({ results: [] }));
      const lim = e.length >= 9 ? 2 : 1;
      rows.push(...(cand || []).filter((r) => levenshtein(e, String(r.company || "").toLowerCase().replace(/[^a-z0-9а-яё]/g, "")) <= lim));
    }
  }
  return rows.slice(0, 10).map((r) => ({
    text: `ROUND ${new Date(r.ts * 1000).toISOString().slice(0, 10)}: ${r.company} — ${usdM(r.usd)}${r.stage ? " " + r.stage : ""}, niche "${r.niche}"${r.investors ? ", investors " + r.investors : ""}: ${r.what_en || r.what_ru || ""}`,
    url: r.url }));
}

function levenshtein(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 9;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = cur;
  }
  return prev[b.length];
}

// ---------------------------------------------------------------------------
// 📥 Самообучение базы: раунды, найденные живым поиском
//
// Nace.AI ($21,5M seed, май 2026) не было в базе — сбор новостей его не
// увидел, нашёл веб-поиск в чате (2026-10-01). Такие раунды бот откладывает в
// found_rounds; пайплайн открывает страницу-источник, проверяет там название
// и сумму и только тогда добавляет раунд — выдумка поиска в базу не попадёт.
// ---------------------------------------------------------------------------
const LEARN_SYSTEM = `From these web facts extract startup funding rounds. Reply JSON only:
{"rounds": [{"company": "...", "usd": 21500000, "stage": "pre-seed|seed|a|b|c+|growth|unknown", "date": "YYYY-MM-DD or empty", "investors": ["..."], "fact": <number of the fact it comes from>}]}
Only rounds explicitly stated in a fact (company name AND amount). Convert the amount to US dollars as a number. Never guess.`;

async function learnRounds(env, facts) {
  const web = facts.filter((f) => f.url && /^WEB/.test(f.text) && /rais|fund|seed|series|round|привлек|раунд|инвест/i.test(f.text));
  if (!web.length) return 0;
  try {
    const r = await groqFetch(env, { temperature: 0, max_completion_tokens: 800, reasoning_effort: "low",
      response_format: { type: "json_object" },
      messages: [{ role: "system", content: LEARN_SYSTEM }, { role: "user", content: web.map((f, i) => `[${i + 1}] ${f.text}`).join("\n").slice(0, 6000) }] },
    ["openai/gpt-oss-20b"], 8000);
    if (!r || !r.ok) return 0;
    const d = JSON.parse((await r.json()).choices[0].message.content || "{}");
    let n = 0;
    for (const x of (d.rounds || []).slice(0, 5)) {
      const f = web[Number(x.fact) - 1];
      if (!f || !x.company || !(Number(x.usd) > 0)) continue;
      const key = String(x.company).toLowerCase().replace(/[^a-z0-9а-яё]/g, "");
      const known = await env.DB.prepare("SELECT 1 FROM rounds WHERE replace(replace(replace(lower(company), '.', ''), ' ', ''), '-', '') = ?1 LIMIT 1")
        .bind(key).first().catch(() => null);
      if (known) continue;
      const res = await env.DB.prepare("INSERT OR IGNORE INTO found_rounds (url, company, usd, stage, date, investors, fact, ts, status) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'new')")
        .bind(f.url, String(x.company).slice(0, 80), Number(x.usd), String(x.stage || ""), String(x.date || ""),
          JSON.stringify((x.investors || []).slice(0, 6)), f.text.slice(0, 400), Math.floor(Date.now() / 1000)).run().catch(() => null);
      n += res && res.meta && res.meta.changes ? 1 : 0;
    }
    return n;
  } catch (e) {
    await noteAiError(env, "learn rounds", 0, String(e));
    return 0;
  }
}


/** Живой поиск со статусом в чате: [{text, url}] — факты для ответа. */
async function liveSearch(env, chatId, question, plan, lang, meter = null) {
  const s = L(lang);
  const src = [["db", s.src_db], ["web", s.src_web], ["hn", "Hacker News"], ["gh", "GitHub"]];
  const state = Object.fromEntries(src.map(([k]) => [k, null]));
  const head = fmt(s.live_head, plan.queries.map((q) => `«${q}»`).join(", "));
  const view = () => head + "\n" + src.map(([k, label]) => `${state[k] === null ? "⏳" : state[k] ? "✅" : "▫️"} ${label}${state[k] ? " · " + state[k] : ""}`).join("\n");
  const st = await tg(env, "sendMessage", { chat_id: chatId, text: view() });
  let lastEdit = 0;
  const done = async (k, list) => {
    state[k] = list.length;
    if (st && st.ok && Date.now() - lastEdit > 700) {
      lastEdit = Date.now();
      await tg(env, "editMessageText", { chat_id: chatId, message_id: st.result.message_id, text: view() });
    }
    return list;
  };
  const [db, web, hn, gh] = await Promise.all([
    dbFacts(env, plan).catch(() => []).then((x) => done("db", x)),
    Promise.all([webFacts(env, question, plan.queries, { model: WEB_FAST_MODEL, timeoutMs: 11000, meter }),
      webFacts(env, question, plan.queries, { model: WEB_SOURCE_MODEL, timeoutMs: 14000, meter })])
      .then(([a, b]) => {
        const seen = new Set(), out = [];
        for (const f of [...b, ...a]) {
          const k = f.text.toLowerCase().replace(/[^a-zа-я0-9]/g, "").slice(0, 60);
          if (!seen.has(k)) { seen.add(k); out.push(f); }
        }
        return out.slice(0, 14);
      }).then((x) => done("web", x)),
    hnFacts(plan.queries).catch(() => []).then((x) => done("hn", x)),
    ghFacts(env, plan.queries).catch(() => []).then((x) => done("gh", x)),
  ]);
  const del = () => (st && st.ok ? tg(env, "deleteMessage", { chat_id: chatId, message_id: st.result.message_id }) : null);
  return { facts: [...db, ...web, ...hn, ...gh], clear: del };
}

// ---------------------------------------------------------------------------
// 📡 Глубокий поиск: X и Google Trends через GitHub Actions
// ---------------------------------------------------------------------------
const RESEARCH_PER_USER = 6;

async function researchDispatch(env, chatId, lang, question, plan) {
  const today = new Date().toISOString().slice(0, 10);
  const used = Number((await meta(env, `research_${chatId}_${today}`)) || 0);
  if (used >= RESEARCH_PER_USER) return "limit";
  const id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
  await setMeta(env, "rjob_" + id, JSON.stringify({ chatId, lang, q: question.slice(0, 800), queries: plan.queries,
    terms: plan.terms.length ? plan.terms : plan.queries.slice(0, 2), ts: Math.floor(Date.now() / 1000) }));
  const err = await dispatchRun(env, { job: id }, "research.yml");
  if (err) { await noteAiError(env, "research dispatch", 0, err); return "err"; }
  await setMeta(env, `research_${chatId}_${today}`, used + 1);
  return null;
}

/** Находки глубокого поиска -> факты -> ответ в чат вторым сообщением. */
async function researchAnswer(env, res) {
  const job = JSON.parse((await meta(env, "rjob_" + res.id)) || "null");
  if (!job) return;
  await setMeta(env, "rjob_" + res.id, "");
  const s = L(job.lang);
  const facts = [];
  const add = (text, url = null) => facts.push({ id: facts.length + 1, text, url });
  for (const t of (res.x || []).slice(0, 18)) {
    add(`X POST ${t.ts ? new Date(t.ts * 1000).toISOString().slice(0, 10) : ""} by @${t.who} (${t.followers ?? "?"} followers; ${t.likes} likes, ${t.replies} replies${t.views ? ", " + t.views + " views" : ""}): ${String(t.text || "").replace(/\s+/g, " ")}`, t.url);
  }
  for (const g of res.trends || []) {
    for (const geo of ["world", "KZ"]) {
      const v = g[geo];
      if (!v) continue;
      add(v.low ? `GOOGLE TRENDS "${g.term}" ${geo === "KZ" ? "in Kazakhstan" : "worldwide"}: almost no searches.`
        : `GOOGLE TRENDS "${g.term}" ${geo === "KZ" ? "in Kazakhstan" : "worldwide"}: last 12 weeks vs previous 12 = ${v.g3m}x, last 8 weeks vs a year ago = ${v.g1y}x, peak ${v.peak_ago} weeks ago; weekly index (0-100) ${JSON.stringify(g[geo + "_weekly"] || [])}. Google changed how it counts searches in mid-2026 — many terms dropped together; treat drops under ~40% with caution.`,
      `https://trends.google.com/trends/explore?q=${encodeURIComponent(g.term)}${geo === "KZ" ? "&geo=KZ" : ""}`);
    }
  }
  if (!facts.length) {
    await tg(env, "sendMessage", { chat_id: job.chatId, text: s.research_empty + (res.x_err ? ` (X: ${String(res.x_err).slice(0, 60)})` : "") });
    return;
  }
  const messages = [
    { role: "system", content: FAST_SYSTEM.research.replace("%LANG%", LANG_EN[job.lang] || "Russian") },
    { role: "user", content: `FACTS:\n${facts.map((f) => `[F${f.id}] ${f.text}`).join("\n").slice(0, 14000)}\n\nQUESTION: ${job.q}` },
  ];
  let full = await streamOpenRouter(env, env.LS_FAST_MODEL || FAST_MODEL, messages, async () => {}, 30000, 1400, 8000);
  if (!full) full = await streamOpenRouter(env, FALLBACK_FAST_MODEL, messages, async () => {}, 25000, 1400, 8000);
  if (!full) return;
  const { html } = groundAnswer(full, facts);
  await tg(env, "sendMessage", { chat_id: job.chatId, text: (s.research_head + "\n\n" + html).slice(0, 3900), parse_mode: "HTML", disable_web_page_preview: true });
}

// ---------------------------------------------------------------------------
// 🧭 Кнопки под ответом и смежные ниши
// ---------------------------------------------------------------------------
function answerKb(s, ctx, following) {
  // Цена — прямо на кнопке: после ответа по базе доплата до 40 LS, после
  // живого поиска остаются только X и Google Trends.
  const live = { text: ctx.liveDone ? fmt(s.ab_xtrends, LS_PRICE.research) : fmt(s.ab_live_p, LS_PRICE.live_up), callback_data: "ca:live" };
  if (!ctx.niche) return { inline_keyboard: [[live], [{ text: s.ab_map, callback_data: "ca:map" }]] };
  return { inline_keyboard: [
    [{ text: s.ab_deep, callback_data: "ca:deep" }, { text: s.ab_map, callback_data: "ca:map" }],
    [{ text: s.ab_wide, callback_data: "ca:wide" }, { text: s.ab_comp, callback_data: "ca:comp" }],
    [{ text: following ? s.ab_unfollow : s.ab_follow, callback_data: "ca:follow" }],
    [live],
  ] };
}

async function saveCtx(env, chatId, ctx) {
  await setMeta(env, "ctx_" + chatId, JSON.stringify({ ...ctx, ts: Math.floor(Date.now() / 1000) }));
}
async function loadCtx(env, chatId) {
  try { return JSON.parse((await meta(env, "ctx_" + chatId)) || "{}") || {}; } catch { return {}; }
}

/** Смежные ниши — соседи по смыслу в векторном индексе, цифры из матрицы. Без модели: мгновенно. */
async function adjacentMsg(env, chatId, lang, ctx) {
  const s = L(lang);
  const id = await vecId(ctx.niche);
  const got = await env.VEC.getByIds([id]).catch(() => []);
  let values = got && got[0] && got[0].values;
  if (!values) values = (await embed(env, [ctx.niche]))[0];
  if (!values) { await tg(env, "sendMessage", { chat_id: chatId, text: s.niches_empty }); return; }
  const res = await env.VEC.query(values, { topK: 9, returnMetadata: "all" });
  const names = ((res && res.matches) || []).map((m) => m.metadata && m.metadata.niche).filter((n) => n && n !== ctx.niche).slice(0, 6);
  if (!names.length) { await tg(env, "sendMessage", { chat_id: chatId, text: s.niches_empty }); return; }
  const { results } = await env.DB.prepare(`SELECT niche, name_ru, data FROM niche_matrix WHERE niche IN (${names.map((_, k) => "?" + (k + 1)).join(",")})`)
    .bind(...names).all().catch(() => ({ results: [] }));
  const byName = Object.fromEntries((results || []).map((r) => [r.niche, r]));
  const lines = [fmt(s.adj_title, esc(lang === "en" ? ctx.niche : ctx.name_ru || ctx.niche))];
  const kb = [], shownTitles = new Set();
  names.forEach((n, i) => {
    const r = byName[n];
    if (!r) return;
    let d = {};
    try { d = JSON.parse(r.data); } catch { /* пусто */ }
    const o = d.opp || {}, g = d.gap || {};
    let title = lang === "en" ? n : r.name_ru || n;
    // Две английские ниши с одинаковым русским названием — различаем по-английски.
    if (shownTitles.has(title)) title = `${title} (${n})`;
    shownTitles.add(title);
    lines.push("", `<b>${esc(title)}</b> — ${s["opp_" + o.type] || ""} · <b>${o.score ?? "?"}</b>/100`,
      `   ${fmt(s.adj_line, d.companies_6m || 0, d.early_6m || 0, usd(d.usd_6m, s))}${g.kz ? ` · ${GAP_ICON[g.kz] || "⚪"} ${s.gap_kz} ${s["gap_" + g.kz] || ""}` : ""}` +
      (d.search && d.search.rel ? ` · 🔎 ${d.search.pct >= 50 ? "↑" : "↓"}` : ""));
    kb.push([{ text: `🔬 ${title}`.slice(0, 40), callback_data: `cn:${i}` }]);
  });
  await saveCtx(env, chatId, { ...ctx, adj: names });
  await tg(env, "sendMessage", { chat_id: chatId, text: lines.join("\n").slice(0, 3900), parse_mode: "HTML", disable_web_page_preview: true,
    reply_markup: { inline_keyboard: kb } });
}

/** Кнопки «ca:*» под ответом и «cn:i» — смежная ниша. */
async function answerAction(env, chatId, lang, data, prefs, cb, msgId) {
  const s = L(lang);
  const ctx = await loadCtx(env, chatId);
  if (data.startsWith("cn:")) {
    const n = (ctx.adj || [])[Number(data.slice(3))];
    if (n) await chatReply(env, chatId, `Dig deep into the niche "${n}".`, lang, "deep", { niche: n });
    return;
  }
  const act = data.slice(3);
  if (!ctx.q && !ctx.niche) { await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: s.ctx_gone }); return; }
  if (act === "deep" && ctx.niche) return chatReply(env, chatId, `Dig deep into the niche "${ctx.niche}".`, lang, "deep", { niche: ctx.niche });
  if (act === "comp" && ctx.niche) return chatReply(env, chatId, `Who already does this in "${ctx.niche}": competitors, their prices and weak spots, and where a newcomer can win. Original question: ${ctx.q || ""}`, lang, "comp", { niche: ctx.niche });
  if (act === "map") return chatReply(env, chatId, ctx.niche ? `Idea map for a startup in the niche "${ctx.niche}". Original question: ${ctx.q || ""}` : `Idea map for: ${ctx.q}`, lang, "map", { niche: ctx.niche || null });
  if (act === "wide" && ctx.niche) return adjacentMsg(env, chatId, lang, ctx);
  if (act === "follow" && ctx.niche) {
    const p = await setPrefs(env, chatId, { niches: toggle(prefs.niches, ctx.niche) });
    const on = (p.niches || []).includes(ctx.niche);
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: on ? s.follow_on : s.follow_off });
    if (msgId) await tg(env, "editMessageReplyMarkup", { chat_id: chatId, message_id: msgId, reply_markup: answerKb(s, ctx, on) });
    return;
  }
  if (act === "live" && ctx.q && ctx.liveDone) {
    // Живой поиск уже был — остаются X и Google Trends.
    if (!(await lsCanAfford(env, chatId, "research"))) return lsShortMsg(env, chatId, lang, "research");
    const r = await researchDispatch(env, chatId, lang, ctx.q, { queries: ctx.queries || [ctx.q.slice(0, 80)], terms: ctx.terms || [] });
    if (!r) await lsSpend(env, chatId, "research", 0);
    return tg(env, "sendMessage", { chat_id: chatId, text: r === "limit" ? fmt(s.research_limit, RESEARCH_PER_USER) : r ? s.research_err : s.research_started });
  }
  if (act === "live" && ctx.q) return chatReply(env, chatId, ctx.q, lang, "chat", { niche: null, live: true, research: true, upgrade: true });
}

/**
 * Быстрый ответ по матрице. mode: chat | deep (niche задана) | check.
 * Возвращает true, если ответ отправлен; false — пусть отвечает запасной путь.
 */
async function fastAnswer(env, chatId, question, lang, mode, { niche = null, profile = "", hist = [], live = false, research = false, upgrade = false } = {}) {
  const s = L(lang);
  if (!env.AI || !env.VEC || !env.LS_OPENROUTER_KEY) return false;
  const snap = await loadSnapshot(env);   // кэш на минуту — обычно мгновенно
  // План живого поиска — параллельно с матрицей (быстрая модель, ~1 с).
  const planP = mode === "chat" || mode === "check" || mode === "map" ? planSearch(env, question) : Promise.resolve(null);
  let fx;
  try {
    fx = await matrixFacts(env, question, { niche, snap });
  } catch (e) {
    await noteAiError(env, "matrix", 0, String(e));
    return false;
  }
  const plan = await planP;
  // Живой поиск: вопрос о конкретной компании или событии, ниши в матрице
  // не нашлось или человек сам нажал «Искать везде».
  let liveRes = null, cleared = false;
  // Вопрос «кто больше всех вкладывает» отвечает наш рейтинг инвесторов —
  // веб на нём тянул пенсионные фонды и PE (замер 2026-10-01); сеть — по кнопке.
  const invAnswered = INVESTOR_Q.test(question) && (((marketOf(snap) || {}).report || {}).investors_top || []).length > 0;
  // Названа конкретная компания (домен или имя из плана) — ищем всегда: модель-
  // планировщик то включала поиск на «insora.dev», то нет (2026-10-01).
  const hints = nameHints(question);
  if (plan && hints.length) plan.entities = [...new Set([...plan.entities, ...hints])];
  const named = /\b[a-z0-9-]{2,}\.(?:ai|dev|io|com|co|app|so|xyz|tech|kz|ru|org|net)\b/i.test(question) || hints.length > 0
    || (plan && plan.entities.some((e) => e.length >= 3 && !/^(ai|ии|saas|b2b|b2c)$/i.test(e)));
  // Живой поиск дороже (40 LS): без баланса на него отвечаем по базе.
  const meter = { usd: 0 };
  const canLive = await lsCanAfford(env, chatId, "live");
  if (plan && canLive && (live || ((plan.live || named) && !invAnswered) || (!fx.niches.length && mode === "chat" && !invAnswered))) {
    liveRes = await liveSearch(env, chatId, question, plan, lang, meter);
    for (const f of liveRes.facts) fx.facts.push({ id: fx.facts.length + 1, text: f.text, url: f.url });
  }
  const clearStatus = async () => { if (liveRes && !cleared) { cleared = true; await liveRes.clear(); } };
  // Сеть — только если про конкурентов в матрице ничего нет.
  if ((mode === "deep" || mode === "map" || mode === "comp") && fx.niches[0] && !fx.niches[0].web) {
    const d = fx.niches[0];
    const web = await webSearchWithStatus(env, chatId, fmt(s.searching_niche, lang === "en" ? d.niche : d.name_ru || d.niche), d.niche,
      (d.top_6m || []).slice(0, 5).map((r) => r.company), meter);
    if (web) {
      web.ts = Math.floor(Date.now() / 1000);
      d.web = web;
      await env.DB.prepare("UPDATE niche_matrix SET data = ?1 WHERE niche = ?2").bind(JSON.stringify(d), d.niche).run().catch(() => null);
      fx = await matrixFacts(env, question, { niche: d.niche, snap });
    }
  }
  if (mode === "check") {
    const web = await webSearchWithStatus(env, chatId, s.searching_idea, question,
      fx.niches.flatMap((d) => (d.top_6m || []).slice(0, 2).map((r) => r.company)).slice(0, 6), meter);
    if (web) {
      for (const c of (web.competitors || []).slice(0, 8)) fx.facts.push({ id: fx.facts.length + 1, text: `COMPETITOR of this idea: ${c.name} (${c.market || "?"}), price ${c.price || "not published"} — ${c.note || ""}`, url: c.url });
      for (const c of (web.complaints || []).slice(0, 4)) fx.facts.push({ id: fx.facts.length + 1, text: `CUSTOMER COMPLAINT about existing products: ${c.text}`, url: c.source });
    }
  }
  const factText = fx.facts.map((f) => `[F${f.id}] ${f.text}`).join("\n").slice(0, 16000);
  const messages = [
    { role: "system", content: FAST_SYSTEM[mode].replace("%LANG%", LANG_EN[lang] || "Russian") },
    ...(mode === "chat" ? hist.slice(-4).map((h) => ({ role: h.role === "user" ? "user" : "assistant", content: String(h.text).slice(0, 800) })) : []),
    { role: "user", content: `TODAY: ${new Date().toISOString().slice(0, 10)}\nPROFILE: ${profile || "unknown"}\n\nFACTS:\n${factText}\n\n${mode === "check" ? "IDEA" : "QUESTION"}: ${question.slice(0, 1500)}` },
  ];
  // Поток: первое сообщение — как только пришли первые слова, дальше правим
  // его не чаще раза в 1,2 секунды (лимит Telegram на правки).
  let msgId = null, lastEdit = 0;
  const onDelta = async (t) => {
    const now = Date.now();
    const shown = t.replace(/\[F\d+(?:\s*[,;–-]\s*F?\d+)*\]/g, "").replace(/\*\*/g, "").replace(/\nPROFILE:.*$/s, "");
    if (!msgId && shown.trim().length > 20) {
      await clearStatus();
      const m = await tg(env, "sendMessage", { chat_id: chatId, text: shown.slice(0, 3800) + " …" });
      if (m && m.ok) msgId = m.result.message_id;
      lastEdit = now;
    } else if (msgId && now - lastEdit > 1200) {
      lastEdit = now;
      await tg(env, "editMessageText", { chat_id: chatId, message_id: msgId, text: shown.slice(0, 3800) + " …" });
    }
  };
  const maxTok = mode === "chat" ? 1100 : 2000;
  let full = await streamOpenRouter(env, env.LS_FAST_MODEL || FAST_MODEL, messages, onDelta, 25000, maxTok, FIRST_TOKEN_MS, meter);
  if (!full && !msgId) full = await streamOpenRouter(env, FALLBACK_FAST_MODEL, messages, onDelta, 20000, maxTok, 8000, meter);
  await clearStatus();
  if (!full || !full.trim()) {
    if (msgId) await tg(env, "deleteMessage", { chat_id: chatId, message_id: msgId });
    return false;
  }
  const prof = /\nPROFILE:\s*(.+)$/s.exec(full);
  const body = full.replace(/\nPROFILE:.*$/s, "").trim();
  const { html, unsupported } = groundAnswer(body, fx.facts, question + " " + profile);
  lastUnsupported = { unsupported, raw: body, facts: fx.facts.length };
  // Списание: живой поиск — по цене поиска, иначе по режиму ответа.
  const charge = await lsSpend(env, chatId, liveRes ? (upgrade ? "live_up" : "live") : (LS_PRICE[mode] ? mode : "chat"), meter.usd);
  // Плашки-«посты» — после одобрения владельцем (переменная LS_CARDS=1).
  let finalText = (html ? (env.LS_CARDS === "0" ? html : cardify(html)) : esc(body));
  // Обрезка не должна разрывать плашку: выкидываем последние блоки целиком.
  while (finalText.length > 3900 && finalText.includes("<blockquote>")) finalText = finalText.slice(0, finalText.lastIndexOf("<blockquote>")).trim();
  finalText = finalText.slice(0, 3900);
  // Кнопки под ответом: что можно сделать с этой нишей дальше.
  const prevCtx = mode === "chat" || mode === "check" ? {} : await loadCtx(env, chatId);
  const top = fx.niches[0];
  const ctx = { niche: niche || (top && top.niche) || null, name_ru: (top && top.name_ru) || prevCtx.name_ru || "",
    q: prevCtx.q || question.slice(0, 800), liveDone: !!liveRes,
    queries: plan ? plan.queries : prevCtx.queries, terms: plan ? plan.terms : prevCtx.terms };
  const following = ctx.niche ? ((await getPrefs(env, chatId)).niches || []).includes(ctx.niche) : false;
  const kb = answerKb(s, ctx, following);
  await saveCtx(env, chatId, ctx);
  if (msgId) {
    const r = await tg(env, "editMessageText", { chat_id: chatId, message_id: msgId, text: finalText, parse_mode: "HTML", disable_web_page_preview: true, reply_markup: kb });
    if (!(r && r.ok)) await tg(env, "editMessageText", { chat_id: chatId, message_id: msgId, text: body.replace(/\[F\d+(?:\s*[,;–-]\s*F?\d+)*\]/g, "").replace(/\*\*/g, "").slice(0, 3900), reply_markup: kb });
  } else {
    await tg(env, "sendMessage", { chat_id: chatId, text: finalText, parse_mode: "HTML", disable_web_page_preview: true, reply_markup: kb });
  }
  if (charge.warn) await tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", text: fmt(s.ls_low, charge.left) + "\n\n" + lsTariffs(s), reply_markup: starsButtons(s) });
  if (liveRes) await learnRounds(env, liveRes.facts);
  // «Искать везде»: X и Google Trends — в GitHub Actions, дополнение придёт следом.
  if (research && plan && !(await lsCanAfford(env, chatId, "research"))) {
    await lsShortMsg(env, chatId, lang, "research");
  } else if (research && plan) {
    const r = await researchDispatch(env, chatId, lang, question, plan);
    if (!r && !upgrade) await lsSpend(env, chatId, "research", 0);
    await tg(env, "sendMessage", { chat_id: chatId, text: r === "limit" ? fmt(s.research_limit, RESEARCH_PER_USER) : r ? s.research_err : s.research_started });
  }
  const now = Math.floor(Date.now() / 1000);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO chat_log (user_id, ts, role, text) VALUES (?1, ?2, 'user', ?3)").bind(String(chatId), now, question.slice(0, 1500)),
    env.DB.prepare("INSERT INTO chat_log (user_id, ts, role, text) VALUES (?1, ?2, 'assistant', ?3)").bind(String(chatId), now + 1, body.replace(/\[F\d+\]/g, "").slice(0, 3000)),
    env.DB.prepare("DELETE FROM chat_log WHERE user_id = ?1 AND ts < ?2").bind(String(chatId), now - 3 * 86400),
  ]).catch(() => null);
  if (prof && prof[1].trim() && prof[1].trim() !== profile) {
    await env.DB.prepare("INSERT OR REPLACE INTO chat_profile (user_id, about, ts) VALUES (?1, ?2, ?3)")
      .bind(String(chatId), prof[1].trim().slice(0, 400), now).run().catch(() => null);
  }
  return true;
}

// ---------------------------------------------------------------------------
// 🎯 Персональный радар: анкета из 5 вопросов -> возможности под человека
//
// «Вот 100 новых стартапов» — информационный продукт; «вот 3 возможности,
// которые подходят именно тебе, и почему» — инструмент решения (разбор
// концепции 2026-09-30). Подбор — прозрачные правила над матрицей ниш, без
// ИИ: у каждой поправки видна причина.
// ---------------------------------------------------------------------------
const FOUNDER_Q = {
  budget: ["5k", "20k", "100k"],
  skills: ["dev", "sales", "marketing", "industry"],
  markets: ["kz", "cis", "mena", "us", "global"],
  models: ["saas", "marketplace", "agent", "fintech", "hardware"],
  horizon: ["1m", "3m", "1y"],
};
const FOUNDER_MULTI = new Set(["skills", "markets", "models"]);
const MODEL_SECTORS = {
  saas: ["b2b_saas", "devtools", "security", "health", "edu", "proptech"],
  marketplace: ["commerce", "consumer", "mobility", "proptech"],
  agent: ["ai_agents", "ai_infra", "devtools", "b2b_saas"],
  fintech: ["fintech", "crypto"],
  hardware: ["hardware", "energy", "defense_space", "mobility"],
};
const HEAVY = new Set(["hardware", "defense_space", "energy", "ai_infra"]);

/** Возможности под профиль: [{n, score, reasons}] — лучшие первыми. */
// Ниши, которые не рекомендуем (решение владельца 2026-10-01): цифры по ним
// остаются в аналитике как факт, но в радар «под вас» они не попадают.
const NOT_RECOMMENDED = /\b(lending|loans?|lender|payday|bnpl|buy now pay later|credit card|mortgage|microfinance|casinos?|gambling|betting|sportsbook|lotter(y|ies)|igaming|alcohol|wine|beer|brewer(y|ies)|spirits|liquor|cannabis|marijuana|adult (content|entertainment|videos?|sites?)|porn\w*|pork)\b/i;

function radarFor(rep, f) {
  const out = [];
  for (const n of (rep.niches || [])) {
    if (NOT_RECOMMENDED.test(n.niche)) continue;
    const o = n.opp || {};
    let score = o.score || 0;
    const why = [];
    const sectors = (f.models || []).flatMap((m) => MODEL_SECTORS[m] || []);
    if (sectors.length && sectors.includes(n.sector)) { score += 15; why.push("fit_model"); }
    if (sectors.length && !sectors.includes(n.sector)) score -= 10;
    if (f.budget === "5k" && HEAVY.has(n.sector)) { score -= 25; why.push("heavy"); }
    if (f.budget !== "100k" && o.type === "overheated") { score -= 20; why.push("overheated"); }
    const local = (f.markets || []).some((m) => m === "kz" || m === "cis");
    if (local && n.gap) {
      if (n.gap.kz === "free") { score += 15; why.push("kz_free"); }
      if (n.gap.kz === "crowded") { score -= 10; why.push("kz_crowded"); }
    }
    if (local && (n.local_tasks_n || 0) > 0) { score += 10; why.push("kz_tasks"); }
    if (f.horizon === "1m" && (o.type === "window" || o.type === "local_gap")) { score += 10; why.push("fast"); }
    if ((f.skills || []).includes("dev") && ["devtools", "ai_agents", "ai_infra"].includes(n.sector)) score += 5;
    if ((f.skills || []).includes("sales") && ["b2b_saas", "fintech", "health"].includes(n.sector)) score += 5;
    if ((n.hiring_n || 0) > 0) why.push("hiring");
    out.push({ n, score: Math.max(0, Math.min(100, Math.round(score))), why });
  }
  return out.sort((a, b) => b.score - a.score);
}

function radarText(rep, f, lang, limit = 3) {
  const s = L(lang);
  const items = radarFor(rep, f).slice(0, limit);
  if (!items.length) return "";
  const lines = [s.radar_title];
  items.forEach(({ n, score, why }, i) => {
    const o = n.opp || {};
    lines.push("", `${i + 1}. <b>${esc(nicheName(rep, n, lang))}</b> — ${s.radar_fit} ${score}/100`);
    lines.push(`   ${s["opp_" + o.type] || o.type || ""}`);
    lines.push(`   ${fmt(s.niche_line, n.n, n.early, usd(n.usd, s))}`);
    const reasons = why.map((w) => s["why_" + w]).filter(Boolean);
    if (reasons.length) lines.push(`   ${s.radar_why} ${reasons.join(", ")}`);
    const t = (n.local_tasks || [])[0];
    if (t) lines.push(`   🏢 <a href="${esc(t.url)}">${esc(String(t.title).slice(0, 90))}</a> — ${esc(t.company || "")}`);
  });
  return lines.join("\n");
}

function founderView(p, lang, step = null) {
  const s = L(lang);
  const f = p.founder || {};
  const on = (k, v) => (FOUNDER_MULTI.has(k) ? (f[k] || []).includes(v) : f[k] === v);
  const rows = [];
  for (const k of Object.keys(FOUNDER_Q)) {
    rows.push([{ text: s["fq_" + k], callback_data: "noop" }]);
    const opts = FOUNDER_Q[k].map((v) => ({ text: (on(k, v) ? "✅ " : "") + s["fa_" + k + "_" + v], callback_data: `fp:${k}:${v}` }));
    for (let i = 0; i < opts.length; i += 3) rows.push(opts.slice(i, i + 3));
  }
  rows.push([{ text: s.radar_show, callback_data: "radar" }, { text: s.done, callback_data: "home" }]);
  return { text: s.founder_title, reply_markup: { inline_keyboard: rows } };
}

async function radarMsg(env, chatId, lang, prefs) {
  const s = L(lang);
  if (!prefs.founder || !Object.keys(prefs.founder).length) {
    await show(env, chatId, null, founderView(prefs, lang));
    return;
  }
  const rep = ((marketOf(await loadSnapshot(env))) || {}).report || {};
  const text = radarText(rep, prefs.founder, lang, 5) || s.niches_empty;
  await tg(env, "sendMessage", { chat_id: chatId, text: text.slice(0, 3900), parse_mode: "HTML", disable_web_page_preview: true,
    reply_markup: { inline_keyboard: [[{ text: s.founder_edit, callback_data: "fprof" }, { text: s.kb_niches, callback_data: "niches" }]] } });
}

/** Профиль основателя — и для чата: коротко, по-английски, в chat_profile. */
function founderSummary(f) {
  const bits = [];
  if (f.budget) bits.push(`budget ~$${f.budget}`);
  if ((f.skills || []).length) bits.push(`skills: ${f.skills.join(", ")}`);
  if ((f.markets || []).length) bits.push(`markets: ${f.markets.join(", ")}`);
  if ((f.models || []).length) bits.push(`business models: ${f.models.join(", ")}`);
  if (f.horizon) bits.push(`time to launch: ${f.horizon}`);
  return bits.length ? "Founder: " + bits.join("; ") + "." : "";
}

// Один раз на экземпляр Worker: иначе каждое нажатие кнопки стоило бы
// пяти лишних запросов к D1.
let tablesReady = false;

async function ensureTables(env) {
  if (tablesReady) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS idea_cards (item_id INTEGER PRIMARY KEY, data TEXT, ts INTEGER)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS prefs (user_id TEXT PRIMARY KEY, topics TEXT, ts INTEGER, lang TEXT, audience TEXT, notify TEXT, sectors TEXT, sources TEXT, sens TEXT)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS chat_log (user_id TEXT, ts INTEGER, role TEXT, text TEXT)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS chat_log_user ON chat_log (user_id, ts)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS chat_profile (user_id TEXT PRIMARY KEY, about TEXT, ts INTEGER)"),
    // Датасет раундов за полгода — для инструментов ИИ (search_rounds).
    env.DB.prepare("CREATE TABLE IF NOT EXISTS rounds (key TEXT PRIMARY KEY, ts INTEGER, company TEXT, usd REAL, stage TEXT, niche TEXT, sector TEXT, country TEXT, investors TEXT, what_ru TEXT, what_en TEXT, url TEXT, outlets INTEGER, doc TEXT)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS rounds_ts ON rounds (ts)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS rounds_niche ON rounds (niche)"),
    // Матрица ниш — готовые цифры для быстрого ответа (fastAnswer).
    env.DB.prepare("CREATE TABLE IF NOT EXISTS niche_matrix (niche TEXT PRIMARY KEY, name_ru TEXT, sector TEXT, data TEXT, ts INTEGER)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS ls_balance (user_id TEXT PRIMARY KEY, plan TEXT, paid_until INTEGER, period_end INTEGER, sub_ls INTEGER, credits INTEGER, warned INTEGER, ts INTEGER)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS users_seen (user_id TEXT PRIMARY KEY, ts INTEGER)"),
    // Первое появление — для «новых за неделю»; прошлых пользователей берём из настроек.
    env.DB.prepare("INSERT OR IGNORE INTO users_seen (user_id, ts) SELECT user_id, ts FROM prefs"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, user_id TEXT, ts INTEGER, item TEXT, stars INTEGER, sub_exp INTEGER, recurring INTEGER)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS ls_log (user_id TEXT, ts INTEGER, action TEXT, ls INTEGER, cost_usd REAL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS ls_log_ts ON ls_log (ts)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS found_rounds (url TEXT, company TEXT, usd REAL, stage TEXT, date TEXT, investors TEXT, fact TEXT, ts INTEGER, status TEXT, PRIMARY KEY (company, url))"),
  ]);
  // Таблица prefs создавалась раньше без языка и фильтров — дополняем на
  // месте. Повторное добавление колонки D1 отклоняет, это ожидаемо.
  for (const col of ["lang", "audience", "notify", "sectors", "sources", "sens", "niches", "founder"]) {
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

// --- личные настройки и рассылка ----------------------------------------------
// Виды уведомлений. «market» — прежний «trends»: еженедельный отчёт теперь
// про рынок, а не про темы наших находок; старое значение переносится.
// Минимум сообщений (решение владельца 2026-09-29): по умолчанию приходит
// ОДНА короткая сводка в день — новые стартапы и ниши, куда пошли деньги.
// Остальное — по желанию в настройках; вопросы — в чат, бот отвечает сам.
const NOTIFY_KEYS = ["brief", "hot", "digest", "market", "funding", "alerts"];
const NOTIFY_DEFAULT = { brief: true, hot: false, digest: false, market: false, funding: false, alerts: false };
const SOURCES = ["x", "hn", "yc", "gh", "ph"];
// Порог находок: «только сильные» / «обычный» (горячие) / «всё заметное».
const SENS = { strict: 70, normal: 58, wide: 48 };

async function getPrefs(env, uid) {
  const row = await env.DB.prepare("SELECT * FROM prefs WHERE user_id = ?1")
    .bind(String(uid)).first().catch(() => null);
  const j = (v, d) => { try { return v ? JSON.parse(v) : d; } catch { return d; } };
  const arr = (v) => (Array.isArray(v) && v.length ? v : null);
  let n = j(row && row.notify, {});
  if (n.trends === false && n.market === undefined) n.market = false;
  delete n.trends;
  // Настройки до 2026-09-29 (без brief) хранили «всё включено» — это и был
  // поток сообщений, на который жаловался владелец. Переводим на новую схему.
  if (n.brief === undefined) n = {};
  return {
    lang: row && LANGS.includes(row.lang) ? row.lang : null,
    topics: arr(j(row && row.topics, null)),
    audience: arr(j(row && row.audience, null)),
    sectors: arr(j(row && row.sectors, null)),
    niches: arr(j(row && row.niches, null)),
    founder: j(row && row.founder, null),
    sources: arr(j(row && row.sources, null)),
    sens: row && SENS[row.sens] ? row.sens : "normal",
    notify: { ...NOTIFY_DEFAULT, ...n },
    exists: !!row,
  };
}

async function setPrefs(env, uid, patch) {
  const cur = await getPrefs(env, uid);
  const next = { ...cur, ...patch };
  await env.DB.prepare(
    "INSERT OR REPLACE INTO prefs (user_id, topics, ts, lang, audience, notify, sectors, sources, sens, niches, founder) " +
      "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)"
  ).bind(String(uid), JSON.stringify(next.topics || []), Math.floor(Date.now() / 1000), next.lang || null,
    JSON.stringify(next.audience || []), JSON.stringify(next.notify || NOTIFY_DEFAULT),
    JSON.stringify(next.sectors || []), JSON.stringify(next.sources || []), next.sens || "normal",
    JSON.stringify(next.niches || []), next.founder ? JSON.stringify(next.founder) : null).run();
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
const wantsSrc = (src, filter) => !filter || !src || filter.includes(src);

/**
 * Проходит ли находка личные фильтры. Секторы, если выбраны, главнее
 * старых «категорий» (тем ИИ-разметки): категории остались у тех, кто
 * настроил их до 2026-09-28, и работают, пока секторы не выбраны.
 */
function passes(p, f) {
  if (!wantsAud(f.audience, p.audience) || !wantsSrc(f.source, p.sources)) return false;
  if (p.sectors) return wants(f.sectors, p.sectors);
  return wants(f.topics, p.topics);
}

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
    const floor = SENS[p.sens] || SENS.normal;
    if (p.notify.hot !== false) {
      for (const h of body.hot || []) {
        if (!passes(p, h)) continue;
        // Горячее (tier hot) идёт всем, кроме «только сильные» с порогом 70;
        // «заметное» (48–58) — только тем, кто выбрал «всё заметное».
        const score = Number(h.score);
        if (Number.isFinite(score) ? score < floor : false) continue;
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
      // Что уже пришло этому подписчику сразу (заметное при пороге «всё
      // заметное»), в сводку второй раз не кладём.
      const lines = (body.digest.items || []).filter((d) => passes(p, d) && !(d.pushed && Number(d.score) >= floor));
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
    // Раунды за сутки приходят по сектору на блок — одним сообщением на
    // человека: первая рассылка 2026-09-29 прислала по сообщению на сектор.
    const funding = [];
    for (const b of body.broadcast || []) {
      // Раунд в нише — только тем, кто сам нажал «Следить» на этой нише.
      if (typeof b !== "string" && b.kind === "niche") {
        if (!(p.niches || []).includes(b.niche)) continue;
        const r = await tg(env, "sendMessage", { chat_id: uid, text: (b.texts && b.texts[lang]) || b.text, parse_mode: "HTML", disable_web_page_preview: true });
        if (r && r.ok) sent++;
        continue;
      }
      if (typeof b !== "string") {
        const kind = b.kind === "trends" ? "market" : b.kind;
        if (NOTIFY_KEYS.includes(kind) && p.notify[kind] === false) continue;
        // Раунды и сдвиги — только по своим секторам, если они выбраны.
        if ((kind === "funding" || kind === "alert" || kind === "alerts") && p.sectors && !wants(b.sectors, p.sectors)) continue;
        if (kind === "alert" && p.notify.alerts === false) continue;
      }
      const text = typeof b === "string" ? b : (b.texts && b.texts[lang]) || b.text;
      if (!text) continue;
      if (typeof b !== "string" && b.kind === "funding") {
        funding.push(text);
        continue;
      }
      let body = text;
      if (typeof b !== "string" && b.kind === "brief" && p.founder && Object.keys(p.founder).length) {
        const rep = ((marketOf(await loadSnapshot(env))) || {}).report || {};
        const extra = radarText(rep, p.founder, lang, 3);
        if (extra && (body + "\n\n" + extra).length < 3900) body += "\n\n" + extra;
      }
      const r = await tg(env, "sendMessage", { chat_id: uid, text: body, parse_mode: "HTML", disable_web_page_preview: true });
      if (r && r.ok) sent++;
      // Сводка дня — ещё и реплика в разговоре: на «расскажи подробнее про
      // эту нишу» чат должен понимать, о какой нише речь.
      if (r && r.ok && typeof b !== "string" && b.kind === "brief") {
        await env.DB.prepare("INSERT INTO chat_log (user_id, ts, role, text) VALUES (?1, ?2, 'assistant', ?3)")
          .bind(String(uid), Math.floor(Date.now() / 1000), body.replace(/<[^>]+>/g, "").slice(0, 3000)).run().catch(() => null);
      }
    }
    // Склейка по блокам, а не по символам: разрез посреди тега ломает HTML.
    let chunk = "";
    for (const t of [...funding, null]) {
      if (t !== null && (chunk + "\n\n" + t).length <= 3900) { chunk = chunk ? chunk + "\n\n" + t : t; continue; }
      if (chunk) {
        const r = await tg(env, "sendMessage", { chat_id: uid, text: chunk, parse_mode: "HTML", disable_web_page_preview: true });
        if (r && r.ok) sent++;
      }
      chunk = t ? t.slice(0, 3900) : "";
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
  const signed = async (token) => {
    if (!token) return false;
    const secret = await hmac(new TextEncoder().encode("WebAppData"), token);
    const sig = await hmac(secret, check);
    return [...sig].map((b) => b.toString(16).padStart(2, "0")).join("") === hash;
  };
  const viaPublic = await signed(env.LS_BOT_TOKEN);
  // Из служебного бота мини-приложение открывает только владелец.
  const viaAdmin = !viaPublic && (await signed(env.LS_ADMIN_TOKEN));
  if (!viaPublic && !viaAdmin) return null;
  if (Date.now() / 1000 - Number(params.get("auth_date") || 0) > 86400) return null;
  let user = null;
  try {
    user = JSON.parse(params.get("user") || "null");
  } catch {
    return null;
  }
  if (!user || !user.id) return null;
  if (viaAdmin) return isOwner(env, user.id) ? user : null;
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
    env = botEnv(env);
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
      if (url.pathname === "/api/admin") {
        if (!isOwner(env, user.id)) return json({ error: "forbidden" }, 403);
        await ensureTables(env);
        return json(await adminStats(env));
      }
      if (url.pathname === "/api/ls") {
        await ensureTables(env);
        const r = await lsGet(env, user.id);
        return json({ plan: r.plan, sub_ls: r.sub_ls, credits: r.credits, plan_ls: (PLANS[r.plan] || PLANS.free).ls, period_end: r.period_end,
          owner: isOwner(env, user.id), plans: PLANS, stars: STAR_ITEMS, prices: LS_PRICE, pack: CREDIT_PACK });
      }
      if (url.pathname === "/api/buy") {
        const lang = (await getPrefs(env, user.id)).lang || "ru";
        const link = await starsLink(env, user.id, url.searchParams.get("item"), lang);
        return link ? json({ link }) : json({ error: "no link" }, 400);
      }
      if (url.pathname === "/api/idea" && request.method === "GET") {
        await ensureTables(env);
        const f = await findFinding(env, url.searchParams.get("id"));
        if (!f) return json({ error: "Находка уже выпала из свежего среза." }, 404);
        const lang = (await getPrefs(env, user.id)).lang || "ru";
        const res = await ideaCard(env, f, user.id, lang, { allowNew: await lsCanAfford(env, user.id, "idea") });
        if (!res.error && !res.cached) await lsSpend(env, user.id, "idea", 0);
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
            // Старое приложение шлёт trends — это нынешний market.
            const n = { ...body.notify };
            if (n.trends !== undefined && n.market === undefined) n.market = n.trends;
            patch.notify = Object.fromEntries(NOTIFY_KEYS.map((k) => [k, n[k] !== false]));
          }
          const known = (list, ok) => (Array.isArray(list) ? list.map(String).filter(ok).slice(0, 30) : undefined);
          if (Array.isArray(body.sectors)) patch.sectors = known(body.sectors, (x) => /^[a-z_]{2,20}$/.test(x));
          if (Array.isArray(body.sources)) patch.sources = known(body.sources, (x) => SOURCES.includes(x));
          if (Array.isArray(body.niches)) patch.niches = body.niches.map((x) => String(x).slice(0, 60)).slice(0, 30);
          if (SENS[body.sens]) patch.sens = body.sens;
          for (const k of ["sectors", "sources", "niches"]) if (patch[k] && !patch[k].length) patch[k] = null;
          const next = await setPrefs(env, user.id, patch);
          return json({ ok: true, prefs: next });
        }
        const p = await getPrefs(env, user.id);
        return json({ lang: p.lang, topics: p.topics || [], audience: p.audience || [], notify: p.notify,
          sectors: p.sectors || [], sources: p.sources || [], sens: p.sens });
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
      const err = await saveSnapshot(env, raw, Number(data.updated) || Math.floor(Date.now() / 1000));
      // Текст ошибки — в ответ, а не безликий 1101: в логе прогона сразу
      // видно, что именно не так (лимит D1, размер, права).
      if (err) return new Response(`срез не сохранён: ${err}`, { status: 503 });
      return new Response(`принято: ${data.findings.length}${env.SNAP ? " (KV)" : " (D1)"}`);
    }

    // Замер быстрого ответа — только в локальной разработке (wrangler dev
    // --var LS_DEBUG:1): в бою переменной нет, маршрут не существует.
    // Переиндексация матрицы — тоже только в локальной разработке.
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-reindex") {
      const off = Number(url.searchParams.get("off") || 0);
      const { results } = await env.DB.prepare("SELECT niche, name_ru, sector, data FROM niche_matrix ORDER BY niche LIMIT 100 OFFSET ?1").bind(off).all();
      const items = (results || []).map((r) => {
        let d = {};
        try { d = JSON.parse(r.data); } catch { d = {}; }
        const comp = (d.top_6m || []).slice(0, 6).map((c) => `${c.company} (${(c.what || {}).en || ""})`).join("; ");
        return { niche: r.niche, doc: `${r.niche}. ${r.name_ru || ""}. Sector: ${r.sector || ""}. Companies: ${comp}` };
      });
      return json({ off, rows: items.length, indexed: await indexNiches(env, items) });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-emb") {
      const r = await env.AI.run(EMB_MODEL, { text: [url.searchParams.get("q") || "insurance"] });
      const v = (await embed(env, [url.searchParams.get("q") || "insurance"]))[0];
      const q = v ? await env.VEC.query(v, { topK: 5, returnMetadata: "all" }) : null;
      return json({ keys: Object.keys(r || {}), shape: r && r.shape, dim: v ? v.length : null, matches: q && q.matches });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-chat") {
      await ensureTables(env);
      globalThis.__tgCap = [];
      const t0 = Date.now();
      const q = url.searchParams.get("q") || "", mode = url.searchParams.get("mode") || "chat";
      const okA = await fastAnswer(env, "debug", q, url.searchParams.get("lang") || "ru", mode,
        { niche: url.searchParams.get("niche") || null, live: url.searchParams.get("live") === "1" });
      const cap = globalThis.__tgCap.map((c) => ({ ...c, t: c.t - t0 }));
      globalThis.__tgCap = null;
      return json({ ok: okA, total_ms: Date.now() - t0, calls: cap, check: lastUnsupported });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-web") {
      const t0 = Date.now();
      const f = await webFacts(env, url.searchParams.get("q") || "", [url.searchParams.get("q") || ""],
        { model: url.searchParams.get("model"), timeoutMs: 40000 });
      return json({ ms: Date.now() - t0, n: f.length, facts: f, err: JSON.parse((await meta(env, "ai_errors")) || "[]")[0] });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-research") {
      const q = url.searchParams.get("q") || "";
      const plan = await planSearch(env, q);
      return json({ plan, r: await researchDispatch(env, "debug", "ru", q, plan) });
    }
    if (env.LS_DEBUG === "1" && request.method === "POST" && url.pathname === "/debug-brand") {
      // Название и аватар бота (просьба владельца 2026-10-01). Только в отладке.
      const api = (m) => `https://api.telegram.org/bot${env.LS_BOT_TOKEN}/${m}`;
      const name = await fetch(api("setMyName"), { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: url.searchParams.get("name") || "Launch Scout" }) }).then((r) => r.json());
      const fd = new FormData();
      fd.append("photo", JSON.stringify({ type: "static", photo: "attach://avatar" }));
      fd.append("avatar", new Blob([await request.arrayBuffer()], { type: "image/png" }), "avatar.png");
      const photo = await fetch(api("setMyProfilePhoto"), { method: "POST", body: fd }).then((r) => r.json()).catch((e) => ({ error: String(e) }));
      const me = await fetch(api("getMe")).then((r) => r.json());
      return json({ name, photo, me: me.result && { first_name: me.result.first_name, username: me.result.username } });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-send") {
      // Настоящий ответ в Telegram указанному чату (показать оформление владельцу).
      await ensureTables(env);
      const ok = await fastAnswer(env, url.searchParams.get("chat"), url.searchParams.get("q") || "", "ru", "chat", {});
      return json({ ok });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-ls") {
      await ensureTables(env);
      const uid = url.searchParams.get("uid"), op = url.searchParams.get("op");
      if (op === "grant" || op === "credit") { globalThis.__tgCap = []; await lsAdmin(env, "x", `/${op} ${uid} ${url.searchParams.get("arg")}`); globalThis.__tgCap = null; }
      if (op === "spend") return json({ res: await lsSpend(env, uid, url.searchParams.get("arg"), 0), bal: await lsGet(env, uid) });
      if (op === "reset") await env.DB.prepare("DELETE FROM ls_balance WHERE user_id = ?1").bind(uid).run();
      return json({ bal: await lsGet(env, uid), can_live: await lsCanAfford(env, uid, "live") });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-plan") {
      const t0 = Date.now();
      return json({ plan: await planSearch(env, url.searchParams.get("q") || ""), ms: Date.now() - t0 });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-admin") {
      await ensureTables(env);
      return json(await adminStats(env));
    }
    if (env.LS_DEBUG === "1" && request.method === "POST" && url.pathname === "/debug-setup-bot") {
      const which = url.searchParams.get("which") === "admin" ? "admin" : "public";
      if (which === "admin" && !env.LS_ADMIN_TOKEN) return json({ error: "нет LS_PUBLIC_BOT_TOKEN — служебный бот ещё не выделен" }, 400);
      return json(await setupBot(env, which, await request.arrayBuffer()));
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-invoice") {
      return json({ link: await starsLink(env, url.searchParams.get("uid") || "1", url.searchParams.get("item") || "pro", "ru") });
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-adj") {
      globalThis.__tgCap = [];
      await adjacentMsg(env, "debug", "ru", { niche: url.searchParams.get("niche") || "" });
      const cap = globalThis.__tgCap; globalThis.__tgCap = null;
      return json(cap);
    }
    if (env.LS_DEBUG === "1" && url.pathname === "/debug-ask") {
      await ensureTables(env);
      const q = url.searchParams.get("q") || "";
      const mode = url.searchParams.get("mode") || "chat";
      const t0 = Date.now();
      const snap = await loadSnapshot(env);
      const fx = await matrixFacts(env, q, { snap, niche: url.searchParams.get("niche") });
      const t1 = Date.now();
      let first = 0;
      const factText = fx.facts.map((f) => `[F${f.id}] ${f.text}`).join("\n").slice(0, 16000);
      const dbgMsgs = [
        { role: "system", content: FAST_SYSTEM[mode].replace("%LANG%", "Russian") },
        { role: "user", content: `TODAY: ${new Date().toISOString().slice(0, 10)}\nPROFILE: unknown\n\nFACTS:\n${factText}\n\nQUESTION: ${q}` },
      ];
      const onD = async () => { if (!first) first = Date.now(); };
      let full = await streamOpenRouter(env, env.LS_FAST_MODEL || FAST_MODEL, dbgMsgs, onD, 25000, mode === "chat" ? 900 : 1800);
      if (!full) full = await streamOpenRouter(env, FALLBACK_FAST_MODEL, dbgMsgs, onD, 20000, mode === "chat" ? 900 : 1800, 8000);
      const t2 = Date.now();
      const g = groundAnswer(full || "", fx.facts, q);
      return json({ facts_ms: t1 - t0, first_token_ms: first ? first - t0 : null, total_ms: t2 - t0,
        niches: fx.niches.map((d) => d.niche), kept: lastKept, matches: lastMatches, n_facts: fx.facts.length, dropped_lines: g.dropped, unsupported: g.unsupported, final_html: g.html, answer: full, facts: fx.facts });
    }

    if (request.method === "POST" && url.pathname === "/ingest-matrix") {
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      await ensureTables(env);
      const body = await request.json().catch(() => null);
      if (!body || !Array.isArray(body.rows)) return new Response("нет rows", { status: 400 });
      try {
        // Ниши, которых больше нет в матрице (склеены, мусорные), — из базы и индекса.
        const gone = (Array.isArray(body.delete) ? body.delete : []).slice(0, 500);
        if (gone.length) {
          for (let i = 0; i < gone.length; i += 50) {
            const part = gone.slice(i, i + 50);
            await env.DB.prepare(`DELETE FROM niche_matrix WHERE niche IN (${part.map((_, k) => "?" + (k + 1)).join(",")})`).bind(...part).run();
            await env.VEC.deleteByIds(await Promise.all(part.map((n) => vecId(n))));
          }
        }
        return new Response(`принято ниш: ${await ingestMatrix(env, body.rows.slice(0, 100))}, удалено: ${gone.length}`);
      } catch (e) {
        return new Response(`не сохранено: ${String(e).slice(0, 300)}`, { status: 503 });
      }
    }

    // Самообучение: пайплайн забирает найденные чатом раунды и отмечает проверенные.
    if (url.pathname === "/found-rounds") {
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      await ensureTables(env);
      if (request.method === "GET") {
        const { results } = await env.DB.prepare("SELECT * FROM found_rounds WHERE status = 'new' ORDER BY ts LIMIT 50").all();
        return json({ rows: results || [] });
      }
      const body = await request.json().catch(() => null);
      const done = (body && Array.isArray(body.done) ? body.done : []).slice(0, 100);
      if (done.length) {
        await env.DB.batch(done.map((d) => env.DB.prepare("UPDATE found_rounds SET status = ?1 WHERE company = ?2 AND url = ?3")
          .bind(String(d.status || "done").slice(0, 20), String(d.company || ""), String(d.url || ""))));
      }
      return new Response(`отмечено: ${done.length}`);
    }

    // Глубокий поиск: Actions забирает задание по номеру и возвращает находки.
    if (url.pathname === "/research-job" || url.pathname === "/research-result") {
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      if (request.method === "GET") {
        const job = await meta(env, "rjob_" + (url.searchParams.get("id") || ""));
        return job ? new Response(job, { headers: { "content-type": "application/json" } }) : new Response("нет задания", { status: 404 });
      }
      const body = await request.json().catch(() => null);
      if (!body || !body.id) return new Response("нет id", { status: 400 });
      ctx.waitUntil(researchAnswer(env, body).catch((e) => console.log("research:", e)));
      return new Response("ok");
    }

    if (request.method === "POST" && url.pathname === "/ingest-rounds") {
      if (!env.LS_INGEST_SECRET || request.headers.get("x-ingest-secret") !== env.LS_INGEST_SECRET) {
        return new Response("нет", { status: 403 });
      }
      await ensureTables(env);
      const body = await request.json().catch(() => null);
      if (!body || !Array.isArray(body.rows)) return new Response("нет rows", { status: 400 });
      try {
        return new Response(`принято раундов: ${await ingestRounds(env, body.rows.slice(0, 500))}`);
      } catch (e) {
        return new Response(`не сохранено: ${String(e).slice(0, 300)}`, { status: 503 });
      }
    }

    if (request.method === "POST" && url.pathname === "/tg-admin") {
      if (request.headers.get("x-telegram-bot-api-secret-token") !== env.LS_WEBHOOK_SECRET || !env.LS_ADMIN_TOKEN) {
        return new Response("нет", { status: 403 });
      }
      const update = await request.json();
      ctx.waitUntil(handleAdminUpdate(env, update).catch((e) => console.log("admin:", e)));
      return new Response("ok");
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
    env = botEnv(env);
    const now = Math.floor(Date.now() / 1000);
    const err = await dispatchRun(env, { digest: "auto" });
    await setMeta(env, "last_dispatch_error", err || "");
    await watchdog(env, now);
    // Расход OpenRouter на начало месяца — для затрат в дашборде; при остатке
    // меньше $3 — напоминание владельцу раз в сутки (иначе бот замолчит).
    const orc = await orCredits(env).catch(() => null);
    await orMonthMark(env, orc).catch(() => null);
    const day = new Date().toISOString().slice(0, 10);
    if (orc && orc.total - orc.used < 3 && (await meta(env, "or_low_alert")) !== day) {
      await setMeta(env, "or_low_alert", day);
      await ownerNotify(env, { text: `⚠️ На OpenRouter осталось $${(orc.total - orc.used).toFixed(2)} — пополните, иначе ИИ-ответы остановятся.` });
    }
    await tokenReminder(env, now);
    await setupCommands(env);
  },
};
