import pandas as pd
rows=[]
start = pd.Timestamp('2023-10-01')
skip = pd.Timestamp('2024-02-01')
for idx in range(12):
    month_start = start + pd.DateOffset(months=idx)
    if month_start == skip:
        continue
    sale_date = month_start + pd.DateOffset(days=10)
    for product_code in ('A','B'):
        if idx >= 9:
            revenue = 1200.0 if product_code == 'A' else 2000.0
        elif idx >= 6:
            revenue = 800.0 if product_code == 'A' else 1000.0
        else:
            revenue = 600.0 if product_code == 'A' else 700.0
        cost_ratio = 0.7 if product_code == 'A' else 0.45
        cost = revenue * cost_ratio
        qty = revenue / 12.0
        rows.append({'Date': sale_date, 'Revenue': revenue, 'Cost': cost, 'QuantityShipped': qty})
df = pd.DataFrame(rows)
rev_sum = df['Revenue'].sum()
margin_sum = (df['Revenue'] - df['Cost']).sum()
print('rev_sum', rev_sum)
print('margin_sum', margin_sum)
print('rounded_margin', round(margin_sum,2))
print('expected_pct', round(round(margin_sum,2) / rev_sum * 100.0, 2))
