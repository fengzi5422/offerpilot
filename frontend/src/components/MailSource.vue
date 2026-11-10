<script setup lang="ts">
import {computed,onMounted,ref} from 'vue';
import {request} from '../api';
import {displayTime} from '../domain';
import type {Mail} from '../types';

const props=defineProps<{source:Record<string,unknown>}>();
const mail=ref<Mail>();
const unavailable=ref(Boolean(props.source.source_unavailable));
const quotes=computed(()=>Array.isArray(props.source.evidence)?props.source.evidence
 .filter((e):e is {quote:string}=>Boolean(e&&typeof e==='object'&&typeof e.quote==='string'))
 .map(e=>e.quote):[]);
const links=computed(()=>(mail.value?.links??[]).flatMap(link=>{
 try{const url=new URL(link.url);return ['http:','https:'].includes(url.protocol)
  ?[{url:url.href,domain:url.hostname,text:link.text||url.hostname}]:[];}catch{return [];}
}));
onMounted(async()=>{
 if(typeof props.source.message_id!=='string'||unavailable.value)return;
 try{mail.value=await request(`/emails/${props.source.message_id}`);}catch{unavailable.value=true;}
});
</script>

<template>
 <section class="source-box">
  <h4>邮件来源</h4>
  <p><strong>{{mail?.subject??source.subject??'原始邮件'}}</strong></p>
  <dl v-if="mail" class="details">
   <dt>发件人</dt><dd>{{mail.from_addr}}</dd>
   <dt>邮件标注时间</dt><dd>{{displayTime(mail.header_date)}}</dd>
   <dt v-if="mail.internal_date">邮箱收件时间</dt><dd v-if="mail.internal_date">{{displayTime(mail.internal_date)}}</dd>
  </dl>
  <p v-for="(quote,index) in quotes" :key="index" class="quote">{{quote}}</p>
  <p v-if="unavailable" class="field-help">原邮件缓存已清理或不可用，已保存的待办仍可使用。</p>
  <details v-if="mail?.text_excerpt"><summary>查看保存的邮件文本</summary><pre>{{mail.text_excerpt}}</pre></details>
  <div v-if="links.length">
   <p class="field-help">以下链接来自邮件，请核对真实域名后主动打开。</p>
   <p v-for="link in links" :key="link.url"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{link.text}} · {{link.domain}}</a></p>
  </div>
 </section>
</template>
