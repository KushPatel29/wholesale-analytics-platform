/**
 * Northgate Retail Analytics Executive Reporting Workspace
 * Enterprise-grade, live, interactive reporting layer.
 */

// Views, not "audiences". Each one is a real subset of the planner: what you
// look at depends on whether you are deciding what to buy, where the network is
// failing, or just what to do this week.
//
// Declared once and published onto the View control as a data attribute, so the
// prerendered build can honour the same four views without this file. A static
// page cannot re-render sections, but it can hide the ones a view excludes -
// and a select that does nothing is worse than no select at all.
const REPORT_VIEW_PRESETS = {
  full: ['plan', 'forecast', 'demand', 'inventory', 'service', 'matrix', 'exposure', 'actions'],
  demand: ['plan', 'forecast', 'demand', 'matrix', 'actions'],
  supply: ['plan', 'inventory', 'service', 'exposure', 'actions'],
  actions: ['plan', 'actions']
};

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, char => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  "'": '&#39;',
  '"': '&quot;'
})[char]);

class ReportWorkspace {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    if (!containerId) return;

    this.bundleUrl = this.container.getAttribute('data-bundle-url');
    this.state = {
      type: 'full',
      sections: ['plan', 'forecast', 'demand', 'inventory', 'service', 'matrix', 'exposure', 'actions'],
      data: null
    };

    // The planner owns five sections and no more. Sales Performance, Customer
    // Portfolio, Product Intelligence and Regional Momentum each have their own
    // page and were duplicated here; "AI Insights" and "Strategic Scenarios"
    // were template prose that asserted rather than measured. All removed.
    this.sectionRegistry = {
      plan: { label: 'Planning Position', icon: 'bi-speedometer2', render: this.renderPlanningPosition.bind(this) },
      forecast: { label: 'Forecast Accuracy', icon: 'bi-bullseye', render: this.renderForecastAccuracy.bind(this) },
      demand: { label: 'Demand Signal', icon: 'bi-graph-up-arrow', render: this.renderDemand.bind(this) },
      inventory: { label: 'Inventory & OTIF', icon: 'bi-box-seam', render: this.renderInventory.bind(this) },
      service: { label: 'Service Level', icon: 'bi-truck', render: this.renderService.bind(this) },
      matrix: { label: 'Demand vs Service', icon: 'bi-grid-3x3-gap', render: this.renderMatrix.bind(this) },
      exposure: { label: 'Vendor Exposure', icon: 'bi-diagram-2', render: this.renderExposure.bind(this) },
      actions: { label: 'What To Do', icon: 'bi-check2-circle', render: this.renderPlanActions.bind(this) }
    };

    this.init();
  }

  async init() {
    this.setupEventListeners();
    this.loadStateFromStorage();
    await this.refreshData();
  }

  setupEventListeners() {
    const typeSelect = document.getElementById('reportTypeSelect');
    if (typeSelect) typeSelect.dataset.staticSections = JSON.stringify(REPORT_VIEW_PRESETS);

    typeSelect?.addEventListener('change', (e) => {
      this.state.type = e.target.value;
      this.handleTypeChange();
      this.render();
    });

    document.getElementById('savePresetBtn')?.addEventListener('click', () => this.saveStateToStorage());
    document.querySelector('[data-print-report]')?.addEventListener('click', () => window.print());
  }

  handleTypeChange() {
    const toggles = document.getElementById('sectionToggles');
    // The toggle list is always built, and only revealed for "Choose sections".
    // Building it unconditionally is what lets the frozen page ship a working
    // checklist: the static runtime can bind buttons that are already there,
    // and cannot render ones that were never drawn.
    this.renderVisibilityToggles();
    if (this.state.type === 'custom') {
      toggles.style.display = 'block';
    } else {
      toggles.style.display = 'none';
      this.state.sections = REPORT_VIEW_PRESETS[this.state.type] || Object.keys(this.sectionRegistry);
    }
  }

  renderVisibilityToggles() {
    const container = document.getElementById('visibilityList');
    if (!container) return;
    container.innerHTML = Object.entries(this.sectionRegistry).map(([id, cfg]) => `
      <button type="button" class="section-toggle ${this.state.sections.includes(id) ? 'active' : ''}"
              data-id="${id}" aria-pressed="${this.state.sections.includes(id)}">
        ${cfg.label}
      </button>
    `).join('');

    container.querySelectorAll('.section-toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-id');
        if (this.state.sections.includes(id)) {
          this.state.sections = this.state.sections.filter(s => s !== id);
        } else {
          this.state.sections.push(id);
        }
        btn.classList.toggle('active');
        this.render();
      });
    });
  }

  async refreshData() {
    try {
      this.container.classList.add('is-loading');
      const response = await fetch(this.bundleUrl + window.location.search);
      if (!response.ok) throw new Error('Analytical Pipeline Failure');
      const bundle = await response.json();
      this.state.data = bundle.data || {};
      this.render();
      // The header said "Loading…" for the life of the page because nothing
      // ever cleared it. Report what the reader actually wants to know: how
      // old the figures are.
      this.setFreshness(bundle.meta || {});
      this.container.classList.remove('is-loading');
    } catch (err) {
      console.error(err);
      this.showError(err.message);
    }
  }

  setFreshness(meta) {
    const el = document.getElementById('lastSyncTime');
    if (!el) return;
    const ageSeconds = Number(meta.cache_age_seconds);
    let note;
    if (!Number.isFinite(ageSeconds) || ageSeconds < 60) {
      note = 'Just now';
    } else if (ageSeconds < 3600) {
      note = `${Math.round(ageSeconds / 60)} min ago`;
    } else {
      note = `${(ageSeconds / 3600).toFixed(1)} h ago`;
    }
    el.innerHTML = `<i class="bi bi-clock-history me-1"></i>Data as of ${note}`;
  }

  render() {
    this.renderNavigation();
    this.renderContent();
    this.renderVisibilityToggles();
    this.setupScrollSpy();
  }

  renderNavigation() {
    const navContainer = document.getElementById('dynamicNavItems');
    // Every section gets a nav item, whether or not the current view includes
    // it, with the excluded ones hidden. The prerendered page keeps whatever is
    // in the DOM, and a view that has to *add* a link is a view a page with no
    // JavaScript cannot switch to.
    navContainer.innerHTML = Object.entries(this.sectionRegistry).map(([id, cfg]) => `
        <a href="#section-${id}" class="report-nav-item" data-report-section="${id}"
           ${this.state.sections.includes(id) ? '' : 'hidden'}>
          <i class="bi ${cfg.icon}"></i>
          ${cfg.label}
        </a>
      `).join('');
  }

  renderContent() {
    const content = document.getElementById('reportContent');
    content.innerHTML = Object.entries(this.sectionRegistry).map(([id, cfg]) => `
        <section id="section-${id}" class="report-section" data-report-section="${id}"
                 ${this.state.sections.includes(id) ? '' : 'hidden'}>
          ${this.createSectionHeader(id, cfg.label)}
          <div id="content-${id}" class="section-body">
            ${cfg.render(this.state.data)}
          </div>
        </section>
      `).join('');
    // Sections are built as HTML strings, so anything that needs a live element
    // has to be drawn after they are in the document.
    this.drawCharts();
    this.enhanceScrollableCards();
  }

  enhanceScrollableCards() {
    this.cardResizeObserver?.disconnect();
    const cards = Array.from(this.container.querySelectorAll('.report-card'));
    const update = (card) => {
      const scrollable = card.scrollWidth > card.clientWidth + 1;
      if (!scrollable) {
        if (card.dataset.scrollRegionEnhanced === '1') {
          card.removeAttribute('tabindex');
          card.removeAttribute('role');
          card.removeAttribute('aria-labelledby');
          delete card.dataset.scrollRegionEnhanced;
        }
        return;
      }

      const section = card.closest('.report-section');
      const heading = section?.querySelector('.section-title');
      if (heading && !heading.id) heading.id = `${section.id}-heading`;
      card.tabIndex = 0;
      card.setAttribute('role', 'region');
      if (heading?.id) card.setAttribute('aria-labelledby', heading.id);
      card.dataset.scrollRegionEnhanced = '1';
    };

    cards.forEach(update);
    if ('ResizeObserver' in window) {
      this.cardResizeObserver = new ResizeObserver((entries) => {
        entries.forEach((entry) => update(entry.target));
      });
      cards.forEach((card) => this.cardResizeObserver.observe(card));
    }
    requestAnimationFrame(() => cards.forEach(update));
  }

  /* The two figures on this page that are genuinely two-dimensional.
   *
   * Both were rendered as grouped lists. A list can tell you Electronics is
   * down 25.5% and 92.4% on time; it cannot show you that one department sits
   * alone in the corner where demand is climbing into a lane that already
   * misses its dates. That is the entire question this page exists to answer,
   * so it should be the thing you see first. */
  drawCharts() {
    const draw = () => {
      this.drawQuadrantChart();
      this.drawDemandChart();
      this.drawExposureChart();
      this.drawServiceChart();
      this.drawCoverChart();
      this.drawForecastChart();
    };
    if (window.ChartUtils && window.ChartUtils.whenPlotlyReady) {
      window.ChartUtils.whenPlotlyReady(draw);
    } else if (window.Plotly) {
      draw();
    }
  }

  _plotTheme() {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
    return {
      text: pick('--wa-text-dim', '#94a3b8'),
      grid: pick('--wa-border', 'rgba(148,163,184,.22)'),
      accent: pick('--wa-accent', '#34d399'),
      danger: pick('--wa-danger', '#f87171'),
      // Below target but not critical, and over-cover, are both "look at this"
      // rather than "act now" - they need their own colour, not danger's.
      warn: pick('--wa-warning', '#fbbf24'),
      muted: pick('--wa-text-dim', '#64748b'),
    };
  }

  drawQuadrantChart() {
    const host = document.getElementById('planQuadrantChart');
    const points = this._plan().matrix || [];
    if (!host || !points.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const target = Number(this._plan().headline?.service_target_pct) || 92;
    const revenues = points.map(p => Number(p.revenue) || 0);
    const maxRevenue = Math.max(...revenues, 1);

    // Only "at risk" is coloured. Everything else is context, and colouring all
    // four quadrants would make the one that matters compete with three that
    // do not.
    const atRisk = points.filter(p => p.quadrant === 'at_risk');
    const others = points.filter(p => p.quadrant !== 'at_risk');
    const series = (rows, colour, name) => ({
      x: rows.map(p => Number(p.change_pct) || 0),
      y: rows.map(p => Number(p.on_time_pct) || 0),
      text: rows.map(p => p.label),
      name,
      type: 'scatter',
      mode: 'markers+text',
      textposition: 'top center',
      textfont: { size: 10, color: theme.text },
      marker: {
        color: colour,
        opacity: colour === theme.danger ? 0.9 : 0.55,
        size: rows.map(p => 14 + 34 * Math.sqrt((Number(p.revenue) || 0) / maxRevenue)),
        line: { width: 1, color: 'rgba(0,0,0,.35)' },
      },
      hovertemplate:
        '<b>%{text}</b><br>Demand %{x:+.1f}%<br>On time %{y:.1f}%<extra></extra>',
    });

    const data = [];
    if (others.length) data.push(series(others, theme.muted, 'Within tolerance'));
    if (atRisk.length) data.push(series(atRisk, theme.danger, 'Growing and unreliable'));

    Plotly.newPlot(host, data, {
      margin: { l: 54, r: 18, t: 10, b: 44 },
      height: 380,
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      xaxis: {
        title: 'Demand change vs prior window',
        ticksuffix: '%', zeroline: false,
        gridcolor: theme.grid, linecolor: theme.grid,
      },
      yaxis: {
        title: 'On-time delivery', ticksuffix: '%',
        gridcolor: theme.grid, linecolor: theme.grid,
      },
      // The two lines that turn a scatter into a quadrant: no demand growth,
      // and the service level the business has committed to.
      shapes: [
        { type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1,
          line: { color: theme.grid, width: 1, dash: 'dot' } },
        { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: target, y1: target,
          line: { color: theme.grid, width: 1, dash: 'dot' } },
      ],
      annotations: [{
        xref: 'paper', yref: 'y', x: 1, y: target, xanchor: 'right', yanchor: 'bottom',
        text: `Service target ${target}%`, showarrow: false,
        font: { size: 10, color: theme.text },
      }],
      showlegend: data.length > 1,
      legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
    }, { displayModeBar: false, responsive: true });
  }

  /* Concentration on its own is not a finding. The table below lists share and
   * the vendor's on-time rate in adjacent columns, which leaves the reader to
   * join them by eye across six rows; the point of the section is the *pair*.
   * Plotting one against the other puts the departments that are both
   * single-sourced and badly served in their own corner, which is the only
   * quadrant anyone has to act on this week. */
  drawExposureChart() {
    const host = document.getElementById('planExposureChart');
    const rows = (this._plan().concentration || []).filter(
      r => Number.isFinite(Number(r.share_pct)) && Number.isFinite(Number(r.vendor_on_time_pct))
    );
    if (!host || !rows.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const warn = this._plan().thresholds?.concentration_warn_pct ?? 45;
    const target = this._plan().thresholds?.service_target_pct ?? 92;
    const revenues = rows.map(r => Number(r.revenue) || 0);
    const maxRevenue = Math.max(...revenues, 1);

    const atRisk = r => Number(r.share_pct) >= warn && Number(r.vendor_on_time_pct) < target;

    Plotly.newPlot(host, [{
      type: 'scatter',
      mode: 'markers+text',
      x: rows.map(r => Number(r.share_pct)),
      y: rows.map(r => Number(r.vendor_on_time_pct)),
      text: rows.map(r => r.department),
      textposition: 'top center',
      textfont: { size: 9, color: theme.text },
      marker: {
        // Area-proportional, so a department carrying twice the revenue looks
        // twice as big rather than twice as wide.
        size: revenues.map(v => 12 + 26 * Math.sqrt(v / maxRevenue)),
        color: rows.map(r => (atRisk(r) ? theme.danger : theme.accent)),
        opacity: 0.75,
        line: { width: 1, color: 'rgba(0,0,0,.25)' },
      },
      customdata: rows.map(r => [r.vendor, r.vendor_count, r.revenue]),
      hovertemplate:
        '<b>%{text}</b><br>%{customdata[0]}<br>%{x:.0f}% of the department'
        + '<br>%{y:.0f}% on time<br>%{customdata[1]} vendors available<extra></extra>',
    }], {
      margin: { l: 56, r: 24, t: 10, b: 46 },
      height: 300,
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      showlegend: false,
      xaxis: {
        title: 'Share carried by the largest vendor', ticksuffix: '%',
        gridcolor: theme.grid, linecolor: theme.grid, zeroline: false,
      },
      yaxis: {
        title: 'That vendor’s on-time rate', ticksuffix: '%',
        gridcolor: theme.grid, linecolor: theme.grid, zeroline: false,
      },
      shapes: [
        // The two thresholds the page already judges every other number by.
        { type: 'line', x0: warn, x1: warn, yref: 'paper', y0: 0, y1: 1,
          line: { color: theme.grid, width: 1, dash: 'dash' } },
        { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: target, y1: target,
          line: { color: theme.grid, width: 1, dash: 'dash' } },
      ],
      annotations: [
        { xref: 'paper', yref: 'paper', x: 1, y: 0, xanchor: 'right', yanchor: 'bottom',
          text: 'single-sourced and missing dates', showarrow: false,
          font: { size: 9, color: theme.danger } },
      ],
    }, { displayModeBar: false, responsive: true });
  }

  /* The service table already prints an on-time rate per lane and a status
   * chip. What it cannot show is the two things that decide whether a number
   * is worth acting on: how far it sits from target, and whether it rests on
   * enough deliveries to mean anything. `_service_by` only ranks groups with
   * 25+ lines for exactly that reason, and the table expresses it as a footnote.
   * Here the thin-sample lanes are drawn hollow, so "below target" and "too
   * few deliveries to say" cannot be mistaken for each other. */
  drawServiceChart() {
    const host = document.getElementById('planServiceChart');
    const plan = this._plan();
    const rows = (plan.service_by_lane || []).filter(
      r => Number.isFinite(Number(r.on_time_pct))
    );
    if (!host || !rows.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const target = plan.thresholds?.service_target_pct ?? 92;
    const critical = plan.thresholds?.service_critical_pct ?? 85;
    const ordered = [...rows].sort((a, b) => Number(a.on_time_pct) - Number(b.on_time_pct));
    const colourFor = (row) => {
      const value = Number(row.on_time_pct);
      if (value < critical) return theme.danger;
      if (value < target) return theme.warn || theme.danger;
      return theme.accent;
    };

    Plotly.newPlot(host, [{
      type: 'bar',
      orientation: 'h',
      x: ordered.map(r => Number(r.on_time_pct)),
      y: ordered.map(r => r.label),
      marker: {
        color: ordered.map(r => (r.reliable_estimate ? colourFor(r) : 'rgba(0,0,0,0)')),
        line: {
          width: ordered.map(r => (r.reliable_estimate ? 0 : 1.5)),
          color: ordered.map(r => colourFor(r)),
        },
        opacity: 0.85,
      },
      customdata: ordered.map(r => [r.lines, r.reliable_estimate ? '' : ' · too few lines to rank']),
      hovertemplate: '<b>%{y}</b><br>%{x:.1f}% on time<br>%{customdata[0]:,} lines%{customdata[1]}<extra></extra>',
    }], {
      margin: { l: 130, r: 24, t: 10, b: 40 },
      height: Math.max(200, 28 * ordered.length + 60),
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      showlegend: false,
      xaxis: {
        title: `On-time rate (target ${target}%)`, ticksuffix: '%',
        range: [Math.min(60, ...ordered.map(r => Number(r.on_time_pct))) - 5, 100],
        gridcolor: theme.grid, linecolor: theme.grid, zeroline: false,
      },
      yaxis: { automargin: true, gridcolor: 'rgba(0,0,0,0)' },
      shapes: [
        { type: 'line', x0: target, x1: target, yref: 'paper', y0: 0, y1: 1,
          line: { color: theme.grid, width: 1, dash: 'dash' } },
      ],
      annotations: [
        { x: target, xanchor: 'left', yref: 'paper', y: 1, yanchor: 'top',
          text: ` target ${target}%`, showarrow: false, font: { size: 9, color: theme.text } },
        { xref: 'paper', x: 0, xanchor: 'left', yref: 'paper', y: -0.16, yanchor: 'top',
          text: 'hollow bars: fewer lines than the reporting floor', showarrow: false,
          font: { size: 9, color: theme.muted || theme.text } },
      ],
    }, { displayModeBar: false, responsive: true });
  }

  /* Cover is the other half of the same decision and the table buries it in a
   * "34d / 45d" cell. Perishable departments run short cover deliberately, so a
   * single bar of days says nothing - what matters is the distance from each
   * department's own target, which is what this draws. */
  drawCoverChart() {
    const host = document.getElementById('planCoverChart');
    const rows = ((this._plan().inventory || {}).by_department || []).filter(
      r => Number.isFinite(Number(r.cover_days)) && Number(r.cover_target_days) > 0
    );
    if (!host || !rows.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const ordered = [...rows]
      .map(r => ({
        label: r.label,
        cover: Number(r.cover_days),
        target: Number(r.cover_target_days),
        value: Number(r.on_hand_value) || 0,
        gap: Number(r.cover_days) - Number(r.cover_target_days),
      }))
      .sort((a, b) => a.gap - b.gap);

    Plotly.newPlot(host, [{
      type: 'bar',
      orientation: 'h',
      x: ordered.map(r => r.gap),
      y: ordered.map(r => r.label),
      marker: {
        // Short of target is a service risk; long is cash sitting still.
        color: ordered.map(r => (r.gap < 0 ? theme.danger : theme.warn || theme.accent)),
        opacity: 0.85,
      },
      customdata: ordered.map(r => [r.cover, r.target, r.value]),
      hovertemplate:
        '<b>%{y}</b><br>%{customdata[0]:.0f} days cover vs %{customdata[1]:.0f} day target'
        + '<br>%{customdata[2]:$,.0f} on hand<extra></extra>',
    }], {
      margin: { l: 130, r: 24, t: 10, b: 44 },
      height: Math.max(200, 28 * ordered.length + 70),
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      showlegend: false,
      xaxis: {
        title: 'Days above (right) or below (left) the department target',
        gridcolor: theme.grid, linecolor: theme.grid,
        zeroline: true, zerolinecolor: theme.text, zerolinewidth: 1,
      },
      yaxis: { automargin: true, gridcolor: 'rgba(0,0,0,0)' },
    }, { displayModeBar: false, responsive: true });
  }

  drawDemandChart() {
    const host = document.getElementById('planDemandChart');
    const rows = (this._plan().demand || []).slice(0, 10);
    if (!host || !rows.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const ordered = [...rows].sort(
      (a, b) => (Number(a.change_pct) || 0) - (Number(b.change_pct) || 0)
    );

    Plotly.newPlot(host, [{
      type: 'bar',
      orientation: 'h',
      x: ordered.map(r => Number(r.change_pct) || 0),
      y: ordered.map(r => r.label),
      marker: {
        color: ordered.map(r => ((Number(r.change_pct) || 0) < 0 ? theme.danger : theme.accent)),
        opacity: 0.85,
      },
      hovertemplate: '<b>%{y}</b><br>%{x:+.1f}% vs prior window<extra></extra>',
    }], {
      margin: { l: 150, r: 24, t: 10, b: 38 },
      height: Math.max(220, 30 * ordered.length + 60),
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      xaxis: {
        title: 'Change vs prior window', ticksuffix: '%',
        zerolinecolor: theme.grid, gridcolor: theme.grid, linecolor: theme.grid,
      },
      yaxis: { automargin: true, gridcolor: 'rgba(0,0,0,0)' },
      showlegend: false,
    }, { displayModeBar: false, responsive: true });
  }

  drawForecastChart() {
    const host = document.getElementById('planForecastChart');
    const rows = this._plan().forecast_accuracy?.series || [];
    if (!host || !rows.length || !window.Plotly) return;

    const theme = this._plotTheme();
    const x = rows.map(row => row.period);
    Plotly.newPlot(host, [
      {
        x, y: rows.map(row => row.lower), type: 'scatter', mode: 'lines',
        line: { width: 0 }, hoverinfo: 'skip', showlegend: false,
      },
      {
        x, y: rows.map(row => row.upper), type: 'scatter', mode: 'lines',
        line: { width: 0 }, fill: 'tonexty', fillcolor: 'rgba(251,191,36,.14)',
        name: '+/-10% tolerance', hoverinfo: 'skip',
      },
      {
        x, y: rows.map(row => row.actual), type: 'scatter', mode: 'lines+markers',
        name: 'Actual', line: { color: theme.accent, width: 3 },
        hovertemplate: '<b>%{x}</b><br>Actual %{y:$,.0f}<extra></extra>',
      },
      {
        x, y: rows.map(row => row.forecast), type: 'scatter', mode: 'lines+markers',
        name: 'Forecast', line: { color: theme.warn, width: 2, dash: 'dash' },
        hovertemplate: '<b>%{x}</b><br>Forecast %{y:$,.0f}<extra></extra>',
      },
    ], {
      margin: { l: 66, r: 20, t: 10, b: 48 }, height: 350,
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, size: 11 },
      xaxis: { title: 'Scored month', gridcolor: theme.grid, linecolor: theme.grid },
      yaxis: { title: 'Sales revenue', tickprefix: '$', tickformat: ',.2s', gridcolor: theme.grid, linecolor: theme.grid },
      legend: { orientation: 'h', y: -0.24 },
    }, { displayModeBar: false, responsive: true });
  }

  createSectionHeader(id, title) {
    const eyebrows = {
      plan: 'Where the plan stands',
      forecast: 'How the demand signal performs out of sample',
      demand: 'Which way demand is moving',
      inventory: 'What is on hand and whether it arrives complete',
      service: 'How reliably we can supply it',
      matrix: 'Where the two disagree',
      exposure: 'Who we depend on',
      actions: 'Ranked by what it costs to ignore'
    };
    return `
      <div class="section-header">
        <div>
          <span class="section-eyebrow">${escapeHtml(eyebrows[id] || 'Reporting Layer')}</span>
          <h2 class="section-title">${escapeHtml(title)}</h2>
        </div>
      </div>
    `;
  }

  // --- Section Renderers ---
  //
  // Every figure below comes from `data.planning`, computed in
  // app/services/planning.py. Nothing on this page is written by a template
  // that did not read the data first.

  _plan() {
    return this.state.data?.planning || {};
  }

  /* House rules (app/static/js/format.js), not a private compact format. This
   * printed "Revenue exposed $1.2M" where every other page prints the same
   * figure in full, and `formatting.py` is explicit that compact currency is
   * for axis ticks and narrow cards only - never a KPI a reader might
   * reconcile against another page, because the rounding differs by design. */
  _money(value) {
    const F = window.WAFormat;
    if (F) return F.currency(value);
    const n = Number(value);
    return Number.isFinite(n) ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—';
  }

  _pct(value, digits = 1) {
    const F = window.WAFormat;
    if (F) return F.percent(value, { decimals: digits });
    const n = Number(value);
    return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '—';
  }

  _delta(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '<span class="plan-delta plan-delta--new">new this window</span>';
    const cls = n >= 8 ? 'plan-delta--up' : n <= -8 ? 'plan-delta--down' : 'plan-delta--flat';
    const arrow = n >= 8 ? '↑' : n <= -8 ? '↓' : '→';
    return `<span class="plan-delta ${cls}">${arrow} ${Math.abs(n).toFixed(1)}%</span>`;
  }

  _statusChip(status) {
    const map = {
      ok: ['plan-chip--ok', 'On target'],
      watch: ['plan-chip--watch', 'Below target'],
      critical: ['plan-chip--critical', 'Critical'],
      unknown: ['plan-chip--muted', 'Too few lines']
    };
    const [cls, label] = map[status] || map.unknown;
    return `<span class="plan-chip ${cls}">${label}</span>`;
  }

  _empty(message) {
    return `<div class="plan-empty">${escapeHtml(message)}</div>`;
  }

  renderPlanningPosition() {
    const plan = this._plan();
    const head = plan.headline || {};
    const window = plan.window || {};
    const target = head.service_target_pct ?? 92;
    const onTime = head.on_time_pct;
    const meetsTarget = Number.isFinite(onTime) && onTime >= target;

    const comparison = window.comparable
      ? `Comparing <strong>${escapeHtml(window.recent_label)}</strong> against <strong>${escapeHtml(window.prior_label)}</strong>.`
      : escapeHtml(window.reason || 'The active window is too short to split into two halves for a demand trend.');

    return `
      <div class="report-card">
        <p class="plan-lede">
          Demand direction and delivery reliability, read together. ${comparison}
        </p>

        <div class="plan-kpis">
          <div class="plan-kpi ${meetsTarget ? '' : 'plan-kpi--warn'}">
            <div class="plan-kpi__label">Lines on time</div>
            <div class="plan-kpi__value">${this._pct(onTime)}</div>
            <div class="plan-kpi__note">${target}% target · ${meetsTarget ? 'meeting it' : 'below it'}</div>
          </div>
          <div class="plan-kpi ${head.at_risk_departments > 0 ? 'plan-kpi--danger' : ''}">
            <div class="plan-kpi__label">Departments at risk</div>
            <div class="plan-kpi__value">${head.at_risk_departments ?? 0}</div>
            <div class="plan-kpi__note">Growing demand into a lane already missing dates</div>
          </div>
          <div class="plan-kpi">
            <div class="plan-kpi__label">Revenue exposed</div>
            <div class="plan-kpi__value">${this._money(head.at_risk_revenue)}</div>
            <div class="plan-kpi__note">${this._pct(head.at_risk_revenue_share_pct, 0)} of the window</div>
          </div>
          <div class="plan-kpi ${head.lanes_below_target > 0 ? 'plan-kpi--warn' : ''}">
            <div class="plan-kpi__label">Lanes below target</div>
            <div class="plan-kpi__value">${head.lanes_below_target ?? 0}</div>
            <div class="plan-kpi__note">Fulfilment methods under ${target}% on time</div>
          </div>
          <div class="plan-kpi">
            <div class="plan-kpi__label">Single-sourced</div>
            <div class="plan-kpi__value">${head.single_sourced_departments ?? 0}</div>
            <div class="plan-kpi__note">Departments over ${plan.thresholds?.concentration_warn_pct ?? 45}% from one vendor</div>
          </div>
        </div>
      </div>
    `;
  }

  renderForecastAccuracy() {
    const forecast = this._plan().forecast_accuracy || {};
    const head = forecast.headline || {};
    const rows = forecast.series || [];
    if (!rows.length) {
      return this._empty('Forecast scoring needs at least five complete calendar months. The current partial month is excluded; broaden the date range to populate this section.');
    }
    const biasDirection = Number(head.bias) > 0 ? 'over-forecast' : Number(head.bias) < 0 ? 'under-forecast' : 'neutral';
    const scoreTable = (items, label) => items.length ? `
      <table class="plan-table">
        <caption class="plan-caption">${label}</caption>
        <thead><tr><th>Name</th><th class="num">WAPE</th><th class="num">SMAPE</th><th class="num">Signed bias</th><th class="num">Hit rate</th><th class="num">Periods</th></tr></thead>
        <tbody>${items.map(row => `
          <tr>
            <td><span class="plan-label">${escapeHtml(row.label || `${row.horizon_weeks}-week horizon`)}</span></td>
            <td class="num">${this._pct(row.wape_pct)}</td>
            <td class="num">${this._pct(row.smape_pct)}</td>
            <td class="num">${this._money(row.bias)}</td>
            <td class="num">${this._pct(row.hit_rate_pct)}</td>
            <td class="num">${row.scored_periods}</td>
          </tr>`).join('')}</tbody>
      </table>` : this._empty(`No ${label.toLowerCase()} scores are available for this scope.`);

    const baseline = forecast.baseline || {};
    const baselineComparison = baseline.available ? `
      <p class="plan-lede">
        <strong>${escapeHtml(baseline.label)}:</strong> ${this._pct(baseline.wape_pct)} WAPE versus
        ${this._pct(baseline.model_wape_pct)} for the model on the same ${baseline.scored_periods} periods.
        <strong>${escapeHtml(baseline.comparison)}</strong>
      </p>` : this._empty(`${baseline.label || 'Naive-seasonal baseline'} unavailable. ${baseline.reason || 'Broaden the date range to include prior-year history.'}`);

    return `
      <div class="report-card">
        <p class="plan-lede">
          Rolling-origin backtest: each point is predicted using only history available at that origin.
          WAPE is the executive headline; SMAPE is the bounded secondary diagnostic. Incomplete calendar
          months and weeks are excluded. The shaded region is a
          <strong>+/-${head.tolerance_pct}% hit-tolerance band</strong>, not a confidence interval.
        </p>
        <div class="plan-kpis">
          <div class="plan-kpi"><div class="plan-kpi__label">WAPE</div><div class="plan-kpi__value">${this._pct(head.wape_pct)}</div><div class="plan-kpi__note">Weighted absolute error · executive headline</div></div>
          <div class="plan-kpi"><div class="plan-kpi__label">SMAPE</div><div class="plan-kpi__value">${this._pct(head.smape_pct)}</div><div class="plan-kpi__note">Bounded symmetric error</div></div>
          <div class="plan-kpi"><div class="plan-kpi__label">Bias</div><div class="plan-kpi__value">${this._money(head.bias)}</div><div class="plan-kpi__note">Forecast - actual; ${biasDirection}</div></div>
          <div class="plan-kpi"><div class="plan-kpi__label">Hit rate</div><div class="plan-kpi__value">${this._pct(head.hit_rate_pct)}</div><div class="plan-kpi__note">Within +/-${head.tolerance_pct}% across ${head.scored_periods} periods</div></div>
          <div class="plan-kpi"><div class="plan-kpi__label">MAPE · diagnostic only</div><div class="plan-kpi__value">${this._pct(head.mape_pct)}</div><div class="plan-kpi__note">Unreliable on low-volume series</div></div>
        </div>
        <div id="planForecastChart" class="plan-chart" role="img" aria-label="Actual sales against rolling-origin forecast and tolerance band"></div>
        <div class="plan-spacer"></div>
        ${baselineComparison}
        <div class="plan-spacer"></div>
        ${scoreTable(forecast.by_horizon || [], 'Signed bias at 1, 4, and 8 weeks')}
        <div class="plan-spacer"></div>
        <div class="report-grid">
          <div>${scoreTable(forecast.by_sku || [], 'By SKU')}</div>
          <div>${scoreTable(forecast.by_region || [], 'By region')}</div>
        </div>
        <p class="plan-footnote">${escapeHtml(forecast.methodology?.model || '')} ${escapeHtml(forecast.methodology?.weekly_horizons || '')} ${escapeHtml(forecast.methodology?.zero_actuals || '')} ${escapeHtml(forecast.methodology?.mape || '')}</p>
      </div>
    `;
  }

  renderDemand() {
    const rows = this._plan().demand || [];
    if (!rows.length) return this._empty('No demand to report for the active filters.');

    const max = Math.max(...rows.map(r => Number(r.revenue) || 0), 1);
    return `
      <div class="report-card">
        <p class="plan-lede">
          Revenue in the recent half of the window against the prior half, by department.
          Half-and-half rather than a fitted trend, because the window is yours to change
          and a slope through four points claims more than it knows.
        </p>
        <div id="planDemandChart" class="plan-chart" role="img"
             aria-label="Department demand change against the prior window"></div>
        <table class="plan-table">
          <caption class="plan-caption">Department demand performance for the active reporting window</caption>
          <thead>
            <tr><th>Department</th><th class="num">Revenue</th><th class="num">Share</th><th class="num">Change</th><th>Direction</th></tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr>
                <td>
                  <div class="plan-bar" style="--w:${((Number(r.revenue) || 0) / max * 100).toFixed(1)}%"></div>
                  <span class="plan-label">${escapeHtml(r.label)}</span>
                </td>
                <td class="num">${this._money(r.revenue)}</td>
                <td class="num">${this._pct(r.share_pct, 1)}</td>
                <td class="num">${this._delta(r.change_pct)}</td>
                <td><span class="plan-dir plan-dir--${escapeHtml(r.direction)}">${escapeHtml(String(r.direction || '').replace('_', ' '))}</span></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  _serviceTable(rows, caption) {
    if (!rows.length) return this._empty('No delivery history for the active filters.');
    const target = this._plan().thresholds?.service_target_pct ?? 92;
    return `
      <table class="plan-table">
        <caption class="plan-caption">${escapeHtml(caption)}</caption>
        <thead>
          <tr><th>Name</th><th class="num">On time</th><th class="num">Lines</th><th class="num">Revenue</th><th>Status</th></tr>
        </thead>
        <tbody>
          ${rows.map(r => `
            <tr class="${r.status === 'critical' ? 'is-critical' : ''}">
              <td>
                <div class="plan-bar plan-bar--service" style="--w:${Math.max(0, Math.min(100, Number(r.on_time_pct) || 0))}%; --target:${target}%"></div>
                <span class="plan-label">${escapeHtml(r.label)}</span>
              </td>
              <td class="num">${this._pct(r.on_time_pct)}</td>
              <td class="num">${(Number(r.lines) || 0).toLocaleString()}</td>
              <td class="num">${this._money(r.revenue)}</td>
              <td>${this._statusChip(r.status)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  renderInventory() {
    const inv = this._plan().inventory || {};
    const head = inv.headline || {};
    const targets = inv.targets || {};
    const rows = inv.by_department || [];
    if (!rows.length) return this._empty('No availability history for the active filters.');

    const otifTarget = targets.otif_pct ?? 90;
    const fillTarget = targets.fill_pct ?? 96;

    const scorecard = (list, caption) => `
      <table class="plan-table">
        <caption class="plan-caption">${escapeHtml(caption)}</caption>
        <thead>
          <tr>
            <th>Name</th>
            <th class="num">OTIF</th><th class="num">Line fill</th><th class="num">On time</th>
            <th class="num">Stockout</th><th class="num">Cover</th><th class="num">On hand</th><th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${list.map(r => `
            <tr class="${r.status === 'critical' ? 'is-critical' : ''}">
              <td>
                <div class="plan-bar plan-bar--service" style="--w:${Math.max(0, Math.min(100, Number(r.otif_pct) || 0))}%"></div>
                <span class="plan-label">${escapeHtml(r.label)}</span>
              </td>
              <td class="num">${this._pct(r.otif_pct, 0)}</td>
              <td class="num">${this._pct(r.line_fill_pct, 0)}</td>
              <td class="num">${this._pct(r.on_time_pct, 0)}</td>
              <td class="num">${this._pct(r.stockout_pct, 1)}</td>
              <td class="num">${Number.isFinite(Number(r.cover_days)) ? Number(r.cover_days).toFixed(0) + 'd' : '—'}
                  <span class="plan-sub">/ ${escapeHtml(r.cover_target_days)}d</span></td>
              <td class="num">${this._money(r.on_hand_value)}</td>
              <td>${this._statusChip(r.status)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;

    return `
      <div class="report-card">
        <p class="plan-lede">
          Four availability numbers, and they are not interchangeable. A lane can hit 95%
          on-time and 95% in-full separately and still miss one delivery in ten, which is
          why <strong>OTIF</strong> — on time <em>and</em> in full, measured on the line rather
          than multiplied out of the other two — is the one a store actually feels.
        </p>
        <div id="planCoverChart" class="plan-chart" role="img"
             aria-label="Days of cover against the category target, by department"></div>

        <div class="plan-kpis">
          <div class="plan-kpi ${(head.otif_pct ?? 100) < otifTarget ? 'plan-kpi--danger' : ''}">
            <div class="plan-kpi__label">OTIF</div>
            <div class="plan-kpi__value">${this._pct(head.otif_pct)}</div>
            <div class="plan-kpi__note">${otifTarget}% target · on time and in full</div>
          </div>
          <div class="plan-kpi ${(head.line_fill_pct ?? 100) < fillTarget ? 'plan-kpi--warn' : ''}">
            <div class="plan-kpi__label">Line fill</div>
            <div class="plan-kpi__value">${this._pct(head.line_fill_pct)}</div>
            <div class="plan-kpi__note">Lines that shipped complete</div>
          </div>
          <div class="plan-kpi">
            <div class="plan-kpi__label">Unit fill</div>
            <div class="plan-kpi__value">${this._pct(head.unit_fill_pct)}</div>
            <div class="plan-kpi__note">Units shipped against units ordered</div>
          </div>
          <div class="plan-kpi ${(head.stockout_pct ?? 0) >= 3 ? 'plan-kpi--warn' : ''}">
            <div class="plan-kpi__label">Stockout rate</div>
            <div class="plan-kpi__value">${this._pct(head.stockout_pct)}</div>
            <div class="plan-kpi__note">Lines that filled to zero</div>
          </div>
          <div class="plan-kpi">
            <div class="plan-kpi__label">Median cover</div>
            <div class="plan-kpi__value">${Number.isFinite(Number(head.cover_days)) ? Number(head.cover_days).toFixed(0) + 'd' : '—'}</div>
            <div class="plan-kpi__note">${this._pct(head.below_reorder_pct, 0)} of lines below reorder point</div>
          </div>
          <div class="plan-kpi">
            <div class="plan-kpi__label">On hand</div>
            <div class="plan-kpi__value">${this._money(head.on_hand_value)}</div>
            <div class="plan-kpi__note">${Number.isFinite(Number(head.turns)) ? Number(head.turns).toFixed(1) + ' turns a year' : 'turns unavailable'}</div>
          </div>
        </div>

        <div class="plan-spacer"></div>
        ${scorecard(rows, 'By department — worst OTIF first')}
        <p class="plan-footnote">
          Cover targets differ by department on purpose: perishable lines are held to
          ${targets.cover_days_perishable}&nbsp;days and ambient to ${targets.cover_days_ambient}, because a single
          chain-wide target would flag all of fresh as a problem and none of general merchandise.
        </p>

        <div class="plan-spacer"></div>
        ${scorecard(inv.by_lane || [], 'By fulfilment lane')}

        ${(inv.actions || []).length ? `
          <div class="plan-spacer"></div>
          <ol class="plan-actions">
            ${inv.actions.slice(0, 5).map(a => `
              <li class="plan-action plan-action--${escapeHtml(a.severity)}">
                <div class="plan-action__head">
                  <span class="plan-action__sev">${escapeHtml(a.severity)}</span>
                  <h3 class="h5">${escapeHtml(a.title)}</h3>
                </div>
                <p>${escapeHtml(a.detail)}</p>
              </li>
            `).join('')}
          </ol>` : ''}
      </div>
    `;
  }

  renderService() {
    const plan = this._plan();
    const target = plan.thresholds?.service_target_pct ?? 92;
    const minRows = plan.thresholds?.min_rows_for_rate ?? 25;
    return `
      <div class="report-card">
        <p class="plan-lede">
          The share of order lines that arrived on or before the date they were promised.
          Anything under ${target}% is below target. Groups with fewer than ${minRows} lines
          are shown but not ranked — a lane with four deliveries that missed one is not
          &ldquo;25% late&rdquo; in any useful sense.
        </p>
        <div id="planServiceChart" class="plan-chart" role="img"
             aria-label="On-time rate by fulfilment lane against the service target"></div>
        ${this._serviceTable(plan.service_by_lane || [], 'By fulfilment lane')}
        <div class="plan-spacer"></div>
        ${this._serviceTable(plan.service_by_vendor || [], 'By vendor — worst service first')}
      </div>
    `;
  }

  renderMatrix() {
    const plan = this._plan();
    const points = plan.matrix || [];
    if (!points.length) {
      return this._empty(
        plan.window?.comparable
          ? 'Not enough delivery history in this window to place departments.'
          : 'Widen the date window to at least two weeks to compare demand against service.'
      );
    }

    const quadrantCopy = {
      at_risk: ['Growing, unreliable', 'Demand is rising into a lane that already misses its dates. Doing nothing makes this bigger, not the same size.'],
      scale: ['Growing, reliable', 'Demand is rising and the network is keeping up. Room to push.'],
      fix_service: ['Flat, unreliable', 'Service is below target but demand is not compounding the problem.'],
      steady: ['Flat, reliable', 'No action indicated.'],
      insufficient_data: ['Too few lines', 'Not enough delivery history to judge.']
    };

    const groups = {};
    points.forEach(p => { (groups[p.quadrant] = groups[p.quadrant] || []).push(p); });
    const order = ['at_risk', 'fix_service', 'scale', 'steady', 'insufficient_data'];

    return `
      <div class="report-card">
        <p class="plan-lede">
          Demand growth against service level. Only one quadrant gets a colour, because
          only one of them means the problem grows while you look at it.
        </p>
        <div id="planQuadrantChart" class="plan-chart" role="img"
             aria-label="Departments plotted by demand change against on-time delivery"></div>
        <div class="plan-quadrants">
          ${order.filter(q => groups[q]).map(q => `
            <section class="plan-quadrant plan-quadrant--${q}">
              <header>
                <h3 class="h4">${escapeHtml(quadrantCopy[q][0])}</h3>
                <p>${escapeHtml(quadrantCopy[q][1])}</p>
              </header>
              <ul>
                ${groups[q].map(p => `
                  <li>
                    <span class="plan-label">${escapeHtml(p.label)}</span>
                    <span class="plan-quadrant__metrics">
                      ${this._delta(p.change_pct)}
                      <span class="plan-sep">·</span>
                      <span>${this._pct(p.on_time_pct, 0)} on time</span>
                      <span class="plan-sep">·</span>
                      <span>${this._money(p.revenue)}</span>
                    </span>
                  </li>
                `).join('')}
              </ul>
            </section>
          `).join('')}
        </div>
      </div>
    `;
  }

  renderExposure() {
    const rows = this._plan().concentration || [];
    if (!rows.length) return this._empty('No vendor data for the active filters.');
    const warn = this._plan().thresholds?.concentration_warn_pct ?? 45;
    return `
      <div class="report-card">
        <p class="plan-lede">
          The largest vendor in each department, and how much of it they carry.
          Over ${warn}% is single-sourced in practice, whatever the contract says —
          and it only matters next to that vendor's service record.
        </p>
        <div id="planExposureChart" class="plan-chart" role="img"
             aria-label="Top-vendor share of each department against that vendor's on-time rate"></div>
        <table class="plan-table">
          <caption class="plan-caption">Largest vendor exposure and service by department</caption>
          <thead>
            <tr><th>Department</th><th>Largest vendor</th><th class="num">Share</th><th class="num">Vendors</th><th class="num">Their on-time</th></tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr class="${r.concentrated ? 'is-critical' : ''}">
                <td><span class="plan-label">${escapeHtml(r.department)}</span></td>
                <td>${escapeHtml(r.vendor)}</td>
                <td class="num">${this._pct(r.share_pct, 0)}${r.concentrated ? ' <span class="plan-chip plan-chip--watch">single-sourced</span>' : ''}</td>
                <td class="num">${r.vendor_count}</td>
                <td class="num">${this._pct(r.vendor_on_time_pct, 0)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  }

  renderPlanActions() {
    const actions = this._plan().actions || [];
    if (!actions.length) {
      return this._empty('Nothing is below target under the active filters. Widen the window or clear a filter to look further.');
    }
    return `
      <div class="report-card">
        <p class="plan-lede">
          Every line here points at a figure elsewhere on this page. Ranked by what it
          costs to ignore, and deliberately short — a list of twenty actions is a list of none.
        </p>
        <ol class="plan-actions">
          ${actions.map(a => `
            <li class="plan-action plan-action--${escapeHtml(a.severity)}">
              <div class="plan-action__head">
                <span class="plan-action__sev">${escapeHtml(a.severity)}</span>
                <h3 class="h5">${escapeHtml(a.title)}</h3>
              </div>
              <p>${escapeHtml(a.detail)}</p>
            </li>
          `).join('')}
        </ol>
      </div>
    `;
  }

  // --- Utility Methods ---

  setupScrollSpy() {
    const sections = document.querySelectorAll('.report-section');
    const navItems = document.querySelectorAll('.report-nav-item');

    window.onscroll = () => {
      let current = '';
      sections.forEach(section => {
        const sectionTop = section.offsetTop;
        if (pageYOffset >= sectionTop - 300) {
          current = section.getAttribute('id');
        }
      });

      navItems.forEach(item => {
        item.classList.remove('active');
        if (item.getAttribute('href').slice(1) === current) {
          item.classList.add('active');
        }
      });
    };
  }

  saveStateToStorage() {
    // Only the choice, not the payload. `this.state` carries the whole planner
    // bundle, and writing it here put a megabyte of numbers into localStorage
    // on every click - which is also why a stale copy could outlive a rebuild.
    localStorage.setItem('wholesale_report_prefs', JSON.stringify({
      type: this.state.type,
      sections: this.state.sections
    }));
    const btn = document.getElementById('savePresetBtn');
    btn.innerHTML = '<i class="bi bi-check-lg me-1"></i> VIEW SAVED';
    setTimeout(() => {
      btn.innerHTML = '<i class="bi bi-bookmark-plus me-1"></i> SAVE VIEW';
    }, 2000);
  }

  loadStateFromStorage() {
    const saved = localStorage.getItem('wholesale_report_prefs');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        this.state.type = parsed.type || this.state.type;
        this.state.sections = parsed.sections || this.state.sections;
        
        // Update UI
        document.getElementById('reportTypeSelect').value = this.state.type;
        this.handleTypeChange();
      } catch (e) {
        console.warn('Failed to load saved report prefs');
      }
    }
  }

  showError(msg) {
    const content = document.getElementById('reportContent');
    content.innerHTML = `
      <div class="text-center py-5">
        <i class="bi bi-exclamation-triangle text-danger" style="font-size: 3rem;"></i>
        <h3 class="mt-3 fw-bold">System Failure</h3>
        <p class="text-muted">${escapeHtml(msg)}</p>
        <button type="button" class="btn btn-dark mt-3" onclick="window.location.reload()">Retry Analysis</button>
      </div>
    `;
  }
}

// Bootstrap the workspace
document.addEventListener('DOMContentLoaded', () => {
  window.reportWorkspace = new ReportWorkspace('stakeholderReport');
});

// Re-init on filter change
window.addEventListener('filterContextChanged', () => {
  window.reportWorkspace?.refreshData();
});
