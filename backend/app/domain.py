from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc)


def millis(value):
    return int(value.timestamp() * 1000)


def iso(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value / 1000, timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AppError(Exception):
    def __init__(self, code, message, status=400, **fields):
        self.code, self.message, self.status, self.fields = code, message, status, fields


class Temporal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["absolute", "relative", "date_only", "unknown"] = "unknown"
    raw_text: str = Field(default="", max_length=4096)
    timezone: str | None = "Asia/Shanghai"
    timezone_source: Literal["explicit", "user_confirmed", "account_default", "unknown"] = "unknown"
    local_date: date | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    due_at: datetime | None = None
    precision: Literal["minute", "date", "unknown"] = "unknown"
    needs_confirmation: bool = True
    reference_kind: str | None = None
    reference_at: datetime | None = None
    candidates: list[dict] = Field(default_factory=list, max_length=10)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check(self):
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("未知时区") from None
        for key in ("starts_at", "ends_at", "due_at", "reference_at"):
            value = getattr(self, key)
            if value and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("时间必须包含时区")
            if value and self.timezone and value.utcoffset().total_seconds() != 0:
                local = value.astimezone(ZoneInfo(self.timezone))
                if local.replace(tzinfo=None) != value.replace(tzinfo=None) or local.utcoffset() != value.utcoffset():
                    raise ValueError("本地时间或 UTC 偏移与所选时区不一致")
        if self.ends_at and (not self.starts_at or self.ends_at <= self.starts_at):
            raise ValueError("结束时间必须晚于开始时间")
        if self.precision == "date" or self.kind == "date_only":
            if not self.local_date or any((self.due_at, self.starts_at, self.ends_at)):
                raise ValueError("仅日期不能包含具体时刻")
            self.precision = "date"
            self.needs_confirmation = True
        if self.precision == "minute" and not (self.due_at or self.starts_at):
            raise ValueError("精确时间需要截止或开始时刻")
        if not self.needs_confirmation and self.precision == "minute":
            if self.timezone_source not in ("explicit", "user_confirmed"):
                raise ValueError("未确认时区不能自动排程")
        if self.precision == "unknown":
            self.needs_confirmation = True
        return self

    def public(self):
        result = self.model_dump(mode="json")
        for key in ("due_at", "starts_at", "ends_at", "reference_at"):
            value = getattr(self, key)
            result[key] = iso(value) if value else None
        return result


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["assessment", "written_exam", "interview", "materials", "reply", "offer", "custom"] = "custom"
    priority: Literal[1, 2, 3] = 2
    application_id: str | None = Field(default=None, max_length=36)
    temporal: Temporal = Field(default_factory=Temporal)
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def trim_title(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("标题不能为空")
        return self


def validated(model, value):
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise AppError("VALIDATION_ERROR", "请检查输入字段", 422,
                       fields=[{"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                               for e in exc.errors()]) from None
