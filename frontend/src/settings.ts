import {kindNames} from './domain';

export type ReminderDraft={offsets:Record<string,string>;quiet:{enabled:boolean;start:string;end:string};privacy:string};
export function reminderDraft(settings:Record<string,any>):ReminderDraft{
 return {offsets:Object.fromEntries(Object.keys(kindNames).map(kind=>[kind,(settings.reminder_offsets_seconds?.[kind]??[]).map((n:number)=>n/60).join(', ')])),
  quiet:{enabled:false,start:'23:00',end:'08:00',...settings.quiet_hours},privacy:settings.notification_privacy??'generic'};
}
export function reminderChanges(draft:ReminderDraft){
 const offsets:Record<string,number[]>={};
 for(const [kind,text] of Object.entries(draft.offsets)){
  const values=text.trim()?text.trim().split(/[,，、\s]+/).map(Number):[];
  if(values.length>10||new Set(values).size!==values.length||values.some(v=>!Number.isInteger(v)||v<1||v>10080))
   throw new Error(`${kindNames[kind]}：请填写最多 10 个不重复的整数分钟（1～10080），留空可关闭。`);
  offsets[kind]=values.map(v=>v*60);
 }
 const q=draft.quiet;
 if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(q.start)||!/^([01]\d|2[0-3]):[0-5]\d$/.test(q.end)||q.start===q.end)
  throw new Error('请设置有效且不同的安静时段起止时间。');
 return {reminder_offsets_seconds:offsets,quiet_hours:{...q},notification_privacy:draft.privacy};
}
type Call=(path:string,method:'POST'|'PUT',body?:any)=>Promise<any>;
export async function saveAndTestMailbox(state:{accountId:string},input:{address:string;code:string;initialScan:string},call:Call,wait:(id:string)=>Promise<any>){
 if(state.accountId)await call(`/accounts/${state.accountId}/credential`,'PUT',{authorization_code:input.code});
 else {const account=await call('/accounts','POST',{provider:'qq',address:input.address.trim(),authorization_code:input.code,initial_scan:input.initialScan});state.accountId=account.id;}
 const job=await call(`/accounts/${state.accountId}/test`,'POST');
 if(job.job_id)await wait(job.job_id);
}
