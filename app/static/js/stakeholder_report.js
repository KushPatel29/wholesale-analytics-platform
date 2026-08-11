/**
 * Northgate Retail Analytics Executive Reporting Workspace
 * Enterprise-grade, live, interactive reporting layer.
 */
class ReportWorkspace {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    if (!containerId) return;

    this.bundleUrl = this.container.getAttribute('data-bundle-url');
    this.state = {
      type: 'full',
      sections: ['plan', 'demand', 'inventory', 'service', 'matrix', 'exposure', 'actions'],
      data: null
    };

    // The planner owns five sections and no more. Sales Performance, Customer
    // Portfolio, Product Intelligence and Regional Momentum each have their own
    // page and were duplicated here; "AI Insights" and "Strategic Scenarios"
    // were template prose that asserted rather than measured. All removed.
    this.sectionRegistry = {
      plan: { label: 'Planning Position', icon: 'bi-speedometer2', render: this.renderPlanningPosition.bind(this) },
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

    typeSelect?.addEventListener('change', (e) => {
      this.state.type = e.target.value;
      this.handleTypeChange();
      this.render();
    });

    document.getElementById('savePresetBtn')?.addEventListener('click', () => this.saveStateToStorage());
  }

  handleTypeChange() {
    const toggles = document.getElementById('sectionToggles');
    if (this.state.type === 'custom') {
      toggles.style.display = 'block';
      this.renderVisibilityToggles();
    } else {
      toggles.style.display = 'none';
      // Preset sections based on type
      // Views, not "audiences". Each one is a real subset of the planner:
      // what you look at depends on whether you are deciding what to buy,
      // where the network is failing, or just what to do this week.
      const presets = {
        full: ['plan', 'demand', 'inventory', 'service', 'matrix', 'exposure', 'actions'],
        demand: ['plan', 'demand', 'matrix', 'actions'],
        supply: ['plan', 'inventory', 'service', 'exposure', 'actions'],
        actions: ['plan', 'actions']
      };
      this.state.sections = presets[this.state.type] || Object.keys(this.sectionRegistry);
    }
  }

  renderVisibilityToggles() {
    const container = document.getElementById('visibilityList');
    container.innerHTML = Object.entries(this.sectionRegistry).map(([id, cfg]) => `
      <button class="section-toggle ${this.state.sections.includes(id) ? 'active' : ''}" data-id="${id}">
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
    this.setupScrollSpy();
  }

  renderNavigation() {
    const navContainer = document.getElementById('dynamicNavItems');
    navContainer.innerHTML = this.state.sections.map(id => {
      const cfg = this.sectionRegistry[id];
      if (!cfg) return '';
      return `
        <a href="#section-${id}" class="report-nav-item">
          <i class="bi ${cfg.icon}"></i>
          ${cfg.label}
        </a>
      `;
    }).join('');
  }

  renderContent() {
    const content = document.getElementById('reportContent');
    content.innerHTML = this.state.sections.map(id => {
      const cfg = this.sectionRegistry[id];
      if (!cfg) return '';
      return `
        <section id="section-${id}" class="report-section">
          ${this.createSectionHeader(id, cfg.label)}
          <div id="content-${id}" class="section-body">
            ${cfg.render(this.state.data)}
          </div>
        </section>
      `;
    }).join('');
    // Sections are built as HTML strings, so anything that needs a live element
    // has to be drawn after they are in the document.
    this.drawCharts();
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

  createSectionHeader(id, title) {
    const eyebrows = {
      plan: 'Where the plan stands',
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
          <span class="section-eyebrow">${eyebrows[id] || 'Reporting Layer'}</span>
          <h2 class="section-title">${title}</h2>
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

  _money(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
    if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(0)}k`;
    return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }

  _pct(value, digits = 1) {
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
    return `<div class="plan-empty">${message}</div>`;
  }

  renderPlanningPosition() {
    const plan = this._plan();
    const head = plan.headline || {};
    const window = plan.window || {};
    const target = head.service_target_pct ?? 92;
    const onTime = head.on_time_pct;
    const meetsTarget = Number.isFinite(onTime) && onTime >= target;

    const comparison = window.comparable
      ? `Comparing <strong>${window.recent_label}</strong> against <strong>${window.prior_label}</strong>.`
      : (window.reason || 'The active window is too short to split into two halves for a demand trend.');

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
          <thead>
            <tr><th>Department</th><th class="num">Revenue</th><th class="num">Share</th><th class="num">Change</th><th>Direction</th></tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr>
                <td>
                  <div class="plan-bar" style="--w:${((Number(r.revenue) || 0) / max * 100).toFixed(1)}%"></div>
                  <span class="plan-label">${r.label}</span>
                </td>
                <td class="num">${this._money(r.revenue)}</td>
                <td class="num">${this._pct(r.share_pct, 1)}</td>
                <td class="num">${this._delta(r.change_pct)}</td>
                <td><span class="plan-dir plan-dir--${r.direction}">${r.direction.replace('_', ' ')}</span></td>
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
        <caption class="plan-caption">${caption}</caption>
        <thead>
          <tr><th>Name</th><th class="num">On time</th><th class="num">Lines</th><th class="num">Revenue</th><th>Status</th></tr>
        </thead>
        <tbody>
          ${rows.map(r => `
            <tr class="${r.status === 'critical' ? 'is-critical' : ''}">
              <td>
                <div class="plan-bar plan-bar--service" style="--w:${Math.max(0, Math.min(100, Number(r.on_time_pct) || 0))}%; --target:${target}%"></div>
                <span class="plan-label">${r.label}</span>
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
        <caption class="plan-caption">${caption}</caption>
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
                <span class="plan-label">${r.label}</span>
              </td>
              <td class="num">${this._pct(r.otif_pct, 0)}</td>
              <td class="num">${this._pct(r.line_fill_pct, 0)}</td>
              <td class="num">${this._pct(r.on_time_pct, 0)}</td>
              <td class="num">${this._pct(r.stockout_pct, 1)}</td>
              <td class="num">${Number.isFinite(Number(r.cover_days)) ? Number(r.cover_days).toFixed(0) + 'd' : '—'}
                  <span class="plan-sub">/ ${r.cover_target_days}d</span></td>
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
              <li class="plan-action plan-action--${a.severity}">
                <div class="plan-action__head">
                  <span class="plan-action__sev">${a.severity}</span>
                  <h5>${a.title}</h5>
                </div>
                <p>${a.detail}</p>
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
                <h4>${quadrantCopy[q][0]}</h4>
                <p>${quadrantCopy[q][1]}</p>
              </header>
              <ul>
                ${groups[q].map(p => `
                  <li>
                    <span class="plan-label">${p.label}</span>
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
        <table class="plan-table">
          <thead>
            <tr><th>Department</th><th>Largest vendor</th><th class="num">Share</th><th class="num">Vendors</th><th class="num">Their on-time</th></tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr class="${r.concentrated ? 'is-critical' : ''}">
                <td><span class="plan-label">${r.department}</span></td>
                <td>${r.vendor}</td>
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
            <li class="plan-action plan-action--${a.severity}">
              <div class="plan-action__head">
                <span class="plan-action__sev">${a.severity}</span>
                <h5>${a.title}</h5>
              </div>
              <p>${a.detail}</p>
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
    localStorage.setItem('wholesale_report_prefs', JSON.stringify(this.state));
    const btn = document.getElementById('savePresetBtn');
    btn.innerHTML = '<i class="bi bi-check-lg me-1"></i> PRESET SAVED';
    setTimeout(() => {
      btn.innerHTML = '<i class="bi bi-bookmark-plus me-1"></i> SAVE PRESET';
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
        <p class="text-muted">${msg}</p>
        <button class="btn btn-dark mt-3" onclick="window.location.reload()">Retry Analysis</button>
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
