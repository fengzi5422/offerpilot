import {aiRuntimeMessage} from './integrations';
import {mailboxErrorMessage} from './settings';
export class ApiError extends Error { constructor(public code:string,message:string,public status:number,public validation?:{reason_code?:string;field?:string}){super(message);} }
let csrf='';
const pendingKeys=new Map<string,string>();
export async function request<T=any>(path:string,method='GET',body?:unknown,key?:string,envelope=false):Promise<T>{
 const headers:Record<string,string>={};
 const command=method+path+(body instanceof FormData?'upload':JSON.stringify(body??null));
 if(method!=='GET'){headers['X-CSRF-Token']=csrf;if(!pendingKeys.has(command))pendingKeys.set(command,key??crypto.randomUUID());headers['Idempotency-Key']=key??pendingKeys.get(command)!;}
 if(method!=='GET' && !(body instanceof FormData))headers['Content-Type']='application/json';
 const response=await fetch(`/api/v1${path}`,{method,headers,credentials:'same-origin',body:method==='GET'?undefined:body instanceof FormData?body:JSON.stringify(body??{})});
 const result=await response.json().catch(()=>({}));
 if(response.ok||response.status<500)pendingKeys.delete(command);
 if(!response.ok)throw new ApiError(result.error?.code??'REQUEST_FAILED',result.error?.message??`请求失败（${response.status}）`,response.status,result.error);
 return (envelope?result:result.data) as T;
}
export function requestPage<T>(path:string){return request<{data:T[];next_cursor?:string|null}>(path,'GET',undefined,undefined,true);}
export async function connect(){
 const fragment=new URLSearchParams(location.hash.slice(1));
 const code=fragment.get('code')??fragment.get('bootstrap');
 if(code){history.replaceState(null,'',location.pathname+location.search);const data=await request('/session/bootstrap','POST',{code});csrf=data.csrf_token??'';}
 const session=await request('/session');csrf=session.csrf_token;
}
export async function waitJob(id:string,options:{onProgress?:(job:any)=>void;isCurrent?:()=>boolean;timeoutMs?:number}={}){
 const deadline=Date.now()+(options.timeoutMs??90000);
 while(Date.now()<deadline){
  if(options.isCurrent&&!options.isCurrent())return null;
  const job=await request(`/jobs/${id}`);
  if(options.isCurrent&&!options.isCurrent())return null;
  options.onProgress?.(job);
  if(job.status==='succeeded'||job.state==='succeeded')return job;
  if(['failed','interrupted'].includes(job.status??job.state))throw new Error(job.error_code?.startsWith('MAIL_')?mailboxErrorMessage(job.error_code):jobErrors[job.error_code]??(job.kind==='ai_parse'?aiRuntimeMessage(job.error_code,job.progress?.validation):'操作未完成，请重试或在诊断中查看状态。'));
  await new Promise(r=>setTimeout(r,1000));
 }
 throw new Error('操作仍在后台进行，请稍后查看状态');
}
const jobErrors:Record<string,string>={INVALID_EML:'这份文件不是可识别的邮件，请从邮箱重新导出 .eml。',BACKUP_INVALID:'备份文件损坏或内容不受支持，请重新选择。',BACKUP_SCHEMA_INVALID:'备份结构不兼容或已损坏，当前数据未改变。',BACKUP_VERSION_UNSUPPORTED:'备份来自不受支持的版本，请使用对应版本应用。',MAIL_CLIENT_ID_FAILED:'邮箱未接受客户端身份，请确认 IMAP 已开启，并使用正确的授权码。',MAIL_PROTOCOL_ERROR:'邮箱返回不完整，已保留扫描进度，请稍后重试。',MAIL_AUTH_FAILED:'邮箱认证失败，请检查 IMAP 开关并更新授权码。',MAIL_NETWORK_ERROR:'连接失败，请检查网络后重试。',JOB_FAILED:'操作未完成，请重试并查看诊断。',APP_RESTARTED:'上次操作因应用重启而中断，请重试。'};
export async function waitMail(id:string){
 for(let i=0;i<90;i++){
  const mail=await request(`/emails/${id}`);
  if(mail.state==='parse_failed')throw new Error('邮件已保存，但识别失败。请稍后重新识别；临时正文过期时请重新导入原邮件。');
  if(mail.state!=='new')return mail;
  await new Promise(r=>setTimeout(r,1000));
 }
 throw new Error('邮件已保存，仍在识别中，可稍后刷新收件列表。');
}
export function errorText(e:unknown){return e instanceof Error?e.message:'操作失败，请重试';}
