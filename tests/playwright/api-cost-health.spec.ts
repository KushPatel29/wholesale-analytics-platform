import { test, expect } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

const adminUser = process.env.ADMIN_USERNAME || process.env.PLAYWRIGHT_ADMIN_USER || 'admin';
const adminPass = process.env.ADMIN_PASSWORD || process.env.PLAYWRIGHT_ADMIN_PASS || 'admin';
const adminToken = process.env.ADMIN_API_TOKEN;
const missingThreshold = Number(process.env.PLAYWRIGHT_COST_MISSING_THRESH || 0.02);

test.describe('API cost health', () => {
  test('overview summary exposes Cost with low missing rate', async ({ page }) => {
    await ensureLoggedIn(page);

    const end = new Date();
    const start = new Date();
    start.setMonth(start.getMonth() - 3);
    const qs = new URLSearchParams({
      start: start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10),
    }).toString();

    const headers: Record<string, string> = {};
    if (adminUser && adminPass) {
      headers['Authorization'] = `Basic ${Buffer.from(`${adminUser}:${adminPass}`).toString('base64')}`;
    }
    if (adminToken) {
      headers['X-Admin-Token'] = adminToken;
    }

    const resp = await page.request.get(`/api/overview/summary?${qs}`, { headers });
    if (!resp.ok()) {
      const failureBody = await resp.text();
      test.skip(
        [424, 503].includes(resp.status()) || /dataset not built|run etl/i.test(failureBody),
        `Overview summary API unavailable in this environment (${resp.status()}).`,
      );
    }
    expect(resp.ok()).toBeTruthy();
    const payload = await resp.json();

    expect(typeof payload?.kpis?.cost).toBe('number');
    const missingCols: string[] = payload?.meta?.missing_columns || [];
    expect(missingCols).not.toContain('Cost');
    const rate = Number(payload?.meta?.cost_missing_rate ?? 1);
    expect(rate).toBeLessThan(missingThreshold);
  });
});
