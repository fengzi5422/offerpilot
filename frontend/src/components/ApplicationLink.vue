<script setup lang="ts">
import {onMounted,ref} from 'vue';
import {ApiError,request,requestPage,errorText} from '../api';
interface Application {id:string;company:string;position:string|null;version:number}
const props=defineProps<{taskId:string;taskVersion:number;disabled:boolean}>();
const emit=defineEmits<{version:[value:number];busy:[value:boolean];stale:[]}>();
const items=ref<Application[]>([]),current=ref(''),chosen=ref(''),busy=ref(true),error=ref(''),message=ref('');
async function load(){
 try{let cursor:string|null|undefined;const values:Application[]=[];do{const page=await requestPage<Application>('/applications?limit=200'+(cursor?'&cursor='+encodeURIComponent(cursor):''));values.push(...page.data);cursor=page.next_cursor;}while(cursor);
  const task=await request(`/tasks/${props.taskId}`);if(task.version!==props.taskVersion){emit('stale');throw new Error('事件已更新，请关闭并重新打开后核对。');}
  items.value=values;current.value=chosen.value=task.application_id??'';
 }catch(e){error.value=errorText(e);}finally{busy.value=false;}
}
async function save(){
 if(current.value===chosen.value)return;
 busy.value=true;emit('busy',true);error.value='';message.value='';
 try{const source=items.value.find(x=>x.id===current.value),target=items.value.find(x=>x.id===chosen.value);
  if(chosen.value){if(!target)throw new Error('请重新打开事件，刷新申请列表。');await request(`/applications/${target.id}/link`,'POST',{expected_version:target.version,task_id:props.taskId,expected_task_version:props.taskVersion,...(source?{expected_source_version:source.version}:{})});}
  else{if(!source)throw new Error('原申请不可用，请重新打开事件。');await request(`/applications/${source.id}/unlink`,'POST',{expected_version:source.version,task_id:props.taskId,expected_task_version:props.taskVersion});}
  emit('version',props.taskVersion+1);current.value=chosen.value;message.value='申请关联已保存，事件与提醒保持原计划。';
  for(const item of items.value)if(item.id===source?.id||item.id===target?.id)item.version++;
 }catch(e){error.value=errorText(e);if(e instanceof ApiError&&e.status===409)emit('stale');}finally{busy.value=false;emit('busy',false);}
}
onMounted(load);
</script>
<template><details class="advanced"><summary>关联求职申请</summary><p class="field-help">先在申请看板创建公司与岗位，再选择关联。此处修改即时保存，解除关联会保留事件与提醒。</p><label>所属申请<select v-model="chosen" :disabled="disabled||busy"><option value="">暂不关联申请</option><option v-for="a in items" :key="a.id" :value="a.id">{{a.company}} · {{a.position||'岗位未填写'}}</option></select></label><button type="button" class="button" :disabled="disabled||busy||current===chosen" @click="save">保存申请关联</button><p v-if="message" class="success-box" role="status">{{message}}</p><p v-if="error" class="error-box" role="alert">{{error}}</p></details></template>
