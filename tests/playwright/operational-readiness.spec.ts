import { test, expect } from '@playwright/test';

import { ensureLoggedIn } from './helpers/auth';

test.describe('Operational readiness regressions', () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  test('desktop header establishes the foreground stacking context', async ({ page }) => {
    await page.goto('/');
    const stacking = await page.locator('.navbar-wholesale').evaluate((navbar) => {
      const style = getComputedStyle(navbar);
      return { position: style.position, zIndex: Number(style.zIndex), isolation: style.isolation };
    });
    expect(stacking.position).toBe('relative');
    expect(stacking.zIndex).toBeGreaterThanOrEqual(1040);
    expect(stacking.isolation).toBe('isolate');
  });

  test('overview action links reveal diagnostic content', async ({ page }) => {
    await page.goto('/');
    const disclosure = page.locator('#diagnosticWorkspacesDisclosure');
    await expect(disclosure).not.toHaveAttribute('open', '');

    await page.getByRole('link', { name: 'Open trust center' }).first().click();
    await expect(disclosure).toHaveAttribute('open', '');
    await expect(page.locator('#dataHealthSection')).toBeVisible();
  });

  for (const route of ['/work', '/work/crm']) {
    test(`${route} exposes command-ledger controls and governed lifecycle`, async ({ page }) => {
      await page.goto(route);
      await expect(page.getByRole('heading', { name: 'Find, focus, and clear the next exception' })).toBeVisible();
      await expect(page.locator('#workspaceSearch')).toBeVisible();
      await expect(page.locator('#workspaceStatus')).toBeVisible();
      await expect(page.locator('#workspaceOwner')).toBeVisible();
      await expect(page.locator('.decision-quick-filters')).toBeVisible();
      await expect(page.getByRole('heading', { name: /process flow|execution flow/i })).toBeVisible();

      if (route.endsWith('/crm')) {
        await expect(page.getByRole('heading', { name: 'Opportunity stage board' })).toBeVisible();
        await expect(page.locator('.decision-crm-intel')).toBeVisible();
        const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(desktopOverflow).toBeLessThanOrEqual(2);
      }
    });
  }

  test('CRM command ledger remains usable at a mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/work/crm');
    await expect(page.locator('#workspaceSearch')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Apply view' })).toBeVisible();
    await expect(page.locator('.decision-quick-filters')).toBeVisible();
    const viewportOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(viewportOverflow).toBeLessThanOrEqual(2);
  });
});
