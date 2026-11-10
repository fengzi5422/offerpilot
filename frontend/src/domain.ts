import type { Temporal } from './types';
let displayZone='Asia/Shanghai';
export function setDisplayTimezone(zone:string){new Intl.DateTimeFormat('zh-CN',{timeZone:zone}).format();displayZone=zone;}
export function changedFields(original:Record<string,unknown>,proposed:Record<string,unknown>){return Object.fromEntries(Object.entries(proposed).filter(([key,value])=>JSON.stringify(original[key])!==JSON.stringify(value)));}
export const kindNames:Record<string,string>={custom:'个人待办',interview:'面试',written_exam:'笔试',assessment:'在线测评',reply:'确认回复',materials:'材料补交',offer:'Offer 确认'};
export const statusNames:Record<string,string>={pending:'待完成',in_progress:'进行中',done:'已完成',cancelled:'已取消',new:'待识别',classified:'已分类',parsed:'已识别',review_needed:'待确认',ignored:'已忽略',parse_failed:'解析失败',sent:'已交给通知渠道',failed:'发送失败',delivery_unknown:'发送结果未确认',missed:'可能错过',suppressed:'已取消计划',ready:'已连接',auth_required:'需要重新授权',network_error:'连接异常',paused:'已暂停'};
function localParts(value:Date,zone:string){const f=new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});return f.format(value).replace(' ','T');}
export function temporalFromForm(date:string,time:string,kind:string,zone:string):Temporal{
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
export function formFromTemporal(t?:Temporal){const zone=t?.timezone??displayZone;const stamp=t?.starts_at??t?.due_at;if(stamp){const p=localParts(new Date(stamp),zone);return {date:p.slice(0,10),time:p.slice(11,16),timezone:zone};}return {date:t?.local_date??'',time:'',timezone:zone};}
export function reviewTemporal(original:Temporal,form:{date:string;time:string;timezone:string},kind:string):Temporal{
 const initial=formFromTemporal(original);
 if(form.date===initial.date&&form.time===initial.time&&form.timezone===initial.timezone){
  return {...original,timezone:form.timezone,timezone_source:'user_confirmed',
   needs_confirmation:original.precision!=='minute'};
 }
 const confirmed=temporalFromForm(form.date,form.time,kind,form.timezone);
 return {...original,...confirmed,raw_text:original.raw_text,starts_at:confirmed.starts_at??null,
  due_at:confirmed.due_at??null,ends_at:null,local_date:confirmed.local_date??null};
}
export function displayTime(value?:string|null){if(!value)return '未设置具体时刻';const d=new Date(value);if(!Number.isFinite(d.getTime()))return value;return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:displayZone,hour12:false}).format(d);}
export function taskGroup(task:{status:string;due_at?:string|null;starts_at?:string|null;due_date?:string|null},now=new Date()){
 if(['done','cancelled'].includes(task.status))return 'done';const value=task.starts_at??task.due_at;
 const today=localParts(now,displayZone).slice(0,10);
 if(!value){if(!task.due_date)return 'undated';return task.due_date<today?'overdue':task.due_date===today?'today':'upcoming';}
 const day=localParts(new Date(value),displayZone).slice(0,10);if(new Date(value)<now)return 'overdue';if(day===today)return 'today';return 'upcoming';
}
