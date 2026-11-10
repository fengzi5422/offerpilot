import {kindNames} from './domain';

export function mailboxErrorMessage(code:string):string{
 const messages:Record<string,string>={
  MAIL_AUTH_FAILED:'邮箱认证失败，请确认已开启 IMAP，并更新授权码。',
  MAIL_NETWORK_ERROR:'暂时无法连接邮箱，稍后将按下方时间自动重试；也可手动立即同步。',
  MAIL_FETCH_FAILED:'邮件正文读取未完成，已保留同步进度，将按下方时间重试。连接测试只验证登录和打开收件箱。',
  MAIL_STRUCTURE_INVALID:'邮件正文结构无法读取，已保留同步进度，将按下方时间重试。',
  MAIL_SEARCH_FAILED:'邮箱暂未完成邮件查询，已保留同步进度，将按下方时间重试。',
  MAIL_PROTOCOL_ERROR:'邮箱返回了不完整的响应，已保留同步进度，将按下方时间重试。',
  MAIL_FOLDER_UNAVAILABLE:'无法只读打开收件箱，请确认 IMAP 服务已开启。',
  MAIL_CLIENT_ID_FAILED:'邮箱未接受客户端身份，请确认已开启 IMAP，并使用邮箱授权码。',
 };
 return messages[code]??'上次同步未完成，已保留已有邮件；可稍后重试或查看诊断。';
}

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
export async function saveAndTestMailbox(state:{accountId:string},input:{address:string;code:string;initialScan:string;provider?:string},call:Call,wait:(id:string)=>Promise<any>){
 if(state.accountId)await call(`/accounts/${state.accountId}/credential`,'PUT',{authorization_code:input.code});
 else {const account=await call('/accounts','POST',{provider:input.provider??'qq',address:input.address.trim(),authorization_code:input.code,initial_scan:input.initialScan});state.accountId=account.id;}
 const job=await call(`/accounts/${state.accountId}/test`,'POST');
 if(job.job_id)await wait(job.job_id);
}
