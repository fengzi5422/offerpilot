<script setup lang="ts">
import {computed} from 'vue';
import type {Temporal} from '../types';
import {proposalForm,type TimeForm} from '../domain';
const props=defineProps<{fields:TimeForm;kind:string;temporal?:Temporal}>();
const scheduled=computed(()=>['interview','written_exam'].includes(props.kind));
const zones=computed(()=>[...new Set([props.fields.timezone,'Asia/Shanghai','Asia/Tokyo','UTC','Europe/London','America/New_York'])]);
const names:Record<string,string>={message_date:'邮件发送时间',internal_date:'邮箱收件时间'};
function choose(c:Record<string,unknown>){Object.assign(props.fields,proposalForm(props.temporal!,c),{reference_kind:c.reference_kind,reference_at:c.reference_at});}
function label(c:Record<string,unknown>){const p=proposalForm(props.temporal!,c);return `${p.date} ${p.time} · ${names[String(c.reference_kind)]??'邮件原文'}${c.reason?' · '+c.reason:''}`;}
</script>
<template><div class="field-section"><div class="field-heading">{{scheduled?'事件时间':'截止时间'}} <span>可以稍后确定</span></div><div v-if="temporal?.candidates?.length" class="time-proposals"><p class="field-help">识别出的时间建议，点击填入后再确认。</p><button v-for="(c,i) in temporal.candidates" :key="i" type="button" class="proposal-button" @click="choose(c)">{{label(c)}}</button></div><div class="form-row"><label>日期<input v-model="fields.date" type="date"/></label><label>{{scheduled?'开始时间':'具体时间'}}<input v-model="fields.time" type="time"/></label></div><div v-if="scheduled" class="form-row"><label>结束日期（可选）<input v-model="fields.endDate" type="date"/></label><label>结束时间（可选）<input v-model="fields.endTime" type="time"/></label></div><p class="field-help">{{fields.date&&!fields.time?'只保存日期，补充具体时间后才安排提醒。':scheduled?'结束时间未知时可以留空，不会假设事件时长。':'按确认的截止时间安排提醒。'}}</p><details class="advanced"><summary>高级时间设置</summary><label>时间所在地区<select v-model="fields.timezone"><option v-for="zone in zones" :key="zone" :value="zone">{{zone==='Asia/Shanghai'?'北京时间':zone==='Asia/Tokyo'?'东京时间':zone==='UTC'?'UTC':zone}}</option></select></label></details></div></template>
