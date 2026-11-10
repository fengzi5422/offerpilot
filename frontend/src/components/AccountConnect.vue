<script setup lang="ts">
import {computed,reactive,ref} from 'vue';
import {request,waitJob,errorText} from '../api';
import {saveAndTestMailbox} from '../settings';
import type {Account} from '../types';

const emit=defineEmits<{connected:[tested:boolean]}>();
const provider=ref<Account['provider']>('qq');
const address=ref(''),code=ref(''),range=ref('14'),busy=ref(false),error=ref('');
const saved=reactive({accountId:''});
const providers={
 qq:{name:'QQ / Foxmail',placeholder:'你的邮箱@qq.com 或 @foxmail.com',domains:['qq.com','foxmail.com'],help:'在 QQ 邮箱网页版「设置 → 账户」中开启 IMAP 服务并获取授权码。Foxmail 地址也使用 QQ 邮箱服务。'},
 '163':{name:'网易 163',placeholder:'你的邮箱@163.com',domains:['163.com'],help:'在 163 邮箱网页版「设置 → POP3/SMTP/IMAP」中开启 IMAP/SMTP 服务，并设置或获取客户端授权码；如提示安全验证，请按网易页面完成。'},
 '126':{name:'网易 126',placeholder:'你的邮箱@126.com',domains:['126.com'],help:'在 126 邮箱网页版「设置 → POP3/SMTP/IMAP」中开启 IMAP/SMTP 服务，并设置或获取客户端授权码；如提示安全验证，请按网易页面完成。'},
};
const selected=computed(()=>providers[provider.value]);
async function save(){
 busy.value=true;error.value='';let tested=false;
 try{
  const domain=address.value.trim().toLowerCase().split('@')[1];
  if(!selected.value.domains.includes(domain??''))throw new Error(`请输入与 ${selected.value.name} 服务对应的邮箱地址。`);
  await saveAndTestMailbox(saved,{provider:provider.value,address:address.value,code:code.value,initialScan:range.value},request,waitJob);
  tested=true;
 }catch(e){error.value=errorText(e);}finally{busy.value=false;code.value='';if(saved.accountId)emit('connected',tested);}
}
</script>
<template>
 <form @submit.prevent="save" class="account-form">
  <label>邮箱服务<select v-model="provider" :disabled="busy||!!saved.accountId"><option value="qq">QQ / Foxmail</option><option value="163">网易 163</option><option value="126">网易 126</option></select></label>
  <label>邮箱地址<input v-model="address" type="email" :placeholder="selected.placeholder" :disabled="busy||!!saved.accountId" required autocomplete="username"/></label>
  <label>邮箱授权码<input v-model="code" type="password" placeholder="请输入 IMAP 客户端授权码" :disabled="busy" required autocomplete="new-password"/></label>
  <p class="field-help">{{selected.help}} 请使用授权码，不要填写邮箱登录密码。授权码安全保存在本机 Windows 凭据管理器中。</p>
  <label>首次查看的邮件范围<select v-model="range" :disabled="busy||!!saved.accountId"><option value="14">最近 14 天</option><option value="30">最近 30 天</option><option value="now">从现在开始</option></select></label>
  <p class="field-help">只读查看收件箱中的邮件正文，不会更改已读状态、发送或删除邮件。每个邮箱可以单独暂停和移除。</p>
  <p v-if="error" class="error-box" role="alert">{{error}}</p>
  <button class="button primary" :disabled="busy">{{busy?'正在测试连接…':saved.accountId?'更新授权码并重新测试':'安全保存并测试连接'}}</button>
 </form>
</template>
