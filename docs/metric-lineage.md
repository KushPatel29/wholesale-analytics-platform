# Northgate governed metric lineage

The public demo runs its application queries on DuckDB and SQLite, while the analytical project below is written in Snowflake-dialect dbt SQL. Both surfaces use the same synthetic source contract. The live metric catalogue is loaded directly from `models/marts/schema.yml`.

```mermaid
flowchart LR
    SF[(sales_fact)] --> SS[stg_sales_fact]
    LF[(labor_fact)] --> SL[stg_labor_fact]
    FF[(finance_month)] --> SFM[stg_finance_month]
    MC[(marketing_campaign)] --> SMC[stg_marketing_campaign]
    FB[(forecast_backtest)] --> SFB[stg_forecast_backtest]
    RR[(return_rma)] --> SR[stg_return_rma]
    RI[(return_rma_item)] --> SRI[stg_return_rma_item]
    RCA[(return_corrective_action)] --> SRCA[stg_return_corrective_action]

    SS --> CP[fct_commercial_performance]
    SR --> CP
    SS --> CM[fct_customer_movement]
    SS --> IP[fct_inventory_performance]
    SL --> LP[fct_labor_performance]
    SFB --> FP[fct_forecast_performance]
    SR --> RP[fct_returns_performance]
    SRI --> RP
    SRCA --> RCAI[fct_return_corrective_actions]
    RP --> RCAI
    SFM --> FIN[fct_finance_monthly]
    SMC --> MKT[fct_marketing_performance]

    CP --> CAT[Generated metric catalogue]
    CM --> CAT
    IP --> CAT
    LP --> CAT
    FP --> CAT
    RP --> CAT
    RCAI --> CAT
    FIN --> CAT
    MKT --> CAT

    CAT --> APP[Flask analytics surfaces]
    CAT --> TESTS[Formula and reconciliation gates]
```

## Certification contract

- **Certified:** shared decision metric with a single implementation and cross-page or reconciliation tests.
- **Verified:** governed metric with a declared formula, grain, source, owner, basis, and mart.
- **Diagnostic:** useful supporting signal whose interpretation requires an explicit caveat.
- **Source-gated:** implemented only when the declared source field is populated for the selected scope.
- **Withheld:** definition is documented, but the required source does not exist. The app must render a source requirement rather than a number.

The dbt project parses against the Snowflake adapter in CI. Runtime data remains entirely synthetic.
