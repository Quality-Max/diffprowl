const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

test('renders the checkout button', async ({ page }) => {
  await page.setContent(fs.readFileSync(path.join(__dirname, 'landing.html'), 'utf8'));
  await expect(page.getByRole('button', { name: 'Checkout' })).toBeVisible();
});
