(() => {
  // Анимации — часть подачи: показываем их и при системном «уменьшить движение».
  const reduce = false;
  let DATA = __PULSE__;
  const PULSE_URL = "https://launch-scout-bot.clam83574.workers.dev/public/pulse";
  // Каждая часть страницы отдельно: сбой одной не гасит остальные.
  const safe = (name, fn) => { try { fn(); } catch (e) { console.error(name, e); } };

/*RENDER-START*/
  const SECTOR_RU = { ai_infra: "ИИ-инфраструктура", energy: "Энергетика", health: "Медицина", ai_agents: "ИИ-агенты", defense_space: "Оборона и космос",
    hardware: "Железо", mobility: "Транспорт", fintech: "Финтех", security: "Кибербез", b2b_saas: "B2B SaaS", proptech: "Недвижимость",
    commerce: "E-commerce", crypto: "Крипто", consumer: "Потребительские", devtools: "DevTools", edu: "Образование" };
  const COUNTRY_RU = { US: "США", DE: "Германия", IN: "Индия", KR: "Корея", PL: "Польша", BG: "Болгария", AU: "Австралия", TW: "Тайвань", RU: "Россия",
    GB: "Великобритания", UK: "Великобритания", FR: "Франция", KZ: "Казахстан", UZ: "Узбекистан", CN: "Китай", JP: "Япония", SG: "Сингапур", IL: "Израиль",
    CA: "Канада", NL: "Нидерланды", ES: "Испания", SE: "Швеция", BR: "Бразилия", AE: "ОАЭ", CH: "Швейцария", FI: "Финляндия", IT: "Италия", ID: "Индонезия" };
  const STAGE = { "pre-seed": "Pre-seed", seed: "Seed", a: "Series A", b: "Series B", "c+": "Series C+" };
  const MON = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  const nf = (n) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  const usd = (v) => v >= 1e9 ? "$" + (v / 1e9).toFixed(v >= 1e11 ? 0 : 1).replace(".", ",").replace(",0", "") + " млрд"
    : "$" + (v / 1e6).toFixed(v >= 1e8 ? 0 : 1).replace(".", ",").replace(",0", "") + " млн";
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const SOURCES = ["X", "Hacker News", "Y Combinator", "GitHub", "Product Hunt", "Google News", "TechCrunch", "Crunchbase News", "EU-Startups",
    "SEC Form D", "PR Newswire", "GlobeNewswire", "Business Wire", "Astana Hub", "Devpost", "HN Hiring", "Google Trends"];
  const DEMOS = [
    { q: "Куда идут деньги в ИИ-агентах?", a: [
      ["v", "<b>Горячо.</b> 148 раундов и $11,5 млрд за 90 дней — четвёртое место из 16 секторов."],
      ["q", "coin", "Свежие сделки", "Flow Engineering — $50 млн, Series B · Medow Health AI — $2,3 млн, Seed"],
      ["q", "compass", "Где свободнее", "Агенты под одну отрасль: недвижимость, медицина, юристы"],
      ["q", "target", "Первый шаг", "Выберите отрасль и проверьте идею командой /check"] ] },
    { q: "/check ИИ-ассистент для частных клиник в Казахстане", a: [
      ["v", "<b>Делать, но узко.</b> Медицина — самый частый сектор: 278 раундов за 90 дней."],
      ["q", "coin", "Деньги в нише есть", "Alma — $17 млн, Series A · Aleph Surgery — $7,5 млн, Pre-seed"],
      ["q", "alert", "Что против", "В СНГ за 90 дней — 1 раунд в медицине. Платить будут клиники, а продажи им долгие"],
      ["q", "flask", "Проверка за 7 дней", "10 интервью с главврачами частных сетей и лендинг с ценой"] ] },
    { q: "Кто конкуренты в proptech?", a: [
      ["v", "<b>Ниша не перегрета:</b> 43 раунда и $1,9 млрд за 90 дней."],
      ["q", "flag", "Кто привлёк", "Kuro (Германия) — $10,8 млн, Seed · Zaritalk (Корея) — $10,7 млн, Series A · Quotr (США) — $4 млн, Seed"],
      ["q", "globe", "Где пусто", "В СНГ за 90 дней — ни одного раунда в proptech"],
      ["q", "bulb", "Идея", "Повторить рабочую модель из Кореи или Германии для своего города"] ] },
  ];
  // В переписке — эмодзи, как в самом Telegram; свои иконки — только на сайте вокруг.
  const EMO = { coin: "💸", compass: "🧭", target: "🎯", alert: "⚠️", flask: "🧪", flag: "🏁", globe: "🌍", bulb: "💡", search: "🔎", map: "🗺", bell: "🔔" };
  const ico = (id) => (EMO[id] || "") + " ";
  const KB = `<div class="kb"><span>${ico("search")}Глубже</span><span>${ico("map")}Карта идеи</span><span>${ico("compass")}Смежные ниши</span><span>${ico("flag")}Конкуренты</span><span>${ico("bell")}Следить</span><span>🌐 Искать везде</span></div>`;
  function tickerHTML(D) {
    const items = (D.recent || []).filter((r) => r.c && r.usd).map((r) => {
      const d = new Date(r.ts * 1000);
      const meta = [STAGE[r.stage] || "", SECTOR_RU[r.s] || "", COUNTRY_RU[r.country] || r.country || "", d.getUTCDate() + " " + MON[d.getUTCMonth()]].filter(Boolean).join(" · ");
      return `<div class="it"><b>${esc(r.c)}</b><span class="usd">${usd(r.usd)}</span><span class="meta">${esc(meta)}</span></div>`;
    }).join("");
    return items + items;
  }
  function chartHTML(D) {
    const secs = (D.sectors || []).filter((s) => SECTOR_RU[s.s]);
    const max = Math.max(...secs.map((s) => s.usd));
    return secs.map((s, i) => `<div class="row" title="${nf(s.n)} раундов"><span class="nm">${SECTOR_RU[s.s]}</span>` +
      `<span class="tr"><i style="--w:${(s.usd / max * 100).toFixed(1)}%;--d:${(i * 0.05).toFixed(2)}s"></i></span>` +
      `<span class="v">${usd(s.usd)}<span class="cnt">${nf(s.n)} раундов</span></span></div>`).join("") +
      `<div class="foot mono"><span>Сумма раундов за 90 дней</span><span>источник: база Launch Scout</span></div>`;
  }
  function chatHTML(i) {
    const d = DEMOS[i];
    return `<div class="msg me">${esc(d.q)}</div><div class="msg bot">` + d.a.map((p) => p[0] === "v"
      ? `<span class="verdict">${p[1]}</span>` : `<blockquote><b>${ico(p[1])}${p[2]}</b>${p[3]}</blockquote>`).join("") + `</div>` + KB;
  }
  const srcsHTML = () => SOURCES.map((s, i) => `<span style="--d:${(i * .29).toFixed(2)}s">${s}</span>`).join("");
  const matrixHTML = () => "<i></i>".repeat(40);
/*RENDER-END*/

  // ---------- живые цифры ----------
  function bind() {
    const map = { usd90: usd(DATA.usd90), rounds90: nf(DATA.rounds90), countries: nf(DATA.countries), niches: nf(DATA.niches) };
    document.querySelectorAll("[data-k]").forEach((el) => { const v = map[el.dataset.k]; if (v) el.textContent = v; });
    const d = new Date(DATA.ts * 1000);
    document.getElementById("upd").textContent = d.getDate() + " " + MON[d.getMonth()] + " " + d.getFullYear();
    document.getElementById("liveTxt").textContent = "Рев. " + String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear();
    document.getElementById("track").innerHTML = tickerHTML(DATA);
    document.getElementById("chart").innerHTML = chartHTML(DATA);
  }
  safe("data", () => {
    fetch(PULSE_URL).then((r) => r.ok ? r.json() : null).then((j) => { if (j && j.rounds90) { DATA = j; safe("bind", bind); } }).catch(() => {});
  });

  // ---------- промокод из ссылки (?promo=aipreneurs) ----------
  safe("promo", () => {
    const code = (new URLSearchParams(location.search).get("promo") || "").toLowerCase();
    if (!["aipreneurs", "tomorrowschool", "digitalbridge"].includes(code)) return;   // только известные коды: иначе плашка обещала бы скидку, которой нет
    document.querySelectorAll('a[href*="t.me/Launch_Scout_bot"]').forEach((a) => { a.href = "https://t.me/Launch_Scout_bot?start=" + code; });
    document.getElementById("promoCode").textContent = code.toUpperCase();
    document.getElementById("promoNote").hidden = false;
  });

  // ---------- прожектор под курсором ----------
  safe("spot", () => {
    const spot = document.getElementById("spot");
    if (!reduce) addEventListener("pointermove", (e) => { spot.style.setProperty("--mx", e.clientX + "px"); spot.style.setProperty("--my", e.clientY + "px"); }, { passive: true });
  });

  // ---------- было/стало и график играют, когда видны ----------
  safe("play", () => {
    if (reduce) return;
    const io = new IntersectionObserver((ents) => ents.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("play"); io.unobserve(en.target); }
    }), { threshold: 0.25 });
    document.querySelectorAll(".vs article, #chart").forEach((el) => io.observe(el));
  });

  // ---------- таймер прогона ----------
  safe("clock", () => {
    const clock = document.getElementById("clock");
    const tick = () => { const d = new Date(), left = 600 - ((d.getMinutes() % 10) * 60 + d.getSeconds());
      clock.textContent = "СЛЕД. ПРОГОН " + String(Math.floor(left / 60)).padStart(2, "0") + ":" + String(left % 60).padStart(2, "0"); };
    tick(); setInterval(tick, 1000);
  });

  // ---------- живая матрица ниш: вспышки разной силы, затухание разной длины ----------
  safe("matrix", () => {
    const cells = [...document.querySelectorAll("#matrix i")], N = cells.length, COLS = 8;
    const base = cells.map(() => 0.03 + Math.random() * 0.1);
    const v = base.slice(), decay = cells.map(() => 0.5);
    const set = (i) => cells[i].style.setProperty("--o", v[i].toFixed(3));
    cells.forEach((_, i) => { v[i] = Math.random() < 0.25 ? 0.2 + Math.random() * 0.6 : base[i]; decay[i] = 0.2 + Math.random(); set(i); });
    if (reduce) return;
    const ignite = (i, peak, rate) => { if (peak > v[i]) { v[i] = peak; decay[i] = rate; } };
    let vis = false, last = performance.now(), acc = 0;
    new IntersectionObserver(([en]) => { vis = en.isIntersecting; }).observe(document.getElementById("matrix"));
    (function loop(now) {
      const dt = Math.min((now - last) / 1000, 0.05); last = now;
      if (vis) {
        acc += dt * (4 + Math.random() * 6);              // 4–10 вспышек в секунду, неравномерно
        while (acc >= 1) {
          acc -= 1;
          const i = Math.floor(Math.random() * N), peak = 0.3 + Math.pow(Math.random(), 0.6) * 0.7, rate = 0.18 + Math.random() * 1.1;
          ignite(i, peak, rate);
          if (peak > 0.75) {                               // сильная вспышка задевает соседей
            const r = Math.floor(i / COLS), c = i % COLS;
            [[0, 1], [0, -1], [1, 0], [-1, 0]].forEach(([dr, dc]) => {
              const rr = r + dr, cc = c + dc;
              if (rr >= 0 && cc >= 0 && cc < COLS && rr * COLS + cc < N) ignite(rr * COLS + cc, peak * (0.25 + Math.random() * 0.25), rate * 1.4);
            });
          }
        }
        for (let i = 0; i < N; i++) {
          if (v[i] > base[i]) { v[i] = Math.max(base[i], v[i] - v[i] * decay[i] * dt - 0.02 * dt); set(i); }
        }
      }
      requestAnimationFrame(loop);
    })(last);
  });

  // ---------- телефон: плавный доворот к курсору и лёгкое парение ----------
  safe("phone", () => {
    const wrap = document.querySelector(".phone-wrap"), phone = document.querySelector(".phone");
    const narrow = () => innerWidth < 900;
    let hover = false, mx = 0.5, my = 0.5, rx = 5, ry = -12, last = performance.now(), t = 0;
    wrap.addEventListener("pointerenter", () => { hover = true; });
    wrap.addEventListener("pointerleave", () => { hover = false; });
    wrap.addEventListener("pointermove", (e) => { const r = wrap.getBoundingClientRect(); mx = (e.clientX - r.left) / r.width; my = (e.clientY - r.top) / r.height; });
    const apply = () => { phone.style.transform = `translateY(${(Math.sin(t * 0.8) * 5).toFixed(2)}px) rotateX(${rx.toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg)`; };
    if (reduce) { apply(); return; }
    (function loop(now) {
      const dt = Math.min((now - last) / 1000, 0.05); last = now; t += dt;
      const n = narrow();
      if (innerWidth < 600) { phone.style.transform = "none"; requestAnimationFrame(loop); return; }   // на телефоне без наклона
      const tx = hover ? -(my - 0.5) * 6 : (n ? 3 : 5);
      const ty = hover ? (mx - 0.5) * 9 : (n ? -6 : -12) + Math.sin(t * 0.5) * 1.5;
      const k = 1 - Math.exp(-dt * (hover ? 3.2 : 1.8));  // мягкое приближение без рывков
      rx += (tx - rx) * k; ry += (ty - ry) * k;
      apply();
      requestAnimationFrame(loop);
    })(last);
  });

  // ---------- демо-переписка ----------
  safe("demo", () => {
    const chat = document.getElementById("chat"), status = document.getElementById("tgStatus");
    const tabs = [...document.querySelectorAll(".tab")];
    const plain = (s) => s.replace(/<[^>]+>/g, "");
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    let run = 0, autoT = null, played = false;
    async function typeInto(el, html, id) {
      const txt = plain(html);
      el.classList.add("caret");
      for (let k = 1; k <= txt.length; k += 6) { if (id !== run) return false; el.textContent = txt.slice(0, k); await sleep(10); }
      el.classList.remove("caret"); el.innerHTML = html; return true;
    }
    async function play(i) {
      const id = ++run;
      tabs.forEach((t, k) => { t.setAttribute("aria-selected", k === i ? "true" : "false"); t.querySelector(".prog").classList.remove("run"); });
      const d = DEMOS[i];
      chat.innerHTML = `<div class="msg me">${esc(d.q)}</div>`;
      status.textContent = "печатает…";
      const typing = document.createElement("div"); typing.className = "msg bot typing"; typing.innerHTML = "<i></i><i></i><i></i>"; chat.appendChild(typing);
      await sleep(380); if (id !== run) return;
      typing.remove();
      const bot = document.createElement("div"); bot.className = "msg bot"; chat.appendChild(bot);
      for (const p of d.a) {
        if (p[0] === "v") { const s = document.createElement("span"); s.className = "verdict"; bot.appendChild(s); if (!(await typeInto(s, p[1], id))) return; }
        else { const b = document.createElement("blockquote"), h = document.createElement("b"), tx = document.createElement("span");
          h.innerHTML = ico(p[1]) + esc(p[2]); b.append(h, tx); bot.appendChild(b); if (!(await typeInto(tx, p[3], id))) return; }
        await sleep(50);
      }
      chat.insertAdjacentHTML("beforeend", KB.replace('class="kb"', 'class="kb wait"'));
      await sleep(60); const kb = chat.querySelector(".kb"); if (kb) kb.classList.remove("wait");
      status.textContent = "венчурный радар";
      tabs[i].style.setProperty("--dur", "6.3s");
      const prog = tabs[i].querySelector(".prog"); void prog.offsetWidth; prog.classList.add("run");
      clearTimeout(autoT); autoT = setTimeout(() => { if (id === run) play((i + 1) % DEMOS.length); }, 6300);
    }
    tabs.forEach((t, i) => t.addEventListener("click", () => { clearTimeout(autoT); play(i); }));
    if (!reduce) new IntersectionObserver(([en], o) => { if (en.isIntersecting && !played) { played = true; o.disconnect(); play(0); } }, { threshold: 0.35 }).observe(chat);
  });

  // ---------- сканер проверки ----------
  safe("scan", () => {
    const scan = document.getElementById("scan"), beam = document.getElementById("beam"), rows = [...scan.querySelectorAll(".srow")];
    const steps = [...document.querySelectorAll("#steps3 li")], scanState = document.getElementById("scanState");
    const setStep = (k) => steps.forEach((s, i) => s.classList.toggle("on", i === k));
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    async function loop() {
      for (;;) {
        rows.forEach((r) => { r.className = "srow"; r.querySelector(".st").textContent = "…"; });
        setStep(0); scanState.textContent = "черновик"; beam.style.opacity = 0;
        await sleep(1600);
        setStep(1); scanState.textContent = "проверка";
        for (const r of rows) {
          beam.style.opacity = 1; beam.style.top = (r.offsetTop + r.offsetHeight / 2) + "px";
          r.classList.add("looking"); r.querySelector(".st").textContent = "ищу источник…";
          await sleep(1100);
          r.classList.remove("looking"); r.classList.add(r.dataset.res); r.querySelector(".st").textContent = r.dataset.src;
          await sleep(350);
        }
        beam.style.opacity = 0; setStep(2); scanState.textContent = "готово";
        await sleep(500); rows[2].classList.add("gone");
        await sleep(4200);
      }
    }
    if (!reduce) new IntersectionObserver(([en], o) => { if (en.isIntersecting) { o.disconnect(); loop(); } }, { threshold: 0.4 }).observe(scan);
  });

  // ---------- 3D: карта денег, съёмка «с дрона», радар с переменной скоростью ----------
  safe("radar", () => {
    if (!window.THREE) return;
    const stage = document.getElementById("stage"), canvas = document.getElementById("radar");
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    const BLUE = new THREE.Color("#1d4fd8"), INK = new THREE.Color("#0f1d3a"), GRID = new THREE.Color("#c9d5ea"), WHITE = new THREE.Color("#ffffff"), SOFT = new THREE.Color("#dfe8fd");
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(32, 1, 0.1, 200), world = new THREE.Group(); scene.add(world);
    scene.fog = new THREE.Fog(0xffffff, 30, 52);   // дальний край карты растворяется в бумаге

    const grid = new THREE.GridHelper(30, 30, GRID, GRID); grid.material.transparent = true; grid.material.opacity = 0.55; world.add(grid);
    const ringMat = new THREE.LineDashedMaterial({ color: BLUE, dashSize: 0.25, gapSize: 0.18, transparent: true, opacity: 0.45 });
    const circle = (r, y) => { const pts = []; for (let i = 0; i <= 160; i++) { const a = i / 160 * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a) * r, y, Math.sin(a) * r)); } return new THREE.BufferGeometry().setFromPoints(pts); };
    [3.5, 7, 10.5].forEach((r) => { const l = new THREE.Line(circle(r, 0.01), ringMat); l.computeLineDistances(); world.add(l); });
    // шкала азимута по внешнему кольцу, как на лимбе
    const ticks = []; for (let i = 0; i < 72; i++) { const a = i / 72 * Math.PI * 2, r1 = 10.5, r2 = i % 6 ? 10.8 : 11.2;
      ticks.push(new THREE.Vector3(Math.cos(a) * r1, .01, Math.sin(a) * r1), new THREE.Vector3(Math.cos(a) * r2, .01, Math.sin(a) * r2)); }
    world.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(ticks), new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.35 })));
    world.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-12, .01, 0), new THREE.Vector3(12, .01, 0), new THREE.Vector3(0, .01, -12), new THREE.Vector3(0, .01, 12)]),
      new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.25 })));

    // луч: шлейф из сегментов с затуханием + вертикальная «шторка» сканера
    const sweepPivot = new THREE.Group(); world.add(sweepPivot);
    const SEG = 14, segL = 0.06;
    for (let k = 0; k < SEG; k++) {
      const m = new THREE.Mesh(new THREE.CircleGeometry(10.5, 24, k * segL, segL + 0.002),
        new THREE.MeshBasicMaterial({ color: BLUE, transparent: true, opacity: 0.2 * Math.pow(1 - k / SEG, 1.6), side: THREE.DoubleSide, depthWrite: false }));
      m.rotation.x = -Math.PI / 2; m.position.y = 0.02 + k * 0.0005; sweepPivot.add(m);
    }
    sweepPivot.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, .03, 0), new THREE.Vector3(10.5, .03, 0)]), new THREE.LineBasicMaterial({ color: BLUE })));
    const cv = document.createElement("canvas"); cv.width = 256; cv.height = 128;
    const cx = cv.getContext("2d"), gv = cx.createLinearGradient(0, 128, 0, 0);
    gv.addColorStop(0, "rgba(29,79,216,0.30)"); gv.addColorStop(0.5, "rgba(29,79,216,0.08)"); gv.addColorStop(1, "rgba(29,79,216,0)");
    cx.fillStyle = gv; cx.fillRect(0, 0, 256, 128);
    const gh = cx.createLinearGradient(0, 0, 256, 0); gh.addColorStop(0, "rgba(255,255,255,1)"); gh.addColorStop(0.15, "rgba(255,255,255,0)");
    cx.globalCompositeOperation = "destination-out"; cx.fillStyle = gh; cx.fillRect(0, 0, 256, 128);
    const curtain = new THREE.Mesh(new THREE.PlaneGeometry(10.5, 6.5), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(cv), transparent: true, side: THREE.DoubleSide, depthWrite: false }));
    curtain.position.set(5.25, 3.25, 0); sweepPivot.add(curtain);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.13, 12, 12), new THREE.MeshBasicMaterial({ color: BLUE })); tip.position.set(10.5, 0.05, 0); sweepPivot.add(tip);

    // столбики-макеты: высота = деньги сектора за 90 дней
    const secs = (DATA.sectors || []).filter((s) => SECTOR_RU[s.s]);
    const maxU = Math.max(...secs.map((s) => s.usd));
    const boxGeo = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0), edgeGeo = new THREE.EdgesGeometry(boxGeo);
    const sq = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-.5, 0, -.5), new THREE.Vector3(.5, 0, -.5), new THREE.Vector3(.5, 0, .5), new THREE.Vector3(-.5, 0, .5), new THREE.Vector3(-.5, 0, -.5)]);
    const bars = secs.map((s, i) => {
      const h = 0.6 + Math.sqrt(s.usd / maxU) * 5.4;
      const ring = i % 2 ? 7.8 : 4.3 + (i % 3) * 0.95, a = i / secs.length * Math.PI * 2 + 0.3;
      const g = new THREE.Group(); g.position.set(Math.cos(a) * ring, 0, Math.sin(a) * ring);
      const fillMat = new THREE.MeshBasicMaterial({ color: WHITE.clone(), transparent: true, opacity: 0.94, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      const fill = new THREE.Mesh(boxGeo, fillMat), edgeMat = new THREE.LineBasicMaterial({ color: INK.clone() }), edges = new THREE.LineSegments(edgeGeo, edgeMat);
      fill.scale.set(.62, h, .62); edges.scale.set(.62, h, .62); g.add(fill, edges);
      const ping = new THREE.Line(circle(1, .02), new THREE.LineBasicMaterial({ color: BLUE, transparent: true, opacity: 0 })); g.add(ping);
      const scanSq = new THREE.Line(sq, new THREE.LineBasicMaterial({ color: BLUE, transparent: true, opacity: 0 })); scanSq.scale.set(.8, 1, .8); g.add(scanSq);
      world.add(g);
      return { s, h, a: Math.atan2(g.position.z, g.position.x), g, fill, fillMat, edges, edgeMat, ping, scanSq, scanT: 9, heat: 0, phase: Math.random() * 6 };
    });

    // источники по краю и сигналы от них
    const SRC_N = 17, srcPos = [], srcMat = new THREE.MeshBasicMaterial({ color: INK });
    for (let i = 0; i < SRC_N; i++) { const a = i / SRC_N * Math.PI * 2, p = new THREE.Vector3(Math.cos(a) * 12.3, 0, Math.sin(a) * 12.3); srcPos.push(p);
      const m = new THREE.Mesh(new THREE.OctahedronGeometry(0.24), srcMat); m.position.copy(p).setY(0.3); world.add(m); }
    const PK = 18, pkArr = new Float32Array(PK * 3), pkGeo = new THREE.BufferGeometry(); pkGeo.setAttribute("position", new THREE.BufferAttribute(pkArr, 3));
    world.add(new THREE.Points(pkGeo, new THREE.PointsMaterial({ color: BLUE, size: 0.24 })));
    const trailMat = new THREE.LineBasicMaterial({ color: BLUE, transparent: true, opacity: 0.1 });
    function newPacket(p) {
      const from = srcPos[Math.floor(Math.random() * SRC_N)], bar = bars[Math.floor(Math.random() * bars.length)];
      const to = bar.g.position.clone(); to.y = bar.h;
      const mid = from.clone().add(to).multiplyScalar(0.5); mid.y = 3 + Math.random() * 3.5;
      p.curve = new THREE.QuadraticBezierCurve3(from.clone().setY(0.3), mid, to); p.t = -Math.random() * 2; p.speed = 0.16 + Math.random() * 0.14; p.bar = bar; return p;
    }
    const packets = []; for (let i = 0; i < PK; i++) packets.push(newPacket({}));
    for (let i = 0; i < 10; i++) world.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(newPacket({}).curve.getPoints(40)), trailMat));

    const tags = bars.map((b, i) => { const el = document.createElement("div"); el.className = "tag3d";
      if (i >= 4) el.classList.add("sm");
      el.innerHTML = `${SECTOR_RU[b.s.s]} <b>${usd(b.s.usd)}</b>`; stage.appendChild(el); return { el, bar: b, w: 0, h: 0 }; });

    function resize() { const w = stage.clientWidth, h = stage.clientHeight; renderer.setSize(w, h, false); camera.aspect = w / h; camera.fov = w < 500 ? 40 : 32; camera.updateProjectionMatrix(); }
    new ResizeObserver(resize).observe(stage); resize();
    let px = 0, py = 0, tx = 0, ty = 0;
    stage.addEventListener("pointermove", (e) => { const r = stage.getBoundingClientRect(); tx = (e.clientX - r.left) / r.width - 0.5; ty = (e.clientY - r.top) / r.height - 0.5; });
    stage.addEventListener("pointerleave", () => { tx = 0; ty = 0; });
    const v = new THREE.Vector3(), target = new THREE.Vector3(), clk = new THREE.Clock();
    let visible = true, t = 0, sa = 2.2;
    new IntersectionObserver(([en]) => { visible = en.isIntersecting; }).observe(stage);
    const ease = (x) => 1 - Math.pow(1 - Math.min(Math.max(x, 0), 1), 3);
    const TAU = Math.PI * 2;

    function frame() {
      const dt = Math.min(clk.getDelta(), 0.05); t += dt;
      px += (tx - px) * 0.04; py += (ty - py) * 0.04;
      // «дрон»: медленный облёт, дыхание высоты и дистанции, плавающая точка взгляда, лёгкий крен
      const intro = ease(t / 3.2);
      const orbit = 0.78 + t * 0.035 + px * 0.5;
      const R = (stage.clientWidth < 560 ? 28 : 24) + Math.sin(t * 0.11) * 2.2 + (1 - intro) * 16;
      const H = 14 + Math.sin(t * 0.17) * 2.4 - py * 4 + (1 - intro) * 12;
      camera.position.set(Math.cos(orbit) * R, H, Math.sin(orbit) * R);
      camera.up.set(Math.sin(t * 0.1) * 0.05, 1, Math.cos(t * 0.08) * 0.03).normalize();
      target.set(Math.sin(t * 0.07) * 1.4, 1.6 + Math.sin(t * 0.13) * 0.4, Math.cos(t * 0.09) * 1.4);
      camera.lookAt(target);

      // радар
      sa = (sa + 0.9 * dt) % TAU;                         // радар — одна ровная скорость
      sweepPivot.rotation.y = -sa;

      bars.forEach((b, i) => {
        const e = ease((t - 0.4 - i * 0.07) / 1.3);
        const d = (sa - b.a + TAU * 2) % TAU;
        if (d < 0.1 && b.scanT > 1.5) { b.scanT = 0; b.goal = 1; }
        b.scanT += dt;
        // луч только подсвечивает макет: высота столбика не меняется
        b.goal = Math.max(0, (b.goal || 0) - dt * 0.5);
        b.heat += (b.goal - b.heat) * (1 - Math.exp(-dt * (b.goal > b.heat ? 7 : 2.5)));
        const hh = Math.max(0.02, b.h * e);
        b.fill.scale.y = hh; b.edges.scale.y = hh;
        // кольцо-пинг у основания и рамка, которая пробегает макет снизу вверх
        const k = Math.min(b.scanT / 1.4, 1); b.ping.scale.setScalar(0.4 + k * 2); b.ping.material.opacity = (1 - k) * 0.8;
        const ks = Math.min(b.scanT / 0.9, 1); b.scanSq.position.y = hh * ks; b.scanSq.material.opacity = b.scanT < 0.9 ? 0.95 : Math.max(0, 1 - (b.scanT - 0.9) * 3);
        b.edgeMat.color.copy(INK).lerp(BLUE, b.heat);
        b.fillMat.color.copy(WHITE).lerp(SOFT, b.heat);
      });
      for (let i = 0; i < PK; i++) { const p = packets[i]; p.t += dt * p.speed;
        if (p.t >= 1) { p.bar.tagUntil = t + 3; newPacket(p); }   // сигнал дошёл до верха — подпись на 3 секунды
        const q = p.t <= 0 ? p.curve.v0 : p.curve.getPoint(p.t); pkArr[i * 3] = q.x; pkArr[i * 3 + 1] = p.t <= 0 ? -50 : q.y; pkArr[i * 3 + 2] = q.z; }
      pkGeo.attributes.position.needsUpdate = true;
      renderer.render(scene, camera);
      const W = stage.clientWidth, Hh = stage.clientHeight;
      const placed = [];
      // сначала уже показанные подписи (чтобы не мигали), потом новые
      const order = tags.filter((g) => g.on).concat(tags.filter((g) => !g.on));
      for (const tg of order) {
        v.copy(tg.bar.g.position); v.y = tg.bar.fill.scale.y + 0.4; v.project(camera);
        const x = (v.x + 1) / 2 * W, y = (1 - v.y) / 2 * Hh;
        if (!tg.w) { tg.w = tg.el.offsetWidth; tg.h = tg.el.offsetHeight; }
        const r = { l: x - tg.w / 2, r: x + tg.w / 2, t: y - tg.h, b: y };
        const want = (tg.bar.tagUntil || 0) > t;
        const free = want && r.l > 4 && r.r < W - 4 && r.t > 22 && r.b < Hh - 22 && !placed.some((p) => r.l < p.r && r.r > p.l && r.t < p.b && r.b > p.t);
        if (free) placed.push(r);
        tg.on = free;
        tg.el.style.left = x.toFixed(1) + "px"; tg.el.style.top = y.toFixed(1) + "px";
        tg.el.style.opacity = free ? 1 : 0; tg.el.classList.toggle("hot", tg.bar.heat > 0.3);
      }
    }
    function loop() { if (visible) frame(); requestAnimationFrame(loop); }
    if (reduce) { t = 6; frame(); addEventListener("resize", () => { resize(); frame(); }); } else loop();
  });
})();
