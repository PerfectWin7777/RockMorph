/**
 * web/js/morphometry.js
 *
 * Client side of the RockMorph results viewer (web/morphometry.html).
 *
 * Python calls renderResults(payload) with:
 *
 *   {
 *     title, subtitle,
 *     indices: [{ key, name, unit, classification, classes: [{label, score}] | null }],
 *     basins:  [{ id, label, values: {key: number|null},
 *                 classes: {key: [label|null, score|null]},
 *                 warnings: [string], profile: {...} | null }],
 *     figures: [{ id, title, data, layout, note }]   // optional, prebuilt
 *   }
 *
 * and the page calls back into Python through the shared QWebChannel bridge:
 *
 *   bridge.select_basin(id)                 – highlight the basin on the map
 *   bridge.save_figure(dataUrl, fmt, name)  – save a chart (file dialog in Python)
 *
 * Design rules (see the data-viz guidance used across RockMorph):
 *   - one y-axis per plot; two measures = two stacked plots;
 *   - categorical colours in a fixed order, class colours as an ordinal
 *     single-hue ramp (class 1 darkest), read from CSS custom properties so
 *     light and dark themes stay in one place;
 *   - every chart has hover tooltips; the table is the accessible view.
 *
 * Authors: RockMorph contributors / Tony
 */

"use strict";

var STATE = {
    payload: null,
    sortKey: null,
    sortDir: 1,
    filter: "",
    selected: [],        // basin ids picked in the table (radar uses them)
    showClasses: true,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) {
        Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
}

/** Number formatting: 4 significant digits, thousands kept readable. */
function fmt(value) {
    if (value === null || value === undefined || !isFinite(value)) { return "—"; }
    var abs = Math.abs(value);
    if (abs !== 0 && (abs < 1e-3 || abs >= 1e6)) { return value.toExponential(2); }
    if (abs >= 1000) { return value.toFixed(0); }
    return Number(value.toPrecision(4)).toString();
}

function indexByKey(key) {
    var list = STATE.payload.indices;
    for (var i = 0; i < list.length; i++) { if (list[i].key === key) { return list[i]; } }
    return null;
}

function numericColumn(key) {
    return STATE.payload.basins.map(function (b) {
        var v = b.values[key];
        return (v === null || v === undefined || !isFinite(v)) ? null : v;
    });
}

function classColor(score) {
    return cssVar("--class-" + Math.min(Math.max(score || 4, 1), 4));
}

/** Plotly layout shared by every chart, themed from the CSS roles. */
function baseLayout(extra) {
    var ink = cssVar("--text-secondary");
    var grid = cssVar("--grid");
    var layout = {
        paper_bgcolor: cssVar("--surface-1"),
        plot_bgcolor: cssVar("--surface-1"),
        font: { family: "Segoe UI, system-ui, Arial, sans-serif", size: 12, color: ink },
        margin: { l: 64, r: 24, t: 40, b: 64 },
        hoverlabel: { bgcolor: cssVar("--surface-1"), bordercolor: cssVar("--border"),
                      font: { color: cssVar("--text-primary") } },
        xaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: cssVar("--border"), automargin: true },
        yaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: cssVar("--border"), automargin: true },
        showlegend: false,
    };
    return Object.assign(layout, extra || {});
}

var PLOT_CONFIG = { displaylogo: false, responsive: true,
                    modeBarButtonsToRemove: ["toImage", "lasso2d", "select2d"] };

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

function renderResults(payload) {
    STATE.payload = payload;
    STATE.sortKey = null;
    STATE.selected = [];
    document.getElementById("title").textContent = payload.title || "Results";
    document.getElementById("subtitle").textContent = payload.subtitle || "";
    var withProfiles = payload.basins.some(function (b) { return !!b.profile; });
    document.getElementById("tab-profiles").style.display = withProfiles ? "" : "none";
    renderTable();
    fillChartControls();
    fillProfileControls();
    renderDiagnostics();
    renderActiveTab();
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function activeTab() {
    var b = document.querySelector("nav button.active");
    return b ? b.getAttribute("data-tab") : "table";
}

function renderActiveTab() {
    var tab = activeTab();
    if (tab === "charts") { renderChart(); }
    if (tab === "profiles") { renderProfile(); }
}

document.getElementById("tabs").addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button) { return; }
    document.querySelectorAll("nav button").forEach(function (b) { b.classList.remove("active"); });
    document.querySelectorAll("main section").forEach(function (s) { s.classList.remove("active"); });
    button.classList.add("active");
    document.getElementById(button.getAttribute("data-tab")).classList.add("active");
    if (STATE.payload) { renderActiveTab(); }
});

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function sortedBasins() {
    var basins = STATE.payload.basins.slice();
    var f = STATE.filter.toLowerCase();
    if (f) {
        basins = basins.filter(function (b) {
            return String(b.label).toLowerCase().indexOf(f) >= 0 || String(b.id).toLowerCase().indexOf(f) >= 0;
        });
    }
    var key = STATE.sortKey;
    if (key) {
        basins.sort(function (a, b) {
            var va = key === "__label" ? a.label : a.values[key];
            var vb = key === "__label" ? b.label : b.values[key];
            if (va === null || va === undefined) { return 1; }     // missing values last
            if (vb === null || vb === undefined) { return -1; }
            if (typeof va === "string") { return STATE.sortDir * va.localeCompare(vb); }
            return STATE.sortDir * (va - vb);
        });
    }
    return basins;
}

function renderTable() {
    var payload = STATE.payload;
    var thead = document.querySelector("#grid thead");
    var tbody = document.querySelector("#grid tbody");
    thead.innerHTML = "";
    tbody.innerHTML = "";

    var head = el("tr");
    var first = el("th", { "data-key": "__label" }, "Basin");
    head.appendChild(first);
    payload.indices.forEach(function (ix) {
        var th = el("th", { "data-key": ix.key, title: ix.name + (ix.classification ? "\nClasses: " + ix.classification : "") });
        th.appendChild(document.createTextNode(ix.key));
        if (ix.unit) { th.appendChild(el("span", { "class": "unit" }, " (" + ix.unit + ")")); }
        head.appendChild(th);
    });
    head.querySelectorAll("th").forEach(function (th) {
        var key = th.getAttribute("data-key");
        if (key === STATE.sortKey) { th.classList.add(STATE.sortDir > 0 ? "sorted-asc" : "sorted-desc"); }
        th.addEventListener("click", function () {
            STATE.sortDir = (STATE.sortKey === key) ? -STATE.sortDir : 1;
            STATE.sortKey = key;
            renderTable();
        });
    });
    thead.appendChild(head);

    sortedBasins().forEach(function (b) {
        var tr = el("tr", { "data-id": String(b.id) });
        if (STATE.selected.indexOf(String(b.id)) >= 0) { tr.classList.add("selected"); }
        var name = el("td", null, b.label);
        if (b.warnings && b.warnings.length) {
            var w = el("span", { "class": "warn", title: b.warnings.join("\n") }, "⚠");
            name.appendChild(w);
        }
        tr.appendChild(name);
        payload.indices.forEach(function (ix) {
            var v = b.values[ix.key];
            var td = el("td", null, fmt(v));
            if (v === null || v === undefined) { td.classList.add("missing"); }
            var cls = b.classes ? b.classes[ix.key] : null;
            if (STATE.showClasses && cls && cls[0]) {
                var chip = el("span", { "class": "chip" + (cls[1] ? " c" + Math.min(cls[1], 4) : ""), title: cls[0] },
                              cls[1] !== null && cls[1] !== undefined ? String(cls[1]) : "•");
                td.appendChild(chip);
                td.title = cls[0];
            }
            tr.appendChild(td);
        });
        tr.addEventListener("click", function (event) { toggleSelection(String(b.id), event.ctrlKey || event.metaKey); });
        tbody.appendChild(tr);
    });
    if (!payload.basins.length) {
        var row = el("tr"); var td = el("td", { colspan: payload.indices.length + 1, "class": "empty" }, "No basin.");
        row.appendChild(td); tbody.appendChild(row);
    }
}

function toggleSelection(id, additive) {
    if (additive) {
        var i = STATE.selected.indexOf(id);
        if (i >= 0) { STATE.selected.splice(i, 1); } else { STATE.selected.push(id); }
    } else {
        STATE.selected = [id];
    }
    document.querySelectorAll("#grid tbody tr").forEach(function (tr) {
        tr.classList.toggle("selected", STATE.selected.indexOf(tr.getAttribute("data-id")) >= 0);
    });
    onBridgeReady(function (bridge) {
        if (bridge.select_basins) { bridge.select_basins(JSON.stringify(STATE.selected)); }
    });
}

document.getElementById("filter").addEventListener("input", function (e) {
    STATE.filter = e.target.value; if (STATE.payload) { renderTable(); }
});
document.getElementById("show-classes").addEventListener("change", function (e) {
    STATE.showClasses = e.target.checked; if (STATE.payload) { renderTable(); }
});

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------

function fillChartControls() {
    var payload = STATE.payload;
    var type = document.getElementById("chart-type");
    // Remove previous prebuilt figures, then add the current ones.
    Array.prototype.slice.call(type.options).forEach(function (o) {
        if (o.value.indexOf("fig:") === 0) { type.removeChild(o); }
    });
    (payload.figures || []).forEach(function (f) {
        type.appendChild(el("option", { value: "fig:" + f.id }, f.title));
    });
    if (payload.figures && payload.figures.length && payload.defaultFigure) {
        type.value = "fig:" + payload.defaultFigure;
    }
    ["chart-x", "chart-y"].forEach(function (id, n) {
        var select = document.getElementById(id);
        var previous = select.value;
        select.innerHTML = "";
        payload.indices.forEach(function (ix) {
            select.appendChild(el("option", { value: ix.key }, ix.key + " — " + ix.name));
        });
        if (previous && indexByKey(previous)) { select.value = previous; }
        else if (n === 1 && payload.indices.length > 1) { select.selectedIndex = 1; }
    });
    updateChartControls();
}

function updateChartControls() {
    var type = document.getElementById("chart-type").value;
    var showX = ["bar", "classes", "scatter"].indexOf(type) >= 0;
    var showY = type === "scatter";
    document.getElementById("chart-x").style.display = showX ? "" : "none";
    document.getElementById("lbl-x").style.display = showX ? "" : "none";
    document.getElementById("lbl-x").textContent = type === "scatter" ? "X" : "Index";
    document.getElementById("chart-y").style.display = showY ? "" : "none";
    document.getElementById("lbl-y").style.display = showY ? "" : "none";
}

["chart-type", "chart-x", "chart-y"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", function () {
        updateChartControls(); if (STATE.payload) { renderChart(); }
    });
});

function setNote(text) { document.getElementById("chart-note").textContent = text || ""; }

function renderChart() {
    var type = document.getElementById("chart-type").value;
    var target = document.getElementById("chart");
    if (!STATE.payload.basins.length) { Plotly.purge(target); setNote("No data."); return; }
    if (type.indexOf("fig:") === 0) { return renderPrebuilt(type.slice(4), target); }
    var renderers = { bar: chartBar, classes: chartClasses, scatter: chartScatter,
                      correlation: chartCorrelation, box: chartBox, radar: chartRadar };
    renderers[type](target);
}

function renderPrebuilt(id, target) {
    var fig = (STATE.payload.figures || []).filter(function (f) { return f.id === id; })[0];
    if (!fig) { return; }
    var layout = baseLayout(fig.layout || {});
    Plotly.react(target, fig.data, layout, PLOT_CONFIG);
    setNote(fig.note || "");
}

function chartBar(target) {
    var key = document.getElementById("chart-x").value;
    var ix = indexByKey(key);
    var basins = STATE.payload.basins.filter(function (b) { return b.values[key] !== null && b.values[key] !== undefined; });
    basins.sort(function (a, b) { return b.values[key] - a.values[key]; });
    var colors = basins.map(function (b) {
        var c = b.classes && b.classes[key];
        return c && c[1] ? classColor(c[1]) : cssVar("--series-1");
    });
    var trace = {
        type: "bar", x: basins.map(function (b) { return b.label; }),
        y: basins.map(function (b) { return b.values[key]; }),
        marker: { color: colors, line: { color: cssVar("--surface-1"), width: 1 } },
        customdata: basins.map(function (b) { var c = b.classes && b.classes[key]; return c && c[0] ? c[0] : ""; }),
        hovertemplate: "<b>%{x}</b><br>" + key + " = %{y:.4g}" + (ix.unit ? " " + ix.unit : "") + "<br>%{customdata}<extra></extra>",
    };
    Plotly.react(target, [trace], baseLayout({
        title: { text: ix.name + (ix.unit ? " (" + ix.unit + ")" : ""), font: { size: 14 } },
        xaxis: Object.assign(baseLayout().xaxis, { type: "category", tickangle: -45 }),
        yaxis: Object.assign(baseLayout().yaxis, { title: key }),
        bargap: 0.25,
    }), PLOT_CONFIG);
    setNote(ix.classes ? "Bar colour = class (" + ix.classification + "); darkest = class 1." : "");
}

function chartClasses(target) {
    var key = document.getElementById("chart-x").value;
    var ix = indexByKey(key);
    if (!ix.classes) {
        Plotly.purge(target);
        setNote("No classification is selected for " + key + ". Choose one in the index settings and run again.");
        return;
    }
    var counts = ix.classes.map(function () { return 0; });
    var unclassified = 0;
    STATE.payload.basins.forEach(function (b) {
        var c = b.classes && b.classes[key];
        var found = false;
        if (c && c[0]) {
            ix.classes.forEach(function (rule, i) { if (rule.label === c[0]) { counts[i]++; found = true; } });
        }
        if (!found) { unclassified++; }
    });
    // Show classes in score order (class 1 on top), whatever the order of
    // the rules in the scheme (rules are stored by increasing value).
    var order = ix.classes.map(function (_, i) { return i; });
    order.sort(function (a, b) { return (ix.classes[a].score || a + 1) - (ix.classes[b].score || b + 1); });
    counts = order.map(function (i) { return counts[i]; });
    var labels = order.map(function (i) { return ix.classes[i].label; });
    var colors = order.map(function (i) { return classColor(ix.classes[i].score || i + 1); });
    if (unclassified) { labels.push("No value"); counts.push(unclassified); colors.push(cssVar("--grid")); }
    Plotly.react(target, [{
        type: "bar", orientation: "h", y: labels, x: counts,
        marker: { color: colors }, text: counts.map(String), textposition: "outside",
        hovertemplate: "%{y}<br>%{x} basin(s)<extra></extra>",
    }], baseLayout({
        title: { text: ix.name + " — " + ix.classification, font: { size: 14 } },
        xaxis: Object.assign(baseLayout().xaxis, { title: "Number of basins", dtick: 1 }),
        yaxis: Object.assign(baseLayout().yaxis, { autorange: "reversed" }),
        margin: { l: 260, r: 40, t: 40, b: 56 },
    }), PLOT_CONFIG);
    setNote("");
}

function chartScatter(target) {
    var kx = document.getElementById("chart-x").value;
    var ky = document.getElementById("chart-y").value;
    var x = numericColumn(kx), y = numericColumn(ky);
    var px = [], py = [], names = [];
    STATE.payload.basins.forEach(function (b, i) {
        if (x[i] !== null && y[i] !== null) { px.push(x[i]); py.push(y[i]); names.push(b.label); }
    });
    var traces = [{
        type: "scatter", mode: "markers+text", x: px, y: py, text: names, textposition: "top center",
        textfont: { size: 10, color: cssVar("--text-secondary") },
        marker: { size: 9, color: cssVar("--series-1"), line: { color: cssVar("--surface-1"), width: 2 } },
        hovertemplate: "<b>%{text}</b><br>" + kx + " = %{x:.4g}<br>" + ky + " = %{y:.4g}<extra></extra>",
    }];
    var note = "";
    if (px.length >= 3) {
        var fit = linearFit(px, py);
        var xmin = Math.min.apply(null, px), xmax = Math.max.apply(null, px);
        traces.push({
            type: "scatter", mode: "lines", x: [xmin, xmax], y: [fit.a + fit.b * xmin, fit.a + fit.b * xmax],
            line: { color: cssVar("--text-muted"), width: 1.5, dash: "dot" }, hoverinfo: "skip",
        });
        var sign = fit.b < 0 ? " − " : " + ";
        note = "Least-squares line: " + ky + " = " + fmt(fit.a) + sign + fmt(Math.abs(fit.b)) + " × " + kx +
               "   ·   Pearson r = " + fmt(fit.r) + " (n = " + px.length + ")";
    }
    Plotly.react(target, traces, baseLayout({
        xaxis: Object.assign(baseLayout().xaxis, { title: kx }),
        yaxis: Object.assign(baseLayout().yaxis, { title: ky }),
    }), PLOT_CONFIG);
    setNote(note);
}

function linearFit(x, y) {
    var n = x.length, mx = 0, my = 0;
    for (var i = 0; i < n; i++) { mx += x[i]; my += y[i]; }
    mx /= n; my /= n;
    var sxx = 0, syy = 0, sxy = 0;
    for (i = 0; i < n; i++) { sxx += (x[i] - mx) * (x[i] - mx); syy += (y[i] - my) * (y[i] - my); sxy += (x[i] - mx) * (y[i] - my); }
    var b = sxx > 0 ? sxy / sxx : 0;
    return { a: my - b * mx, b: b, r: (sxx > 0 && syy > 0) ? sxy / Math.sqrt(sxx * syy) : NaN };
}

function chartCorrelation(target) {
    var keys = STATE.payload.indices.map(function (ix) { return ix.key; });
    var cols = keys.map(numericColumn);
    var z = keys.map(function (_, i) {
        return keys.map(function (_, j) {
            var a = [], b = [];
            cols[i].forEach(function (v, k) { if (v !== null && cols[j][k] !== null) { a.push(v); b.push(cols[j][k]); } });
            return a.length >= 3 ? linearFit(a, b).r : null;
        });
    });
    // Diverging scale: blue (negative) - gray midpoint - red (positive).
    var scale = [[0, "#184f95"], [0.25, "#6da7ec"], [0.5, cssVar("--grid") || "#f0efec"], [0.75, "#ec835a"], [1, "#d03b3b"]];
    Plotly.react(target, [{
        type: "heatmap", x: keys, y: keys, z: z, zmin: -1, zmax: 1, colorscale: scale, xgap: 2, ygap: 2,
        texttemplate: keys.length <= 15 ? "%{z:.2f}" : "", textfont: { size: 11 },
        colorbar: { title: "r", thickness: 12 },
        hovertemplate: "%{y} vs %{x}<br>Pearson r = %{z:.2f}<extra></extra>",
    }], baseLayout({
        xaxis: Object.assign(baseLayout().xaxis, { tickangle: -45, type: "category" }),
        yaxis: Object.assign(baseLayout().yaxis, { autorange: "reversed", type: "category" }),
        margin: { l: 110, r: 24, t: 24, b: 110 },
    }), PLOT_CONFIG);
    setNote("Pearson correlation between indices across basins (pairwise complete; blank = fewer than 3 basins).");
}

function zscores(values) {
    var v = values.filter(function (x) { return x !== null; });
    if (v.length < 2) { return values.map(function () { return null; }); }
    var m = v.reduce(function (s, x) { return s + x; }, 0) / v.length;
    var sd = Math.sqrt(v.reduce(function (s, x) { return s + (x - m) * (x - m); }, 0) / (v.length - 1));
    return values.map(function (x) { return x === null || sd === 0 ? null : (x - m) / sd; });
}

function chartBox(target) {
    var basins = STATE.payload.basins;
    var traces = STATE.payload.indices.map(function (ix) {
        var raw = numericColumn(ix.key);
        var z = zscores(raw);
        return {
            type: "box", name: ix.key, y: z, boxpoints: "all", jitter: 0.4, pointpos: 0,
            marker: { size: 6, color: cssVar("--series-1") }, line: { color: cssVar("--text-secondary"), width: 1 },
            fillcolor: "rgba(0,0,0,0)",
            text: basins.map(function (b, i) { return b.label + "<br>" + ix.key + " = " + fmt(raw[i]); }),
            hovertemplate: "%{text}<br>z = %{y:.2f}<extra></extra>",
        };
    });
    Plotly.react(target, traces, baseLayout({
        yaxis: Object.assign(baseLayout().yaxis, { title: "z-score (standard deviations from the mean)" }),
        xaxis: Object.assign(baseLayout().xaxis, { tickangle: -45 }),
    }), PLOT_CONFIG);
    setNote("Values standardised per index so indices with different units share one axis.");
}

function chartRadar(target) {
    var keys = STATE.payload.indices.map(function (ix) { return ix.key; });
    var cols = keys.map(numericColumn);
    var ranges = cols.map(function (c) {
        var v = c.filter(function (x) { return x !== null; });
        return [Math.min.apply(null, v), Math.max.apply(null, v)];
    });
    var ids = STATE.selected.length ? STATE.selected.slice(0, 3)
        : STATE.payload.basins.slice(0, 3).map(function (b) { return String(b.id); });
    var palette = [cssVar("--series-1"), cssVar("--series-2"), cssVar("--series-3")];
    var traces = [];
    ids.forEach(function (id, n) {
        var i = STATE.payload.basins.findIndex(function (b) { return String(b.id) === id; });
        if (i < 0) { return; }
        var b = STATE.payload.basins[i];
        var r = keys.map(function (_, k) {
            var v = cols[k][i], lo = ranges[k][0], hi = ranges[k][1];
            return v === null || !isFinite(lo) || hi === lo ? null : (v - lo) / (hi - lo);
        });
        traces.push({
            type: "scatterpolar", r: r.concat([r[0]]), theta: keys.concat([keys[0]]), name: b.label,
            fill: "toself", opacity: 0.55, line: { color: palette[n], width: 2 },
            customdata: keys.concat([keys[0]]).map(function (_, k) { return fmt(cols[k % keys.length][i]); }),
            hovertemplate: b.label + "<br>%{theta} = %{customdata} (scaled %{r:.2f})<extra></extra>",
        });
    });
    Plotly.react(target, traces, baseLayout({
        showlegend: true, legend: { orientation: "h", y: -0.08 },
        polar: { bgcolor: cssVar("--surface-1"),
                 radialaxis: { range: [0, 1], gridcolor: cssVar("--grid"), tickfont: { size: 10 } },
                 angularaxis: { gridcolor: cssVar("--grid") } },
    }), PLOT_CONFIG);
    setNote("Each index is min–max scaled across all basins (0 = lowest, 1 = highest). " +
            "Ctrl+click rows in the Table tab to choose up to three basins.");
}

// ---------------------------------------------------------------------------
// Profiles
// ---------------------------------------------------------------------------

function fillProfileControls() {
    var select = document.getElementById("profile-basin");
    select.innerHTML = "";
    STATE.payload.basins.forEach(function (b, i) {
        if (b.profile) { select.appendChild(el("option", { value: String(i) }, b.label)); }
    });
}

document.getElementById("profile-basin").addEventListener("change", renderProfile);

function renderProfile() {
    var select = document.getElementById("profile-basin");
    var target = document.getElementById("profile");
    if (!select.value) { Plotly.purge(target); return; }
    var b = STATE.payload.basins[Number(select.value)];
    var p = b.profile;
    var km = function (arr) { return arr.map(function (d) { return d / 1000; }); };
    var traces = [
        { type: "scatter", mode: "lines", x: km(p.distance), y: p.elevation, xaxis: "x", yaxis: "y",
          line: { color: cssVar("--series-1"), width: 2 },
          customdata: p.area_km2,
          hovertemplate: "Distance %{x:.2f} km<br>Elevation %{y:.0f} m<br>Drainage area %{customdata:.2f} km²<extra></extra>" },
        { type: "bar", x: km(p.sl_mid), y: p.sl, xaxis: "x", yaxis: "y2",
          marker: { color: cssVar("--series-2") },
          hovertemplate: "Reach centre %{x:.2f} km<br>SL = %{y:.0f}<extra></extra>" },
    ];
    var layout = baseLayout({
        title: { text: b.label, font: { size: 14 } },
        grid: { rows: 2, columns: 1, roworder: "top to bottom" },
        xaxis: Object.assign(baseLayout().xaxis, { title: "Distance from source (km)", anchor: "y2" }),
        yaxis: Object.assign(baseLayout().yaxis, { title: "Elevation (m)", domain: [0.42, 1] }),
        yaxis2: Object.assign(baseLayout().yaxis, { title: "SL", domain: [0, 0.32] }),
        bargap: 0.05,
    });
    Plotly.react(target, traces, layout, PLOT_CONFIG);
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

function renderDiagnostics() {
    var list = document.getElementById("diag-list");
    list.innerHTML = "";
    var count = 0;
    STATE.payload.basins.forEach(function (b) {
        if (!b.warnings || !b.warnings.length) { return; }
        count++;
        var box = el("div", { "class": "diag" });
        box.appendChild(el("h3", null, b.label));
        var ul = el("ul");
        b.warnings.forEach(function (w) { ul.appendChild(el("li", null, w)); });
        box.appendChild(ul);
        list.appendChild(box);
    });
    if (!count) { list.appendChild(el("div", { "class": "empty" }, "No warning: every basin was processed without remarks.")); }
    document.getElementById("diag-count").textContent = count ? "(" + count + ")" : "";
}

// ---------------------------------------------------------------------------
// Export through Python
// ---------------------------------------------------------------------------

document.querySelectorAll("button[data-export]").forEach(function (button) {
    button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-target") || "chart");
        var format = button.getAttribute("data-export");
        if (!target || !target.data) { return; }
        var width = target.offsetWidth || 1200, height = target.offsetHeight || 800;
        Plotly.toImage(target, { format: format, width: width, height: height, scale: format === "png" ? 3 : 1 })
            .then(function (url) {
                onBridgeReady(function (bridge) {
                    var name = (STATE.payload.title || "rockmorph").replace(/[^A-Za-z0-9_-]+/g, "_");
                    bridge.save_figure(url, format, name);
                });
            });
    });
});

window.addEventListener("resize", function () {
    ["chart", "profile"].forEach(function (id) {
        var node = document.getElementById(id);
        if (node && node.data) { Plotly.Plots.resize(node); }
    });
});
