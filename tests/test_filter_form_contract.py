from datetime import date

import pandas as pd

from app.core.filters import build_global_filter_form


def test_build_global_filter_form_supports_statuses_and_shipping_methods(app):
    df = pd.DataFrame(
        [
            {
                "Date": date(2024, 1, 1),
                "OrderStatus": "Open",
                "Region": "West",
                "CustomerName": "Atlas",
                "Supplier_Name": "Prairie",
                "Name": "Ribeye",
                "ShipMethod_Name": "Ground",
            },
            {
                "Date": date(2024, 1, 2),
                "OrderStatus": "Closed",
                "Region": "East",
                "CustomerName": "Beacon",
                "Supplier_Name": "River",
                "Name": "Striploin",
                "ShipMethod_Name": "Air",
            },
        ]
    )

    with app.test_request_context("/"):
        form = build_global_filter_form(
            df,
            data={
                "statuses": ["Open"],
                "regions": ["West"],
                "shipping_methods": ["Ground"],
                "products": ["Ribeye"],
            },
        )

        assert form.statuses.data == ["Open"]
        assert ("Open", "Open") in form.statuses.choices
        assert form.shipping_methods.data == ["Ground"]
        assert ("Ground", "Ground") in form.shipping_methods.choices
        assert form.products.data == ["Ribeye"]


def test_build_global_filter_form_keeps_selected_values_even_without_dataset_choices(app):
    with app.test_request_context("/"):
        form = build_global_filter_form(
            None,
            data={
                "statuses": ["Pending"],
                "shipping_methods": ["Courier"],
                "customers": ["Key Account"],
            },
        )

        assert ("Pending", "Pending") in form.statuses.choices
        assert form.statuses.data == ["Pending"]
        assert ("Courier", "Courier") in form.shipping_methods.choices
        assert form.shipping_methods.data == ["Courier"]
        assert ("Key Account", "Key Account") in form.customers.choices
