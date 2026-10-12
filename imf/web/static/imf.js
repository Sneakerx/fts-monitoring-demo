/* IMF portal helpers: data fetching, Plotly charts, live status polling. */
const IMF = (() => {
  const C = { series: "#1f5fa8", warn: "#e0a800", alarm: "#c62828", grid: "#e6eaef", ink: "#1f2a37",
              palette: ["#1f5fa8", "#d1495b", "#2a9d8f", "#8e3b9a", "#e9a03b", "#3d5a80", "#6a994e", "#bc6c25"] };
  const cfg = { displaylogo: false, responsive: true,
                modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"] };

  async function json(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return null; }
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  }

  function toast(msg) {
    let t = document.querySelector(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3500);
  }

  function limitShapes(lim, yref) {
    const shapes = [];
    if (!lim) return shapes;
    const add = (y, color, dash) => { if (y !== null && y !== undefined)
      shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, yref, y0: y, y1: y,
                    line: { color, width: 1.4, dash } }); };
    add(lim.warn_lo, C.warn, "dash"); add(lim.warn_hi, C.warn, "dash");
    add(lim.alarm_lo, C.alarm, "solid"); add(lim.alarm_hi, C.alarm, "solid");
    return shapes;
  }

  function eventShapes(s, yref, end) {
    return (s.events || []).map(e => ({
      type: "rect", xref: "x", yref: yref + " domain",
      x0: e.time, x1: e.cleared || end || s.t[s.t.length - 1], y0: 0, y1: 1, layer: "below", line: { width: 0 },
      fillcolor: e.severity === "ALARM" ? "rgba(198,40,40,0.10)" : "rgba(224,168,0,0.13)" }));
  }

  function yRange(s) {
    if (!s.v.length) return undefined;
    let lo = Math.min(...s.v), hi = Math.max(...s.v);
    const span = Math.max(hi - lo, Math.abs(hi) * 1e-3, 1e-9), mid = (lo + hi) / 2;
    const lim = s.limit || {};
    for (const k of ["warn_lo", "warn_hi"]) {
      const v = lim[k];
      if (v !== null && v !== undefined && Math.abs(v - mid) < 3 * span) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
    const pad = 0.12 * ((hi - lo) || span);
    return [lo - pad, hi + pad];
  }

  function traces(s, axis, color, showLegend) {
    const hover = s.product
      ? s.product.map(p => `<br><span style="font-size:11px">${p}</span>`) : s.t.map(() => "");
    const tmpl = `%{x|%Y-%m-%d %H:%M}<br><b>%{y:.4g}</b> ${s.unit}%{text}<extra>${s.code}</extra>`;
    const base = { x: s.t, y: s.v, type: "scattergl", mode: "lines+markers", name: s.name,
      line: { width: 1.1, color }, marker: { size: 3.5, color }, xaxis: "x", yaxis: axis,
      text: hover, customdata: s.t.map((_, i) => i), showlegend: showLegend, hovertemplate: tmpl };
    const out = [base];
    [[1, C.warn, "warning"], [2, C.alarm, "alarm"]].forEach(([lvl, col, label]) => {
      const idx = s.level.map((l, i) => l === lvl ? i : -1).filter(i => i >= 0);
      if (idx.length) out.push({ x: idx.map(i => s.t[i]), y: idx.map(i => s.v[i]), type: "scattergl", mode: "markers",
        name: label, marker: { size: 6, color: col }, xaxis: "x", yaxis: axis, showlegend: false,
        text: idx.map(i => hover[i]), customdata: idx, hovertemplate: tmpl });
    });
    return out;
  }

  function annotationMarks(s, yref) {
    return (s.annotations || []).map(a => ({ x: a.time, y: 1, xref: "x", yref: yref + " domain",
      text: "✎", hovertext: `${a.author}: ${a.text}`, showarrow: false, font: { size: 14, color: "#8e3b9a" },
      yanchor: "top" }));
  }

  /* Single or stacked multi-parameter plot. data = /api/series response. */
  function plot(div, data, opts = {}) {
    const codes = Object.keys(data.series);
    const n = codes.length;
    if (!n) { div.innerHTML = '<p class="muted">No accessible data.</p>'; return; }
    const gap = 0.06, h = (1 - gap * (n - 1)) / n;
    const layout = { margin: { l: 64, r: 16, t: opts.compact ? 6 : 24, b: 36 }, hovermode: "closest",
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "#fff", font: { size: 11, color: C.ink },
      xaxis: { type: "date", gridcolor: C.grid, showspikes: true, spikemode: "across", spikethickness: 1,
               range: data.start && data.end ? [data.start, data.end] : undefined },
      shapes: [], annotations: [], showlegend: false };
    let traceList = [];
    codes.forEach((code, i) => {
      const s = data.series[code];
      const ax = i === 0 ? "y" : "y" + (i + 1);
      const key = i === 0 ? "yaxis" : "yaxis" + (i + 1);
      const top = 1 - i * (h + gap);
      layout[key] = { domain: [top - h, top], gridcolor: C.grid, zeroline: false, range: yRange(s),
                      title: opts.compact ? undefined : { text: n > 1 ? `${s.code}<br>[${s.unit}]` : `[${s.unit}]`, font: { size: 11 } } };
      const color = n > 1 ? C.palette[i % C.palette.length] : C.series;
      traceList = traceList.concat(traces(s, ax, color, n > 1));
      layout.shapes = layout.shapes.concat(limitShapes(s.limit, ax), eventShapes(s, ax, data.end));
      layout.annotations = layout.annotations.concat(annotationMarks(s, ax));
    });
    if (n > 1) layout.xaxis.anchor = "y" + n;  // time labels below the bottom panel
    if (opts.compact) layout.yaxis.title = undefined;
    Plotly.react(div, traceList, layout, cfg);
  }

  /* Live status polling for the nav indicator; calls onChange when new data arrived. */
  function watchStatus(onChange, intervalMs = 10000) {
    let last = null;
    const el = document.getElementById("live");
    async function tick() {
      try {
        const st = await json("/api/status");
        if (!st) return;
        if (el) {
          el.classList.toggle("busy", st.polling);
          el.querySelector("span.txt").textContent = st.polling ? "ingesting…" :
            (st.latest_sensing ? "data until " + st.latest_sensing.slice(0, 16).replace("T", " ") : "no data");
        }
        if (last !== null && st.products_processed !== last.products_processed) {
          const d = st.products_processed - last.products_processed;
          if (d > 0 && d < 50) toast(`${d} new product${d > 1 ? "s" : ""} ingested`);
          onChange && onChange(st, last);
        } else if (last === null) { onChange && onChange(st, null); }
        last = st;
      } catch (e) { console.warn(e); }
    }
    tick(); return setInterval(tick, intervalMs);
  }

  return { json, plot, toast, watchStatus, C, cfg };
})();
