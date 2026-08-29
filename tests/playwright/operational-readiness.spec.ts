import AxeBuilder from '@axe-core/playwright';
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

  test('desktop header menus support keyboard focus, dismissal, and selection', async ({ page }) => {
    await page.goto('/');
    const analyticsToggle = page.locator('.navbar-wholesale [data-bs-toggle="dropdown"]')
      .filter({ hasText: 'Analytics' }).first();
    const menu = analyticsToggle.locator('xpath=..').locator('.dropdown-menu');
    const firstItem = menu.locator('a[href], button:not([disabled]), [role="menuitem"]').first();

    await analyticsToggle.focus();
    await analyticsToggle.press('ArrowDown');
    await expect(analyticsToggle).toHaveAttribute('aria-expanded', 'true');
    await expect(menu).toBeVisible();
    await expect(firstItem).toBeFocused();

    await page.keyboard.press('Escape');
    await expect(analyticsToggle).toHaveAttribute('aria-expanded', 'false');
    await expect(analyticsToggle).toBeFocused();

    await analyticsToggle.click();
    await Promise.all([
      page.waitForURL(/\/metrics\/?(?:\?.*)?$/),
      menu.getByRole('menuitem', { name: 'Metric Catalogue' }).click(),
    ]);
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

  test('operational details use domain intelligence and remain mobile-safe', async ({ page }) => {
    await page.goto('/work/crm');
    const form = page.locator('form[action="/work/crm/records"]');
    await form.locator('#recordTitle').fill('Accessibility verification opportunity');
    await form.locator('#recordType').selectOption('opportunity');
    await form.locator('#recordStatus').selectOption('proposal');
    await form.locator('#accountRef').fill('PW-ACCOUNT-1');
    await form.locator('#amountValue').fill('120000');
    await form.locator('#probabilityValue').fill('45');
    await form.locator('#forecastCategory').selectOption('best_case');
    await form.locator('#nextStep').fill('Confirm the buying committee');
    await form.locator('#dueValue').fill('2026-10-15T00:00');
    await Promise.all([
      page.waitForURL(/\/work\/records\/\d+$/),
      form.getByRole('button', { name: 'Create draft' }).click(),
    ]);

    await expect(page.getByText('Weighted value', { exact: true })).toBeVisible();
    await expect(page.getByText('Next step', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Revenue decision timeline' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Protect the close date/ })).toBeVisible();
    await expect(page.getByText('Create linked action', { exact: true })).toBeVisible();
    await expect(page.getByText('Quantity', { exact: true })).toHaveCount(0);

    const axe = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
      .analyze();
    expect(axe.violations.filter((violation) => violation.impact === 'critical')).toEqual([]);

    await page.setViewportSize({ width: 390, height: 844 });
    const viewportOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(viewportOverflow).toBeLessThanOrEqual(2);
    await expect(page.locator('.decision-control-brief')).toBeVisible();
  });
});
