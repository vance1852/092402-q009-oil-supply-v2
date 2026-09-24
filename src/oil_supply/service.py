"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    RouteCalendarSpec,
    SupplyScenario,
    TransferReceiptRequest,
)
from .schedule import RouteCalendar, next_receivable
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
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write"},
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

    def register_calendar(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记线路接收日历的新版本；已发运的转运继续沿用冻结的旧版本。"""
        self._require(actor_id, "catalog.write")
        spec = RouteCalendarSpec.from_dict(raw)
        self.route(spec.route_id)
        calendar = RouteCalendar.from_snapshot(
            {
                "timezone": spec.timezone,
                "business_days": list(spec.business_days),
                "windows": [{"start": start, "end": end} for start, end in spec.windows],
                "closed_dates": list(spec.closed_dates),
                "open_dates": list(spec.open_dates),
            }
        )
        snapshot = calendar.as_snapshot()
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT max(version) AS latest FROM route_calendars WHERE route_id=?",
                (spec.route_id,),
            ).fetchone()
            version = 1 if row["latest"] is None else int(row["latest"]) + 1
            self.connection.execute(
                "INSERT INTO route_calendars(route_id,version,timezone,business_days_json,windows_json,"
                "closed_dates_json,open_dates_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    spec.route_id,
                    version,
                    snapshot["timezone"],
                    canonical_json(snapshot["business_days"]),
                    canonical_json(snapshot["windows"]),
                    canonical_json(snapshot["closed_dates"]),
                    canonical_json(snapshot["open_dates"]),
                    actor_id,
                    self._now(),
                ),
            )
            self._audit("route", spec.route_id, "calendar.registered", actor_id, {"version": version})
        return {"route_id": spec.route_id, "version": version, "calendar": snapshot}

    def _calendar_row(self, route_id: str, version: int | None = None) -> sqlite3.Row | None:
        if version is None:
            return self.connection.execute(
                "SELECT * FROM route_calendars WHERE route_id=? ORDER BY version DESC LIMIT 1",
                (route_id,),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM route_calendars WHERE route_id=? AND version=?",
            (route_id, version),
        ).fetchone()

    @staticmethod
    def _calendar_from_row(row: sqlite3.Row) -> RouteCalendar:
        return RouteCalendar.from_snapshot(
            {
                "timezone": row["timezone"],
                "business_days": json.loads(row["business_days_json"]),
                "windows": json.loads(row["windows_json"]),
                "closed_dates": json.loads(row["closed_dates_json"]),
                "open_dates": json.loads(row["open_dates_json"]),
            }
        )

    def latest_calendar(self, route_id: str) -> dict[str, Any]:
        self.route(route_id)
        row = self._calendar_row(route_id)
        if row is None:
            raise NotFound("线路尚未配置接收日历")
        return {
            "route_id": route_id,
            "version": int(row["version"]),
            "calendar": self._calendar_from_row(row).as_snapshot(),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
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
        transit_hours = int(nomination["transit_hours"])
        transit_eta = parse_utc(departed_at) + timedelta(hours=transit_hours)
        calendar_row = self._calendar_row(nomination["route_id"])
        calendar_version: int | None = None
        calendar_snapshot: str | None = None
        if calendar_row is None:
            # 线路未配置接收日历：沿用旧的固定小时数规则
            expected_arrival = transit_eta
        else:
            calendar = self._calendar_from_row(calendar_row)
            expected_arrival = next_receivable(calendar, transit_eta)
            calendar_version = int(calendar_row["version"])
            calendar_snapshot = canonical_json(calendar.as_snapshot())
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
                "expected_delivered_barrels,departed_at,transit_eta,expected_arrival,calendar_version,"
                "calendar_snapshot_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    utc_text(transit_eta),
                    utc_text(expected_arrival),
                    calendar_version,
                    calendar_snapshot,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit(
                "transfer",
                transfer_id,
                "transfer.dispatched",
                actor_id,
                {"nomination_id": nomination_id, "calendar_version": calendar_version},
            )
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "transit_eta": utc_text(transit_eta),
            "expected_arrival": utc_text(expected_arrival),
            "calendar_version": calendar_version,
        }

    def register_receipt(self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记终端收货：部分到货、质量待验或最终签收。

        每次扫描携带幂等键，重复扫描重放首次响应、不重复累计；只有
        final 签收的数量会结转提名并结案转运。
        """
        self._require(actor_id, "transfer.write")
        receipt = TransferReceiptRequest.from_dict(raw)
        request_digest = digest({"transfer_id": transfer_id, **dict(raw)})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='receipt' AND idempotency_key=?",
            (receipt.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同收货内容")
            return json.loads(stored["response_json"])
        transfer = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFound("转运不存在")
        if transfer["state"] != "in_transit":
            raise InvalidState("转运已结案，不能登记收货")
        recorded_at = self._now()
        quantity = decimal_text(receipt.quantity_barrels)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO transfer_receipts(transfer_id,kind,quantity_barrels,idempotency_key,note,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        transfer_id,
                        receipt.kind,
                        quantity,
                        receipt.idempotency_key,
                        receipt.note,
                        actor_id,
                        recorded_at,
                    ),
                )
                receipt_id = int(cursor.lastrowid)
                if receipt.kind == "final":
                    self.connection.execute(
                        "UPDATE transfers SET state='delivered',arrived_at=?,revision=revision+1 "
                        "WHERE transfer_id=? AND state='in_transit'",
                        (recorded_at, transfer_id),
                    )
                    self.connection.execute(
                        "UPDATE nominations SET delivered_barrels=?,state='delivered',revision=revision+1 "
                        "WHERE nomination_id=? AND state='in_transit'",
                        (quantity, transfer["nomination_id"]),
                    )
                response = {
                    "receipt_id": receipt_id,
                    "transfer_id": transfer_id,
                    "kind": receipt.kind,
                    "quantity_barrels": quantity,
                    "recorded_at": recorded_at,
                    "transfer_state": "delivered" if receipt.kind == "final" else "in_transit",
                }
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('receipt',?,?,?,?)",
                    (receipt.idempotency_key, request_digest, canonical_json(response), recorded_at),
                )
                self._audit(
                    "transfer",
                    transfer_id,
                    f"transfer.receipt.{receipt.kind}",
                    actor_id,
                    {"receipt_id": receipt_id, "quantity_barrels": quantity},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("收货幂等键冲突") from exc
        return response

    def transfer_detail(self, transfer_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT t.*,n.route_id,n.shipper_id FROM transfers t "
            "JOIN nominations n ON n.nomination_id=t.nomination_id WHERE t.transfer_id=?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise NotFound("转运不存在")
        receipts = self.connection.execute(
            "SELECT * FROM transfer_receipts WHERE transfer_id=? ORDER BY receipt_id",
            (transfer_id,),
        ).fetchall()
        totals = {"partial": Decimal(0), "quality_hold": Decimal(0), "final": Decimal(0)}
        for item in receipts:
            totals[item["kind"]] += Decimal(item["quantity_barrels"])
        expected = row["expected_arrival"]
        overdue = row["state"] == "in_transit" and expected is not None and parse_utc(expected) < self.clock.now()
        return {
            **dict(row),
            "receipts": [dict(item) for item in receipts],
            "received_totals": {
                "partial_barrels": decimal_text(totals["partial"]),
                "quality_hold_barrels": decimal_text(totals["quality_hold"]),
                "final_barrels": decimal_text(totals["final"]),
            },
            "overdue": overdue,
        }

    def overdue_transfers(self, as_of: str | None = None) -> dict[str, Any]:
        """列出已超过承诺接收时刻仍未结案的转运；判断完全基于落库时刻。"""
        try:
            moment = self.clock.now() if as_of is None else parse_utc(as_of, "as_of")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        rows = self.connection.execute(
            "SELECT t.transfer_id,t.nomination_id,t.expected_arrival,t.departed_at,n.route_id,n.shipper_id "
            "FROM transfers t JOIN nominations n ON n.nomination_id=t.nomination_id "
            "WHERE t.state='in_transit' AND t.expected_arrival IS NOT NULL AND t.expected_arrival<? "
            "ORDER BY t.expected_arrival,t.transfer_id",
            (utc_text(moment),),
        ).fetchall()
        items = []
        for row in rows:
            overdue_seconds = Decimal(str((moment - parse_utc(row["expected_arrival"])).total_seconds()))
            items.append(
                {
                    **dict(row),
                    "overdue_hours": decimal_text(quantize_volume(overdue_seconds / Decimal(3600))),
                }
            )
        return {"as_of": utc_text(moment), "overdue": items}

    def preview_calendar_impact(self, transfer_id: str) -> dict[str, Any]:
        """用线路最新日历重算在途承诺，只读预览，不改变冻结的预计到达。"""
        row = self.connection.execute(
            "SELECT t.*,n.route_id FROM transfers t JOIN nominations n ON n.nomination_id=t.nomination_id "
            "WHERE t.transfer_id=?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise NotFound("转运不存在")
        calendar_row = self._calendar_row(row["route_id"])
        if calendar_row is None:
            raise NotFound("线路尚未配置接收日历")
        calendar = self._calendar_from_row(calendar_row)
        transit_eta = row["transit_eta"]
        if transit_eta is None:
            route = self.route(row["route_id"])
            transit_eta = utc_text(parse_utc(row["departed_at"]) + timedelta(hours=int(route["transit_hours"])))
        projected = next_receivable(calendar, parse_utc(transit_eta))
        frozen = parse_utc(row["expected_arrival"])
        shift_seconds = Decimal(str((projected - frozen).total_seconds()))
        return {
            "transfer_id": transfer_id,
            "route_id": row["route_id"],
            "state": row["state"],
            "frozen_calendar_version": row["calendar_version"],
            "latest_calendar_version": int(calendar_row["version"]),
            "frozen_expected_arrival": row["expected_arrival"],
            "projected_expected_arrival": utc_text(projected),
            "shift_minutes": decimal_text(quantize_volume(shift_seconds / Decimal(60))),
            "changed": projected != frozen,
        }

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
