/* NetMon panel controller */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const S = window.Scope;
  const { bytes, speed, num } = S;
  const TZ = -new Date().getTimezoneOffset();
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const PCOL = { TCP: "--rx", UDP: "--tx", ICMP: "--warn", GRE: "--violet", ESP: "--ok", SCTP: "--orange", Other: "--gray" };
  const WLBL = { "15m": "15 minutes", "1h": "hour", "6h": "6 hours", "12h": "12 hours", "24h": "24 hours", today: "today", yesterday: "yesterday", "7d": "week", "30d": "30 days" };

  /* ---------------------------------------------------------------- api */
  async function get(path) {
    const r = await fetch(path + (path.includes("?") ? "&" : "?") + "tz=" + TZ, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-NetMon": "1" }, body: JSON.stringify(body || {}) });
    if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok || j.ok === false) throw new Error(j.error || j.msg || r.statusText);
    return j;
  }
  function toast(msg, kind) {
    const d = document.createElement("div");
    d.className = kind || "ok"; d.textContent = msg;
    $("#toast").appendChild(d);
    setTimeout(() => d.remove(), kind === "bad" ? 5200 : 2600);
  }
  async function act(btn, fn, okMsg) {
    if (btn) btn.disabled = true;
    try { const r = await fn(); if (okMsg !== false) toast(okMsg || r.msg || "Done"); return r; }
    catch (e) { if (e.message !== "auth") toast(e.message, "bad"); }
    finally { if (btn) btn.disabled = false; }
  }
  function confirmBox(title, body, yes) {
    const dlg = $("#confirm-dlg");
    $("#cf-title").textContent = title;
    $("#cf-body").textContent = body;
    $("#cf-yes").textContent = yes || "Confirm";
    return new Promise((res) => {
      dlg.onclose = () => res(dlg.returnValue === "yes");
      dlg.showModal();
    });
  }

  /* segmented controls */
  function seg(id, cb) {
    const box = $(id);
    const st = { v: (box.querySelector('[aria-pressed="true"]') || box.querySelector("button")).dataset.v };
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b || !box.contains(b)) return;
      box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      st.v = b.dataset.v; cb && cb(st.v);
    });
    return st;
  }

  const ago = (ts) => {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  const when = (ts) => ts ? S.tfull(ts, 1).replace(/:\d\d$/, "") : "";
  const dur = (s) => {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };
  const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);

  /* ---------------------------------------------------------------- theme */
  function setThemeIcon() {
    const light = document.documentElement.dataset.theme === "light";
    $("#themeBtn use").setAttribute("href", light ? "#i-moon" : "#i-sun");
  }
  setThemeIcon();
  $("#themeBtn").onclick = () => {
    const t = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("nm-theme", t); } catch (e) {}
    setThemeIcon();
    window.dispatchEvent(new Event("nm-theme"));
    refresh();
  };

  /* ---------------------------------------------------------------- nav */
  let page = "overview";
  function go(p) {
    page = p;
    $$(".nav button").forEach((b) => (b.dataset.page === p ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
    $$(".page").forEach((s) => s.classList.toggle("on", s.id === "p-" + p));
    try { history.replaceState(null, "", "#" + p); } catch (e) {}
    const cur = document.querySelector('.nav button[aria-current="page"]');
    if (cur && window.innerWidth <= 1000) cur.scrollIntoView({ inline: "center", block: "nearest" });
    window.scrollTo(0, 0);
    refresh();
    window.dispatchEvent(new Event("resize"));
  }
  $$(".nav button").forEach((b) => (b.onclick = () => go(b.dataset.page)));
  document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) go(g.dataset.goto); });

  /* ================================================================ LIVE */
  const scope = new S.LiveScope("#scope", { seconds: 120 });
  async function live() {
    let d;
    try { d = await get("/api/live?seconds=120"); } catch (e) { $("#live-dot").classList.add("stale"); return; }
    const stale = Date.now() / 1000 - d.ts > 6;
    $("#live-dot").classList.toggle("stale", stale);
    $("#live-lbl").textContent = stale ? "Waiting for samples…" : "Live, 1-second samples";
    $("#lv-rx").textContent = speed(d.rx);
    $("#lv-tx").textContent = speed(d.tx);
    $("#rl-rx").textContent = speed(d.rx);
    $("#rl-tx").textContent = speed(d.tx);
    $("#lv-conn").textContent = num(d.active);
    $("#lv-new").textContent = (d.new_in + d.new_out).toFixed(1);
    $("#lv-prx").textContent = speed(d.peak_rx[0]);
    $("#lv-prx").title = when(d.peak_rx[1]);
    $("#lv-ptx").textContent = speed(d.peak_tx[0]);
    $("#lv-ptx").title = when(d.peak_tx[1]);
    if (page === "overview") {
      scope.setData(d.series);
      $("#scope-scale").textContent = scope.max ? "full scale " + speed(scope.max) : "";
    }
    LAST.live = d;
    if (page === "countries") renderIps(d.top_ips);
  }
  const LAST = {};

  /* ================================================================ OVERVIEW */
  const ovWin = seg("#ov-win", () => ovChart());
  const ovMetric = seg("#ov-metric", () => ovChart());
  const ovPmode = seg("#ov-pmode", () => ovDonut());
  const chOv = new S.TimeChart("#ov-chart", { height: 280, series: [
    { label: "Download", color: "--rx", fill: true },
    { label: "Upload", color: "--tx", fill: true },
    { label: "Peak download", color: "--rx", width: 1, dash: [3, 3] },
    { label: "Peak upload", color: "--tx", width: 1, dash: [3, 3] },
  ], yfmt: (v) => speed(v, 0), tipFmt: (v) => speed(v) });

  function speedCols(d) {
    const st = d.step;
    return [d.rx.map((v) => (v == null ? null : v / st)), d.tx.map((v) => (v == null ? null : v / st)), d.rx_peak, d.tx_peak];
  }

  async function overview() {
    const d = await get("/api/overview?window=24h");
    LAST.ov = d;
    const s = d.summary;
    $("#host").textContent = "v" + d.version;
    const cell = (k, lab) => {
      const x = s[k];
      const pk = x.peak_rx ? `<span class="faint" title="Peak download ${when(x.peak_rx[1])}">peak ${speed(x.peak_rx[0], 0)}</span>` : "";
      return `<div><div class="k">${lab}</div><div class="v">${bytes(x.rx + x.tx)}</div>
        <div class="s"><span class="rx">↓ ${bytes(x.rx)}</span><span class="tx">↑ ${bytes(x.tx)}</span></div></div>`;
    };
    const at = s.alltime;
    $("#totals").innerHTML = cell("today", "Today") + cell("24h", "Last 24 hours") + cell("7d", "Last 7 days") + cell("30d", "Last 30 days") +
      `<div><div class="k">All time${at.since ? " · since " + S.tfull(at.since, 86400).replace(/^\w+ /, "") : ""}</div><div class="v">${bytes(at.rx + at.tx)}</div>
       <div class="s"><span class="rx">↓ ${bytes(at.rx)}</span><span class="tx">↑ ${bytes(at.tx)}</span></div></div>`;

    const h = d.highlights;
    const pcard = (k, p, val, sub) => p ? `<div class="panel hl"><div class="k">${k}</div>
        <div class="v">${p.port}<small>${esc(p.service || p.proto)}</small></div>
        <div class="s">${val} · ${p.role === 0 ? "inbound" : "outbound"} ${p.proto}${sub ? " · " + sub : ""}</div></div>`
      : `<div class="panel hl"><div class="k">${k}</div><div class="v faint">—</div><div class="s">No port data yet</div></div>`;
    const c = h.country, pr = h.proto;
    $("#ov-hl").innerHTML =
      pcard("Busiest port by traffic", h.port_bytes, h.port_bytes && bytes(h.port_bytes.rx + h.port_bytes.tx)) +
      pcard("Most requested port", h.port_req, h.port_req && num(h.port_req.req) + " requests") +
      (c ? `<div class="panel hl"><div class="k">Top source country</div><div class="v"><span class="flag">${c.flag}</span>${esc(c.name)}</div><div class="s">${num(c.req)} new connections · ${(c.share * 100).toFixed(0)}% of all</div></div>`
         : `<div class="panel hl"><div class="k">Top source country</div><div class="v faint">—</div><div class="s">${d.info.geoip ? "No incoming connections yet" : "Country database not installed"}</div></div>`) +
      (pr ? `<div class="panel hl"><div class="k">Most used protocol</div><div class="v">${pr.name}<small>by requests</small></div><div class="s">${num(pr.req_in + pr.req_out)} requests · ${bytes(pr.rx + pr.tx)}</div></div>`
          : `<div class="panel hl"><div class="k">Most used protocol</div><div class="v faint">—</div><div class="s">No data yet</div></div>`);

    const [ports, ccs] = await Promise.all([get("/api/ports?window=24h&limit=6"), get("/api/countries?window=24h&dir=0")]);
    const pmax = Math.max(1, ...ports.map((p) => p.rx + p.tx));
    $("#ov-ports").innerHTML = ports.length ? ports.map((p, i) => `<li><span class="n">${i + 1}</span><span class="chip ${p.role ? "out" : "in"}">${p.role ? "out" : "in"}</span>
      <div class="name"><b><span class="port">${p.port}</span> <span class="dim">${esc(p.service || p.process || p.proto)}</span></b><div class="bar"><i style="width:${pct(p.rx + p.tx, pmax)}%"></i></div></div>
      <div class="val">${bytes(p.rx + p.tx)}<small>${num(p.req)} req</small></div></li>`).join("") : `<li class="empty">Port counters fill in within a minute</li>`;
    const top = ccs.slice(0, 6), cmax = Math.max(1, ...top.map((x) => x.req));
    $("#ov-cc").innerHTML = top.length ? top.map((x, i) => ccRow(x, i, cmax)).join("") : `<li class="empty">${d.info.geoip ? "No incoming connections yet" : "Country database not installed — run netmon geoip-update"}</li>`;
    ovDonut();
  }
  function ccRow(x, i, max) {
    return `<li><span class="n">${i + 1}</span><span class="flag" title="${x.cc}">${x.flag}</span>
      <div class="name"><b>${esc(x.name)}</b><div class="bar"><i style="width:${pct(x.req, max)}%"></i></div></div>
      <div class="val">${num(x.req)}<small>${(x.share * 100).toFixed(x.share < 0.1 ? 1 : 0)}%</small></div></li>`;
  }
  async function ovDonut() {
    const d = await get("/api/protocols?window=24h");
    const items = d.totals.map((r) => ({ label: r.name, color: PCOL[r.name], value: ovPmode.v === "req" ? r.req_in + r.req_out : r.rx + r.tx }))
      .filter((x) => x.value > 0 || ["TCP", "UDP"].includes(x.label));
    S.donut("#ov-donut", items, { fmt: ovPmode.v === "req" ? num : bytes, sub: ovPmode.v === "req" ? "requests" : "transferred" });
  }
  async function ovChart() {
    const d = await get("/api/traffic?window=" + ovWin.v);
    if (ovMetric.v === "speed") {
      chOv.o.yfmt = (v) => speed(v, 0); chOv.o.tipFmt = (v) => speed(v);
      chOv.setData(d.ts, speedCols(d), d.step);
      $("#ov-sub").textContent = `Average speed per ${stepName(d.step)}, dashed lines are the highest 1-second speed`;
    } else {
      chOv.o.yfmt = (v) => bytes(v, 0); chOv.o.tipFmt = (v) => bytes(v);
      chOv.setData(d.ts, [d.rx, d.tx, null, null], d.step);
      $("#ov-sub").textContent = `Bytes transferred per ${stepName(d.step)}`;
    }
  }
  function stepName(s) {
    if (s < 60) return s + " seconds";
    if (s < 3600) return s === 60 ? "minute" : s / 60 + " minutes";
    if (s < 86400) return s === 3600 ? "hour" : s / 3600 + " hours";
    return "day";
  }

  /* ================================================================ TRAFFIC */
  const STEPS_FINE = [[5, "5s"], [15, "15s"], [60, "1m"], [300, "5m"], [900, "15m"]];
  const STEPS_HOUR = [[3600, "1h"], [21600, "6h"], [86400, "1d"]];
  const AUTO = { "15m": 5, "1h": 5, "6h": 15, "24h": 60, today: 60, yesterday: 300, "7d": 3600, "30d": 3600 };
  let trStep = null;
  const trWin = seg("#tr-win", () => { trStep = null; buildSteps(); traffic(); });
  function buildSteps() {
    const hourly = ["7d", "30d", "yesterday"].includes(trWin.v);
    const list = trWin.v === "yesterday" ? [[300, "5m"], [900, "15m"], [3600, "1h"]] : hourly ? STEPS_HOUR : STEPS_FINE;
    const cur = trStep || AUTO[trWin.v];
    $("#tr-step").innerHTML = list.map(([v, l]) => `<button data-v="${v}" aria-pressed="${v === cur}">${l}</button>`).join("");
  }
  $("#tr-step").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; $$("#tr-step button").forEach((x) => x.setAttribute("aria-pressed", x === b)); trStep = +b.dataset.v; traffic(); });
  buildSteps();
  const trDmode = seg("#tr-dmode", () => daily());
  const chSpeed = new S.TimeChart("#tr-speed", { height: 300, series: [
    { label: "Download", color: "--rx", fill: true }, { label: "Upload", color: "--tx", fill: true },
    { label: "Peak download", color: "--rx", width: 1, dash: [3, 3] }, { label: "Peak upload", color: "--tx", width: 1, dash: [3, 3] },
  ], yfmt: (v) => speed(v, 0), tipFmt: (v) => speed(v) });
  const chVol = new S.TimeChart("#tr-vol", { height: 230, byteAxis: true, series: [
    { label: "Download", color: "--rx", fill: true }, { label: "Upload", color: "--tx", fill: true }], yfmt: (v) => bytes(v), tipFmt: (v) => bytes(v) });
  const chConn = new S.TimeChart("#tr-conn", { height: 230, series: [
    { label: "New incoming", color: "--rx" }, { label: "New outgoing", color: "--tx" }, { label: "Open connections", color: "--violet", width: 1.2, dash: [4, 3] }], yfmt: (v) => num(v) });
  const chDaily = new S.TimeChart("#tr-daily", { height: 240, bars: true, zoom: false, byteAxis: true, series: [
    { label: "Download", color: "--rx" }, { label: "Upload", color: "--tx" }], yfmt: (v) => bytes(v), tipFmt: (v) => bytes(v),
    tfmt: (t) => { const d = new Date(t * 1000); return d.getDate() + " " + ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()]; } });

  async function traffic() {
    const d = await get(`/api/traffic?window=${trWin.v}` + (trStep ? `&step=${trStep}` : ""));
    if (!trStep) $$("#tr-step button").forEach((b) => b.setAttribute("aria-pressed", +b.dataset.v === d.step));
    chSpeed.setData(d.ts, speedCols(d), d.step);
    chVol.setData(d.ts, [d.rx, d.tx], d.step);
    chConn.setData(d.ts, [d.new_in, d.new_out, d.conns], d.step);
    const sum = (a) => a.reduce((s, v) => s + (v || 0), 0);
    const pk = (a) => { let m = 0, i = -1; a.forEach((v, j) => { if (v > m) { m = v; i = j; } }); return [m, i >= 0 ? d.ts[i] : 0]; };
    const rx = sum(d.rx), tx = sum(d.tx), prx = pk(d.rx_peak), ptx = pk(d.tx_peak);
    const span = d.ts.length ? Math.max(d.step, d.ts[d.ts.length - 1] - d.ts[0] + d.step) : 1;
    const label = WLBL[trWin.v];
    $("#tr-tiles").innerHTML = `
      <div class="panel tile"><div class="k"><span class="rx">↓</span> Downloaded, ${label}</div><div class="v rx">${bytes(rx)}</div><div class="s">average ${speed(rx / span)}</div></div>
      <div class="panel tile"><div class="k"><span class="tx">↑</span> Uploaded, ${label}</div><div class="v tx">${bytes(tx)}</div><div class="s">average ${speed(tx / span)}</div></div>
      <div class="panel tile"><div class="k">Peak download</div><div class="v">${speed(prx[0])}</div><div class="s">${prx[1] ? S.tfull(prx[1], d.step) : "—"}</div></div>
      <div class="panel tile"><div class="k">Peak upload</div><div class="v">${speed(ptx[0])}</div><div class="s">${ptx[1] ? S.tfull(ptx[1], d.step) : "—"}</div></div>`;
  }
  async function daily() {
    const d = await get("/api/daily?days=30");
    if (trDmode.v === "peak") {
      chDaily.o.yfmt = (v) => speed(v, 0); chDaily.o.tipFmt = (v) => speed(v); chDaily.o.byteAxis = false;
      chDaily.series[0].label = "Peak download"; chDaily.series[1].label = "Peak upload";
      chDaily.drawLegend(); chDaily.setData(d.ts, [d.rx_peak, d.tx_peak], 86400);
    } else {
      chDaily.o.yfmt = (v) => bytes(v, 0); chDaily.o.tipFmt = (v) => bytes(v); chDaily.o.byteAxis = true;
      chDaily.series[0].label = "Download"; chDaily.series[1].label = "Upload";
      chDaily.drawLegend(); chDaily.setData(d.ts, [d.rx, d.tx], 86400);
    }
  }

  /* ================================================================ PORTS */
  let poRows = [], poSel = null;
  const poWin = seg("#po-win", () => { ports(); if (poSel) portChart(); });
  const poRole = seg("#po-role", () => ports());
  const poSort = seg("#po-sort", () => renderPorts());
  const poDmode = seg("#po-dmode", () => portChart());
  const chPort = new S.TimeChart("#po-chart", { height: 260, series: [
    { label: "Download", color: "--rx", fill: true }, { label: "Upload", color: "--tx", fill: true },
    { label: "Peak download", color: "--rx", width: 1, dash: [3, 3] }, { label: "Peak upload", color: "--tx", width: 1, dash: [3, 3] }],
    yfmt: (v) => speed(v, 0), tipFmt: (v) => speed(v) });

  async function ports() {
    poRows = await get(`/api/ports?window=${poWin.v}&role=${poRole.v}&limit=300&sort=total`);
    const best = (f) => poRows.reduce((a, b) => (f(b) > f(a || b) ? b : a || b), null);
    const bT = best((p) => p.rx + p.tx), bR = best((p) => p.req), bP = best((p) => Math.max(p.rx_peak, p.tx_peak));
    const card = (k, p, v, s) => `<div class="panel hl"><div class="k">${k}</div>${p ? `<div class="v">${p.port}<small>${esc(p.service || p.process || "")} · ${p.proto} ${p.role ? "outbound" : "inbound"}</small></div><div class="s">${v}${s ? " · " + s : ""}</div>` : `<div class="v faint">—</div><div class="s">No data in this range</div>`}</div>`;
    $("#po-hl").innerHTML =
      card("Most traffic", bT, bT && bytes(bT.rx + bT.tx), bT && `↓ ${bytes(bT.rx)} ↑ ${bytes(bT.tx)}`) +
      card("Most requests", bR && bR.req ? bR : null, bR && num(bR.req) + " new connections") +
      card("Fastest", bP && Math.max(bP.rx_peak, bP.tx_peak) ? bP : null, bP && `↓ ${speed(bP.rx_peak)} ↑ ${speed(bP.tx_peak)}`, "peak over 5 seconds");
    renderPorts();
  }
  function renderPorts() {
    const key = { total: (p) => p.rx + p.tx, rx: (p) => p.rx, tx: (p) => p.tx, req: (p) => p.req, peak: (p) => Math.max(p.rx_peak, p.tx_peak) }[poSort.v];
    const rows = poRows.slice().sort((a, b) => key(b) - key(a)).slice(0, 100);
    $("#po-rows").innerHTML = rows.length ? rows.map((p, i) => {
      const t = p.rx + p.tx, k = `${p.port}:${p.role}:${p.p}`;
      return `<tr class="click${poSel === k ? " sel" : ""}" data-k="${k}"><td class="rank">${i + 1}</td><td><span class="port">${p.port}</span> <span class="faint">${p.proto}</span></td>
        <td>${esc(p.service) || '<span class="faint">—</span>'}${p.process ? ` <span class="chip">${esc(p.process)}</span>` : ""}</td>
        <td><span class="chip ${p.role ? "out" : "in"}">${p.role ? "Outbound" : "Inbound"}</span></td>
        <td class="n rx">${bytes(p.rx)}</td><td class="n tx">${bytes(p.tx)}</td><td class="n"><b>${bytes(t)}</b></td>
        <td><div class="split"><i class="r" style="width:${pct(p.rx, t)}%"></i><i class="t" style="width:${t ? 100 - pct(p.rx, t) : 0}%"></i></div></td>
        <td class="n">${num(p.req)}</td><td class="n">${speed(p.rx_peak)}</td><td class="n">${speed(p.tx_peak)}</td></tr>`;
    }).join("") : `<tr><td colspan="11" class="empty">No port traffic recorded in this range yet. Counters start within a minute of install.</td></tr>`;
  }
  $("#po-rows").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-k]"); if (!tr) return;
    poSel = tr.dataset.k;
    $$("#po-rows tr").forEach((r) => r.classList.toggle("sel", r === tr));
    portChart();
    $("#po-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  async function portChart() {
    if (!poSel) return;
    const [port, role, p] = poSel.split(":");
    const d = await get(`/api/port_series?port=${port}&role=${role}&p=${p}&window=${poWin.v}`);
    const row = poRows.find((r) => `${r.port}:${r.role}:${r.p}` === poSel) || {};
    $("#po-detail").hidden = false;
    $("#po-dt").textContent = `Port ${port} ${p == 6 ? "TCP" : "UDP"} ${role == 1 ? "outbound" : "inbound"}${row.service ? " — " + row.service : ""}`;
    const m = poDmode.v;
    if (m === "speed") {
      chPort.series = chPort.series.slice(0, 4);
      Object.assign(chPort.o, { yfmt: (v) => speed(v, 0), tipFmt: (v) => speed(v), byteAxis: false });
      chPort.series[0].label = "Download"; chPort.series[1].label = "Upload";
      chPort.drawLegend();
      chPort.setData(d.ts, [d.rx.map((v) => v / d.step), d.tx.map((v) => v / d.step), d.rx_peak, d.tx_peak], d.step);
    } else if (m === "volume") {
      Object.assign(chPort.o, { yfmt: (v) => bytes(v), tipFmt: (v) => bytes(v), byteAxis: true });
      chPort.series[0].label = "Download"; chPort.series[1].label = "Upload"; chPort.drawLegend();
      chPort.setData(d.ts, [d.rx, d.tx, null, null], d.step);
    } else {
      Object.assign(chPort.o, { yfmt: (v) => num(v), tipFmt: (v) => num(v) + " requests", byteAxis: false });
      chPort.series[0].label = "Requests"; chPort.drawLegend();
      chPort.setData(d.ts, [d.req, null, null, null], d.step);
    }
  }

  /* ================================================================ COUNTRIES */
  const ccDir = seg("#cc-dir", () => countries());
  const ccWin = seg("#cc-win", () => countries());
  let chCc = null;
  const CCOL = ["--rx", "--tx", "--warn", "--violet", "--ok", "--orange"];
  async function countries() {
    const list = await get(`/api/countries?window=${ccWin.v}&dir=${ccDir.v}`);
    const max = Math.max(1, ...list.map((x) => x.req));
    const total = list.reduce((s, x) => s + x.req, 0);
    $("#cc-count").textContent = list.length ? `${list.length} countries · ${num(total)} connections` : "";
    const geo = LAST.ov ? LAST.ov.info.geoip : true;
    $("#cc-list").innerHTML = list.length ? list.map((x, i) => ccRow(x, i, max)).join("")
      : `<li class="empty">${geo ? "No connections recorded in this range yet" : "Country database isn't installed. Run sudo netmon geoip-update on the server."}</li>`;
    const top = list.filter((x) => x.cc !== "??").slice(0, 6);
    const ser = top.map((x, i) => ({ label: `${x.flag} ${x.name}`, color: CCOL[i] }));
    if (!chCc || chCc.series.length !== ser.length || chCc.series.some((s, i) => s.label !== ser[i].label)) {
      chCc = new S.TimeChart("#cc-chart", { height: 260, series: ser.length ? ser : [{ label: "No data", color: "--gray" }], yfmt: (v) => num(v), tipFmt: (v) => num(v) + " new" });
    }
    if (!top.length) { chCc.setData([], []); return; }
    const d = await get(`/api/country_series?window=${ccWin.v}&dir=${ccDir.v}&cc=${top.map((x) => x.cc).join(",")}`);
    chCc.setData(d.ts, top.map((x) => d.series[x.cc]), d.step);
    if (LAST.live) renderIps(LAST.live.top_ips);
  }
  function renderIps(ips) {
    $("#cc-ips").innerHTML = ips && ips.length ? ips.slice(0, 15).map((x) => `<tr><td class="mono">${esc(x.ip)}</td><td><span class="flag">${x.flag}</span> ${esc(x.name)}</td><td class="n">${num(x.n)}</td>
      <td class="n"><button class="btn sm danger" data-blockip="${esc(x.ip)}" type="button">Block</button></td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">No inbound connections open right now</td></tr>`;
  }
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-blockip]"); if (!b) return;
    const ip = b.dataset.blockip;
    if (!(await confirmBox(`Block ${ip}?`, "All traffic from and to this address will be dropped and its open connections cut.", "Block IP"))) return;
    await act(b, () => post("/api/fw/ip", { cidr: ip, label: "from live list" }), `Blocked ${ip}`);
  });

  /* ================================================================ PROTOCOLS */
  const prWin = seg("#pr-win", () => protocols());
  const prDir = seg("#pr-dir", () => protocols(true));
  let chPr = null, chPrReq = null, prCache = null;
  async function protocols(onlyChart) {
    if (!onlyChart) {
      const d = await get(`/api/protocols?window=${prWin.v}`);
      const order = d.totals.slice().sort((a, b) => b.rx + b.tx + (b.req_in + b.req_out) * 1e-3 - (a.rx + a.tx + (a.req_in + a.req_out) * 1e-3));
      $("#pr-cards").innerHTML = order.map((r) => {
        const lv = d.live[String(r.p)] || [0, 0];
        return `<div class="proto" style="--c:${S.color(PCOL[r.name])}"><h3>${r.name}<small>${bytes(r.rx + r.tx)}</small></h3>
          <div class="pt"><span></span><span class="rx">↓ in</span><span class="tx">↑ out</span>
          <span>Bytes</span><b>${bytes(r.rx)}</b><b>${bytes(r.tx)}</b>
          <span>Packets</span><b>${num(r.rx_pk)}</b><b>${num(r.tx_pk)}</b>
          <span>Requests</span><b>${num(r.req_in)}</b><b>${num(r.req_out)}</b>
          <span>Peak</span><b>${speed(r.rx_peak)}</b><b>${speed(r.tx_peak)}</b>
          <span>Now</span><b>${speed(lv[0])}</b><b>${speed(lv[1])}</b></div></div>`;
      }).join("");
      const it = (f) => d.totals.map((r) => ({ label: r.name, color: PCOL[r.name], value: f(r) })).filter((x) => x.value > 0 || ["TCP", "UDP"].includes(x.label));
      S.donut("#pr-d1", it((r) => r.rx + r.tx), { fmt: bytes, sub: "transferred" });
      S.donut("#pr-d2", it((r) => r.req_in + r.req_out), { fmt: num, sub: "requests" });
      prCache = await get(`/api/proto_series?window=${prWin.v}`);
    }
    const d = prCache; if (!d) return;
    const names = Object.keys(d.series).sort((a, b) => Object.keys(PCOL).indexOf(a) - Object.keys(PCOL).indexOf(b));
    const key = names.join(",");
    if (!chPr || chPr._key !== key) {
      chPr = new S.TimeChart("#pr-chart", { height: 280, stacked: true, byteAxis: true, series: names.map((n) => ({ label: n, color: PCOL[n] || "--gray", width: 1 })), yfmt: (v) => bytes(v), tipFmt: (v) => bytes(v) });
      chPr._key = key;
      chPrReq = new S.TimeChart("#pr-req", { height: 240, stacked: true, series: names.map((n) => ({ label: n, color: PCOL[n] || "--gray", width: 1 })), yfmt: (v) => num(v) });
    }
    const pick = (s) => prDir.v === "rx" ? s.rx : prDir.v === "tx" ? s.tx : s.rx.map((v, i) => v + s.tx[i]);
    chPr.setData(d.ts, names.map((n) => pick(d.series[n])), d.step);
    chPrReq.setData(d.ts, names.map((n) => d.series[n].req), d.step);
  }

  /* ================================================================ FIREWALL */
  let FW = null;
  const bkKind = seg("#bk-kind", (v) => { $("#bk-single").hidden = v !== "single"; $("#bk-from").hidden = $("#bk-to").hidden = v !== "range"; });
  async function firewall() {
    const [d, log] = await Promise.all([get("/api/firewall"), get("/api/fwlog")]);
    FW = d;
    $("#fw-me").innerHTML = `Your IP <b class="mono">${esc(d.me)}</b>${d.me_whitelisted ? " · whitelisted" : ""}`;
    $("#fw-me").className = "chip " + (d.me_whitelisted ? "ok" : "");
    // SSH
    const open = d.ssh_open;
    $("#ssh-sw").checked = open;
    $("#ssh-ico").classList.toggle("closed", !open);
    $("#ssh-ico use").setAttribute("href", open ? "#i-unlock" : "#i-lock");
    $("#ssh-title").textContent = open ? "SSH is open on port 22" : "SSH is closed on port 22";
    const other = d.ssh_ports.filter((p) => p !== 22);
    $("#ssh-sub").textContent = (open ? "New SSH logins are accepted." : "New SSH logins are refused. Sessions already open stay connected.") +
      (other.length ? ` sshd also listens on ${other.join(", ")}.` : "");
    const n = $("#ssh-note");
    if (!d.me_whitelisted) { n.hidden = false; n.className = "note"; n.innerHTML = `Before closing SSH, make sure you have another way in. Adding your IP (<b class="mono">${esc(d.me)}</b>) to “Never block” keeps SSH open for you only.`; }
    else { n.hidden = false; n.className = "note okn"; n.textContent = "Your IP is whitelisted, so you can still reach SSH even when it's closed for everyone else."; }
    $("#bk-kill").checked = d.kill_on_block;
    // rules
    const R = [];
    d.blocks.forEach((b) => R.push(`<li class="${b.enabled === false ? "paused" : ""}"><span class="chip bad">closed</span><span class="what">${b.kind === "range" ? `${b.lo}–${b.hi}` : b.port}</span>
      <span class="chip">${b.proto === "both" ? "TCP+UDP" : b.proto.toUpperCase()}</span><span class="chip ${b.direction === "out" ? "out" : "in"}">${{ in: "incoming", out: "outgoing", both: "both ways" }[b.direction] || b.direction}</span>
      <span class="lab">${esc(b.label)}</span>
      <label class="sw" title="${b.enabled === false ? "Paused" : "Active"}"><input type="checkbox" data-btoggle="${b.id}" ${b.enabled === false ? "" : "checked"} aria-label="Rule active"><span></span></label>
      <button class="btn sm ghost" data-bdel="${b.id}" type="button">Remove</button></li>`));
    d.allows.forEach((a) => R.push(`<li><span class="chip ok">open</span><span class="what">${a.port}</span><span class="chip">${a.proto === "both" ? "TCP+UDP" : a.proto.toUpperCase()}</span><span class="lab">exception inside a closed range ${esc(a.label)}</span><button class="btn sm ghost" data-adel="${a.id}" type="button">Remove</button></li>`));
    d.ip_blocks.forEach((x) => R.push(`<li><span class="chip bad">IP</span><span class="what mono" style="font-size:13.5px">${esc(x.cidr)}</span><span class="lab">${esc(x.label)}</span><button class="btn sm ghost" data-idel="${x.id}" type="button">Unblock</button></li>`));
    if (d.ratelimit.enabled) R.push(`<li><span class="chip warn">limit</span><span class="what">Flood protection</span><span class="lab">${d.ratelimit.rate} new/s per IP, burst ${d.ratelimit.burst}, max ${d.ratelimit.conn_max} open</span></li>`);
    $("#rules").innerHTML = R.join("") || `<li class="empty" style="justify-content:center;width:100%">Nothing is closed. Every port is open.</li>`;
    $("#rules-count").textContent = R.length ? `${R.length} rule${R.length > 1 ? "s" : ""}` : "";
    // whitelist
    $("#wl-list").innerHTML = d.whitelist.map((ip) => `<span class="chip ok mono">${esc(ip)}<button data-wldel="${esc(ip)}" type="button" aria-label="Remove ${esc(ip)}">×</button></span>`).join("") || `<span class="faint">No whitelisted IPs</span>`;
    if (document.activeElement !== $("#tp-in")) $("#tp-in").value = d.tunnel_ports.join(", ");
    $("#rl-on").checked = d.ratelimit.enabled;
    ["rate", "burst", "conn_max"].forEach((k, i) => { const el = $(["#rl-rate", "#rl-burst", "#rl-max"][i]); if (document.activeElement !== el) el.value = d.ratelimit[k]; });
    // well-known
    $("#wk").innerHTML = d.groups.map((g) => `<div class="cat"><h3>${esc(g.name)}</h3><div class="wk">${g.ports.map((p) => `
      <div class="wk-item${p.open ? "" : " closed"}"><span class="p">${p.port}</span><div class="m"><b>${p.listening ? '<span class="live-dot" title="Listening on this server"></span>' : ""}${esc(p.service)} <span class="faint" style="font-weight:400">${p.proto === "both" ? "TCP+UDP" : p.proto.toUpperCase()}</span></b><span>${esc(p.note)}</span></div>
      <label class="sw" title="${p.open ? "Open" : "Closed"}"><input type="checkbox" data-wk="${p.port}" data-proto="${p.proto}" data-svc="${esc(p.service)}" ${p.open ? "checked" : ""} ${p.port === d.panel_port ? "disabled" : ""} aria-label="${esc(p.service)} port ${p.port} open"><span></span></label></div>`).join("")}</div></div>`).join("");
    $("#fw-log").innerHTML = log.map((l) => `<li><time>${when(l.ts)}</time><b>${esc(l.action)}</b><span class="dim">${esc(l.detail)}</span></li>`).join("") || `<li class="faint">No changes yet</li>`;
  }

  $("#ssh-sw").addEventListener("change", async (e) => {
    const want = e.target.checked;
    if (!want) {
      const ok = await confirmBox("Close SSH on port 22?", FW && FW.me_whitelisted
        ? "New SSH logins from anywhere except your whitelisted IPs will be refused. Open sessions stay connected."
        : "New SSH logins will be refused for everyone, including you. Open sessions stay connected. Make sure you have another way in, like your provider's console.", "Close SSH");
      if (!ok) { e.target.checked = true; return; }
    }
    await act(e.target, () => post("/api/fw/port", { port: 22, proto: "tcp", open: want, label: "SSH" }), want ? "SSH opened" : "SSH closed");
    firewall();
  });
  $("#wk").addEventListener("change", async (e) => {
    const c = e.target.closest("[data-wk]"); if (!c) return;
    const port = +c.dataset.wk, open = c.checked;
    if (port === 22 && !open && !(await confirmBox("Close SSH on port 22?", "New SSH logins will be refused. Open sessions stay connected.", "Close SSH"))) { c.checked = true; return; }
    await act(c, () => post("/api/fw/port", { port, proto: c.dataset.proto, open, label: c.dataset.svc }), `${c.dataset.svc} ${open ? "opened" : "closed"}`);
    firewall();
  });
  $("#bk-go").onclick = async (e) => {
    const body = { proto: $("#bk-proto").value, direction: $("#bk-dir").value, label: $("#bk-label").value, kill: $("#bk-kill").checked };
    if (bkKind.v === "range") { body.lo = $("#bk-lo").value; body.hi = $("#bk-hi").value; } else body.port = $("#bk-port").value;
    const what = bkKind.v === "range" ? `ports ${body.lo}–${body.hi}` : `port ${body.port}`;
    const sshHit = bkKind.v === "range" ? +body.lo <= 22 && +body.hi >= 22 : +body.port === 22;
    if (sshHit && body.direction !== "out" && !(await confirmBox(`Close ${what}?`, "This includes SSH (22). New SSH logins will be refused.", "Close"))) return;
    const r = await act(e.currentTarget, () => post("/api/fw/block", body), `Closed ${what}`);
    if (r) { $("#bk-port").value = $("#bk-lo").value = $("#bk-hi").value = $("#bk-label").value = ""; firewall(); }
  };
  $("#op-go").onclick = async (e) => {
    const port = $("#op-port").value;
    const r = await act(e.currentTarget, () => post("/api/fw/port", { port, proto: $("#op-proto").value, open: true }), `Opened port ${port}`);
    if (r) { $("#op-port").value = ""; firewall(); }
  };
  $("#ip-go").onclick = async (e) => {
    const r = await act(e.currentTarget, () => post("/api/fw/ip", { cidr: $("#ip-cidr").value.trim(), label: $("#ip-label").value }), "IP blocked");
    if (r) { $("#ip-cidr").value = $("#ip-label").value = ""; firewall(); }
  };
  $("#rules").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-bdel],[data-adel],[data-idel]"); if (!b) return;
    if (b.dataset.bdel) await act(b, () => post("/api/fw/block/delete", { id: b.dataset.bdel }), "Rule removed, port open");
    else if (b.dataset.adel) await act(b, () => post("/api/fw/allow/delete", { id: b.dataset.adel }), "Exception removed");
    else await act(b, () => post("/api/fw/ip/delete", { id: b.dataset.idel }), "IP unblocked");
    firewall();
  });
  $("#rules").addEventListener("change", async (e) => {
    const t = e.target.closest("[data-btoggle]"); if (!t) return;
    await act(t, () => post("/api/fw/block/toggle", { id: t.dataset.btoggle, enabled: t.checked }), t.checked ? "Rule active" : "Rule paused");
    firewall();
  });
  $("#wl-add").onclick = async (e) => {
    const ip = $("#wl-ip").value.trim(); if (!ip) return;
    const r = await act(e.currentTarget, () => post("/api/fw/whitelist", { ips: FW.whitelist.concat([ip]) }), `${ip} will never be blocked`);
    if (r) { $("#wl-ip").value = ""; firewall(); }
  };
  $("#wl-me").onclick = async (e) => { await act(e.currentTarget, () => post("/api/fw/whitelist/me", {}), "Your IP is whitelisted"); firewall(); };
  $("#wl-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-wldel]"); if (!b) return;
    await act(b, () => post("/api/fw/whitelist", { ips: FW.whitelist.filter((x) => x !== b.dataset.wldel) }), "Removed from whitelist");
    firewall();
  });
  $("#tp-save").onclick = async (e) => {
    const ports = $("#tp-in").value.split(/[\s,]+/).filter(Boolean);
    await act(e.currentTarget, () => post("/api/fw/whitelist", { tunnel_ports: ports }), "Tunnel ports saved");
    firewall();
  };
  const rlSave = async (btn) => {
    await act(btn, () => post("/api/fw/ratelimit", { enabled: $("#rl-on").checked, rate: +$("#rl-rate").value, burst: +$("#rl-burst").value, conn_max: +$("#rl-max").value }),
      $("#rl-on").checked ? "Flood protection on" : "Flood protection off");
    firewall();
  };
  $("#rl-save").onclick = (e) => rlSave(e.currentTarget);
  $("#rl-on").onchange = (e) => rlSave(e.currentTarget);
  $("#bk-kill").onchange = (e) => act(null, () => post("/api/fw/settings", { kill_on_block: e.target.checked }), false);

  /* ================================================================ SYSTEM */
  async function system() {
    const [d, fw] = await Promise.all([get("/api/system"), FW ? Promise.resolve(FW) : get("/api/firewall")]);
    FW = fw;
    const memUsed = d.mem.total - d.mem.available, diskUsed = d.disk.total - d.disk.free;
    $("#sys-sub").textContent = `${d.hostname} · ${d.os} · kernel ${d.kernel}`;
    const meter = (p, c) => `<div class="meter"><i style="width:${Math.min(100, p)}%;background:var(${p > 90 ? "--bad" : p > 75 ? "--warn" : c})"></i></div>`;
    $("#sys-tiles").innerHTML = `
      <div class="panel tile"><div class="k">CPU</div><div class="v">${d.cpu.toFixed(0)}%</div><div class="s">${d.cpus} cores · load ${d.load.join(" ")}</div>${meter(d.cpu, "--rx")}</div>
      <div class="panel tile"><div class="k">Memory</div><div class="v">${bytes(memUsed)}</div><div class="s">of ${bytes(d.mem.total)}${d.swap.total ? ` · swap ${bytes(d.swap.total - d.swap.free)}` : ""}</div>${meter(pct(memUsed, d.mem.total), "--violet")}</div>
      <div class="panel tile"><div class="k">Disk</div><div class="v">${bytes(diskUsed)}</div><div class="s">of ${bytes(d.disk.total)} · ${bytes(d.disk.free)} free</div>${meter(pct(diskUsed, d.disk.total), "--tx")}</div>
      <div class="panel tile"><div class="k">Uptime</div><div class="v">${dur(d.uptime)}</div><div class="s">NetMon running ${dur(d.netmon.uptime)}</div></div>`;
    const nm = d.netmon;
    const kv = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
    $("#sys-nm").innerHTML = `<dl style="margin:0">` +
      kv("Version", nm.version) + kv("Watching", esc(nm.ifaces.join(", "))) +
      kv("Kernel counters", nm.accounting ? `<span class="ok">on</span>${nm.ipv6 ? " · IPv4 + IPv6" : " · IPv4"}` : `<span class="warn">off</span>`) +
      kv("Ports with own counters", `${nm.watched_server} inbound · ${nm.watched_client} outbound`) +
      kv("Connection source", nm.conn_source) + kv("Country database", nm.geoip ? '<span class="ok">loaded</span>' : '<span class="warn">missing</span>') +
      kv("Memory used", bytes(d.rss)) + kv("Database size", bytes(d.db_size)) +
      kv("Sample rate", `${nm.poll}s traffic · ${nm.bucket}s ports`) +
      kv("Speed units", `<span class="seg" id="units"><button data-v="bits" aria-pressed="${window.NM_UNITS !== "bytes"}">Mbps</button><button data-v="bytes" aria-pressed="${window.NM_UNITS === "bytes"}">MB/s</button></span>`) +
      (nm.error ? kv("Last error", `<span class="bad">${esc(nm.error)}</span>`) : "") + `</dl>`;
    seg("#units", (v) => { window.NM_UNITS = v; try { localStorage.setItem("nm-units", v); } catch (e) {} toast(v === "bits" ? "Speeds in bits per second" : "Speeds in bytes per second"); });
    $("#sys-if").innerHTML = d.ifaces.filter((i) => i.rx || i.tx || i.addrs.length).map((i) => `<tr><td><b>${esc(i.name)}</b>${nm.ifaces.includes(i.name) ? ' <span class="chip in">watched</span>' : ""}</td>
      <td class="mono dim" style="white-space:normal">${i.addrs.map(esc).join("<br>") || "—"}</td><td class="n rx">${bytes(i.rx)}</td><td class="n tx">${bytes(i.tx)}</td></tr>`).join("");
    const isOpen = (port, proto) => {
      const p = proto.toLowerCase();
      if (fw.allows.some((a) => a.port === port && (a.proto === "both" || a.proto === p))) return true;
      return !fw.blocks.some((b) => b.enabled !== false && b.direction !== "out" && (b.proto === "both" || b.proto === p) &&
        (b.kind === "range" ? b.lo <= port && port <= b.hi : b.port === port));
    };
    $("#sys-ls").innerHTML = d.listening.map((l) => {
      const o = isOpen(l.port, l.proto);
      return `<tr><td><span class="port">${l.port}</span></td><td>${l.proto}</td><td>${esc(l.process) || '<span class="faint">—</span>'}</td>
        <td class="mono dim">${esc(l.addr)}${l.local_only ? ' <span class="chip">local only</span>' : ""}</td>
        <td>${l.port === fw.panel_port ? '<span class="chip ok">panel</span>' : `<span class="chip ${o ? "ok" : "bad"}">${o ? "open" : "closed"}</span>`}</td></tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">No listening sockets found</td></tr>`;
  }
  $("#pw-open").onclick = () => { $("#pw-user").value = ""; $("#pw-cur").value = $("#pw-new").value = ""; $("#pw-dlg").showModal(); };
  $("#pw-form").addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const r = await act($("#pw-save"), () => post("/api/account/password", { user: $("#pw-user").value.trim() || undefined, current: $("#pw-cur").value, new: $("#pw-new").value }), "Password updated");
    if (r) $("#pw-dlg").close();
  });

  /* ================================================================ refresh loop */
  const LOADERS = {
    overview: () => Promise.all([overview(), ovChart()]),
    traffic: () => Promise.all([traffic(), daily()]),
    ports: () => Promise.all([ports(), portChart()]),
    countries: countries, protocols: () => protocols(), firewall: firewall, system: system,
  };
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try { await LOADERS[page](); } catch (e) { if (e.message !== "auth") console.warn(page, e); }
    finally { busy = false; }
  }
  let tick = 0;
  setInterval(() => {
    if (document.hidden) return;
    tick++;
    live();
    const every = { overview: 15, traffic: 20, ports: 30, countries: 20, protocols: 20, firewall: 30, system: 10 }[page];
    if (tick % every === 0 && !document.querySelector("dialog[open]") && !(page === "firewall" && document.activeElement && document.activeElement.matches("input,select"))) refresh();
  }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { live(); refresh(); } });

  const start = (location.hash || "").slice(1);
  live();
  go(LOADERS[start] ? start : "overview");
})();
