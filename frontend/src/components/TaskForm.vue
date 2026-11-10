<script setup lang="ts">
import {reactive,ref} from 'vue';
import type {Task,Temporal} from '../types';
import {formFromTemporal,endFromTemporal,reviewTemporal,kindNames,displayTime,changedFields,statusNames} from '../domain';
import {request,errorText} from '../api';
import MailSource from './MailSource.vue';
import EventTimeFields from './EventTimeFields.vue';
import PreparationChecklist from './PreparationChecklist.vue';
import ApplicationLink from './ApplicationLink.vue';
const props=defineProps<{task?:Task}>();
const emit=defineEmits<{saved:[];cancel:[]}>();
const original:Temporal=props.task?.temporal??{kind:'unknown',raw_text:'',timezone:formFromTemporal().timezone,timezone_source:'user_confirmed',precision:'unknown',needs_confirmation:true};
const end=endFromTemporal(original);
const initial={title:props.task?.title??'',kind:props.task?.kind??'custom',priority:props.task?.priority??2,notes:props.task?.notes??'',location:props.task?.event_details?.location??'',meeting_url:props.task?.event_details?.meeting_url??'',...formFromTemporal(original),endDate:end.date,endTime:end.time};
const fields=reactive({...initial}),error=ref(''),busy=ref(false);
const parentVersion=ref(props.task?.version??1),preparationBusy=ref(false),linkBusy=ref(false),stale=ref(false);
function preparationStale(){stale.value=true;error.value='事件已更新，请关闭并重新打开，核对最新信息。';}
async function save(){if(preparationBusy.value||linkBusy.value||stale.value)return;busy.value=true;error.value='';try{
 const basic={title:fields.title.trim(),kind:fields.kind,priority:Number(fields.priority),notes:fields.notes};
 if(!basic.title)throw new Error('请填写事件名称');
 const changes:Record<string,unknown>=props.task?changedFields({title:initial.title,kind:initial.kind,priority:initial.priority,notes:initial.notes},basic):basic;
 const details={location:fields.location.trim(),meeting_url:fields.meeting_url.trim()};
 if(!props.task||details.location!==initial.location||details.meeting_url!==initial.meeting_url)changes.event_details=details;
 const timeChanged=['date','time','timezone','kind','endDate','endTime'].some(key=>(fields as any)[key]!==(initial as any)[key]);
 if(!props.task||timeChanged){
  const temporal=reviewTemporal(original,fields,fields.kind);
  if(!props.task)temporal.raw_text=fields.date+(fields.time?' '+fields.time:'');
  if(props.task&&!confirm(`原时间：${displayTime(props.task.starts_at??props.task.due_at)}\n新时间：${fields.date||'未确定'} ${fields.time||'具体时刻待确认'}\n保存后旧提醒将失效并按新时间安排。是否继续？`))return;
  changes.temporal=temporal;
 }
 if(props.task)await request(`/tasks/${props.task.id}`,'PATCH',{expected_version:parentVersion.value,changes});
 else await request('/tasks','POST',changes);
 emit('saved');
}catch(e){error.value=errorText(e);}finally{busy.value=false;}}
async function start(){if(!props.task||preparationBusy.value||linkBusy.value||stale.value)return;busy.value=true;try{await request(`/tasks/${props.task.id}`,'PATCH',{expected_version:parentVersion.value,changes:{status:'in_progress'}});emit('saved');}catch(e){error.value=errorText(e);}finally{busy.value=false;}}
async function snooze(r:any){busy.value=true;try{await request(`/reminders/${r.id}/snooze`,'POST',{expected_version:r.version,minutes:10});emit('saved');}catch(e){error.value=errorText(e);}finally{busy.value=false;}}
</script>
<template><form @submit.prevent="save"><div class="form-body">
<label>事件名称<input v-model="fields.title" placeholder="例如：示例科技技术面试" maxlength="200" required autofocus/></label>
<div class="form-row"><label>事项类型<select v-model="fields.kind"><option v-for="(label,key) in kindNames" :key="key" :value="key">{{label}}</option></select></label><label>优先级<select v-model="fields.priority"><option :value="1">高优先级</option><option :value="2">普通优先级</option><option :value="3">低优先级</option></select></label></div>
<EventTimeFields :fields="fields" :kind="fields.kind"/>
<label>地点<input v-model="fields.location" maxlength="1000" placeholder="例如：上海市示例路 100 号，或线上"/></label>
<label>会议链接<input v-model="fields.meeting_url" type="url" maxlength="4096" placeholder="https://…"/></label>
<a v-if="task?.event_details?.meeting_url" :href="task.event_details.meeting_url" target="_blank" rel="noopener noreferrer" class="text-button">打开会议链接 ↗</a>
<label>备注<textarea v-model="fields.notes" rows="3" maxlength="4000" placeholder="需要准备的材料、注意事项…"/></label>
<button v-if="task?.status==='pending'" type="button" class="button" :disabled="busy||preparationBusy||linkBusy||stale" @click="start">标记为进行中</button>
<div v-if="task?.next_reminder_at" class="subtle-box">下一次提醒：{{displayTime(task.next_reminder_at)}}</div>
<details v-if="task?.reminders?.length"><summary>提醒计划与历史</summary><div v-for="r in task.reminders" :key="String(r.id)" class="notice"><p>{{r.channel==='webhook'?'手机通知 / webhook':'Windows'}} · {{displayTime(String(r.fire_at))}} · {{statusNames[String(r.state)]??r.state}}</p><button v-if="['pending','retry_wait'].includes(String(r.state))" type="button" class="text-button" :disabled="busy||preparationBusy||linkBusy||stale" @click="snooze(r)">仅延后此提醒 10 分钟</button></div></details>
<PreparationChecklist v-if="task" :task-id="task.id" :parent-version="parentVersion" :disabled="busy||linkBusy||stale" @version="parentVersion=$event" @busy="preparationBusy=$event" @stale="preparationStale"/>
<ApplicationLink v-if="task" :task-id="task.id" :task-version="parentVersion" :disabled="busy||preparationBusy||stale" @version="parentVersion=$event" @busy="linkBusy=$event" @stale="preparationStale"/>
<MailSource v-if="task?.source" :source="task.source"/>
<p v-if="error" class="error-box" role="alert">{{error}}<span v-if="error.includes('版本')||error.includes('其他')"> 请关闭并重新打开事件，核对最新信息。</span></p>
</div><footer class="modal-actions"><button type="button" class="button" @click="emit('cancel')">取消</button><button class="button primary" :disabled="busy||preparationBusy||linkBusy||stale">{{busy?'正在保存…':task?'保存修改':'添加事件'}}</button></footer></form></template>
