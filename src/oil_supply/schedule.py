"""线路接收日历与 IANA 时区时刻计算。

转运的预计到达不再只是 UTC 离港时间加固定小时数：终端按当地营业日、
交接窗口和节假日例外接收货物。本模块把到达时刻映射到线路目的地时区，
找出下一个可接收时刻。夏令时前拨造成的缺失本地时刻顺延到下一个存在
的分钟，回拨造成的重复本地时刻取第一次出现，保证结果确定且可重放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationFailed


WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# 下一可接收时刻的搜索上界：覆盖跨年、长假期和整年关闭的误配置。
MAX_SEARCH_DAYS = 400
# 缺失本地时刻顺延的搜索上界，覆盖跨日界线这类 24 小时跳变。
MAX_GAP_MINUTES = 26 * 60


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed(f"timezone 不是可用的 IANA 时区: {name}") from exc


def resolve_local(naive: datetime, tz: ZoneInfo) -> datetime:
    """把本地墙钟时间映射为确定的时刻。

    重复时刻（夏令时回拨）取第一次出现；缺失时刻（夏令时前拨或跨日界
    线）顺延到下一个真实存在的本地分钟。
    """
    if naive.tzinfo is not None:
        raise ValueError("本地时间不能带时区")
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=tz, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(tz)
        if round_trip.replace(tzinfo=None) == naive and round_trip.fold == fold:
            return candidate
    probe = naive
    for _ in range(MAX_GAP_MINUTES):
        probe += timedelta(minutes=1)
        candidate = probe.replace(tzinfo=tz, fold=0)
        round_trip = candidate.astimezone(timezone.utc).astimezone(tz)
        if round_trip.replace(tzinfo=None) == probe:
            return candidate
    raise ValidationFailed("本地时刻在时区中无法解析")


@dataclass(frozen=True, slots=True)
class HandoverWindow:
    """交接窗口；end 不晚于 start 时表示跨午夜到次日结束。"""

    start: time
    end: time


@dataclass(frozen=True, slots=True)
class RouteCalendar:
    """线路目的地的接收日历。"""

    timezone: str
    business_days: frozenset[int]  # 0=周一 … 6=周日
    windows: tuple[HandoverWindow, ...]
    closed_dates: frozenset[date]  # 节假日等例外关闭日
    open_dates: frozenset[date]  # 例外开放日，优先于每周规则

    def is_business_day(self, day: date) -> bool:
        if day in self.open_dates:
            return True
        if day in self.closed_dates:
            return False
        return day.weekday() in self.business_days

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "business_days": [WEEKDAY_CODES[day] for day in sorted(self.business_days)],
            "windows": [
                {"start": window.start.isoformat(timespec="minutes"), "end": window.end.isoformat(timespec="minutes")}
                for window in self.windows
            ],
            "closed_dates": [day.isoformat() for day in sorted(self.closed_dates)],
            "open_dates": [day.isoformat() for day in sorted(self.open_dates)],
        }

    @classmethod
    def from_snapshot(cls, raw: Mapping[str, Any]) -> "RouteCalendar":
        timezone_name = str(raw["timezone"])
        load_timezone(timezone_name)
        return cls(
            timezone=timezone_name,
            business_days=frozenset(WEEKDAY_CODES.index(code) for code in raw["business_days"]),
            windows=tuple(
                HandoverWindow(time.fromisoformat(item["start"]), time.fromisoformat(item["end"]))
                for item in raw["windows"]
            ),
            closed_dates=frozenset(date.fromisoformat(day) for day in raw["closed_dates"]),
            open_dates=frozenset(date.fromisoformat(day) for day in raw["open_dates"]),
        )


def _window_intervals(calendar: RouteCalendar, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """生成某日各交接窗口的 UTC 区间，跨午夜窗口的结束时刻落在次日。"""
    intervals: list[tuple[datetime, datetime]] = []
    for window in calendar.windows:
        start_local = resolve_local(datetime.combine(day, window.start), tz)
        end_day = day if window.end > window.start else day + timedelta(days=1)
        end_local = resolve_local(datetime.combine(end_day, window.end), tz)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        if end_utc <= start_utc:
            raise ValidationFailed("交接窗口在时区换算后没有正时长")
        intervals.append((start_utc, end_utc))
    return intervals


def next_receivable(calendar: RouteCalendar, instant: datetime) -> datetime:
    """返回 instant（含）之后第一个可以接收货物的时刻（UTC）。

    到达时刻落在交接窗口内即可立即接收；否则顺延到本日下一个窗口，
    再不行就推到下一个营业日。跨午夜窗口属于它的开始日，前一日的夜
    班窗口可以覆盖当日凌晨。
    """
    if instant.tzinfo is None:
        raise ValueError("时刻必须带时区")
    moment = instant.astimezone(timezone.utc)
    tz = load_timezone(calendar.timezone)
    local_date = moment.astimezone(tz).date()
    day = local_date - timedelta(days=1)
    deadline = local_date + timedelta(days=MAX_SEARCH_DAYS)
    while day <= deadline:
        if calendar.is_business_day(day):
            for start_utc, end_utc in _window_intervals(calendar, day, tz):
                if moment <= start_utc:
                    return start_utc
                if moment <= end_utc:
                    return moment
        day += timedelta(days=1)
    raise ValidationFailed("线路日历在可预见的范围内没有可接收窗口")
