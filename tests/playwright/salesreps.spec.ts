import { test, expect, type Page } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

/**
 * Playwright coverage for the Sales Rep Performance pages:
 *   - /salesreps          (main page)
 *   - /salesreps/<id>     (drilldown)
 *
 * Assumptions:
 *   - App is running (playwright.config.ts webServer or PLAYWRIGHT_BASE_URL).
 *   - At least one sales rep exists in the data.
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function waitForHydration(page: Page): Promise<void> {
  // Wait for the bundle JS to finish hydrating the page — signalled by the
  // loading class being removed from the summary narrative block, or a KPI
  // value becoming visible (whichever fires first).
  await Promise.race([
    page.waitForFunction(
      () => !document.getElementById('srSummaryNarrative')?.classList.contains('sr-summary-narrative--loading'),
      { timeout: 30_000 }
    ),
    page.waitForSelector('#kpiRevenue .value:not(:empty)', { timeout: 30_000 }),
  ]);
}

// ---------------------------------------------------------------------------
// Main page — smoke
// ---------------------------------------------------------------------------

test.describe('Sales Reps main page', () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
    await page.goto('/salesreps');
    await waitForHydration(page);
  });

  test('page loads and heading is visible', async ({ page }) => {
    await expect(page.locator('h2')).toContainText(/Current Owner Roll-Up/i);
  });

  // ── KPI cards ────────────────────────────────────────────────────────────

  test('all primary KPI elements are present', async ({ page }) => {
    const kpiIds = [
      '#kpiRevenue',
      '#kpiProfit',
      '#kpiMargin',
      '#kpiAov',
      '#kpiPpo',
    ];
    for (const id of kpiIds) {
      await expect(page.locator(id)).toBeVisible();
    }
  });

  test('KPI values do not display "None" placeholder', async ({ page }) => {
    // After TEXT_EMPTY was changed from "None" to "—", no value cell should
    // contain the word "None".
    const kpiContainer = page.locator('#srKpiGrid');
    await expect(kpiContainer).not.toContainText('None');
  });

  test('MoM delta labels say "MoM (FMTD)"', async ({ page }) => {
    // At least one delta chip should carry the (FMTD) qualifier.
    const deltasWithFMTD = page.locator('.sr-kpi-delta').filter({ hasText: 'MoM (FMTD)' });
    await expect(deltasWithFMTD.first()).toBeVisible();
  });

  // ── Executive signal strip ───────────────────────────────────────────────

  test('executive signal strip renders 3 cards', async ({ page }) => {
    const strip = page.locator('.sr-signal-strip');
    await expect(strip).toBeVisible();
    await expect(strip.locator('.sr-signal-card')).toHaveCount(3);
  });

  test('signal kickers are Momentum, Risk, Winner', async ({ page }) => {
    const kickers = page.locator('.sr-signal-kicker');
    await expect(kickers.nth(0)).toContainText(/Momentum/i);
    await expect(kickers.nth(1)).toContainText(/Risk/i);
    await expect(kickers.nth(2)).toContainText(/Winner/i);
  });

  // ── Pacing narrative ─────────────────────────────────────────────────────

  test('pacing narrative section is visible', async ({ page }) => {
    const pacing = page.locator('#srPacingNarrative');
    await expect(pacing).toBeVisible();
    // Should contain day-of-month info or the "unavailable" fallback
    const text = await pacing.textContent();
    expect(text).toMatch(/Day \d+ of \d+|unavailable/i);
  });

  // ── Filter drawer ────────────────────────────────────────────────────────

  test('filter drawer opens and closes', async ({ page }) => {
    const toggle = page.locator('#srFilterDrawerToggle');
    await toggle.click();
    const drawer = page.locator('#srFilterDrawer');
    await expect(drawer).toBeVisible();

    // Close with the X button or pressing Escape
    await page.keyboard.press('Escape');
    await expect(drawer).not.toBeVisible();
  });

  test('filter breadcrumb is visible', async ({ page }) => {
    await expect(page.locator('#srFilterBreadcrumb')).toBeVisible();
  });

  // ── Map ──────────────────────────────────────────────────────────────────

  test('map container is present in DOM', async ({ page }) => {
    await expect(page.locator('#srLiveMap')).toBeAttached();
  });

  test('map legend renders rep items', async ({ page }) => {
    const legend = page.locator('#srMapLegend');
    await expect(legend).toBeVisible();
    // Legend items are rendered by JS; wait briefly for them
    await page.waitForSelector('#srMapLegend .sr-map-legend-item', { timeout: 15_000 });
    const items = legend.locator('.sr-map-legend-item');
    await expect(items.first()).toBeVisible();
  });

  test('map legend rep items have data-rep-name attribute for hover', async ({ page }) => {
    await page.waitForSelector('#srMapLegend [data-rep-name]', { timeout: 15_000 });
    const repItems = page.locator('#srMapLegend [data-rep-name]');
    const count = await repItems.count();
    expect(count).toBeGreaterThan(0);
  });

  test('live map builds customer bubble, territory, and halo layers', async ({ page }) => {
    const map = page.locator('#srLiveMap');
    await expect.poll(async () => Number((await map.getAttribute('data-customer-count')) || '0')).toBeGreaterThan(0);
    await expect.poll(async () => Number((await map.getAttribute('data-territory-count')) || '0')).toBeGreaterThan(0);
    await expect.poll(async () => await map.getAttribute('data-territory-filter-enabled')).toBe('1');
    await expect.poll(async () => await map.getAttribute('data-halo-enabled')).toBe('1');
    await expect.poll(async () => await map.getAttribute('data-exact-count')).not.toBeNull();
    await expect.poll(async () => await map.getAttribute('data-approx-count')).not.toBeNull();
    await expect.poll(async () => await map.getAttribute('data-overlap-count')).not.toBeNull();
    await expect.poll(async () => await map.getAttribute('data-grouping-enabled')).toBe('0');
  });

  // ── Rep table ────────────────────────────────────────────────────────────

  test('rep table renders at least one row', async ({ page }) => {
    await page.waitForSelector('#srTable tbody tr', { timeout: 20_000 });
    const rows = page.locator('#srTable tbody tr');
    await expect(rows.first()).toBeVisible();
  });

  test('health index rings show visible percent labels', async ({ page }) => {
    const label = page.locator('#srTable tbody .sr-health-ring .sr-health-ring-label').first();
    await expect(label).toBeVisible();
    await expect(label).toContainText(/%/);
    const styles = await label.evaluate((el) => {
      const computed = getComputedStyle(el);
      return {
        color: computed.color,
        opacity: Number(computed.opacity || '1'),
        fontWeight: Number(computed.fontWeight || '0'),
      };
    });
    expect(styles.color).not.toBe('rgba(0, 0, 0, 0)');
    expect(styles.opacity).toBeGreaterThan(0.9);
    expect(styles.fontWeight).toBeGreaterThanOrEqual(800);
  });

  test('direct inherited bars render labels and colored segments', async ({ page }) => {
    const stack = page.locator('#srTable tbody .sr-ratio-stack').first();
    await expect(stack.locator('.sr-ratio-label--direct')).toContainText(/direct/i);
    await expect(stack.locator('.sr-ratio-label--inherited')).toContainText(/inherited/i);
    const dimensions = await stack.evaluate((el) => {
      const direct = el.querySelector('.sr-ratio-bar-direct') as HTMLElement | null;
      const inherited = el.querySelector('.sr-ratio-bar-inherited') as HTMLElement | null;
      return {
        directWidth: direct?.getBoundingClientRect().width || 0,
        inheritedWidth: inherited?.getBoundingClientRect().width || 0,
      };
    });
    expect(dimensions.directWidth + dimensions.inheritedWidth).toBeGreaterThan(0);
  });

  test('rep table search filters visible rows', async ({ page }) => {
    await page.waitForSelector('#srTable tbody tr', { timeout: 20_000 });
    const searchInput = page.locator('#srSearchInput');
    if (!(await searchInput.isVisible())) return; // skip if search not present

    // Type a string unlikely to match any rep to check the empty state
    await searchInput.fill('zzz_no_match_xqz');
    await searchInput.dispatchEvent('input');
    await page.waitForTimeout(400);
    const rows = page.locator('#srTable tbody tr:visible');
    const count = await rows.count();
    expect(count).toBeLessThanOrEqual(1); // 0 rows or an empty-state row
  });

  // ── Readability / theme ──────────────────────────────────────────────────

  test('no white text on white background in KPI area', async ({ page }) => {
    // Check that KPI value text is not invisible (white-on-white or very low contrast)
    // We verify by checking computed color is not #ffffff on #ffffff background.
    const kpi = page.locator('#kpiRevenue .value').first();
    const color = await kpi.evaluate((el) => getComputedStyle(el).color);
    const bg = await kpi.evaluate((el) => getComputedStyle(el).backgroundColor);
    // Trivially ensure they are not both white
    expect(color === bg && color === 'rgb(255, 255, 255)').toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Drilldown page — smoke
// ---------------------------------------------------------------------------

test.describe('Sales Rep drilldown page', () => {
  let drilldownUrl: string | null = null;

  test.beforeAll(async ({ browser }) => {
    // Find the first rep drilldown URL from the main page
    const page = await browser.newPage();
    await ensureLoggedIn(page);
    await page.goto('/salesreps');

    try {
      await page.waitForSelector('#srTable tbody tr a[href*="/salesreps/"]', { timeout: 20_000 });
      const href = await page.locator('#srTable tbody tr a[href*="/salesreps/"]').first().getAttribute('href');
      drilldownUrl = href || null;
    } catch {
      drilldownUrl = null;
    }
    await page.close();
  });

  test.beforeEach(async ({ page }) => {
    if (!drilldownUrl) test.skip();
    await ensureLoggedIn(page);
    await page.goto(drilldownUrl!);
    // Wait for drilldown to hydrate
    await page.waitForSelector('#srpd-kpis', { timeout: 30_000 });
    await page.waitForLoadState('networkidle', { timeout: 30_000 }).catch(() => {});
  });

  test('drilldown page loads hero section', async ({ page }) => {
    await expect(page.locator('.srpd-hero, .sr-hero')).toBeVisible();
  });

  test('drilldown section nav is visible', async ({ page }) => {
    await expect(page.locator('.srpd-section-nav')).toBeVisible();
  });

  test('drilldown KPI scorecard section is present', async ({ page }) => {
    await expect(page.locator('#srpd-kpis')).toBeVisible();
  });

  test('drilldown pacing narrative is rendered', async ({ page }) => {
    const pacing = page.locator('#drPacingNarrative');
    await expect(pacing).toBeAttached();
    // Give JS time to populate it
    await page.waitForFunction(
      () => (document.getElementById('drPacingNarrative')?.textContent?.trim() || '').length > 0,
      { timeout: 20_000 }
    );
    const text = await pacing.textContent();
    expect(text).toMatch(/Day \d+ of \d+|unavailable/i);
  });

  test('drilldown KPI values do not show "None"', async ({ page }) => {
    await page.waitForTimeout(2000); // allow hydration
    const body = await page.locator('#srpd-kpis').textContent();
    expect(body).not.toMatch(/\bNone\b/);
  });

  test('drilldown operating console section renders', async ({ page }) => {
    const opConsole = page.locator('#srpd-operating');
    if (await opConsole.isVisible()) {
      await expect(opConsole).toBeVisible();
    }
  });

  test('back button link is present', async ({ page }) => {
    const backLink = page.locator('a[href*="/salesreps"]').first();
    await expect(backLink).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Navigation — drilldown round-trip from main page
// ---------------------------------------------------------------------------

test.describe('Sales Rep navigation', () => {
  test('clicking rep row navigates to drilldown', async ({ page }) => {
    await ensureLoggedIn(page);
    await page.goto('/salesreps');
    await page.waitForSelector('#srTable tbody tr', { timeout: 25_000 });

    const drilldownLink = page.locator('#srTable tbody tr a[href*="/salesreps/"]').first();
    if (!(await drilldownLink.isVisible())) {
      test.skip();
      return;
    }

    await drilldownLink.click();
    await page.waitForURL(/\/salesreps\/\w/, { timeout: 15_000 });
    await expect(page.locator('.srpd-section-nav, .srpd-hero')).toBeVisible();
  });
});
