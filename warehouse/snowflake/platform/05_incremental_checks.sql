-- The state of the incremental pipeline after the latest batch, as one JSON
-- object. Expected values are computed from SYNTHETIC_SEED.SALES_FACT directly,
-- never from the tables the tasks maintain.
ALTER SESSION SET QUERY_TAG = 'northgate:incremental-checks';
USE ROLE SYSADMIN;
USE WAREHOUSE DBT_WH;
USE SCHEMA NORTHGATE_DEMO.INGEST;

WITH
landed AS (SELECT COALESCE(MAX(BATCH_ID), 0) AS N, COUNT(*) AS EVENTS FROM SALES_LINE_EVENTS),
truth AS (
  SELECT "OrderLineId" AS ORDER_LINE_ID, "Date" AS ORDER_DATE, "OrderId" AS ORDER_ID, "CustomerId" AS CUSTOMER_ID,
         "ProductId" AS PRODUCT_ID, "SupplierName" AS SUPPLIER_NAME, "RegionName" AS REGION_NAME,
         "Revenue" AS REVENUE, "DiscountAmount" AS DISCOUNT_AMOUNT, "Cost" AS COST,
         "QuantityShipped" AS QUANTITY_SHIPPED, "WeightLb" AS WEIGHT_LB,
         CASE WHEN MOD("OrderLineId", 997) = 0 AND "Date" < '2026-06-01' THEN 3
              WHEN "Date" < '2026-04-01' THEN 1 WHEN "Date" < '2026-06-01' THEN 2 ELSE 3 END AS ARRIVES_IN
  FROM NORTHGATE_DEMO.SYNTHETIC_SEED.SALES_FACT
),
-- What should be in the current table after batch N, from the truth alone.
expected AS (
  SELECT
    (SELECT COUNT(*) FROM truth WHERE ARRIVES_IN <= (SELECT N FROM landed)) AS REAL_LINES,
    IFF((SELECT N FROM landed) BETWEEN 1 AND 2,
        (SELECT COUNT(*) FROM truth WHERE MOD(ORDER_LINE_ID, 1013) = 0 AND ARRIVES_IN = 1), 0) AS TEST_ORDERS,
    IFF((SELECT N FROM landed) = 1,
        (SELECT COUNT(*) FROM truth WHERE MOD(ORDER_LINE_ID, 101) = 0 AND ARRIVES_IN = 1), 0) AS PROVISIONAL_PRICES
),
current_state AS (
  SELECT COUNT_IF(ORDER_LINE_ID > 0) AS REAL_LINES, COUNT_IF(ORDER_LINE_ID < 0) AS TEST_ORDERS
  FROM SALES_LINES_CURRENT
),
-- Real lines whose values differ from the source, and lines in one but not the other.
diff AS (
  SELECT
    (SELECT COUNT(*) FROM SALES_LINES_CURRENT c JOIN truth t USING (ORDER_LINE_ID)
       WHERE NOT (c.ORDER_DATE = t.ORDER_DATE AND c.ORDER_ID = t.ORDER_ID AND c.CUSTOMER_ID = t.CUSTOMER_ID
                  AND c.PRODUCT_ID = t.PRODUCT_ID AND c.SUPPLIER_NAME = t.SUPPLIER_NAME
                  AND c.REGION_NAME = t.REGION_NAME AND c.REVENUE = t.REVENUE
                  AND c.DISCOUNT_AMOUNT = t.DISCOUNT_AMOUNT AND c.COST = t.COST
                  AND c.QUANTITY_SHIPPED = t.QUANTITY_SHIPPED AND c.WEIGHT_LB = t.WEIGHT_LB)) AS VALUE_MISMATCHES,
    (SELECT COUNT(*) FROM truth t WHERE t.ARRIVES_IN <= (SELECT N FROM landed)
       AND t.ORDER_LINE_ID NOT IN (SELECT ORDER_LINE_ID FROM SALES_LINES_CURRENT)) AS MISSING_LINES,
    (SELECT COUNT(*) FROM SALES_LINES_CURRENT c WHERE c.ORDER_LINE_ID > 0
       AND c.ORDER_LINE_ID NOT IN (SELECT t.ORDER_LINE_ID FROM truth t
                                   WHERE t.ARRIVES_IN <= (SELECT N FROM landed))) AS UNEXPECTED_LINES
),
-- The incrementally maintained aggregate against a full recompute of the current table.
full_daily AS (
  SELECT ORDER_DATE, REGION_NAME, COUNT(*) AS ORDER_LINES, SUM(REVENUE::NUMBER(18, 6)) AS REVENUE,
         SUM(COST::NUMBER(18, 6)) AS COST, SUM(DISCOUNT_AMOUNT::NUMBER(18, 6)) AS DISCOUNT_AMOUNT
  FROM SALES_LINES_CURRENT GROUP BY 1, 2
),
daily_diff AS (
  SELECT
    (SELECT COUNT(*) FROM (SELECT ORDER_DATE, REGION_NAME, ORDER_LINES, REVENUE, COST, DISCOUNT_AMOUNT FROM full_daily
                           MINUS SELECT ORDER_DATE, REGION_NAME, ORDER_LINES, REVENUE, COST, DISCOUNT_AMOUNT FROM DAILY_REGION_SALES)) AS MISSING_OR_WRONG,
    (SELECT COUNT(*) FROM (SELECT ORDER_DATE, REGION_NAME, ORDER_LINES, REVENUE, COST, DISCOUNT_AMOUNT FROM DAILY_REGION_SALES
                           MINUS SELECT ORDER_DATE, REGION_NAME, ORDER_LINES, REVENUE, COST, DISCOUNT_AMOUNT FROM full_daily)) AS EXTRA_OR_WRONG,
    (SELECT COUNT(*) FROM DAILY_REGION_SALES) AS CELLS
),
runs AS (
  SELECT ARRAY_AGG(OBJECT_CONSTRUCT('task', NAME, 'state', STATE, 'scheduled_from', SCHEDULED_FROM,
                                    'scheduled', SCHEDULED_TIME, 'completed', COMPLETED_TIME,
                                    'query_id', QUERY_ID, 'error', ERROR_MESSAGE))
           WITHIN GROUP (ORDER BY SCHEDULED_TIME, NAME) AS RUNS
  FROM TABLE(NORTHGATE_DEMO.INFORMATION_SCHEMA.TASK_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP()), RESULT_LIMIT => 200))
  WHERE DATABASE_NAME = 'NORTHGATE_DEMO' AND SCHEMA_NAME = 'INGEST'
    AND STATE IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
)
SELECT OBJECT_CONSTRUCT(
  'after_batch', landed.N,
  'events_landed', landed.EVENTS,
  'expected', OBJECT_CONSTRUCT('real_lines', expected.REAL_LINES, 'test_orders', expected.TEST_ORDERS,
                               'provisional_prices', expected.PROVISIONAL_PRICES),
  'actual', OBJECT_CONSTRUCT('real_lines', current_state.REAL_LINES, 'test_orders', current_state.TEST_ORDERS,
                             'value_mismatches', diff.VALUE_MISMATCHES, 'missing_lines', diff.MISSING_LINES,
                             'unexpected_lines', diff.UNEXPECTED_LINES),
  'daily_region_sales', OBJECT_CONSTRUCT('cells', daily_diff.CELLS, 'missing_or_wrong', daily_diff.MISSING_OR_WRONG,
                                         'extra_or_wrong', daily_diff.EXTRA_OR_WRONG),
  'streams_pending', OBJECT_CONSTRUCT(
      'events', SYSTEM$STREAM_HAS_DATA('NORTHGATE_DEMO.INGEST.SALES_LINE_EVENTS_STREAM'),
      'current', SYSTEM$STREAM_HAS_DATA('NORTHGATE_DEMO.INGEST.SALES_LINES_CURRENT_STREAM')),
  'task_runs', runs.RUNS,
  'checked_at', CURRENT_TIMESTAMP()) AS INCREMENTAL_STATE
FROM landed, expected, current_state, diff, daily_diff, runs;
