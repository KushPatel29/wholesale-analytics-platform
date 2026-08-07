import { test, expect } from '@playwright/test';

import { ensureLoggedIn } from './helpers/auth';
import {
  auditHoverSelectors,
  captureRouteArtifacts,
  discoverAttribute,
  inspectCapture,
  scanThemeIssues,
  waitForAnySelector,
  waitForRouteReady,
  type RouteAuditConfig,
} from './helpers/theme-audit';

const WIDE_WINDOW_QS = 'start=2018-01-01&end=2035-01-01';
const withWideWindow = (path: string): string => `${path}${path.includes('?') ? '&' : '?'}${WIDE_WINDOW_QS}`;
const withWindowOnDiscovery = (path: string | undefined): string =>
  path ? `${path}${path.includes('?') ? '&' : '?'}${WIDE_WINDOW_QS}` : '';
const SEEDED_FALLBACKS =
  process.env.PLAYWRIGHT_THEME_AUDIT_SEED === '1'
    ? {
        customer: withWideWindow('/customers/drilldown/C_MAIN'),
        supplier: withWideWindow('/suppliers/SUP_A'),
        salesrep: withWideWindow('/salesreps/R1'),
        product: withWideWindow('/products/SKU-001/drilldown'),
        region: withWideWindow('/regions/Region-01'),
      }
    : null;

const MAIN_ROUTES: RouteAuditConfig[] = [
  {
    name: 'overview',
    path: withWideWindow('/'),
    readySelectors: ['#overviewPage .overview-hero', '#overviewPage .hero-title', '#overviewPage .hero-side'],
    captures: [
      { name: 'hero', selector: '#overviewPage .overview-hero .hero-main', tone: 'surface', snapshot: true },
      { name: 'lead-card', selector: '#overviewPage .overview-hero .hero-side', tone: 'surface' },
    ],
    hoverSelectors: ['#overviewPage .hero-link', '#downloadSnapshotBtn'],
  },
  {
    name: 'customers-kpis',
    path: withWideWindow('/customers/kpis'),
    readySelectors: ['main .card-header.bg-white', '.customer-table', '.table-row-click'],
    captures: [
      { name: 'page-header', selector: 'main .card-header.bg-white', tone: 'surface' },
      { name: 'summary-card', selector: 'main .card', tone: 'surface' },
    ],
    hoverSelectors: ['.nav-tabs .nav-link.active', '#exportXlsxBtn', '.table-row-click'],
  },
  {
    name: 'customers-rfm',
    path: withWideWindow('/customers/rfm'),
    readySelectors: ['main h1', '.nav-tabs', 'main .card'],
    captures: [
      { name: 'tabs', selector: 'main .nav-tabs', tone: 'surface' },
      { name: 'primary-card', selector: 'main .card', tone: 'surface' },
    ],
    hoverSelectors: ['.nav-tabs .nav-link.active', '.btn-outline-secondary'],
  },
  {
    name: 'customers-clv',
    path: withWideWindow('/customers/clv'),
    readySelectors: ['main h1', '.nav-tabs', 'main .card'],
    captures: [
      { name: 'tabs', selector: 'main .nav-tabs', tone: 'surface' },
      { name: 'primary-card', selector: 'main .card', tone: 'surface' },
    ],
    hoverSelectors: ['.nav-tabs .nav-link.active', '.btn-outline-secondary'],
  },
  {
    name: 'customers-cohorts',
    path: withWideWindow('/customers/cohorts'),
    readySelectors: ['main h1', '.nav-tabs', 'main .card'],
    captures: [
      { name: 'tabs', selector: 'main .nav-tabs', tone: 'surface' },
      { name: 'primary-card', selector: 'main .card', tone: 'surface' },
    ],
    hoverSelectors: ['.nav-tabs .nav-link.active', '.btn-outline-secondary'],
  },
  {
    name: 'suppliers',
    path: withWideWindow('/suppliers/'),
    readySelectors: ['.suppliers-hero-title', '#supV2TableBody', '.suppliers-kpi-card'],
    captures: [
      { name: 'hero', selector: '.suppliers-hero', tone: 'inverse', snapshot: true },
      { name: 'command-table', selector: '#supV2TableBody', tone: 'surface' },
    ],
    hoverSelectors: ['#supV2ExportXlsx', '.suppliers-subnav-link', '[data-supplier-link]'],
  },
  {
    name: 'salesreps',
    path: withWideWindow('/salesreps/'),
    readySelectors: ['.sr-hero', '#srKpiGrid', '#salesreps-table-body'],
    captures: [
      { name: 'hero', selector: '.sr-hero', tone: 'surface', snapshot: true },
      { name: 'leadership-heading', selector: '.sr-section-heading', tone: 'surface' },
    ],
    hoverSelectors: ['#salesrepsActionsMenu', '.sr-kpi-clickable', '[data-href^="/salesreps/"]'],
  },
  {
    name: 'products',
    path: withWideWindow('/products/'),
    readySelectors: ['main h1', '#productTbody', '#productIntelOpenDrilldown'],
    captures: [
      { name: 'hero', selector: 'main :is(.products-hero, .hero-card, .page-header)', tone: 'inverse', snapshot: true },
      { name: 'table', selector: '#productTbody', tone: 'surface' },
    ],
    hoverSelectors: ['#productIntelOpenDrilldown', '#productTbody a.intel-btn'],
  },
  {
    name: 'regions',
    path: withWideWindow('/regions/'),
    readySelectors: ['.regions-v2-hero', '#regionsV2TableBody'],
    captures: [
      { name: 'hero', selector: '.regions-v2-hero', tone: 'surface', snapshot: true },
      { name: 'table', selector: '#regionsV2TableBody', tone: 'surface' },
    ],
    hoverSelectors: ['#regionsV2TableBody a[href^="/regions/"]', '.btn-primary'],
  },
  {
    name: 'labor',
    path: withWideWindow('/labor/'),
    readySelectors: ['.labor-hero', '.labor-hero-title', 'main .card'],
    captures: [
      { name: 'hero', selector: '.labor-hero', tone: 'inverse' },
      { name: 'summary-card', selector: 'main .card', tone: 'surface' },
    ],
    hoverSelectors: ['.labor-chip', '.labor-command-link'],
  },
];

function summarizeIssues(routeName: string, issues: ReturnType<typeof formatIssues>): string[] {
  return issues.map((entry) => `${routeName}: ${entry}`);
}

function formatIssues(scan: Awaited<ReturnType<typeof scanThemeIssues>>): string[] {
  const messages: string[] = [];
  for (const issue of scan.textIssues) {
    messages.push(
      `[${issue.severity}] ${issue.category} at ${issue.selector} :: "${issue.text || ''}" :: ${issue.detail || ''}`,
    );
  }
  for (const issue of scan.surfaceIssues) {
    messages.push(`[${issue.severity}] ${issue.category} at ${issue.selector} :: ${issue.detail || ''}`);
  }
  for (const issue of scan.overlayIssues) {
    messages.push(`[${issue.severity}] ${issue.category} at ${issue.selector} :: ${issue.detail || ''}`);
  }
  return messages;
}

test.describe.serial('Whole-app theme audit', () => {
  test('audits route coverage, drilldowns, and header readability', async ({ page }, testInfo) => {
    test.slow();
    test.setTimeout(480_000);

    const failures: string[] = [];
    const discovered: Record<string, string> = {};
    const routeIssueCounts: Record<string, number> = {};

    await ensureLoggedIn(page);

    for (const route of MAIN_ROUTES) {
      try {
        await waitForRouteReady(page, route);
        await captureRouteArtifacts(page, route, testInfo);

        const captureFailures: string[] = [];
        for (const capture of route.captures) {
          const result = await inspectCapture(page, capture);
          if (result.issues.length) {
            captureFailures.push(
              `${capture.name} (${capture.selector}) -> ${result.issues.join(', ')} :: "${result.text}" :: contrast=${String(result.contrast)}`,
            );
          }
        }

        const hoverFindings = await auditHoverSelectors(page, route.hoverSelectors || []);
        const hoverFailures = hoverFindings
          .filter((finding) => finding.issues.length)
          .map(
            (finding) =>
              `hover ${finding.selector} -> ${finding.issues.join(', ')} :: contrast=${String(finding.contrast)}`,
          );

        const scan = await scanThemeIssues(page);
        const routeIssues = [
          ...captureFailures.map((entry) => `[component] ${entry}`),
          ...hoverFailures.map((entry) => `[hover] ${entry}`),
          ...formatIssues(scan),
        ];
        routeIssueCounts[route.name] = routeIssues.length;
        await testInfo.attach(`${route.name}-theme-scan`, {
          body: JSON.stringify(
            {
              route: route.name,
              scan,
              componentIssues: captureFailures,
              hoverIssues: hoverFailures,
            },
            null,
            2,
          ),
          contentType: 'application/json',
        });

        failures.push(...summarizeIssues(route.name, routeIssues));

        if (route.name === 'customers-kpis' || route.name === 'customers-rfm' || route.name === 'customers-clv') {
          const customerPath =
            (await discoverAttribute(page, ['[data-href*="/customers/drilldown/"]'], 'data-href')) ||
            (await discoverAttribute(page, ['a[href*="/customers/drilldown/"]'], 'href'));
          if (customerPath) discovered.customer = withWindowOnDiscovery(customerPath);
        }
        if (route.name === 'suppliers') {
          const supplierId = await discoverAttribute(page, ['[data-supplier-link]'], 'data-supplier-link');
          if (supplierId) discovered.supplier = withWideWindow(`/suppliers/${encodeURIComponent(supplierId)}`);
        }
        if (route.name === 'salesreps') {
          const salesrepPath = await discoverAttribute(page, ['[data-href^="/salesreps/"]'], 'data-href');
          if (salesrepPath) discovered.salesrep = withWindowOnDiscovery(salesrepPath);
          const actionsMenu = page.locator('#salesrepsActionsMenu').first();
          if (await actionsMenu.count()) {
            await actionsMenu.click();
            await waitForAnySelector(page, ['.dropdown-menu.show']);
            const dropdownScan = await scanThemeIssues(page);
            const dropdownIssues = formatIssues(dropdownScan).slice(0, 8);
            failures.push(
              ...dropdownIssues.map((entry) => `salesreps: [dropdown] ${entry}`),
            );
            await page.keyboard.press('Escape');
          }
        }
        if (route.name === 'products') {
          const productPath = await discoverAttribute(page, ['#productTbody a.intel-btn'], 'href');
          if (productPath) discovered.product = withWindowOnDiscovery(productPath);
        }
        if (route.name === 'regions') {
          const regionPath = await discoverAttribute(page, ['#regionsV2TableBody a[href^="/regions/"]'], 'href');
          if (regionPath) discovered.region = withWindowOnDiscovery(regionPath);
        }
      } catch (err) {
        failures.push(
          `${route.name}: failed to load or audit route :: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }

    if (SEEDED_FALLBACKS) {
      discovered.customer ||= SEEDED_FALLBACKS.customer;
      discovered.supplier ||= SEEDED_FALLBACKS.supplier;
      discovered.salesrep ||= SEEDED_FALLBACKS.salesrep;
      discovered.product ||= SEEDED_FALLBACKS.product;
      discovered.region ||= SEEDED_FALLBACKS.region;
    }

    const dynamicRoutes: RouteAuditConfig[] = [
      {
        name: 'customer-drilldown',
        path: discovered.customer || '',
        readySelectors: ['.ciw-hero', '.ciw-title'],
        captures: [
          { name: 'hero', selector: '.ciw-hero', tone: 'surface', minContrast: 3.6, snapshot: true },
          { name: 'summary-strip', selector: '.ciw-stat-strip', tone: 'surface' },
        ],
        hoverSelectors: ['.ciw-export-card', '.ciw-pill'],
      },
      {
        name: 'supplier-drilldown',
        path: discovered.supplier || '',
        readySelectors: ['.supplier-v2-hero', '#v2Title'],
        captures: [
          { name: 'hero', selector: '.supplier-v2-hero', tone: 'surface', minContrast: 3.6, snapshot: true },
          { name: 'kpi-card', selector: '.supplier-drilldown-v2 .supplier-v2-kpi-card', tone: 'surface' },
        ],
        hoverSelectors: ['.supplier-v2-hero-actions .btn', '.supplier-v2-status-pill'],
      },
      {
        name: 'salesrep-drilldown',
        path: discovered.salesrep || '',
        readySelectors: ['.srpd-hero', '#salesrepName'],
        captures: [
          { name: 'hero', selector: '.srpd-hero', tone: 'surface', minContrast: 3.6, snapshot: true },
          { name: 'kpi-panel', selector: '.srpd-kpi-panel', tone: 'surface' },
        ],
        hoverSelectors: ['.srpd-nav-link', '#salesrepExportXlsx'],
      },
      {
        name: 'product-drilldown',
        path: discovered.product || '',
        readySelectors: ['.product-drilldown-v2', 'main h1', '.product-v2-hero'],
        captures: [
          { name: 'hero', selector: 'main :is(.product-v2-hero, .page-header, .hero-card)', tone: 'surface', snapshot: true },
          { name: 'summary-card', selector: '.product-drilldown-v2 .card', tone: 'surface' },
        ],
        hoverSelectors: ['.product-v2-subnav-link', '.product-v2-chip'],
      },
      {
        name: 'region-drilldown',
        path: discovered.region || '',
        readySelectors: ['.region-v2-hero', 'main h1', '#kpiRevenue'],
        captures: [
          { name: 'hero', selector: '.region-v2-hero', tone: 'surface' },
          { name: 'kpi-card', selector: '.region-kpi-card', tone: 'surface' },
        ],
        hoverSelectors: ['.btn-primary', '.btn-outline-secondary'],
      },
    ].filter((route) => Boolean(route.path));

    for (const route of dynamicRoutes) {
      try {
        await waitForRouteReady(page, route);
        await captureRouteArtifacts(page, route, testInfo);
        const scan = await scanThemeIssues(page);
        const componentFailures: string[] = [];
        for (const capture of route.captures) {
          const result = await inspectCapture(page, capture);
          if (result.issues.length) {
            componentFailures.push(
              `${capture.name} (${capture.selector}) -> ${result.issues.join(', ')} :: "${result.text}" :: contrast=${String(result.contrast)}`,
            );
          }
        }
        const hoverFindings = await auditHoverSelectors(page, route.hoverSelectors || []);
        const hoverFailures = hoverFindings
          .filter((finding) => finding.issues.length)
          .map(
            (finding) =>
              `hover ${finding.selector} -> ${finding.issues.join(', ')} :: contrast=${String(finding.contrast)}`,
          );
        const routeIssues = [
          ...componentFailures.map((entry) => `[component] ${entry}`),
          ...hoverFailures.map((entry) => `[hover] ${entry}`),
          ...formatIssues(scan),
        ];
        routeIssueCounts[route.name] = routeIssues.length;
        await testInfo.attach(`${route.name}-theme-scan`, {
          body: JSON.stringify(
            {
              route: route.name,
              scan,
              componentIssues: componentFailures,
              hoverIssues: hoverFailures,
            },
            null,
            2,
          ),
          contentType: 'application/json',
        });
        failures.push(...summarizeIssues(route.name, routeIssues));
      } catch (err) {
        failures.push(
          `${route.name}: failed to load or audit route :: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }

    await testInfo.attach('theme-audit-discovery', {
      body: JSON.stringify({ discovered, routeIssueCounts }, null, 2),
      contentType: 'application/json',
    });

    expect(discovered.customer, 'customer drilldown route should be discoverable').toBeTruthy();
    expect(discovered.supplier, 'supplier drilldown route should be discoverable').toBeTruthy();
    expect(discovered.salesrep, 'salesrep drilldown route should be discoverable').toBeTruthy();

    expect(
      failures,
      `Theme audit found ${failures.length} issue(s):\n${failures.join('\n')}`,
    ).toEqual([]);
  });
});
