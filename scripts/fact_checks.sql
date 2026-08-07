-- Fact Dataset Diagnostics (DuckDB)
--
-- Usage (on an ETL/app host with duckdb installed):
--   duckdb -c ".read scripts/fact_checks.sql"
--
-- By default this assumes you're running from the repo/app root and that the
-- partitioned dataset lives under `cache/fact_dataset/`.
--
-- If your dataset lives elsewhere, edit the `read_parquet(...)` path below.
--
CREATE OR REPLACE VIEW fact_raw AS
SELECT * FROM read_parquet('cache/fact_dataset/**/*.parquet', union_by_name=true);

CREATE OR REPLACE VIEW fact AS
SELECT * FROM fact_raw;

-- 1) Raw fact row count + max/min dates
SELECT
  COUNT(*) AS rows,
  MIN(CAST(Date AS DATE)) AS min_date,
  MAX(CAST(Date AS DATE)) AS max_date
FROM fact;

-- 2) Totals sanity (adjust columns if your schema differs)
SELECT
  COALESCE(SUM(CAST(Revenue AS DOUBLE)), 0) AS revenue,
  COALESCE(SUM(CAST(Cost AS DOUBLE)), 0) AS cost,
  COUNT(DISTINCT OrderId) AS orders,
  COUNT(DISTINCT OrderLineId) AS order_lines
FROM fact;

-- 3) Duplicate detection by business key (OrderLineId is the canonical key)
SELECT
  OrderLineId,
  COUNT(*) AS c
FROM fact
GROUP BY 1
HAVING COUNT(*) > 1
ORDER BY c DESC
LIMIT 50;

-- 4) 2026 presence check
SELECT
  COUNT(*) AS rows_2026
FROM fact
WHERE CAST(Date AS DATE) >= DATE '2026-01-01';
