# Assistant Enterprise QA Matrix (Release Hardening)

Date: 2026-03-11  
Scope: Wholesale Analytics Analytics assistant end-to-end validation for production readiness.

## Coverage Axes
- Role: `admin`, `sales_manager`, `sales`, `production`, `warehouse`
- Page: `overview`, `customers`, `products`, `regions`, `suppliers`, `salesreps`, `returns`
- Intent: summary, definition, ranking, grouped, history, comparison, risk, forecast/trust, export, modify, returns workflow, executive/analyst modes
- Output mode: standard, executive, analyst, simple, export/file
- Failure mode: empty data, permission-limited, async export pending, chart/data sparsity

## Scenario Matrix

| ID | Role | Page | Prompt Pattern | Expected Behavior | Validation |
|---|---|---|---|---|---|
| Q1 | sales | overview | summarize this page | page summary sections + scope/trust | automated |
| Q2 | sales | overview | what is AOV | definition route + glossary/help sections | automated |
| Q3 | sales | overview | top 5 regions by revenue | ranking route + ranked output | automated |
| Q4 | sales | overview | revenue by region | grouped route + grouped output | automated |
| Q5 | sales | overview | compare this page with last year | comparison route + comparison sections | automated |
| Q6 | sales | overview | full history for this page | history route + history sections | automated |
| Q7 | production | suppliers | top customers for this supplier | supplier context applied + ranking route | automated |
| Q8 | sales | customers | top products for this customer | customer context applied | automated |
| Q9 | warehouse | returns | approvals pending + workflow | returns workflow/analytics route + pending evidence | automated |
| Q10 | sales | overview | export this page/workbook | export action with download/status metadata | automated |
| Q11 | sales | overview | async export top N | async status URL + job polling + completion | automated |
| Q12 | sales | overview | chart image as SVG | file export route + svg download action | automated |
| Q13 | sales | overview | include all available columns | permission-safe column policy only | automated |
| Q14 | sales | overview | use full history instead (follow-up) | follow-up rewrite + full history slot | automated |
| Q15 | sales | overview | executive vs analyst mode | section shape differences | automated |
| Q16 | warehouse | overview | business + margin question | permission-limited response, no leakage | automated |
| Q17 | sales | overview | repeated diverse intents | materially different question types/answers | automated |
| Q18 | any | any | tool/provider unavailable path | graceful non-stacktrace fallback | automated |

## Exit Criteria
- No RBAC/scope leakage in responses or export column policy.
- No export action with missing status for actionable export cards.
- Ranking/grouped/history/comparison/definition/returns/export produce distinct routes and section shapes.
- Async exports return stable `pending/running/completed` lifecycle with pollable status endpoints.
- Sparse chart requests degrade cleanly to scoped fallback chart artifacts.
