/* scope.js — tiny dependency-free canvas charts for NetMon.
   TimeChart: multi-series lines / areas / stacked areas / grouped bars on a time
   axis, with crosshair tooltip, legend toggles, drag-to-zoom and double-click reset.
   LiveScope: oscilloscope-style rolling trace for the 1-second live feed.
   Donut: SVG ring with hover.
   Colours are read from CSS custom properties so both themes just work. */
(function (W) {
  "use strict";
  const D = document;
  const css = (n) => getComputedStyle(D.documentElement).getPropertyValue(n).trim();
  const color = (c) => (c && c.startsWith("--") ? css(c) : c);

  function alpha(c, a) {
    c = (c || "#888").trim();
    if (c.startsWith("rgb")) {
      const m = c.match(/[\d.]+/g) || [0, 0, 0];
      return `rgba(${m[0]},${m[1]},${m[2]},${a})`;
    }
    let h = c.replace("#", "");
    if (h.length === 3) h = h.split("").map((x) => x + x).join("");
    const n = parseInt(h.slice(0, 6), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  /* ---------------- formatting ---------------- */
  const U = ["B", "KB", "MB", "GB", "TB", "PB"];
  function bytes(n, d) {
    if (n == null || isNaN(n)) return "–";
    let i = 0, v = Math.abs(n);
    while (v >= 1024 && i < U.length - 1) { v /= 1024; i++; }
    const dec = d != null ? d : v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
    return (n < 0 ? "-" : "") + v.toFixed(dec) + " " + U[i];
  }
  const BU = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
  function bits(bytesPerSec, d) {
    if (bytesPerSec == null || isNaN(bytesPerSec)) return "–";
    let v = Math.abs(bytesPerSec) * 8, i = 0;
    while (v >= 1000 && i < BU.length - 1) { v /= 1000; i++; }
    const dec = d != null ? d : v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
    return v.toFixed(dec) + " " + BU[i];
  }
  function speed(bps, d) {
    return W.NM_UNITS === "bytes" ? bytes(bps, d) + "/s" : bits(bps, d);
  }
  function num(n) {
    if (n == null || isNaN(n)) return "–";
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return Math.round(n).toLocaleString("en-US");
  }
  const pad = (x) => String(x).padStart(2, "0");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function tlabel(ts, span) {
    const d = new Date(ts * 1000);
    if (span > 3 * 86400) return `${MON[d.getMonth()]} ${d.getDate()}`;
    if (span > 86400) return `${MON[d.getMonth()]} ${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (span <= 600) return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function tfull(ts, step) {
    const d = new Date(ts * 1000);
    let s = `${MON[d.getMonth()]} ${d.getDate()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (step < 60) s += ":" + pad(d.getSeconds());
    if (step >= 86400) s = `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()]} ${MON[d.getMonth()]} ${d.getDate()}`;
    else if (step > 1) {
      const e = new Date((ts + step) * 1000);
      s += ` – ${pad(e.getHours())}:${pad(e.getMinutes())}` + (step < 60 ? ":" + pad(e.getSeconds()) : "");
    }
    return s;
  }

  function niceStep(range, ticks) {
    const raw = range / Math.max(1, ticks);
    const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  function niceBytesStep(range, ticks) {
    if (range <= 0) return 1;
    const k = Math.max(0, Math.floor(Math.log(range) / Math.log(1024)));
    const base = Math.pow(1024, k);
    return niceStep(range / base, ticks) * base;
  }
  const TSTEPS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];

  function setupCanvas(cv, w, h) {
    const dpr = Math.min(W.devicePixelRatio || 1, 2);
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.max(1, Math.round(h * dpr));
    cv.style.width = w + "px";
    cv.style.height = h + "px";
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  /* ================================================================ TimeChart */
  class TimeChart {
    constructor(el, opt) {
      this.el = typeof el === "string" ? D.querySelector(el) : el;
      this.o = Object.assign({ height: 260, yfmt: (v) => num(v), tfmt: null, bars: false,
        stacked: false, legend: true, zoom: true, minY: 0, tipFmt: null }, opt || {});
      this.series = this.o.series.map((s) => Object.assign({ on: true, width: 1.8, fill: false }, s));
      this.ts = []; this.data = []; this.step = 1; this.view = null;
      this.el.classList.add("tc");
      this.el.innerHTML = "";
      if (this.o.legend) {
        this.leg = D.createElement("div"); this.leg.className = "tc-legend";
        this.el.appendChild(this.leg);
        this.drawLegend();
      }
      this.box = D.createElement("div"); this.box.className = "tc-box";
      this.cv = D.createElement("canvas");
      this.tip = D.createElement("div"); this.tip.className = "tc-tip";
      this.sel = D.createElement("div"); this.sel.className = "tc-sel";
      this.reset = D.createElement("button"); this.reset.className = "tc-reset";
      this.reset.type = "button"; this.reset.textContent = "Reset zoom";
      this.reset.onclick = () => { this.view = null; this.reset.style.display = "none"; this.draw(); };
      this.empty = D.createElement("div"); this.empty.className = "tc-empty";
      this.box.append(this.cv, this.sel, this.tip, this.reset, this.empty);
      this.el.appendChild(this.box);
      this.bind();
      this.ro = new ResizeObserver(() => this.draw());
      this.ro.observe(this.box);
      W.addEventListener("nm-theme", () => this.draw());
    }

    drawLegend() {
      this.leg.innerHTML = "";
      this.series.forEach((s, i) => {
        if (s.na) return;
        const b = D.createElement("button");
        b.type = "button";
        b.className = "tc-key" + (s.on ? "" : " off");
        b.innerHTML = `<i class="${s.dash ? "dash" : ""}" style="--c:${color(s.color)}"></i>${s.label}`;
        b.setAttribute("aria-pressed", s.on ? "true" : "false");
        b.onclick = () => { s.on = !s.on; this.drawLegend(); this.draw(); };
        this.leg.appendChild(b);
      });
    }

    setData(ts, cols, step) {
      this.ts = ts || []; this.data = cols || [];
      let na = false;
      this.series.forEach((s, i) => { const v = !this.data[i]; if (v !== !!s.na) na = true; s.na = v; });
      if (na && this.leg) this.drawLegend(); this.step = step || (ts.length > 1 ? ts[1] - ts[0] : 1);
      if (this.view && (this.view[0] < (this.ts[0] || 0) || this.view[1] > (this.ts[this.ts.length - 1] || 0))) this.view = null;
      this.draw();
    }

    range() {
      const n = this.ts.length;
      if (!n) return [0, 0, 0, -1];
      let a = 0, b = n - 1;
      if (this.view) {
        while (a < n - 1 && this.ts[a] < this.view[0]) a++;
        while (b > 0 && this.ts[b] > this.view[1]) b--;
        if (b <= a) { a = 0; b = n - 1; this.view = null; }
      }
      return [this.ts[a], this.ts[b] + (this.o.bars ? this.step : 0), a, b];
    }

    draw() {
      const w = this.box.clientWidth, h = this.o.height;
      if (!w) return;
      this.box.style.height = h + "px";
      const ctx = setupCanvas(this.cv, w, h);
      this.ctx = ctx;
      const ink3 = css("--ink-3"), line = css("--grid"), mono = css("--f-mono") || "monospace";
      const P = this.pad = { l: 64, r: 14, t: 10, b: 26 };
      const pw = w - P.l - P.r, ph = h - P.t - P.b;
      ctx.clearRect(0, 0, w, h);
      const [t0, t1, a, b] = this.range();
      this.r = { t0, t1, a, b, pw, ph };
      const vis = this.series.map((s, i) => ({ s, i })).filter((x) => x.s.on && this.data[x.i]);
      // y max
      let ymax = 0;
      const stackTop = [];
      for (let k = a; k <= b; k++) {
        let acc = 0;
        for (const { s, i } of vis) {
          const v = this.data[i][k];
          if (v == null) continue;
          if (this.o.stacked) acc += v; else if (v > ymax) ymax = v;
        }
        if (this.o.stacked) { stackTop[k] = acc; if (acc > ymax) ymax = acc; }
      }
      const hasData = b >= a && vis.some(({ i }) => this.data[i].slice(a, b + 1).some((v) => v != null && v !== 0));
      this.empty.textContent = this.o.emptyText || "No data in this range yet";
      this.empty.style.display = hasData ? "none" : "flex";
      const ticks = Math.max(2, Math.min(6, Math.floor(ph / 44)));
      const stepY = this.o.byteAxis ? niceBytesStep(ymax || 1, ticks) : niceStep(ymax || 1, ticks);
      ymax = Math.max(stepY, Math.ceil((ymax || 1) / stepY) * stepY);
      this.ymax = ymax;
      const X = (t) => P.l + ((t - t0) / Math.max(1, t1 - t0)) * pw;
      const Y = (v) => P.t + ph - (v / ymax) * ph;
      this.X = X; this.Y = Y;

      // grid
      ctx.font = `11px ${mono}`;
      ctx.lineWidth = 1;
      ctx.textBaseline = "middle"; ctx.textAlign = "right"; ctx.fillStyle = ink3;
      for (let v = 0; v <= ymax + 1e-9; v += stepY) {
        const y = Math.round(Y(v)) + 0.5;
        ctx.strokeStyle = line; ctx.beginPath(); ctx.moveTo(P.l, y); ctx.lineTo(w - P.r, y); ctx.stroke();
        ctx.fillText(this.o.yfmt(v), P.l - 8, y);
      }
      // time ticks
      const span = t1 - t0;
      if (span > 0) {
        const want = Math.max(2, Math.floor(pw / 92));
        let ts = TSTEPS.find((s) => span / s <= want) || 604800;
        const off = new Date().getTimezoneOffset() * 60;
        ctx.textAlign = "center"; ctx.textBaseline = "top";
        let first = Math.ceil((t0 - off) / ts) * ts + off;
        if (ts >= 86400) first = Math.ceil((t0 - off) / 86400) * 86400 + off; // local midnight
        for (let t = first; t <= t1; t += ts) {
          const x = Math.round(X(t)) + 0.5;
          ctx.strokeStyle = line; ctx.globalAlpha = 0.55;
          ctx.beginPath(); ctx.moveTo(x, P.t); ctx.lineTo(x, P.t + ph); ctx.stroke();
          ctx.globalAlpha = 1;
          ctx.fillText((this.o.tfmt || tlabel)(t, span), x, P.t + ph + 8);
        }
      }
      if (b < a) return;

      if (this.o.bars) this.drawBars(ctx, vis, a, b, X, Y);
      else this.drawLines(ctx, vis, a, b, X, Y);
      if (this.hover != null && this.hover >= a && this.hover <= b) this.drawHover(this.hover);
    }

    drawLines(ctx, vis, a, b, X, Y) {
      const base = new Array(this.ts.length).fill(0);
      const glow = css("--glow") !== "0";
      for (const { s, i } of vis) {
        const d = this.data[i], c = color(s.color);
        const pts = [];
        for (let k = a; k <= b; k++) {
          const v = d[k];
          if (v == null) { pts.push(null); continue; }
          const top = this.o.stacked ? base[k] + v : v;
          pts.push([X(this.ts[k]), Y(top), this.o.stacked ? Y(base[k]) : Y(0)]);
          if (this.o.stacked) base[k] += v;
        }
        // segments split by gaps
        const segs = []; let cur = [];
        for (const p of pts) { if (p) cur.push(p); else if (cur.length) { segs.push(cur); cur = []; } }
        if (cur.length) segs.push(cur);
        for (const sg of segs) {
          if (s.fill || this.o.stacked) {
            const g = ctx.createLinearGradient(0, this.pad.t, 0, this.pad.t + this.r.ph);
            g.addColorStop(0, alpha(c, this.o.stacked ? 0.55 : 0.32));
            g.addColorStop(1, alpha(c, this.o.stacked ? 0.25 : 0.02));
            ctx.beginPath();
            ctx.moveTo(sg[0][0], sg[0][1]);
            for (const p of sg) ctx.lineTo(p[0], p[1]);
            for (let j = sg.length - 1; j >= 0; j--) ctx.lineTo(sg[j][0], sg[j][2]);
            ctx.closePath(); ctx.fillStyle = g; ctx.fill();
          }
          ctx.save();
          if (glow && !this.o.stacked) { ctx.shadowColor = alpha(c, 0.75); ctx.shadowBlur = 8; }
          ctx.strokeStyle = c; ctx.lineWidth = s.width; ctx.lineJoin = "round";
          if (s.dash) ctx.setLineDash(s.dash);
          ctx.beginPath();
          sg.forEach((p, j) => (j ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
          if (sg.length === 1) { ctx.arc(sg[0][0], sg[0][1], 1.5, 0, 7); }
          ctx.stroke(); ctx.restore();
        }
      }
    }

    drawBars(ctx, vis, a, b, X, Y) {
      const n = Math.max(1, vis.length);
      const slot = Math.max(1, X(this.ts[a] + this.step) - X(this.ts[a]));
      const gap = Math.min(6, slot * 0.18);
      const bw = Math.max(1, (slot - gap * 2) / n);
      vis.forEach(({ s, i }, j) => {
        const c = color(s.color);
        ctx.fillStyle = alpha(c, 0.85);
        for (let k = a; k <= b; k++) {
          const v = this.data[i][k];
          if (!v) continue;
          const x = X(this.ts[k]) + gap + j * bw, y = Y(v);
          ctx.fillRect(x, y, Math.max(1, bw - 1), Y(0) - y);
        }
      });
    }

    idxAt(px) {
      const { t0, t1, a, b } = this.r;
      if (b < a) return null;
      const t = t0 + ((px - this.pad.l) / this.r.pw) * (t1 - t0) - (this.o.bars ? this.step / 2 : 0);
      let lo = a, hi = b;
      while (lo < hi) { const m = (lo + hi) >> 1; if (this.ts[m] < t) lo = m + 1; else hi = m; }
      if (lo > a && Math.abs(this.ts[lo - 1] - t) < Math.abs(this.ts[lo] - t)) lo--;
      return lo;
    }

    drawHover(k) {
      const ctx = this.ctx, P = this.pad;
      const x = this.X(this.ts[k]) + (this.o.bars ? (this.X(this.ts[k] + this.step) - this.X(this.ts[k])) / 2 : 0);
      ctx.save();
      ctx.strokeStyle = css("--ink-3"); ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, P.t); ctx.lineTo(Math.round(x) + 0.5, P.t + this.r.ph); ctx.stroke();
      ctx.restore();
      if (!this.o.bars) {
        let acc = 0;
        this.series.forEach((s, i) => {
          if (!s.on || !this.data[i]) return;
          const v = this.data[i][k]; if (v == null) return;
          acc += v;
          const y = this.Y(this.o.stacked ? acc : v);
          ctx.fillStyle = color(s.color);
          ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 7); ctx.fill();
        });
      }
      // tooltip
      let rows = "", tot = 0;
      this.series.forEach((s, i) => {
        if (!s.on || !this.data[i]) return;
        const v = this.data[i][k];
        if (v != null) tot += v;
        rows += `<div class="r"><i style="--c:${color(s.color)}"></i><span>${s.label}</span><b>${v == null ? "no data" : (s.fmt || this.o.tipFmt || this.o.yfmt)(v)}</b></div>`;
      });
      if (this.o.stacked && this.o.showTotal !== false) rows += `<div class="r tot"><span>Total</span><b>${(this.o.tipFmt || this.o.yfmt)(tot)}</b></div>`;
      this.tip.innerHTML = `<div class="t">${(this.o.tipTime || tfull)(this.ts[k], this.step)}</div>${rows}`;
      this.tip.style.display = "block";
      const tw = this.tip.offsetWidth, bw = this.box.clientWidth;
      let left = x + 14;
      if (left + tw > bw - 4) left = x - tw - 14;
      this.tip.style.left = Math.max(4, left) + "px";
      this.tip.style.top = P.t + 4 + "px";
    }

    bind() {
      let drag = null;
      const pos = (e) => { const r = this.cv.getBoundingClientRect(); const p = e.touches ? e.touches[0] : e; return [p.clientX - r.left, p.clientY - r.top]; };
      const move = (e) => {
        if (!this.r) return;
        const [x] = pos(e);
        if (drag) {
          const l = Math.max(this.pad.l, Math.min(drag, x)), r = Math.min(this.box.clientWidth - this.pad.r, Math.max(drag, x));
          Object.assign(this.sel.style, { display: "block", left: l + "px", width: r - l + "px", top: this.pad.t + "px", height: this.r.ph + "px" });
        }
        if (x < this.pad.l || x > this.box.clientWidth - this.pad.r) { leave(); return; }
        const k = this.idxAt(x);
        if (k !== this.hover) { this.hover = k; this.draw(); }
      };
      const leave = () => { if (this.hover != null) { this.hover = null; this.tip.style.display = "none"; this.draw(); } };
      this.cv.addEventListener("mousemove", move);
      this.cv.addEventListener("mouseleave", () => { leave(); });
      this.cv.addEventListener("touchmove", (e) => { move(e); }, { passive: true });
      this.cv.addEventListener("touchend", () => setTimeout(leave, 1600));
      if (this.o.zoom && !this.o.bars) {
        this.cv.addEventListener("mousedown", (e) => { drag = pos(e)[0]; });
        W.addEventListener("mouseup", (e) => {
          if (drag == null) return;
          const x = pos(e)[0]; const a = Math.min(drag, x), b = Math.max(drag, x);
          drag = null; this.sel.style.display = "none";
          if (b - a < 8 || !this.r) return;
          const { t0, t1, pw } = this.r;
          const ta = t0 + ((a - this.pad.l) / pw) * (t1 - t0), tb = t0 + ((b - this.pad.l) / pw) * (t1 - t0);
          if (tb - ta < this.step * 2) return;
          this.view = [ta, tb]; this.reset.style.display = "block"; this.draw();
        });
        this.cv.addEventListener("dblclick", () => { this.view = null; this.reset.style.display = "none"; this.draw(); });
      }
    }
  }

  /* ================================================================ LiveScope */
  class LiveScope {
    constructor(el, opt) {
      this.el = typeof el === "string" ? D.querySelector(el) : el;
      this.o = Object.assign({ seconds: 120 }, opt || {});
      this.cv = D.createElement("canvas");
      this.el.appendChild(this.cv);
      this.pts = [];
      this.ro = new ResizeObserver(() => this.draw());
      this.ro.observe(this.el);
      W.addEventListener("nm-theme", () => this.draw());
    }
    setData(pts) { this.pts = pts || []; this.draw(); }
    draw() {
      const w = this.el.clientWidth, h = this.el.clientHeight;
      if (!w || !h) return;
      const ctx = setupCanvas(this.cv, w, h);
      ctx.clearRect(0, 0, w, h);
      const grid = css("--grid"), gridHi = css("--grid-hi");
      // graticule: 12 x 6 divisions with minor ticks on the centre lines
      const cols = 12, rows = 6;
      ctx.lineWidth = 1;
      for (let i = 0; i <= cols; i++) {
        const x = Math.round((w / cols) * i) + 0.5;
        ctx.strokeStyle = i === cols / 2 ? gridHi : grid;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
      for (let j = 0; j <= rows; j++) {
        const y = Math.round((h / rows) * j) + 0.5;
        ctx.strokeStyle = j === rows / 2 ? gridHi : grid;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      }
      ctx.strokeStyle = gridHi;
      for (let i = 0; i <= cols * 5; i++) {
        const x = Math.round((w / (cols * 5)) * i) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, h / 2 - 3); ctx.lineTo(x, h / 2 + 3); ctx.stroke();
      }
      const n = this.o.seconds, pts = this.pts.slice(-n);
      if (pts.length < 2) return;
      let max = 1;
      for (const p of pts) max = Math.max(max, p[1], p[2]);
      max *= 1.15;
      this.max = max;
      const now = pts[pts.length - 1][0];
      const X = (t) => w - ((now - t) / (n - 1)) * w;
      const glow = css("--glow") !== "0";
      [[1, "--rx"], [2, "--tx"]].forEach(([k, cv]) => {
        const c = color(cv);
        const Y = (v) => h - 4 - (v / max) * (h - 8);
        const g = ctx.createLinearGradient(0, 0, 0, h);
        g.addColorStop(0, alpha(c, 0.22)); g.addColorStop(1, alpha(c, 0));
        ctx.beginPath();
        pts.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[k])) : ctx.moveTo(X(p[0]), Y(p[k]))));
        ctx.lineTo(X(now), h); ctx.lineTo(X(pts[0][0]), h); ctx.closePath();
        ctx.fillStyle = g; ctx.fill();
        ctx.save();
        if (glow) { ctx.shadowColor = c; ctx.shadowBlur = 12; }
        ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.lineJoin = "round";
        ctx.beginPath();
        pts.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[k])) : ctx.moveTo(X(p[0]), Y(p[k]))));
        ctx.stroke();
        const last = pts[pts.length - 1];
        ctx.fillStyle = c; ctx.beginPath(); ctx.arc(X(last[0]) - 2, Y(last[k]), 3.5, 0, 7); ctx.fill();
        ctx.restore();
      });
    }
  }

  /* ================================================================ Donut */
  function donut(el, items, opt) {
    el = typeof el === "string" ? D.querySelector(el) : el;
    opt = opt || {};
    const total = items.reduce((s, x) => s + (x.value || 0), 0);
    const R = 60, r = 44, C = 2 * Math.PI * ((R + r) / 2), sw = R - r;
    let off = 0, arcs = "";
    if (total > 0) {
      items.forEach((it, i) => {
        if (!it.value) return;
        const len = (it.value / total) * C;
        arcs += `<circle class="dn-arc" data-i="${i}" r="${(R + r) / 2}" cx="70" cy="70" fill="none" stroke="${color(it.color)}" stroke-width="${sw}" stroke-dasharray="${Math.max(0, len - 1.5)} ${C}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"/>`;
        off += len;
      });
    } else {
      arcs = `<circle r="${(R + r) / 2}" cx="70" cy="70" fill="none" stroke="var(--line)" stroke-width="${sw}"/>`;
    }
    const fmt = opt.fmt || num;
    el.innerHTML = `<div class="dn"><svg viewBox="0 0 140 140" role="img" aria-label="${opt.label || "share"}">${arcs}
      <text x="70" y="68" class="dn-v">${fmt(total)}</text><text x="70" y="86" class="dn-s">${opt.sub || "total"}</text></svg>
      <ul class="dn-legend">${items.map((it, i) => `<li data-i="${i}"><i style="--c:${color(it.color)}"></i><span>${it.label}</span><b>${total ? ((it.value / total) * 100).toFixed(it.value / total < 0.1 ? 1 : 0) : 0}%</b><em>${fmt(it.value)}</em></li>`).join("")}</ul></div>`;
    const svg = el.querySelector("svg"), vt = el.querySelector(".dn-v"), st = el.querySelector(".dn-s");
    const show = (i) => {
      if (i == null) { vt.textContent = fmt(total); st.textContent = opt.sub || "total"; svg.classList.remove("hl"); return; }
      vt.textContent = fmt(items[i].value); st.textContent = items[i].label;
      svg.classList.add("hl");
      svg.querySelectorAll(".dn-arc").forEach((a) => a.classList.toggle("on", a.dataset.i == i));
    };
    el.querySelectorAll("[data-i]").forEach((n) => {
      n.addEventListener("mouseenter", () => show(+n.dataset.i));
      n.addEventListener("mouseleave", () => show(null));
    });
  }

  W.Scope = { TimeChart, LiveScope, donut, bytes, bits, speed, num, tlabel, tfull, color, alpha, css };
})(window);
