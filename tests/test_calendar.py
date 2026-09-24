from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oil_supply.api import JsonApplication
from oil_supply.calendar_support import BusinessCalendar
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from oil_supply.service import SupplyService
from oil_supply.storage import connect
from oil_supply.clock import parse_utc


def calendar_definition(**overrides: object) -> dict[str, object]:
    definition: dict[str, object] = {
        "timezone": "Asia/Shanghai",
        "business_days": ["mon", "tue", "wed", "thu", "fri"],
        "windows": [
            {"weekday": day, "opens": "08:30", "closes": "17:00"}
            for day in ("mon", "tue", "wed", "thu", "fri")
        ],
        "holidays": [],
        "extra_workdays": [],
        "sla_grace_minutes": 0,
    }
    definition.update(overrides)
    return definition


class CalendarMathTests(unittest.TestCase):
    def ny_calendar(self, opens: str, closes: str, *, days: list[str], grace: int = 0) -> BusinessCalendar:
        return BusinessCalendar.from_dict(calendar_definition(
            timezone="America/New_York",
            business_days=days,
            windows=[{"weekday": day, "opens": opens, "closes": closes} for day in days],
            sla_grace_minutes=grace,
        ))

    def test_spring_forward_gap_advances_missing_wall_time(self) -> None:
        # 2026-03-08 02:00 在纽约不存在：02:30 缺口时刻按跳时后的真实瞬间计算。
        calendar = self.ny_calendar("02:30", "04:00", days=["sun"])
        slot = calendar.next_acceptance(parse_utc("2026-03-08T06:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-03-08T07:30:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-03-08T08:00:00+00:00")
        self.assertEqual(slot.receivable_at, slot.opens_at)

    def test_fall_back_repeated_hour_window_covers_both_occurrences(self) -> None:
        # 窗口 01:00-02:30 跨越重复小时：起点取第一次（EDT 05:00Z），
        # 终点取第二次（EST 02:30 = 07:30Z），物理时长 2 小时 30 分。
        calendar = self.ny_calendar("01:00", "02:30", days=["sun"])
        slot = calendar.next_acceptance(parse_utc("2026-11-01T04:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-11-01T05:00:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-11-01T07:30:00+00:00")
        # 第一次本地 1:30（EDT）与第二次本地 1:30（EST）都落在窗口内。
        inside = calendar.next_acceptance(parse_utc("2026-11-01T06:30:00Z"))
        self.assertEqual(inside.receivable_at.isoformat(), "2026-11-01T06:30:00+00:00")
        self.assertLess(parse_utc("2026-11-01T07:00:00Z"), slot.closes_at)
        after = calendar.next_acceptance(parse_utc("2026-11-01T07:31:00Z"))
        self.assertNotEqual(after.opens_at.date(), slot.opens_at.date())

    def test_holiday_and_makeup_workday(self) -> None:
        calendar = BusinessCalendar.from_dict(calendar_definition(
            windows=[
                *({"weekday": d, "opens": "08:30", "closes": "17:00"} for d in ("mon", "tue", "wed", "thu", "fri")),
                {"weekday": "sat", "opens": "08:30", "closes": "12:00"},
            ],
            holidays=["2026-10-01", "2026-10-02"],
            extra_workdays=["2026-10-03"],
        ))
        # 10-03 是周六，因调休上班而营业，使用周六窗口。
        slot = calendar.next_acceptance(parse_utc("2026-10-02T18:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-10-03T00:30:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-10-03T04:00:00+00:00")

    def test_cross_year_holiday_searches_into_next_year(self) -> None:
        calendar = BusinessCalendar.from_dict(calendar_definition(
            holidays=["2026-12-31", "2027-01-01"],
        ))
        # 12-31 03:00Z（本地周五 11:00）遇节假日，周末后到 2027-01-04 周一。
        slot = calendar.next_acceptance(parse_utc("2026-12-31T03:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2027-01-04T00:30:00+00:00")

    def test_definition_hash_is_stable_and_content_sensitive(self) -> None:
        first = BusinessCalendar.from_dict(calendar_definition())
        second = BusinessCalendar.from_dict(calendar_definition())
        changed = BusinessCalendar.from_dict(calendar_definition(sla_grace_minutes=30))
        self.assertEqual(first.definition_sha256(), second.definition_sha256())
        self.assertNotEqual(first.definition_sha256(), changed.definition_sha256())

    def test_validation_rejects_bad_timezone_window_and_conflicts(self) -> None:
        weekdays = ("mon", "tue", "wed", "thu", "fri")
        with self.assertRaises(ValidationFailed):
            BusinessCalendar.from_dict(calendar_definition(timezone="Mars/Olympus"))
        with self.assertRaises(ValidationFailed):
            BusinessCalendar.from_dict(calendar_definition(
                windows=[{"weekday": day, "opens": "09:00", "closes": "09:00"} for day in weekdays]
            ))
        with self.assertRaises(ValidationFailed):
            BusinessCalendar.from_dict(calendar_definition(
                business_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                windows=[{"weekday": day, "opens": "08:00", "closes": "09:00"} for day in weekdays],
            ))  # sat/sun 营业却缺少窗口
        with self.assertRaises(ValidationFailed):
            BusinessCalendar.from_dict(calendar_definition(
                holidays=["2026-10-01"], extra_workdays=["2026-10-01"]
            ))

    def test_overnight_window_catches_after_midnight_arrival(self) -> None:
        # 周五 22:00 到周六 06:00 的跨午夜班次：周六凌晨 03:00Z（上海 11:00 不在此例，
        # 改用 UTC 终端）—— 构造 UTC 日历验证跨日窗口。
        calendar = BusinessCalendar.from_dict(calendar_definition(
            timezone="UTC",
            business_days=["fri"],
            windows=[{"weekday": "fri", "opens": "22:00", "closes": "06:00"}],
        ))
        # 2026-09-26 是周六 03:00Z，仍处于周五晚开始的窗口内。
        slot = calendar.next_acceptance(parse_utc("2026-09-26T03:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-09-25T22:00:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-09-26T06:00:00+00:00")
        self.assertEqual(slot.receivable_at.isoformat(), "2026-09-26T03:00:00+00:00")
        # 窗口关闭后要等到下周五。
        later = calendar.next_acceptance(parse_utc("2026-09-26T07:00:00Z"))
        self.assertEqual(later.opens_at.isoformat(), "2026-10-02T22:00:00+00:00")

    def test_overnight_window_spanning_fall_back_extra_hour(self) -> None:
        # 纽约 2026-11-01（周日）凌晨回拨；周六晚 22:00 到周日 02:30 的跨午夜窗口，
        # 终点取第二次 02:30（07:30Z），因此窗口持续 9.5 小时（含重复的一小时）。
        calendar = BusinessCalendar.from_dict(calendar_definition(
            timezone="America/New_York",
            business_days=["sat"],
            windows=[{"weekday": "sat", "opens": "22:00", "closes": "02:30"}],
        ))
        slot = calendar.next_acceptance(parse_utc("2026-11-01T02:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-11-01T02:00:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-11-01T07:30:00+00:00")

    def test_overnight_window_spanning_spring_forward_gap(self) -> None:
        # 纽约 2026-03-08（周日）02:00 跳 03:00；周六晚 23:00 到周日 02:30 的窗口，
        # 终点 02:30 不存在，前移到 03:30 EDT（07:30Z）。
        calendar = BusinessCalendar.from_dict(calendar_definition(
            timezone="America/New_York",
            business_days=["sat"],
            windows=[{"weekday": "sat", "opens": "23:00", "closes": "02:30"}],
        ))
        slot = calendar.next_acceptance(parse_utc("2026-03-08T07:00:00Z"))
        self.assertEqual(slot.opens_at.isoformat(), "2026-03-08T04:00:00+00:00")
        self.assertEqual(slot.closes_at.isoformat(), "2026-03-08T07:30:00+00:00")
        self.assertEqual(slot.receivable_at.isoformat(), "2026-03-08T07:00:00+00:00")


class CalendarServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "120000", "unit_cost_usd": "91", "received_at": "2026-09-24T06:00:00Z"})

    def tearDown(self) -> None:
        self.connection.close()

    def allocate_and_dispatch(self, transfer_id: str = "tr-1", nomination_id: str = "nom-1") -> dict[str, object]:
        self.service.submit_nomination("dispatch", {"nomination_id": nomination_id, "route_id": "pipe-a-b", "shipper_id": "s-1", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": f"key-{nomination_id}"})
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        return self.service.dispatch_transfer("dispatch", transfer_id, nomination_id, "lot-1", 2)

    def test_timezone_must_match_destination_facility(self) -> None:
        with self.assertRaises(Conflict):
            self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition(timezone="UTC"))

    def test_dispatcher_cannot_configure_calendar(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.configure_route_calendar("dispatch", "pipe-a-b", calendar_definition())

    def test_dispatch_freezes_calendar_and_later_revision_keeps_commitment(self) -> None:
        first = self.service.configure_route_calendar(
            "plan", "pipe-a-b", calendar_definition(sla_grace_minutes=120), "初始版本"
        )
        self.assertEqual(first["calendar_revision"], 1)
        transfer = self.allocate_and_dispatch()
        # 周四 08:00Z + 36h = 周六 04:00 上海，窗口推到周一 08:30（00:30Z），11:00Z 截止。
        self.assertEqual(transfer["expected_arrival"], "2026-09-28T00:30:00Z")
        self.assertEqual(transfer["committed_due_at"], "2026-09-28T11:00:00Z")
        self.assertEqual(transfer["calendar_revision"], 1)
        stored = self.service.transfer_status("tr-1")
        self.assertIsNotNone(stored["calendar_sha256"])
        self.assertIn("tzdata", stored["tzdata_version"])

        preview = self.service.preview_calendar_change("plan", "pipe-a-b", calendar_definition(
            business_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            windows=[
                *( {"weekday": d, "opens": "08:30", "closes": "17:00"} for d in ("mon", "tue", "wed", "thu", "fri") ),
                {"weekday": "sat", "opens": "06:00", "closes": "12:00"},
                {"weekday": "sun", "opens": "06:00", "closes": "12:00"},
            ],
            sla_grace_minutes=0,
        ))
        self.assertEqual(preview["changed_count"], 1)
        impact = preview["in_transit_impacts"][0]
        self.assertTrue(impact["changed"])
        # 候选日历周六即可接收，截止也更早。
        self.assertEqual(impact["projected"]["scheduled_acceptance_at"], "2026-09-25T22:00:00Z")

        # 正式修订为第 2 版；已发运承诺仍然按冻结的第 1 版。
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition(
            business_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            windows=[
                *( {"weekday": d, "opens": "08:30", "closes": "17:00"} for d in ("mon", "tue", "wed", "thu", "fri") ),
                {"weekday": "sat", "opens": "06:00", "closes": "12:00"},
                {"weekday": "sun", "opens": "06:00", "closes": "12:00"},
            ],
            sla_grace_minutes=0,
        ), "周末也收货")
        after = self.service.transfer_status("tr-1")
        self.assertEqual(after["calendar_revision"], 1)
        self.assertEqual(after["scheduled_acceptance_at"], "2026-09-28T00:30:00Z")
        self.assertEqual(after["committed_due_at"], "2026-09-28T11:00:00Z")
        revisions = self.service.calendar_revisions("audit", "pipe-a-b")["revisions"]
        self.assertEqual([row["calendar_revision"] for row in revisions], [1, 2])

    def test_identical_definition_does_not_create_revision(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
        with self.assertRaises(Conflict):
            self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())

    def test_partial_quality_pending_and_final_receipt_flow(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
        self.allocate_and_dispatch()
        nomination_before = self.connection.execute(
            "SELECT delivered_barrels FROM nominations WHERE nomination_id='nom-1'"
        ).fetchone()
        self.assertEqual(nomination_before["delivered_barrels"], "0")

        partial = self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "60000",
            "scan_code": "SCAN-1", "note": "首车",
        })
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["partial_received_barrels"], "60000.000")
        # 部分到货和质量待验都不得结转提名。
        self.assertEqual(partial["nomination"]["delivered_barrels"], "0")

        quality = self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "quality_pending", "quantity_barrels": "19800",
            "scan_code": "SCAN-2",
        })
        self.assertEqual(quality["state"], "quality_hold")
        self.assertEqual(quality["quality_pending_barrels"], "19800.000")
        self.assertEqual(quality["nomination"]["delivered_barrels"], "0")

        # 重复扫描：原样返回，不累计第二次。
        replay = self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "60000",
            "scan_code": "SCAN-1",
        })
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["partial_received_barrels"], "60000.000")
        receipt_rows = self.connection.execute(
            "SELECT count(*) c FROM transfer_receipts WHERE scan_code='SCAN-1'"
        ).fetchone()
        self.assertEqual(receipt_rows["c"], 1)

        final = self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "final", "quantity_barrels": "79800",
            "scan_code": "SCAN-3",
        })
        self.assertEqual(final["state"], "completed")
        # 只有最终数量结转提名。
        self.assertEqual(final["nomination"]["state"], "delivered")
        self.assertEqual(final["nomination"]["delivered_barrels"], "79800.000")
        self.assertEqual(final["final_received_barrels"], "79800.000")

        with self.assertRaises(InvalidState):
            self.service.record_receipt("dispatch", {
                "transfer_id": "tr-1", "stage": "final", "quantity_barrels": "1",
                "scan_code": "SCAN-4",
            })

    def test_receipts_cannot_exceed_loaded_quantity(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
        self.allocate_and_dispatch()
        self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "70000", "scan_code": "S-1",
        })
        with self.assertRaises(Conflict):
            self.service.record_receipt("dispatch", {
                "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "10001", "scan_code": "S-2",
            })
        with self.assertRaises(Conflict):
            self.service.record_receipt("dispatch", {
                "transfer_id": "tr-1", "stage": "final", "quantity_barrels": "80001", "scan_code": "S-3",
            })

    def test_overdue_determination_and_grace_window(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition(sla_grace_minutes=120))
        self.allocate_and_dispatch()
        # 截止为周一 11:00Z（发运后 99 小时）。
        self.clock.advance(hours=98, minutes=59)  # 周一 10:59Z
        self.assertFalse(self.service.transfer_status("tr-1")["is_overdue"])
        self.clock.advance(minutes=2)  # 周一 11:01Z，超过截止
        overdue = self.service.transfer_status("tr-1")
        self.assertTrue(overdue["is_overdue"])
        self.assertEqual(overdue["state"], "overdue")
        self.assertIsNotNone(overdue["overdue_since"])

    def test_partial_receipt_then_overdue_keeps_partial_state(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
        self.allocate_and_dispatch()
        self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "1000", "scan_code": "S-1",
        })
        self.clock.advance(days=5)
        status = self.service.transfer_status("tr-1")
        self.assertEqual(status["state"], "partial")
        self.assertTrue(status["is_overdue"])

    def test_overdue_is_restart_safe_from_persisted_due_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "supply.sqlite3"
            connection = connect(database)
            service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
            for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
                service.create_user(user_id, user_id, role)
            service.create_facility("plan", {"facility_id": "field-a", "name": "油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
            service.create_facility("plan", {"facility_id": "terminal-b", "name": "终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
            service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 0, "transit_hours": 36})
            service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
            service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "120000", "unit_cost_usd": "91", "received_at": "2026-09-24T06:00:00Z"})
            service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pipe-a-b", "shipper_id": "s-1", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "key-1"})
            service.allocate("dispatch", "pipe-a-b", "2026-09-25")
            service.dispatch_transfer("dispatch", "tr-1", "nom-1", "lot-1", 2)
            connection.close()

            # 重启：新进程、新连接、新时钟实例，只凭数据库中的冻结承诺判定。
            restarted = connect(database)
            future = SupplyService(restarted, FrozenClock(datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)))
            sweep = future.sweep_overdue()
            self.assertEqual(sweep["marked_overdue"], 1)
            status = future.transfer_status("tr-1")
            self.assertTrue(status["is_overdue"])
            self.assertEqual(status["committed_due_at"], "2026-09-28T09:00:00Z")
            # 再次扫描是幂等的。
            self.assertEqual(future.sweep_overdue()["marked_overdue"], 0)
            restarted.close()

    def test_route_without_calendar_keeps_fixed_hours_fallback(self) -> None:
        transfer = self.allocate_and_dispatch("tr-fallback")
        self.assertIsNone(transfer["calendar_revision"])
        self.assertEqual(transfer["expected_arrival"], "2026-09-25T20:00:00Z")

    def test_migration_from_legacy_transfers_table(self) -> None:
        # 旧 transfers 行需要满足新表外键：先备好提名和批次。
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-legacy", "route_id": "pipe-a-b", "shipper_id": "s-9", "service_date": "2026-09-25", "requested_barrels": "10", "priority": 10, "idempotency_key": "key-legacy"})
        # 模拟旧库：删除新表并按旧结构重建，写入一行后重新初始化。
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute("DROP TABLE transfer_receipts")
        self.connection.execute("DROP TABLE transfers")
        self.connection.execute(
            "CREATE TABLE transfers (transfer_id TEXT PRIMARY KEY,nomination_id TEXT, "
            "inventory_lot_id TEXT,loaded_barrels TEXT,expected_delivered_barrels TEXT, "
            "departed_at TEXT,arrived_at TEXT,state TEXT DEFAULT 'in_transit',revision INTEGER DEFAULT 1, "
            "created_by TEXT,created_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
            "expected_delivered_barrels,departed_at,state,revision,created_by,created_at) "
            "VALUES('legacy-1','nom-legacy','lot-1','10','10','2026-09-24T08:00:00Z','in_transit',1,'dispatch','2026-09-24T08:00:00Z')"
        )
        self.connection.execute("PRAGMA foreign_keys=ON")
        SupplyService(self.connection, self.clock)  # 重新初始化触发迁移
        row = self.connection.execute("SELECT calendar_revision,committed_due_at,is_overdue,state FROM transfers WHERE transfer_id='legacy-1'").fetchone()
        self.assertIsNone(row["calendar_revision"])
        self.assertIsNone(row["committed_due_at"])
        self.assertEqual(row["is_overdue"], 0)
        self.assertEqual(row["state"], "in_transit")

    def test_api_routes_for_calendar_and_receipts(self) -> None:
        app = JsonApplication(self.service)
        response = app.handle("PUT", "/routes/pipe-a-b/calendar", {"X-Actor-Id": "plan"},
                              json.dumps(calendar_definition()).encode())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["calendar_revision"], 1)
        fetched = app.handle("GET", "/routes/pipe-a-b/calendar", {"X-Actor-Id": "plan"})
        self.assertEqual(fetched.status, 200)
        revisions = app.handle("GET", "/routes/pipe-a-b/calendar/revisions", {"X-Actor-Id": "plan"})
        self.assertEqual(revisions.status, 200)
        self.assertEqual(len(revisions.body["revisions"]), 1)
        preview = app.handle("POST", "/routes/pipe-a-b/calendar/preview", {"X-Actor-Id": "plan"},
                             json.dumps(calendar_definition(sla_grace_minutes=45)).encode())
        self.assertEqual(preview.status, 200)
        self.assertEqual(preview.body["current_revision"], 1)
        forbidden = app.handle("PUT", "/routes/pipe-a-b/calendar", {"X-Actor-Id": "dispatch"},
                               json.dumps(calendar_definition(sla_grace_minutes=5)).encode())
        self.assertEqual(forbidden.status, 403)
        self.allocate_and_dispatch()
        receipt = app.handle("POST", "/transfers/receipts", {"X-Actor-Id": "dispatch"},
                             json.dumps({"transfer_id": "tr-1", "stage": "final", "quantity_barrels": "79800", "scan_code": "API-1"}).encode())
        self.assertEqual(receipt.status, 201)
        status = app.handle("GET", "/transfers/tr-1", {"X-Actor-Id": "dispatch"})
        self.assertEqual(status.body["state"], "completed")
        missing = app.handle("GET", "/transfers/unknown", {"X-Actor-Id": "dispatch"})
        self.assertEqual(missing.status, 404)

    def test_scan_code_reused_on_other_transfer_conflicts(self) -> None:
        self.service.configure_route_calendar("plan", "pipe-a-b", calendar_definition())
        self.allocate_and_dispatch("tr-1", "nom-1")
        self.service.record_receipt("dispatch", {
            "transfer_id": "tr-1", "stage": "partial", "quantity_barrels": "100", "scan_code": "SHARED",
        })
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-2", "route_id": "pipe-a-b", "shipper_id": "s-2", "service_date": "2026-09-26", "requested_barrels": "100", "priority": 20, "idempotency_key": "key-nom-2"})
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-26")
        self.service.dispatch_transfer("dispatch", "tr-2", "nom-2", "lot-1", 2)
        with self.assertRaises(Conflict):
            self.service.record_receipt("dispatch", {
                "transfer_id": "tr-2", "stage": "partial", "quantity_barrels": "100", "scan_code": "SHARED",
            })
        # 原单数量没有受影响。
        self.assertEqual(self.service.transfer_status("tr-1")["partial_received_barrels"], "100.000")
        self.assertEqual(self.service.transfer_status("tr-2")["partial_received_barrels"], "0.000")


if __name__ == "__main__":
    unittest.main()
