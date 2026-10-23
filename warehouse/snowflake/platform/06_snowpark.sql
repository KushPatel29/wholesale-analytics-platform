-- A second judge, written in Snowpark Python instead of SQL.
--
-- The procedure rebuilds the order lines and the daily region aggregate from
-- SYNTHETIC_SEED.SALES_FACT with the Snowpark DataFrame API, compares them with
-- what the Streams-and-Tasks pipeline maintained (EXCEPT both ways), and writes
-- one row per run to PLATFORM.SNOWPARK_RECONCILIATION. It runs inside
-- Snowflake: no data leaves the warehouse and no credential is involved.
ALTER SESSION SET QUERY_TAG = 'northgate:snowpark';
USE ROLE SYSADMIN;
USE WAREHOUSE DBT_WH;

CREATE TABLE IF NOT EXISTS NORTHGATE_DEMO.PLATFORM.SNOWPARK_RECONCILIATION (
  RUN_AT TIMESTAMP_LTZ, RESULT VARIANT);

CREATE OR REPLACE PROCEDURE NORTHGATE_DEMO.PLATFORM.RECONCILE_INCREMENTAL()
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python')
  HANDLER = 'run'
  EXECUTE AS OWNER
AS
$$
import json

from snowflake.snowpark import Session
from snowflake.snowpark import functions as F
from snowflake.snowpark.types import DecimalType

SOURCE = {
    "OrderLineId": "ORDER_LINE_ID", "Date": "ORDER_DATE", "OrderId": "ORDER_ID", "CustomerId": "CUSTOMER_ID",
    "ProductId": "PRODUCT_ID", "SupplierName": "SUPPLIER_NAME", "RegionName": "REGION_NAME", "Revenue": "REVENUE",
    "DiscountAmount": "DISCOUNT_AMOUNT", "Cost": "COST", "QuantityShipped": "QUANTITY_SHIPPED", "WeightLb": "WEIGHT_LB",
}
MONEY = DecimalType(18, 6)


def run(session: Session):
    truth = session.table("NORTHGATE_DEMO.SYNTHETIC_SEED.SALES_FACT").select(
        [F.col(f'"{src}"').alias(dst) for src, dst in SOURCE.items()])
    current = session.table("NORTHGATE_DEMO.INGEST.SALES_LINES_CURRENT").select(list(SOURCE.values()))

    expected_daily = (
        truth.select("ORDER_DATE", "REGION_NAME",
                     F.col("REVENUE").cast(MONEY).alias("R"), F.col("COST").cast(MONEY).alias("C"),
                     F.col("DISCOUNT_AMOUNT").cast(MONEY).alias("D"))
        .group_by("ORDER_DATE", "REGION_NAME")
        .agg(F.count(F.lit(1)).alias("ORDER_LINES"), F.sum("R").alias("REVENUE"),
             F.sum("C").alias("COST"), F.sum("D").alias("DISCOUNT_AMOUNT")))
    daily = session.table("NORTHGATE_DEMO.INGEST.DAILY_REGION_SALES").select(
        "ORDER_DATE", "REGION_NAME", "ORDER_LINES", "REVENUE", "COST", "DISCOUNT_AMOUNT")

    totals = truth.agg(F.count(F.lit(1)).alias("N"), F.sum(F.col("REVENUE").cast(MONEY)).alias("REV")).collect()[0]
    daily_totals = daily.agg(F.sum("ORDER_LINES").alias("N"), F.sum("REVENUE").alias("REV")).collect()[0]
    result = {
        "engine": "Snowpark Python " + session.sql("SELECT CURRENT_VERSION()").collect()[0][0],
        "source_lines": totals["N"],
        "current_lines": current.count(),
        "lines_only_in_source": truth.except_(current).count(),
        "lines_only_in_pipeline": current.except_(truth).count(),
        "daily_cells_expected": expected_daily.count(),
        "daily_cells_pipeline": daily.count(),
        "daily_only_in_source": expected_daily.except_(daily).count(),
        "daily_only_in_pipeline": daily.except_(expected_daily).count(),
        "source_revenue": str(totals["REV"]),
        "pipeline_revenue": str(daily_totals["REV"]),
        "pipeline_lines_in_daily": daily_totals["N"],
    }
    session.sql(
        "INSERT INTO NORTHGATE_DEMO.PLATFORM.SNOWPARK_RECONCILIATION SELECT CURRENT_TIMESTAMP(), PARSE_JSON(?)",
        params=[json.dumps(result)]).collect()
    return result
$$;

CALL NORTHGATE_DEMO.PLATFORM.RECONCILE_INCREMENTAL();
