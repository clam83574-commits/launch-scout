// Сборка сайта на трёх языках: deploy/ (ru), deploy/kk/, deploy/en/.
// Данные пульса и готовая разметка — прямо в HTML: страница полная даже без скриптов.
// Переводы — i18n.json: html (русская строка → kk, en) и js (словари скрипта).
const fs = require("fs");
const src = fs.readFileSync("src.html", "utf8"), js = fs.readFileSync("site.js", "utf8");
const pulse = fs.readFileSync("pulse.json", "utf8").trim(), DATA = JSON.parse(pulse);
const T = JSON.parse(fs.readFileSync("i18n.json", "utf8"));
const render = js.slice(js.indexOf("/*RENDER-START*/"), js.indexOf("/*RENDER-END*/"));
const SITE = "https://launch-scout-site.pages.dev";
const norm = (x) => x.replace(/&nbsp;/g, " ").replace(/ /g, " ").replace(/\s+/g, " ").trim();
const escAttr = (x) => x.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");

function translate(html, li) {
  if (li < 0) return html;
  const dict = new Map(T.html.map((r) => [norm(r[0]), r[li]]));
  const miss = new Set();
  // атрибуты: подписи, описания, data-src в схеме проверки
  html = html.replace(/(\s(?:aria-label|title|alt|content|data-src)=")([^"]*[А-Яа-яЁё][^"]*)(")/g, (m, a, v, b) => {
    const t = dict.get(norm(v));
    if (t === undefined) { miss.add(v); return m; }
    return a + escAttr(t) + b;
  });
  // текст между тегами (кроме script/style)
  const parts = html.split(/(<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>)/);
  for (let i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i].replace(/>([^<>]*[А-Яа-яЁё][^<>]*)</g, (m, text) => {
      const t = dict.get(norm(text));
      if (t === undefined) { miss.add(norm(text)); return m; }
      const lead = /^\s/.test(text) ? " " : "", tail = /\s$/.test(text) ? " " : "";
      return ">" + lead + t + tail + "<";
    });
  }
  html = parts.join("");
  return { html, miss: [...miss] };
}

const out = {};
for (const [li, lang] of [[-1, "ru"], [0, "kk"], [1, "en"]].map(([i, l]) => [i < 0 ? -1 : i + 1, l])) {
  const I = T.js[lang], M = T.meta[lang];
  const R = new Function("DATA", "I", render + "; return { tickerHTML, chartHTML, chatHTML, srcsHTML, matrixHTML, usd, nf, MON };")(DATA, I);
  let html = src;
  // живые цифры и даты — сразу в формате языка
  const kv = { usd90: R.usd(DATA.usd90), rounds90: R.nf(DATA.rounds90), countries: R.nf(DATA.countries), niches: R.nf(DATA.niches) };
  html = html.replace(/(data-k="(\w+)"[^>]*>)([^<]*)(<)/g, (m, a, k, v, b) => (kv[k] !== undefined ? a + kv[k] + b : m));
  const d = new Date(DATA.ts * 1000);
  html = html.replace(/(<span id="liveTxt">)[^<]*(<)/, `$1${I.rev} ${String(d.getUTCDate()).padStart(2, "0")}.${String(d.getUTCMonth() + 1).padStart(2, "0")}.${d.getUTCFullYear()}$2`)
    .replace(/(<span id="upd">)[^<]*(<)/, `$1${I.date.replace("{d}", d.getUTCDate()).replace("{m}", R.MON[d.getUTCMonth()]).replace("{y}", d.getUTCFullYear())}$2`);
  html = html.replace("<!--TICKER-->", R.tickerHTML(DATA)).replace("<!--CHART-->", R.chartHTML(DATA))
    .replace("<!--CHAT-->", R.chatHTML(0)).replace("<!--SRCS-->", R.srcsHTML()).replace("<!--MATRIX-->", R.matrixHTML());
  const tr = translate(html, li);
  if (li >= 0) { html = tr.html; if (lang === "en" && tr.miss.length) console.log("[en] без перевода:", tr.miss); }
  // превью и язык страницы
  html = html.replace(`content="${SITE}/"`, `content="${SITE}${M.path}"`)
    .replace(/content="https:\/\/launch-scout-site\.pages\.dev\/og\.png\?v=2"/g, `content="${SITE}/${M.og}"`)
    .replace('content="ru_RU"', `content="${M.locale}"`)
    .replace(`data-l="${lang}"`, `data-l="${lang}" class="on"`);
  const alt = ["ru", "kk", "en"].map((l) => `<link rel="alternate" hreflang="${l}" href="${SITE}${T.meta[l].path}">`).join("\n");
  html = html.replace("<style>", alt + "\n<style>");
  html = html.replace("<!--SCRIPT-->", "<script>\n" + js.replace("__PULSE__", pulse).replace("__I18N__", JSON.stringify(I)) + "</script>\n");
  const m = html.match(/<script>\n([\s\S]*?)<\/script>/); new Function(m[1]);   // проверка синтаксиса
  out[lang] = html;
}
fs.writeFileSync("index.html", out.ru);   // для превью-артефакта (без обёртки)
const wrap = (h, lang) => `<!doctype html>\n<html lang="${lang}">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n` +
  h.replace(/(<\/style>)/, "$1\n</head>\n<body>") + "</body>\n</html>\n";
for (const lang of ["ru", "kk", "en"]) {
  const dir = lang === "ru" ? "deploy" : `deploy/${lang}`;
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(`${dir}/index.html`, wrap(out[lang], lang));
}
console.log("ok", Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.length])));

// OG-картинки на каждом языке: og.html → og_<lang>.html → PNG (Chrome без окна).
const { execFileSync } = require("child_process");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
if (process.argv.includes("--og")) {
  for (const [li, lang] of [[0, "ru"], [1, "kk"], [2, "en"]]) {
    const I = T.js[lang];
    let h = fs.readFileSync("og.html", "utf8").replace("__PULSE__", pulse);
    if (li) h = translate(h, li).html;
    h = h.replace('Math.round(D.usd90 / 1e9) + " млрд"', `Math.round(D.usd90 / 1e9) + ${JSON.stringify(I.bn)}`)
      .replace('/g, " ");', `/g, ${JSON.stringify(I.thou)});`);
    fs.writeFileSync(`og_${lang}.html`, h);
    const png = require("path").resolve(lang === "ru" ? "deploy/og.png" : `deploy/og_${lang}.png`);
    execFileSync(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", "--virtual-time-budget=6000",
      "--window-size=1200,630", "--screenshot=" + png, "file:///" + require("path").resolve(`og_${lang}.html`).split(require("path").sep).join("/")], { stdio: "ignore" });
    fs.unlinkSync(`og_${lang}.html`);
    console.log("og", lang, png);
  }
}
