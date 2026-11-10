"""Pure RFC 5545 snapshot export of TaskService.serialize dictionaries.

This module does no I/O and never reads settings, mail bodies, or credential stores.
Importing snapshots is not synchronization; client replacement behavior varies.
"""

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


def _utc(value: datetime | str) -> str:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Calendar timestamps require an explicit timezone")
    return instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _text(value: str) -> str:
    # RFC 5545 allows HTAB; CR/LF are normalized and escaped below.
    value = "".join(character for character in value
                    if character in "\t\r\n" or 32 <= ord(character) != 127)
    # Escape backslashes first so encoded newlines cannot become content lines.
    return (value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,"))


def _fold(line: str) -> str:
    """Fold at UTF-8 code point boundaries; the continuation space counts too."""
    parts, current, size = [], "", 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " ", 1
        current += character
        size += width
    parts.append(current)
    return "\r\n".join(parts)


def _schedule(task: Mapping[str, Any]) -> tuple[str, list[str]] | None:
    temporal = task.get("temporal") or {}
    precision = temporal.get("precision", "unknown")
    is_event = task["kind"] in ("interview", "written_exam")
    if precision == "date":
        local_date = temporal.get("local_date") or task.get("due_date")
        if not local_date:
            return None
        day = date.fromisoformat(local_date).strftime("%Y%m%d")
        return ("VEVENT", [f"DTSTART;VALUE=DATE:{day}"]) if is_event else (
            "VTODO", [f"DUE;VALUE=DATE:{day}"])
    if precision != "minute":
        return None
    if temporal.get("needs_confirmation") and not task.get("confirmed"):
        return None
    starts_at, due_at = task.get("starts_at"), task.get("due_at")
    if is_event and starts_at:
        lines = [f"DTSTART:{_utc(starts_at)}"]
        if task.get("ends_at"):
            lines.append(f"DTEND:{_utc(task['ends_at'])}")
        return "VEVENT", lines
    if due_at:
        return "VTODO", [f"DUE:{_utc(due_at)}"]
    if starts_at:
        return "VTODO", [f"DTSTART:{_utc(starts_at)}"]
    return None


def _details(task: Mapping[str, Any]) -> list[str]:
    lines, description = [], []
    if task.get("notes"):
        description.append(task["notes"])
    source = task.get("source") or {}
    # Never serialize arbitrary source/event dictionaries: they may gain secrets.
    for key, label in (("subject", "来源邮件"), ("message_id", "来源编号")):
        value = source.get(key)
        if isinstance(value, str) and value:
            description.append(f"{label}: {value}")
    if description:
        lines.append("DESCRIPTION:" + _text("\n".join(description)))
    details = task.get("event_details") or {}
    if details.get("location"):
        lines.append("LOCATION:" + _text(details["location"]))
    url = details.get("meeting_url") or ""
    if url and not any(ord(character) <= 32 or ord(character) == 127 for character in url):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme in ("https", "http") and parsed.hostname
                    and parsed.username is None and parsed.password is None):
                lines.append("URL:" + url)
        except ValueError:
            pass
    return lines


def export_calendar(
    tasks: Iterable[Mapping[str, Any]], exported_at: datetime, include_details: bool = False,
) -> tuple[bytes, int]:
    """Return (UTF-8 ICS snapshot, number of tasks omitted for unknown/unconfirmed dates).

    ``tasks`` must contain valid TaskService.serialize snapshots. ``exported_at``
    must be timezone-aware. UUIDs remain stable, and task versions become SEQUENCE.
    Date-only interviews/exams are all-day events; other dates are VTODO deadlines.
    Custom tasks with only a start retain DTSTART without inventing a deadline.
    Details are opt-in and restricted to notes, source subject/local message ID,
    location and a validated meeting URL; no other task/source fields are copied.
    """
    if type(include_details) is not bool:
        raise TypeError("include_details must be an explicit boolean")
    stamp = _utc(exported_at)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OfferPilot//Calendar Snapshot//ZH-CN",
             "CALSCALE:GREGORIAN"]
    omitted = 0
    for task in tasks:
        schedule = _schedule(task)
        if schedule is None:
            omitted += 1
            continue
        name, dates = schedule
        lines.extend([f"BEGIN:{name}", f"UID:{UUID(task['id'])}", f"DTSTAMP:{stamp}",
                      f"SEQUENCE:{int(task['version'])}", "SUMMARY:" + _text(task["title"]), *dates])
        status = task["status"]
        if status == "cancelled":
            lines.append("STATUS:CANCELLED")
        elif name == "VTODO":
            lines.append("STATUS:" + {"done": "COMPLETED", "in_progress": "IN-PROCESS",
                                      "pending": "NEEDS-ACTION"}[status])
        if include_details:
            lines.extend(_details(task))
        lines.append(f"END:{name}")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8"), omitted
