"""基于 IANA 时区的营业日、交接窗口与节假日计算。

日历只描述终端当地的可接收规则：哪些工作日营业、每天的交接窗口、
节假日例外和调休上班日。所有对外结果都转换为 UTC 时刻，夏令时缺口与
重复时刻通过 PEP 495 的 ``fold`` 规则确定性处理：

- 春季缺口（不存在的本地时刻）：窗口起点按 ``fold=0`` 解析，等价于
  缺口结束后的第一个真实时刻；
- 秋季重复（同一本地时刻出现两次）：窗口起点按 ``fold=0``（第一次，
  夏令时）、终点按 ``fold=1``（第二次，标准时）解析，因此跨越重复
  小时的窗口物理上覆盖两次本地小时；
- 窗口区间为左闭右开 ``[opens, closes)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationFailed
from .planning import canonical_json, digest

UTC = timezone.utc

WEEKDAY_CODES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
WEEKDAY_NAMES = {value: key for key, value in WEEKDAY_CODES.items()}

# 向前搜索营业日的保护上限，覆盖最长的新年/春节连休。
MAX_SEARCH_DAYS = 400


@dataclass(frozen=True, slots=True)
class Window:
    weekday: int
    opens: time
    closes: time
    overnight: bool = False


@dataclass(frozen=True, slots=True)
class Slot:
    """某一营业日交接窗口换算到 UTC 后的时间区间。"""

    opens_at: datetime
    closes_at: datetime
    receivable_at: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "opens_at": _utc_text(self.opens_at),
            "closes_at": _utc_text(self.closes_at),
            "receivable_at": _utc_text(self.receivable_at),
        }


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    timezone_name: str
    tzinfo: ZoneInfo
    business_days: frozenset[int]
    windows: tuple[Window, ...]
    holidays: frozenset[date]
    extra_workdays: frozenset[date]
    sla_grace_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BusinessCalendar":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("日历定义必须是对象")
        timezone_name = str(raw.get("timezone", "")).strip()
        if not timezone_name:
            raise ValidationFailed("timezone 不能为空")
        try:
            tzinfo = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationFailed("timezone 不是有效的 IANA 时区") from exc

        days_value = raw.get("business_days")
        if not isinstance(days_value, list) or not days_value:
            raise ValidationFailed("business_days 必须是非空星期缩写数组")
        days: set[int] = set()
        for item in days_value:
            code = str(item).strip().lower()
            if code not in WEEKDAY_CODES:
                raise ValidationFailed("business_days 只接受 mon 到 sun 缩写")
            if WEEKDAY_CODES[code] in days:
                raise ValidationFailed(f"business_days 中 {code} 重复")
            days.add(WEEKDAY_CODES[code])

        windows_value = raw.get("windows")
        if not isinstance(windows_value, list) or not windows_value:
            raise ValidationFailed("windows 必须是非空交接窗口数组")
        windows: list[Window] = []
        for item in windows_value:
            if not isinstance(item, Mapping):
                raise ValidationFailed("交接窗口必须是对象")
            code = str(item.get("weekday", "")).strip().lower()
            if code not in WEEKDAY_CODES:
                raise ValidationFailed("窗口 weekday 只接受 mon 到 sun 缩写")
            weekday = WEEKDAY_CODES[code]
            opens = _parse_clock(item.get("opens"), "opens")
            closes = _parse_clock(item.get("closes"), "closes")
            if closes == opens:
                raise ValidationFailed("交接窗口 closes 不能等于 opens")
            overnight = closes < opens  # 跨午夜班次，例如 22:00 到次日 06:00
            windows.append(Window(weekday, opens, closes, overnight))
        weekdays_with_window = {w.weekday for w in windows}
        missing = days - weekdays_with_window
        if missing:
            names = "、".join(WEEKDAY_NAMES[i] for i in sorted(missing))
            raise ValidationFailed(f"营业日 {names} 缺少交接窗口")

        holidays = _parse_dates(raw.get("holidays", []), "holidays")
        extra_workdays = _parse_dates(raw.get("extra_workdays", []), "extra_workdays")
        overlap = holidays & extra_workdays
        if overlap:
            raise ValidationFailed("同一日期不能既是节假日又是调休上班日")

        grace = raw.get("sla_grace_minutes", 0)
        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
            raise ValidationFailed("sla_grace_minutes 必须是非负整数")

        return cls(
            timezone_name=timezone_name,
            tzinfo=tzinfo,
            business_days=frozenset(days),
            windows=tuple(sorted(windows, key=lambda w: (w.weekday, w.opens, w.closes))),
            holidays=frozenset(holidays),
            extra_workdays=frozenset(extra_workdays),
            sla_grace_minutes=grace,
        )

    def to_definition(self) -> dict[str, Any]:
        """规范化定义：哈希、版本冻结与差异比较都以它为准。"""
        return {
            "timezone": self.timezone_name,
            "business_days": [WEEKDAY_NAMES[i] for i in sorted(self.business_days)],
            "windows": [
                {
                    "weekday": WEEKDAY_NAMES[w.weekday],
                    "opens": _clock_text(w.opens),
                    "closes": _clock_text(w.closes),
                }
                for w in self.windows
            ],
            "holidays": [day.isoformat() for day in sorted(self.holidays)],
            "extra_workdays": [day.isoformat() for day in sorted(self.extra_workdays)],
            "sla_grace_minutes": self.sla_grace_minutes,
        }

    def definition_json(self) -> str:
        return canonical_json(self.to_definition())

    def definition_sha256(self) -> str:
        return digest(self.to_definition())

    def is_open_day(self, day: date) -> bool:
        if day in self.extra_workdays:
            return True
        if day in self.holidays:
            return False
        return day.weekday() in self.business_days

    def _windows_on(self, day: date) -> Sequence[Window]:
        return tuple(w for w in self.windows if w.weekday == day.weekday())

    def next_acceptance(self, earliest_utc: datetime) -> Slot:
        """返回不早于 ``earliest_utc`` 的下一可接收时刻所在窗口。

        若 ``earliest_utc`` 已落在某个交接窗口内，则该时刻本身即可接收。
        """
        if earliest_utc.tzinfo is None:
            raise ValueError("最早到达时刻必须带时区")
        earliest = earliest_utc.astimezone(UTC)
        local_date = earliest.astimezone(self.tzinfo).date()
        # 提前一天起步：当地凌晨可能仍处于前一营业日开始的跨午夜窗口内。
        cursor_day = local_date - timedelta(days=1)
        for offset in range(MAX_SEARCH_DAYS + 1):
            day = cursor_day + timedelta(days=offset)
            if not self.is_open_day(day):
                continue
            for window in self._windows_on(day):
                close_day = day + timedelta(days=1) if window.overnight else day
                opens_at, closes_at = _slot_bounds(day, close_day, window, self.tzinfo)
                if closes_at <= earliest:
                    continue
                receivable = max(earliest, opens_at)
                return Slot(opens_at, closes_at, receivable)
        raise ValidationFailed("日历在未来 400 天内没有可接收窗口")


def _parse_clock(value: object, field: str) -> time:
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是 HH:MM 本地时间")
    text_value = value.strip()
    parsed: time | None = None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text_value, fmt).time()
        except ValueError:
            continue
        break
    if parsed is None:
        raise ValidationFailed(f"{field} 必须是 HH:MM 本地时间")
    return parsed


def _parse_dates(value: object, field: str) -> frozenset[date]:
    if value in (None, []):
        return frozenset()
    if not isinstance(value, list):
        raise ValidationFailed(f"{field} 必须是日期数组")
    result: set[date] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValidationFailed(f"{field} 只接受 YYYY-MM-DD 日期")
        try:
            parsed = date.fromisoformat(item.strip())
        except ValueError as exc:
            raise ValidationFailed(f"{field} 只接受 YYYY-MM-DD 日期") from exc
        if parsed in result:
            raise ValidationFailed(f"{field} 中 {parsed.isoformat()} 重复")
        result.add(parsed)
    return frozenset(result)


def _wall_to_utc(day: date, wall: time, tzinfo: ZoneInfo, *, fold: int) -> datetime:
    aware = datetime(
        day.year,
        day.month,
        day.day,
        wall.hour,
        wall.minute,
        wall.second,
        fold=fold,
        tzinfo=tzinfo,
    )
    return aware.astimezone(UTC)


def _wall_exists(day: date, wall: time, tzinfo: ZoneInfo) -> bool:
    """本地挂钟时刻是否真实存在（春季缺口内的时刻往返换算后对不上）。"""
    aware = datetime(day.year, day.month, day.day, wall.hour, wall.minute, wall.second, tzinfo=tzinfo)
    roundtrip = aware.astimezone(UTC).astimezone(tzinfo).replace(tzinfo=None)
    return roundtrip == aware.replace(tzinfo=None)


def _slot_bounds(
    open_day: date,
    close_day: date,
    window: Window,
    tzinfo: ZoneInfo,
) -> tuple[datetime, datetime]:
    # 起点：缺口时刻用 fold=0 落到跳时后的第一个真实瞬间；重复时刻取第一次。
    opens_at = _wall_to_utc(open_day, window.opens, tzinfo, fold=0)
    # 终点：缺口时刻同样前移；重复时刻取第二次（fold=1），跨重复小时的
    # 窗口因此物理上覆盖两次本地小时。
    close_fold = 0 if not _wall_exists(close_day, window.closes, tzinfo) else 1
    closes_at = _wall_to_utc(close_day, window.closes, tzinfo, fold=close_fold)
    return opens_at, closes_at


def _clock_text(value: time) -> str:
    if value.second:
        return value.strftime("%H:%M:%S")
    return value.strftime("%H:%M")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def tzdata_version() -> str:
    """标识运行时使用的时区数据库，随日历版本一并冻结。"""
    try:
        return "pypi-tzdata==" + version("tzdata")
    except PackageNotFoundError:
        pass
    for candidate in (Path("/usr/share/zoneinfo/+VERSION"), Path("/usr/lib/zoneinfo/+VERSION")):
        try:
            text_value = candidate.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if text_value:
            return "system-tzdata==" + text_value
    return "system-tzdata=unknown"
