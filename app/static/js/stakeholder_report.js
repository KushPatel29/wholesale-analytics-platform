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
      type: 'executive',
      audience: 'executive',
      detail: 'standard',
      sections: [
        'overview', 'signals', 'sales', 'customers', 'suppliers', 
        'products', 'regions', 'portfolio', 'ai', 'scenarios', 'actions'
      ],
      data: null
    };

    this.sectionRegistry = {
      overview: { label: 'Executive Summary', icon: 'bi-lightning-charge', render: this.renderOverview.bind(this) },
      signals: { label: 'Core Signals', icon: 'bi-activity', render: this.renderSignals.bind(this) },
      sales: { label: 'Sales Performance', icon: 'bi-person-badge', render: this.renderSales.bind(this) },
      customers: { label: 'Customer Portfolio', icon: 'bi-people', render: this.renderCustomers.bind(this) },
      suppliers: { label: 'Supplier Exposure', icon: 'bi-truck', render: this.renderSuppliers.bind(this) },
      products: { label: 'Product Intelligence', icon: 'bi-box-seam', render: this.renderProducts.bind(this) },
      regions: { label: 'Regional Momentum', icon: 'bi-geo-alt', render: this.renderRegions.bind(this) },
      portfolio: { label: 'Retention & Risk', icon: 'bi-shield-check', render: this.renderPortfolio.bind(this) },
      ai: { label: 'AI Insights', icon: 'bi-cpu', render: this.renderAI.bind(this) },
      scenarios: { label: 'Strategic Scenarios', icon: 'bi-diagram-3', render: this.renderScenarios.bind(this) },
      actions: { label: 'Action Priority', icon: 'bi-check2-circle', render: this.renderActions.bind(this) }
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
    const audienceSelect = document.getElementById('audienceSelect');
    const detailSelect = document.getElementById('detailSelect');

    typeSelect?.addEventListener('change', (e) => {
      this.state.type = e.target.value;
      this.handleTypeChange();
      this.render();
    });

    audienceSelect?.addEventListener('change', (e) => {
      this.state.audience = e.target.value;
      this.render();
    });

    detailSelect?.addEventListener('change', (e) => {
      this.state.detail = e.target.value;
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
      const presets = {
        executive: ['overview', 'signals', 'ai', 'scenarios', 'actions'],
        sales: ['overview', 'sales', 'customers', 'regions', 'actions'],
        commercial: ['overview', 'sales', 'customers', 'suppliers', 'products'],
        operational: ['overview', 'signals', 'products', 'suppliers', 'actions']
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
      this.container.classList.remove('is-loading');
    } catch (err) {
      console.error(err);
      this.showError(err.message);
    }
  }

  render() {
    this.renderNavigation();
    this.renderContent();
    this.setupScrollSpy();
    if (this.state.sections.includes('overview')) {
      setTimeout(() => this.renderCharts(), 50);
    }
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
  }

  createSectionHeader(id, title) {
    const eyebrows = {
      overview: 'Strategic Intelligence',
      signals: 'Operational Pulse',
      sales: 'Commercial Audit',
      customers: 'Portfolio Health',
      suppliers: 'Supply Chain Sensitivity',
      products: 'Yield & Mix Management',
      regions: 'Geographic Velocity',
      portfolio: 'Retention Diagnostic',
      ai: 'Intelligent Optimization',
      scenarios: 'Strategic Scenarios',
      actions: 'Tactical Directives'
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

  renderOverview(data) {
    const overview = data.overview || {};
    return `
      <div class="report-hero">
        <h1 class="hero-headline">${overview.headline || 'Market Pulse'}</h1>
        <p class="lead text-muted mb-4" style="font-size: 1.25rem;">${overview.summary || 'Synthesizing market signals...'}</p>
        
        <div class="row g-4 mb-5">
          ${(overview.kpis || []).map(kpi => `
            <div class="col-md-4">
              <div class="p-3 bg-white rounded-3 shadow-sm border">
                <div class="card-label">${kpi.label}</div>
                <div class="card-value">${kpi.value}</div>
                <div class="card-trend ${kpi.trend >= 0 ? 'trend-up' : 'trend-down'}">
                  ${kpi.trend >= 0 ? '↑' : '↓'} ${Math.abs(kpi.trend).toFixed(1)}% vs Target
                </div>
              </div>
            </div>
          `).join('')}
        </div>

        <div class="bg-white rounded-3 shadow-sm border p-4">
          <h6 class="fw-black text-uppercase small text-muted mb-3">6-Month Trailing Performance</h6>
          <div style="height: 250px;">
            <canvas id="overviewTrendChart"></canvas>
          </div>
        </div>
      </div>
    `;
  }

  renderCharts() {
    const trendCanvas = document.getElementById('overviewTrendChart');
    if (trendCanvas) {
      const chartData = this.state.data?.charts?.trend || { labels: [], revenue: [], margin: [] };
      if (this._overviewChart) this._overviewChart.destroy();
      this._overviewChart = new Chart(trendCanvas, {
        type: 'line',
        data: {
          labels: chartData.labels,
          datasets: [
            {
              label: 'Revenue ($)',
              data: chartData.revenue,
              borderColor: '#0f172a',
              backgroundColor: 'rgba(15, 23, 42, 0.05)',
              borderWidth: 3,
              tension: 0.4,
              fill: true,
              yAxisID: 'y'
            },
            {
              label: 'Margin (%)',
              data: chartData.margin,
              borderColor: '#3b82f6',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [5, 5],
              tension: 0.4,
              yAxisID: 'y1'
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'top', align: 'end', labels: { usePointStyle: true, boxWidth: 6 } } },
          scales: {
            x: { grid: { display: false } },
            y: { type: 'linear', display: true, position: 'left', ticks: { callback: function(value) { return '$' + (value/1000).toFixed(0) + 'k'; } } },
            y1: { type: 'linear', display: true, position: 'right', grid: { display: false }, ticks: { callback: function(value) { return value + '%'; } } }
          }
        }
      });
    }

    const mixCanvas = document.getElementById('productMixChart');
    if (mixCanvas) {
      const mixData = this.state.data?.charts?.product_mix || { labels: [], values: [] };
      if (this._mixChart) this._mixChart.destroy();
      this._mixChart = new Chart(mixCanvas, {
        type: 'doughnut',
        data: {
          labels: mixData.labels,
          datasets: [{
            data: mixData.values,
            backgroundColor: ['#0f172a', '#3b82f6', '#10b981', '#f59e0b', '#64748b', '#e2e8f0'],
            borderWidth: 0
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: '75%',
          plugins: {
            legend: { position: 'right', labels: { usePointStyle: true, padding: 20 } },
            tooltip: { callbacks: { label: function(context) { return ' $' + context.raw.toLocaleString(); } } }
          }
        }
      });
    }

    const riskCanvas = document.getElementById('customerRiskChart');
    if (riskCanvas) {
      const riskData = this.state.data?.charts?.customer_risk || [];
      if (this._riskChart) this._riskChart.destroy();
      this._riskChart = new Chart(riskCanvas, {
        type: 'bubble',
        data: {
          datasets: [{
            label: 'Accounts',
            data: riskData,
            backgroundColor: 'rgba(59, 130, 246, 0.6)',
            borderColor: '#3b82f6',
            borderWidth: 1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(context) {
                  const pt = context.raw;
                  return pt.label + ': Margin ' + pt.x.toFixed(1) + '%, Rev $' + pt.y.toLocaleString();
                }
              }
            }
          },
          scales: {
            x: { title: { display: true, text: 'Contribution Margin (%)' } },
            y: { title: { display: true, text: 'MTD Revenue ($)' }, ticks: { callback: function(value) { return '$' + (value/1000).toFixed(0) + 'k'; } } }
          }
        }
      });
    }
  }

  renderSignals(data) {
    const signals = data.signals || [];
    return `
      <div class="report-grid">
        ${signals.map(s => `
          <div class="report-card">
            <div class="d-flex justify-content-between align-items-start mb-3">
              <span class="card-label">${s.label}</span>
              <span class="badge ${s.status === 'success' ? 'bg-success-subtle text-success' : 'bg-warning-subtle text-warning'} border-0">${s.status.toUpperCase()}</span>
            </div>
            <div class="card-value" style="font-size: 1.5rem;">${s.value}</div>
            <p class="small text-muted mt-3 mb-0">${s.explanation}</p>
          </div>
        `).join('')}
      </div>
    `;
  }

  renderSales(data) {
    const sales = data.sales || {};
    return `
      <div class="row g-4">
        <div class="col-md-7">
          <div class="report-card h-100">
            <span class="card-label">Commercial Readiness Index</span>
            <div class="table-responsive mt-3">
              <table class="exec-table">
                <thead>
                  <tr>
                    <th>SalesRep (BC)</th>
                    <th>MTD Rev</th>
                    <th>Margin</th>
                    <th>Health</th>
                  </tr>
                </thead>
                <tbody>
                  ${(sales.watchlist || []).map(r => `
                    <tr>
                      <td class="fw-bold">${r.name}</td>
                      <td>${r.revenue}</td>
                      <td>${r.margin}</td>
                      <td>
                        <div class="d-flex align-items-center gap-2">
                          <div class="progress flex-grow-1" style="height: 6px;">
                            <div class="progress-bar ${r.health > 80 ? 'bg-success' : 'bg-primary'}" style="width: ${r.health}%"></div>
                          </div>
                          <span class="small">${r.health}%</span>
                        </div>
                      </td>
                      <td>
                        <a href="/salesreps/rep/${r.id}" class="text-primary text-decoration-none small fw-bold">
                          AUDIT <i class="bi bi-chevron-right"></i>
                        </a>
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="col-md-5">
          <div class="report-card h-100" style="background: #f8fafc;">
            <span class="card-label">Strategic Narrative</span>
            <p class="mt-3 fw-bold" style="line-height: 1.6;">${sales.risk_summary || 'Territory velocity remains consistent with monthly projections.'}</p>
            <hr>
            <h6 class="fw-black text-uppercase small mb-3">Top Momentum Drivers</h6>
            ${(sales.top_reps || []).map(r => `
              <div class="d-flex justify-content-between py-1 small fw-bold">
                <span>${r.name}</span>
                <span class="text-primary">${r.revenue}</span>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  }

  renderCustomers(data) {
    const customers = data.customers || [];
    return `
      <div class="report-grid">
        ${customers.map(c => `
          <div class="report-card">
            <span class="card-label">${c.segment}</span>
            <div class="card-value">${c.count}</div>
            <p class="small text-muted mt-3 mb-0" style="line-height: 1.4;">
              <i class="bi bi-info-circle me-1"></i> ${c.insight}
            </p>
          </div>
        `).join('')}
      </div>
    `;
  }

  renderSuppliers(data) {
    const suppliers = data.suppliers || {};
    return `
      <div class="report-card">
        <span class="card-label">Supply Chain Exposure (Top 5)</span>
        <div class="row g-5 mt-2">
          <div class="col-md-7">
            <div class="table-responsive">
              <table class="exec-table">
                <thead>
                  <tr>
                    <th>Vendor</th>
                    <th>Wallet Share</th>
                    <th>Risk Factor</th>
                  </tr>
                </thead>
                <tbody>
                  ${(suppliers.top_exposure || []).map(s => `
                    <tr>
                      <td class="fw-bold">${s.name}</td>
                      <td>${s.share}%</td>
                      <td><span class="badge bg-light text-dark">Stable</span></td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>
          <div class="col-md-5">
            <div class="p-4 rounded" style="background: #f1f5f9; border-left: 4px solid var(--report-accent);">
              <h6 class="fw-black text-uppercase small mb-2">Concentration Note</h6>
              <p class="small mb-0">${suppliers.summary || 'Supplier concentration is within acceptable risk corridors for the Vancouver operations.'}</p>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  renderProducts(data) {
    const products = data.products || [];
    return `
      <div class="row g-4">
        ${products.map(p => `
          <div class="col-md-6">
            <div class="report-card h-100">
              <div class="d-flex justify-content-between align-items-center mb-3">
                <span class="fw-black text-uppercase small text-muted">${p.category}</span>
                <span class="badge bg-primary px-2">${p.momentum}</span>
              </div>
              <p class="fw-bold mb-0" style="font-size: 1.1rem; line-height: 1.4;">${p.summary}</p>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  }

  renderRegions(data) {
    const regions = (data.regions || {}).performance || [];
    if (regions.length === 0) {
      return `
        <div class="report-card">
          <div class="text-center py-4">
            <p class="text-muted mb-0">No regional variance detected in the current filter context.</p>
          </div>
        </div>
      `;
    }
    return `
      <div class="report-card">
        <span class="card-label">Top Performing Territories (BC)</span>
        <div class="table-responsive mt-3">
          <table class="exec-table">
            <thead>
              <tr>
                <th>Region</th>
                <th>MTD Revenue</th>
                <th>Contribution Margin</th>
              </tr>
            </thead>
            <tbody>
              ${regions.map(r => `
                <tr>
                  <td class="fw-bold">${r.name}</td>
                  <td>${r.revenue}</td>
                  <td class="text-primary">${r.margin}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  renderPortfolio(data) {
    const portfolio = data.portfolio || {};
    return `
      <div class="report-grid">
        <div class="report-card">
          <span class="card-label">Churn Risk Probability</span>
          <div class="card-value">${portfolio.churn_risk || 0}%</div>
          <div class="progress mt-3" style="height: 4px;">
            <div class="progress-bar ${portfolio.churn_risk > 15 ? 'bg-danger' : 'bg-success'}" style="width: ${portfolio.churn_risk || 0}%"></div>
          </div>
          <p class="small text-muted mt-2 mb-0">Calculated based on 30-day silent accounts.</p>
        </div>
        <div class="report-card">
          <span class="card-label">Portfolio Stability Index</span>
          <div class="card-value">${portfolio.retention_pct || 100}%</div>
          <div class="progress mt-3" style="height: 4px;">
            <div class="progress-bar bg-primary" style="width: ${Math.min(100, portfolio.retention_pct || 100)}%"></div>
          </div>
          <p class="small text-muted mt-2 mb-0">Net account movement for the current period.</p>
        </div>
        <div class="report-card">
          <span class="card-label">High-Risk Recovery Rate</span>
          <div class="card-value">${portfolio.recovery_stat || '0 / 0'}</div>
          <div class="progress mt-3" style="height: 4px;">
            <div class="progress-bar bg-warning" style="width: 50%"></div>
          </div>
          <p class="small text-muted mt-2 mb-0">Accounts reactivated vs total silent cohort.</p>
        </div>
      </div>
    `;
  }

  renderAI(data) {
    const solutions = data.ai_solutions || [];
    return `
      <div class="ai-insight-block">
        <div class="d-flex align-items-center gap-2 mb-4">
          <i class="bi bi-stars text-primary" style="font-size: 1.5rem;"></i>
          <span class="fw-black text-uppercase small" style="letter-spacing: 0.1em;">Intelligent Optimization Engine</span>
        </div>
        <div class="row g-4">
          ${solutions.map(s => `
            <div class="col-md-4">
              <div class="p-4 h-100 rounded" style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); transition: transform 0.2s;">
                <h6 class="fw-bold text-white mb-3" style="font-size: 1.1rem;">${s.title}</h6>
                <p class="small mb-0" style="color: #cbd5e1; line-height: 1.5;">${s.description}</p>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  renderScenarios(data) {
    const scenarios = data.scenarios || [];
    return `
      <div class="report-grid">
        ${scenarios.map(s => `
          <div class="report-card" style="border-left: 4px solid #334155;">
            <div class="d-flex justify-content-between mb-2">
              <span class="badge bg-light text-dark">${s.type}</span>
              <span class="small fw-bold text-primary">${s.signal}</span>
            </div>
            <h5 class="fw-bold">${s.title}</h5>
            <p class="small text-muted mb-3">${s.description}</p>
            <div class="p-3 rounded small" style="background: #f8fafc; font-weight: 500;">
              <span class="d-block fw-black text-uppercase mb-1" style="font-size: 0.65rem;">Analyst Insight</span>
              ${s.explanation}
            </div>
          </div>
        `).join('')}
      </div>
    `;
  }

  renderActions(data) {
    const actions = data.actions || [];
    return `
      <div class="report-card" style="background: var(--report-accent); color: white; border: none;">
        <div class="d-flex flex-column gap-4">
          ${actions.map((a, idx) => `
            <div class="d-flex gap-4 align-items-start">
              <div class="bg-white text-dark rounded-circle d-flex align-items-center justify-content-center fw-black" style="width: 28px; height: 28px; flex-shrink: 0; font-size: 0.85rem;">
                ${idx + 1}
              </div>
              <div>
                <h5 class="text-white fw-bold mb-1" style="font-size: 1.15rem;">${a.title}</h5>
                <p class="mb-0 small" style="color: #94a3b8; font-weight: 500;">${a.description}</p>
              </div>
            </div>
          `).join('')}
        </div>
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
        this.state.audience = parsed.audience || this.state.audience;
        this.state.detail = parsed.detail || this.state.detail;
        this.state.sections = parsed.sections || this.state.sections;
        
        // Update UI
        document.getElementById('reportTypeSelect').value = this.state.type;
        document.getElementById('audienceSelect').value = this.state.audience;
        document.getElementById('detailSelect').value = this.state.detail;
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
