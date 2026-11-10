export interface WebhookSettings {enabled:boolean;endpoint_origin:string;has_secret:boolean;version:number;consent_version?:string|null}
export interface WebhookDraft {enabled:boolean;url:string;token:string;clearToken:boolean;consent:boolean}
export interface WebhookPatch {expected_version:number;changes:{enabled:boolean;url?:string;token?:string;consent?:true}}
export interface WebhookResult {status?:string;accepted?:boolean;read?:boolean;error_code?:string}
class WebhookFormError extends Error {}
export function webhookDraft(saved:WebhookSettings):WebhookDraft{
 return {enabled:saved.enabled,url:'',token:'',clearToken:false,consent:false};
}
export function buildWebhookPatch(saved:WebhookSettings,draft:WebhookDraft,intent:'save'|'disable'='save'):WebhookPatch{
 if(intent==='disable')return {expected_version:saved.version,changes:{enabled:false}};
 const url=draft.url.trim(),token=draft.token.trim();
 if(url){
  try{
   const target=new URL(url),host=target.hostname.replace(/^\[|\]$/g,'').toLowerCase();
   const octets=/^\d+\.\d+\.\d+\.\d+$/.test(host)?host.split('.').map(Number):null;
   const [first=0,second=0]=octets??[];
   const privateIp=octets&&(first===0||first===10||first===127||
    first===169&&second===254||first===172&&second>=16&&second<=31||
    first===192&&second===168||first===100&&second>=64&&second<=127);
   if(url.length>4096||/[\x00-\x20\x7f]/.test(url)||url.includes('\\')||url.includes('#')||
    /\/\/[^/]*@/.test(url)||target.protocol!=='https:'||!host||host==='localhost'||host.endsWith('.localhost')||
    privateIp||host==='::'||host==='::1'||/^(fc|fd|fe[89ab])[0-9a-f]*:/i.test(host))throw new Error();
  }catch{throw new WebhookFormError('接收地址须为有效的公网 HTTPS 地址，不能包含账户、片段或换行，也不能使用本机或私网地址。');}
 }
 if(token.length>4096||/[\x00-\x1f\x7f-\uffff]/.test(draft.token))throw new WebhookFormError('通知令牌无效，请重新粘贴不含换行的英文或数字令牌，最多 4096 字。');
 if(!url&&!saved.has_secret&&(draft.enabled||token||draft.clearToken))throw new WebhookFormError('请先填写完整的 HTTPS 接收地址。');
 if(draft.enabled&&!draft.consent)throw new WebhookFormError('请阅读泛化提醒与发送限制，并勾选同意后启用或保存。');
 const changes:WebhookPatch['changes']={enabled:draft.enabled};
 if(url){changes.url=url;changes.token=draft.clearToken?'':token;}
 else if(draft.clearToken)changes.token='';
 else if(token)changes.token=token;
 if(draft.enabled&&draft.consent)changes.consent=true;
 return {expected_version:saved.version,changes};
}
const messages:Record<string,string>={
 WEBHOOK_DISABLED:'请先保存并启用接收服务，再发送测试。',
 WEBHOOK_CONFIG_CHANGED:'接收配置已改变，请刷新设置后重新测试。',
 WEBHOOK_CONFIG_UNAVAILABLE:'接收配置暂时不可用，请检查凭据管理器并重新保存。',
 WEBHOOK_CONFIG_INVALID:'已保存的接收配置无效，请重新填写地址与可选令牌。',
 WEBHOOK_KEY_INVALID:'本次提醒标识无效，请刷新后重试。',
 WEBHOOK_AUTH_FAILED:'接收服务拒绝认证，请检查地址和 Bearer 令牌，不会自动重试本次提醒。',
 WEBHOOK_REQUEST_REJECTED:'接收服务拒绝了请求，请确认它支持通用 JSON 提醒，不会自动重试本次提醒。',
 WEBHOOK_RATE_LIMITED:'接收服务暂时限流。定时提醒会在有效期内有限重试；测试请求不会自动重发。',
 WEBHOOK_SERVICE_FAILED:'接收服务暂时不可用。定时提醒会在有效期内有限重试；测试请求不会自动重发。',
 WEBHOOK_RESULT_UNKNOWN:'本次发送结果未知，接收方可能已受理。应用不会自动重发，避免重复提醒；请先在接收服务中核对。',
 WEBHOOK_REDIRECT_BLOCKED:'接收地址发生跳转，本次已停止。请填写服务提供的最终 HTTPS 接收地址。',
 SERVICE_REDIRECT_BLOCKED:'接收地址发生跳转，本次已停止。请填写最终 HTTPS 接收地址。',
 ENDPOINT_INVALID:'接收地址无效，请使用公网 HTTPS 地址。',
 ENDPOINT_BLOCKED:'接收地址指向本机、私网或不允许的网络，请使用公网 HTTPS 接收服务。',
 SERVICE_AUTH_REQUIRED:'无法读取接收服务凭据，请重新保存地址和可选令牌。',
 CREDENTIAL_STORE_UNAVAILABLE:'Windows 凭据管理器暂时不可用，请检查后重新保存。',
 CONSENT_REQUIRED:'请完整填写接收配置，并勾选同意发送泛化提醒后启用。',
 VERSION_CONFLICT:'配置已在其他操作中更新，请刷新后重新填写。',
 VALIDATION_ERROR:'接收配置无效，请检查地址与可选令牌后重新保存。',
 DELIVERY_SUPPRESSED:'该提醒已暂停或取消，没有继续发送。',
 REMINDER_CANCELLED:'该提醒已暂停或取消，没有继续发送。',
};
export function webhookErrorMessage(error:unknown):string{
 if(error instanceof WebhookFormError)return error.message;
 const code=error&&typeof error==='object'?('error_code' in error?error.error_code:'code' in error?error.code:undefined):undefined;
 return typeof code==='string'&&messages[code]?messages[code]:'接收服务操作未完成，请检查配置后重试；本机提醒继续独立工作。';
}
export function webhookResultMessage(result:unknown):{tone:'success'|'uncertain'|'error';text:string}{
 if(result&&typeof result==='object'&&'status' in result){
  if(result.status==='sent'&&'accepted' in result&&result.accepted===true)return {tone:'success',text:'接收服务已受理测试请求，不代表手机已显示或已读。请在你配置的接收服务中核对通知。'};
  if(result.status==='unknown')return {tone:'uncertain',text:messages.WEBHOOK_RESULT_UNKNOWN!};
 }
 return {tone:'error',text:webhookErrorMessage(result)};
}
