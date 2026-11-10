<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue';
import {request} from '../api';
import {displayTime} from '../domain';
import {aiSettingsDraft,buildAIPatch,aiSettingsError,aiRuntimeMessage} from '../integrations';
import type {AISettings,AISettingsDraft} from '../integrations';
import Icon from './Icon.vue';

const emit=defineEmits<{changed:[]}>();
const saved=ref<AISettings|null>(null);
const draft=ref<AISettingsDraft>({enabled:false,baseUrl:'',model:'',apiKey:'',consent:false});
const operation=ref('load'),error=ref(''),message=ref(''),confirmDelete=ref(false);
const busy=computed(()=>!!operation.value);
const unsaved=computed(()=>!!saved.value&&(draft.value.enabled!==saved.value.enabled||
 draft.value.baseUrl.trim()!==saved.value.base_url||draft.value.model.trim()!==saved.value.model||!!draft.value.apiKey.trim()));
const runtimeMessage=computed(()=>aiRuntimeMessage(saved.value?.runtime?.last_error_code,saved.value?.runtime?.validation));
const canTest=computed(()=>!!saved.value?.enabled&&!unsaved.value&&!busy.value);
watch(()=>[draft.value.baseUrl,draft.value.model],()=>{draft.value.consent=false;});

function apply(value:AISettings){saved.value=value;draft.value=aiSettingsDraft(value);confirmDelete.value=false;}
async function load(){
 operation.value='load';error.value='';message.value='';
 try{apply(await request<AISettings>('/integrations/ai'));}
 catch(e){error.value=aiSettingsError(e);}finally{operation.value='';}
}
async function save(intent:'save'|'disable'='save'){
 if(!saved.value)return;
 operation.value=intent==='disable'?'disable':'save';error.value='';message.value='';
 try{
  const patch=buildAIPatch(saved.value,draft.value,intent);
  const next=await request<AISettings>('/integrations/ai','PATCH',patch);
  apply(next);emit('changed');
  message.value=intent==='disable'?'AI 已停用。规则识别继续在本机工作。':
   next.enabled?'AI 配置已保存并启用。AI 建议仍需你审核。':'配置已保存，AI 保持关闭。';
 }catch(e){error.value=aiSettingsError(e);}finally{draft.value.apiKey='';operation.value='';}
}
async function remove(){
 if(!saved.value)return;
 operation.value='delete';error.value='';message.value='';
 try{
  apply(await request<AISettings>('/integrations/ai','DELETE',{expected_version:saved.value.version}));
  emit('changed');message.value='AI 配置已删除并停用。已有邮件和任务继续保留。';
 }catch(e){error.value=aiSettingsError(e);}finally{draft.value.apiKey='';operation.value='';confirmDelete.value=false;}
}
async function test(){
 if(!canTest.value)return;
 operation.value='test';error.value='';message.value='正在用固定示例测试 AI 服务，通常需要几秒，自动恢复时最多约一分钟，请稍候…';
 try{
  const result=await request<{status:string}>('/integrations/ai/test','POST',{});
  if(result.status!=='validated')throw new Error();
  message.value='固定示例连接测试通过，服务返回了可校验的建议。本次没有发送你的真实邮件；实际邮件的处理结果仍需审核。';
 }catch(e){message.value='';error.value=aiSettingsError(e);}finally{operation.value='';}
}
onMounted(load);
</script>

<template>
 <section class="setting-card ai-settings" aria-labelledby="ai-settings-title" :aria-busy="busy">
  <div class="card-title ai-heading"><Icon name="leaf"/><h2 id="ai-settings-title">AI 辅助解析</h2><span class="badge ai-badge">{{saved?(saved.enabled?'已启用':'已关闭'):'默认关闭'}}</span></div>
  <p class="muted ai-intro">需要时，再多一份建议。规则识别始终可以独立在本机工作；AI 给出的事项会进入审核，由你确认。</p>
  <div class="ai-disclosure">
   <strong>启用前，请了解发送范围</strong>
   <p>应用会把必要的邮件主题与正文片段发送到你填写的模型服务。邮箱、手机号、个人称呼等联系信息会尽量脱敏，链接只保留域名；脱敏并不等于完全匿名，内容仍可能识别你的身份。</p>
   <p>服务可能产生费用，由你与服务商结算；本机模型的地址和费用由你自行确认。未启用时，不会调用 AI 服务。</p>
  </div>
  <p v-if="operation==='load'&&!saved" class="muted ai-loading" role="status">正在读取 AI 设置…</p>
  <form v-if="saved" class="ai-form" @submit.prevent="save()">
   <label class="setting-toggle ai-enable"><span><strong>保存后启用 AI 辅助解析</strong><small>当前{{saved.enabled?'已启用':'已关闭'}}；修改开关后需要保存</small></span><input v-model="draft.enabled" type="checkbox" :disabled="busy"/></label>
   <label>服务地址<input v-model="draft.baseUrl" type="url" placeholder="https://服务商地址/v1" :disabled="busy" maxlength="4096" autocomplete="off" :required="draft.enabled" aria-describedby="ai-endpoint-help"/></label>
   <p id="ai-endpoint-help" class="field-help">填写 OpenAI 兼容 API 的基础地址，通常以 /v1 结尾，不要包含 /chat/completions、密钥或查询参数。本机模型可用 http://localhost:端口/v1。</p>
   <label>模型名称<input v-model="draft.model" type="text" placeholder="填写服务商提供的模型名称" :disabled="busy" maxlength="200" autocomplete="off" :required="draft.enabled"/></label>
   <label>API Key<input v-model="draft.apiKey" type="password" :placeholder="saved.has_secret?'已安全保存，留空保留原密钥':'请输入模型服务的 API Key'" :disabled="busy" maxlength="4096" autocomplete="new-password" :required="draft.enabled&&!saved.has_secret" aria-describedby="ai-secret-help"/></label>
   <p id="ai-secret-help" class="field-help">{{saved.has_secret?'已保存密钥不会回显；留空不会清除原密钥。':'首次启用需要填写密钥。'}} 密钥安全存放在本机 Windows 凭据管理器中，不写入邮件或备份。</p>
   <label class="ai-consent"><input v-model="draft.consent" type="checkbox" :disabled="busy"/><span>我已阅读并同意将上述必要内容发送给所填服务，了解脱敏并非完全匿名，并承担可能产生的服务费用。<small>每次启用或保存启用配置都需确认；修改地址或模型后需重新勾选。</small></span></label>
   <div class="button-row wrap ai-actions">
    <button type="submit" class="button primary" :disabled="busy">{{operation==='save'?'正在保存…':draft.enabled?'保存并启用':'保存配置，保持关闭'}}</button>
    <button v-if="saved.enabled" type="button" class="button" :disabled="busy" @click="save('disable')">{{operation==='disable'?'正在停用…':'立即停用 AI'}}</button>
   </div>
   <div class="setting-divider"/>
   <div class="ai-test-head"><strong>用固定示例测试连接</strong><span>不发送真实邮件</span></div>
   <p class="field-help">测试会联系已保存的模型服务，也可能产生少量费用。请先保存变更并启用配置，再点击测试。</p>
   <div class="button-row wrap ai-actions"><button type="button" class="button" :disabled="!canTest" @click="test">{{operation==='test'?'正在测试连接…':'发送固定示例测试'}}</button><button type="button" class="text-button" :disabled="busy" @click="load">刷新配置与状态</button></div>
   <p v-if="unsaved" class="field-help">有尚未保存的更改，保存后才能测试当前配置。</p>
   <div v-if="runtimeMessage||saved.runtime?.paused_until" class="ai-runtime" role="status"><p v-if="runtimeMessage">{{runtimeMessage}}</p><p v-if="saved.runtime?.paused_until">AI 服务已临时暂停，将在 {{displayTime(saved.runtime.paused_until)}} 后尝试恢复。规则识别继续工作。</p></div>
   <div class="ai-delete"><button type="button" class="text-button danger" :disabled="busy" @click="confirmDelete=!confirmDelete">删除 AI 配置与密钥</button><div v-if="confirmDelete" class="ai-delete-confirm"><p>删除会停用 AI 并移除已保存的服务地址、模型和密钥。已有邮件与任务保留。</p><div class="button-row wrap ai-actions"><button type="button" class="button" :disabled="busy" @click="confirmDelete=false">保留配置</button><button type="button" class="button danger" :disabled="busy" @click="remove">确认删除配置</button></div></div></div>
  </form>
  <p v-if="error" class="error-box" role="alert">{{error}}</p>
  <p v-if="message" class="success-box" role="status" aria-live="polite">{{message}}</p>
  <button v-if="!saved&&operation!=='load'" class="button" :disabled="busy" @click="load">重新读取设置</button>
 </section>
</template>

<style scoped>
.ai-settings{align-self:start}.ai-heading{flex-wrap:wrap;gap:9px;margin-bottom:14px}.ai-badge{margin-left:auto;font-size:10px}.ai-intro{line-height:1.9}.ai-disclosure{margin:19px 0 7px;padding:16px 17px;background:#f1f5ec;border:1px solid #dfe8d7;border-radius:7px;color:#60765a;font-size:11px;line-height:1.9;overflow-wrap:anywhere}.ai-disclosure strong{display:block;font-weight:500;color:#41623f;margin-bottom:7px}.ai-disclosure p+p{margin-top:8px}.ai-form{display:grid;gap:13px;min-width:0}.ai-enable{margin:12px 0 3px}.ai-form>label:not(.setting-toggle):not(.ai-consent){display:grid;gap:8px}.ai-form input:not([type=checkbox]){min-width:0;width:100%}.ai-form .field-help{margin:0;color:#7c8d73;line-height:1.85}.ai-consent{display:flex;align-items:flex-start;gap:10px;background:#fafbf7;border:1px solid #e6ebde;border-radius:6px;padding:13px;margin-top:4px;font-size:11px;line-height:1.9;color:#596f50}.ai-consent input{width:16px;min-height:16px;height:16px;margin:3px 0 0;flex-shrink:0;accent-color:var(--green)}.ai-consent span{min-width:0}.ai-consent small{display:block;font-size:10px;line-height:1.8;color:#84927b;margin-top:6px}.ai-actions{margin:0;align-items:center}.ai-form>.setting-divider{margin:9px 0}.ai-test-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;color:#5a7151}.ai-test-head strong{font-size:12px;font-weight:500}.ai-test-head>span{font-size:10px;color:#89977f}.ai-runtime{padding:12px 14px;border-left:2px solid #b1c19e;background:#f6f7f1;font-size:11px;line-height:1.9;color:#718164;overflow-wrap:anywhere}.ai-runtime p+p{margin-top:6px}.ai-delete{border-top:1px solid #e9ede2;margin-top:6px;padding-top:7px}.ai-delete-confirm{padding:13px;background:#faf6ef;border-radius:6px;font-size:11px;color:#8b785e;line-height:1.85}.ai-delete-confirm .ai-actions{margin-top:12px}.ai-loading{padding:20px 0;font-size:12px}@media(max-width:520px){.ai-settings{padding:20px 17px}.ai-actions .button{flex:1;white-space:normal}.ai-disclosure{padding:14px}.ai-consent{padding:12px}}
</style>
