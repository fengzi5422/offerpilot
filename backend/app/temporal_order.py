"""Ordering only: a date-only value remains a date, never a scheduled deadline."""
from datetime import datetime
from zoneinfo import ZoneInfo


def event_order(temporal,default_timezone='Asia/Shanghai'):
    stamp=temporal.get('starts_at') or temporal.get('due_at')
    try:
        if stamp:
            return (0,datetime.fromisoformat(stamp).timestamp())
        day=temporal.get('local_date') or temporal.get('due_date')
        if day:
            return (0,datetime.fromisoformat(day).replace(
                tzinfo=ZoneInfo(temporal.get('timezone') or default_timezone)).timestamp())
    except (ValueError,TypeError):
        pass
    return (1,0)
