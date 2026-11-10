<script setup lang="ts">
import {computed,onMounted,reactive,ref} from 'vue';
import {ApiError,errorText,request,requestPage} from '../api';
import {displayTime,kindNames,statusNames} from '../domain';
import type {Task} from '../types';
import Icon from '../components/Icon.vue';
import Modal from '../components/Modal.vue';
import MailSource from '../components/MailSource.vue';

interface BoardTask extends Task {application_id:string|null}
interface HistoryEntry {id:string;action:string;at:string;actor:string;old_stage?:string|null;new_stage?:string;task_id?:string;old_application_id?:string|null;new_application_id?:string|null}
interface Application {id:string;version:number;company:string;position:string|null;stage:string;source:string;tasks:BoardTask[];history:HistoryEntry[]}
const stages=[['applied','已投递'],['assessment','测评'],['written_exam','笔试'],['interview','面试'],['awaiting_result','等结果'],['offer','录用通知'],['closed','已关闭']];
const names=Object.fromEntries(stages);
const applications=ref<Application[]>([]),tasks=ref<BoardTask[]>([]),loading=ref(false),busy=ref(false),error=ref(''),modalError=ref(''),notice=ref('');
const opened=ref(false),selectedId=ref(''),chosenTask=ref(''),taskCursor=ref<string|null>(),search=ref('');
const draft=reactive({company:'',position:'',stage:'applied'}),moveTargets=reactive<Record<string,string>>({});
const selected=computed(()=>applications.value.find(application=>application.id===selectedId.value));
const candidates=computed(()=>tasks.value.filter(task=>task.application_id!==selectedId.value));
const chosen=computed(()=>tasks.value.find(task=>task.id===chosenTask.value));
const active=computed(()=>applications.value.filter(application=>application.stage!=='closed').length);
const groups=computed(()=>stages.map(([stage,label])=>({stage,label,items:applications.value.filter(application=>application.stage===stage&&`${application.company} ${application.position??''}`.toLowerCase().includes(search.value.trim().toLowerCase()))})));
const history=computed(()=>(selected.value?.history??[]).filter(entry=>entry.action!=='application.changed').slice().reverse());
function applicationLabel(id:string|null|undefined){const application=applications.value.find(item=>item.id===id);return application?`${application.company} · ${application.position||'岗位未填写'}`:id?'原申请':'未关联申请';}
function taskLabel(id:string|undefined){return tasks.value.find(task=>task.id===id)?.title??'任务';}
function sourceLabel(source:string){return source==='manual'?'手动创建':'邮件识别';}
function historyLabel(entry:HistoryEntry){
 if(entry.action==='application.created')return `创建申请 · ${names[entry.new_stage??'applied']??'已投递'}`;
 if(entry.action==='application.stage_changed')return `${names[entry.old_stage??'']??'原阶段'} → ${names[entry.new_stage??'']??'新阶段'}`;
 if(entry.action==='application.task_linked')return `关联「${taskLabel(entry.task_id)}」${entry.old_application_id?'，来自 '+applicationLabel(entry.old_application_id):''}`;
 if(entry.action==='application.task_unlinked')return `解除「${taskLabel(entry.task_id)}」${entry.new_application_id?'，移至 '+applicationLabel(entry.new_application_id):''}`;
 return '申请信息已更新';
}
async function allApplications(){
 const values:Application[]=[];let cursor:string|null|undefined;
 do{const page=await requestPage<Application>('/applications?limit=200'+(cursor?'&cursor='+encodeURIComponent(cursor):''));values.push(...page.data);cursor=page.next_cursor;}while(cursor);
 return values;
}
async function load(){
 loading.value=true;error.value='';
 try{const [list,page]=await Promise.all([allApplications(),requestPage<BoardTask>('/tasks?limit=200')]);applications.value=list;tasks.value=page.data;taskCursor.value=page.next_cursor;}
 catch(e){error.value=errorText(e);if(opened.value)modalError.value=error.value;}finally{loading.value=false;}
}
async function moreTasks(){if(!taskCursor.value)return;busy.value=true;try{const page=await requestPage<BoardTask>('/tasks?limit=200&cursor='+encodeURIComponent(taskCursor.value));tasks.value=[...tasks.value,...page.data];taskCursor.value=page.next_cursor;}catch(e){modalError.value=errorText(e);}finally{busy.value=false;}}
function open(application?:Application){selectedId.value=application?.id??'';Object.assign(draft,{company:application?.company??'',position:application?.position??'',stage:application?.stage??'applied'});chosenTask.value='';modalError.value='';notice.value='';opened.value=true;}
function failed(e:unknown){modalError.value=errorText(e)+(e instanceof ApiError&&e.code==='VERSION_CONFLICT'?' 请刷新申请后再操作。':'');}
async function refreshDetails(){await load();if(selected.value)Object.assign(draft,{company:selected.value.company,position:selected.value.position??'',stage:selected.value.stage});chosenTask.value='';modalError.value='';}
async function save(){
 busy.value=true;modalError.value='';notice.value='';
 try{
  const value={company:draft.company.trim(),position:draft.position.trim()||null,stage:draft.stage};
  if(!value.company)throw new Error('请填写公司名称');
  if(selected.value){const application=selected.value;const changes=Object.fromEntries(Object.entries(value).filter(([key,value])=>value!==application[key as 'company'|'position'|'stage']));if(!Object.keys(changes).length){notice.value='申请信息没有变化';return;}await request(`/applications/${application.id}`,'PATCH',{expected_version:application.version,changes});}
  else{const created=await request<Application>('/applications','POST',value);selectedId.value=created.id;}
  await load();notice.value='申请已保存';
 }catch(e){failed(e);}finally{busy.value=false;}
}
async function associate(task:BoardTask,destination:Application){
 if(!selected.value)return;
 busy.value=true;modalError.value='';notice.value='';
 try{const source=task.application_id?applications.value.find(application=>application.id===task.application_id):undefined;
  await request(`/applications/${destination.id}/link`,'POST',{expected_version:destination.version,task_id:task.id,expected_task_version:task.version,...(source?{expected_source_version:source.version}:{})});
  chosenTask.value='';await load();notice.value='任务关联已更新，原有提醒时间已保留';
 }catch(e){failed(e);}finally{busy.value=false;}
}
async function linkChosen(){if(chosen.value&&selected.value)await associate(chosen.value,selected.value);}
async function move(task:BoardTask){const destination=applications.value.find(application=>application.id===moveTargets[task.id]);if(destination)await associate(task,destination);}
async function unlink(task:BoardTask){
 if(!selected.value)return;busy.value=true;modalError.value='';notice.value='';
 try{await request(`/applications/${selected.value.id}/unlink`,'POST',{expected_version:selected.value.version,task_id:task.id,expected_task_version:task.version});await load();notice.value='已解除关联，任务与原有提醒继续保留';}
 catch(e){failed(e);}finally{busy.value=false;}
}
onMounted(load);
</script>

<template>
 <section class="page applications-page">
  <div class="page-eyebrow"><span class="tiny-line"/>每一份申请，都有自己的下一步</div>
  <div class="page-heading"><div><h1>看见求职的进展</h1><p>按公司与岗位整理申请，阶段由你亲自决定。</p></div><button class="button primary" @click="open()"><Icon name="plus" :size="18"/>新建申请</button></div>
  <div class="application-summary" aria-label="申请概览"><div><strong>{{active}}</strong><span>进行中的申请</span></div><div><strong>{{applications.filter(item=>item.stage==='offer').length}}</strong><span>份录用通知</span></div><p>同一家公司，不同岗位各自记录。<br>任务关联可以调整，提醒时间继续保留。</p></div>
  <div class="section-toolbar board-toolbar"><label class="board-search">查找申请<input v-model="search" type="search" placeholder="公司或岗位名称"/></label><button class="text-button" :disabled="loading" @click="load">刷新申请</button></div>
  <p v-if="error" class="error-box" role="alert">{{error}}</p>
  <div v-if="loading&&!applications.length" class="loading-block" role="status"><span class="spinner"/>正在整理申请…</div>
  <div v-else class="application-board" :aria-busy="loading">
   <section v-for="group in groups" :key="group.stage" class="application-column" :aria-label="group.label">
    <h2><span class="stage-mark" :class="group.stage"/>{{group.label}}<span class="stage-count">{{group.items.length}}</span></h2>
    <button v-for="application in group.items" :key="application.id" class="application-card" @click="open(application)">
     <span class="application-company">{{application.company}}<Icon name="chevron" :size="15"/></span><strong>{{application.position||'岗位未填写'}}</strong><span class="application-meta"><span>{{application.tasks?.length??0}} 项关联任务</span><span>{{sourceLabel(application.source)}}</span></span>
    </button>
    <p v-if="!group.items.length" class="column-empty">{{search?'没有匹配的申请':'还没有这一阶段的申请'}}</p>
   </section>
  </div>
 </section>
 <Modal v-if="opened" :title="selected?'申请详情':'新建申请'" wide @close="opened=false">
  <form @submit.prevent="save"><div class="form-body">
   <label>公司名称<input v-model="draft.company" required maxlength="200" :disabled="busy" placeholder="例如：示例科技"/></label>
   <label>申请岗位<input v-model="draft.position" maxlength="200" :disabled="busy" placeholder="例如：后端开发工程师"/></label>
   <label>当前阶段<select v-model="draft.stage" :disabled="busy"><option v-for="[stage,label] in stages" :key="stage" :value="stage">{{label}}</option></select></label>
   <p class="field-help">阶段可向前或向后调整，关闭后也可重新打开。关闭申请后，已有任务和提醒继续保留。</p>
   <div class="button-row"><button class="button primary" :disabled="busy">{{busy?'正在保存…':selected?'保存申请':'创建申请'}}</button><button v-if="selected" type="button" class="text-button" :disabled="busy||loading" @click="refreshDetails">刷新申请</button></div>
   <p v-if="modalError" class="error-box" role="alert">{{modalError}}</p><p v-if="notice" class="success-box" role="status">{{notice}}</p>
  </div></form>
  <template v-if="selected">
   <section class="board-detail-section"><h3>关联任务 <span>{{selected.tasks.length}}</span></h3><p class="field-help">只调整归属；任务的状态、时间和提醒不会自动改变。</p>
    <div v-for="task in selected.tasks" :key="task.id" class="linked-task">
     <strong>{{task.title}}</strong><div class="task-meta"><span class="tag">{{kindNames[task.kind]??'其他事项'}}</span><span>{{statusNames[task.status]??'待处理'}}</span><span>{{task.due_date??displayTime(task.starts_at??task.due_at)}}</span><span>{{task.source?.preparation_id?'准备事项':task.source?'来自招聘邮件':'手动添加'}}</span></div>
     <details v-if="task.source" class="task-source"><summary>查看任务来源</summary><MailSource :source="task.source"/></details>
     <div class="linked-actions"><label :for="'move-'+task.id">移动到其他申请<select :id="'move-'+task.id" v-model="moveTargets[task.id]" :disabled="busy"><option value="">请选择目标申请</option><option v-for="other in applications.filter(item=>item.id!==selectedId)" :key="other.id" :value="other.id">{{other.company}} · {{other.position||'岗位未填写'}}</option></select></label><button class="button" :disabled="busy||!moveTargets[task.id]" @click="move(task)">移动任务</button><button class="text-button" :disabled="busy" @click="unlink(task)">解除关联</button></div>
    </div>
    <p v-if="!selected.tasks.length" class="muted">尚未关联任务，可从已有待办中选择。</p>
    <div class="association-picker"><label for="association-task">从已有任务关联<select id="association-task" v-model="chosenTask" :disabled="busy"><option value="">选择未关联或其他申请的任务</option><option v-for="task in candidates" :key="task.id" :value="task.id">{{task.title}} · {{applicationLabel(task.application_id)}}</option></select></label><p v-if="chosen?.application_id" class="field-help">将从「{{applicationLabel(chosen.application_id)}}」移动到当前申请。</p><div class="button-row wrap"><button class="button" :disabled="busy||!chosenTask" @click="linkChosen">{{chosen?.application_id?'移动并关联任务':'关联所选任务'}}</button><button v-if="taskCursor" class="text-button" :disabled="busy" @click="moreTasks">加载更多可选任务</button></div></div>
   </section>
   <section class="board-detail-section"><h3>阶段与关联历史</h3><ol class="application-history"><li v-for="entry in history" :key="entry.id"><strong>{{historyLabel(entry)}}</strong><span>{{entry.actor==='user'?'手动操作':'系统记录'}} · {{displayTime(entry.at)}}</span></li></ol><p v-if="!history.length" class="muted">暂无历史记录。</p></section>
  </template>
  <footer class="modal-actions"><button class="button" @click="opened=false">关闭</button></footer>
 </Modal>
</template>

<style scoped>
.applications-page{max-width:1500px}.application-summary{display:flex;align-items:center;gap:34px;background:#f1f3e9;border:1px solid #e3e8da;border-radius:9px;padding:25px 28px;margin-bottom:25px}.application-summary>div{display:flex;align-items:baseline;gap:12px}.application-summary strong{font:36px Georgia,serif;color:#3f5d3d}.application-summary span{font-size:11px;color:#7f8c71}.application-summary p{margin-left:auto;font-size:11px;color:#8e9c82;line-height:1.9}.board-toolbar{gap:20px;padding-bottom:16px}.board-search{display:flex;flex-direction:row;gap:12px;align-items:center;font-size:11px;color:#7a886e}.board-search input{width:230px;min-height:36px}.application-board{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}.application-column{min-width:0;border:1px solid #e3e8db;border-radius:8px;background:#f2f4ec;padding:15px 13px;min-height:180px}.application-column h2{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:500;margin:2px 2px 17px}.stage-mark{height:7px;width:7px;border-radius:50%;background:#9ba993}.stage-mark.interview{background:#608c74}.stage-mark.offer{background:#b89557}.stage-mark.closed{background:#b3b8ab}.stage-count{margin-left:auto;color:#9ba58f;font-size:10px}.application-card{display:grid;width:100%;gap:12px;text-align:left;padding:16px 14px;margin-top:10px;border:1px solid #e0e7d8;border-radius:6px;background:white;transition:border-color .15s,box-shadow .15s}.application-card:hover{border-color:#acbea1;box-shadow:0 2px 7px #334b3110}.application-company{display:flex;justify-content:space-between;gap:10px;font-size:13px;overflow-wrap:anywhere;line-height:1.6}.application-company svg{flex-shrink:0;margin-top:3px;color:#a1ae95}.application-card>strong{font-size:12px;font-weight:400;color:#7d8d71;overflow-wrap:anywhere}.application-meta{display:flex;justify-content:space-between;gap:8px;font-size:10px;color:#9ba58f;flex-wrap:wrap}.column-empty{font-size:11px;color:#a0aa94;padding:22px 2px;line-height:1.7}.board-detail-section{padding:24px 26px;border-top:1px solid var(--line);overflow-wrap:anywhere}.board-detail-section h3{font-size:14px;font-weight:500;margin-bottom:13px}.board-detail-section h3>span{color:#96a189;font-size:11px;margin-left:5px}.linked-task{padding:16px 0;border-bottom:1px solid var(--line)}.linked-task>strong{font-size:13px;font-weight:500;line-height:1.8}.linked-actions{display:flex;align-items:end;gap:12px;flex-wrap:wrap;margin-top:16px}.linked-actions label{flex:1;min-width:160px}.linked-actions button{flex-shrink:0}.association-picker{padding-top:22px}.task-source{margin-top:16px}.task-source summary{font-size:11px;cursor:pointer;color:#718364;margin-bottom:12px}.application-history{list-style:none;padding:0 0 0 10px;margin:20px 0 0;border-left:1px solid #dce4d2}.application-history li{position:relative;display:grid;gap:7px;padding:0 0 22px 15px}.application-history li:before{content:'';position:absolute;left:-14px;top:5px;width:7px;height:7px;border-radius:50%;background:#9aaf88;border:2px solid #fafbf6}.application-history li strong{font-size:12px;font-weight:400;line-height:1.8}.application-history li span{font-size:10px;color:#9aa58e}.application-history li:last-child{padding-bottom:0}
@media(min-width:1500px){.application-board{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:1100px){.application-board{grid-template-columns:repeat(2,minmax(0,1fr))}.application-summary{flex-wrap:wrap;gap:20px}.application-summary p{margin-left:0;flex-basis:100%}}@media(max-width:600px){.application-board{grid-template-columns:1fr}.application-summary{padding:20px}.board-toolbar{align-items:flex-end}.board-search{flex-direction:column;align-items:flex-start;gap:7px;min-width:0}.board-search input{width:100%;min-width:0}.board-detail-section{padding:20px 18px}.linked-actions label{flex-basis:100%}.application-column{min-height:130px}.application-summary>div{gap:8px}}
</style>
