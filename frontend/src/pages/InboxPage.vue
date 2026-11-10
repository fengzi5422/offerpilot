<script setup lang="ts">
import {computed,onMounted,onUnmounted,reactive,ref} from 'vue';
import {request,requestPage,waitJob,waitMail,errorText} from '../api';
import type {Mail,Task,Candidate} from '../types';
import {displayTime,statusNames,formFromTemporal,endFromTemporal,reviewTemporal,kindNames,issueText} from '../domain';
import Icon from '../components/Icon.vue';
import Modal from '../components/Modal.vue';
import TaskForm from '../components/TaskForm.vue';
import EventTimeFields from '../components/EventTimeFields.vue';
import {aiRuntimeMessage} from '../integrations';
const mails=ref<Mail[]>([]),loading=ref(false),busy=ref(false),error=ref(''),message=ref(''),selected=ref<Mail>(),file=ref<HTMLInputElement>(),filter=ref('all'),cursor=ref<string|null>(),edits=reactive<Record<string,any>>({}),tasks=ref<Task[]>([]),editingTask=ref<Task>();
const openCandidates=computed(()=>selected.value?.candidates?.filter(c=>!c.state||c.state==='open')??[]);
const regularCandidates=computed(()=>openCandidates.value.filter(c=>!isChange(c)));
const parseProgress=ref(''),parsingMode=ref('');let disposed=false;
function isChange(c:Candidate){return Array.isArray(c.issues)&&c.issues.includes('CHANGE_REQUIRES_REVIEW');}
function target(c:Candidate){return tasks.value.find(t=>t.id===edits[c.candidate_id]?.task_id);}
function choices(c:Candidate){return tasks.value.filter(t=>!['done','cancelled'].includes(t.status)&&(c.kind==='custom'||t.kind===c.kind));}
async function load(more=false){loading.value=true;try{const result=await requestPage<Mail>('/emails?limit=200'+(filter.value==='review'?'&state=review_needed':'')+(more&&cursor.value?'&cursor='+encodeURIComponent(cursor.value):''));mails.value=more?[...mails.value,...result.data]:result.data;cursor.value=result.next_cursor;}catch(e){error.value=errorText(e);}finally{loading.value=false;}}
async function importFile(e:Event){const f=(e.target as HTMLInputElement).files?.[0];if(!f)return;error.value='';busy.value=true;try{const body=new FormData();body.append('file',f);const job=await request('/emails/import','POST',body);const result=await waitJob(job.job_id);const id=result.result?.message_id;if(id)await waitMail(id);message.value='邮件已导入，识别结果已更新。';await load();if(id)await open({id} as Mail);}catch(e){error.value=errorText(e);}finally{busy.value=false;if(file.value)file.value.value='';}}
async function open(mail:Mail){try{parseProgress.value='';error.value='';selected.value=await request(`/emails/${mail.id}`);for(const c of selected.value?.candidates??[]){const end=endFromTemporal(c.temporal);edits[c.candidate_id]={selected:true,title:c.title,...formFromTemporal(c.temporal),endDate:end.date,endTime:end.time,location:c.event_details?.location??'',meeting_url:c.event_details?.meeting_url??'',task_id:'',action:/取消/.test(c.temporal.raw_text+' '+selected.value?.subject)?'cancel':'reschedule',change_details:Boolean(c.event_details?.location||c.event_details?.meeting_url)};}
 if(selected.value?.candidates?.some(isChange)){tasks.value=[];let next:string|null|undefined;do{const result=await requestPage<Task>('/tasks?limit=200'+(next?'&cursor='+encodeURIComponent(next):''));tasks.value.push(...result.data);next=result.next_cursor;}while(next);}
}catch(e){error.value=errorText(e);}}
function changes(c:Candidate){const e=edits[c.candidate_id];return {title:e.title,kind:c.kind,priority:c.priority,temporal:reviewTemporal(c.temporal,e,c.kind),event_details:{location:e.location,meeting_url:e.meeting_url}};}
async function review(ignore=false){if(!selected.value)return;busy.value=true;error.value='';try{const m=selected.value;const decisions=ignore?openCandidates.value.map(c=>({candidate_id:c.candidate_id,action:'ignore'})):regularCandidates.value.map(c=>({candidate_id:c.candidate_id,action:edits[c.candidate_id].selected?'edit':'ignore',changes:edits[c.candidate_id].selected?changes(c):undefined}));
 if(!decisions.length)decisions.push({candidate_id:'all',action:'ignore'});
 await request(`/emails/${m.id}/review`,'POST',{expected_version:m.version,decisions});selected.value=undefined;message.value=ignore?'已忽略建议，已有事件保持不变。':'事件已保存，可在待办和日历查看。';await load();}catch(e){error.value=errorText(e);}finally{busy.value=false;}}
async function resolve(c:Candidate){if(!selected.value)return;const original=target(c),e=edits[c.candidate_id];if(!original){error.value='请先选择需要修改的原事件。';return;}
 busy.value=true;error.value='';try{const value:any={expected_version:selected.value.version,candidate_id:c.candidate_id,task_id:original.id,expected_task_version:original.version,action:e.action};
 if(e.action==='reschedule'){value.temporal=reviewTemporal(c.temporal,e,original.kind);if(value.temporal.needs_confirmation)throw new Error('请先确认新的日期和具体时间');if(e.change_details)value.event_details={location:e.location,meeting_url:e.meeting_url};}
 if(!confirm(`${e.action==='cancel'?'取消':'改期'}：${original.title}\n原时间：${displayTime(original.starts_at??original.due_at)}\n${e.action==='cancel'?'未发送的提醒将取消。':`新时间：${e.date} ${e.time}\n旧提醒失效，按新时间安排。`}\n已经发送的系统通知无法撤回。是否确认？`))return;
 await request(`/emails/${selected.value.id}/resolve-change`,'POST',value);message.value='原事件已更新，没有创建重复事件。';await open(selected.value);await load();
 }catch(e){error.value=errorText(e);}finally{busy.value=false;}}
async function reparse(mode='rules'){
 if(!selected.value)return;
 const id=selected.value.id,version=selected.value.version;
 const current=()=>!disposed&&selected.value?.id===id;
 busy.value=true;error.value='';parsingMode.value=mode;
 parseProgress.value=mode==='ai'?'正在提交 AI 识别，请稍候…':'正在重新识别…';
 try{
  const job=await request(`/emails/${id}/reparse`,'POST',{expected_version:version,mode});
  const result=await waitJob(job.job_id,{timeoutMs:180000,isCurrent:current,onProgress:job=>{
   if(mode!=='ai')return;
   const retry=job.progress?.retry_at;
   parseProgress.value=job.status==='running'?'AI 正在读取并核对原文，通常需要几秒到半分钟…':
    retry?`服务暂未完成，约 ${Math.max(1,Math.ceil((retry-Date.now())/1000))} 秒后重试。已有事件不受影响。`:'AI 已排队，正在等待服务处理…';
  }});
  if(!result||!current())return;
  if(mode==='rules')await waitMail(id);
  if(!current())return;
  await open({id} as Mail);await load();
  parseProgress.value=mode==='ai'?'AI 识别完成，可核对已填入的字段；已保存或忽略的事件保持不变。':'规则识别已更新。';
 }catch(e){if(current()){
  parseProgress.value='';error.value=errorText(e);
  // Enqueue/failure updates the mail version. Keep the user's edits while
  // refreshing that version, so Retry does not immediately conflict.
  try{const latest=await request<Mail>(`/emails/${id}`);if(current()&&selected.value)selected.value={...selected.value,version:latest.version,classification:latest.classification};}catch{}
 }}
 finally{busy.value=false;parsingMode.value='';}
}
async function editTask(c:Candidate){try{editingTask.value=await request(`/tasks/${c.task_id}`);selected.value=undefined;}catch(e){error.value=errorText(e);}}
let refreshTimer:ReturnType<typeof setInterval>;
onMounted(()=>{load();refreshTimer=setInterval(()=>{if(!document.hidden&&!selected.value&&!busy.value&&!cursor.value&&!loading.value)load();},30000);});
onUnmounted(()=>{disposed=true;clearInterval(refreshTimer);});
</script>
<template><section class="page"><div class="page-eyebrow"><span class="tiny-line"/>从通知，到事件</div><div class="page-heading"><div><h1>收件识别</h1><p>按最早事件时间升序排列，时间未知的邮件排在最后。</p></div><input ref="file" type="file" accept=".eml,message/rfc822" hidden @change="importFile"><button class="button primary" :disabled="busy" @click="file?.click()"><Icon name="upload" :size="18"/>{{busy?'正在处理…':'导入 .eml 邮件'}}</button></div><div class="info-banner"><Icon name="mail" :size="22"/><div><strong>你的邮箱保持原样</strong><p>只读获取邮件。识别建议可以修改；明确的日期时间自动生成待办；省略年份使用设置中的默认年。</p></div></div><p v-if="error&&!selected" class="error-box" role="alert">{{error}}</p><p v-if="message" class="success-box" role="status">{{message}}</p><div class="section-toolbar"><div class="tabs"><button @click="filter='all';load()">全部邮件</button><button @click="filter='review';load()">待确认</button></div><button class="text-button" @click="load()">刷新</button></div><div v-if="loading" class="loading-block">正在读取邮件…</div><div v-else-if="!mails.length" class="empty-state"><Icon name="inbox" :size="36"/><h3>让招聘通知，变成清楚的下一步</h3><p>导入 .eml 邮件，或在设置中连接 QQ、163 或 126 邮箱。</p></div><button v-for="mail in mails" :key="mail.id" class="mail-row" @click="open(mail)"><span class="mail-icon"><Icon name="mail"/></span><span class="mail-main"><strong>{{mail.subject||'无主题邮件'}}</strong><small>{{mail.from_addr}}</small></span><span class="badge" :class="{warning:mail.state==='review_needed'}">{{statusNames[mail.state]??mail.state}}</span><span class="mail-date">{{mail.event_at?displayTime(mail.event_at):mail.event_date??'事件时间待定'}}</span><Icon name="chevron" :size="18"/></button><button v-if="cursor" class="button" @click="load(true)">加载更多邮件</button></section>
<Modal v-if="selected" title="确认事件信息" wide @close="selected=undefined"><div class="review-layout"><div class="mail-evidence"><span class="overline">邮件来源</span><h3>{{selected.subject}}</h3><p class="muted">{{selected.from_addr}}</p><pre>{{selected.text_excerpt||'正文已清理，已保存的事件仍然保留。'}}</pre></div><div class="review-actions"><span class="overline">事件与建议</span>
<fieldset v-for="c in openCandidates" :key="c.candidate_id" class="candidate-card" :disabled="busy" style="min-width:0">
<span class="badge">{{c.parser_source==='ai'?'AI 建议 · 请审核':'本地规则识别'}}</span><template v-if="isChange(c)"><h3>处理事件变更</h3><label>要更新的原事件<select v-model="edits[c.candidate_id].task_id"><option value="">请选择，不会自动合并</option><option v-for="t in choices(c)" :key="t.id" :value="t.id">{{t.title}} · {{displayTime(t.starts_at??t.due_at)}}</option></select></label><div v-if="target(c)" class="subtle-box"><strong>原事件</strong><p>{{target(c)?.title}}</p><p>{{displayTime(target(c)?.starts_at??target(c)?.due_at)}}</p><p>{{target(c)?.event_details?.location||'地点未填写'}}</p></div><label>处理方式<select v-model="edits[c.candidate_id].action"><option value="reschedule">更新原事件时间</option><option value="cancel">取消原事件</option></select></label><template v-if="edits[c.candidate_id].action==='reschedule'"><EventTimeFields :fields="edits[c.candidate_id]" :kind="target(c)?.kind??c.kind" :temporal="c.temporal"/><label class="check-label"><input v-model="edits[c.candidate_id].change_details" type="checkbox"/>同时更新地点或会议链接</label><template v-if="edits[c.candidate_id].change_details"><label>新地点<input v-model="edits[c.candidate_id].location" maxlength="1000"/></label><label>新会议链接<input v-model="edits[c.candidate_id].meeting_url" type="url" maxlength="4096"/></label></template></template><button class="button primary" :disabled="busy||!target(c)" @click="resolve(c)">确认更新原事件</button></template>
<template v-else><label class="check-label"><input v-model="edits[c.candidate_id].selected" type="checkbox"/>{{kindNames[c.kind]??c.kind}}</label><label>事件名称<input v-model="edits[c.candidate_id].title" maxlength="200"/></label><p class="quote">{{c.temporal.raw_text||'邮件未给出明确时间'}}</p><EventTimeFields :fields="edits[c.candidate_id]" :kind="c.kind" :temporal="c.temporal"/><label>地点<input v-model="edits[c.candidate_id].location" maxlength="1000" placeholder="地点或线上"/></label><label>会议链接<input v-model="edits[c.candidate_id].meeting_url" type="url" maxlength="4096" placeholder="https://…"/></label></template>
<p v-if="Array.isArray(c.issues)&&c.issues.length" class="field-help">{{c.issues.map((x:string)=>issueText(x)).join('；')}}</p></fieldset>
<div v-for="c in selected.candidates?.filter(c=>c.state==='accepted')" :key="c.candidate_id" class="subtle-box"><strong>{{c.title}}</strong><p class="field-help">已处理。以保存的事件信息为准，点击下方查看最新时间与地点。</p><button v-if="c.task_id" class="text-button" @click="editTask(c)">查看并编辑已保存的事件 →</button></div>
<p v-if="parseProgress" class="info-banner" role="status" aria-live="polite">{{parseProgress}}</p><p v-if="!parseProgress&&selected.classification?.ai_status" class="field-help" role="status">AI 状态：{{({queued:'已排队',running:'正在识别',succeeded:'识别已完成',failed:aiRuntimeMessage(selected.classification.ai_error_code),retry_wait:'服务暂未完成，等待自动重试'} as Record<string,string>)[selected.classification.ai_status]??'处理中'}}</p><p class="field-help">AI 建议需审核后保存；已接受或忽略的事项不会被 AI 覆盖。</p><p v-if="!selected.candidates?.length" class="subtle-box">没有识别出需要处理的事件。可以手动添加，或重新识别。</p><p v-if="error" class="error-box" role="alert">{{error}}</p></div></div><footer class="modal-actions"><button class="text-button" :disabled="busy" @click="reparse('rules')">规则重新识别</button><button class="text-button" :disabled="busy" @click="reparse('ai')">{{parsingMode==='ai'?'AI 正在识别…':'AI 辅助识别（需启用）'}}</button><div class="spacer"/><button class="button" :disabled="busy" @click="review(true)">忽略建议 / 已处理</button><button v-if="regularCandidates.length" class="button primary" :disabled="busy" @click="review()">确认所选事件</button></footer></Modal>
<Modal v-if="editingTask" title="编辑事件" @close="editingTask=undefined"><TaskForm :task="editingTask" @cancel="editingTask=undefined" @saved="editingTask=undefined;load()"/></Modal></template>
