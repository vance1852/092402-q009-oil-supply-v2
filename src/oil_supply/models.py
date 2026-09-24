"""油气供应领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CRUDE_GRADES = {"BRENT", "WTI", "DUBAI", "ESPO", "URAL", "CUSTOM"}
PRODUCTS = {"crude", "gasoline-92", "gasoline-95", "diesel", "jet-fuel", "condensate"}
ROUTE_KINDS = {"pipeline", "terminal", "refinery", "storage", "truck-rack"}
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
RECEIPT_KINDS = ("partial", "quality_hold", "final")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def time_text(value: object, field: str) -> str:
    result = required_text(value, field, 5)
    try:
        return time.fromisoformat(result).isoformat(timespec="minutes")
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 HH:MM 时间") from exc


def _date_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是日期数组")
    return tuple(sorted({date_text(item, f"{field} 元素") for item in value}))


@dataclass(frozen=True, slots=True)
class IndexQuote:
    price_index: str
    trade_date: str
    close_usd: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        price_index = required_text(raw.get("price_index"), "price_index", 16).upper()
        if price_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("price_index 必须是 BRENT、WTI、DUBAI、ESPO 或 URAL")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            price_index=price_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_usd=decimal_value(raw.get("close_usd"), "close_usd", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_barrels: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_barrels=decimal_value(
                raw.get("capacity_barrels"), "capacity_barrels", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("线路起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_barrels: Decimal
    unit_cost_usd: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            unit_cost_usd=decimal_value(
                raw.get("unit_cost_usd"), "unit_cost_usd", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_barrels: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_barrels=decimal_value(
                raw.get("requested_barrels"), "requested_barrels", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    price_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            price_index_drop_percent=decimal_value(
                raw.get("price_index_drop_percent", 0),
                "price_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


@dataclass(frozen=True, slots=True)
class RouteCalendarSpec:
    """线路接收日历的一次版本登记。"""

    route_id: str
    timezone: str
    business_days: tuple[str, ...]
    windows: tuple[tuple[str, str], ...]
    closed_dates: tuple[str, ...]
    open_dates: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RouteCalendarSpec":
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        days_raw = raw.get("business_days")
        if not isinstance(days_raw, Sequence) or isinstance(days_raw, str) or not days_raw:
            raise ValidationFailed("business_days 必须是非空数组")
        days: set[str] = set()
        for item in days_raw:
            code = required_text(item, "business_days 元素", 3).upper()
            if code not in WEEKDAY_CODES:
                raise ValidationFailed("business_days 必须是 MON 到 SUN 的代码")
            days.add(code)
        windows_raw = raw.get("windows")
        if not isinstance(windows_raw, Sequence) or isinstance(windows_raw, str) or not windows_raw:
            raise ValidationFailed("windows 必须是非空数组")
        windows: list[tuple[str, str]] = []
        for item in windows_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("windows 元素必须是含 start 和 end 的对象")
            start = time_text(item.get("start"), "windows.start")
            end = time_text(item.get("end"), "windows.end")
            if start == end:
                raise ValidationFailed("交接窗口的 start 和 end 不能相同")
            windows.append((start, end))
        _check_window_overlap(windows)
        closed = _date_list(raw.get("closed_dates", []), "closed_dates")
        opened = _date_list(raw.get("open_dates", []), "open_dates")
        if set(closed) & set(opened):
            raise ValidationFailed("closed_dates 和 open_dates 不能包含同一天")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            timezone=timezone,
            business_days=tuple(code for code in WEEKDAY_CODES if code in days),
            windows=tuple(sorted(windows)),
            closed_dates=closed,
            open_dates=opened,
        )


def _check_window_overlap(windows: Sequence[tuple[str, str]]) -> None:
    """拒绝互相重叠的交接窗口，跨午夜窗口展开到次日分钟轴上检查。"""
    intervals: list[tuple[int, int]] = []
    for start, end in windows:
        first = int(start[:2]) * 60 + int(start[3:])
        last = int(end[:2]) * 60 + int(end[3:])
        if last <= first:
            last += 24 * 60
        intervals.append((first, last))
        if last > 24 * 60:
            intervals.append((0, last - 24 * 60))
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise ValidationFailed("交接窗口不能互相重叠")


@dataclass(frozen=True, slots=True)
class TransferReceiptRequest:
    """终端对一次转运的收货登记。"""

    kind: str
    quantity_barrels: Decimal
    idempotency_key: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransferReceiptRequest":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in RECEIPT_KINDS:
            raise ValidationFailed("kind 必须是 partial、quality_hold 或 final")
        note = raw.get("note", "")
        if not isinstance(note, str) or len(note) > 256:
            raise ValidationFailed("note 不能超过 256 个字符")
        return cls(
            kind=kind,
            quantity_barrels=decimal_value(
                raw.get("quantity_barrels"), "quantity_barrels", minimum=Decimal("0.001")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            note=note.strip(),
        )
