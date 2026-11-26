const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testMatch: '*.spec.cjs',
  projects: ['chromium', 'firefox', 'webkit'].map(browserName => ({
    name: browserName, use: { browserName },
  })),
  reporter: [
    ['list'],
    ...(process.env.DIFFPROWL_PLAYWRIGHT_REPORTER ? [[process.env.DIFFPROWL_PLAYWRIGHT_REPORTER]] : []),
  ],
});
