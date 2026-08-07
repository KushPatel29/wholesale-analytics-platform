import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from flask import Flask
from app.blueprints import regions
from app.services import analytics_utils as au

# Create a minimal app for context
app = Flask(__name__)
app.secret_key = 'test'

def test_analytics_utils_functions():
    # Test calculate_rolling_average
    s = pd.Series([10, 20, 30, 40, 50])
    rolling = au.calculate_rolling_average(s, window=3)
    assert len(rolling) == 5
    assert pd.isna(rolling.iloc[0]) is False # min_periods=1
    assert rolling.iloc[0] == 10.0
    assert rolling.iloc[2] == 20.0  # (10+20+30)/3
    
    # Test calculate_yoy_growth
    assert au.calculate_yoy_growth(120, 100) == 20.0
    assert au.calculate_yoy_growth(100, 100) == 0.0
    assert au.calculate_yoy_growth(80, 100) == -20.0
    assert au.calculate_yoy_growth(100, 0) is None
    assert au.calculate_yoy_growth(100, None) is None

@patch('app.blueprints.regions.get_fact_df')
@patch('app.blueprints.regions.current_user')
@patch('app.blueprints.regions.apply_global_filters')
@patch('app.blueprints.regions.scope_dataframe')
def test_build_drilldown_payload(mock_scope, mock_apply, mock_user, mock_get_df):
    # Setup mock dataframe
    dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
    df = pd.DataFrame({
        'Date': dates,
        'Region': ['North'] * 100,
        'Revenue': np.random.rand(100) * 1000,
        'OrderId': range(100),
        'CustomerId': ['C1'] * 50 + ['C2'] * 50,
        'ProductId': ['P1'] * 100
    })
    
    mock_get_df.return_value = df
    mock_apply.side_effect = lambda d, f: d
    mock_scope.side_effect = lambda d, u: d
    
    mock_user.id = 'test_user'
    mock_user.roles = ['admin']
    mock_user.region_id = None
    
    # Clear cache
    regions._cached_region_overview.cache_clear()
    
    with app.test_request_context():
        # Run drilldown build
        payload = regions._build_drilldown_payload('North')
        
        # Verify structure
        assert 'months' in payload
        assert 'monthly_revenue' in payload
        assert 'kpi' in payload
        
        kpi = payload['kpi']
        assert 'total_revenue' in kpi
        assert 'mom_growth' in kpi
        assert 'wow_growth' in kpi
        
        print(f"MoM: {kpi['mom_growth']}")
        print(f"WoW: {kpi['wow_growth']}")
        
        # Check specific logic
        dates2 = pd.to_datetime(['2023-01-15', '2023-02-15'])
        df2 = pd.DataFrame({
            'Date': dates2,
            'Region': ['South'] * 2,
            'Revenue': [100.0, 120.0], # 20% growth
            'OrderId': [1, 2],
            'CustomerId': ['C1', 'C1'],
            'ProductId': ['P1', 'P1']
        })
        mock_get_df.return_value = df2
        regions._cached_region_overview.cache_clear()
        
        payload2 = regions._build_drilldown_payload('South')
        # Note: 2023-01-15 and 2023-02-15 are in different months.
        # MoM logic compares last month to prev month.
        # resample('M') will create bins for Jan and Feb.
        # Jan total = 100, Feb total = 120.
        # Growth = (120 - 100) / 100 = 0.20 = 20%
        
        assert payload2['kpi']['mom_growth'] == 20.0