# 🎯 Northgate Retail Analytics: Project Vision

## 🗺️ High-Level Mission
Northgate Retail Analytics is a high-performance, DuckDB-powered dashboarding suite designed for the hospitality/retail sector. Its mission is to turn massive MSSQL datasets into actionable insights for Overview, Sales, Customers, Products, Suppliers, and Labor.

## 🔑 Core Philosophy
- **Speed First:** Use DuckDB and Parquet for sub-second analytic queries.
- **Security by Default:** Enforce row-level security (RBAC) across all dashboards and exports.
- **Unified Filters:** Shared `FilterParams` across the stack to ensure consistent views.
- **Audit-Ready:** Every major data flow or permission change is logged.

## 🚀 Key Value Propositions
- **The Bundle Pattern:** A unified server-side payload building system that fuels both the UI and JSON exports.
- **Dynamic Forecasting:** Integrated ML-driven forecasting for products and overview metrics.
- **Export Integrity:** Automated masking of sensitive fields in XLSX/CSV exports.

---
*Generated for AI context by Gemini CLI.*

## 🛡️ Security-First Mantra
- **No Bypasses:** Local/Demo modes must still use `AccessPolicy` to prevent silent production leaks.
- **Zero Secret Drift:** `SECRET_KEY` must never have a default value in `config.py`.
- **Audit Everything:** All admin actions and data exports are logged with actor context.
