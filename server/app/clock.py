"""現在時刻。テストで差し替えられるよう 1 か所にまとめる。"""

import datetime as dt
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_jst(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc).astimezone(JST)


def jst_to_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=JST).astimezone(dt.timezone.utc).replace(tzinfo=None)
