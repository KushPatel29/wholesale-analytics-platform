-- Overview validation SQL (DuckDB)
-- Replace {{start}} and {{end}} before execution.
-- Canonical date for Overview: Date.

WITH current_window AS (
    SELECT *
    FROM fact
    WHERE CAST(Date AS DATE) >= DATE '{{start}}'
      AND CAST(Date AS DATE) <= DATE '{{end}}'
),
prior_month_window AS (
    SELECT *
    FROM fact
    WHERE CAST(Date AS DATE) >= DATE '{{start}}' - INTERVAL '1 month'
      AND CAST(Date AS DATE) <= DATE '{{end}}' - INTERVAL '1 month'
),
prior_year_window AS (
    SELECT *
    FROM fact
    WHERE CAST(Date AS DATE) >= DATE '{{start}}' - INTERVAL '12 month'
      AND CAST(Date AS DATE) <= DATE '{{end}}' - INTERVAL '12 month'
),
current_totals AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(CASE WHEN Cost IS NOT NULL THEN COALESCE(Cost, 0) ELSE 0 END) AS cost,
        SUM(CASE WHEN Cost IS NOT NULL THEN COALESCE(Revenue, 0) ELSE 0 END) AS revenue_with_cost,
        SUM(COALESCE(QuantityShipped, 0)) AS qty,
        COUNT(DISTINCT OrderId) AS orders,
        COUNT(DISTINCT CustomerId) AS customers
    FROM current_window
),
prior_month_totals AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(COALESCE(QuantityShipped, 0)) AS qty
    FROM prior_month_window
),
prior_year_totals AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(COALESCE(QuantityShipped, 0)) AS qty
    FROM prior_year_window
)
SELECT
    c.revenue AS revenue_current,
    c.cost AS cost_current,
    c.revenue_with_cost - c.cost AS profit_current,
    CASE WHEN c.revenue_with_cost > 0
        THEN (c.revenue_with_cost - c.cost) / c.revenue_with_cost
        ELSE NULL
    END AS margin_current,
    CASE WHEN c.revenue > 0 THEN c.revenue / NULLIF(c.orders, 0) ELSE NULL END AS aov_current,
    CASE WHEN c.qty > 0 THEN c.revenue / c.qty ELSE NULL END AS asp_current,
    c.orders AS orders_current,
    c.customers AS customers_current,
    pm.revenue AS revenue_prior_month,
    py.revenue AS revenue_prior_year,
    c.revenue - pm.revenue AS delta_mom,
    CASE WHEN ABS(pm.revenue) > 0
        THEN (c.revenue - pm.revenue) / ABS(pm.revenue)
        ELSE NULL
    END AS delta_mom_pct,
    c.revenue - py.revenue AS delta_yoy,
    CASE WHEN ABS(py.revenue) > 0
        THEN (c.revenue - py.revenue) / ABS(py.revenue)
        ELSE NULL
    END AS delta_yoy_pct
FROM current_totals c
CROSS JOIN prior_month_totals pm
CROSS JOIN prior_year_totals py;

-- New vs Returning customers (window-over-window).
SELECT
    COUNT(*) FILTER (WHERE prev.customer_id IS NULL) AS new_customers,
    COUNT(*) FILTER (WHERE prev.customer_id IS NOT NULL) AS returning_customers
FROM (
    SELECT DISTINCT CAST(CustomerId AS VARCHAR) AS customer_id FROM current_window WHERE CustomerId IS NOT NULL
) curr
LEFT JOIN (
    SELECT DISTINCT CAST(CustomerId AS VARCHAR) AS customer_id FROM prior_month_window WHERE CustomerId IS NOT NULL
) prev
ON curr.customer_id = prev.customer_id;

-- Concentration / HHI (customer, current window).
WITH customer_rev AS (
    SELECT CAST(CustomerId AS VARCHAR) AS customer_id, SUM(COALESCE(Revenue, 0)) AS revenue
    FROM current_window
    WHERE CustomerId IS NOT NULL
    GROUP BY 1
),
ranked AS (
    SELECT
        customer_id,
        revenue,
        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
        SUM(revenue) OVER () AS total_rev
    FROM customer_rev
)
SELECT
    MAX(CASE WHEN total_rev > 0 THEN revenue / total_rev END) * 100 AS top1_share,
    SUM(CASE WHEN rn <= 5 AND total_rev > 0 THEN revenue / total_rev ELSE 0 END) * 100 AS top5_share,
    SUM(CASE WHEN total_rev > 0 THEN POWER(revenue / total_rev, 2) ELSE 0 END) * 10000 AS hhi
FROM ranked;

-- Price / Volume / Mix decomposition (revenue).
WITH curr AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(COALESCE(QuantityShipped, 0)) AS qty
    FROM current_window
),
pm AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(COALESCE(QuantityShipped, 0)) AS qty
    FROM prior_month_window
),
py AS (
    SELECT
        SUM(COALESCE(Revenue, 0)) AS revenue,
        SUM(COALESCE(QuantityShipped, 0)) AS qty
    FROM prior_year_window
)
SELECT
    curr.revenue - pm.revenue AS mom_total,
    CASE WHEN curr.qty > 0 AND pm.qty > 0 THEN ((curr.revenue / curr.qty) - (pm.revenue / pm.qty)) * curr.qty ELSE NULL END AS mom_price,
    CASE WHEN curr.qty > 0 AND pm.qty > 0 THEN (curr.qty - pm.qty) * (pm.revenue / pm.qty) ELSE NULL END AS mom_volume,
    CASE
        WHEN curr.revenue IS NOT NULL AND pm.revenue IS NOT NULL THEN
            (curr.revenue - pm.revenue)
            - COALESCE(CASE WHEN curr.qty > 0 AND pm.qty > 0 THEN ((curr.revenue / curr.qty) - (pm.revenue / pm.qty)) * curr.qty ELSE NULL END, 0)
            - COALESCE(CASE WHEN curr.qty > 0 AND pm.qty > 0 THEN (curr.qty - pm.qty) * (pm.revenue / pm.qty) ELSE NULL END, 0)
        ELSE NULL
    END AS mom_mix,
    curr.revenue - py.revenue AS yoy_total,
    CASE WHEN curr.qty > 0 AND py.qty > 0 THEN ((curr.revenue / curr.qty) - (py.revenue / py.qty)) * curr.qty ELSE NULL END AS yoy_price,
    CASE WHEN curr.qty > 0 AND py.qty > 0 THEN (curr.qty - py.qty) * (py.revenue / py.qty) ELSE NULL END AS yoy_volume,
    CASE
        WHEN curr.revenue IS NOT NULL AND py.revenue IS NOT NULL THEN
            (curr.revenue - py.revenue)
            - COALESCE(CASE WHEN curr.qty > 0 AND py.qty > 0 THEN ((curr.revenue / curr.qty) - (py.revenue / py.qty)) * curr.qty ELSE NULL END, 0)
            - COALESCE(CASE WHEN curr.qty > 0 AND py.qty > 0 THEN (curr.qty - py.qty) * (py.revenue / py.qty) ELSE NULL END, 0)
        ELSE NULL
    END AS yoy_mix
FROM curr
CROSS JOIN pm
CROSS JOIN py;

-- Movers duplicate sanity check (customer labels).
WITH movers_window AS (
    SELECT *
    FROM fact
    WHERE CAST(Date AS DATE) >= DATE '{{start}}' - INTERVAL '1 month'
      AND CAST(Date AS DATE) <= DATE '{{end}}'
),
customer_monthly AS (
    SELECT
        COALESCE(NULLIF(CustomerName, ''), CAST(CustomerId AS VARCHAR), 'Unknown') AS label,
        CAST(date_trunc('month', CAST(Date AS DATE)) AS DATE) AS month,
        SUM(COALESCE(Revenue, 0)) AS revenue
    FROM movers_window
    GROUP BY 1, 2
),
periods AS (
    SELECT
        CAST(date_trunc('month', DATE '{{end}}') AS DATE) AS curr_month,
        CAST(date_trunc('month', DATE '{{end}}' - INTERVAL '1 month') AS DATE) AS prev_month
),
customer_movers AS (
    SELECT
        label,
        SUM(CASE WHEN month = (SELECT curr_month FROM periods) THEN revenue ELSE 0 END) AS current,
        SUM(CASE WHEN month = (SELECT prev_month FROM periods) THEN revenue ELSE 0 END) AS previous
    FROM customer_monthly
    GROUP BY 1
)
SELECT
    COUNT(*) AS rows_total,
    COUNT(DISTINCT label) AS distinct_labels
FROM customer_movers;
