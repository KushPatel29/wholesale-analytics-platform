import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { ensureLoggedIn } from './helpers/auth';

const adminUser = process.env.ADMIN_USERNAME || process.env.PLAYWRIGHT_ADMIN_USER || 'admin';
const adminPass = process.env.ADMIN_PASSWORD || process.env.PLAYWRIGHT_ADMIN_PASS || 'admin';
const adminToken = process.env.ADMIN_API_TOKEN;
const artifactsDir = path.join('artifacts', 'playwright');

const toNumber = (text: string | null): number => {
  if (!text) return 0;
  const cleaned = text.replace(/[^0-9.\-]/g, '');
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : 0;
};

const pctDiff = (actual: number, expected: number): number => {
  if (!expected) return actual ? 1 : 0;
  return Math.abs(actual - expected) / expected;
};

test.describe('Admin parity smoke', () => {
  test('overview/products/suppliers match SQL truth within 0.5% (last 3 months)', async ({ page }) => {
    fs.mkdirSync(artifactsDir, { recursive: true });
    await ensureLoggedIn(page);

    const end = new Date();
    const start = new Date();
    start.setMonth(start.getMonth() - 3);
    const qs = new URLSearchParams({
      start: start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10),
      statuses: 'packed',
    }).toString();

    const headers: Record<string, string> = {
      Authorization: `Basic ${Buffer.from(`${adminUser}:${adminPass}`).toString('base64')}`,
    };
    if (adminToken) {
      headers['X-Admin-Token'] = adminToken;
    }
    const auditResp = await page.request.get(`/api/_admin/audit/window?${qs}`, { headers });
    if (!auditResp.ok()) {
      const failureBody = await auditResp.text();
      test.skip(
        [424, 503].includes(auditResp.status()) || /dataset not built|run etl/i.test(failureBody),
        `Admin audit API unavailable in this environment (${auditResp.status()}).`,
      );
    }
    expect(auditResp.ok()).toBeTruthy();
    const audit = await auditResp.json();
    await fs.promises.writeFile(
      path.join(artifactsDir, 'audit-window.json'),
      JSON.stringify(audit, null, 2),
      'utf-8',
    );

    const sqlTruth = audit?.sql_truth || {};
    const targetRevenue = Number(sqlTruth.total_revenue || 0);
    const targetProducts = Number(sqlTruth.distinct_products || 0);
    expect(targetRevenue).toBeGreaterThan(0);

    // Overview page revenue
    await page.goto(`/overview?${qs}`);
    const overviewRevenueEl = page.locator('[data-kpi="revenue"]');
    await expect(overviewRevenueEl).toBeVisible({ timeout: 20000 });
    await expect(overviewRevenueEl).not.toContainText('ƒ', { timeout: 20000 });
    const overviewRevenue = toNumber(await overviewRevenueEl.textContent());
    expect(pctDiff(overviewRevenue, targetRevenue)).toBeLessThan(0.005);
    await page.screenshot({ path: path.join(artifactsDir, 'overview.png'), fullPage: true });

    // Products page revenue + unique products
    await page.goto(`/products?${qs}`);
    const prodRevenueEl = page.locator('#kpiRevenueValue');
    const prodProductsEl = page.locator('#kpiUniqueProductsValue');
    await expect(prodRevenueEl).toBeVisible({ timeout: 20000 });
    await expect(prodRevenueEl).not.toContainText('ƒ', { timeout: 20000 });
    const productsRevenue = toNumber(await prodRevenueEl.textContent());
    const uniqueProducts = toNumber(await prodProductsEl.textContent());
    expect(pctDiff(productsRevenue, targetRevenue)).toBeLessThan(0.005);
    if (targetProducts > 0) {
      expect(pctDiff(uniqueProducts, targetProducts)).toBeLessThan(0.005);
    }
    await page.screenshot({ path: path.join(artifactsDir, 'products.png'), fullPage: true });

    // Suppliers page revenue
    await page.goto(`/suppliers?${qs}`);
    const suppliersRevenueEl = page.locator('#kpiRevenue');
    await expect(suppliersRevenueEl).toBeVisible({ timeout: 20000 });
    await expect(suppliersRevenueEl).not.toContainText('ƒ', { timeout: 20000 });
    const suppliersRevenue = toNumber(await suppliersRevenueEl.textContent());
    expect(pctDiff(suppliersRevenue, targetRevenue)).toBeLessThan(0.005);
    await page.screenshot({ path: path.join(artifactsDir, 'suppliers.png'), fullPage: true });
  });
});
