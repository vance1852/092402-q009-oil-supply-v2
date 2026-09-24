"""线路接收日历、终端签收与超时判断的测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock, parse_utc, utc_text
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from oil_supply.schedule import RouteCalendar, next_receivable, resolve_local, load_timezone
from oil_supply.service import SupplyService
from oil_supply.storage import connect


def make_calendar(**overrides: object) -> RouteCalendar:
    snapshot = {
        "timezone": "Asia/Shanghai",
        "business_days": ["MON", "TUE", "WED", "THU", "FRI"],
        "windows": [{"start": "08:00", "end": "17:00"}],
        "closed_dates": [],
        "open_dates": [],
    }
    snapshot.update(overrides)
    return RouteCalendar.from_snapshot(snapshot)


class ScheduleTests(unittest.TestCase):
    def test_dst_gap_window_start_shifts_forward(self) -> None:
        # 2026-03-08 美国东部夏令时开始，02:30 本地时刻不存在
        tz = load_timezone("America/New_York")
        resolved = resolve_local(datetime(2026, 3, 8, 2, 30), tz)
        self.assertEqual(utc_text(resolved), "2026-03-08T07:00:00Z")
        calendar = make_calendar(
            timezone="America/New_York",
            business_days=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            windows=[{"start": "02:30", "end": "12:00"}],
        )
        # 01:30 EST 到达，窗口开始时刻缺失，顺延到 03:00 EDT
        moment = parse_utc("2026-03-08T06:30:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, moment)), "2026-03-08T07:00:00Z")

    def test_dst_fold_picks_first_occurrence(self) -> None:
        # 2026-11-01 美国东部夏令时结束，01:30 本地时刻出现两次
        tz = load_timezone("America/New_York")
        resolved = resolve_local(datetime(2026, 11, 1, 1, 30), tz)
        self.assertEqual(utc_text(resolved), "2026-11-01T05:30:00Z")
        calendar = make_calendar(
            timezone="America/New_York",
            business_days=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            windows=[{"start": "01:00", "end": "04:00"}],
        )
        # 窗口开始取第一次出现的 01:00（EDT）
        before = parse_utc("2026-11-01T04:30:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, before)), "2026-11-01T05:00:00Z")
        # 两次出现的 01:30 都落在窗口内，可立即接收
        first = parse_utc("2026-11-01T05:45:00Z")
        second = parse_utc("2026-11-01T06:30:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, first)), "2026-11-01T05:45:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, second)), "2026-11-01T06:30:00Z")

    def test_cross_year_holiday_and_weekend_roll_forward(self) -> None:
        calendar = make_calendar(closed_dates=["2027-01-01"])
        # 2026-12-31T20:00Z = 2027-01-01 04:00 CST，元旦关闭，随后是周末
        moment = parse_utc("2026-12-31T20:00:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, moment)), "2027-01-04T00:00:00Z")

    def test_cross_midnight_window_covers_next_morning(self) -> None:
        calendar = make_calendar(windows=[{"start": "20:00", "end": "02:00"}])
        # 周五夜班窗口覆盖周六凌晨：2026-09-25T17:30Z = 周六 01:30 CST
        night = parse_utc("2026-09-25T17:30:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, night)), "2026-09-25T17:30:00Z")
        # 周日凌晨到达，周五窗口已过，下一窗口是周一 20:00 CST
        sunday = parse_utc("2026-09-26T19:00:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, sunday)), "2026-09-28T12:00:00Z")

    def test_arrival_inside_window_is_immediate(self) -> None:
        calendar = make_calendar()
        # 2026-09-24T02:00Z = 周四 10:00 CST，窗口内
        moment = parse_utc("2026-09-24T02:00:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, moment)), "2026-09-24T02:00:00Z")

    def test_open_date_overrides_weekly_rule(self) -> None:
        calendar = make_calendar(open_dates=["2026-09-27"])
        # 2026-09-27 是周日，例外开放；01:00 CST 到达等到 08:00
        moment = parse_utc("2026-09-26T17:00:00Z")
        self.assertEqual(utc_text(next_receivable(calendar, moment)), "2026-09-27T00:00:00Z")

    def test_closed_calendar_never_resolves(self) -> None:
        calendar = make_calendar(business_days=["MON"], closed_dates=[])
        closed = RouteCalendar(
            timezone="UTC",
            business_days=frozenset(),
            windows=calendar.windows,
            closed_dates=frozenset(),
            open_dates=frozenset(),
        )
        with self.assertRaises(ValidationFailed):
            next_receivable(closed, parse_utc("2026-09-24T00:00:00Z"))


class TransferServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "America/New_York", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "200000", "unit_cost_usd": "91", "received_at": "2026-09-24T06:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def calendar_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "route_id": "pipe-a-b",
            "timezone": "Asia/Shanghai",
            "business_days": ["MON", "TUE", "WED", "THU", "FRI"],
            "windows": [{"start": "08:00", "end": "18:00"}],
            "closed_dates": [],
            "open_dates": [],
        }
        payload.update(overrides)
        return payload

    def dispatch(self, number: int = 1) -> dict[str, object]:
        self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-25", "requested_barrels": "40000", "priority": 10, "idempotency_key": f"key-{number}"})
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        return self.service.dispatch_transfer("dispatch", f"transfer-{number}", f"nom-{number}", "lot-1", 2)

    def test_dispatch_freezes_calendar_and_expected_arrival(self) -> None:
        self.service.register_calendar("plan", self.calendar_payload())
        transfer = self.dispatch()
        # 周四 08:00Z 离港 + 36h = 周六 04:00 CST，顺延到周一 08:00 CST
        self.assertEqual(transfer["transit_eta"], "2026-09-25T20:00:00Z")
        self.assertEqual(transfer["expected_arrival"], "2026-09-28T00:00:00Z")
        self.assertEqual(transfer["calendar_version"], 1)

    def test_dispatch_without_calendar_falls_back_to_fixed_hours(self) -> None:
        transfer = self.dispatch()
        self.assertEqual(transfer["expected_arrival"], "2026-09-25T20:00:00Z")
        self.assertIsNone(transfer["calendar_version"])

    def test_calendar_revision_keeps_in_transit_commitment_but_preview_shows_impact(self) -> None:
        self.service.register_calendar("plan", self.calendar_payload())
        self.dispatch()
        self.service.register_calendar("plan", self.calendar_payload(closed_dates=["2026-09-28"]))
        detail = self.service.transfer_detail("transfer-1")
        self.assertEqual(detail["expected_arrival"], "2026-09-28T00:00:00Z")
        self.assertEqual(detail["calendar_version"], 1)
        preview = self.service.preview_calendar_impact("transfer-1")
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["frozen_calendar_version"], 1)
        self.assertEqual(preview["latest_calendar_version"], 2)
        self.assertEqual(preview["frozen_expected_arrival"], "2026-09-28T00:00:00Z")
        self.assertEqual(preview["projected_expected_arrival"], "2026-09-29T00:00:00Z")
        self.assertEqual(preview["shift_minutes"], "1440.000")
        # 在途承诺不被改写
        self.assertEqual(self.service.transfer_detail("transfer-1")["expected_arrival"], "2026-09-28T00:00:00Z")
        # 新发运使用新版本日历
        second = self.dispatch(2)
        self.assertEqual(second["calendar_version"], 2)
        self.assertEqual(second["expected_arrival"], "2026-09-29T00:00:00Z")

    def test_partial_and_quality_hold_do_not_settle_nomination(self) -> None:
        self.dispatch()
        self.service.register_receipt("dispatch", "transfer-1", {"kind": "partial", "quantity_barrels": "10000", "idempotency_key": "scan-1"})
        self.service.register_receipt("dispatch", "transfer-1", {"kind": "quality_hold", "quantity_barrels": "5000", "idempotency_key": "scan-2"})
        nomination = self.connection.execute("SELECT * FROM nominations WHERE nomination_id='nom-1'").fetchone()
        self.assertEqual(nomination["delivered_barrels"], "0")
        self.assertEqual(nomination["state"], "in_transit")
        detail = self.service.transfer_detail("transfer-1")
        self.assertEqual(detail["received_totals"]["partial_barrels"], "10000")
        self.assertEqual(detail["received_totals"]["quality_hold_barrels"], "5000")

    def test_final_receipt_settles_nomination_exactly_once(self) -> None:
        self.dispatch()
        self.service.register_receipt("dispatch", "transfer-1", {"kind": "partial", "quantity_barrels": "10000", "idempotency_key": "scan-1"})
        final = self.service.register_receipt("dispatch", "transfer-1", {"kind": "final", "quantity_barrels": "38000", "idempotency_key": "scan-2"})
        self.assertEqual(final["transfer_state"], "delivered")
        nomination = self.connection.execute("SELECT * FROM nominations WHERE nomination_id='nom-1'").fetchone()
        self.assertEqual(nomination["delivered_barrels"], "38000")
        self.assertEqual(nomination["state"], "delivered")
        detail = self.service.transfer_detail("transfer-1")
        self.assertEqual(detail["state"], "delivered")
        self.assertIsNotNone(detail["arrived_at"])
        with self.assertRaises(InvalidState):
            self.service.register_receipt("dispatch", "transfer-1", {"kind": "final", "quantity_barrels": "1000", "idempotency_key": "scan-3"})
        nomination = self.connection.execute("SELECT * FROM nominations WHERE nomination_id='nom-1'").fetchone()
        self.assertEqual(nomination["delivered_barrels"], "38000")

    def test_duplicate_scan_replays_without_double_counting(self) -> None:
        self.dispatch()
        payload = {"kind": "partial", "quantity_barrels": "10000", "idempotency_key": "scan-1"}
        first = self.service.register_receipt("dispatch", "transfer-1", payload)
        second = self.service.register_receipt("dispatch", "transfer-1", dict(payload))
        self.assertEqual(first, second)
        detail = self.service.transfer_detail("transfer-1")
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertEqual(detail["received_totals"]["partial_barrels"], "10000")
        with self.assertRaises(Conflict):
            self.service.register_receipt("dispatch", "transfer-1", {"kind": "partial", "quantity_barrels": "12000", "idempotency_key": "scan-1"})
        # 最终签收的重复扫描同样只结转一次
        final_payload = {"kind": "final", "quantity_barrels": "38000", "idempotency_key": "scan-9"}
        final_first = self.service.register_receipt("dispatch", "transfer-1", final_payload)
        final_second = self.service.register_receipt("dispatch", "transfer-1", dict(final_payload))
        self.assertEqual(final_first, final_second)
        nomination = self.connection.execute("SELECT * FROM nominations WHERE nomination_id='nom-1'").fetchone()
        self.assertEqual(nomination["delivered_barrels"], "38000")
        count = self.connection.execute("SELECT count(*) FROM transfer_receipts WHERE transfer_id='transfer-1'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_receipt_requires_existing_in_transit_transfer(self) -> None:
        with self.assertRaises(NotFound):
            self.service.register_receipt("dispatch", "transfer-x", {"kind": "partial", "quantity_barrels": "1", "idempotency_key": "scan-x"})
        with self.assertRaises(Forbidden):
            self.service.register_receipt("plan", "transfer-x", {"kind": "partial", "quantity_barrels": "1", "idempotency_key": "scan-y"})
        with self.assertRaises(ValidationFailed):
            self.service.register_receipt("dispatch", "transfer-x", {"kind": "unknown", "quantity_barrels": "1", "idempotency_key": "scan-z"})

    def test_calendar_registration_validates_rules(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_calendar("dispatch", self.calendar_payload())
        with self.assertRaises(ValidationFailed):
            self.service.register_calendar("plan", self.calendar_payload(timezone="Mars/Olympus"))
        with self.assertRaises(ValidationFailed):
            self.service.register_calendar("plan", self.calendar_payload(windows=[{"start": "08:00", "end": "12:00"}, {"start": "11:00", "end": "13:00"}]))
        with self.assertRaises(ValidationFailed):
            self.service.register_calendar("plan", self.calendar_payload(closed_dates=["2026-10-01"], open_dates=["2026-10-01"]))
        with self.assertRaises(NotFound):
            self.service.latest_calendar("pipe-a-b")
        registered = self.service.register_calendar("plan", self.calendar_payload())
        self.assertEqual(registered["version"], 1)
        self.assertEqual(self.service.latest_calendar("pipe-a-b")["version"], 1)

    def test_overdue_follows_calendar_commitment_not_raw_hours(self) -> None:
        self.service.register_calendar("plan", self.calendar_payload())
        self.dispatch()
        # 已过固定的 36 小时（09-25T20:00Z），但未到日历承诺的周一 08:00 CST
        self.clock.current = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(self.service.overdue_transfers()["overdue"], [])
        self.assertFalse(self.service.transfer_detail("transfer-1")["overdue"])
        # 超过承诺时刻后超时
        self.clock.current = datetime(2026, 9, 28, 0, 0, 1, tzinfo=timezone.utc)
        overdue = self.service.overdue_transfers()["overdue"]
        self.assertEqual([item["transfer_id"] for item in overdue], ["transfer-1"])
        self.assertTrue(self.service.transfer_detail("transfer-1")["overdue"])
        # 最终签收后不再超时
        self.service.register_receipt("dispatch", "transfer-1", {"kind": "final", "quantity_barrels": "38000", "idempotency_key": "scan-1"})
        self.assertEqual(self.service.overdue_transfers()["overdue"], [])

    def test_overdue_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "supply.sqlite3"
            connection = connect(path)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            service = SupplyService(connection, clock)
            service.create_user("plan", "plan", "planner")
            service.create_user("dispatch", "dispatch", "dispatcher")
            service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
            service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "America/New_York", "capacity_barrels": "800000"})
            service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
            service.register_calendar("plan", self.calendar_payload())
            service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "60000", "unit_cost_usd": "91", "received_at": "2026-09-24T06:00:00Z"})
            service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-25", "requested_barrels": "40000", "priority": 10, "idempotency_key": "key-1"})
            service.allocate("dispatch", "pipe-a-b", "2026-09-25")
            service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
            connection.close()
            # 服务重启：新连接、新实例、时钟已越过承诺时刻
            reopened = connect(path)
            try:
                restarted = SupplyService(reopened, FrozenClock(datetime(2026, 9, 29, 0, 0, 1, tzinfo=timezone.utc)))
                overdue = restarted.overdue_transfers()["overdue"]
                self.assertEqual([item["transfer_id"] for item in overdue], ["transfer-1"])
                self.assertEqual(overdue[0]["expected_arrival"], "2026-09-28T00:00:00Z")
                self.assertEqual(overdue[0]["overdue_hours"], "24.000")
                self.assertTrue(restarted.transfer_detail("transfer-1")["overdue"])
            finally:
                reopened.close()

    def test_api_exposes_calendar_receipt_and_overdue(self) -> None:
        app = JsonApplication(self.service)
        headers = {"X-Actor-Id": "plan"}
        created = app.handle("POST", "/routes/pipe-a-b/calendars", headers, b'{"timezone":"Asia/Shanghai","business_days":["MON","TUE","WED","THU","FRI"],"windows":[{"start":"08:00","end":"18:00"}]}')
        self.assertEqual(created.status, 201)
        latest = app.handle("GET", "/routes/pipe-a-b/calendar", headers)
        self.assertEqual(latest.status, 200)
        self.assertEqual(latest.body["version"], 1)
        self.dispatch()
        receipt = app.handle("POST", "/transfers/transfer-1/receipts", {"X-Actor-Id": "dispatch"}, b'{"kind":"final","quantity_barrels":"38000","idempotency_key":"scan-1"}')
        self.assertEqual(receipt.status, 201)
        detail = app.handle("GET", "/transfers/transfer-1", headers)
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.body["state"], "delivered")
        overdue = app.handle("GET", "/transfers/overdue", headers)
        self.assertEqual(overdue.status, 200)
        self.assertEqual(overdue.body["overdue"], [])
        preview = app.handle("GET", "/transfers/transfer-1/calendar-preview", headers)
        self.assertEqual(preview.status, 200)
        self.assertFalse(preview.body["changed"])


if __name__ == "__main__":
    unittest.main()
