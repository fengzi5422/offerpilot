"""RFC 5545 snapshots, verified through the third-party icalendar parser."""

from copy import deepcopy
from datetime import date, datetime, timezone
from importlib import import_module
from uuid import UUID

import pytest


EXPORTED_AT = datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)
TASK_ID = "dfd4a810-8573-4f85-9205-c3d599eacbf0"


def task(**changes):
    result = {
        "id": TASK_ID, "version": 3, "title": "星河技术面试", "kind": "interview",
        "status": "pending", "starts_at": "2026-11-11T14:00:00+08:00", "ends_at": None,
        "due_at": None, "due_date": None,
        "temporal": {"kind": "absolute", "precision": "minute", "local_date": None},
        "notes": "", "source": None, "event_details": {"location": "", "meeting_url": ""},
    }
    result.update(changes)
    return result


def export(tasks, **kwargs):
    return import_module("app.icalendar").export_calendar(tasks, EXPORTED_AT, **kwargs)


def parsed(data):
    from icalendar import Calendar

    calendar = Calendar.from_ical(data)
    assert not calendar.errors
    components = [component for component in calendar.subcomponents]
    assert all(not component.errors for component in components)
    # Parse a second serialization too; validity must survive a standard parser round trip.
    reparsed = Calendar.from_ical(calendar.to_ical())
    assert len(reparsed.subcomponents) == len(components)
    return calendar, components


def test_exact_interview_is_utc_event_without_invented_end():
    data, omitted = export([task()])
    calendar, (event,) = parsed(data)
    assert omitted == 0
    assert str(calendar["VERSION"]) == "2.0"
    assert event.name == "VEVENT"
    assert event.decoded("DTSTART") == datetime(2026, 11, 11, 6, tzinfo=timezone.utc)
    assert event.decoded("DTSTAMP") == EXPORTED_AT
    assert "DTEND" not in event and "DURATION" not in event
    assert b"DTSTART:20261111T060000Z\r\n" in data


@pytest.mark.parametrize("kind", ["interview", "written_exam"])
def test_explicit_event_end_is_preserved(kind):
    data, _ = export([task(kind=kind, ends_at="2026-11-11T07:00:00Z")])
    _, (event,) = parsed(data)
    assert event.decoded("DTEND") == datetime(2026, 11, 11, 7, tzinfo=timezone.utc)


@pytest.mark.parametrize("kind", ["assessment", "materials", "reply", "offer", "custom", "interview"])
def test_deadline_is_a_todo_with_no_invented_start(kind):
    data, _ = export([task(kind=kind, starts_at=None, due_at="2026-11-12T18:00:00+08:00")])
    _, (todo,) = parsed(data)
    assert todo.name == "VTODO"
    assert todo.decoded("DUE") == datetime(2026, 11, 12, 10, tzinfo=timezone.utc)
    assert "DTSTART" not in todo and "DTEND" not in todo


@pytest.mark.parametrize("kind,component_name,property_name", [
    ("interview", "VEVENT", "DTSTART"), ("written_exam", "VEVENT", "DTSTART"),
    ("custom", "VTODO", "DUE"), ("materials", "VTODO", "DUE"),
])
def test_date_only_keeps_calendar_date_without_midnight_conversion(kind, component_name, property_name):
    data, _ = export([task(kind=kind, starts_at=None, due_date="2026-11-12",
                          temporal={"kind": "date_only", "precision": "date", "local_date": "2026-11-12"})])
    _, (component,) = parsed(data)
    assert component.name == component_name
    assert component.decoded(property_name) == date(2026, 11, 12)
    assert component[property_name].params["VALUE"] == "DATE"
    assert "DTEND" not in component and "DURATION" not in component


def test_unknown_dates_are_omitted_and_counted_including_stale_projected_values():
    unknown = task(temporal={"kind": "unknown", "precision": "unknown"})
    data, omitted = export([unknown, task(starts_at=None), task()])
    _, components = parsed(data)
    assert omitted == 2
    assert len(components) == 1


def test_uid_is_stable_task_uuid_and_sequence_tracks_task_version():
    first, _ = export([task()])
    second, _ = export([task(title="改名", version=4)])
    _, (old,) = parsed(first)
    _, (new,) = parsed(second)
    assert UUID(str(old["UID"])) == UUID(TASK_ID)
    assert old["UID"] == new["UID"]
    assert int(old["SEQUENCE"]) == 3
    assert int(new["SEQUENCE"]) == 4


@pytest.mark.parametrize("kind,starts_at,due_at", [
    ("interview", "2026-11-11T06:00:00Z", None),
    ("custom", None, "2026-11-11T06:00:00Z"),
])
def test_cancelled_tasks_remain_in_export(kind, starts_at, due_at):
    data, omitted = export([task(kind=kind, starts_at=starts_at, due_at=due_at, status="cancelled")])
    _, (component,) = parsed(data)
    assert omitted == 0
    assert str(component["STATUS"]) == "CANCELLED"


@pytest.mark.parametrize("status,expected", [("done", "COMPLETED"), ("pending", "NEEDS-ACTION"),
                                             ("in_progress", "IN-PROCESS")])
def test_todo_status_is_preserved(status, expected):
    data, _ = export([task(kind="custom", starts_at=None, due_at="2026-11-11T06:00:00Z", status=status)])
    _, (todo,) = parsed(data)
    assert str(todo["STATUS"]) == expected
    assert "COMPLETED" not in todo  # No recorded completion instant exists to export.


def test_text_escaping_prevents_property_or_component_injection():
    title = "公司,职位;轮次\\路径\r\nATTENDEE:mailto:intruder@example.test\rEND:VEVENT\nBEGIN:VTODO"
    data, _ = export([task(title=title)])
    _, (event,) = parsed(data)
    assert str(event["SUMMARY"]) == title.replace("\r\n", "\n").replace("\r", "\n")
    assert "ATTENDEE" not in event
    assert data.count(b"BEGIN:VEVENT\r\n") == 1
    assert b"\r\nBEGIN:VTODO\r\n" not in data


def test_utf8_folding_has_crlf_and_at_most_75_octets_per_physical_line():
    title = "中文面试🧭,分号;反斜线\\" * 15
    data, _ = export([task(title=title)])
    assert isinstance(data, bytes) and data.endswith(b"\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b"\r" not in data.replace(b"\r\n", b"")
    lines = data.split(b"\r\n")
    assert all(len(line) <= 75 for line in lines)
    assert any(line.startswith(b" ") for line in lines)
    assert all(line.decode("utf-8") is not None for line in lines)
    _, (event,) = parsed(data)
    assert str(event["SUMMARY"]) == title


def test_details_are_private_by_default_and_allowlisted_when_selected():
    private_task = task(notes="用户笔记;请准备,简历\n第二行", source={
        "subject": "来源邮件", "message_id": "local-message-id", "access_token": "SOURCE-CREDENTIAL",
        "vault_ref": "VAULT-REFERENCE", "evidence": [{"text": "UNSELECTED-MAIL-BODY"}],
    }, event_details={"location": "会议室二", "meeting_url": "https://meet.example.test/join?token=chosen-link"},
        secrets={"api_key": "OUTSIDE-CREDENTIAL"})
    data, _ = export([private_task])
    _, (event,) = parsed(data)
    assert "DESCRIPTION" not in event and "URL" not in event and "LOCATION" not in event
    for text in ["用户笔记", "来源邮件", "local-message-id", "会议室二", "chosen-link", "CREDENTIAL", "VAULT"]:
        assert text not in data.decode()
    selected, _ = export([private_task], include_details=True)
    _, (with_details,) = parsed(selected)
    assert private_task["notes"] in str(with_details["DESCRIPTION"])
    assert "来源邮件" in str(with_details["DESCRIPTION"])
    assert "local-message-id" in str(with_details["DESCRIPTION"])
    assert str(with_details["URL"]) == private_task["event_details"]["meeting_url"]
    assert str(with_details["LOCATION"]) == "会议室二"
    assert "CREDENTIAL" not in selected.decode() and "VAULT" not in selected.decode()
    assert "UNSELECTED-MAIL-BODY" not in selected.decode()


@pytest.mark.parametrize("url", ["https://user:password@example.test/join", "https://example.test/\r\nATTENDEE:x",
                                 "file:///secret", "javascript:alert(1)"])
def test_invalid_or_credential_bearing_url_is_not_exported(url):
    data, _ = export([task(event_details={"location": "", "meeting_url": url})], include_details=True)
    _, (event,) = parsed(data)
    assert "URL" not in event and "ATTENDEE" not in event


def test_export_is_deterministic_and_does_not_modify_input():
    tasks = [task()]
    original = deepcopy(tasks)
    assert export(tasks) == export(tasks)
    assert tasks == original


def test_empty_snapshot_is_valid_calendar():
    data, omitted = export([])
    _, components = parsed(data)
    assert omitted == 0 and components == []


def test_export_timestamp_must_have_explicit_timezone():
    with pytest.raises(ValueError, match="timezone"):
        import_module("app.icalendar").export_calendar([], datetime(2026, 11, 10))


def test_start_only_custom_task_remains_a_todo_without_inventing_deadline():
    data, omitted = export([task(kind="custom")])
    _, (todo,) = parsed(data)
    assert omitted == 0 and todo.name == "VTODO"
    assert todo.decoded("DTSTART") == datetime(2026, 11, 11, 6, tzinfo=timezone.utc)
    assert "DUE" not in todo


@pytest.mark.parametrize("selection", ["false", "true", 1, None])
def test_details_selection_requires_explicit_boolean(selection):
    with pytest.raises(TypeError, match="include_details"):
        export([task()], include_details=selection)


def test_real_task_service_snapshots_export_without_shape_translation(app):
    from test_core import task_body

    service = app.state.services.tasks
    event = service.create(task_body())
    body = task_body()
    body.update(kind="custom", title="准备资料")
    body["temporal"] = {"kind": "date_only", "precision": "date", "local_date": "2030-01-01"}
    todo = service.create(body)
    body["temporal"] = {"kind": "unknown", "precision": "unknown"}
    unknown = service.create(body)
    data, omitted = export([event, todo, unknown])
    _, components = parsed(data)
    assert omitted == 1
    assert [component.name for component in components] == ["VEVENT", "VTODO"]
    assert str(components[0]["UID"]) == event["id"]
    assert components[0].decoded("DTSTART") == datetime(2030, 1, 2, 7, tzinfo=timezone.utc)
    assert components[1].decoded("DUE") == date(2030, 1, 1)


@pytest.mark.parametrize("kind", ["interview", "written_exam", "custom"])
def test_unconfirmed_minute_requiring_confirmation_is_omitted(kind):
    uncertain = task(kind=kind, confirmed=False, temporal={
        "kind": "absolute", "precision": "minute", "needs_confirmation": True,
    })
    data, omitted = export([uncertain, task()])
    _, components = parsed(data)
    assert omitted == 1
    assert len(components) == 1


@pytest.mark.parametrize("kind", ["interview", "written_exam", "custom"])
def test_confirmed_minute_requiring_confirmation_can_be_exported(kind):
    confirmed = task(kind=kind, confirmed=True, temporal={
        "kind": "absolute", "precision": "minute", "needs_confirmation": True,
    })
    data, omitted = export([confirmed])
    _, (component,) = parsed(data)
    assert omitted == 0
    assert component.decoded("DTSTART") == datetime(2026, 11, 11, 6, tzinfo=timezone.utc)


def test_forbidden_text_controls_are_removed_from_all_text_properties():
    forbidden = "".join(chr(code) for code in range(32) if code not in (9, 10, 13)) + chr(127)
    text = "Before" + forbidden + "After\tTabbed\r\nSecond line,semicolon;slash\\"
    expected = "BeforeAfter\tTabbed\nSecond line,semicolon;slash\\"
    data, omitted = export([task(
        title=text, notes=text, source={"subject": text, "message_id": text},
        event_details={"location": text, "meeting_url": ""},
    )], include_details=True)
    assert omitted == 0
    assert all(character.encode() not in data for character in forbidden)
    _, (event,) = parsed(data)
    assert str(event["SUMMARY"]) == expected
    assert str(event["LOCATION"]) == expected
    description = str(event["DESCRIPTION"])
    assert description.startswith(expected + "\n")
    assert description.count(expected) == 3
