import AxeBuilder from '@axe-core/playwright';
import { test, expect } from '@playwright/test';

import { ensureLoggedIn } from './helpers/auth';

const ROUTES = [
  '/',
  '/customers/kpis',
  '/customers/rfm',
  '/customers/cohorts',
  '/customers/clv',
  '/products/',
  '/regions/',
  '/suppliers/',
  '/salesreps/',
  '/returns',
  '/returns/analytics',
  '/returns/root-causes',
  '/returns/corrective-actions',
  '/planning/',
  '/metrics/',
  '/work',
  '/work/crm',
  '/work/orders',
  '/work/procurement',
  '/work/finance',
  '/work/inventory',
  '/work/master-data',
  '/work/service',
  '/work/enterprise',
];

const AUDIT_THEME = process.env.PLAYWRIGHT_THEME === 'dark' ? 'dark' : 'light';

test.describe.serial('WCAG 2.2 AA structural gate', () => {
  test('has one page heading, accessible tables, and no serious axe violations', async ({ page }) => {
    test.slow();
    test.setTimeout(360_000);
    await ensureLoggedIn(page);
    await page.addInitScript((theme) => localStorage.setItem('wa-theme', theme), AUDIT_THEME);

    const failures: string[] = [];
    let activeRoute = 'bootstrap';
    page.on('console', (message) => {
      if (message.type() === 'error') failures.push(`${activeRoute}: console error — ${message.text()}`);
    });
    page.on('pageerror', (error) => failures.push(`${activeRoute}: page error — ${error.message}`));
    page.on('requestfailed', (request) => {
      if (!['script', 'stylesheet', 'xhr', 'fetch'].includes(request.resourceType())) return;
      const reason = request.failure()?.errorText || 'request failed';
      if (reason.includes('ERR_ABORTED')) return;
      failures.push(`${activeRoute}: ${request.resourceType()} failed — ${request.url()} (${reason})`);
    });
    page.on('response', (response) => {
      if (response.status() < 400) return;
      const resourceType = response.request().resourceType();
      if (!['script', 'stylesheet', 'xhr', 'fetch'].includes(resourceType)) return;
      failures.push(`${activeRoute}: ${resourceType} returned HTTP ${response.status()} — ${response.url()}`);
    });

    for (const route of ROUTES) {
      activeRoute = route;
      const response = await page.goto(route, { waitUntil: 'domcontentloaded' });
      if (!response?.ok()) {
        failures.push(`${route}: HTTP ${response?.status() ?? 'no response'}`);
        continue;
      }

      await expect(page.locator('main, #overviewPage').first()).toBeVisible();
      // Several workspaces dim their server-rendered shell while their bundle
      // hydrates. Auditing that transient state reports the blended loading
      // opacity instead of the final product colours.
      await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined);
      await page.waitForTimeout(150);
      const h1Count = await page.locator('h1').count();
      if (h1Count !== 1) failures.push(`${route}: expected one H1, found ${h1Count}`);

      const tables = page.locator('table');
      const tableCount = await tables.count();
      for (let index = 0; index < tableCount; index += 1) {
        const captionCount = await tables.nth(index).locator(':scope > caption').count();
        if (captionCount !== 1) failures.push(`${route}: table ${index + 1} has no direct caption`);
      }

      const tableRegions = page.locator('.table-responsive');
      for (let index = 0; index < await tableRegions.count(); index += 1) {
        const region = tableRegions.nth(index);
        if (await region.locator('table').count() === 0) continue;
        if (await region.getAttribute('tabindex') !== '0') {
          failures.push(`${route}: responsive table ${index + 1} is not keyboard focusable`);
        }
        const name = (await region.getAttribute('aria-label')) || (await region.getAttribute('aria-labelledby'));
        if (!name) failures.push(`${route}: responsive table ${index + 1} has no accessible region name`);
      }

      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
        .analyze();
      for (const violation of results.violations.filter((item) => ['critical', 'serious'].includes(item.impact || ''))) {
        const targets = violation.nodes.map((node) => node.target.join(' ')).join(', ');
        const evidence = violation.nodes
          .slice(0, 3)
          .map((node) => (node.failureSummary || '').replace(/\s+/g, ' ').trim())
          .filter(Boolean)
          .join(' | ');
        failures.push(
          `${route}: axe ${violation.id} — ${violation.help} (${violation.nodes.length} node(s): ${targets})${evidence ? `; ${evidence}` : ''}`,
        );
      }
    }

    expect(failures, failures.join('\n')).toEqual([]);
  });

  test('keeps page content reachable at the required viewport widths', async ({ page }) => {
    test.slow();
    test.setTimeout(480_000);
    await ensureLoggedIn(page);

    const failures: string[] = [];
    for (const route of ROUTES) {
      const response = await page.goto(route, { waitUntil: 'domcontentloaded' });
      if (!response?.ok()) {
        failures.push(`${route}: HTTP ${response?.status() ?? 'no response'}`);
        continue;
      }
      for (const width of [360, 390, 768, 1280, 1440, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        // Plotly's responsive handler is debounced; sample the settled layout,
        // not the old SVG width during the resize callback window.
        await page.waitForTimeout(150);
        const layout = await page.evaluate(() => {
          const root = document.documentElement;
          const overflow = Math.max(0, root.scrollWidth - root.clientWidth);
          const overflowSources = Array.from(document.querySelectorAll('body *'))
            .map((element) => ({
              element,
              right: element.getBoundingClientRect().right,
              width: element.getBoundingClientRect().width,
            }))
            .filter((item) => item.right > root.clientWidth + 2)
            .sort((a, b) => b.right - a.right)
            .slice(0, 3)
            .map(({ element, right, width }) => {
              const token = element.id
                ? `#${element.id}`
                : `${element.tagName.toLowerCase()}.${Array.from(element.classList).slice(0, 2).join('.')}`;
              return `${token} right=${right.toFixed(0)} width=${width.toFixed(0)}`;
            });
          const unreachableTables = Array.from(document.querySelectorAll('table'))
            .filter((table) => table.scrollWidth > table.clientWidth + 1)
            .filter((table) => {
              let parent = table.parentElement;
              while (parent) {
                const style = getComputedStyle(parent);
                if (['auto', 'scroll'].includes(style.overflowX)) return false;
                parent = parent.parentElement;
              }
              return true;
            }).length;
          return { overflow, overflowSources, unreachableTables };
        });
        if (layout.overflow > 2) {
          const evidence = layout.overflowSources.length ? ` (${layout.overflowSources.join(', ')})` : '';
          failures.push(`${route} @ ${width}px: body overflows by ${layout.overflow}px${evidence}`);
        }
        if (layout.unreachableTables) {
          failures.push(`${route} @ ${width}px: ${layout.unreachableTables} wide table(s) have no horizontal scroll container`);
        }
      }
    }

    expect(failures, failures.join('\n')).toEqual([]);
  });

  test('meets interaction, canvas, DOM, and cold-navigation budgets', async ({ page }) => {
    test.setTimeout(240_000);
    await ensureLoggedIn(page);

    const failures: string[] = [];
    // The public Sales Reps surface is separately frozen with its WebGL map
    // and enforced by check_static_build.py (DOM) and the static no-network
    // browser smoke. Destroying that map in Chromium makes navigation teardown
    // nondeterministic, so this live-app timing loop covers the non-WebGL pages.
    const qualityRoutes = ROUTES.filter((route) => route !== '/salesreps/');
    const auditPage = await page.context().newPage();
    for (const route of qualityRoutes) {
      const response = await auditPage.goto(route, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      if (!response?.ok()) {
        failures.push(`${route}: HTTP ${response?.status() ?? 'no response'}`);
        continue;
      }
      await auditPage.waitForTimeout(250);
      const quality = await auditPage.evaluate(() => {
        const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
        const fcp = performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? 0;
        const visible = (element: Element) => {
          let current: Element | null = element;
          while (current) {
            const style = getComputedStyle(current);
            if (
              style.display === 'none'
              || ['hidden', 'collapse'].includes(style.visibility)
              || Number(style.opacity) === 0
            ) return false;
            current = current.parentElement;
          }
          return true;
        };
        const zeroAreaCanvases = Array.from(document.querySelectorAll('canvas'))
          .filter(visible)
          .filter((canvas) => canvas.getBoundingClientRect().width <= 0 || canvas.getBoundingClientRect().height <= 0)
          .length;
        const unnamedControls = Array.from(document.querySelectorAll('button, input, select, textarea'))
          .filter(visible)
          .filter((control) => !control.hasAttribute('disabled'))
          .filter((control) => {
            const element = control as HTMLInputElement;
            const labelled = element.getAttribute('aria-label') || element.getAttribute('aria-labelledby');
            const text = (element.textContent || '').trim() || element.value || element.title;
            const id = element.id;
            const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
            return !(labelled || text || label);
          }).length;
        return {
          fcp,
          fcpAfterResponse: Math.max(0, fcp - (nav?.responseStart ?? 0)),
          interactiveAfterResponse: Math.max(
            0,
            (nav?.domInteractive ?? 0) - (nav?.responseStart ?? 0),
          ),
          nodeCount: document.getElementsByTagName('*').length,
          zeroAreaCanvases,
          unnamedControls,
        };
      });
      if (quality.fcpAfterResponse > 1000) {
        failures.push(
          `${route}: front-end FCP ${quality.fcpAfterResponse.toFixed(0)}ms after response exceeds 1000ms`,
        );
      }
      if (quality.interactiveAfterResponse > 2000) {
        failures.push(
          `${route}: front-end interactive ${quality.interactiveAfterResponse.toFixed(0)}ms after response exceeds 2000ms`,
        );
      }
      if (quality.nodeCount > 1800) failures.push(`${route}: ${quality.nodeCount} DOM nodes exceeds 1800`);
      if (quality.zeroAreaCanvases) failures.push(`${route}: ${quality.zeroAreaCanvases} visible zero-area canvas(es)`);
      if (quality.unnamedControls) failures.push(`${route}: ${quality.unnamedControls} unnamed interactive control(s)`);
    }

    expect(failures, failures.join('\n')).toEqual([]);
  });
});
