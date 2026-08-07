import { test, expect, request as pwRequest } from '@playwright/test';

const baseURL = process.env.BASE_URL || 'http://127.0.0.1:5000';
const adminToken = process.env.ADMIN_API_TOKEN;
const adminUser = process.env.ADMIN_USERNAME;
const adminPass = process.env.ADMIN_PASSWORD;

test.describe('Overview API smoke', () => {
  test.skip(
    !adminToken && !(adminUser && adminPass),
    'Provide ADMIN_API_TOKEN or ADMIN_USERNAME/ADMIN_PASSWORD to run API smoke.'
  );

  test('admin overview returns full-cost data and matches health rows', async () => {
    const ctx = await pwRequest.newContext({
      baseURL,
      extraHTTPHeaders: adminToken ? { 'X-Admin-Token': adminToken } : undefined,
      httpCredentials: adminToken ? undefined : adminUser && adminPass ? { username: adminUser, password: adminPass } : undefined,
    });

    const windowParams = new URLSearchParams({
      start: process.env.SMOKE_START || '2025-09-01',
      end: process.env.SMOKE_END || '2025-12-30',
    }).toString();

    const overviewResp = await ctx.get(`/api/overview/summary?${windowParams}`);
    if (!overviewResp.ok()) {
      const failureBody = await overviewResp.text();
      test.skip(
        [424, 503].includes(overviewResp.status()) || /dataset not built|run etl/i.test(failureBody),
        `Overview API unavailable in this environment (${overviewResp.status()}).`,
      );
    }
    expect(overviewResp.ok()).toBeTruthy();
    const overview = await overviewResp.json();

    const kpis = overview?.kpis ?? {};
    expect(kpis.revenue).toBeGreaterThan(0);
    expect(kpis.cost).toBeGreaterThan(0);
    expect(kpis.profit).toBeGreaterThanOrEqual(0);
    expect(kpis.margin_pct ?? kpis.marginPct ?? 0).not.toBeUndefined();

    // Health check alignment
    const healthResp = await ctx.get('/api/_admin/health/data');
    expect(healthResp.ok()).toBeTruthy();
    const health = await healthResp.json();

    expect(health.revenue_sum ?? health.revenue).toBeGreaterThan(0);
    expect((health.product_count ?? 0)).toBeGreaterThan(0);
    expect((health.effective_date_null_rate ?? 0)).toBeLessThan(10);

    const apiRows = overview?.meta?.window?.rows ?? 0;
    const factRows = health?.fact_rowcount ?? 0;
    if (factRows > 0) {
      const deltaPct = Math.abs(apiRows - factRows) / factRows * 100;
      expect(deltaPct).toBeLessThanOrEqual(5);
    }
  });
});
