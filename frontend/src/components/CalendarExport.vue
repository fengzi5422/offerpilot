<script setup lang="ts">
import {ref} from 'vue';
const details=ref(false),busy=ref(false),message=ref(''),error=ref('');
async function download(){
 busy.value=true;message.value='';error.value='';
 try{
  const response=await fetch(`/api/v1/calendar/export?include_details=${details.value}`,{credentials:'same-origin'});
  if(!response.ok)throw new Error('日历导出未完成，请重新打开应用后再试。');
  const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url;link.download='OfferPilot.ics';link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  const omitted=Number(response.headers.get('X-OfferPilot-Omitted-Count')??0);
  message.value=`日历文件已就绪${omitted?`，跳过 ${omitted} 项时间未知或待确认的事件`:''}。`;
 }catch(e){error.value=e instanceof Error?e.message:'日历导出失败，请稍后重试。';}finally{busy.value=false;}
}
</script>
<template><details class="advanced"><summary>导出到其他日历（ICS）</summary><p class="field-help">这是当前安排的快照，不会自动同步。默认只导出标题和时间；反复导入可能产生重复事件，取决于日历软件。</p><label class="setting-toggle"><span>包含地点、会议链接与备注（可能含个人信息）</span><input v-model="details" type="checkbox" :disabled="busy"/></label><button class="button" :disabled="busy" @click="download">{{busy?'正在导出…':'下载 ICS 日历'}}</button><p v-if="message" class="success-box" role="status">{{message}}</p><p v-if="error" class="error-box" role="alert">{{error}}</p></details></template>
