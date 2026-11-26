const { test, expect } = require('@playwright/test');

test('shows the status message', async ({ page }) => {
  await page.setContent('<p role="status">Ready</p>');
  await expect(page.getByRole('status')).toHaveText('Ready');
});
