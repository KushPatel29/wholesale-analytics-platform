# 📖 Wholesale Analytics Data Dictionary (Canonical)

Use this guide for generating DuckDB SQL or Pandas logic. The system uses "Best Match" logic (see `app/services/fact_schema.py`), but the following are the **Canonical Names**.

## 📊 Sales Fact Dataset (`sales_fact.parquet`)

| Canonical Name | DuckDB Type | Description |
|---|---|---|
| `Date` | `DATE` | Transaction/Shipment Date |
| `OrderId` | `VARCHAR` | Unique identifier for the order |
| `ProductId` | `VARCHAR` | Unique identifier for the product |
| `ProductName` | `VARCHAR` | Human-readable product name |
| `SupplierId` | `VARCHAR` | Unique identifier for the supplier |
| `SupplierName` | `VARCHAR` | Human-readable supplier name |
| `CustomerId` | `VARCHAR` | Unique identifier for the customer |
| `CustomerName` | `VARCHAR` | Human-readable customer name |
| `RegionName` | `VARCHAR` | Geographic region name |
| `SalesRepName` | `VARCHAR` | Assigned sales representative |
| `Revenue` | `DOUBLE` | Total revenue (Price * Qty) |
| `Cost` | `DOUBLE` | Total cost (COGS) |
| `QuantityShipped`| `INTEGER` | Number of units shipped |
| `WeightLb` | `DOUBLE` | Total weight in pounds |

## 🧠 Candidate Column Search (Fuzzy Matching)
If a canonical column is missing, the system searches these candidates in order:
- **Revenue:** `revenue_packs_only`, `Revenue`, `ExtRevenue`, `Sales`, `NetSales`.
- **Cost:** `cost_packs_only`, `Cost`, `ExtCost`, `COGS`, `TotalCost`.
- **Quantity:** `QuantityShipped`, `Qty`, `Units`, `Quantity`.
- **Date:** `Date`, `ShipDate`, `OrderDate`, `InvoiceDate`.

## 🔒 Masking Rules
Columns containing `Cost`, `Margin`, `Profit`, `Spend`, or `COGS` are **auto-masked** for users without the `can_view_costs` permission.

---
*Generated for AI context by Gemini CLI. Refer to \`app/services/fact_schema.py\` for implementation details.*
