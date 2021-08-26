"""
Adapters for date/time types.
"""

# Copyright (C) 2020-2021 The Psycopg Team

import re
import sys
import struct
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, cast, Optional, Tuple, TYPE_CHECKING

from .. import postgres
from ..pq import Format
from .._tz import get_tzinfo
from ..abc import AdaptContext, DumperKey
from ..adapt import Buffer, Dumper, Loader, PyFormat
from ..errors import InterfaceError, DataError
from .._struct import pack_int4, pack_int8, unpack_int4, unpack_int8

if TYPE_CHECKING:
    from ..connection import BaseConnection

_struct_timetz = struct.Struct("!qi")  # microseconds, sec tz offset
_pack_timetz = cast(Callable[[int, int], bytes], _struct_timetz.pack)
_unpack_timetz = cast(
    Callable[[bytes], Tuple[int, int]], _struct_timetz.unpack
)

_struct_interval = struct.Struct("!qii")  # microseconds, days, months
_pack_interval = cast(Callable[[int, int, int], bytes], _struct_interval.pack)
_unpack_interval = cast(
    Callable[[bytes], Tuple[int, int, int]], _struct_interval.unpack
)

utc = timezone.utc
_pg_date_epoch_days = date(2000, 1, 1).toordinal()
_pg_datetime_epoch = datetime(2000, 1, 1)
_pg_datetimetz_epoch = datetime(2000, 1, 1, tzinfo=utc)
_py_date_min_days = date.min.toordinal()


class DateDumper(Dumper):

    format = Format.TEXT
    oid = postgres.types["date"].oid

    def dump(self, obj: date) -> bytes:
        # NOTE: whatever the PostgreSQL DateStyle input format (DMY, MDY, YMD)
        # the YYYY-MM-DD is always understood correctly.
        return str(obj).encode("utf8")


class DateBinaryDumper(Dumper):

    format = Format.BINARY
    oid = postgres.types["date"].oid

    def dump(self, obj: date) -> bytes:
        days = obj.toordinal() - _pg_date_epoch_days
        return pack_int4(days)


class _BaseTimeDumper(Dumper):
    def get_key(self, obj: time, format: PyFormat) -> DumperKey:
        # Use (cls,) to report the need to upgrade to a dumper for timetz (the
        # Frankenstein of the data types).
        if not obj.tzinfo:
            return self.cls
        else:
            return (self.cls,)

    def upgrade(self, obj: time, format: PyFormat) -> Dumper:
        raise NotImplementedError


class _BaseTimeTextDumper(_BaseTimeDumper):

    format = Format.TEXT

    def dump(self, obj: time) -> bytes:
        return str(obj).encode("utf8")


class TimeDumper(_BaseTimeTextDumper):

    oid = postgres.types["time"].oid

    def upgrade(self, obj: time, format: PyFormat) -> Dumper:
        if not obj.tzinfo:
            return self
        else:
            return TimeTzDumper(self.cls)


class TimeTzDumper(_BaseTimeTextDumper):

    oid = postgres.types["timetz"].oid


class TimeBinaryDumper(_BaseTimeDumper):

    format = Format.BINARY
    oid = postgres.types["time"].oid

    def dump(self, obj: time) -> bytes:
        us = obj.microsecond + 1_000_000 * (
            obj.second + 60 * (obj.minute + 60 * obj.hour)
        )
        return pack_int8(us)

    def upgrade(self, obj: time, format: PyFormat) -> Dumper:
        if not obj.tzinfo:
            return self
        else:
            return TimeTzBinaryDumper(self.cls)


class TimeTzBinaryDumper(_BaseTimeDumper):

    format = Format.BINARY
    oid = postgres.types["timetz"].oid

    def dump(self, obj: time) -> bytes:
        us = obj.microsecond + 1_000_000 * (
            obj.second + 60 * (obj.minute + 60 * obj.hour)
        )
        off = obj.utcoffset()
        assert off is not None
        return _pack_timetz(us, -int(off.total_seconds()))


class _BaseDatetimeDumper(Dumper):
    def get_key(self, obj: datetime, format: PyFormat) -> DumperKey:
        # Use (cls,) to report the need to upgrade (downgrade, actually) to a
        # dumper for naive timestamp.
        if obj.tzinfo:
            return self.cls
        else:
            return (self.cls,)

    def upgrade(self, obj: datetime, format: PyFormat) -> Dumper:
        raise NotImplementedError


class _BaseDatetimeTextDumper(_BaseDatetimeDumper):

    format = Format.TEXT

    def dump(self, obj: datetime) -> bytes:
        # NOTE: whatever the PostgreSQL DateStyle input format (DMY, MDY, YMD)
        # the YYYY-MM-DD is always understood correctly.
        return str(obj).encode("utf8")


class DatetimeDumper(_BaseDatetimeTextDumper):

    oid = postgres.types["timestamptz"].oid

    def upgrade(self, obj: datetime, format: PyFormat) -> Dumper:
        if obj.tzinfo:
            return self
        else:
            return DatetimeNoTzDumper(self.cls)


class DatetimeNoTzDumper(_BaseDatetimeTextDumper):

    oid = postgres.types["timestamp"].oid


class DatetimeBinaryDumper(_BaseDatetimeDumper):

    format = Format.BINARY
    oid = postgres.types["timestamptz"].oid

    def dump(self, obj: datetime) -> bytes:
        delta = obj - _pg_datetimetz_epoch
        micros = delta.microseconds + 1_000_000 * (
            86_400 * delta.days + delta.seconds
        )
        return pack_int8(micros)

    def upgrade(self, obj: datetime, format: PyFormat) -> Dumper:
        if obj.tzinfo:
            return self
        else:
            return DatetimeNoTzBinaryDumper(self.cls)


class DatetimeNoTzBinaryDumper(_BaseDatetimeDumper):

    format = Format.BINARY
    oid = postgres.types["timestamp"].oid

    def dump(self, obj: datetime) -> bytes:
        delta = obj - _pg_datetime_epoch
        micros = delta.microseconds + 1_000_000 * (
            86_400 * delta.days + delta.seconds
        )
        return pack_int8(micros)


class TimedeltaDumper(Dumper):

    format = Format.TEXT
    oid = postgres.types["interval"].oid

    def __init__(self, cls: type, context: Optional[AdaptContext] = None):
        super().__init__(cls, context)
        if self.connection:
            if (
                self.connection.pgconn.parameter_status(b"IntervalStyle")
                == b"sql_standard"
            ):
                setattr(self, "dump", self._dump_sql)

    def dump(self, obj: timedelta) -> bytes:
        return str(obj).encode("utf8")

    def _dump_sql(self, obj: timedelta) -> bytes:
        # sql_standard format needs explicit signs
        # otherwise -1 day 1 sec will mean -1 sec
        return b"%+d day %+d second %+d microsecond" % (
            obj.days,
            obj.seconds,
            obj.microseconds,
        )


class TimedeltaBinaryDumper(Dumper):

    format = Format.BINARY
    oid = postgres.types["interval"].oid

    def dump(self, obj: timedelta) -> bytes:
        micros = 1_000_000 * obj.seconds + obj.microseconds
        return _pack_interval(micros, obj.days, 0)


class DateLoader(Loader):

    format = Format.TEXT

    _ORDER_YMD = 0
    _ORDER_DMY = 1
    _ORDER_MDY = 2

    def __init__(self, oid: int, context: Optional[AdaptContext] = None):
        super().__init__(oid, context)
        ds = _get_datestyle(self.connection)
        if ds.startswith(b"I"):  # ISO
            self._order = self._ORDER_YMD
        elif ds.startswith(b"G"):  # German
            self._order = self._ORDER_DMY
        elif ds.startswith(b"S") or ds.startswith(b"P"):  # SQL or Postgres
            self._order = (
                self._ORDER_DMY if ds.endswith(b"DMY") else self._ORDER_MDY
            )
        else:
            raise InterfaceError(f"unexpected DateStyle: {ds.decode('ascii')}")

    def load(self, data: Buffer) -> date:
        if self._order == self._ORDER_YMD:
            ye = data[:4]
            mo = data[5:7]
            da = data[8:]
        elif self._order == self._ORDER_DMY:
            da = data[:2]
            mo = data[3:5]
            ye = data[6:]
        else:
            mo = data[:2]
            da = data[3:5]
            ye = data[6:]

        try:
            return date(int(ye), int(mo), int(da))
        except ValueError as e:
            s = bytes(data).decode("utf8", "replace")
            if len(s) != 10:
                raise DataError(f"date not supported: {s!r}") from None
            raise DataError(f"can't parse date {s!r}: {e}") from None


class DateBinaryLoader(Loader):

    format = Format.BINARY

    def load(self, data: Buffer) -> date:
        days = unpack_int4(data)[0] + _pg_date_epoch_days
        try:
            return date.fromordinal(days)
        except ValueError:
            if days < _py_date_min_days:
                raise DataError("date too small (before year 1)") from None
            else:
                raise DataError("date too large (after year 10K)") from None


class TimeLoader(Loader):

    format = Format.TEXT

    _re_format = re.compile(rb"^(\d+):(\d+):(\d+)(?:\.(\d+))?")

    def load(self, data: Buffer) -> time:
        m = self._re_format.match(data)
        if not m:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse time {s!r}")

        ho, mi, se, fr = m.groups()

        # Pad the fraction of second to get micros
        if fr:
            us = int(fr)
            if len(fr) < 6:
                us *= _uspad[len(fr)]
        else:
            us = 0

        try:
            return time(int(ho), int(mi), int(se), us)
        except ValueError as e:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse time {s!r}: {e}") from None


class TimeBinaryLoader(Loader):

    format = Format.BINARY

    def load(self, data: Buffer) -> time:
        val = unpack_int8(data)[0]
        val, us = divmod(val, 1_000_000)
        val, s = divmod(val, 60)
        h, m = divmod(val, 60)
        try:
            return time(h, m, s, us)
        except ValueError:
            raise DataError(
                f"time not supported by Python: hour={h}"
            ) from None


class TimetzLoader(Loader):

    format = Format.TEXT
    _py37 = sys.version_info >= (3, 7)

    _re_format = re.compile(
        rb"""(?ix)
        ^
        (\d+) : (\d+) : (\d+) (?: \. (\d+) )?       # Time and micros
        ([-+]) (\d+) (?: : (\d+) )? (?: : (\d+) )?  # Timezone
        $
        """
    )

    def load(self, data: Buffer) -> time:
        m = self._re_format.match(data)
        if not m:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse timetz {s!r}")

        ho, mi, se, fr, sgn, oh, om, os = m.groups()

        # Pad the fraction of second to get the micros
        if fr:
            us = int(fr)
            if len(fr) < 6:
                us *= _uspad[len(fr)]
        else:
            us = 0

        # Calculate timezone
        off = 60 * 60 * int(oh)
        if om:
            off += 60 * int(om)
        if os and self._py37:
            off += int(os)
        tz = timezone(timedelta(0, off if sgn == b"+" else -off))

        try:
            return time(int(ho), int(mi), int(se), us, tz)
        except ValueError as e:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse timetz {s!r}: {e}") from None


class TimetzBinaryLoader(Loader):

    format = Format.BINARY

    def load(self, data: Buffer) -> time:
        val, off = _unpack_timetz(data)

        val, us = divmod(val, 1_000_000)
        val, s = divmod(val, 60)
        h, m = divmod(val, 60)

        try:
            return time(h, m, s, us, self._tz_from_sec(off))
        except ValueError:
            raise DataError(
                f"time not supported by Python: hour={h}"
            ) from None

    def _tz_from_sec(self, sec: int) -> timezone:
        return timezone(timedelta(seconds=-sec))

    def _tz_from_sec_36(self, sec: int) -> timezone:
        if sec % 60:
            sec = round(sec / 60.0) * 60
        return timezone(timedelta(seconds=-sec))


if sys.version_info < (3, 7):
    setattr(
        TimetzBinaryLoader, "_tz_from_sec", TimetzBinaryLoader._tz_from_sec_36
    )


class TimestampLoader(Loader):

    format = Format.TEXT

    _re_format = re.compile(
        rb"""(?ix)
        ^
        (\d+) [^a-z0-9] (\d+) [^a-z0-9] (\d+)   # Date
        (?: T | [^a-z0-9] )                     # Separator, including T
        (\d+) [^a-z0-9] (\d+) [^a-z0-9] (\d+)   # Time
        (?: \.(\d+) )?                          # Micros
        $
        """
    )
    _re_format_pg = re.compile(
        rb"""(?ix)
        ^
        [a-z]+          [^a-z0-9]               # DoW, separator
        (\d+|[a-z]+)    [^a-z0-9]               # Month or day
        (\d+|[a-z]+)    [^a-z0-9]               # Month or day
        (\d+) [^a-z0-9] (\d+) [^a-z0-9] (\d+)   # Time
        (?: \.(\d+) )?                          # Micros
        [^a-z0-9] (\d+)                         # Year
        $
        """
    )

    _ORDER_YMD = 0
    _ORDER_DMY = 1
    _ORDER_MDY = 2
    _ORDER_PGDM = 3
    _ORDER_PGMD = 4

    def __init__(self, oid: int, context: Optional[AdaptContext] = None):
        super().__init__(oid, context)

        ds = _get_datestyle(self.connection)
        if ds.startswith(b"I"):  # ISO
            self._order = self._ORDER_YMD
        elif ds.startswith(b"G"):  # German
            self._order = self._ORDER_DMY
        elif ds.startswith(b"S"):  # SQL
            self._order = (
                self._ORDER_DMY if ds.endswith(b"DMY") else self._ORDER_MDY
            )
        elif ds.startswith(b"P"):  # Postgres
            self._order = (
                self._ORDER_PGDM if ds.endswith(b"DMY") else self._ORDER_PGMD
            )
            self._re_format = self._re_format_pg
        else:
            raise InterfaceError(f"unexpected DateStyle: {ds.decode('ascii')}")

    def load(self, data: Buffer) -> datetime:
        m = self._re_format.match(data)
        if not m:
            s = bytes(data).decode("utf8", "replace")
            if s.endswith("BC"):
                raise DataError(f"BC timestamps not supported, got {s!r}")
            raise DataError(f"can't parse timestamp {s!r}")

        if self._order == self._ORDER_YMD:
            ye, mo, da, ho, mi, se, fr = m.groups()
            imo = int(mo)
        elif self._order == self._ORDER_DMY:
            da, mo, ye, ho, mi, se, fr = m.groups()
            imo = int(mo)
        elif self._order == self._ORDER_MDY:
            mo, da, ye, ho, mi, se, fr = m.groups()
            imo = int(mo)
        else:
            if self._order == self._ORDER_PGDM:
                da, mo, ho, mi, se, fr, ye = m.groups()
            else:
                mo, da, ho, mi, se, fr, ye = m.groups()
            try:
                imo = _month_abbr[mo]
            except KeyError:
                s = mo.decode("utf8", "replace")
                raise DataError(f"can't parse month: {s!r}") from None

        # Pad the fraction of second to get the micros
        if fr:
            us = int(fr)
            if len(fr) < 6:
                us *= _uspad[len(fr)]
        else:
            us = 0

        try:
            return datetime(
                int(ye), imo, int(da), int(ho), int(mi), int(se), us
            )
        except ValueError as e:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse timestamp {s!r}: {e}") from None


class TimestampBinaryLoader(Loader):

    format = Format.BINARY

    def load(self, data: Buffer) -> datetime:
        micros = unpack_int8(data)[0]
        try:
            return _pg_datetime_epoch + timedelta(microseconds=micros)
        except OverflowError:
            if micros <= 0:
                raise DataError(
                    "timestamp too small (before year 1)"
                ) from None
            else:
                raise DataError(
                    "timestamp too large (after year 10K)"
                ) from None


class TimestamptzLoader(Loader):

    format = Format.TEXT
    _re_format = re.compile(
        rb"""(?ix)
        ^
        (\d+) [^a-z0-9] (\d+) [^a-z0-9] (\d+)       # Date
        (?: T | [^a-z0-9] )                         # Separator, including T
        (\d+) [^a-z0-9] (\d+) [^a-z0-9] (\d+)       # Time
        (?: \.(\d+) )?                              # Micros
        ([-+]) (\d+) (?: : (\d+) )? (?: : (\d+) )?  # Timezone
        $
        """
    )

    def __init__(self, oid: int, context: Optional[AdaptContext] = None):
        super().__init__(oid, context)
        self._timezone = get_tzinfo(
            self.connection.pgconn if self.connection else None
        )

        ds = _get_datestyle(self.connection)
        if not ds.startswith(b"I"):  # not ISO
            setattr(self, "load", self._load_notimpl)

    def load(self, data: Buffer) -> datetime:
        m = self._re_format.match(data)
        if not m:
            s = bytes(data).decode("utf8", "replace")
            if s.endswith("BC"):
                raise DataError(f"BC timestamps not supported, got {s!r}")
            raise DataError(f"can't parse timestamp {s!r}")

        ye, mo, da, ho, mi, se, fr, sgn, oh, om, os = m.groups()

        # Pad the fraction of second to get the micros
        if fr:
            us = int(fr)
            if len(fr) < 6:
                us *= _uspad[len(fr)]
        else:
            us = 0

        # Calculate timezone offset
        soff = 60 * 60 * int(oh)
        if om:
            soff += 60 * int(om)
        if os:
            soff += int(os)
        tzoff = timedelta(0, soff if sgn == b"+" else -soff)

        # The return value is a datetime with the timezone of the connection
        # (in order to be consistent with the binary loader, which is the only
        # thing it can return). So create a temporary datetime object, in utc,
        # shift it by the offset parsed from the timestamp, and then move it to
        # the connection timezone.
        dt = None
        ex: Exception
        try:
            dt = datetime(
                int(ye), int(mo), int(da), int(ho), int(mi), int(se), us, utc
            )
            return (dt - tzoff).astimezone(self._timezone)
        except OverflowError as e:
            # If we have created the temporary 'dt' it means that we have a
            # datetime close to max, the shift pushed it past max, overflowing.
            # In this case return the datetime in a fixed offset timezone.
            if dt is not None:
                return dt.replace(tzinfo=timezone(tzoff))
            else:
                ex = e
        except ValueError as e:
            ex = e

        s = bytes(data).decode("utf8", "replace")
        raise DataError(f"can't parse timestamptz {s!r}: {ex}") from None

    def _load_notimpl(self, data: Buffer) -> datetime:
        s = bytes(data).decode("utf8", "replace")
        ds = _get_datestyle(self.connection).decode("ascii")
        raise NotImplementedError(
            f"can't parse timestamptz with DateStyle {ds!r}: {s!r}"
        )


class TimestamptzBinaryLoader(Loader):

    format = Format.BINARY

    def __init__(self, oid: int, context: Optional[AdaptContext] = None):
        super().__init__(oid, context)
        self._timezone = get_tzinfo(
            self.connection.pgconn if self.connection else None
        )

    def load(self, data: Buffer) -> datetime:
        micros = unpack_int8(data)[0]
        try:
            ts = _pg_datetimetz_epoch + timedelta(microseconds=micros)
            return ts.astimezone(self._timezone)
        except OverflowError:
            # If we were asked about a timestamp which would overflow in UTC,
            # but not in the desired timezone (e.g. datetime.max at Chicago
            # timezone) we can still save the day by shifting the value by the
            # timezone offset and then replacing the timezone.
            if self._timezone:
                utcoff = self._timezone.utcoffset(
                    datetime.min if micros < 0 else datetime.max
                )
                if utcoff:
                    usoff = 1_000_000 * int(utcoff.total_seconds())
                    try:
                        ts = _pg_datetime_epoch + timedelta(
                            microseconds=micros + usoff
                        )
                    except OverflowError:
                        pass  # will raise downstream
                    else:
                        return ts.replace(tzinfo=self._timezone)

            if micros <= 0:
                raise DataError(
                    "timestamp too small (before year 1)"
                ) from None
            else:
                raise DataError(
                    "timestamp too large (after year 10K)"
                ) from None


class IntervalLoader(Loader):

    format = Format.TEXT

    _re_interval = re.compile(
        br"""
        (?: ([-+]?\d+) \s+ years? \s* )?                # Years
        (?: ([-+]?\d+) \s+ mons? \s* )?                 # Months
        (?: ([-+]?\d+) \s+ days? \s* )?                 # Days
        (?: ([-+])? (\d+) : (\d+) : (\d+ (?:\.\d+)?)    # Time
        )?
        """,
        re.VERBOSE,
    )

    def __init__(self, oid: int, context: Optional[AdaptContext] = None):
        super().__init__(oid, context)
        if self.connection:
            ints = self.connection.pgconn.parameter_status(b"IntervalStyle")
            if ints != b"postgres":
                setattr(self, "load", self._load_notimpl)

    def load(self, data: Buffer) -> timedelta:
        m = self._re_interval.match(data)
        if not m:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse interval {s!r}")

        ye, mo, da, sgn, ho, mi, se = m.groups()
        days = 0
        seconds = 0.0

        if ye:
            days += 365 * int(ye)
        if mo:
            days += 30 * int(mo)
        if da:
            days += int(da)

        if ho:
            seconds = 3600 * int(ho) + 60 * int(mi) + float(se)
            if sgn == b"-":
                seconds = -seconds

        try:
            return timedelta(days=days, seconds=seconds)
        except OverflowError as e:
            s = bytes(data).decode("utf8", "replace")
            raise DataError(f"can't parse interval {s!r}: {e}") from None

    def _load_notimpl(self, data: Buffer) -> timedelta:
        s = bytes(data).decode("utf8", "replace")
        ints = (
            self.connection
            and self.connection.pgconn.parameter_status(b"IntervalStyle")
            or b"unknown"
        ).decode("utf8", "replace")
        raise NotImplementedError(
            f"can't parse interval with IntervalStyle {ints}: {s!r}"
        )


class IntervalBinaryLoader(Loader):

    format = Format.BINARY

    def load(self, data: Buffer) -> timedelta:
        micros, days, months = _unpack_interval(data)
        if months > 0:
            years, months = divmod(months, 12)
            days = days + 30 * months + 365 * years
        elif months < 0:
            years, months = divmod(-months, 12)
            days = days - 30 * months - 365 * years
        return timedelta(days=days, microseconds=micros)


def _get_datestyle(conn: Optional["BaseConnection[Any]"]) -> bytes:
    if conn:
        ds = conn.pgconn.parameter_status(b"DateStyle")
        if ds:
            return ds

    return b"ISO, DMY"


_month_abbr = {
    n: i
    for i, n in enumerate(
        b"Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1
    )
}

# Pad to get microseconds from a fraction of seconds
_uspad = [0, 100_000, 10_000, 1_000, 100, 10, 1]


def register_default_adapters(context: AdaptContext) -> None:
    adapters = context.adapters
    adapters.register_dumper("datetime.date", DateDumper)
    adapters.register_dumper("datetime.date", DateBinaryDumper)
    adapters.register_dumper("datetime.time", TimeDumper)
    adapters.register_dumper("datetime.time", TimeBinaryDumper)
    adapters.register_dumper("datetime.datetime", DatetimeDumper)
    adapters.register_dumper("datetime.datetime", DatetimeBinaryDumper)
    adapters.register_dumper("datetime.timedelta", TimedeltaDumper)
    adapters.register_dumper("datetime.timedelta", TimedeltaBinaryDumper)
    adapters.register_loader("date", DateLoader)
    adapters.register_loader("date", DateBinaryLoader)
    adapters.register_loader("time", TimeLoader)
    adapters.register_loader("time", TimeBinaryLoader)
    adapters.register_loader("timetz", TimetzLoader)
    adapters.register_loader("timetz", TimetzBinaryLoader)
    adapters.register_loader("timestamp", TimestampLoader)
    adapters.register_loader("timestamp", TimestampBinaryLoader)
    adapters.register_loader("timestamptz", TimestamptzLoader)
    adapters.register_loader("timestamptz", TimestamptzBinaryLoader)
    adapters.register_loader("interval", IntervalLoader)
    adapters.register_loader("interval", IntervalBinaryLoader)
