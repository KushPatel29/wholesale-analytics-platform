import { test, expect } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

/**
 * Smoke test for the Products intelligence page.
 * Assumes the app is running locally (override with PLAYWRIGHT_BASE_URL).
 * If authentication is required, provide PLAYWRIGHT_AUTH_STATE pointing to a storage state JSON.
 */
test.describe('Products page', () => {
  test('renders KPIs, charts, velocity, and recommendations', async ({ page }) => {
    await ensureLoggedIn(page);
    // Navigate to products page
    await page.goto('/products');

    // Basic page identity
    await expect(page.getByRole('heading', { name: /Product Intelligence/i })).toBeVisible();

    // Hero exports visible
    await expect(page.getByRole('link', { name: /Export Excel/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Export CSV/i })).toBeVisible();

    // Velocity pulse metrics are present
    const velocityIds = [
      '#velAvgWeekly',
      '#velW13',
      '#velWeeklyRevenue',
      '#velRevPerProduct',
      '#velActive',
      '#velRoi',
      '#velRetail',
      '#velTopMover',
    ];
    for (const id of velocityIds) {
      await expect(page.locator(id)).toBeVisible();
    }

    // Key charts exist
    await expect(page.locator('#priceVelocityChart')).toBeVisible();
    await expect(page.locator('#trendChart')).toBeVisible();
    await expect(page.locator('#topChart')).toBeVisible();
    await expect(page.locator('#moversChart')).toBeVisible();

    // Table loads rows (after client hydration)
    await page.waitForSelector('#productTbody tr', { timeout: 20000 });
    const rowCount = await page.locator('#productTbody tr').count();
    expect(rowCount).toBeGreaterThan(0);

    // Recommendations panel responds to Intel button if available
    const intelButton = page.locator('.js-rec').first();
    if (await intelButton.isVisible()) {
      await intelButton.click();
      await expect(page.locator('#recPanel')).toBeVisible();
    }
  });
});
