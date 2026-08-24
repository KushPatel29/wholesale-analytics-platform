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

test.describe.serial('WCAG 2.2 AA structural gate', () => {
  test('has one page heading, accessible tables, and no critical axe violations', async ({ page }) => {
    test.slow();
    test.setTimeout(360_000);
    await ensureLoggedIn(page);

    const failures: string[] = [];
    for (const route of ROUTES) {
      const response = await page.goto(route, { waitUntil: 'domcontentloaded' });
      if (!response?.ok()) {
        failures.push(`${route}: HTTP ${response?.status() ?? 'no response'}`);
        continue;
      }

      await expect(page.locator('main, #overviewPage').first()).toBeVisible();
      const h1Count = await page.locator('h1').count();
      if (h1Count !== 1) failures.push(`${route}: expected one H1, found ${h1Count}`);

      const tables = page.locator('table');
      const tableCount = await tables.count();
      for (let index = 0; index < tableCount; index += 1) {
        const captionCount = await tables.nth(index).locator(':scope > caption').count();
        if (captionCount !== 1) failures.push(`${route}: table ${index + 1} has no direct caption`);
      }

      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
        .analyze();
      for (const violation of results.violations.filter((item) => item.impact === 'critical')) {
        const targets = violation.nodes.map((node) => node.target.join(' ')).join(', ');
        failures.push(
          `${route}: axe ${violation.id} — ${violation.help} (${violation.nodes.length} node(s): ${targets})`,
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
        const layout = await page.evaluate(() => {
          const root = document.documentElement;
          const overflow = Math.max(0, root.scrollWidth - root.clientWidth);
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
          return { overflow, unreachableTables };
        });
        if (layout.overflow > 2) failures.push(`${route} @ ${width}px: body overflows by ${layout.overflow}px`);
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
