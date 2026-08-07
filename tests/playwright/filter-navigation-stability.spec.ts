import { test, expect, type Page } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

async function waitForFiltersReady(page: Page): Promise<void> {
  await expect(page.locator('#GlobalFilters')).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('#fDatePreset')).toBeAttached();
  await expect(page.locator('#filtersApply')).toBeVisible();
}

async function setDatePreset(page: Page, preset: string): Promise<void> {
  await page.click(`[data-range="${preset}"]`);
}

async function armFiltersChanged(page: Page, key: string): Promise<void> {
  await page.evaluate((promiseKey) => {
    (window as Window & Record<string, unknown>)[promiseKey] = new Promise((resolve) => {
      window.addEventListener(
        'globalFilters:changed',
        (evt) => resolve((evt as CustomEvent).detail || {}),
        { once: true },
      );
    });
  }, key);
}

async function waitForArmedFiltersChanged(page: Page, key: string): Promise<void> {
  await page.evaluate(async (promiseKey) => {
    await (window as Window & Record<string, Promise<unknown>>)[promiseKey];
  }, key);
}

async function expectFiltersHealthy(page: Page): Promise<void> {
  await expect(page.locator('#filtersApplySpinner')).toHaveClass(/d-none/);
  await expect(page.locator('#filtersErrorBanner')).toBeHidden();
  await expect(page.locator('#filtersRetryWrap')).toBeHidden();
}

test.describe('Filter navigation stability', () => {
  test('keeps active filters stable across overview, products, and suppliers without request loops', async ({ page }) => {
    const counts = {
      filterApply: 0,
      productsBundle: 0,
      suppliersBundle: 0,
    };
    const filterLifecycleErrors: string[] = [];

    page.on('requestfinished', (request) => {
      const url = request.url();
      if (url.includes('/api/filters/apply')) counts.filterApply += 1;
      if (url.includes('/api/products/bundle')) counts.productsBundle += 1;
      if (url.includes('/api/suppliers/bundle')) counts.suppliersBundle += 1;
    });
    page.on('response', (response) => {
      if (response.status() >= 500 && response.url().includes('/api/filters/')) {
        filterLifecycleErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await ensureLoggedIn(page);
    await page.goto('/overview');
    await expect(page.locator('#overviewPage')).toBeVisible({ timeout: 20_000 });
    await waitForFiltersReady(page);

    const customPreset = 'all';

    await setDatePreset(page, customPreset);
    await expect(page.locator('#filtersApply')).toBeEnabled();

    await armFiltersChanged(page, '__gfChangedApply');
    await page.click('#filtersApply');
    await waitForArmedFiltersChanged(page, '__gfChangedApply');
    await expectFiltersHealthy(page);
    await expect(page.locator('#fDatePreset')).toHaveValue(customPreset);
    expect(page.url()).toContain(`date_preset=${encodeURIComponent(customPreset)}`);

    await page.goto('/products');
    await expect(page.locator('#products-main')).toBeVisible({ timeout: 20_000 });
    await page.waitForSelector('#productTbody tr', { timeout: 20_000 });
    await waitForFiltersReady(page);
    await expectFiltersHealthy(page);
    await expect(page.locator('#fDatePreset')).toHaveValue(customPreset);

    await page.goto('/suppliers/');
    await expect(page.locator('#SuppliersV2App, #SuppliersApp')).toBeVisible({ timeout: 20_000 });
    await waitForFiltersReady(page);
    await expectFiltersHealthy(page);
    await expect(page.locator('#fDatePreset')).toHaveValue(customPreset);

    expect(filterLifecycleErrors).toEqual([]);
    expect(counts.filterApply).toBe(1);
    expect(counts.productsBundle).toBeLessThanOrEqual(3);
    expect(counts.suppliersBundle).toBeLessThanOrEqual(3);
  });

  test('reset clears active filters and keeps subsequent navigation stable', async ({ page }) => {
    await ensureLoggedIn(page);
    await page.goto('/overview');
    await expect(page.locator('#overviewPage')).toBeVisible({ timeout: 20_000 });
    await waitForFiltersReady(page);

    const defaultPreset = await page.locator('#fDatePreset').inputValue();
    const customPreset = 'all';

    await setDatePreset(page, customPreset);
    await armFiltersChanged(page, '__gfChangedBeforeReset');
    await page.click('#filtersApply');
    await waitForArmedFiltersChanged(page, '__gfChangedBeforeReset');
    await expect(page.locator('#fDatePreset')).toHaveValue(customPreset);

    await armFiltersChanged(page, '__gfChangedReset');
    await page.click('#resetFiltersBtn');
    await waitForArmedFiltersChanged(page, '__gfChangedReset');
    await expectFiltersHealthy(page);
    await expect(page.locator('#fDatePreset')).toHaveValue(defaultPreset);
    expect(page.url()).not.toContain(`date_preset=${encodeURIComponent(customPreset)}`);
    await expect(page.locator('#overviewPage')).toBeVisible({ timeout: 20_000 });

    await page.goto('/products');
    await expect(page.locator('#products-main')).toBeVisible({ timeout: 20_000 });
    await waitForFiltersReady(page);
    await expectFiltersHealthy(page);
    await expect(page.locator('#fDatePreset')).toHaveValue(defaultPreset);
    expect(page.url()).not.toContain(`date_preset=${encodeURIComponent(customPreset)}`);
  });
});
