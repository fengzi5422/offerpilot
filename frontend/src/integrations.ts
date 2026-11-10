export interface AISettings {
 enabled:boolean; base_url:string; model:string; has_secret:boolean; version:number;
 consent_version?:string|null;
 runtime?:{queued?:number;last_error_code?:string|null;paused_until?:string|null};
}
export interface AISettingsDraft {enabled:boolean;baseUrl:string;model:string;apiKey:string;consent:boolean}
export interface AISettingsPatch {expected_version:number;changes:{enabled:boolean;base_url?:string;model?:string;api_key?:string;consent?:true}}
class AIFormError extends Error {}

export function aiSettingsDraft(saved:AISettings):AISettingsDraft{
 return {enabled:saved.enabled,baseUrl:saved.base_url,model:saved.model,apiKey:'',consent:false};
}
export function buildAIPatch(saved:AISettings,draft:AISettingsDraft,intent:'save'|'disable'='save'):AISettingsPatch{
 if(intent==='disable')return {expected_version:saved.version,changes:{enabled:false}};
 const baseUrl=draft.baseUrl.trim(),model=draft.model.trim(),key=draft.apiKey.trim();
 if(baseUrl){
  try{
   const target=new URL(baseUrl);
   const rawHost=/^https?:\/\/(\[[^\]]+\]|[^/:]+)(?::\d+)?(?:\/|$)/i.exec(baseUrl)?.[1]?.toLowerCase();
   const local=['localhost','127.0.0.1','[::1]'].includes(rawHost??'');
   if(baseUrl.length>4096||/[\x00-\x20\x7f]/.test(baseUrl)||baseUrl.includes('?')||baseUrl.includes('#')||baseUrl.includes('\\')||
      /\/\/[^/]*@/.test(baseUrl)||!target.hostname||
      !(target.protocol==='https:'||target.protocol==='http:'&&local))throw new Error();
  }catch{throw new AIFormError('请填写有效的 HTTPS 服务地址；本机模型可用 localhost 或 127.0.0.1 的 HTTP 地址。地址中不能包含密钥、查询参数或片段。');}
 }
 if(model.length>200||/[\x00-\x1f\x7f]/.test(draft.model))throw new AIFormError('模型名称无效，请填写不超过 200 字且不含换行的名称。');
 if(key.length>4096||/[\x00-\x1f\x7f]/.test(draft.apiKey))throw new AIFormError('密钥无效，请重新粘贴不超过 4096 字且不含换行的 API Key。');
 if(draft.enabled){
  if(!baseUrl||!model)throw new AIFormError('请先填写服务地址和模型名称，再启用 AI。');
  if(!saved.has_secret&&!key)throw new AIFormError('首次启用需要填写服务商提供的 API 密钥。');
  if(!draft.consent)throw new AIFormError('请阅读发送范围与费用说明，并勾选确认后启用或保存。');
 }
 const changes:AISettingsPatch['changes']={enabled:draft.enabled,base_url:baseUrl,model};
 if(key)changes.api_key=key;
 if(draft.enabled&&draft.consent)changes.consent=true;
 return {expected_version:saved.version,changes};
}

const messages:Record<string,string>={
 AI_AUTH_FAILED:'AI 服务认证或额度不足，请检查 API 密钥、账户余额与使用权限。',
 AI_RATE_LIMITED:'AI 服务请求较多，稍后会重试。规则识别仍可独立使用。',
 AI_SERVICE_FAILED:'AI 服务暂时不可用，请检查服务状态后重试。规则识别仍可独立使用。',
 AI_OUTPUT_INVALID:'服务返回的建议无法与原文核对，本次保留规则识别结果。',
 AI_OUTPUT_INCOMPLETE:'AI 服务仍未返回完整事件数据，已自动扩大输出预算恢复一次。请稍后重试；已有事件保持不变。',
 AI_COOLDOWN:'AI 连续失败后已暂时暂停，请检查服务配置，或稍后重试。规则识别仍可使用。',
 AI_INPUT_CHANGED:'邮件或设置已变化，本次 AI 已取消；请核对当前内容后重新识别。',
 AI_CONFIG_CHANGED:'AI 配置已改变，请保存当前设置后重新测试。',
 AI_NOT_ENABLED:'请先保存并启用 AI 配置，再测试连接。',
 AI_DISABLED:'请先保存并启用 AI 配置，再测试连接。',
 SERVICE_AUTH_REQUIRED:'服务密钥不可用，请重新填写 API Key 并保存。',
 SERVICE_TIMEOUT:'AI 服务响应超时，请稍后重试。规则识别仍可独立使用。',
 SERVICE_NETWORK_ERROR:'无法连接 AI 服务，请检查服务地址、证书和网络。规则识别仍可独立使用。',
 SERVICE_REDIRECT_REJECTED:'服务地址发生跳转，请填写服务商提供的直接 API 地址。',
 SERVICE_REDIRECT_BLOCKED:'服务地址发生跳转，请填写服务商提供的直接 API 地址。',
 SERVICE_RESPONSE_TOO_LARGE:'服务返回内容过长，本次保留规则识别结果。',
 SERVICE_INPUT_TOO_LARGE:'内容超过服务限制，本次保留规则识别结果。',
 ENDPOINT_INVALID:'服务地址无效。请使用 HTTPS；本机模型可使用 localhost 或 127.0.0.1 的 HTTP 地址。',
 ENDPOINT_BLOCKED:'此服务地址不允许连接，请检查地址；本机模型请明确填写 localhost 或 127.0.0.1。',
 CONSENT_REQUIRED:'请完整配置服务，并勾选确认发送范围与费用后启用。',
 CREDENTIAL_STORE_UNAVAILABLE:'Windows 凭据管理器暂时不可用，密钥尚未安全保存，请检查后重试。',
 VERSION_CONFLICT:'配置已在其他操作中更新，请刷新配置后重新填写。',
 VALIDATION_ERROR:'配置内容无效，请检查地址、模型和密钥后重新保存。',
 AI_QUEUE_FULL:'AI 暂时繁忙，规则识别仍可独立使用，请稍后重试。',
};
export function aiSettingsError(error:unknown):string{
 if(error instanceof AIFormError)return error.message;
 const code=error&&typeof error==='object'&&'code' in error?error.code:undefined;
 return typeof code==='string'&&messages[code]?messages[code]:'AI 设置操作未完成，请检查连接后重试。规则识别仍可独立使用。';
}
export function aiRuntimeMessage(code:unknown):string{
 if(!code)return '';
 return typeof code==='string'&&messages[code]?messages[code]:'AI 上次处理未完成，规则识别仍可独立使用；可检查配置并测试连接。';
}
