-- What this costs, and the cap that stops it costing more.
--
-- A resource monitor holds DBT_WH to 10 credits a month: notify at 75%, stop
-- new queries at 100%, cancel running ones at 110%. The warehouse is X-Small,
-- suspends after 60 seconds idle and cancels any statement over 10 minutes.
-- Every script here sets a QUERY_TAG ('northgate:...'), so the report below can
-- say which part of the platform spent the time. ACCOUNT_USAGE views lag by up
-- to three hours, so run the report a while after the work.
ALTER SESSION SET QUERY_TAG = 'northgate:cost';
USE ROLE ACCOUNTADMIN;

CREATE RESOURCE MONITOR IF NOT EXISTS NORTHGATE_MONTHLY
  WITH CREDIT_QUOTA = 10
       FREQUENCY = MONTHLY
       START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 75 PERCENT DO NOTIFY
           ON 100 PERCENT DO SUSPEND
           ON 110 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE DBT_WH SET
  RESOURCE_MONITOR = NORTHGATE_MONTHLY
  AUTO_SUSPEND = 60
  STATEMENT_TIMEOUT_IN_SECONDS = 600;

SHOW RESOURCE MONITORS LIKE 'NORTHGATE_MONTHLY';
SET MONITOR_QID = LAST_QUERY_ID();
SHOW WAREHOUSES LIKE 'DBT_WH';
SET WAREHOUSE_QID = LAST_QUERY_ID();

WITH
monitor AS (
  SELECT OBJECT_CONSTRUCT('name', "name", 'credit_quota', "credit_quota", 'used_credits', "used_credits",
                          'frequency', "frequency", 'notify_at', "notify_at", 'suspend_at', "suspend_at",
                          'suspend_immediately_at', "suspend_immediately_at") AS M
  FROM TABLE(RESULT_SCAN($MONITOR_QID))
),
warehouse AS (
  SELECT OBJECT_CONSTRUCT('name', "name", 'size', "size", 'auto_suspend', "auto_suspend",
                          'resource_monitor', "resource_monitor") AS W
  FROM TABLE(RESULT_SCAN($WAREHOUSE_QID))
),
metering AS (
  SELECT ARRAY_AGG(OBJECT_CONSTRUCT('day', DAY, 'warehouse', WAREHOUSE_NAME, 'credits', CREDITS))
           WITHIN GROUP (ORDER BY DAY, WAREHOUSE_NAME) AS DAYS,
         SUM(CREDITS) AS TOTAL
  FROM (SELECT TO_DATE(START_TIME) AS DAY, WAREHOUSE_NAME, ROUND(SUM(CREDITS_USED), 4) AS CREDITS
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE START_TIME >= '2026-10-01' GROUP BY 1, 2)
),
by_tag AS (
  SELECT ARRAY_AGG(OBJECT_CONSTRUCT('query_tag', TAG, 'queries', QUERIES, 'execution_seconds', SECS,
                                    'gb_scanned', GB))
           WITHIN GROUP (ORDER BY SECS DESC) AS TAGS
  FROM (SELECT QUERY_TAG AS TAG, COUNT(*) AS QUERIES, ROUND(SUM(EXECUTION_TIME) / 1000, 1) AS SECS,
               ROUND(SUM(BYTES_SCANNED) / POWER(1024, 3), 3) AS GB
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= '2026-10-01' AND QUERY_TAG LIKE 'northgate:%'
        GROUP BY 1)
),
storage AS (
  SELECT ROUND(AVG(AVERAGE_DATABASE_BYTES) / POWER(1024, 2), 1) AS MB
  FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
  WHERE DATABASE_NAME = 'NORTHGATE_DEMO'
    AND USAGE_DATE = (SELECT MAX(USAGE_DATE) FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
                      WHERE DATABASE_NAME = 'NORTHGATE_DEMO')
)
SELECT OBJECT_CONSTRUCT(
  'resource_monitor', monitor.M,
  'warehouse', warehouse.W,
  'credits_by_day', metering.DAYS,
  'credits_total_since_oct_1', metering.TOTAL,
  'northgate_queries_by_tag', by_tag.TAGS,
  'northgate_demo_storage_mb', storage.MB,
  'reported_at', CURRENT_TIMESTAMP()) AS COST_REPORT
FROM monitor, warehouse, metering, by_tag, storage;
