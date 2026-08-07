from __future__ import annotations
import pytest
import pandas as pd
from datetime import datetime, timezone
from app.returns import service
from app.returns.models import ReturnRMA, ReturnRMAItem, ReturnEvent, get_session

@pytest.fixture(autouse=True)
def enterprise_config(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_V2=True,
        RETURNS_FINAL_V1=True,
    )
    # Bypass customer scoping for service layer tests
    monkeypatch.setattr(service, "can_access_customer", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_send_rma_bulk_email", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_send_return_event_email", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_send_finance_notification_email", lambda *args, **kwargs: True)

def _make_rma_payload(order_id="ORD-ENT-1", company="Wholesale Provisions"):
    return {
        "order_id": order_id,
        "customer_id": "C-ENT-1",
        "customer_name": "Enterprise Customer",
        "company": company,
        "date_submitted": datetime.now(timezone.utc).isoformat(),
        "return_type": "Sales Return",
        "advised_customer": True,
        "advised_customer_provided": True,
        "items": [
            {
                "sku": "SKU-1",
                "product_code": "SKU-1",
                "product_name": "Item 1",
                "product_desc": "Item 1 Description",
                "price_per_lb": 10.0,
                "weight_lb": 5.0,
                "product_returning": True,
                "product_returning_provided": True,
                "reason_for_return": "damaged",
                "follow_up_action": "Credit",
                "supplier_credit": False,
                "supplier_credit_provided": True
            }
        ]
    }

def test_enterprise_rma_creation_initial_status(app):
    """Test that a new enterprise RMA starts in awaiting_ops."""
    with app.app_context():
        payload = _make_rma_payload()
        result = service.create_rma(order_payload=payload, item_payloads=payload["items"])
        assert result["status"] == "awaiting_ops"
        assert result["company"] == "Wholesale Provisions"

def test_enterprise_sla_timestamps(app):
    """Test that SLA timestamps are set during transitions."""
    with app.app_context():
        payload = _make_rma_payload()
        rma = service.create_rma(order_payload=payload, item_payloads=payload["items"])
        rma_id = rma["id"]
        
        # 1. Schedule Pickup -> ops_cleared_at
        service.schedule_pickup(rma_id)
        with get_session() as session:
            row = session.get(ReturnRMA, rma_id)
            assert row.ops_cleared_at is not None
            assert row.status == "pickup_scheduled"
            
        # 2. Mark Picked Up
        service.mark_picked_up(rma_id)
        
        # 3. WH Approve -> wh_reviewed_at
        service.update_receiving_review(rma_id, item_updates=[{
            "item_id": rma["items"][0]["id"],
            "received_weight_lb": 5.0,
            "warehouse_outcome": "Returning to Inventory",
            "packs_count": 1,
            "follow_up_action": "Credit"
        }])
        service.approve_warehouse(rma_id)
        
        with get_session() as session:
            row = session.get(ReturnRMA, rma_id)
            assert row.wh_reviewed_at is not None
            assert row.status == "wh_approved"

def test_enterprise_audit_logging(app):
    """Test that granular field changes are recorded in ReturnEvent."""
    with app.app_context():
        payload = _make_rma_payload()
        rma = service.create_rma(order_payload=payload, item_payloads=payload["items"])
        rma_id = rma["id"]
        item_id = rma["items"][0]["id"]
        
        # Update receiving with a weight change
        service.update_receiving_review(rma_id, item_updates=[{
            "item_id": item_id,
            "received_weight_lb": 4.5, # Changed from 5.0
            "warehouse_outcome": "Spoilage",
            "packs_count": 1,
            "follow_up_action": "Credit"
        }])
        
        with get_session() as session:
            events = session.query(ReturnEvent).filter(
                ReturnEvent.rma_id == rma_id,
                ReturnEvent.event_type == "field_changed"
            ).all()
            
            # Should have events for weight and outcome
            field_names = [e.field_name for e in events]
            assert f"item_{item_id}_weight_lb" in field_names
            assert f"item_{item_id}_outcome" in field_names
            
            weight_event = next(e for e in events if e.field_name == f"item_{item_id}_weight_lb")
            assert weight_event.new_value == "4.5"

def test_enterprise_routing_logic(app):
    """Test the Scott vs Brian vs Kyle routing rules."""
    with app.app_context():
        # Rule A: Not returning -> Scott
        payload_scott = _make_rma_payload()
        payload_scott["items"][0]["product_returning"] = False
        rma_scott = service.create_rma(order_payload=payload_scott, item_payloads=payload_scott["items"])
        service.update_receiving_review(rma_scott["id"], item_updates=[{
            "item_id": rma_scott["items"][0]["id"],
            "packs_count": 1,
            "follow_up_action": "Credit"
        }])
        service.approve_warehouse(rma_scott["id"])
        with get_session() as session:
            assert session.get(ReturnRMA, rma_scott["id"]).approval_target == "Scott"
            
        # Rule B: Returning and < $300 -> Brian
        payload_brian = _make_rma_payload()
        rma_brian = service.create_rma(order_payload=payload_brian, item_payloads=payload_brian["items"])
        service.update_receiving_review(rma_brian["id"], item_updates=[{
            "item_id": rma_brian["items"][0]["id"],
            "packs_count": 1,
            "follow_up_action": "Credit"
        }])
        service.approve_warehouse(rma_brian["id"])
        with get_session() as session:
            assert session.get(ReturnRMA, rma_brian["id"]).approval_target == "Brian"
            
        # Rule C: Returning and >= $300 -> Kyle
        payload_kyle = _make_rma_payload()
        payload_kyle["items"][0]["weight_lb"] = 40.0
        rma_kyle = service.create_rma(order_payload=payload_kyle, item_payloads=payload_kyle["items"])
        service.update_receiving_review(rma_kyle["id"], item_updates=[{
            "item_id": rma_kyle["items"][0]["id"],
            "received_weight_lb": 40.0,
            "packs_count": 1,
            "follow_up_action": "Credit"
        }])
        service.approve_warehouse(rma_kyle["id"])
        with get_session() as session:
            assert session.get(ReturnRMA, rma_kyle["id"]).approval_target == "Kyle"

def test_enterprise_bulk_export(app):
    """Test the multi-RMA Sage CSV export."""
    with app.app_context():
        p1 = _make_rma_payload("ORD-B1")
        rma1 = service.create_rma(order_payload=p1, item_payloads=p1["items"])
        p2 = _make_rma_payload("ORD-B2")
        rma2 = service.create_rma(order_payload=p2, item_payloads=p2["items"])
        
        csv_bytes = service.export_sage_csv([rma1["id"], rma2["id"]])
        assert len(csv_bytes) > 0
        content = csv_bytes.decode("utf-8")
        assert "ORD-B1" in content
        assert "ORD-B2" in content
        assert "RMA Number" in content
