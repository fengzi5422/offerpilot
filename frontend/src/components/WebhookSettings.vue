<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue';
import {request} from '../api';
import {webhookDraft,buildWebhookPatch,webhookErrorMessage,webhookResultMessage} from '../webhook';
import type {WebhookSettings,WebhookDraft} from '../webhook';
import Icon from './Icon.vue';
const emit=defineEmits<{changed:[]}>();
const saved=ref<WebhookSettings|null>(null);
const draft=ref<WebhookDraft>({enabled:false,url:'',token:'',clearToken:false,consent:false});
const operation=ref('load'),error=ref(''),message=ref(''),messageTone=ref<'success'|'uncertain'|'error'>('success'),confirmDelete=ref(false);
const busy=computed(()=>!!operation.value);
const unsaved=computed(()=>!!saved.value&&(draft.value.enabled!==saved.value.enabled||!!draft.value.url.trim()||!!draft.value.token.trim()||draft.value.clearToken));
const canTest=computed(()=>!!saved.value?.enabled&&!unsaved.value&&!busy.value);
watch(()=>draft.value.url,()=>{draft.value.consent=false;});
watch(()=>draft.value.clearToken,value=>{if(value)draft.value.token='';});
function apply(value:WebhookSettings){saved.value=value;draft.value=webhookDraft(value);confirmDelete.value=false;}
function clearNotice(){error.value='';message.value='';messageTone.value='success';}
async function load(){
 operation.value='load';clearNotice();
 try{apply(await request<WebhookSettings>('/integrations/webhook'));}
 catch(e){error.value=webhookErrorMessage(e);}finally{operation.value='';}
}
async function save(intent:'save'|'disable'='save'){
 if(!saved.value)return;
 operation.value=intent==='disable'?'disable':'save';clearNotice();
 try{
  const patch=buildWebhookPatch(saved.value,draft.value,intent);
  const next=await request<WebhookSettings>('/integrations/webhook','PATCH',patch);
  apply(next);emit('changed');message.value=intent==='disable'?'外部提醒已停用。本机通知继续按原设置工作。':
   next.enabled?'接收配置已保存并启用，将用于今后的提醒。':'接收配置已保存，外部提醒保持关闭。';
 }catch(e){error.value=webhookErrorMessage(e);}finally{draft.value.token='';operation.value='';}
}
async function remove(){
 if(!saved.value)return;
 operation.value='delete';clearNotice();
 try{
  apply(await request<WebhookSettings>('/integrations/webhook','DELETE',{expected_version:saved.value.version}));
  emit('changed');message.value='接收配置已删除并停用。已有任务和本机通知设置保留。';
 }catch(e){error.value=webhookErrorMessage(e);}finally{draft.value.token='';confirmDelete.value=false;operation.value='';}
}
async function test(){
 if(!canTest.value)return;
 operation.value='test';clearNotice();
 try{
  const result=webhookResultMessage(await request('/integrations/webhook/test','POST',{}));
  message.value=result.text;messageTone.value=result.tone;
 }catch(e){error.value=webhookErrorMessage(e);}finally{operation.value='';}
}
onMounted(load);
</script>

<template>
 <section class="setting-card hook-settings" aria-labelledby="webhook-settings-title" :aria-busy="busy">
  <div class="card-title hook-heading"><Icon name="bell"/><h2 id="webhook-settings-title">外部提醒 · Webhook</h2><span class="badge hook-badge">{{saved?(saved.enabled?'已启用':'已关闭'):'默认关闭'}}</span></div>
  <p class="muted hook-intro">把一条轻量提醒交给你选择的接收服务。手机能否收到通知，取决于接收服务的转发和设备设置。</p>
  <div class="hook-disclosure"><strong>只发送一条泛化提示</strong><p>发送内容为「你有一项求职待办，请打开OfferPilot查看」、应用名称与防重复标识，不包含任务标题、邮件正文、邮箱地址或链接。</p><p>这是独立的外部提醒通道；电脑上的应用必须运行，退出、休眠或关机期间无法发送。接收服务受理请求不代表手机已显示或已读；服务可能另行收费。</p></div>
  <p v-if="operation==='load'&&!saved" class="muted hook-loading" role="status">正在读取接收配置…</p>
  <form v-if="saved" class="hook-form" @submit.prevent="save()">
   <label class="setting-toggle hook-enable"><span><strong>保存后启用外部提醒</strong><small>当前{{saved.enabled?'已启用':'已关闭'}}；本机通知可独立使用</small></span><input v-model="draft.enabled" type="checkbox" :disabled="busy"/></label>
   <div v-if="saved.has_secret" class="hook-saved"><span>已保存接收服务</span><strong>{{saved.endpoint_origin||'已安全保存'}}</strong><small>完整地址与令牌不会回显</small></div>
   <label>HTTPS 接收地址<input v-model="draft.url" type="url" :placeholder="saved.has_secret?'留空保留已保存的完整地址':'https://接收服务/你的通知入口'" :disabled="busy" :required="draft.enabled&&!saved.has_secret" maxlength="4096" autocomplete="off" spellcheck="false" aria-describedby="webhook-url-help"/></label>
   <p id="webhook-url-help" class="field-help">只支持公网 HTTPS 地址，不连接本机、私网或跳转地址。完整地址可能含专属凭据，将与令牌一起安全保存在本机 Windows 凭据管理器中，不进入备份。</p>
   <label>Bearer 令牌（可选）<input v-model="draft.token" type="password" :placeholder="saved.has_secret?'留空保留原令牌；更换地址时不会沿用':'接收服务不需要时可留空'" :disabled="busy||draft.clearToken" maxlength="4096" autocomplete="new-password"/></label>
   <p class="field-help">输入新地址时，请重新填写该服务的令牌；留空将以无 Bearer 令牌方式连接新地址。未修改地址时，令牌留空会保留原配置。</p>
   <label v-if="saved.has_secret" class="hook-check"><input v-model="draft.clearToken" type="checkbox" :disabled="busy"/><span>清除已保存的 Bearer 令牌，接收服务不需要此认证</span></label>
   <label class="hook-consent"><input v-model="draft.consent" type="checkbox" :disabled="busy"/><span>我同意将上述泛化提醒发送给所填接收服务，了解接收与已读的区别、运行限制及可能产生的服务费用。<small>每次启用或保存启用配置都需确认；更换接收地址后需重新勾选。</small></span></label>
   <div class="button-row wrap hook-actions"><button type="submit" class="button primary" :disabled="busy">{{operation==='save'?'正在保存…':draft.enabled?'保存并启用':'保存配置，保持关闭'}}</button><button v-if="saved.enabled" type="button" class="button" :disabled="busy" @click="save('disable')">{{operation==='disable'?'正在停用…':'立即停用外部提醒'}}</button></div>
   <div class="setting-divider"/>
   <div class="hook-test-head"><strong>发送固定内容测试</strong><span>不使用真实任务内容</span></div>
   <p class="field-help">先保存并启用配置，再发送一条测试。接收端显示“已受理”后，请自行核对手机通知。测试请求不会自动重发。</p>
   <div class="button-row wrap hook-actions"><button type="button" class="button" :disabled="!canTest" @click="test">{{operation==='test'?'正在发送测试…':'发送泛化测试提醒'}}</button><button type="button" class="text-button" :disabled="busy" @click="load">刷新配置</button></div>
   <p v-if="unsaved" class="field-help">有尚未保存的更改，保存后才能测试当前配置。</p>
   <details class="advanced hook-compatibility"><summary>接收服务兼容要求</summary><p>接收服务需支持 HTTPS JSON POST，读取 app、message 与 idempotency_key 字段，并按防重复标识去重；可选认证方式为 Authorization: Bearer。并非所有手机推送平台都接受这一格式，请先确认服务兼容。</p></details>
   <div class="hook-delete"><button type="button" class="text-button danger" :disabled="busy" @click="confirmDelete=!confirmDelete">删除接收配置与凭据</button><div v-if="confirmDelete" class="hook-delete-confirm"><p>删除会停用外部提醒，并移除已保存的地址和令牌。已有任务和本机通知设置保留。</p><div class="button-row wrap hook-actions"><button type="button" class="button" :disabled="busy" @click="confirmDelete=false">保留配置</button><button type="button" class="button danger" :disabled="busy" @click="remove">确认删除配置</button></div></div></div>
  </form>
  <p v-if="error" class="error-box" role="alert">{{error}}</p>
  <p v-if="message" :class="messageTone==='success'?'success-box':'error-box'" role="status" aria-live="polite">{{message}}</p>
  <button v-if="!saved&&operation!=='load'" class="button" :disabled="busy" @click="load">重新读取配置</button>
 </section>
</template>

<style scoped>
.hook-settings{align-self:start}.hook-heading{flex-wrap:wrap;gap:9px;margin-bottom:14px}.hook-badge{margin-left:auto;font-size:10px}.hook-intro{line-height:1.9}.hook-disclosure{margin:19px 0 7px;padding:16px 17px;background:#f1f5ec;border:1px solid #dfe8d7;border-radius:7px;color:#60765a;font-size:11px;line-height:1.9;overflow-wrap:anywhere}.hook-disclosure strong{display:block;font-weight:500;color:#41623f;margin-bottom:7px}.hook-disclosure p+p{margin-top:8px}.hook-form{display:grid;gap:13px;min-width:0}.hook-enable{margin:12px 0 3px}.hook-form>label:not(.setting-toggle):not(.hook-consent):not(.hook-check){display:grid;gap:8px}.hook-form input:not([type=checkbox]){min-width:0;width:100%}.hook-form .field-help{margin:0;color:#7c8d73;line-height:1.85}.hook-saved{display:grid;gap:6px;border-left:2px solid #b0c29f;background:#f6f8f1;padding:11px 13px;overflow-wrap:anywhere}.hook-saved span,.hook-saved small{font-size:10px;color:#84937b}.hook-saved strong{font-size:11px;font-weight:500;color:#607354}.hook-consent,.hook-check{display:flex;align-items:flex-start;gap:10px;font-size:11px;line-height:1.9;color:#596f50}.hook-consent{background:#fafbf7;border:1px solid #e6ebde;border-radius:6px;padding:13px;margin-top:4px}.hook-consent input,.hook-check input{width:16px;min-height:16px;height:16px;margin:3px 0 0;flex-shrink:0;accent-color:var(--green)}.hook-consent span,.hook-check span{min-width:0}.hook-consent small{display:block;font-size:10px;line-height:1.8;color:#84927b;margin-top:6px}.hook-actions{margin:0;align-items:center}.hook-form>.setting-divider{margin:9px 0}.hook-test-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;color:#5a7151}.hook-test-head strong{font-size:12px;font-weight:500}.hook-test-head>span{font-size:10px;color:#89977f}.hook-compatibility{margin:4px 0;line-height:1.9;overflow-wrap:anywhere}.hook-compatibility summary{margin-bottom:6px}.hook-delete{border-top:1px solid #e9ede2;margin-top:6px;padding-top:7px}.hook-delete-confirm{padding:13px;background:#faf6ef;border-radius:6px;font-size:11px;color:#8b785e;line-height:1.85}.hook-delete-confirm .hook-actions{margin-top:12px}.hook-loading{padding:20px 0;font-size:12px}@media(max-width:520px){.hook-settings{padding:20px 17px}.hook-actions .button{flex:1;white-space:normal}.hook-disclosure{padding:14px}.hook-consent{padding:12px}}
</style>
