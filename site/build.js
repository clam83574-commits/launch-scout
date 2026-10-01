// Сборка: данные пульса и готовая разметка прямо в HTML — страница полная даже без скриптов.
const fs = require("fs");
const src = fs.readFileSync("src.html", "utf8"), js = fs.readFileSync("site.js", "utf8");
const pulse = fs.readFileSync("pulse.json", "utf8").trim(), DATA = JSON.parse(pulse);
const render = js.slice(js.indexOf("/*RENDER-START*/"), js.indexOf("/*RENDER-END*/"));
const R = new Function("DATA", render + "; return { tickerHTML, chartHTML, chatHTML, srcsHTML, matrixHTML };")(DATA);
let out = src.replace("<!--TICKER-->", R.tickerHTML(DATA)).replace("<!--CHART-->", R.chartHTML(DATA))
  .replace("<!--CHAT-->", R.chatHTML(0)).replace("<!--SRCS-->", R.srcsHTML()).replace("<!--MATRIX-->", R.matrixHTML())
  .replace("<!--SCRIPT-->", "<script>\n" + js.replace("__PULSE__", pulse) + "</script>\n");
const m = out.match(/<script>\n([\s\S]*?)<\/script>/); new Function(m[1]);   // проверка синтаксиса
fs.writeFileSync("index.html", out);
fs.writeFileSync("deploy/index.html", "<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n" +
  out.replace(/(<\/style>)/, "$1\n</head>\n<body>") + "</body>\n</html>\n");
console.log("ok", out.length);
