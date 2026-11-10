<script setup lang="ts">
import {computed,onMounted,ref} from 'vue';
import {ApiError,errorText,request} from '../api';
import {displayTime} from '../domain';
interface Preparation {id:string;task_id:string;text:string;completed:boolean;version:number;remind_at:string|null;reminder_task_id:string|null;parent_changed:boolean}
interface Checklist {items:Preparation[];parent_version:number;parent_notice:string|null}
const props=defineProps<{taskId:string;parentVersion:number;disabled?:boolean}>();
const emit=defineEmits<{version:[value:number];busy:[value:boolean];stale:[]}>();
const items=ref<Preparation[]>([]),notice=ref<string|null>(null),error=ref(''),loading=ref(true),busy=ref(false),stale=ref(false);
const text=ref(''),remindAt=ref(''),editId=ref<string|null>(null),editText=ref(''),editAt=ref('');
const blocked=computed(()=>loading.value||busy.value||props.disabled||stale.value);
const completed=computed(()=>items.value.filter(item=>item.completed).length);
const base=computed(()=>`/tasks/${props.taskId}/preparations`);
function localInput(value:string|null){if(!value)return '';const date=new Date(value);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}T${String(date.getHours()).padStart(2,'0')}:${String(date.getMinutes()).padStart(2,'0')}`;}
function explicitTime(value:string){if(!value)return null;const date=new Date(value);if(!Number.isFinite(date.getTime())||date.getTime()<=Date.now())throw new Error('请选择未来的准备提醒时刻');return date.toISOString();}
function accept(result:Checklist){items.value=result.items;notice.value=result.parent_notice;emit('version',result.parent_version);}
async function load(){try{const result=await request<Checklist>(base.value);items.value=result.items;notice.value=result.parent_notice;if(result.parent_version!==props.parentVersion){stale.value=true;emit('stale');throw new Error('事件已更新，请关闭并重新打开后核对最新内容。');}}catch(e){error.value=errorText(e);}finally{loading.value=false;}}
async function mutate(path:string,method:string,body:Record<string,unknown>){busy.value=true;emit('busy',true);error.value='';try{const result=await request<Checklist>(path,method,{expected_parent_version:props.parentVersion,...body});accept(result);return true;}catch(e){error.value=errorText(e);if(e instanceof ApiError&&e.status===409){stale.value=true;emit('stale');}return false;}finally{busy.value=false;emit('busy',false);}}
async function add(){try{const value=text.value.trim();if(!value)throw new Error('请填写准备内容');if(await mutate(base.value,'POST',{text:value,remind_at:explicitTime(remindAt.value)})){text.value='';remindAt.value='';}}catch(e){error.value=errorText(e);}}
async function applyTemplate(){await mutate(`${base.value}/templates`,'POST',{});}
async function toggle(item:Preparation){await mutate(`${base.value}/${item.id}`,'PATCH',{expected_version:item.version,changes:{completed:!item.completed}});}
function edit(item:Preparation){editId.value=item.id;editText.value=item.text;editAt.value=localInput(item.remind_at);}
async function saveEdit(item:Preparation){try{if(!editText.value.trim())throw new Error('请填写准备内容');const changes:Record<string,unknown>={text:editText.value.trim()};if(editAt.value!==localInput(item.remind_at))changes.remind_at=explicitTime(editAt.value);if(await mutate(`${base.value}/${item.id}`,'PATCH',{expected_version:item.version,changes}))editId.value=null;}catch(e){error.value=errorText(e);}}
async function remove(item:Preparation){if(!confirm(`删除“${item.text}”？其独立准备提醒也会停止。`))return;await mutate(`${base.value}/${item.id}`,'DELETE',{expected_version:item.version});}
onMounted(load);
</script>
<template>
<section class="preparation-checklist" aria-labelledby="preparation-heading">
 <div class="preparation-heading"><div><p class="eyebrow">BEFORE THE DAY</p><h3 id="preparation-heading">准备事项 <span>{{completed}} / {{items.length}}</span></h3></div><button type="button" class="text-button" :disabled="blocked" @click="applyTemplate">添加准备模板</button></div>
 <p class="preparation-hint">清单修改即时保存。只有选择提醒时刻后，才会添加一条独立准备提醒。</p>
 <p v-if="notice" class="notice" role="status">{{notice}}</p>
 <p v-if="loading" class="muted" role="status">正在读取准备清单…</p>
 <ul v-else class="preparation-items">
  <li v-for="item in items" :key="item.id" :class="{completed:item.completed}">
   <template v-if="editId!==item.id"><label class="check-item"><input type="checkbox" :checked="item.completed" :disabled="blocked" @change="toggle(item)"/><span>{{item.text}}</span></label><p v-if="item.remind_at" class="preparation-reminder">准备提醒：{{displayTime(item.remind_at)}}</p><div class="item-actions"><button type="button" class="text-button" :disabled="blocked" @click="edit(item)">编辑</button><button type="button" class="text-button" :disabled="blocked" @click="remove(item)">删除</button></div></template>
   <div v-else class="preparation-editor"><label>准备内容<input v-model="editText" maxlength="200" :disabled="blocked" @keydown.enter.prevent="saveEdit(item)"/></label><label>准备提醒时刻（可选，当前设备时区）<input v-model="editAt" type="datetime-local" :disabled="blocked"/></label><div class="item-actions"><button type="button" class="button" :disabled="blocked" @click="editId=null">取消编辑</button><button type="button" class="button primary" :disabled="blocked" @click="saveEdit(item)">保存准备事项</button></div></div>
  </li>
 </ul>
 <p v-if="!loading&&!items.length" class="preparation-empty">还没有准备事项。可以添加模板，也可以写下自己的计划。</p>
 <div class="preparation-editor"><label>添加准备事项<input v-model="text" maxlength="200" placeholder="例如：准备项目介绍、测试摄像头" :disabled="blocked" @keydown.enter.prevent="add"/></label><label>准备提醒时刻（可选，当前设备时区）<input v-model="remindAt" type="datetime-local" :disabled="blocked"/></label><button type="button" class="button" :disabled="blocked||!text.trim()" @click="add">添加准备事项</button></div>
 <p v-if="error" class="error-box" role="alert">{{error}}</p>
</section>
</template>
<style scoped>
.preparation-checklist{border-top:1px solid var(--line,#e5e7eb);padding-top:24px;margin-top:8px}.preparation-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.preparation-heading h3{margin:2px 0;font-size:18px}.preparation-heading h3 span{font-size:12px;font-weight:500;color:var(--muted,#687474);margin-left:8px}.eyebrow{font-size:10px;letter-spacing:.14em;color:var(--muted,#687474);margin:0}.preparation-hint,.preparation-empty{font-size:13px;line-height:1.7;color:var(--muted,#687474)}.preparation-items{list-style:none;padding:0;margin:18px 0}.preparation-items li{border-bottom:1px solid var(--line,#e5e7eb);padding:12px 0}.check-item{display:flex;flex-direction:row;align-items:flex-start;gap:10px;line-height:1.6}.check-item input{width:17px;height:17px;min-height:17px;padding:0;accent-color:var(--green);margin-top:4px;flex-shrink:0}.completed .check-item span{text-decoration:line-through;color:var(--muted,#687474)}.preparation-reminder{font-size:12px;color:var(--muted,#687474);margin:6px 0 0 27px}.item-actions{display:flex;gap:14px;justify-content:flex-end;margin-top:8px}.preparation-editor{display:grid;gap:12px;padding:14px;background:var(--surface-muted,#f6f8f7);border-radius:12px}.preparation-editor .button{justify-self:end}.preparation-editor label{margin:0}
</style>
