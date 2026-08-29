import { defineConfig, devices } from '@playwright/test';

const host = process.env.PLAYWRIGHT_HOST || '127.0.0.1';
const port = process.env.PLAYWRIGHT_PORT || '4173';
const baseURL = process.env.PLAYWRIGHT_BASE_URL || `http://${host}:${port}`;

export default defineConfig({
  testDir: 'tests/playwright',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['github'], ['line']] : [['list']],
  // The release specs share one analytics server and deliberately exercise
  // expensive, stateful routes. CI runners must not fan those files out across
  // workers or the browser budget becomes a measure of self-contention.
  workers: process.env.CI ? 1 : undefined,
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    // Audit the settled interface, not intermediate opacity frames from entry
    // animations. The application already honors this OS-level preference.
    reducedMotion: 'reduce',
    ignoreHTTPSErrors: true,
    storageState: process.env.PLAYWRIGHT_AUTH_STATE || undefined,
  },
  webServer: {
    command: `${process.env.PYTHON || 'python3'} tests/playwright/theme_audit_boot.py`,
    url: baseURL,
    timeout: 180_000,
    reuseExistingServer: process.env.PLAYWRIGHT_REUSE_SERVER === '1',
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
