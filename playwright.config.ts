import { defineConfig, devices } from '@playwright/test';

const host = process.env.PLAYWRIGHT_HOST || '127.0.0.1';
const port = process.env.PLAYWRIGHT_PORT || '4173';
const baseURL = process.env.PLAYWRIGHT_BASE_URL || `http://${host}:${port}`;

export default defineConfig({
  testDir: 'tests/playwright',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    ignoreHTTPSErrors: true,
    storageState: process.env.PLAYWRIGHT_AUTH_STATE || undefined,
  },
  webServer: {
    command: `${process.env.PYTHON || 'python3'} tests/playwright/theme_audit_boot.py`,
    url: baseURL,
    timeout: 180_000,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
