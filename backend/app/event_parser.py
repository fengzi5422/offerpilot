"""Group explicit action labels and their adjacent details without sharing deadlines."""
import re

from pydantic import ValidationError

from app.domain import EventDetails


def action_kind(text):
    text=re.sub(r'(?:无需|不需要|请勿)\s*(?:回复|确认)', '', text)
    for kind, pattern in (
        ('reply', r'回复|确认.*出席|确认.*参加|确认.*意向|reply|confirm.*attend'),
        ('materials', r'上传|提交.*材料|提供.*材料|补充.*材料|成绩单|upload'),
        ('assessment', r'测评|测验|assessment'), ('written_exam', r'笔试|written.exam'),
        ('interview', r'面试|interview'), ('offer', r'接受.*offer|签署.*offer|接受.*录用|offer.*accept')):
        if re.search(pattern, text, re.I):
            return kind
    return None


def has_date(text):
    return bool(re.search(r'\d{1,4}[年/月.-]\d|明天|后天|本周|下周|收到后|小时'
        r'|[零〇一二两三四五六七八九十]{1,3}月[零〇一二两三四五六七八九十\d]{1,3}[日号]'
        r'|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?'
        r'|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b', text, re.I))


def action_blocks(text):
    blocks=[]
    current=None
    for match in re.finditer(r'[^\n。；;]+', text):
        segment=match.group().strip()
        start=match.start()+len(match.group())-len(match.group().lstrip())
        # Commas can join an action's date and instruction; split only when both
        # sides introduce different, explicitly named actions.
        parts=list(re.finditer(r'[^，]+', segment))
        split=len({action_kind(p.group()) for p in parts if action_kind(p.group())})>1
        spans=parts if split else [None]
        for part in spans:
            value=part.group().strip() if part else segment
            begin=start+part.start() if part else start
            end=begin+len(value)
            detail=re.match(r'(?:(面试|笔试|测评)\s*)?(地点|地址|会议链接|会议地址|线上链接)\s*[:：]\s*(.+)',value)
            if detail:
                detail_kind={'面试':'interview','笔试':'written_exam','测评':'assessment'}.get(detail[1])
                if detail_kind and (not current or current['kind']!=detail_kind):
                    current={'start':begin,'end':end,'kind':detail_kind,'has_time':False,
                             'event_details':{'location':'','meeting_url':''}}
                    blocks.append(current)
                if current:
                    key='meeting_url' if '链接' in detail[2] or detail[2]=='会议地址' else 'location'
                    content=detail[3].strip()
                    if key=='meeting_url':
                        url=re.search(r'https?://[^\s<>"\u3002]+', content)
                        content=url[0].rstrip('，,）)') if url else ''
                        try:
                            EventDetails(meeting_url=content)
                        except ValidationError:
                            content=''
                    current['event_details'][key]=content[:4096 if key=='meeting_url' else 1000]
                    current['end']=end
                continue
            kind=action_kind(value)
            if kind:
                # A heading or short invitation and the following labelled time
                # are the same action. Two separately dated actions stay separate.
                merge=current and current['kind']==kind and not current['has_time']
                if merge:
                    current['end']=end
                    current['has_time']=has_date(value)
                else:
                    current={'start':begin,'end':end,'kind':kind,'has_time':has_date(value),'event_details':{'location':'','meeting_url':''}}
                    blocks.append(current)
            elif current and (has_date(value) or re.match(r'(时间|日期|开始|结束)\s*[:：]',value)):
                current['end']=end
                current['has_time']=current['has_time'] or has_date(value)
    for block in blocks:
        block['text']=text[block['start']:block['end']]
    return blocks
