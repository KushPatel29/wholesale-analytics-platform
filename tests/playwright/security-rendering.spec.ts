import { test, expect } from '@playwright/test';
import fs from 'fs';

import { ensureLoggedIn } from './helpers/auth';

const attack = '<img id="dom-injection-proof" src="x">';

test.describe('Untrusted data rendering', () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  test('admin user and role labels render as text, never markup', async ({ page }) => {
    await page.route('**/api/_admin/users?*', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const baseline = Array.isArray(payload.users) && payload.users.length ? payload.users[0] : {};
      await route.fulfill({
        response,
        json: {
          ...payload,
          users: [{
            ...baseline,
            id: baseline.id || 901,
            email: `security-${attack}@example.test`,
            username: attack,
            full_name: attack,
            role: 'admin',
            is_active: true,
            is_approved: true,
          }],
          pagination: { page: 1, page_size: 25, total: 1 },
          stats: { pending: 0, active: 1, disabled: 0 },
        },
      });
    });

    await page.goto('/admin/users');
    const body = page.locator('[data-user-table-body]');
    await expect(body).toContainText(attack);
    await expect(page.locator('#dom-injection-proof')).toHaveCount(0);

    await page.unroute('**/api/_admin/users?*');
    await page.route('**/api/_admin/roles', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      await route.fulfill({
        response,
        json: { ...payload, roles: [{ id: 902, name: attack, permission_count: 1, permissions: [] }] },
      });
    });
    await page.goto('/admin/roles');
    await expect(page.locator('#roleTableBody')).toContainText(attack);
    await expect(page.locator('#dom-injection-proof')).toHaveCount(0);
  });

  test('overview narratives, driver labels, and movers render as text', async ({ page }) => {
    const narrativeAttack = `<img id="overview-narrative-injection" src="x">`;
    const moverAttack = `<img id="overview-mover-injection" src="x">`;
    const context = {
      meta: { has_data: true },
      narrative_insights: { narrative: [narrativeAttack], watchouts: [], callouts: [] },
      drivers: {
        mom: { revenue: { drivers: [{ driver: narrativeAttack, delta: 1, share_of_delta_pct: 1 }] } },
      },
      scorecard: {},
      deltas: {},
      trend_series: {},
      risk: {},
      data_health: {},
    };
    const movers = {
      rows: [{ label: moverAttack, current: 100, delta: 10, delta_pct_label: moverAttack }],
      meta: { rows: 1 },
    };
    await page.setContent(`
      <div id="overviewPageV2" data-api="/overview/api/context" data-movers-api="/overview/api/movers" data-overview-movers-fast="1">
        <ul id="whatChangedList"></ul><ul id="watchoutsList"></ul>
        <table><tbody id="driversMomRows"></tbody><tbody id="driversYoyRows"></tbody></table>
        <table><tbody id="moversGainersBody"></tbody><tbody id="moversDeclinersBody"></tbody></table>
      </div>
    `);
    await page.evaluate(({ contextPayload, moversPayload }) => {
      const mockFetch = async (input: RequestInfo | URL) => {
        const url = String(input);
        const payload = url.includes('/movers') ? moversPayload : contextPayload;
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };
      window.fetch = mockFetch as typeof window.fetch;
      (window as typeof window & { authFetch?: typeof window.fetch }).authFetch = mockFetch as typeof window.fetch;
      (window as typeof window & { filtersReady?: Promise<Record<string, never>> }).filtersReady = Promise.resolve({});
    }, { contextPayload: context, moversPayload: movers });
    const source = fs.readFileSync('app/static/js/overview_v2.js', 'utf8');
    await page.addScriptTag({ content: source });
    await expect(page.locator('#whatChangedList')).toContainText(narrativeAttack);
    await expect(page.locator('#driversMomRows')).toContainText(narrativeAttack);
    await expect(page.locator('#moversGainersBody')).toContainText(moverAttack);
    await expect(page.locator('#overview-narrative-injection')).toHaveCount(0);
    await expect(page.locator('#overview-mover-injection')).toHaveCount(0);
  });
});
