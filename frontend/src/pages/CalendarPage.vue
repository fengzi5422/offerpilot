<script setup lang="ts">
import CalendarExport from '../components/CalendarExport.vue';
import {computed,onMounted,ref} from 'vue';
import {request,errorText} from '../api';
import type {Task} from '../types';
import {kindNames,displayTime} from '../domain';
import {dayKey,shiftDate,weekStart,occursOn} from '../calendar';
import Icon from '../components/Icon.vue';
import Modal from '../components/Modal.vue';
import TaskForm from '../components/TaskForm.vue';
interface Calendar {entries:Task[];uncertain_entries:Task[];conflicts:{task_ids:string[];kind:string}[];conflicts_truncated:boolean}
const zone=ref('Asia/Shanghai'),first=ref(''),jump=ref(''),data=ref<Calendar>(),error=ref(''),loading=ref(true),editor=ref(false),selected=ref<Task>();
const today=computed(()=>dayKey(new Date(),zone.value));
const days=computed(()=>first.value?Array.from({length:7},(_,i)=>shiftDate(first.value,i)):[]);
const undated=computed(()=>data.value?.uncertain_entries.filter(t=>!t.starts_at&&!t.due_at)??[]);
const labels=['周一','周二','周三','周四','周五','周六','周日'];
function entries(day:string){return data.value?.entries.filter(t=>occursOn(t,day,zone.value))??[];}
function clock(stamp?:string|null){return stamp?new Intl.DateTimeFormat('zh-CN',{timeZone:zone.value,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(stamp)):'';}
function timeLabel(t:Task,day:string){
 if(!t.starts_at)return clock(t.due_at)+' 截止';
 const begins=dayKey(t.starts_at,zone.value)===day?clock(t.starts_at):'前日开始';
 return begins+(t.ends_at?' — '+(dayKey(t.ends_at,zone.value)===day?clock(t.ends_at):displayTime(t.ends_at)):' · 结束待定');
}
function taskName(id:string){return data.value?.entries.find(t=>t.id===id)?.title??'事件';}
function conflict(id:string){return data.value?.conflicts.some(c=>c.task_ids.includes(id));}
async function load(){loading.value=true;error.value='';try{data.value=await request(`/calendar?start=${first.value}&end=${shiftDate(first.value,7)}&timezone=${encodeURIComponent(zone.value)}`);}catch(e){error.value=errorText(e);}finally{loading.value=false;}}
async function move(offset:number){first.value=shiftDate(first.value,offset);await load();}
async function jumpTo(){if(jump.value){first.value=weekStart(jump.value);await load();}}
async function edit(task?:Task){try{selected.value=task?await request(`/tasks/${task.id}`):undefined;editor.value=true;}catch(e){error.value=errorText(e);}}
async function saved(){editor.value=false;await load();}
onMounted(async()=>{try{zone.value=(await request('/settings')).timezone;first.value=weekStart(today.value);await load();}catch(e){error.value=errorText(e);loading.value=false;}});
</script>
<template><section class="page calendar-page"><div class="page-eyebrow"><span class="tiny-line"/>为重要的见面，留出时间</div><div class="page-heading"><div><h1>事件日历</h1><p>查看一周安排，提前留意时间冲突。</p></div><button class="button primary" @click="edit()"><Icon name="plus" :size="18"/>添加事件</button></div>
<CalendarExport/><div class="section-toolbar calendar-toolbar"><strong v-if="first">{{first}} — {{shiftDate(first,6)}}</strong><div class="button-row"><label class="calendar-jump">跳转日期<input v-model="jump" type="date" :disabled="loading" @change="jumpTo"/></label><button class="button" :disabled="loading" @click="move(-7)">上一周</button><button class="button" :disabled="loading" @click="first=weekStart(today);load()">本周</button><button class="button" :disabled="loading" @click="move(7)">下一周</button><button class="text-button" :disabled="loading" @click="load">刷新</button></div></div>
<p v-if="error" class="error-box" role="alert">{{error}}</p><div v-if="loading" class="loading-block"><span class="spinner"/>正在整理本周事件…</div><template v-else-if="data">
<div v-if="data.conflicts.length" class="calendar-conflicts" role="status"><strong>有需要核对的时间安排</strong><p v-for="(c,i) in data.conflicts" :key="i">{{c.kind==='overlap'?'时间重叠':'可能冲突（结束时间未知）'}}：<button class="text-button" @click="edit(data.entries.find(t=>t.id===c.task_ids[0]))">{{taskName(c.task_ids[0]!)}}</button> / <button class="text-button" @click="edit(data.entries.find(t=>t.id===c.task_ids[1]))">{{taskName(c.task_ids[1]!)}}</button></p><p v-if="data.conflicts_truncated">冲突较多，仅显示前 500 组，请分批核对。</p></div>
<div class="calendar-grid"><section v-for="(day,i) in days" :key="day" class="calendar-day" :class="{today:day===today}"><h3><span>{{labels[i]}}</span><time :datetime="day">{{day.slice(5).replace('-',' / ')}}</time><small v-if="day===today">今天</small></h3><p v-if="!entries(day).length" class="calendar-empty">暂无安排</p><button v-for="t in entries(day)" :key="t.id" class="calendar-event" :class="{conflicting:conflict(t.id)}" @click="edit(t)"><span class="calendar-event-time">{{timeLabel(t,day)}}</span><strong>{{t.title}}</strong><span>{{kindNames[t.kind]}}</span><span v-if="t.event_details?.location" class="calendar-location">{{t.event_details.location}}</span><span v-if="t.event_details?.meeting_url">有会议链接 ↗</span><span v-if="!t.confirmed" class="text-orange">时间待确认</span><span v-if="conflict(t.id)" class="text-orange">请核对冲突</span></button></section></div>
<div class="calendar-uncertain"><div class="field-heading">时间待完善 <span>{{undated.length}} 项</span></div><p class="field-help">只有日期或尚未定时的事件列在这里；补齐具体时间后才安排提醒。结束时间未知的事件显示在对应日期，不假设时长。</p><button v-for="t in undated" :key="t.id" class="calendar-pending" @click="edit(t)"><strong>{{t.title}}</strong><span>{{t.due_date??'日期待定'}}</span><span>{{t.event_details?.location}}</span><Icon name="chevron" :size="16"/></button><p v-if="!undated.length" class="muted">本周没有待完善的日期，未定日期的事件也会列在这里。</p></div></template></section>
<Modal v-if="editor" :title="selected?'编辑事件':'添加事件'" @close="editor=false"><TaskForm :task="selected" @saved="saved" @cancel="editor=false"/></Modal></template>
