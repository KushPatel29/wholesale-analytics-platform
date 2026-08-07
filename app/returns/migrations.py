"""Additive schema migrations for the returns module."""

from __future__ import annotations

import json
from typing import Any, Callable


def apply_returns_migrations(conn: Any, safe_exec: Callable[[Any, str, str], None]) -> None:
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_rmas (
            id INTEGER PRIMARY KEY,
            customer_id VARCHAR(128) NOT NULL,
            customer_name VARCHAR(255),
            customer_email VARCHAR(255),
            customer_phone VARCHAR(64),
            order_id VARCHAR(128) NOT NULL,
            rep_user_id INTEGER,
            rep_name VARCHAR(255),
            date_submitted DATETIME,
            date_shipped DATETIME,
            return_type VARCHAR(64),
            advised_customer BOOLEAN NOT NULL DEFAULT 0,
            additional_notes TEXT,
            status VARCHAR(64) NOT NULL,
            created_by_user_id INTEGER,
            assigned_user_id INTEGER,
            decision_summary VARCHAR(255),
            policy_version VARCHAR(64),
            risk_score DOUBLE NOT NULL DEFAULT 0,
            external_reference VARCHAR(128),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_rmas",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_rma_items (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            order_line_id VARCHAR(128),
            sku VARCHAR(128),
            product_name VARCHAR(255),
            product_code VARCHAR(128),
            product_desc VARCHAR(255),
            price_per_lb DOUBLE NOT NULL DEFAULT 0,
            weight_lb DOUBLE NOT NULL DEFAULT 0,
            credit_pct DOUBLE NOT NULL DEFAULT 100,
            credit_amount DOUBLE NOT NULL DEFAULT 0,
            product_returning BOOLEAN NOT NULL DEFAULT 1,
            reason_for_return VARCHAR(255),
            follow_up_action VARCHAR(255),
            supplier_credit BOOLEAN NOT NULL DEFAULT 0,
            qty DOUBLE NOT NULL DEFAULT 0,
            price DOUBLE NOT NULL DEFAULT 0,
            reason_code VARCHAR(64),
            item_condition VARCHAR(64),
            notes TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_rma_items",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_reason_codes (
            id INTEGER PRIMARY KEY,
            reason_code VARCHAR(64) NOT NULL UNIQUE,
            reason_text VARCHAR(255) NOT NULL,
            category VARCHAR(32) NOT NULL DEFAULT 'Other',
            active BOOLEAN NOT NULL DEFAULT 1
        )
        """,
        "returns.ensure.return_reason_codes",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_settings (
            id INTEGER PRIMARY KEY,
            setting_key VARCHAR(128) NOT NULL UNIQUE,
            setting_value TEXT NOT NULL DEFAULT '{}',
            updated_by_user_id INTEGER,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_settings",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_events (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            from_status VARCHAR(64),
            to_status VARCHAR(64),
            actor_user_id INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_events",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_approvals (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            wh_approved_by INTEGER,
            wh_approved_at DATETIME,
            mgr_approved_by INTEGER,
            mgr_approved_at DATETIME,
            rejected_by INTEGER,
            rejected_at DATETIME,
            reject_reason TEXT
        )
        """,
        "returns.ensure.return_approvals",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_attachments (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            item_id INTEGER,
            file_path TEXT NOT NULL,
            mime VARCHAR(255),
            size INTEGER NOT NULL DEFAULT 0,
            uploaded_by_user_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_attachments",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_comments (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            user_id INTEGER,
            body TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_comments",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_inspections (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            disposition VARCHAR(64),
            notes TEXT,
            photos_json TEXT NOT NULL DEFAULT '[]',
            inspected_by_user_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_inspections",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_shipments (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            carrier VARCHAR(64),
            label_url TEXT,
            tracking_number VARCHAR(128),
            shipping_cost DOUBLE NOT NULL DEFAULT 0,
            status VARCHAR(64),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_shipments",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_refunds (
            id INTEGER PRIMARY KEY,
            rma_id INTEGER NOT NULL,
            amount DOUBLE NOT NULL DEFAULT 0,
            method VARCHAR(64),
            status VARCHAR(64),
            processor_ref VARCHAR(128),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_refunds",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_policy_versions (
            id INTEGER PRIMARY KEY,
            version VARCHAR(64) NOT NULL UNIQUE,
            rules_json TEXT NOT NULL DEFAULT '{}',
            is_active BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "returns.ensure.return_policy_versions",
    )
    safe_exec(
        conn,
        """
        CREATE TABLE IF NOT EXISTS return_webhook_events (
            id INTEGER PRIMARY KEY,
            source VARCHAR(64) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at DATETIME,
            status VARCHAR(64) NOT NULL DEFAULT 'pending'
        )
        """,
        "returns.ensure.return_webhook_events",
    )

    def _table_columns(table_name: str) -> set[str]:
        try:
            return {str(row[1]) for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
        except Exception:
            return set()

    def _add_column(table_name: str, column_name: str, ddl: str) -> None:
        cols = _table_columns(table_name)
        if column_name not in cols:
            safe_exec(conn, ddl, f"returns.alter.{table_name}.{column_name}")

    _add_column("return_rmas", "rep_user_id", "ALTER TABLE return_rmas ADD COLUMN rep_user_id INTEGER")
    _add_column("return_rmas", "rep_name", "ALTER TABLE return_rmas ADD COLUMN rep_name VARCHAR(255)")
    _add_column("return_rmas", "rma_number", "ALTER TABLE return_rmas ADD COLUMN rma_number VARCHAR(64)")
    _add_column("return_rmas", "order_date", "ALTER TABLE return_rmas ADD COLUMN order_date DATE")
    _add_column("return_rmas", "date_submitted", "ALTER TABLE return_rmas ADD COLUMN date_submitted DATETIME")
    _add_column("return_rmas", "date_shipped", "ALTER TABLE return_rmas ADD COLUMN date_shipped DATETIME")
    _add_column("return_rmas", "return_type", "ALTER TABLE return_rmas ADD COLUMN return_type VARCHAR(64)")
    _add_column("return_rmas", "advised_customer", "ALTER TABLE return_rmas ADD COLUMN advised_customer BOOLEAN NOT NULL DEFAULT 0")
    _add_column("return_rmas", "additional_notes", "ALTER TABLE return_rmas ADD COLUMN additional_notes TEXT")
    _add_column("return_rmas", "total_credit_amount", "ALTER TABLE return_rmas ADD COLUMN total_credit_amount DOUBLE NOT NULL DEFAULT 0")
    _add_column("return_rmas", "total_weight_lb", "ALTER TABLE return_rmas ADD COLUMN total_weight_lb DOUBLE NOT NULL DEFAULT 0")
    _add_column("return_rmas", "total_packs", "ALTER TABLE return_rmas ADD COLUMN total_packs INTEGER NOT NULL DEFAULT 0")
    _add_column("return_rmas", "primary_reason", "ALTER TABLE return_rmas ADD COLUMN primary_reason VARCHAR(255)")
    _add_column("return_rmas", "primary_category", "ALTER TABLE return_rmas ADD COLUMN primary_category VARCHAR(64)")
    _add_column("return_rmas", "rec_prod_signoff", "ALTER TABLE return_rmas ADD COLUMN rec_prod_signoff BOOLEAN NOT NULL DEFAULT 0")
    _add_column("return_rmas", "rec_prod_signed_by_user_id", "ALTER TABLE return_rmas ADD COLUMN rec_prod_signed_by_user_id INTEGER")
    _add_column("return_rmas", "rec_prod_signed_at", "ALTER TABLE return_rmas ADD COLUMN rec_prod_signed_at DATETIME")
    _add_column("return_rmas", "wh_approved_by_user_id", "ALTER TABLE return_rmas ADD COLUMN wh_approved_by_user_id INTEGER")
    _add_column("return_rmas", "wh_approved_at", "ALTER TABLE return_rmas ADD COLUMN wh_approved_at DATETIME")
    _add_column("return_rmas", "mgr_approved_by_user_id", "ALTER TABLE return_rmas ADD COLUMN mgr_approved_by_user_id INTEGER")
    _add_column("return_rmas", "mgr_approved_at", "ALTER TABLE return_rmas ADD COLUMN mgr_approved_at DATETIME")
    _add_column("return_rmas", "rejected_by_user_id", "ALTER TABLE return_rmas ADD COLUMN rejected_by_user_id INTEGER")
    _add_column("return_rmas", "rejected_at", "ALTER TABLE return_rmas ADD COLUMN rejected_at DATETIME")
    _add_column("return_rmas", "reject_reason", "ALTER TABLE return_rmas ADD COLUMN reject_reason TEXT")
    _add_column("return_rmas", "company", "ALTER TABLE return_rmas ADD COLUMN company VARCHAR(64)")
    _add_column("return_rmas", "approval_target", "ALTER TABLE return_rmas ADD COLUMN approval_target VARCHAR(64)")
    _add_column("return_rmas", "ops_cleared_at", "ALTER TABLE return_rmas ADD COLUMN ops_cleared_at DATETIME")
    _add_column("return_rmas", "wh_reviewed_at", "ALTER TABLE return_rmas ADD COLUMN wh_reviewed_at DATETIME")
    _add_column("return_rmas", "fin_cleared_at", "ALTER TABLE return_rmas ADD COLUMN fin_cleared_at DATETIME")
    _add_column("return_rma_items", "product_code", "ALTER TABLE return_rma_items ADD COLUMN product_code VARCHAR(128)")
    _add_column("return_rma_items", "product_desc", "ALTER TABLE return_rma_items ADD COLUMN product_desc VARCHAR(255)")
    _add_column("return_rma_items", "price_per_lb", "ALTER TABLE return_rma_items ADD COLUMN price_per_lb DOUBLE NOT NULL DEFAULT 0")
    _add_column("return_rma_items", "weight_lb", "ALTER TABLE return_rma_items ADD COLUMN weight_lb DOUBLE NOT NULL DEFAULT 0")
    _add_column("return_rma_items", "credit_pct", "ALTER TABLE return_rma_items ADD COLUMN credit_pct DOUBLE NOT NULL DEFAULT 100")
    _add_column("return_rma_items", "credit_amount", "ALTER TABLE return_rma_items ADD COLUMN credit_amount DOUBLE NOT NULL DEFAULT 0")
    _add_column("return_rma_items", "product_returning", "ALTER TABLE return_rma_items ADD COLUMN product_returning BOOLEAN NOT NULL DEFAULT 1")
    _add_column("return_rma_items", "packs_count", "ALTER TABLE return_rma_items ADD COLUMN packs_count INTEGER NOT NULL DEFAULT 0")
    _add_column("return_rma_items", "pack_barcode", "ALTER TABLE return_rma_items ADD COLUMN pack_barcode VARCHAR(255)")
    _add_column("return_rma_items", "reason_for_return", "ALTER TABLE return_rma_items ADD COLUMN reason_for_return VARCHAR(255)")
    _add_column("return_rma_items", "follow_up_action", "ALTER TABLE return_rma_items ADD COLUMN follow_up_action VARCHAR(255)")
    _add_column("return_rma_items", "category", "ALTER TABLE return_rma_items ADD COLUMN category VARCHAR(64)")
    _add_column("return_rma_items", "supplier_credit", "ALTER TABLE return_rma_items ADD COLUMN supplier_credit BOOLEAN NOT NULL DEFAULT 0")
    _add_column("return_rma_items", "receiving_notes", "ALTER TABLE return_rma_items ADD COLUMN receiving_notes TEXT")
    _add_column("return_rma_items", "warehouse_outcome", "ALTER TABLE return_rma_items ADD COLUMN warehouse_outcome VARCHAR(128)")
    _add_column("return_rma_items", "received_weight_lb", "ALTER TABLE return_rma_items ADD COLUMN received_weight_lb DOUBLE")
    _add_column("return_attachments", "filename", "ALTER TABLE return_attachments ADD COLUMN filename VARCHAR(255)")
    _add_column("return_attachments", "mimetype", "ALTER TABLE return_attachments ADD COLUMN mimetype VARCHAR(255)")
    _add_column("return_attachments", "storage_path", "ALTER TABLE return_attachments ADD COLUMN storage_path TEXT")
    _add_column("return_attachments", "uploaded_at", "ALTER TABLE return_attachments ADD COLUMN uploaded_at DATETIME")
    _add_column("return_events", "field_name", "ALTER TABLE return_events ADD COLUMN field_name VARCHAR(128)")
    _add_column("return_events", "old_value", "ALTER TABLE return_events ADD COLUMN old_value TEXT")
    _add_column("return_events", "new_value", "ALTER TABLE return_events ADD COLUMN new_value TEXT")
    safe_exec(conn, "UPDATE return_rma_items SET credit_pct = COALESCE(credit_pct, 100)", "returns.backfill.return_rma_items.credit_pct")

    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_rmas_rma_number ON return_rmas(rma_number)", "returns.index.return_rmas_rma_number")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_order_id ON return_rmas(order_id)", "returns.index.return_rmas_order_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_customer_id ON return_rmas(customer_id)", "returns.index.return_rmas_customer_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_status_date_submitted ON return_rmas(status, date_submitted)", "returns.index.return_rmas_status_date_submitted")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_date_submitted ON return_rmas(date_submitted)", "returns.index.return_rmas_date_submitted")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_status_created ON return_rmas(status, created_at)", "returns.index.return_rmas_status_created")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rmas_rep_user_id ON return_rmas(rep_user_id)", "returns.index.return_rmas_rep_user_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rma_items_rma_id ON return_rma_items(rma_id)", "returns.index.return_rma_items_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rma_items_product_code ON return_rma_items(product_code)", "returns.index.return_rma_items_product_code")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_rma_items_reason_code ON return_rma_items(reason_code)", "returns.index.return_rma_items_reason_code")
    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_reason_codes_reason_code ON return_reason_codes(reason_code)", "returns.index.return_reason_codes_reason_code")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_reason_codes_active ON return_reason_codes(active)", "returns.index.return_reason_codes_active")
    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_settings_key ON return_settings(setting_key)", "returns.index.return_settings_key")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_events_rma_created ON return_events(rma_id, created_at)", "returns.index.return_events_rma_created")
    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_approvals_rma_id ON return_approvals(rma_id)", "returns.index.return_approvals_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_attachments_rma_id ON return_attachments(rma_id)", "returns.index.return_attachments_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_comments_rma_id ON return_comments(rma_id)", "returns.index.return_comments_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_inspections_rma_id ON return_inspections(rma_id)", "returns.index.return_inspections_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_shipments_rma_id ON return_shipments(rma_id)", "returns.index.return_shipments_rma_id")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_refunds_rma_id ON return_refunds(rma_id)", "returns.index.return_refunds_rma_id")
    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_policy_versions_version ON return_policy_versions(version)", "returns.index.return_policy_versions_version")
    safe_exec(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_return_webhook_events_idempotency ON return_webhook_events(idempotency_key)", "returns.index.return_webhook_events_idempotency")
    safe_exec(conn, "CREATE INDEX IF NOT EXISTS ix_return_webhook_events_status ON return_webhook_events(status)", "returns.index.return_webhook_events_status")

    default_rules = {
        "auto_approve_reason_codes": ["damaged", "wrong_item"],
        "evidence_required_reason_codes": ["damaged", "quality_issue"],
        "auto_review_risk_threshold": 0.35,
        "default_disposition": "refund",
    }
    payload = json.dumps(default_rules, separators=(",", ":"), sort_keys=True).replace("'", "''")
    safe_exec(
        conn,
        f"""
        INSERT INTO return_policy_versions (version, rules_json, is_active)
        SELECT '1', '{payload}', 1
        WHERE NOT EXISTS (
            SELECT 1 FROM return_policy_versions WHERE version = '1'
        )
        """,
        "returns.seed.default_policy",
    )
    safe_exec(
        conn,
        """
        UPDATE return_policy_versions
        SET is_active = CASE WHEN version = (
            SELECT version
            FROM return_policy_versions
            ORDER BY CASE WHEN is_active = 1 THEN 0 ELSE 1 END, created_at DESC, id DESC
            LIMIT 1
        ) THEN 1 ELSE 0 END
        WHERE EXISTS (SELECT 1 FROM return_policy_versions)
        """,
        "returns.seed.single_active_policy",
    )
    for reason_code, reason_text, category in (
        ("quality_issue", "Quality Issue", "Production"),
        ("damaged", "Damaged", "Warehouse"),
        ("wrong_item", "Wrong Item", "Sales"),
        ("customer_return", "Customer Return", "Sales"),
        ("short_issue", "Short / Issue", "Warehouse"),
        ("vendor_return", "Vendor Return", "Other"),
    ):
        safe_exec(
            conn,
            f"""
            INSERT INTO return_reason_codes (reason_code, reason_text, category, active)
            SELECT '{reason_code}', '{reason_text}', '{category}', 1
            WHERE NOT EXISTS (
                SELECT 1 FROM return_reason_codes WHERE reason_code = '{reason_code}'
            )
            """,
            f"returns.seed.reason_code.{reason_code}",
        )
    default_settings = {
        "defaults": {
            "return_type": "Sales Return",
            "follow_up_action": "Credit",
        },
        "email_templates": {
            "new_return_subject": "New return pending review for order {{ order_id }}",
            "manager_approval_subject": "Return #{{ rma_id }} approved",
            "rejection_subject": "Return #{{ rma_id }} rejected",
        },
    }
    settings_payload = json.dumps(default_settings, separators=(",", ":"), sort_keys=True).replace("'", "''")
    safe_exec(
        conn,
        f"""
        INSERT INTO return_settings (setting_key, setting_value)
        SELECT 'returns', '{settings_payload}'
        WHERE NOT EXISTS (
            SELECT 1 FROM return_settings WHERE setting_key = 'returns'
        )
        """,
        "returns.seed.settings",
    )
