"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .calendar_support import BusinessCalendar, tzdata_version
from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    ReceiptRequest,
    Route,
    SupplyScenario,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "calendar.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "receipt.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _require_any(self, user_id: str, *permissions: str) -> sqlite3.Row:
        user = self._user(user_id)
        if not any(permission in ROLE_PERMISSIONS[user["role"]] for permission in permissions):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permissions[0]}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM price_index_quotes WHERE price_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"price_index": quote.price_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("报价版本冲突") from exc
        return {"quote_id": quote_id, "price_index": quote.price_index, "trade_date": quote.trade_date}

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_usd FROM price_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM price_index_quotes "
            "WHERE price_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (price_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": price_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_barrels,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_barrels),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return dict(row)

    def _destination(self, route_id: str) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT f.* FROM routes r JOIN facilities f ON f.facility_id=r.destination_id WHERE r.route_id=?",
            (route_id,),
        ).fetchone()

    def configure_route_calendar(
        self,
        actor_id: str,
        route_id: str,
        raw: Mapping[str, Any],
        change_summary: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "calendar.write")
        self.route(route_id)
        calendar = BusinessCalendar.from_dict(raw)
        destination = self._destination(route_id)
        if calendar.timezone_name != destination["timezone"]:
            raise Conflict("日历时区必须与终端设施时区一致")
        if not isinstance(change_summary, str) or len(change_summary) > 256:
            raise ValidationFailed("change_summary 必须是不超过 256 字符的字符串")
        sha256 = calendar.definition_sha256()
        definition = calendar.definition_json()
        current = self.connection.execute(
            "SELECT current_revision,definition_sha256 FROM route_calendars WHERE route_id=?",
            (route_id,),
        ).fetchone()
        if current is not None and current["definition_sha256"] == sha256:
            raise Conflict("日历定义与当前版本相同，无需修订")
        timezone_database = tzdata_version()
        with transaction(self.connection, immediate=True):
            if current is None:
                revision_number = 1
                self.connection.execute(
                    "INSERT INTO route_calendars(route_id,current_revision,definition_json,definition_sha256,"
                    "updated_by,updated_at) VALUES(?,?,?,?,?,?)",
                    (route_id, revision_number, definition, sha256, actor_id, self._now()),
                )
            else:
                revision_number = int(current["current_revision"]) + 1
                self.connection.execute(
                    "UPDATE route_calendars SET current_revision=?,definition_json=?,definition_sha256=?,"
                    "updated_by=?,updated_at=? WHERE route_id=?",
                    (revision_number, definition, sha256, actor_id, self._now(), route_id),
                )
            self.connection.execute(
                "INSERT INTO route_calendar_revisions(route_id,calendar_revision,definition_json,definition_sha256,"
                "tzdata_version,change_summary,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    route_id,
                    revision_number,
                    definition,
                    sha256,
                    timezone_database,
                    change_summary.strip(),
                    actor_id,
                    self._now(),
                ),
            )
            self._audit(
                "route",
                route_id,
                "calendar.revised",
                actor_id,
                {"calendar_revision": revision_number, "definition_sha256": sha256},
            )
        return {
            "route_id": route_id,
            "calendar_revision": revision_number,
            "calendar": json.loads(definition),
            "definition_sha256": sha256,
            "tzdata_version": timezone_database,
        }

    def route_calendar(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT c.*,r.tzdata_version FROM route_calendars c "
            "JOIN route_calendar_revisions r ON r.route_id=c.route_id "
            "AND r.calendar_revision=c.current_revision WHERE c.route_id=?",
            (route_id,),
        ).fetchone()
        if row is None:
            raise NotFound("线路尚未配置营业日历")
        return {
            "route_id": route_id,
            "calendar_revision": row["current_revision"],
            "calendar": json.loads(row["definition_json"]),
            "definition_sha256": row["definition_sha256"],
            "tzdata_version": row["tzdata_version"],
            "updated_at": row["updated_at"],
        }

    def calendar_revisions(self, actor_id: str, route_id: str) -> dict[str, Any]:
        self._require_any(actor_id, "report.read", "calendar.write")
        self.route(route_id)
        rows = self.connection.execute(
            "SELECT calendar_revision,definition_sha256,tzdata_version,change_summary,created_by,created_at "
            "FROM route_calendar_revisions WHERE route_id=? ORDER BY calendar_revision",
            (route_id,),
        ).fetchall()
        return {"route_id": route_id, "revisions": [dict(row) for row in rows]}

    def _calendar_schedule(
        self,
        route: Mapping[str, Any],
        departed_at: str,
    ) -> dict[str, Any] | None:
        """按当前日历计算可接收时刻；未配置日历的线路保留固定小时数算法。"""
        calendar_row = self.connection.execute(
            "SELECT current_revision,definition_json,definition_sha256 FROM route_calendars WHERE route_id=?",
            (route["route_id"],),
        ).fetchone()
        earliest = parse_utc(departed_at) + timedelta(hours=int(route["transit_hours"]))
        if calendar_row is None:
            arrival = utc_text(earliest)
            return {
                "calendar_revision": None,
                "calendar_sha256": None,
                "tzdata_version": None,
                "window_opens_at": arrival,
                "scheduled_acceptance_at": arrival,
                "committed_due_at": arrival,
                "window_closes_at": arrival,
            }
        revision_row = self.connection.execute(
            "SELECT tzdata_version FROM route_calendar_revisions WHERE route_id=? AND calendar_revision=?",
            (route["route_id"], calendar_row["current_revision"]),
        ).fetchone()
        calendar = BusinessCalendar.from_dict(json.loads(calendar_row["definition_json"]))
        slot = calendar.next_acceptance(earliest)
        due_at = slot.closes_at + timedelta(minutes=calendar.sla_grace_minutes)
        return {
            "calendar_revision": int(calendar_row["current_revision"]),
            "calendar_sha256": calendar_row["definition_sha256"],
            "tzdata_version": revision_row["tzdata_version"],
            "window_opens_at": utc_text(slot.opens_at),
            "window_closes_at": utc_text(slot.closes_at),
            "scheduled_acceptance_at": utc_text(slot.receivable_at),
            "committed_due_at": utc_text(due_at),
        }

    def preview_calendar_change(self, actor_id: str, route_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """用候选日历预演在途单的新时刻，但不写库、不改变承诺。"""
        self._require(actor_id, "calendar.write")
        route = self.route(route_id)
        candidate = BusinessCalendar.from_dict(raw)
        destination = self._destination(route_id)
        if candidate.timezone_name != destination["timezone"]:
            raise Conflict("日历时区必须与终端设施时区一致")
        candidate_json = candidate.definition_json()
        current = self.connection.execute(
            "SELECT current_revision FROM route_calendars WHERE route_id=?",
            (route_id,),
        ).fetchone()
        transfers = self.connection.execute(
            "SELECT t.* FROM transfers t JOIN nominations n ON n.nomination_id=t.nomination_id "
            "WHERE n.route_id=? AND t.calendar_revision IS NOT NULL AND t.state IN "
            "('in_transit','partial','quality_hold','overdue') ORDER BY t.transfer_id",
            (route_id,),
        ).fetchall()
        impacts: list[dict[str, Any]] = []
        for transfer in transfers:
            frozen_row = self.connection.execute(
                "SELECT definition_json FROM route_calendar_revisions WHERE route_id=? AND calendar_revision=?",
                (route_id, transfer["calendar_revision"]),
            ).fetchone()
            frozen_calendar = BusinessCalendar.from_dict(json.loads(frozen_row["definition_json"]))
            earliest = parse_utc(transfer["departed_at"]) + timedelta(hours=int(route["transit_hours"]))
            frozen_slot = frozen_calendar.next_acceptance(earliest)
            candidate_slot = candidate.next_acceptance(earliest)
            candidate_due = candidate_slot.closes_at + timedelta(minutes=candidate.sla_grace_minutes)
            projected = {
                "window_opens_at": utc_text(candidate_slot.opens_at),
                "window_closes_at": utc_text(candidate_slot.closes_at),
                "scheduled_acceptance_at": utc_text(candidate_slot.receivable_at),
                "committed_due_at": utc_text(candidate_due),
            }
            frozen = {
                "calendar_revision": transfer["calendar_revision"],
                "window_opens_at": transfer["window_opens_at"],
                "window_closes_at": utc_text(frozen_slot.closes_at),
                "scheduled_acceptance_at": transfer["scheduled_acceptance_at"],
                "committed_due_at": transfer["committed_due_at"],
            }
            changed = any(projected[key] != transfer[key] for key in (
                "window_opens_at", "scheduled_acceptance_at", "committed_due_at"
            ))
            impacts.append({
                "transfer_id": transfer["transfer_id"],
                "frozen": frozen,
                "projected": projected,
                "changed": changed,
            })
        changed_count = sum(1 for item in impacts if item["changed"])
        return {
            "route_id": route_id,
            "current_revision": None if current is None else int(current["current_revision"]),
            "candidate_calendar": json.loads(candidate_json),
            "candidate_sha256": candidate.definition_sha256(),
            "in_transit_impacts": impacts,
            "changed_count": changed_count,
            "note": "预览结果不会改变已冻结的在途承诺",
        }

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.unit_cost_usd),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("库存批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("库存批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_barrels,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_barrels),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_barrels"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_barrels"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_barrels"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        if available < allocated:
            raise Conflict("库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        schedule = self._calendar_schedule(self.route(nomination["route_id"]), departed_at)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
                "expected_delivered_barrels,departed_at,created_by,created_at,"
                "calendar_revision,calendar_sha256,tzdata_version,window_opens_at,"
                "scheduled_acceptance_at,committed_due_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                    schedule["calendar_revision"],
                    schedule["calendar_sha256"],
                    schedule["tzdata_version"],
                    schedule["window_opens_at"],
                    schedule["scheduled_acceptance_at"],
                    schedule["committed_due_at"],
                ),
            )
            self._audit(
                "transfer",
                transfer_id,
                "transfer.dispatched",
                actor_id,
                {
                    "nomination_id": nomination_id,
                    "calendar_revision": schedule["calendar_revision"],
                    "calendar_sha256": schedule["calendar_sha256"],
                    "scheduled_acceptance_at": schedule["scheduled_acceptance_at"],
                    "committed_due_at": schedule["committed_due_at"],
                },
            )
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "expected_arrival": schedule["scheduled_acceptance_at"],
            "window_opens_at": schedule["window_opens_at"],
            "committed_due_at": schedule["committed_due_at"],
            "calendar_revision": schedule["calendar_revision"],
        }

    def _refresh_overdue(self, transfer: sqlite3.Row, now: datetime | None = None) -> sqlite3.Row:
        """按冻结承诺重新计算超时；任何时刻调用结果一致，因此重启安全。"""
        moment = (self.clock.now() if now is None else now).astimezone(timezone.utc)
        if transfer["state"] in ("completed", "disputed", "delivered"):
            return transfer
        is_overdue = bool(transfer["is_overdue"])
        overdue_since = transfer["overdue_since"]
        if not is_overdue and transfer["committed_due_at"] is not None:
            if moment > parse_utc(transfer["committed_due_at"]):
                is_overdue = True
                overdue_since = utc_text(moment)
                # 标记与审计在同一事务内，崩溃不会留下无审计的超时。
                with transaction(self.connection):
                    if transfer["state"] == "in_transit":
                        # 已部分到货或质量待验的单保留原状态；全程在途的单进入 overdue。
                        self.connection.execute(
                            "UPDATE transfers SET is_overdue=1,overdue_since=?,state='overdue' "
                            "WHERE transfer_id=? AND is_overdue=0",
                            (overdue_since, transfer["transfer_id"]),
                        )
                    else:
                        self.connection.execute(
                            "UPDATE transfers SET is_overdue=1,overdue_since=? WHERE transfer_id=? AND is_overdue=0",
                            (overdue_since, transfer["transfer_id"]),
                        )
                    self._audit(
                        "transfer",
                        transfer["transfer_id"],
                        "transfer.overdue",
                        "system",
                        {"committed_due_at": transfer["committed_due_at"], "evaluated_at": utc_text(moment)},
                    )
                transfer = self.connection.execute(
                    "SELECT * FROM transfers WHERE transfer_id=?", (transfer["transfer_id"],)
                ).fetchone()
        return transfer

    def _receipt_totals(self, transfer_id: str) -> dict[str, Decimal]:
        rows = self.connection.execute(
            "SELECT stage,quantity_barrels,quality_state FROM transfer_receipts WHERE transfer_id=?",
            (transfer_id,),
        ).fetchall()
        partial = Decimal("0")
        quality_pending = Decimal("0")
        final = Decimal("0")
        final_accepted = Decimal("0")
        for row in rows:
            quantity = Decimal(row["quantity_barrels"])
            if row["stage"] == "partial":
                partial += quantity
            elif row["stage"] == "quality_pending":
                quality_pending += quantity
            else:
                final += quantity
                if row["quality_state"] == "accepted":
                    final_accepted += quantity
        return {
            "partial": partial,
            "quality_pending": quality_pending,
            "final": final,
            "final_accepted": final_accepted,
        }

    def transfer_status(self, transfer_id: str) -> dict[str, Any]:
        transfer = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFound("转运记录不存在")
        transfer = self._refresh_overdue(transfer)
        totals = self._receipt_totals(transfer_id)
        return {
            "transfer_id": transfer_id,
            "state": transfer["state"],
            "loaded_barrels": transfer["loaded_barrels"],
            "expected_delivered_barrels": transfer["expected_delivered_barrels"],
            "departed_at": transfer["departed_at"],
            "arrived_at": transfer["arrived_at"],
            "window_opens_at": transfer["window_opens_at"],
            "scheduled_acceptance_at": transfer["scheduled_acceptance_at"],
            "committed_due_at": transfer["committed_due_at"],
            "is_overdue": bool(transfer["is_overdue"]),
            "overdue_since": transfer["overdue_since"],
            "calendar_revision": transfer["calendar_revision"],
            "calendar_sha256": transfer["calendar_sha256"],
            "tzdata_version": transfer["tzdata_version"],
            "partial_received_barrels": decimal_text(quantize_volume(totals["partial"])),
            "quality_pending_barrels": decimal_text(quantize_volume(totals["quality_pending"])),
            "final_received_barrels": decimal_text(quantize_volume(totals["final"])),
            "final_accepted_barrels": decimal_text(quantize_volume(totals["final_accepted"])),
        }

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt = ReceiptRequest.from_dict(raw)
        transfer = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (receipt.transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFound("转运记录不存在")
        transfer = self._refresh_overdue(transfer)
        if transfer["state"] in ("completed", "disputed"):
            raise InvalidState("转运已结案，不能继续登记到货")
        duplicate = self.connection.execute(
            "SELECT transfer_id FROM transfer_receipts WHERE scan_code=?",
            (receipt.scan_code,),
        ).fetchone()
        if duplicate is not None:
            if duplicate["transfer_id"] != receipt.transfer_id:
                raise Conflict("扫描码已用于其他转运记录")
            # 同一转运记录重复扫描：原样返回，不得重复累计。
            return self._receipt_response(receipt.transfer_id, replayed=True)
        totals = self._receipt_totals(receipt.transfer_id)
        quantity = quantize_volume(receipt.quantity_barrels)
        loaded = Decimal(transfer["loaded_barrels"])
        if receipt.stage in ("partial", "quality_pending"):
            already = totals["partial"] + totals["quality_pending"] + totals["final"]
            if already + quantity > loaded:
                raise Conflict("累计到货数量不能超过装车数量")
            quality_state = "pending"
        else:
            if totals["final"] > 0:
                raise Conflict("最终签收已经登记，不能重复最终签收")
            if quantity > loaded:
                raise Conflict("最终签收数量不能超过装车数量")
            quality_state = "accepted"
        recorded_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO transfer_receipts(transfer_id,stage,quantity_barrels,quality_state,"
                "scan_code,note,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt.transfer_id,
                    receipt.stage,
                    decimal_text(quantity),
                    quality_state,
                    receipt.scan_code,
                    receipt.note,
                    actor_id,
                    recorded_at,
                ),
            )
            if receipt.stage == "partial":
                new_state = "partial"
                arrived_at = transfer["arrived_at"] or recorded_at
            elif receipt.stage == "quality_pending":
                new_state = "quality_hold"
                arrived_at = transfer["arrived_at"] or recorded_at
            else:
                new_state = "completed"
                arrived_at = recorded_at
            self.connection.execute(
                "UPDATE transfers SET state=?,arrived_at=?,revision=revision+1 WHERE transfer_id=?",
                (new_state, arrived_at, receipt.transfer_id),
            )
            if receipt.stage == "final":
                self.connection.execute(
                    "UPDATE nominations SET delivered_barrels=?,state='delivered',revision=revision+1 "
                    "WHERE nomination_id=?",
                    (decimal_text(quantity), transfer["nomination_id"]),
                )
            self._audit(
                "transfer",
                receipt.transfer_id,
                f"receipt.{receipt.stage}",
                actor_id,
                {"scan_code": receipt.scan_code, "quantity_barrels": decimal_text(quantity)},
            )
        return self._receipt_response(receipt.transfer_id, replayed=False)

    def _receipt_response(self, transfer_id: str, *, replayed: bool) -> dict[str, Any]:
        status = self.transfer_status(transfer_id)
        nomination = self.connection.execute(
            "SELECT n.nomination_id,n.delivered_barrels,n.state FROM nominations n "
            "JOIN transfers t ON t.nomination_id=n.nomination_id WHERE t.transfer_id=?",
            (transfer_id,),
        ).fetchone()
        status["idempotent_replay"] = replayed
        status["nomination"] = dict(nomination) if nomination is not None else None
        return status

    def sweep_overdue(self, actor_id: str | None = None) -> dict[str, Any]:
        """批量判定超时；服务重启后重放结果相同，不依赖内存状态。"""
        if actor_id is not None:
            self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM transfers WHERE is_overdue=0 AND committed_due_at IS NOT NULL "
            "AND state NOT IN ('completed','disputed','delivered') ORDER BY transfer_id"
        ).fetchall()
        marked = 0
        for row in rows:
            refreshed = self._refresh_overdue(row)
            if refreshed["is_overdue"]:
                marked += 1
        return {"evaluated": len(rows), "marked_overdue": marked, "evaluated_at": self._now()}

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_usd FROM price_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用报价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_barrels AS REAL)) available_barrels "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_usd"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_usd"]),
            price_index_drop_percent=scenario.price_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
