import type { Temporal } from './types';
let displayZone='Asia/Shanghai';
export function setDisplayTimezone(zone:string){new Intl.DateTimeFormat('zh-CN',{timeZone:zone}).format();displayZone=zone;}
export function changedFields(original:Record<string,unknown>,proposed:Record<string,unknown>){return Object.fromEntries(Object.entries(proposed).filter(([key,value])=>JSON.stringify(original[key])!==JSON.stringify(value)));}
export const kindNames:Record<string,string>={custom:'个人待办',interview:'面试',written_exam:'笔试',assessment:'在线测评',reply:'确认回复',materials:'材料补交',offer:'Offer 确认'};
export const statusNames:Record<string,string>={pending:'待完成',in_progress:'进行中',done:'已完成',cancelled:'已取消',new:'待识别',classified:'已分类',parsed:'已识别',review_needed:'待确认',ignored:'已忽略',parse_failed:'解析失败',sent:'已交给通知渠道',failed:'发送失败',delivery_unknown:'发送结果未确认',missed:'可能错过',suppressed:'已取消计划',ready:'已连接',auth_required:'需要重新授权',network_error:'连接异常',paused:'已暂停'};
function localParts(value:Date,zone:string){const f=new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});return f.format(value).replace(' ','T');}
export function temporalFromForm(date:string,time:string,kind:string,zone:string):Temporal{
 if(time&&!date)throw new Error('请先填写事件日期');
 const base={timezone:zone,timezone_source:'user_confirmed',raw_text:date+(time?' '+time:'')};
 if(!date)return {...base,kind:'unknown',precision:'unknown',needs_confirmation:true};
 if(!time)return {...base,kind:'date_only',local_date:date,precision:'date',needs_confirmation:true};
 const desired=`${date}T${time}`,wall=Date.parse(desired+'Z');if(!Number.isFinite(wall))throw new Error('请填写有效的日期和时间');
 let instant=wall;for(let i=0;i<4;i++){const represented=Date.parse(localParts(new Date(instant),zone)+'Z');instant+=wall-represented;}
 if(localParts(new Date(instant),zone)!==desired)throw new Error('这个时区不存在所选时刻，请调整时间');
 // A repeated DST wall time requires an explicit offset; this form deliberately refuses to guess.
 if([-3600000,3600000].some(delta=>localParts(new Date(instant+delta),zone)===desired))throw new Error('该时刻处于夏令时重叠区间，请改用 UTC 时间确认');
 const t:Temporal={...base,kind:'absolute',precision:'minute',needs_confirmation:false};
 if(['interview','written_exam'].includes(kind))t.starts_at=new Date(instant).toISOString();else t.due_at=new Date(instant).toISOString();return t;
}
export interface TimeForm {date:string;time:string;timezone:string;endDate?:string;endTime?:string;reference_kind?:string;reference_at?:string}
export function proposalForm(t:Temporal,candidate:Record<string,unknown>):TimeForm{
 const zone=t.timezone??displayZone,stamp=candidate.starts_at??candidate.due_at;
 if(typeof stamp==='string'&&Number.isFinite(Date.parse(stamp))){const p=localParts(new Date(stamp),zone);return {date:p.slice(0,10),time:p.slice(11,16),timezone:zone};}
 return {date:typeof candidate.local_date==='string'?candidate.local_date:'',time:'',timezone:zone};
}
export function formFromTemporal(t?:Temporal):TimeForm{const zone=t?.timezone??displayZone;const stamp=t?.starts_at??t?.due_at;if(stamp){const p=localParts(new Date(stamp),zone);return {date:p.slice(0,10),time:p.slice(11,16),timezone:zone};}if(!t?.local_date&&t?.candidates?.length===1)return proposalForm(t,t.candidates[0]!);return {date:t?.local_date??'',time:'',timezone:zone};}
export function endFromTemporal(t?:Temporal){return t?.ends_at?proposalForm(t,{starts_at:t.ends_at}):{date:'',time:'',timezone:t?.timezone??displayZone};}
export function reviewTemporal(original:Temporal,form:TimeForm,kind:string):Temporal{
 const initial=formFromTemporal(original);
 const scheduled=['interview','written_exam'].includes(kind);
 const compatible=scheduled?!original.due_at:!original.starts_at&&!original.ends_at;
 const end=endFromTemporal(original),endUnchanged=(form.endDate??end.date)===end.date&&(form.endTime??end.time)===end.time;
 if(compatible&&form.date===initial.date&&form.time===initial.time&&form.timezone===initial.timezone&&endUnchanged&&(original.starts_at||original.due_at||original.local_date)){
  return {...original,timezone:form.timezone,timezone_source:'user_confirmed',
   needs_confirmation:original.precision!=='minute'};
 }
 const confirmed=temporalFromForm(form.date,form.time,kind,form.timezone);
 const proposals=original.candidates?.filter(c=>{const p=proposalForm(original,c);return p.date===form.date&&p.time===form.time&&(!form.reference_kind||c.reference_kind===form.reference_kind);})??[];
 const chosen=proposals.length===1?proposals[0]:undefined;
 if(scheduled&&Boolean(form.endDate)!==Boolean(form.endTime))throw new Error('请同时填写结束日期和结束时间，或都留空');
 const ends_at=scheduled&&form.endDate&&form.endTime?temporalFromForm(form.endDate,form.endTime,'interview',form.timezone).starts_at:null;
 if(ends_at&&(!confirmed.starts_at||new Date(ends_at)<=new Date(confirmed.starts_at)))throw new Error('结束时间必须晚于开始时间');
 return {...original,...confirmed,raw_text:original.raw_text,starts_at:confirmed.starts_at??null,
  due_at:confirmed.due_at??null,ends_at,local_date:confirmed.local_date??null,
  reference_kind:(chosen?.reference_kind as string|undefined)??original.reference_kind,
  reference_at:(chosen?.reference_at as string|undefined)??original.reference_at};
}
const issueNames:Record<string,string>={TIMEZONE_UNCONFIRMED:'请核对事件时间',YEAR_OR_DATE_MISSING:'年份或日期需要确认',RELATIVE_TIME_REQUIRES_CONFIRMATION:'请选择时间依据',MULTIPLE_TIMES:'有多个时间，请选择本事件的时间',CHANGE_REQUIRES_REVIEW:'请关联需要改期或取消的原事件',TEXT_TRUNCATED:'正文已截断，请核对原邮件',COMPANY_MISSING:'公司未识别，可确认后保存',FORWARDED_MESSAGE:'转发邮件，请核对时间依据',DUPLICATE_CANDIDATE:'可能重复，请核对已有事件',REPARSE_DIFFERENCE:'重新识别得到更新，请核对',INVALID_TIME_RANGE:'时间区间无效，请手动修正'};
export function issueText(code:string){return issueNames[code]??'请对照原文核对信息';}
export function displayTime(value?:string|null){if(!value)return '未设置具体时刻';const d=new Date(value);if(!Number.isFinite(d.getTime()))return value;return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:displayZone,hour12:false}).format(d);}
export function taskGroup(task:{status:string;due_at?:string|null;starts_at?:string|null;due_date?:string|null},now=new Date()){
 if(['done','cancelled'].includes(task.status))return 'done';const value=task.starts_at??task.due_at;
 const today=localParts(now,displayZone).slice(0,10);
 if(!value){if(!task.due_date)return 'undated';return task.due_date<today?'overdue':task.due_date===today?'today':'upcoming';}
 const day=localParts(new Date(value),displayZone).slice(0,10);if(new Date(value)<now)return 'overdue';if(day===today)return 'today';return 'upcoming';
}
