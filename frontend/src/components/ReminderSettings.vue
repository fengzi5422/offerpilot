<script setup lang="ts">
import {ref,watch} from 'vue';
import {kindNames} from '../domain';
import {reminderDraft,reminderChanges} from '../settings';
const props=defineProps<{settings:Record<string,any>;busy:boolean}>();
const emit=defineEmits<{save:[changes:Record<string,unknown>]}>();
const draft=ref(reminderDraft(props.settings)),error=ref('');
watch(()=>props.settings,value=>{draft.value=reminderDraft(value);error.value='';});
function save(){try{error.value='';emit('save',reminderChanges(draft.value));}catch(e){error.value=(e as Error).message;}}
</script>
<template><form @submit.prevent="save"><div class="setting-divider"/><h3>按事项类型提前提醒</h3><p class="field-help">填写提前的分钟数，多个时间用逗号分隔，例如 1440, 60。最多 10 个，范围 1～10080 分钟；留空关闭该类型。只有日期的事项仍需先补充具体时刻。</p><label v-for="(name,kind) in kindNames" :key="kind">{{name}}<input v-model="draft.offsets[kind]" :disabled="busy" inputmode="text" placeholder="如 1440, 60"/></label><label class="setting-toggle"><span><strong>安静时段</strong><small>使用当前显示时区；通知顺延至结束，超过任务时刻则记录为可能错过</small></span><input v-model="draft.quiet.enabled" type="checkbox" :disabled="busy"/></label><div class="button-row"><label>开始时间<input v-model="draft.quiet.start" type="time" required :disabled="busy"/></label><label>结束时间<input v-model="draft.quiet.end" type="time" required :disabled="busy"/></label></div><label>系统通知中的内容<select v-model="draft.privacy" :disabled="busy"><option value="generic">隐藏任务名称（推荐）</option><option value="details">显示任务名称</option></select></label><p class="field-help">保存后，会重新安排所有未完成事项的提醒，包含已延后的计划。应用内提醒中心始终显示详情。</p><p v-if="error" class="error-box" role="alert">{{error}}</p><button class="button primary" :disabled="busy">保存提醒设置</button></form></template>
