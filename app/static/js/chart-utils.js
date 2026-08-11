/* global XLSX, Plotly */
/**
 * Chart Utilities for Customer Analytics Pages
 * Provides common functions for chart rendering, export, and empty state handling
 */

(function () {
  'use strict';

  /**
   * Check if array has valid data
   * @param {Array} arr - Array to check
   * @returns {boolean} - True if array has valid data
   */
  function hasData(arr) {
    return Array.isArray(arr) && arr.length > 0 && arr.some(v => v != null);
  }

  /**
   * Show loading spinner for a chart element
   * @param {string} elementId - ID of the chart container
   */
  function showLoadingSpinner(elementId) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.innerHTML = `
      <div class="d-flex justify-content-center align-items-center py-5">
        <div class="spinner-border text-primary" role="status">
          <span class="visually-hidden">Loading...</span>
        </div>
        <span class="ms-3 text-muted">Loading chart data...</span>
      </div>
    `;
  }

  /**
   * Show empty state message for a chart
   * @param {string} elementId - ID of the chart container
   * @param {string} message - Optional custom message
   */
  function showEmptyState(elementId, message = 'No data available for this chart.') {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.innerHTML = `
      <div class="text-muted small text-center py-5">
        <i class="bi bi-info-circle me-2"></i>${message}
      </div>
    `;
  }

  /**
   * Convert Plotly chart data to Array of Arrays (AOA) format for Excel export
   * @param {HTMLElement} gd - Plotly graph div element
   * @returns {Array<Array>} - Array of arrays suitable for Excel export
   */
  function plotlyToAOA(gd) {
    if (!gd || !gd.data || gd.data.length === 0) return [["No data"]];

    const traces = gd.data;
    const type0 = (traces[0].type || "").toLowerCase();

    // Heatmap
    if (type0.includes('heatmap')) {
      const z = traces[0].z || [];
      const x = (traces[0].x || []).map(String);
      const y = (traces[0].y || []).map(String);
      const header = [''].concat(x);
      const rows = [header];
      for (let i = 0; i < z.length; i++) {
        const r = [(y[i] != null ? y[i] : String(i + 1))];
        const row = z[i] || [];
        for (let j = 0; j < x.length; j++) {
          r.push((row[j] != null ? row[j] : ''));
        }
        rows.push(r);
      }
      return rows;
    }

    // Pie chart
    if (type0.includes('pie')) {
      const labels = traces[0].labels || [];
      const values = traces[0].values || [];
      const rows = [["Label", "Value"]];
      for (let i = 0; i < labels.length; i++) {
        rows.push([labels[i], values[i]]);
      }
      return rows;
    }

    // Histogram
    if (type0.includes('histogram')) {
      const x = traces[0].x || [];
      const rows = [["Raw Values"], ...x.map(v => [v])];
      return rows;
    }

    // Scatter/bubble
    if (type0.includes('scatter')) {
      const header = [];
      if (traces[0].x) header.push('X');
      if (traces[0].y) header.push('Y');
      if (traces[0].text) header.push('Label');
      const rows = [header];
      const maxLen = Math.max(...traces.map(t => (t.x?.length || 0)));
      for (let i = 0; i < maxLen; i++) {
        const row = [];
        if (traces[0].x) row.push(traces[0].x?.[i] ?? '');
        if (traces[0].y) row.push(traces[0].y?.[i] ?? '');
        if (traces[0].text) row.push(traces[0].text?.[i] ?? '');
        rows.push(row);
      }
      return rows;
    }

    // Default: Cartesian (bar/line)
    const hasX = traces.some(t => Array.isArray(t.x));
    const xRef = traces.find(t => Array.isArray(t.x))?.x;
    const maxLen = Math.max(...traces.map(t => (t.x?.length || t.y?.length || 0)));
    const header = [hasX ? 'X' : 'Index'];
    traces.forEach((t, i) => header.push(t.name || `Trace ${i + 1}`));
    const rows = [header];
    for (let i = 0; i < maxLen; i++) {
      const xVal = hasX ? (xRef?.[i] ?? (i + 1)) : i + 1;
      const row = [xVal];
      traces.forEach(t => {
        let v = t.y?.[i] ?? '';
        row.push(v != null ? v : '');
      });
      rows.push(row);
    }
    return rows;
  }

  /**
   * Convert Array of Arrays to Excel file and download
   * @param {Array<Array>} aoa - Array of arrays
   * @param {string} filename - Filename for download
   * @param {string} sheetName - Sheet name in Excel workbook
   */
  function aoaToExcel(aoa, filename = "chart.xlsx", sheetName = "Sheet1") {
    if (typeof XLSX === 'undefined') {
      // Fallback to CSV if XLSX not available
      const csv = aoa.map(r => r.map(v => `"${String(v ?? '').replace(/"/g, '""')}"`).join(',')).join('\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename.replace(/\.xlsx$/i, '') + '.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      return;
    }
    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    XLSX.utils.book_append_sheet(wb, ws, sheetName.slice(0, 31));
    XLSX.writeFile(wb, filename);
  }

  /**
   * Export Plotly chart to Excel
   * @param {string} divId - ID of the Plotly chart div
   * @param {string} filename - Optional filename
   * @param {string} sheetName - Optional sheet name
   */
  function exportPlotlyDiv(divId, filename, sheetName) {
    const gd = document.getElementById(divId);
    if (!gd || gd.style.display === 'none') {
      console.warn('Chart element not found or hidden:', divId);
      return;
    }

    const aoa = plotlyToAOA(gd);
    const card = gd.closest('.card');
    const titleEl = card?.querySelector('.card-title');
    const title = titleEl?.textContent?.trim() || sheetName || divId;
    const fname = filename || title.replace(/[^\w\d\-]+/g, '_') + '.xlsx';
    aoaToExcel(aoa, fname, title);
  }

  /**
   * Add export button to chart card title
   * @param {string} chartId - ID of the chart container
   */
  function addExportButton(chartId) {
    const el = document.getElementById(chartId);
    if (!el || el.innerHTML.includes('No data')) return;

    const card = el.closest('.card-body')?.closest('.card');
    if (!card) return;

    const titleEl = card.querySelector('.card-title');
    if (!titleEl || titleEl.querySelector('.btn-outline-success')) return;

    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-outline-success ms-auto';
    btn.innerHTML = '<i class="bi bi-file-earmark-spreadsheet"></i>';
    btn.title = 'Export to Excel';
    btn.setAttribute('aria-label', 'Export chart to Excel');

    btn.onclick = () => {
      const title = titleEl.textContent.trim();
      exportPlotlyDiv(chartId, title.replace(/[^\w\d\-]+/g, '_') + '.xlsx', title);
    };

    if (titleEl.classList.contains('d-flex')) {
      titleEl.appendChild(btn);
    } else {
      titleEl.classList.add('d-flex', 'align-items-center', 'justify-content-between');
      titleEl.appendChild(btn);
    }
  }

  /**
   * Safe Plotly plot with error handling and empty state
   * @param {string} divId - ID of the chart container
   * @param {Array} data - Plotly data array
   * @param {Object} layout - Plotly layout object
   * @param {Object} config - Plotly config object
   * @param {string} emptyMessage - Optional custom empty state message
   * @returns {Promise} - Promise from Plotly.newPlot
   */
  async function safePlotlyPlot(divId, data, layout = {}, config = {}, emptyMessage = null) {
    const el = document.getElementById(divId);
    if (!el) {
      console.error('Chart element not found:', divId);
      return Promise.reject(new Error('Element not found'));
    }

    // Check if data is valid
    const hasValidData = Array.isArray(data) && data.length > 0 &&
      data.some(trace => {
        return (hasData(trace.x) || hasData(trace.y) || hasData(trace.values) || hasData(trace.labels));
      });

    if (!hasValidData) {
      showEmptyState(divId, emptyMessage);
      return Promise.resolve();
    }

    // Default config
    const defaultConfig = {
      displayModeBar: false,
      responsive: true,
      ...config
    };

    // Default layout
    const defaultLayout = {
      margin: { t: 20, l: 60, r: 40, b: 60 },
      ...layout
    };

    try {
      return await Plotly.newPlot(divId, data, defaultLayout, defaultConfig);
    } catch (error) {
      console.error('Error plotting chart:', error);
      showEmptyState(divId, 'Error loading chart. Please try again.');
      return Promise.reject(error);
    }
  }

  /**
   * Load SheetJS library dynamically if not already loaded
   * @returns {Promise} - Promise that resolves when library is loaded
   */
  function loadSheetJS() {
    if (typeof XLSX !== 'undefined') {
      return Promise.resolve();
    }

    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js";
      script.crossOrigin = "anonymous";
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Failed to load SheetJS library'));
      document.head.appendChild(script);
    });
  }

  function getCssVar(name, fallback = "") {
    const styles = getComputedStyle(document.body || document.documentElement);
    const rootStyles = getComputedStyle(document.documentElement);
    return styles.getPropertyValue(name).trim() || rootStyles.getPropertyValue(name).trim() || fallback;
  }

  function getThemePalette() {
    return {
      textPrimary: getCssVar('--color-text-primary', '#132033'),
      textSecondary: getCssVar('--color-text-secondary', '#30455f'),
      textMuted: getCssVar('--color-text-muted', '#607590'),
      inverse: getCssVar('--color-text-inverse', '#f8fbff'),
      border: getCssVar('--color-border-default', 'rgba(29, 42, 67, 0.16)'),
      grid: getCssVar('--color-border-soft', 'rgba(29, 42, 67, 0.1)'),
      surface: getCssVar('--color-surface-card', 'rgba(255, 255, 255, 0.96)'),
      series: seriesPalette(),
    };
  }

  // Fixed-order categorical palette, read from the theme tokens so light and
  // dark each get the steps that were validated against their own surface.
  // Series colour follows the entity, not its rank, so callers ask for a slot
  // by index and keep that index for the life of the chart.
  const SERIES_FALLBACK = ['#199e70', '#3987e5', '#c98500', '#d55181', '#9085e9', '#d95926'];

  function seriesPalette() {
    return SERIES_FALLBACK.map((fallback, i) => getCssVar(`--wa-cat-${i + 1}`, fallback));
  }

  // A translucent version of a slot, for area fills under a line and for bar
  // bodies whose outline carries the identity.
  function seriesFill(index, alpha = 0.18) {
    const hex = seriesPalette()[index % 6] || SERIES_FALLBACK[index % 6];
    const match = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!match) return hex;
    const int = parseInt(match[1], 16);
    return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`;
  }

  function seriesColor(index) {
    return seriesPalette()[index % 6] || SERIES_FALLBACK[index % 6];
  }

  function themedPlotlyAxis(axis = {}, palette = getThemePalette()) {
    const next = { ...axis };
    const tickfont = { color: palette.textMuted, ...(axis.tickfont || {}) };
    const titlefont = { color: palette.textSecondary, ...(axis.titlefont || {}) };
    next.tickfont = tickfont;
    next.titlefont = titlefont;
    if (axis.showgrid !== false) next.gridcolor = axis.gridcolor || palette.grid;
    if (axis.zeroline !== false) next.zerolinecolor = axis.zerolinecolor || palette.grid;
    next.linecolor = axis.linecolor || palette.border;
    return next;
  }

  function themedPlotlyLayout(layout = {}) {
    const palette = getThemePalette();
    const next = {
      paper_bgcolor: layout.paper_bgcolor ?? 'rgba(0,0,0,0)',
      plot_bgcolor: layout.plot_bgcolor ?? 'rgba(0,0,0,0)',
      font: {
        color: palette.textSecondary,
        family: 'Manrope, Segoe UI, sans-serif',
        ...(layout.font || {}),
      },
      ...layout,
    };

    const axisKeys = [
      'xaxis', 'yaxis', 'xaxis2', 'yaxis2', 'xaxis3', 'yaxis3', 'xaxis4', 'yaxis4',
    ];
    axisKeys.forEach((key) => {
      if (next[key]) next[key] = themedPlotlyAxis(next[key], palette);
    });

    next.legend = {
      font: { color: palette.textSecondary, ...((layout.legend || {}).font || {}) },
      ...(layout.legend || {}),
    };

    if (Array.isArray(layout.annotations)) {
      next.annotations = layout.annotations.map((annotation) => ({
        font: { color: palette.textSecondary, ...(annotation.font || {}) },
        ...annotation,
      }));
    }

    return next;
  }

  function applyChartJsDefaults() {
    const ChartLib = window.Chart;
    if (!ChartLib) return;
    const palette = getThemePalette();
    ChartLib.defaults.color = palette.textMuted;
    ChartLib.defaults.borderColor = palette.grid;
    ChartLib.defaults.font.family = 'Manrope, Segoe UI, sans-serif';
    if (ChartLib.defaults.plugins?.legend?.labels) {
      ChartLib.defaults.plugins.legend.labels.color = palette.textSecondary;
    }
    if (ChartLib.defaults.plugins?.title) {
      ChartLib.defaults.plugins.title.color = palette.textPrimary;
    }
    if (ChartLib.defaults.scale?.grid) {
      ChartLib.defaults.scale.grid.color = palette.grid;
    }
    if (ChartLib.defaults.scale?.ticks) {
      ChartLib.defaults.scale.ticks.color = palette.textMuted;
    }
    if (ChartLib.defaults.scale?.title) {
      ChartLib.defaults.scale.title.color = palette.textSecondary;
    }
    ChartLib.__wholesaleContrastApplied = true;
  }

  function refreshPlotlyCharts() {
    const PlotlyLib = window.Plotly;
    if (!PlotlyLib || typeof PlotlyLib.relayout !== 'function') return;
    const palette = getThemePalette();
    const axisKeys = [
      'xaxis', 'yaxis', 'xaxis2', 'yaxis2', 'xaxis3', 'yaxis3', 'xaxis4', 'yaxis4',
    ];

    document.querySelectorAll('.js-plotly-plot').forEach((chart) => {
      const update = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        'font.color': palette.textSecondary,
        'legend.font.color': palette.textSecondary,
      };
      axisKeys.forEach((key) => {
        if (!chart.layout?.[key] && !chart._fullLayout?.[key]) return;
        update[`${key}.tickfont.color`] = palette.textMuted;
        update[`${key}.titlefont.color`] = palette.textSecondary;
        update[`${key}.gridcolor`] = palette.grid;
        update[`${key}.zerolinecolor`] = palette.grid;
        update[`${key}.linecolor`] = palette.border;
      });
      try {
        const pending = PlotlyLib.relayout(chart, update);
        if (pending && typeof pending.catch === 'function') pending.catch(() => {});
      } catch (_err) {
        // A chart may be purged while the theme is changing; the next render
        // still receives the themed defaults from the Plotly wrapper.
      }
    });
  }

  function installPlotlyTheme() {
    const PlotlyLib = window.Plotly;
    if (!PlotlyLib || PlotlyLib.__wholesaleContrastApplied) return;
    ['newPlot', 'react'].forEach((method) => {
      const original = PlotlyLib[method];
      if (typeof original !== 'function') return;
      PlotlyLib[method] = function patchedPlotlyTheme(gd, data, layout, config) {
        return original.call(this, gd, data, themedPlotlyLayout(layout || {}), config);
      };
    });
    PlotlyLib.__wholesaleContrastApplied = true;
  }

  function applyChartTheme() {
    applyChartJsDefaults();
    installPlotlyTheme();
  }

  // Export to global scope
  /* Run `draw` once Plotly exists.
   *
   * Plotly is fetched in a requestIdleCallback so a megabyte of renderer cannot
   * hold up the KPIs, which means page code frequently has its data *before*
   * the renderer arrives. Drawing straight away is a race, and every page that
   * lost it left an empty frame or a "Plotly not loaded." message that nothing
   * ever revisited. It only looked fine while the payloads were slow enough to
   * lose the race the other way.
   *
   * The timeout is a backstop for the renderer genuinely never arriving, so the
   * caller's own "no chart" handling runs rather than the frame sitting blank.
   */
  function whenPlotlyReady(draw, timeoutMs) {
    if (window.Plotly) {
      draw();
      return;
    }
    let done = false;
    const run = () => {
      if (done) return;
      done = true;
      draw();
    };
    window.addEventListener('wa:plotlyready', run, { once: true });
    window.setTimeout(run, typeof timeoutMs === 'number' ? timeoutMs : 8000);
  }

  window.ChartUtils = {
    hasData,
    whenPlotlyReady,
    showLoadingSpinner,
    showEmptyState,
    plotlyToAOA,
    aoaToExcel,
    exportPlotlyDiv,
    addExportButton,
    safePlotlyPlot,
    loadSheetJS,
    getThemePalette,
    themedPlotlyLayout,
    applyChartTheme,
    refreshPlotlyCharts,
    seriesPalette,
    seriesColor,
    seriesFill
  };

  applyChartTheme();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyChartTheme, { once: true });
  }

  // Plotly is loaded on idle for the primary workspaces, after this file has
  // already run. Install the wrapper before those pages render their charts.
  window.addEventListener('wa:plotlyready', applyChartTheme);

  // The toggle swaps the tokens; charts hold the values they resolved at draw
  // time, so refresh existing Plotly layouts as well as future chart defaults.
  window.addEventListener('wa:themechange', () => {
    applyChartTheme();
    refreshPlotlyCharts();
  });

})();
