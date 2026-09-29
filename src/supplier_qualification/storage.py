"""供应商资格服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS qual_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('qualification_admin','quality','buyer','procurement_lead','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_code TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factories (
    factory_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(supplier_id, name)
);

CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    qual_type TEXT NOT NULL CHECK(qual_type IN ('type_test','iso9001','grid_admission','industry_cert')),
    standard_no TEXT NOT NULL,
    product_category TEXT NOT NULL,
    voltage_level_kv INTEGER NOT NULL,
    scope_text TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_from <= valid_until)
);

CREATE TABLE IF NOT EXISTS approved_products (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    factory_id TEXT NOT NULL REFERENCES factories(factory_id),
    product_code TEXT NOT NULL,
    product_category TEXT NOT NULL,
    qualification_id TEXT NOT NULL REFERENCES qualifications(qualification_id),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','withdrawn')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(supplier_id, factory_id, product_code)
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    title TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_from <= valid_until)
);

CREATE TABLE IF NOT EXISTS contract_scopes (
    scope_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    product_code TEXT NOT NULL,
    factory_id TEXT NOT NULL REFERENCES factories(factory_id),
    UNIQUE(contract_id, product_code, factory_id)
);

CREATE TABLE IF NOT EXISTS quality_events (
    event_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    factory_id TEXT REFERENCES factories(factory_id),
    product_code TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    description TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    closed_on TEXT,
    closure_note TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suspensions (
    suspension_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_code TEXT,
    factory_id TEXT REFERENCES factories(factory_id),
    reason TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','lifted')),
    lifted_on TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    decided_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    order_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    factory_id TEXT NOT NULL REFERENCES factories(factory_id),
    product_code TEXT NOT NULL,
    product_category TEXT NOT NULL,
    required_voltage_kv INTEGER NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    planned_delivery_on TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('cleared','blocked','review_required','shipped','accepted','substituted')),
    substitution_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    shipped_at TEXT,
    accepted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_supplier_state
ON purchase_orders(supplier_id, state);

CREATE TABLE IF NOT EXISTS compliance_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    phase TEXT NOT NULL CHECK(phase IN ('creation','recheck','shipment','acceptance')),
    as_of_date TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass','fail')),
    detail_json TEXT NOT NULL,
    checked_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checks_order
ON compliance_checks(order_id, check_id);

CREATE TABLE IF NOT EXISTS substitutions (
    substitution_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    substitute_supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    substitute_factory_id TEXT NOT NULL REFERENCES factories(factory_id),
    substitute_product_code TEXT NOT NULL,
    quantity TEXT NOT NULL,
    needed_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected','applied','expired')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS substitution_approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    substitution_id TEXT NOT NULL REFERENCES substitutions(substitution_id),
    approval_type TEXT NOT NULL CHECK(approval_type IN ('quality','procurement')),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    approved_quantity TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    note TEXT NOT NULL,
    approver TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(substitution_id, approval_type)
);

CREATE TABLE IF NOT EXISTS qualification_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_type TEXT NOT NULL CHECK(change_type IN (
        'renewal','revocation','suspension','suspension_lifted',
        'quality_event_opened','quality_event_closed'
    )),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    qualification_id TEXT REFERENCES qualifications(qualification_id),
    reference_id TEXT,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_supplier
ON qualification_changes(supplier_id, change_id);

CREATE TABLE IF NOT EXISTS order_impacts (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id INTEGER NOT NULL REFERENCES qualification_changes(change_id),
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    classification TEXT NOT NULL CHECK(classification IN ('review_required','acceptance_pending','retained')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(change_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_impacts_order
ON order_impacts(order_id, impact_id);

CREATE TABLE IF NOT EXISTS qual_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS qual_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qual_audit_entity
ON qual_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
