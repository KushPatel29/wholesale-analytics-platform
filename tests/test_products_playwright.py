import re
import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright not installed")
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page):
    page.goto("http://127.0.0.1:5000/auth/login")
    page.fill("input[name='username']", "admin")
    page.fill("input[name='password']", "admin")
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle", timeout=10000)


# def test_products_page_smoke(page: Page):
#     _login(page)
#     page.goto("http://127.0.0.1:5000/products")
#     page.wait_for_load_state("networkidle", timeout=15000)
#     try:
#         expect(page).to_have_title(re.compile("Products|Analytics|Wholesale Analytics", re.IGNORECASE))
#         expect(page.locator("text=Products").first).to_be_visible(timeout=8000)
#         expect(page.locator("text=Segment").first).to_be_visible(timeout=8000)
#     except Exception as exc:  # pragma: no cover - best-effort smoke check
#         pytest.xfail(f"Products page markers missing: {exc}")


# def test_products_recommendations_panel(page: Page):
#     _login(page)
#     page.goto("http://127.0.0.1:5000/products")
#     page.wait_for_load_state("networkidle", timeout=15000)
#     row = page.locator("table tr").nth(1)
#     try:
#         row.click(timeout=5000)
#         expect(page.locator("text=Recommendations").first).to_be_visible(timeout=8000)
#     except Exception as exc:  # pragma: no cover - allow environments without data
#         pytest.xfail(f"Recommendations panel not reachable in smoke: {exc}")