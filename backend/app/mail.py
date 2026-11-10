import hashlib
import re
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.domain import AppError, Temporal, iso, millis, validated
from app.event_parser import action_blocks
from app.temporal_order import event_order
from app.persistence import Candidate, Email, MailCache, Occurrence, Task, audit, check_version, get_record

MAX_EML_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024


def normalize_mail(raw):
    if len(raw) > MAX_EML_BYTES:
        raise AppError("FILE_TOO_LARGE", "EML 文件不能超过 10 MiB", 413)
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        raise AppError("INVALID_EML", "无法读取此 EML 文件", 422) from None
    if not any(message.get(name) for name in ("From", "Subject", "Date", "Message-ID")):
        raise AppError("INVALID_EML", "EML 缺少可识别的邮件头", 422)
    plain, html, attachments, links = [], [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            if len(attachments) < 100:
                attachments.append({"name": (part.get_filename() or "attachment")[:300],
                                    "content_type": part.get_content_type(),
                                    "declared_size": part.get("Content-Length")})
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            payload = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if not isinstance(payload, str):
            continue
        if part.get_content_type() == "text/plain":
            plain.append(payload)
        else:
            soup = BeautifulSoup(payload, "html.parser")
            for node in soup(["script", "style", "form", "iframe", "object", "embed", "svg"]):
                node.decompose()
            for anchor in soup.find_all("a", href=True):
                url = str(anchor["href"])
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme in ("http", "https") and parsed.hostname and len(links) < 100:
                        links.append({"text": anchor.get_text(" ", strip=True)[:300], "url": url[:4096],
                                      "domain": parsed.hostname})
                except ValueError:
                    pass
            html.append(soup.get_text("\n", strip=True))
    text = "\n".join(plain if plain else html).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    truncated = len(text.encode("utf-8")) > MAX_TEXT_BYTES
    text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    for match in re.finditer(r"https?://[^\s<>\"']+", text):
        try:
            url = match.group().rstrip("。；，）")
            parsed = urlsplit(url)
            if parsed.hostname and len(links) < 100 and not any(link["url"] == url for link in links):
                links.append({"text": url, "url": url, "domain": parsed.hostname})
        except ValueError:
            pass
    header_date = None
    try:
        parsed = parsedate_to_datetime(str(message.get("Date", "")))
        if parsed.tzinfo:
            header_date = iso(parsed)
    except (ValueError, TypeError, OverflowError):
        pass
    subject = str(message.get("Subject", ""))[:1000]
    from_addr = str(message.get("From", ""))[:1000]
    message_id_header = str(message.get("Message-ID", ""))[:1000] or None
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    digest = hashlib.sha256("\0".join([subject, from_addr, header_date or "", message_id_header or "", text_hash]).encode()).hexdigest()
    return {"subject": subject, "from_addr": from_addr, "header_date": header_date,
            "message_id_header": message_id_header, "text_excerpt": text, "text_hash": text_hash,
            "text_truncated": truncated, "canonical_digest": digest, "links": links, "attachments": attachments}


DATE_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})[年/.-](?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})(?!\d)日?")
DATE_TOKENS = re.compile(DATE_RE.pattern+
    r'|(?<![\d年/.-])(?P<cm>\d{1,2})月(?P<cd>\d{1,2})(?!\d)日?'
    r'|(?<![\d年/.:：-])(?P<nm>\d{1,2})[/.-](?P<nd>\d{1,2})(?![\d:：])日?')
TIME_RE = re.compile(r"(?P<hour>\d{1,2})(?::|：|点)(?P<minute>\d{1,2})?分?")


def normalize_chinese_clocks(text):
    digits = dict(zip('零一二三四五六七八九', range(10)))
    digits['两'] = 2
    def number(value):
        if value.isdigit():
            return int(value)
        if '十' in value:
            left, right = value.split('十', 1)
            return (digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0)
        return digits.get(value, -1)
    def clock(match):
        hour = number(match['hour'])
        suffix = match['minute'] or ''
        minute = {'半': 30, '一刻': 15, '三刻': 45}.get(suffix)
        if minute is None:
            minute = number(suffix.rstrip('分')) if suffix else 0
        return f'{hour}:{minute:02d}'
    return re.sub(r'(?P<hour>\d{1,2}|[零一二两三四五六七八九十]{1,3})[点时]'
        r'(?P<minute>半|[一三]刻|(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})分?)?', clock, text)


def normalize_dayperiod_clocks(text):
    """Resolve each local clock before parsing ranges; never apply PM globally."""
    text = re.sub(r'(?<!\d)(?<!\d[:：])(\d{1,2})\s*([ap]\.?m\.?)\b', r'\1:00 \2', text, flags=re.I)
    pattern = re.compile(r'(?:(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间|夜里|夜间|午夜|晚|早)[ \t]*)?'
        r'(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?:分)?(?:[ \t]*(?P<ampm>[ap]\.?m\.?(?![a-z])))?', re.I)
    clocks = list(pattern.finditer(text))
    output, cursor, inherited = [], 0, None
    for index, match in enumerate(clocks):
        period = match['period'] or (match['ampm'] or '').lower().replace('.', '') or None
        gap = text[clocks[index - 1].end():match.start()] if index else ''
        connected = bool(index and re.fullmatch(r'\s*[-—–~～至到]\s*', gap))
        if not period and connected:
            period = inherited
        # In "7:00–9:00 PM" the suffix qualifies both ends of the range.
        if not period and index + 1 < len(clocks):
            following = clocks[index + 1]
            if following['ampm'] and not following['period'] and re.fullmatch(
                    r'\s*[-—–~～至到]\s*', text[match.end():following.start()]):
                period = following['ampm'].lower().replace('.', '')
        hour = int(match['hour'])
        if period in ('下午', '傍晚', '晚上', '晚间', '夜里', '夜间', '晚', 'pm') and hour < 12:
            hour += 12
        elif period == '中午' and 1 <= hour <= 3:
            hour += 12
        elif period in ('凌晨', '午夜', 'am') and hour == 12:
            hour = 0
        inherited = period
        output.extend((text[cursor:match.start()], f'{hour:02d}:{int(match["minute"]):02d}'))
        cursor = match.end()
    return ''.join(output) + text[cursor:]


def parse_temporal(text, kind, default_timezone="Asia/Shanghai", header_date=None, internal_date=None, default_event_year=2026):
    original_text=text
    # Evidence retains the complete block; incidental clocks in addresses, URLs
    # and UTC offsets must not become competing event times.
    text=re.sub(r'(?m)^\s*(?:(?:面试|笔试|测评)\s*)?(?:地点|地址|会议链接|会议地址|线上链接)\s*[:：][^\n]*', '', text)
    text=re.sub(r'https?://[^\s<>]+', '', text)
    text=normalize_dayperiod_clocks(normalize_chinese_clocks(text))
    zone = "Asia/Shanghai" if re.search(r"北京时间|中国标准时间|Asia/Shanghai|(?:UTC|GMT)\+0?8(?::00)?(?![:\d])|\+08:00(?!\d)", text) else None
    if re.search(r"\bUTC\b(?!\+)|\bGMT\b(?!\+)|\bZ\b", text):
        zone = "UTC"
    value = {"kind": "unknown", "raw_text": original_text[:4096], "timezone": zone or default_timezone,
             "timezone_source": "explicit" if zone else "account_default", "precision": "unknown",
             "needs_confirmation": True, "reason_codes": []}
    text=re.sub(r'(?:UTC|GMT)\+0?8(?::00)?(?![:\d])|\+08:00(?!\d)', '', text)
    # One token pass protects explicit years and also handles mixed explicit/omitted years.
    text=DATE_TOKENS.sub(lambda match:match.group() if match['year'] else
        f"{default_event_year}年{match['cm'] or match['nm']}月{match['cd'] or match['nd']}日",text)
    dates, times = list(DATE_RE.finditer(text)), list(TIME_RE.finditer(text))
    if len(dates)==1 and re.search(r'(?:或(?:者)?|、|至|到|和|及|[-—～~])\s*\d+日',text[dates[0].end():]):
        value['reason_codes']=['MULTIPLE_TIMES']
        return Temporal.model_validate(value).public()
    if len(dates)==1 and len(times)==2 and re.fullmatch(r'\s*[-—–~～至到]\s*', text[times[0].end():times[1].start()]):
        if kind not in ('interview','written_exam'):
            value['reason_codes']=['MULTIPLE_TIMES']
            return Temporal.model_validate(value).public()
        start_text=text[:times[1].start()]+text[times[1].end():]
        result=parse_temporal(start_text,kind,default_timezone,header_date,internal_date,default_event_year)
        result['raw_text']=original_text[:4096]
        if result.get('starts_at'):
            try:
                start=datetime.fromisoformat(result['starts_at']).astimezone(ZoneInfo(result['timezone']))
                end_hour=int(times[1]['hour'])
                end=start.replace(hour=end_hour,minute=int(times[1]['minute'] or 0))
                if (end.astimezone(timezone.utc).astimezone(end.tzinfo).replace(tzinfo=None)!=end.replace(tzinfo=None)
                        or end.replace(fold=0).utcoffset()!=end.replace(fold=1).utcoffset()):
                    raise ValueError()
                result['ends_at']=iso(end)
                result['raw_text']=original_text[:4096]
                return Temporal.model_validate(result).public()
            except (ValueError,OverflowError):
                value['reason_codes']=['INVALID_TIME_RANGE']
        return Temporal.model_validate(value).public()
    if len(dates) > 1 or len(times) > 1:
        value["reason_codes"] = ["MULTIPLE_TIMES"]
    elif dates:
        d = dates[0]
        try:
            day = datetime(int(d["year"]), int(d["month"]), int(d["day"]))
            if times:
                t = times[0]
                hour = int(t["hour"])
                minute = int(t["minute"] or 0)
                local = day.replace(hour=hour, minute=minute, tzinfo=ZoneInfo(zone or default_timezone))
                # Verify the local clock survives UTC conversion, rejecting nonexistent DST times.
                if local.astimezone(timezone.utc).astimezone(local.tzinfo).replace(tzinfo=None) != local.replace(tzinfo=None):
                    raise ValueError()
                ambiguous = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
                unclear_zone=bool(re.search(r'\b(?:CST|EST|EDT|PST|PDT|MST|MDT|CET|CEST|JST|IST)\b'
                    r'|(?:UTC|GMT)\s*[+-]\s*\d|(?<!\d)[+-]\d{2}:?\d{2}\b'
                    r'|(?:美东|美西|东京|日本|伦敦|纽约|(?:美国)?(?:东部|西部|太平洋))(?:标准|夏令)?时间'
                    r'|\b(?!Asia/Shanghai\b)[A-Za-z_]+/[A-Za-z_]+\b',text,re.I))
                value.update(kind="absolute", precision="minute", needs_confirmation=ambiguous or unclear_zone)
                if unclear_zone:
                    value['reason_codes'].append('TIMEZONE_UNCONFIRMED')
                value["starts_at" if kind in ("interview", "written_exam") else "due_at"] = iso(local)
                if ambiguous:
                    value["reason_codes"].append("AMBIGUOUS_LOCAL_TIME")
            else:
                value.update(kind="date_only", local_date=day.date().isoformat(), precision="date")
                value["reason_codes"].append("TIME_MISSING")
        except (ValueError, OverflowError):
            value["reason_codes"].append("INVALID_LOCAL_TIME")
    elif re.search(r"明天|后天|本周|下周|小时|小时内|收到后", text):
        value["kind"] = "relative"
        value["reason_codes"].append("RELATIVE_TIME_REQUIRES_CONFIRMATION")
        hours = re.search(r"(?:收到后|收到.*?后)(\d{1,3})小时", text)
        if hours:
            value["candidates"] = []
            for reference_kind, reference in (("message_date", header_date), ("internal_date", internal_date)):
                if reference:
                    try:
                        anchor = datetime.fromisoformat(reference)
                        value["candidates"].append({"reference_kind": reference_kind, "reference_at": iso(anchor),
                                                    "due_at": iso(anchor + timedelta(hours=int(hours[1])))})
                    except (ValueError, OverflowError):
                        continue
        elif header_date and re.search(r"明天|后天|(?:本周|下周)[一二三四五六日天]", text):
            reference = datetime.fromisoformat(header_date).astimezone(ZoneInfo(zone or default_timezone))
            weekday=re.search(r'(本周|下周)([一二三四五六日天])', text)
            if weekday:
                target='一二三四五六日'.index(weekday[2].replace('天','日'))
                day=reference.date()+timedelta(days=target-reference.weekday()+(7 if weekday[1]=='下周' else 0))
            else:
                day = reference.date() + timedelta(days=2 if "后天" in text else 1)
            candidate = {"reference_kind": "message_date", "reference_at": header_date, "local_date": str(day)}
            if len(times) == 1:
                try:
                    hour=int(times[0]['hour'])
                    local = datetime.combine(day, reference.time()).replace(hour=hour,
                             minute=int(times[0]["minute"] or 0), second=0, microsecond=0, tzinfo=reference.tzinfo)
                    candidate["starts_at" if kind in ("interview", "written_exam") else "due_at"] = iso(local)
                except ValueError:
                    pass
            value["candidates"] = [candidate]
    else:
        value["reason_codes"].append("YEAR_OR_DATE_MISSING")
    return Temporal.model_validate(value).public()


def classify_and_parse(mail, default_timezone, default_event_year=2026):
    text, subject = mail.text_excerpt, mail.subject
    combined = subject + "\n" + text
    changed = bool(re.search(r"改期|取消|延期|重新安排|reschedul|cancel", combined, re.I))
    forwarded = bool(re.search(r"转发|转寄|Forwarded|^Fwd:|原始邮件", combined, re.I))
    company_match = re.search(r"([\w\u4e00-\u9fff]{2,30}(?:科技|公司|集团|银行|大学))", subject + " " + text)
    company = company_match.group(1) if company_match else None
    candidates = []
    for block in action_blocks(text):
        segment = block['text']
        if not segment:
            continue
        kind = block['kind']
        if not kind:
            continue
        # Bare salutations/subject repeats are not independent actions.
        if not re.search(r"\d|请|安排|参加|提交|上传|完成|截止|改期|取消|明天|后天|周|邀请|schedule|complete", segment, re.I):
            continue
        temporal = parse_temporal(segment, kind, default_timezone, mail.header_date, mail.internal_date,default_event_year)
        issues = list(temporal["reason_codes"])
        if changed:
            issues.append("CHANGE_REQUIRES_REVIEW")
        if forwarded:
            issues.append("FORWARDED_MESSAGE")
        if mail.text_truncated:
            issues.append("TEXT_TRUNCATED")
        if not company:
            issues.append("COMPANY_MISSING")
        start = block['start']
        labels = {"reply": "回复确认", "materials": "提交材料", "assessment": "完成测评",
                  "written_exam": "参加笔试", "interview": "参加面试", "offer": "处理录用通知"}
        candidates.append({"title": ((company + " · ") if company else "") + labels[kind], "kind": kind,
                           "priority": 2, "temporal": temporal, "issues": issues,
                           "company": company, "event_details": block['event_details'], "evidence": [{"message_id": mail.id, "text_hash": mail.text_hash,
                           "segment_id": str(start), "start": start, "end": start + len(segment),
                           "quote": segment, "field": "action"}]})
        if len(candidates) == 30:
            break
    if changed and not candidates:
        candidates.append({"title": "审核招聘安排变更", "kind": "custom", "priority": 2,
                           "temporal": Temporal().public(), "issues": ["CHANGE_REQUIRES_REVIEW"], "evidence": []})
    candidates.sort(key=lambda candidate:event_order(candidate['temporal'],default_timezone))
    classification = {"relevance": "recruitment" if candidates else "uncertain",
                      "categories": sorted({c["kind"] for c in candidates}), "rule_version": "rules-v3"}
    if not candidates and re.search(r"已收到.*申请|申请.*收到|application.*received|感谢.*投递", combined, re.I):
        classification.update(relevance="recruitment", categories=["application_received"])
    elif not candidates and re.search(r"促销|优惠券|退订|sale|unsubscribe", combined, re.I):
        classification.update(relevance="non_recruitment", categories=["promotion"])
    return classification, candidates


class MailService:
    def __init__(self, db, tasks, settings, clock):
        self.db, self.tasks, self.settings, self.clock = db, tasks, settings, clock
        self.parse_lock = threading.Lock()
        self.ai_enqueue = None

    def upgrade_year_defaults(self,checkpoint):
        """Upgrade untouched legacy suggestions only; user decisions remain authoritative."""
        updates=[]
        with self.db.read() as session:
            profile=self.settings.get(session)
            for candidate in session.scalars(select(Candidate).where(Candidate.state=='open',Candidate.task_id.is_(None))):
                draft=candidate.candidate
                original=draft.get('temporal',{})
                if 'YEAR_OR_DATE_MISSING' not in original.get('reason_codes',[]):
                    continue
                if DATE_RE.search(original.get('raw_text','')):
                    continue
                temporal=parse_temporal(original.get('raw_text',''),draft['kind'],
                    original.get('timezone') or profile['timezone'],default_event_year=profile['default_event_year'])
                if temporal['kind'] not in ('absolute','date_only'):
                    continue
                issues=[issue for issue in draft.get('issues',[]) if issue not in original.get('reason_codes',[])]
                updates.append((candidate.id,candidate.version,{**draft,'event_details':draft.get('event_details',{}),'temporal':temporal,
                    'issues':list(dict.fromkeys(issues+temporal['reason_codes']))}))
        if not updates:
            return 0
        checkpoint()
        with self.db.tx() as session:
            messages=set()
            for candidate_id,version,draft in updates:
                candidate=get_record(session,Candidate,candidate_id)
                check_version(candidate,version)
                candidate.candidate=draft
                candidate.version+=1
                mail=get_record(session,Email,candidate.message_id)
                messages.add(mail.id)
                if not draft['issues'] and not draft['temporal']['needs_confirmation']:
                    task=self.tasks.create({key:draft[key] for key in ('title','kind','priority','temporal','event_details')},
                        session=session,source={'message_id':mail.id,'subject':mail.subject,'evidence':draft.get('evidence',[])},
                        action_key=candidate.action_key)
                    candidate.state,candidate.task_id='accepted',task['id']
            session.flush()
            for message_id in messages:
                mail=get_record(session,Email,message_id)
                mail.state='review_needed' if session.scalar(select(Candidate.id).where(
                    Candidate.message_id==message_id,Candidate.state=='open')) else 'parsed'
                mail.version+=1
                audit(session,mail,'mail.parsed',{'candidates':len(updates)})
        return len(updates)

    def ingest(self, raw, *, account_id=None, uidvalidity=None, message_uid=None, internal_date=None, session=None):
        normalized = normalize_mail(raw)
        import_digest = hashlib.sha256(raw).hexdigest()
        occurrence_key = (f"{account_id}:INBOX:{uidvalidity}:{message_uid}" if account_id else "import:" + import_digest)
        if session is None:
            with self.db.tx() as s:
                return self.ingest(raw, account_id=account_id, uidvalidity=uidvalidity, message_uid=message_uid,
                                   internal_date=internal_date, session=s)
        existing_occurrence = session.scalar(select(Occurrence).where(Occurrence.occurrence_key == occurrence_key))
        if existing_occurrence:
            record = get_record(session, Email, existing_occurrence.message_id)
            if record.state == "parse_failed" and not record.ignored_by_user:
                cache = session.get(MailCache, record.id)
                if cache is None:
                    cache = MailCache(message_id=record.id, content=normalized["text_excerpt"],
                                      expires_at=millis(self.clock())+604800000)
                    session.add(cache)
                else:
                    cache.content, cache.expires_at = normalized["text_excerpt"], millis(self.clock())+604800000
                record.state, record.text_excerpt = "new", normalized["text_excerpt"][:4096]
                record.version += 1
            return existing_occurrence.message_id
        same = list(session.scalars(select(Email).where(Email.canonical_digest == normalized["canonical_digest"])))
        existing = None
        for mail in same:
            occurrences = list(session.scalars(select(Occurrence).where(Occurrence.message_id == mail.id)))
            if account_id is None or any(o.account_id in (None, account_id) for o in occurrences):
                existing = mail
                break
        if existing:
            mail = existing
        else:
            text = normalized.pop("text_excerpt")
            mail = Email(**normalized, text_excerpt=text[:4096], internal_date=internal_date,
                         state="new", ignored_by_user=False, classification={"duplicate_candidate": bool(same)},
                         created_at=millis(self.clock()))
            session.add(mail)
            session.flush()
            session.add(MailCache(message_id=mail.id, content=text, expires_at=millis(self.clock())+604800000))
        session.add(Occurrence(message_id=mail.id, occurrence_key=occurrence_key, account_id=account_id,
                               folder="INBOX" if account_id else None, uidvalidity=uidvalidity, uid=message_uid))
        session.flush()
        return mail.id

    def parse_pending(self, limit=20):
        if not self.parse_lock.acquire(blocking=False):
            return
        try:
            with self.db.activity():
                self.cleanup()
                with self.db.read() as session:
                    ids = list(session.scalars(select(Email.id).where(Email.state == "new")
                                               .order_by(Email.created_at, Email.id).limit(min(limit, 20))))
                for message_id in ids:
                    try:
                        self._parse(message_id)
                    except Exception:
                        with self.db.tx() as session:
                            mail = get_record(session, Email, message_id)
                            if mail.state == "new":
                                mail.state, mail.classification = "parse_failed", {**mail.classification, "error_code": "PARSE_FAILED"}
                                mail.version += 1
                        self.db.log("parse.failed", module="parse", entity_id=message_id, error_code="PARSE_FAILED")
        finally:
            self.parse_lock.release()

    def _parse(self, message_id):
        with self.db.read() as session:
            record = get_record(session, Email, message_id)
            if record.state != "new" or record.ignored_by_user:
                return
            cache = session.get(MailCache, message_id)
            if cache is None:
                raise AppError("SOURCE_UNAVAILABLE", "临时正文已清理，请重新导入原邮件", 409)
            version = record.version
            duplicate = record.classification.get("duplicate_candidate", False)
            fields = {column.name: getattr(record, column.name) for column in Email.__table__.columns}
            fields["text_excerpt"] = cache.content
            profile = self.settings.get(session)
        classification, drafts = classify_and_parse(SimpleNamespace(**fields), profile['timezone'],profile['default_event_year'])
        with self.db.tx() as session:
            mail = get_record(session, Email, message_id)
            if mail.version != version or mail.state != "new" or mail.ignored_by_user:
                return
            mail.classification = {**classification, "duplicate_candidate": duplicate}
            for draft in drafts:
                if duplicate:
                    draft["issues"].append("DUPLICATE_CANDIDATE")
                action_key = mail.id+":"+hashlib.sha256((draft["kind"]+repr(draft["evidence"])).encode()).hexdigest()
                if session.scalar(select(Candidate.id).where(Candidate.action_key == action_key)):
                    continue
                candidate = Candidate(message_id=mail.id, candidate=draft, action_key=action_key, state="open")
                session.add(candidate)
                session.flush()
                if not draft["issues"] and not draft["temporal"]["needs_confirmation"]:
                    task = self.tasks.create({key: draft[key] for key in ("title", "kind", "priority", "temporal", "event_details")},
                        session=session, source={"message_id": mail.id, "subject": mail.subject,
                        "evidence": draft["evidence"]}, action_key=candidate.action_key)
                    candidate.state, candidate.task_id = "accepted", task["id"]
            session.flush()
            mail.state = "review_needed" if session.scalar(select(Candidate.id).where(
                Candidate.message_id == mail.id, Candidate.state == "open")) else "parsed"
            mail.version += 1
            session.delete(session.get(MailCache, message_id))
            audit(session, mail, "mail.parsed", {"candidates": len(drafts)})
            if mail.state == "review_needed":
                self.db.log("parse.review_needed", module="parse", entity_id=message_id, count=len(drafts))
        if self.ai_enqueue:
            self.ai_enqueue(message_id)

    def cleanup(self):
        with self.db.tx() as session:
            for cache in session.scalars(select(MailCache).where(MailCache.expires_at <= millis(self.clock()))):
                mail = session.get(Email, cache.message_id)
                if mail and mail.state in ("new", "parse_failed"):
                    mail.state, mail.classification = "parse_failed", {**mail.classification, "error_code": "SOURCE_CACHE_EXPIRED"}
                    mail.version += 1
                session.delete(cache)

    def serialize(self, mail, session, detail=False):
        candidates=list(session.scalars(select(Candidate).where(Candidate.message_id==mail.id)
                                        .order_by(Candidate.created_at,Candidate.id)))
        def temporal(candidate):
            task=session.get(Task,candidate.task_id) if candidate.task_id else None
            return task.temporal if task else candidate.candidate.get('temporal',{})
        candidates.sort(key=lambda candidate:(*event_order(temporal(candidate)),candidate.created_at,candidate.id))
        earliest=next((temporal(c) for c in candidates if c.state not in ('ignored','superseded')
                       and event_order(temporal(c))[0]==0),{})
        data = {"id": mail.id, "version": mail.version, "subject": mail.subject, "from_addr": mail.from_addr,
                "state": mail.state, "header_date": mail.header_date, "internal_date": mail.internal_date,
                "first_seen_at": iso(mail.created_at), "ignored_by_user": mail.ignored_by_user,
                "text_truncated": mail.text_truncated, "classification": mail.classification,
                'event_at':earliest.get('starts_at') or earliest.get('due_at'),
                'event_date':earliest.get('local_date'),'event_timezone':earliest.get('timezone')}
        if detail:
            data.update(text_excerpt=mail.text_excerpt, text_hash=mail.text_hash, links=mail.links,
                        attachments=mail.attachments, candidates=[
                            {**c.candidate, "candidate_id": c.id, "state": c.state, "task_id": c.task_id}
                            for c in candidates])
        return data

    def get(self, message_id):
        with self.db.read() as session:
            return self.serialize(get_record(session, Email, message_id), session, True)

    def list(self, state=None, account_id=None):
        with self.db.read() as session:
            query = select(Email).order_by(Email.created_at.desc(), Email.id)
            if state:
                query = query.where(Email.state == state)
            if account_id:
                query = query.where(Email.id.in_(select(Occurrence.message_id).where(Occurrence.account_id == account_id)))
            values=[self.serialize(m, session) for m in session.scalars(query)]
            return sorted(values,key=lambda mail:(*event_order({'starts_at':mail['event_at'],
                'local_date':mail['event_date'],'timezone':mail['event_timezone']}),mail['first_seen_at'],mail['id']))

    def reviews(self):
        with self.db.read() as session:
            ids = select(Candidate.message_id).where(Candidate.state == "open")
            return [self.serialize(m, session, True) for m in session.scalars(select(Email).where(Email.id.in_(ids))
                                                                              .order_by(Email.created_at, Email.id))]

    def review(self, message_id, expected_version, decisions):
        if not isinstance(decisions, list) or not 1 <= len(decisions) <= 100:
            raise AppError("VALIDATION_ERROR", "请选择需要处理的审核项", 422)
        with self.db.tx() as session:
            mail = get_record(session, Email, message_id)
            check_version(mail, expected_version)
            if decisions == [{"candidate_id": "all", "action": "ignore"}]:
                for candidate in session.scalars(select(Candidate).where(Candidate.message_id == mail.id,
                                                                         Candidate.state == "open")):
                    candidate.state = "ignored"
                mail.state, mail.ignored_by_user = "ignored", True
                mail.version += 1
                cache = session.get(MailCache, message_id)
                if cache:
                    session.delete(cache)
                audit(session, mail, "mail.ignored")
                return {"email": self.serialize(mail, session, True), "tasks": [], "task_ids": [], "new_version": mail.version}
            tasks = []
            seen = set()
            for decision in decisions:
                candidate_id = decision.get("candidate_id")
                if candidate_id in seen:
                    raise AppError("VALIDATION_ERROR", "审核项不能重复", 422)
                seen.add(candidate_id)
                candidate = get_record(session, Candidate, candidate_id)
                if candidate.message_id != mail.id or candidate.state != "open":
                    raise AppError("STATE_CONFLICT", "审核项已处理或不属于此邮件", 409)
                action = decision.get("action")
                if action == "ignore":
                    candidate.state = "ignored"
                    continue
                if action not in ("accept", "edit"):
                    raise AppError("VALIDATION_ERROR", "审核操作无效", 422)
                if "CHANGE_REQUIRES_REVIEW" in candidate.candidate["issues"]:
                    raise AppError("MANUAL_RESCHEDULE_REQUIRED", "变更邮件请先手动修改原任务，再忽略此审核项", 409)
                value = {key: candidate.candidate[key] for key in ("title", "kind", "priority", "temporal")}
                value['event_details'] = candidate.candidate.get('event_details', {})
                if decision.get("application_id"):
                    value["application_id"] = decision["application_id"]
                if action == "edit":
                    changes = decision.get("changes", {})
                    if not isinstance(changes, dict) or set(changes) - set(value) - {"notes", "application_id"}:
                        raise AppError("VALIDATION_ERROR", "审核修改字段无效", 422)
                    value.update(changes)
                validated(Temporal, value["temporal"])
                task = self.tasks.create(value, session=session,
                                         source={"message_id": mail.id, "subject": mail.subject,
                                                 "evidence": candidate.candidate["evidence"], "reviewed": True},
                                         action_key=candidate.action_key)
                candidate.state, candidate.task_id = "accepted", task["id"]
                tasks.append(task)
            session.flush()
            all_candidates = list(session.scalars(select(Candidate).where(Candidate.message_id == mail.id)))
            mail.ignored_by_user = all(c.state == "ignored" for c in all_candidates)
            mail.state = "ignored" if mail.ignored_by_user else (
                "review_needed" if any(c.state == "open" for c in all_candidates) else "parsed")
            mail.version += 1
            audit(session, mail, "mail.reviewed", {"decisions": len(decisions)})
            return {"email": self.serialize(mail, session, True), "tasks": tasks,
                    "task_ids": [t["id"] for t in tasks], "new_version": mail.version}

    def reparse(self, message_id, expected_version):
        with self.db.tx() as session:
            mail = get_record(session, Email, message_id)
            check_version(mail, expected_version)
            if mail.ignored_by_user:
                raise AppError("IGNORED_MESSAGE", "已忽略邮件不会自动重新解析", 409)
            if mail.state == "new":
                return {"message_id": mail.id}
            if mail.state == "parse_failed":
                cache = session.get(MailCache, message_id)
                if cache is None or cache.expires_at <= millis(self.clock()):
                    raise AppError("SOURCE_UNAVAILABLE", "临时正文已清理，请重新导入原邮件", 409)
                mail.state = "new"
                mail.version += 1
                return {"message_id": mail.id}
            if mail.classification.get("rule_version") == "rules-v3":
                return {"message_id": mail.id, "unchanged": True, "source_is_excerpt": True}
            # Rules are versioned. Existing accepted actions and user fields are immutable to reparse.
            profile=self.settings.get(session)
            classification, drafts = classify_and_parse(mail,profile['timezone'],profile['default_event_year'])
            mail.classification = classification
            existing = list(session.scalars(select(Candidate).where(Candidate.message_id == mail.id)))
            by_key = {c.action_key:c for c in existing}
            # Replace stale open suggestions only. Accepted/ignored decisions and
            # task fields are preserved. A changed segmentation cannot authorize
            # creating a second task for a previously accepted source action.
            for candidate in existing:
                if candidate.state=='open':
                    candidate.state='superseded'
            for draft in drafts:
                action_key = mail.id + ":" + hashlib.sha256((draft["kind"] + repr(draft["evidence"])).encode()).hexdigest()
                prior=by_key.get(action_key)
                if prior and prior.state in ('accepted','ignored'):
                    continue
                if any(c.state=='ignored' and c.candidate['kind']==draft['kind'] and
                       any(isinstance(old.get('start'),int) and isinstance(old.get('end'),int) and
                           isinstance(new.get('start'),int) and isinstance(new.get('end'),int) and
                           old['start']<new['end'] and new['start']<old['end']
                           for old in c.candidate.get('evidence',[]) for new in draft.get('evidence',[]))
                       for c in existing):
                    continue
                draft['issues'].append('REPARSE_DIFFERENCE')
                if any(c.state=='accepted' and c.candidate['kind']==draft['kind'] for c in existing):
                    draft['issues'].append('CHANGE_REQUIRES_REVIEW')
                if prior:
                    prior.candidate,prior.state=draft,'open'
                else:
                    session.add(Candidate(message_id=mail.id, candidate=draft, action_key=action_key))
            session.flush()
            mail.state='review_needed' if session.scalar(select(Candidate.id).where(
                Candidate.message_id==mail.id,Candidate.state=='open')) else 'parsed'
            mail.version += 1
            return {"message_id": mail.id}
