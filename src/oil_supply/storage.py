"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    price_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_usd TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES price_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(price_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON price_index_quotes(price_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_barrels TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_barrels TEXT NOT NULL,
    available_barrels TEXT NOT NULL,
    unit_cost_usd TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_barrels TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_barrels TEXT NOT NULL,
    allocated_barrels TEXT NOT NULL DEFAULT '0',
    delivered_barrels TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS route_calendars (
    route_id TEXT PRIMARY KEY REFERENCES routes(route_id),
    current_revision INTEGER NOT NULL DEFAULT 0,
    definition_json TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES supply_users(user_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_calendar_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    calendar_revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    tzdata_version TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, calendar_revision)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    loaded_barrels TEXT NOT NULL,
    expected_delivered_barrels TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit'
        CHECK(state IN ('in_transit','partial','quality_hold','delivered','completed','disputed','overdue')),
    revision INTEGER NOT NULL DEFAULT 1,
    -- 发运时刻冻结的日历版本；日历后续修订不会改变这些承诺。
    calendar_revision INTEGER,
    calendar_sha256 TEXT,
    tzdata_version TEXT,
    window_opens_at TEXT,
    scheduled_acceptance_at TEXT,
    committed_due_at TEXT,
    is_overdue INTEGER NOT NULL DEFAULT 0 CHECK(is_overdue IN (0,1)),
    overdue_since TEXT,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL REFERENCES transfers(transfer_id),
    stage TEXT NOT NULL CHECK(stage IN ('partial','quality_pending','final')),
    quantity_barrels TEXT NOT NULL,
    quality_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(quality_state IN ('pending','accepted','rejected')),
    scan_code TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_transfer
ON transfer_receipts(transfer_id, receipt_id);

CREATE INDEX IF NOT EXISTS idx_transfers_overdue
ON transfers(is_overdue, committed_due_at);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：线程化 HTTP 服务器的多个工作线程共享连接，
    # 由调用方（API 层的锁）串行化使用。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    legacy_pending = _stage_legacy_transfers(connection)
    connection.executescript(SCHEMA)
    if legacy_pending:
        _copy_legacy_transfers(connection)


def _transfers_columns(connection: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in connection.execute("PRAGMA table_info(transfers)").fetchall()}


def _stage_legacy_transfers(connection: sqlite3.Connection) -> bool:
    """旧库的 transfers 缺少日历列时先改名，让 SCHEMA 可以创建新表。"""
    columns = _transfers_columns(connection)
    if not columns or "calendar_revision" in columns:
        return False
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN")
    try:
        connection.execute("ALTER TABLE transfers RENAME TO transfers_legacy")
    except BaseException:
        connection.rollback()
        connection.execute("PRAGMA foreign_keys=ON")
        raise
    else:
        connection.commit()
    return True


def _copy_legacy_transfers(connection: sqlite3.Connection) -> None:
    """把旧 transfers 数据搬进新表并校验外键，整个过程在一个事务内。"""
    connection.execute("BEGIN")
    try:
        connection.execute(
            """
INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,
    expected_delivered_barrels,departed_at,arrived_at,state,revision,created_by,created_at)
SELECT transfer_id,nomination_id,inventory_lot_id,loaded_barrels,
    expected_delivered_barrels,departed_at,arrived_at,state,revision,created_by,created_at
FROM transfers_legacy
"""
        )
        connection.execute("DROP TABLE transfers_legacy")
        failures = connection.execute("PRAGMA foreign_key_check").fetchall()
        if failures:
            raise sqlite3.DatabaseError(f"transfers 迁移后外键检查失败: {failures!r}")
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


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
