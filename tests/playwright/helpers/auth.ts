import type { Page } from '@playwright/test';

const adminUser = process.env.ADMIN_USERNAME || process.env.PLAYWRIGHT_ADMIN_USER || 'admin';
const adminPass = process.env.ADMIN_PASSWORD || process.env.PLAYWRIGHT_ADMIN_PASS || 'admin';
const isLoginPath = (value: string): boolean => /\/(?:auth\/)?login(?:[/?]|$)/.test(String(value || ''));
let cachedCookies: Array<{
  name: string;
  value: string;
  domain: string;
  path: string;
  expires: number;
  httpOnly: boolean;
  secure: boolean;
  sameSite: 'Strict' | 'Lax' | 'None';
}> | null = null;

export async function ensureLoggedIn(page: Page): Promise<void> {
  if (cachedCookies?.length) {
    await page.context().addCookies(cachedCookies);
  }

  const dashboardProbe = await page.request.get('/dashboard/', {
    failOnStatusCode: false,
    maxRedirects: 0,
  });
  const redirectTarget = dashboardProbe.headers()['location'] || '';
  const redirectedToLogin =
    [301, 302, 303, 307, 308].includes(dashboardProbe.status()) &&
    isLoginPath(redirectTarget);
  if (dashboardProbe.ok() || !redirectedToLogin) {
    return;
  }

  let loginPath = '/auth/login';
  try {
    const redirectUrl = new URL(redirectTarget, 'http://localhost');
    const nextValue = redirectUrl.searchParams.get('next');
    if (nextValue) {
      loginPath = `/auth/login?next=${encodeURIComponent(nextValue)}`;
    }
  } catch (_err) {
    loginPath = '/auth/login';
  }
  const loginPage = await page.request.get(loginPath, {
    failOnStatusCode: false,
  });
  if (!loginPage.ok()) {
    throw new Error(`Unable to load login page: ${loginPage.status()}`);
  }

  const loginHtml = await loginPage.text();
  const csrfMatch =
    loginHtml.match(/name="csrf_token"[^>]*value="([^"]+)"/i) ||
    loginHtml.match(/value="([^"]+)"[^>]*name="csrf_token"/i);
  const csrfToken = csrfMatch?.[1] || '';

  const loginResp = await page.request.post(loginPath, {
    failOnStatusCode: false,
    maxRedirects: 0,
    form: {
      csrf_token: csrfToken,
      username: adminUser,
      password: adminPass,
      remember_me: 'y',
      submit: 'Login',
    },
  });

  if (![200, 302, 303].includes(loginResp.status())) {
    throw new Error(`Login request failed: ${loginResp.status()}`);
  }

  const target = loginResp.headers()['location'] || '/dashboard/';
  await page.goto(target);
  if (isLoginPath(await page.url())) {
    throw new Error('Login did not establish an authenticated browser session.');
  }

  cachedCookies = await page.context().cookies();
}
