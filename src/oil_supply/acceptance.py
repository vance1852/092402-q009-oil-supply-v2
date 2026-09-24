"""贯通报价、线路、库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    calendar = service.configure_route_calendar("plan", "pipe-a-b", {
        "timezone": "Asia/Shanghai",
        "business_days": ["mon", "tue", "wed", "thu", "fri"],
        "windows": [{"weekday": "mon", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "tue", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "wed", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "thu", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "fri", "opens": "08:30", "closes": "17:00"}],
        "holidays": ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"],
        "extra_workdays": [],
        "sla_grace_minutes": 120,
    }, "国庆假期交接窗口")
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    preview = service.preview_calendar_change("plan", "pipe-a-b", {
        "timezone": "Asia/Shanghai",
        "business_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
        "windows": [{"weekday": "mon", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "tue", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "wed", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "thu", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "fri", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "sat", "opens": "09:00", "closes": "12:00"}],
        "holidays": [],
        "extra_workdays": [],
        "sla_grace_minutes": 60,
    })
    # 日历后来修订（取消国庆假期）；在途承诺保持发运时冻结的版本不变。
    service.configure_route_calendar("plan", "pipe-a-b", {
        "timezone": "Asia/Shanghai",
        "business_days": ["mon", "tue", "wed", "thu", "fri"],
        "windows": [{"weekday": "mon", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "tue", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "wed", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "thu", "opens": "08:30", "closes": "17:00"},
                    {"weekday": "fri", "opens": "08:30", "closes": "17:00"}],
        "holidays": [],
        "extra_workdays": [],
        "sla_grace_minutes": 120,
    }, "取消国庆例外")
    partial = service.record_receipt("dispatch", {"transfer_id": "transfer-001", "stage": "partial", "quantity_barrels": "60000", "scan_code": "SCAN-0001", "note": "首车部分到货"})
    replayed = service.record_receipt("dispatch", {"transfer_id": "transfer-001", "stage": "partial", "quantity_barrels": "60000", "scan_code": "SCAN-0001", "note": "首车部分到货"})
    quality = service.record_receipt("dispatch", {"transfer_id": "transfer-001", "stage": "quality_pending", "quantity_barrels": "15000", "scan_code": "SCAN-0002", "note": "尾车质量待验"})
    final = service.record_receipt("dispatch", {"transfer_id": "transfer-001", "stage": "final", "quantity_barrels": "79800", "scan_code": "SCAN-0003", "note": "最终签收"})
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "calendar_revision": calendar["calendar_revision"], "transfer": transfer, "calendar_preview_changed": preview["changed_count"], "partial_received": partial["partial_received_barrels"], "duplicate_scan_replayed": replayed["idempotent_replay"], "quality_pending": quality["quality_pending_barrels"], "final_received": final["final_received_barrels"], "nomination_delivered": final["nomination"]["delivered_barrels"], "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
