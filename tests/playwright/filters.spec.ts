import { test, expect } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

test.describe('Global filters', () => {
  test('render and populate without reload', async ({ page }) => {
    await ensureLoggedIn(page);
    await page.goto('/');

    const filters = page.locator('#GlobalFilters');
    await expect(filters).toBeVisible();
    await expect(filters.locator('#filtersApply')).toBeVisible();

    const regionOptions = await filters.locator('#fRegions option').count();
    const regionTileText = await filters.locator('#filterTileRegions').textContent();
    expect(
      regionOptions > 0 ||
      (regionTileText || '').includes('No values available') ||
      (regionTileText || '').includes('Open selector'),
    ).toBeTruthy();

    const methodOptions = await filters.locator('#fMethods option').count();
    const methodTileText = await filters.locator('#filterTileMethods').textContent();
    expect(
      methodOptions > 0 ||
      (methodTileText || '').includes('No values available') ||
      (methodTileText || '').includes('Open selector'),
    ).toBeTruthy();
  });
});
