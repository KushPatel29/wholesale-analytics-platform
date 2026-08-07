import { test, expect } from '@playwright/test';
import { ensureLoggedIn } from './helpers/auth';

test.describe('Returns Enterprise Workflow', () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  test('Full Lifecycle: Submission to Finance Closure', async ({ page }) => {
    console.log('Starting lifecycle test with order 462640...');
    
    // 1. Sales Submission
    await page.goto('/returns/new', { waitUntil: 'networkidle', timeout: 60000 });
    await expect(page.locator('#order_number')).toBeVisible({ timeout: 30000 });
    
    await page.fill('#order_number', '462640');
    await page.click('#loadOrderButton');
    await page.waitForTimeout(3000);
    
    // Fill manually if not loaded (mock env)
    const custName = await page.locator('#customer_name').inputValue();
    if (!custName) {
      console.log('Order not found in mock, filling manually...');
      await page.fill('#customer_name', 'Enterprise E2E Customer');
      await page.evaluate(() => {
        (document.getElementById('customerIdField') as HTMLInputElement).value = 'C-E2E-1';
        (document.getElementById('orderDateField') as HTMLInputElement).value = '2026-04-13';
      });
    }

    await page.selectOption('#company', { label: 'the distributor Meats' });
    await page.fill('#additional_notes', 'Enterprise E2E Test - Order 462640');
    
    // Line items
    const itemRows = page.locator('.return-item-row');
    if (await itemRows.count() === 0) {
      await page.click('#addItemButton');
      await page.fill('input[name="product_code[]"]', 'TEST-SKU');
      await page.fill('input[name="product_desc[]"]', 'TEST-PRODUCT');
      await page.fill('input[name="weight_lb[]"]', '45.5');
      await page.fill('input[name="price_per_lb[]"]', '2.13');
    }
    
    // Ensure reasons are set
    const reasonSelects = page.locator('select[name="reason_for_return[]"]');
    const reasonCount = await reasonSelects.count();
    for (let i = 0; i < reasonCount; i++) {
      await reasonSelects.nth(i).selectOption({ index: 0 });
    }
    
    await page.click('button[type="submit"]');
    await expect(page.locator('.alert-success')).toContainText('created', { timeout: 30000 });
    
    const rmaId = page.url().replace(/\/$/, '').split('/').pop();
    console.log('RMA created:', rmaId);
    
    // 2. Ops Stage
    console.log('Ops Stage...');
    const debug = page.locator('#enterprise-debug');
    console.log('Diagnostic Data:', {
      status: await debug.getAttribute('data-status'),
      canOps: await debug.getAttribute('data-can-ops'),
      canWH: await debug.getAttribute('data-can-wh'),
      canMGR: await debug.getAttribute('data-can-mgr'),
      user: await debug.getAttribute('data-user'),
    });
    console.log('Buttons:', await page.locator('button').allTextContents());

    await page.click('button:has-text("Schedule Pickup")', { timeout: 30000 });
    await expect(page.locator('.step.active')).toContainText('Operations');
    await page.click('button:has-text("Mark Picked Up")');
    await expect(page.locator('.step.active')).toContainText('Warehouse', { timeout: 30000 });
    console.log('Marked as picked up successfully.');

    // 3. Warehouse Review
    console.log('Warehouse Stage...');
    // Find the first line item ID from the hidden inputs
    const itemIds = await page.locator('input[name="item_ids"]').evaluateAll(inputs => inputs.map(i => (i as HTMLInputElement).value));
    const firstItemId = itemIds[0];
    console.log('Interacting with Item ID:', firstItemId);

    await page.fill(`input[name="item_received_weight_lb_${firstItemId}"]`, '45.0');
    await page.selectOption(`select[name="item_warehouse_outcome_${firstItemId}"]`, 'Returning to Inventory');
    await page.fill(`input[name="item_packs_count_${firstItemId}"]`, '1');
    await page.fill(`input[name="item_follow_up_action_${firstItemId}"]`, 'Credit');

    await page.click('button:has-text("Save Receiving Update")');
    await expect(page.locator('.alert-success')).toContainText('Receiving updates saved', { timeout: 30000 });

    await page.click('button:has-text("WH Approve")');
    await expect(page.locator('.step.active')).toContainText('Approval', { timeout: 30000 });
    
    // 4. Manager Approval
    console.log('Approval Stage - Waiting for Manager Approve button...');
    const mgrApproveBtn = page.locator('button:has-text("Manager Approve")').first();
    await expect(mgrApproveBtn).toBeVisible({ timeout: 30000 });
    
    console.log('Clicking Manager Approve...');
    // Manager approve triggers a download
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 60000 }),
      mgrApproveBtn.click({ force: true })
    ]);
    console.log('Credit-PO Downloaded:', download.suggestedFilename());
    
    // Reload to see new status
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.locator('.step.active')).toContainText('Finance', { timeout: 30000 });
    
    // 5. Finance Stage
    console.log('Finance Stage...');
    await expect(page.locator('a:has-text("Sage CSV")')).toBeVisible();
    await page.click('button:has-text("Finance Close")');
    
    // Final State
    await expect(page.locator('.badge-primary, .text-bg-primary')).toContainText('Completed', { timeout: 30000 });
    console.log('Lifecycle test complete.');
  });

  test('Batch Operations: Finance Export', async ({ page }) => {
    console.log('Starting batch export test...');
    await page.goto('/returns', { waitUntil: 'networkidle' });
    
    // Wait for at least one row to appear
    const row = page.locator('.row-select').first();
    await expect(row).toBeVisible({ timeout: 30000 });
    
    await row.check();
    await expect(page.locator('#bulkActionsRow')).toBeVisible();
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.click('#bulkExportSage')
    ]);
    expect(download.suggestedFilename()).toContain('batch-sage-export');
    console.log('Batch Export successful.');
  });
});
