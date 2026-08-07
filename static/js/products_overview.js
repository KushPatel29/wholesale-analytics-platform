document.addEventListener('DOMContentLoaded', function () {
    const pageContainer = document.getElementById('productsOverviewPage');
    if (!pageContainer) return;

    const apiSummaryUrl = pageContainer.dataset.apiSummary;
    const exportUrl = pageContainer.dataset.exportUrl;
    const fetchMode = pageContainer.dataset.fetchMode;
    const overviewAlerts = document.getElementById('overviewAlerts');

    const kpiRevenueValue = document.getElementById('kpiRevenueValue');
    const kpiQuantityValue = document.getElementById('kpiQuantityValue');
    const kpiAvgUnitsOrder = document.getElementById('kpiAvgUnitsOrder');
    const kpiWeightValue = document.getElementById('kpiWeightValue');
    const kpiAvgWeightOrder = document.getElementById('kpiAvgWeightOrder');
    const kpiUniqueProductsValue = document.getElementById('kpiUniqueProductsValue');
    const kpiAvgUnitPriceValue = document.getElementById('kpiAvgUnitPriceValue');
    const kpiMedianUnitPrice = document.getElementById('kpiMedianUnitPrice');
    const kpiAvgMarginPctValue = document.getElementById('kpiAvgMarginPctValue');
    const kpiTotalProfit = document.getElementById('kpiTotalProfit');
    const topProductsTableBody = document.getElementById('topProductsTableBody');

    // Chart instances
    var chartRevenueTrend = echarts.init(document.getElementById('chartRevenueTrend'));
    var chartPriceDistribution = echarts.init(document.getElementById('chartPriceDistribution'));

    function formatCurrency(value) {
        if (value === null || value === undefined) return '';
        return '$' + parseFloat(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function formatIntComma(value) {
        if (value === null || value === undefined) return '';
        return parseInt(value).toLocaleString();
    }

    function formatPercent(value, decimals = 1) {
        if (value === null || value === undefined) return '';
        return parseFloat(value).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + '%';
    }

    function showWarning(message) {
        if (!overviewAlerts) return;
        if (message) {
            overviewAlerts.textContent = message;
            overviewAlerts.classList.remove('d-none');
            overviewAlerts.classList.remove('alert-danger');
            overviewAlerts.classList.add('alert-warning');
        } else {
            overviewAlerts.textContent = '';
            overviewAlerts.classList.add('d-none');
        }
    }

    function updateUI(data) {
        showWarning(data.warning);
        // Update KPIs
        if (kpiRevenueValue) kpiRevenueValue.textContent = formatCurrency(data.kpis.total_revenue);
        if (kpiQuantityValue) kpiQuantityValue.textContent = formatIntComma(data.kpis.total_qty);
        if (kpiAvgUnitsOrder) kpiAvgUnitsOrder.textContent = formatIntComma(data.kpis.avg_qty_per_product);
        if (kpiWeightValue) kpiWeightValue.textContent = formatIntComma(data.kpis.total_weight);
        if (kpiAvgWeightOrder) kpiAvgWeightOrder.textContent = formatIntComma(data.kpis.avg_weight_per_order); // Assuming this KPI exists
        if (kpiUniqueProductsValue) kpiUniqueProductsValue.textContent = formatIntComma(data.kpis.unique_products);
        if (kpiAvgUnitPriceValue) kpiAvgUnitPriceValue.textContent = formatCurrency(data.kpis.avg_unit_price);
        if (kpiMedianUnitPrice) kpiMedianUnitPrice.textContent = formatCurrency(data.kpis.median_unit_price);
        if (kpiAvgMarginPctValue) kpiAvgMarginPctValue.textContent = formatPercent(data.kpis.avg_margin_pct || 0); // Assuming avg_margin is in percent
        if (kpiTotalProfit) kpiTotalProfit.textContent = formatCurrency(data.kpis.total_profit); // Assuming total_profit exists

        // Update Revenue Trend Chart
        if (chartRevenueTrend && data.trend) {
            const trendOptions = {
                xAxis: {
                    data: data.trend.labels
                },
                series: [{
                    data: data.trend.revenue
                }]
            };
            chartRevenueTrend.setOption(trendOptions);
        }

        // Update Price Distribution Chart
        if (chartPriceDistribution && data.price_dist) {
            const priceDistOptions = {
                series: [{
                    data: data.price_dist.prices
                }]
            };
            chartPriceDistribution.setOption(priceDistOptions);
        }

        // Update Top Products Table
        if (topProductsTableBody) {
            topProductsTableBody.innerHTML = ''; // Clear existing rows
            if (data.top_products && data.top_products.length > 0) {
                data.top_products.forEach(product => {
                    const row = topProductsTableBody.insertRow();
                    row.insertCell().innerHTML = `<a href="/products/${product.product_id}">${product.sku}</a>`;
                    row.insertCell().textContent = product.desc;
                    row.insertCell().textContent = product.category || 'N/A';
                    row.insertCell().textContent = product.supplier || 'N/A';
                    row.insertCell().textContent = formatCurrency(product.revenue);
                    row.insertCell().textContent = formatPercent(product.revenue_share, 2);
                    row.insertCell().textContent = formatIntComma(product.qty);
                    row.insertCell().textContent = formatCurrency(product.avg_price);
                });
            } else {
                const row = topProductsTableBody.insertRow();
                row.insertCell(0).colSpan = 8;
                row.cells[0].textContent = 'No top products found.';
                row.cells[0].classList.add('text-center');
            }
        }
    }

    function fetchData() {
        // Show loading indicators
        // ... (can add spinners here)

        const filterForm = document.getElementById('globalFilterForm');
        const formData = new FormData(filterForm);
        const params = new URLSearchParams(formData);

        fetch(`${apiSummaryUrl}?${params.toString()}`)
            .then(response => response.json())
            .then(data => {
                updateUI(data);
                // Hide loading indicators
            })
            .catch(error => {
                console.error('Error fetching products summary:', error);
                showWarning('Unable to load products summary. Please verify the products parquet exists.');
            });
    }

    // Initial data load
    if (fetchMode === 'dynamic') {
        fetchData();
    }

    // Handle filter form submission
    const filterForm = document.getElementById('globalFilterForm');
    if (filterForm) {
        filterForm.addEventListener('submit', function (event) {
            event.preventDefault();
            fetchData();
        });
    }

    // Handle refresh button click
    const refreshButton = document.getElementById('refreshOverviewBtn');
    if (refreshButton) {
        refreshButton.addEventListener('click', function() {
            fetchData();
        });
    }

    // Handle export button click (basic implementation)
    const exportButton = document.getElementById('exportOverviewBtn');
    if (exportButton) {
        exportButton.addEventListener('click', function() {
            const filterForm = document.getElementById('globalFilterForm');
            const formData = new FormData(filterForm);
            const params = new URLSearchParams(formData);
            window.location.href = `${exportUrl}?${params.toString()}`;
        });
    }

    // Basic chart resizing
    window.addEventListener('resize', function() {
        if (chartRevenueTrend) chartRevenueTrend.resize();
        if (chartPriceDistribution) chartPriceDistribution.resize();
    });
});
