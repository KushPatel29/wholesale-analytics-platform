"""
Playwright E2E tests for enhanced overview analytics
Run with: pytest tests/test_overview_playwright.py
Or: python -m playwright codegen http://127.0.0.1:5000 (to generate tests)
"""
import re

import pytest
try:
    from playwright.sync_api import Page, expect, sync_playwright
except ModuleNotFoundError:
    pytest.skip("Playwright package not installed in this environment", allow_module_level=True)

try:
    with sync_playwright() as _pw:
        _ = _pw.chromium
except Exception:
    pytest.skip("Playwright browser not available in this environment", allow_module_level=True)

# The `page` fixture comes from the pytest-playwright plugin, which is a
# separate package from `playwright` itself. Installing playwright for the
# static build made the two module-level guards above pass while the fixture
# was still missing, turning a clean skip into a collection error.
pytest.importorskip(
    "pytest_playwright",
    reason="pytest-playwright provides the `page` fixture these tests take",
)

def test_overview_page_loads(page: Page):
    """Test that overview page loads successfully"""
    # Navigate to login
    page.goto("http://127.0.0.1:5000/auth/login")

    # Login
    page.fill("input[name='username']", "admin")
    page.fill("input[name='password']", "admin")
    page.click("button[type='submit']")

    # Wait for navigation
    page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

    # Check that page loaded
    expect(page).to_have_title(re.compile("Overview|Northgate Retail Analytics|Analytics", re.IGNORECASE))


# def test_growth_analytics_section_exists(page: Page):
#     """Test that growth analytics section exists"""
#     # Login first
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Check for growth analytics section
#     growth_section = page.locator("text=Growth Analytics").first
#     expect(growth_section).to_be_visible(timeout=10000)

#     # Check for MoM/YoY values
#     mom_value = page.locator("#rev-mom-value").first
#     yoy_value = page.locator("#rev-yoy-value").first

#     expect(mom_value).to_be_visible(timeout=10000)
#     expect(yoy_value).to_be_visible(timeout=10000)


# def test_weight_analytics_section_exists(page: Page):
#     """Test that weight analytics section exists"""
#     # Login first
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Check for weight analytics section
#     weight_section = page.locator("text=Weight").first
#     expect(weight_section).to_be_visible(timeout=10000)

#     # Check for total weight value
#     total_weight = page.locator("#total-weight-value").first
#     expect(total_weight).to_be_visible(timeout=10000)


# def test_predictions_section_exists(page: Page):
#     """Test that predictions section exists"""
#     # Login first
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Check for predictions section
#     pred_section = page.locator("text=Predictive Analytics").first
#     expect(pred_section).to_be_visible(timeout=10000)

#     # Check for prediction model info
#     pred_model = page.locator("#prediction-model").first
#     expect(pred_model).to_be_visible(timeout=10000)


# def test_customer_insights_section_exists(page: Page):
#     """Test that customer insights section exists"""
#     # Login first
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Check for customer insights section
#     customer_section = page.locator("text=Customer Insights").first
#     expect(customer_section).to_be_visible(timeout=10000)

#     # Check for total customers
#     total_customers = page.locator("#total-customers-insight").first
#     expect(total_customers).to_be_visible(timeout=10000)


# def test_filter_changes_update_data(page: Page):
#     """Test that changing filters updates the analytics"""
#     # Login first
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Wait for initial load
#     page.wait_for_timeout(2000)

#     # Get initial value
#     mom_value_before = page.locator("#rev-mom-value").first
#     initial_text = mom_value_before.inner_text()

#     # Change date filter (if exists)
#     date_start = page.locator("#fStart").first
#     if date_start.is_visible():
#         date_start.fill("2024-01-01")

#         # Click apply filters button
#         apply_btn = page.locator("button:has-text('Apply')").first
#         if apply_btn.is_visible():
#             apply_btn.click()

#             # Wait for page reload
#             page.wait_for_load_state("networkidle", timeout=10000)

#             # Check that value potentially changed (or at least page reloaded)
#             mom_value_after = page.locator("#rev-mom-value").first
#             expect(mom_value_after).to_be_visible(timeout=10000)


# def test_no_javascript_errors(page: Page):
#     """Test that there are no JavaScript errors on page load"""
#     errors = []

#     # Capture console errors
#     page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

#     # Login and navigate
#     page.goto("http://127.0.0.1:5000/auth/login")
#     page.fill("input[name='username']", "admin")
#     page.fill("input[name='password']", "admin")
#     page.click("button[type='submit']")
#     page.wait_for_url("http://127.0.0.1:5000/", timeout=5000)

#     # Wait for page to fully load
#     page.wait_for_load_state("networkidle", timeout=10000)
#     page.wait_for_timeout(3000)

#     # Check for errors
#     print(f"Console errors captured: {errors}")
#     assert len(errors) == 0, f"Page has JavaScript errors: {errors}"
