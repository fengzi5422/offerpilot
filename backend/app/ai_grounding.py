"""Local evidence alignment and calendar parsing; no model output is trusted."""
import re
import unicodedata
from datetime import datetime

from app.domain import AppError, Temporal
from app.event_parser import action_blocks
from app.mail import DATE_RE, DATE_TOKENS, TIME_RE, normalize_chinese_clocks, normalize_dayperiod_clocks, parse_temporal


def invalid(reason, field):
    return AppError('AI_OUTPUT_INVALID', 'AI 建议中有无法核对的字段，已保留现有结果', 503,
                    reason_code=reason, field=field)


def aligned(text):
    chars, positions = [], []
    for position, char in enumerate(text):
        for normalized in unicodedata.normalize('NFKC', char):
            if not normalized.isspace():
                chars.append(normalized)
                positions.append(position)
    return ''.join(chars), positions


class EvidenceIndex:
    def __init__(self, mail, text, body, mapping):
        self.mail, self.text, self.body, self.mapping = mail, text, body, mapping
        self.normalized, self.positions = aligned(body)
        self.blocks = action_blocks(text)

    def find(self, quote, field):
        needle, _ = aligned(quote)
        if not needle:
            raise invalid('AI_EVIDENCE_NOT_FOUND', field)
        matches = []
        for match in re.finditer(re.escape(needle), self.normalized):
            offset, last = self.positions[match.start()], self.positions[match.end() - 1]
            if (offset and self.mapping[offset - 1] == self.mapping[offset] or
                    last + 1 < len(self.mapping) and self.mapping[last] == self.mapping[last + 1]):
                continue
            begin, end = self.mapping[offset][0], self.mapping[last][1]
            matches.append({'message_id': self.mail.id, 'text_hash': self.mail.text_hash,
                'segment_id': str(begin), 'start': begin, 'end': end, 'quote': self.text[begin:end], 'field': field})
            if len(matches) > 100:
                raise invalid('AI_EVIDENCE_AMBIGUOUS', field)
        if not matches:
            raise invalid('AI_EVIDENCE_NOT_FOUND', field)
        return matches

    def block(self, proof, kind):
        matches = [b for b in self.blocks if b['kind'] == kind and b['start'] < proof['end'] and b['end'] > proof['start']]
        return matches[0] if len(matches) == 1 else None

    def action(self, action):
        matches = self.find(action.quote, 'action')
        if len(matches) > 1 and action.time_quote:
            times = self.find(action.time_quote, 'time')
            narrowed = [p for p in matches if (b := self.block(p, action.kind)) and
                        any(b['start'] <= t['start'] and t['end'] <= b['end'] for t in times)]
            if narrowed:
                matches = narrowed
        if len(matches) > 1:
            contexts = [self.block(p, action.kind) for p in matches]
            if not all(contexts) or len({aligned(b['text'])[0] for b in contexts}) != 1:
                raise invalid('AI_EVIDENCE_AMBIGUOUS', 'action')
        return matches[0]

    def field(self, quote, field, action_proof, kind):
        matches = self.find(quote, field)
        block = self.block(action_proof, kind)
        if block:
            within = [p for p in matches if block['start'] <= p['start'] and p['end'] <= block['end']]
            if within:
                matches = within
            elif any(b['start'] <= p['start'] and p['end'] <= b['end'] for p in matches for b in self.blocks):
                raise invalid('AI_EVIDENCE_CONFLICT', field)
        if len(matches) != 1:
            raise invalid('AI_EVIDENCE_AMBIGUOUS', field)
        return matches[0]


def calendar_text(text):
    text = unicodedata.normalize('NFKC', text)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    def number(value):
        if value.isdigit():
            return int(value)
        if '十' in value:
            left, right = value.split('十', 1)
            return (digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0)
        return int(''.join(str(digits[ch]) for ch in value))
    text = re.sub(r'[零〇一二三四五六七八九]{4}(?=年)', lambda m: str(number(m[0])), text)
    text = re.sub(r'(?P<m>[\d零〇一二两三四五六七八九十]{1,3})月(?P<d>[\d零〇一二两三四五六七八九十]{1,3})[日号]',
                  lambda m: f'{number(m["m"])}月{number(m["d"])}日', text)
    months = {name: i for i, names in enumerate((
        ('january', 'jan'), ('february', 'feb'), ('march', 'mar'), ('april', 'apr'), ('may',), ('june', 'jun'),
        ('july', 'jul'), ('august', 'aug'), ('september', 'sep', 'sept'), ('october', 'oct'),
        ('november', 'nov'), ('december', 'dec')), 1) for name in names}
    month = '(?:' + '|'.join(months) + ')'
    def english(match):
        year = f'{match["year"]}年' if match['year'] else ''
        return year + f'{months[match["month"].lower()]}月{int(match["day"])}日'
    text = re.sub(r'\b(?P<month>' + month + r')\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
                  r'(?:,?\s+(?P<year>20\d{2}))?\b', english, text, flags=re.I)
    return re.sub(r'\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>' + month + r')\.?'
                  r'(?:,?\s+(?P<year>20\d{2}))?\b', english, text, flags=re.I)


def grounded_time(text, kind, profile, mail):
    zone, year = profile.get('timezone', 'Asia/Shanghai'), profile.get('default_event_year', 2026)
    normalized = calendar_text(text)
    value = parse_temporal(normalized, kind, zone, mail.header_date, mail.internal_date, year)
    canonical = normalize_dayperiod_clocks(normalize_chinese_clocks(normalized))
    canonical = DATE_TOKENS.sub(lambda m: m.group() if m['year'] else
        f'{year}年{m["cm"] or m["nm"]}月{m["cd"] or m["nd"]}日', canonical)
    dates, clocks = list(DATE_RE.finditer(canonical)), list(TIME_RE.finditer(canonical))
    if kind in ('interview', 'written_exam') and len(dates) == len(clocks) == 2 and (
            dates[0].end() <= clocks[0].start() < dates[1].start() <= clocks[1].start() and
            re.fullmatch(r'\s*[-—–~～至到]\s*', canonical[clocks[0].end():dates[1].start()])):
        effective_zone = value['timezone'] or zone
        start = parse_temporal(canonical[:dates[1].start()], kind, effective_zone, default_event_year=year)
        end = parse_temporal(canonical[dates[1].start():], kind, effective_zone, default_event_year=year)
        if start.get('starts_at') and end.get('starts_at') and datetime.fromisoformat(end['starts_at']) > datetime.fromisoformat(start['starts_at']):
            value = {**start, 'timezone_source': value['timezone_source'], 'ends_at': end['starts_at'],
                     'needs_confirmation': start['needs_confirmation'] or end['needs_confirmation'],
                     'reason_codes': list(dict.fromkeys(start['reason_codes'] + end['reason_codes']))}
    value['raw_text'] = text[:4096]
    return Temporal.model_validate(value).public()
