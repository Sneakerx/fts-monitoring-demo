/* IMF portal helpers: data fetching, Plotly charts, live status polling. */
const IMF = (() => {
  const C = { series: "#1f5fa8", warn: "#e0a800", alarm: "#c62828", grid: "#e6eaef", ink: "#1f2a37",
              palette: ["#1f5fa8", "#d1495b", "#2a9d8f", "#8e3b9a", "#e9a03b", "#3d5a80", "#6a994e", "#bc6c25",
                        "#5e548e", "#9c6644", "#264653", "#e76f51"] };
  const OVERLAY = { DATA_GAP: "rgba(91,102,117,0.16)", L1_GAP: "rgba(209,73,91,0.12)",
                    CALIBRATION_MODE: "rgba(142,59,154,0.22)", OPERATIONS: "rgba(42,157,143,0.16)",
                    MANUAL: "rgba(233,160,59,0.18)" };
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

  function requirementShapes(req, yref) {
    if (!req) return [];
    const out = [];
    [req.lo, req.hi].forEach(y => { if (y !== null && y !== undefined)
      out.push({ type: "line", xref: "paper", x0: 0, x1: 1, yref, y0: y, y1: y, line: { color: "#2a9d8f", width: 1.2, dash: "dot" } }); });
    return out;
  }

  function lastTime(s, end) {
    let last = end || null;
    (s.traces || []).forEach(tr => { if (tr.t.length && (!last || tr.t[tr.t.length - 1] > last)) last = tr.t[tr.t.length - 1]; });
    return last;
  }

  function eventShapes(s, yref, end) {
    const stop = lastTime(s, end);
    return (s.events || []).filter(e => e.category !== "timeline").map(e => ({
      type: "rect", xref: "x", yref: yref + " domain",
      x0: e.time, x1: e.cleared || stop, y0: 0, y1: 1, layer: "below", line: { width: 0 },
      fillcolor: e.severity === "ALARM" ? "rgba(198,40,40,0.10)" : "rgba(224,168,0,0.13)" }));
  }

  function timelineShapes(entries, yref) {
    return (entries || []).map(e => ({
      type: "rect", xref: "x", yref: yref + " domain", x0: e.start, x1: e.stop || e.start, y0: 0, y1: 1,
      layer: "below", line: { width: e.stop ? 0 : 1, color: OVERLAY[e.type] || "rgba(0,0,0,.1)" },
      fillcolor: OVERLAY[e.type] || "rgba(0,0,0,0.08)" }));
  }

  function yRange(s) {
    const vals = [].concat(...(s.traces || []).map(t => t.v));
    if (!vals.length) return undefined;
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const span = Math.max(hi - lo, Math.abs(hi) * 1e-3, 1e-9), mid = (lo + hi) / 2;
    const lim = s.limit || {};
    for (const k of ["warn_lo", "warn_hi"]) {
      const v = lim[k];
      if (v !== null && v !== undefined && Math.abs(v - mid) < 3 * span) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
    const pad = 0.12 * ((hi - lo) || span);
    return [lo - pad, hi + pad];
  }

  function traces(s, key, axis, color, multi, showLegend) {
    const out = [];
    s.traces.forEach((tr, k) => {
      const col = multi ? C.palette[k % C.palette.length] : color;
      const hover = tr.product ? tr.product.map(p => `<br><span style="font-size:11px">${p}</span>`) : tr.t.map(() => "");
      const label = tr.dims ? tr.dims.replace(/;/g, " ") : s.name;
      const tmpl = `%{x|%Y-%m-%d %H:%M}<br><b>%{y:.5g}</b> ${s.unit}%{text}<extra>${s.code}${tr.dims ? " " + label : ""}</extra>`;
      out.push({ x: tr.t, y: tr.v, type: "scattergl", mode: "lines+markers", name: label,
        line: { width: multi ? 0.9 : 1.1, color: col }, marker: { size: multi ? 2.5 : 3.5, color: col },
        xaxis: "x", yaxis: axis, text: hover, customdata: tr.t.map((_, i) => i), meta: [key, k],
        showlegend: multi || showLegend, hovertemplate: tmpl });
      [[1, C.warn, "warning"], [2, C.alarm, "alarm"]].forEach(([lvl, c, lab]) => {
        const idx = tr.level.map((l, i) => l === lvl ? i : -1).filter(i => i >= 0);
        if (idx.length) out.push({ x: idx.map(i => tr.t[i]), y: idx.map(i => tr.v[i]), type: "scattergl",
          mode: "markers", name: lab, marker: { size: 6, color: c }, xaxis: "x", yaxis: axis, showlegend: false,
          text: idx.map(i => hover[i]), customdata: idx, meta: [key, k], hovertemplate: tmpl });
      });
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
    const multiSeries = codes.some(c => data.series[c].traces.length > 1);
    const layout = { margin: { l: 64, r: 16, t: opts.compact ? 6 : 24, b: 36 }, hovermode: "closest",
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "#fff", font: { size: 11, color: C.ink },
      xaxis: { type: "date", gridcolor: C.grid, showspikes: true, spikemode: "across", spikethickness: 1,
               range: data.start && data.end ? [data.start, data.end] : undefined },
      shapes: [], annotations: [], showlegend: n > 1 || multiSeries,
      legend: { orientation: "h", y: -0.12, font: { size: 10 } } };
    let traceList = [];
    codes.forEach((code, i) => {
      const s = data.series[code];
      const ax = i === 0 ? "y" : "y" + (i + 1);
      const key = i === 0 ? "yaxis" : "yaxis" + (i + 1);
      const top = 1 - i * (h + gap);
      layout[key] = { domain: [top - h, top], gridcolor: C.grid, zeroline: false, range: yRange(s),
                      title: opts.compact ? undefined : { text: n > 1 ? `${s.code}<br>[${s.unit}]` : `[${s.unit}]`, font: { size: 11 } } };
      const color = n > 1 ? C.palette[i % C.palette.length] : C.series;
      traceList = traceList.concat(traces(s, code, ax, color, s.traces.length > 1, n > 1));
      layout.shapes = layout.shapes.concat(limitShapes(s.limit, ax), requirementShapes(s.requirement, ax),
                                           eventShapes(s, ax, data.end), timelineShapes(data.timeline, ax));
      layout.annotations = layout.annotations.concat(annotationMarks(s, ax));
    });
    if (n > 1) layout.xaxis.anchor = "y" + n;  // time labels below the bottom panel
    if (opts.compact) { layout.yaxis.title = undefined; layout.showlegend = false; }
    Plotly.react(div, traceList, layout, cfg);
  }

  /* Spectra of a vector parameter (latest + mean over range) with the reference template. */
  function plotSpectrum(div, data) {
    const keys = Object.keys(data.series);
    if (!keys.length) { div.innerHTML = '<p class="muted">No spectra in the selected range.</p>'; return; }
    const n = keys.length, gap = 0.08, h = (1 - gap * (n - 1)) / n;
    const layout = { margin: { l: 64, r: 16, t: 20, b: 40 }, hovermode: "x unified", paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "#fff", font: { size: 11, color: C.ink }, legend: { orientation: "h", y: -0.08 }, annotations: [] };
    let tr = [];
    keys.forEach((k, i) => {
      const s = data.series[k], ax = i === 0 ? "" : String(i + 1), top = 1 - i * (h + gap);
      layout["xaxis" + ax] = { anchor: "y" + ax, gridcolor: C.grid, title: i === n - 1 ? { text: "wavenumber [cm⁻¹]" } : undefined };
      layout["yaxis" + ax] = { domain: [top - h, top], gridcolor: C.grid, title: { text: `${k || data.code}<br>[${data.unit}]`, font: { size: 10 } } };
      tr.push({ x: s.axis, y: s.mean, xaxis: "x" + ax, yaxis: "y" + ax, name: `mean of ${s.n} (${k})`, mode: "lines",
                line: { color: C.series, width: 1.4 }, showlegend: i === 0, legendgroup: "mean" });
      tr.push({ x: s.axis, y: s.latest.values, xaxis: "x" + ax, yaxis: "y" + ax, name: `latest ${s.latest.time.slice(0, 16)}`,
                mode: "lines", line: { color: "#8a94a3", width: 0.8 }, showlegend: i === 0, legendgroup: "latest" });
      const t = (data.template || {})[k];
      if (t) tr.push({ x: t.axis, y: t.values, xaxis: "x" + ax, yaxis: "y" + ax, name: `template v${t.version}`,
                       mode: "lines", line: { color: C.alarm, width: 1.2, dash: "dash" }, showlegend: i === 0, legendgroup: "tmpl" });
    });
    Plotly.react(div, tr, layout, cfg);
  }

  /* Ground tracks in an equirectangular lat/lon frame (works offline: no map tiles / topojson needed). */
  function plotTracks(div, data) {
    if (!data.tracks.length) { div.innerHTML = '<p class="muted">No geolocation data.</p>'; return; }
    const tr = data.tracks.map((t, i) => ({ type: "scattergl", mode: "markers", x: t.lon, y: t.lat,
      name: `orbit ${t.orbit || "?"}`, marker: { size: 5, color: C.palette[i % C.palette.length] },
      hovertemplate: `orbit ${t.orbit || "?"} · ${t.time.slice(0, 16)}<br>lat %{y:.2f}°, lon %{x:.2f}°<extra></extra>` }));
    Plotly.react(div, tr, { margin: { l: 50, r: 16, t: 10, b: 40 }, showlegend: false, paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "#f7f9fb", font: { size: 11, color: C.ink },
      xaxis: { range: [-180, 180], dtick: 30, gridcolor: C.grid, zeroline: true, title: { text: "longitude [°]" } },
      yaxis: { range: [-90, 90], dtick: 30, gridcolor: C.grid, zeroline: true, title: { text: "latitude [°]" },
               scaleanchor: "x", scaleratio: 1 } }, cfg);
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
        if (last !== null && st.products_total !== last.products_total) {
          const d = st.products_processed - last.products_processed;
          if (d > 0 && d < 50) toast(`${d} new L1 product${d > 1 ? "s" : ""} ingested`);
          onChange && onChange(st, last);
        } else if (last === null) { onChange && onChange(st, null); }
        last = st;
      } catch (e) { console.warn(e); }
    }
    tick(); return setInterval(tick, intervalMs);
  }

  return { json, plot, plotSpectrum, plotTracks, toast, watchStatus, C, cfg };
})();
