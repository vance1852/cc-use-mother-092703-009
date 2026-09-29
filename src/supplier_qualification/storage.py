"""供应商资格服务的 SQLite 模式与事务辅助。

时间规则（时点资格原则）：
- 订单发货时把当时适用的资格版本（资质/暂停/替代批准）快照到订单行；
- 订单验收后即冻结，资质续期、暂停或整改关闭不再回溯已验收订单；
- 未发货订单在资格变化时标记 review_required，由质量/采购复核后才能继续。
"""

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
    role TEXT NOT NULL CHECK(role IN ('qual_engineer','quality','buyer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 企业资质（如 ISO 9001、特种产品生产许可、入网资质），带有效范围与有效期
CREATE TABLE IF NOT EXISTS certificates (
    certificate_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    cert_type TEXT NOT NULL,
    cert_name TEXT NOT NULL,
    scope_text TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','expired','revoked','renewed')),
    supersedes_certificate_id TEXT REFERENCES certificates(certificate_id),
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_until > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_cert_supplier ON certificates(supplier_id, status);

-- 获准产品（资质范围在物料维度的落地，可限定到指定工厂与规格）
CREATE TABLE IF NOT EXISTS approved_products (
    approval_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    material_code TEXT NOT NULL,
    material_name TEXT NOT NULL,
    plant_id TEXT,
    spec_revision TEXT,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
    certificate_id TEXT REFERENCES certificates(certificate_id),
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_until > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_approved_product_scope
ON approved_products(supplier_id, material_code, COALESCE(plant_id, ''));

CREATE INDEX IF NOT EXISTS idx_approved_product_lookup
ON approved_products(material_code, status, valid_until);

-- 工厂
CREATE TABLE IF NOT EXISTS plants (
    plant_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','closed')),
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plants_supplier ON plants(supplier_id, status);

-- 质量事件
CREATE TABLE IF NOT EXISTS quality_events (
    event_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    material_code TEXT,
    plant_id TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','correcting','closed')),
    corrective_action TEXT,
    closed_by TEXT REFERENCES qual_users(user_id),
    closed_at TEXT,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_events_supplier ON quality_events(supplier_id, state);

-- 暂停决定（针对供应商，可限定到物料/工厂范围；解除即恢复）
CREATE TABLE IF NOT EXISTS suspensions (
    suspension_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    material_code TEXT,
    plant_id TEXT,
    reason_event_id TEXT REFERENCES quality_events(event_id),
    reason_text TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    lifted_at TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','lifted')),
    decided_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_suspensions_scope
ON suspensions(supplier_id, state, material_code, plant_id);

-- 采购订单（采购方案）。open/intransit 为未发货口径，received 为已完成验收
CREATE TABLE IF NOT EXISTS purchase_orders (
    order_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    material_code TEXT NOT NULL,
    plant_id TEXT,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    expect_delivery_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK(state IN ('open','intransit','received','cancelled')),
    -- 发货时固化的合规依据快照；验收后按当时规则保留，不随后续资格变化改写
    qualification_snapshot_json TEXT,
    shipped_at TEXT,
    accepted_at TEXT,
    review_required INTEGER NOT NULL DEFAULT 0 CHECK(review_required IN (0,1)),
    review_reason TEXT,
    reviewed_by TEXT REFERENCES qual_users(user_id),
    reviewed_at TEXT,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_open
ON purchase_orders(supplier_id, material_code, state);

-- 订单发货批次的检验记录（批次检测只覆盖登记过的批次）
CREATE TABLE IF NOT EXISTS order_lots (
    order_lot_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    batch_no TEXT NOT NULL,
    plant_id TEXT,
    inspection_standard TEXT NOT NULL,
    inspection_result TEXT NOT NULL CHECK(inspection_result IN ('pending','passed','failed')),
    inspection_report TEXT,
    inspected_by TEXT REFERENCES qual_users(user_id),
    inspected_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, batch_no)
);

-- 紧急替代批准：质量与采购分别批准，只对明确数量和期限生效
CREATE TABLE IF NOT EXISTS emergency_approvals (
    approval_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    original_supplier_id TEXT NOT NULL,
    substitute_supplier_id TEXT NOT NULL,
    material_code TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    quality_approved_by TEXT REFERENCES qual_users(user_id),
    quality_approved_at TEXT,
    procurement_approved_by TEXT REFERENCES qual_users(user_id),
    procurement_approved_at TEXT,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','void')),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES qual_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_until > valid_from),
    CHECK(original_supplier_id <> substitute_supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_emergency_lookup
ON emergency_approvals(substitute_supplier_id, material_code, state);

-- 每次资格变化对订单的影响记录（采购人员可按变化单追溯受影响订单）
CREATE TABLE IF NOT EXISTS qualification_change_impacts (
    impact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_kind TEXT NOT NULL
        CHECK(change_kind IN ('certificate.renewed','certificate.expired','certificate.revoked',
                              'approval.withdrawn','suspension.started','suspension.lifted',
                              'event.opened','event.closed')),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    material_code TEXT,
    plant_id TEXT,
    order_id TEXT NOT NULL REFERENCES purchase_orders(order_id),
    classification TEXT NOT NULL CHECK(classification IN ('review','retained','none')),
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_impact_change
ON qualification_change_impacts(entity_type, entity_id, impact_id);
CREATE INDEX IF NOT EXISTS idx_impact_order
ON qualification_change_impacts(order_id, impact_id);

-- 哈希链审计
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
    # HTTP 服务为多线程分发，连接配合 WAL 与 BEGIN IMMEDIATE 串行写入后可跨线程使用
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
