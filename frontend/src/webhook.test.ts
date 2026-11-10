import {expect,it} from 'vitest';
import {webhookDraft,buildWebhookPatch,webhookErrorMessage,webhookResultMessage} from './webhook';
import type {WebhookSettings} from './webhook';
const saved=():WebhookSettings=>({enabled:false,endpoint_origin:'https://notify.example',has_secret:true,version:4});
it('keeps full URL and token blank when reading an existing configuration',()=>{
 expect(webhookDraft(saved())).toMatchObject({url:'',token:'',consent:false});
 expect(JSON.stringify(webhookDraft(saved()))).not.toContain('notify.example');
});
it('disables without sending unsaved destination or token and without requiring consent',()=>{
 expect(buildWebhookPatch(saved(),{...webhookDraft(saved()),enabled:true,url:'invalid',token:'secret'},'disable'))
  .toEqual({expected_version:4,changes:{enabled:false}});
});
it('requires an explicit new consent for every enabled save',()=>{
 expect(()=>buildWebhookPatch(saved(),{...webhookDraft(saved()),enabled:true})).toThrow('同意');
 expect(buildWebhookPatch(saved(),{...webhookDraft(saved()),enabled:true,consent:true}).changes).toEqual({enabled:true,consent:true});
});
it('requires a destination for first enable, but not a bearer token',()=>{
 const initial={...saved(),has_secret:false,endpoint_origin:''};
 expect(()=>buildWebhookPatch(initial,{...webhookDraft(initial),enabled:true,consent:true})).toThrow('地址');
 expect(buildWebhookPatch(initial,{...webhookDraft(initial),enabled:true,consent:true,url:'https://notify.example/hook?token=private'}).changes)
  .toEqual({enabled:true,url:'https://notify.example/hook?token=private',token:'',consent:true});
});
it('never carries the old bearer credential to a newly entered destination',()=>{
 const patch=buildWebhookPatch(saved(),{...webhookDraft(saved()),enabled:true,consent:true,url:' https://other.example/new '});
 expect(patch.changes.url).toBe('https://other.example/new');expect(patch.changes.token).toBe('');
});
it('replaces a token only when provided, or explicitly clears it',()=>{
 const draft={...webhookDraft(saved()),enabled:true,consent:true};
 expect(buildWebhookPatch(saved(),{...draft,token:' new-token '}).changes).toMatchObject({token:'new-token'});
 expect(buildWebhookPatch(saved(),{...draft,clearToken:true}).changes).toMatchObject({token:''});
});
it('keeps an incomplete initial configuration disabled without storing empty secrets',()=>{
 const initial={...saved(),has_secret:false,endpoint_origin:''};
 expect(buildWebhookPatch(initial,webhookDraft(initial)).changes).toEqual({enabled:false});
 expect(()=>buildWebhookPatch(initial,{...webhookDraft(initial),token:'new-token'})).toThrow('地址');
});
it.each(['http://notify.example/hook','https://user:pass@notify.example/hook','https://notify.example/hook#secret','https://localhost/hook','https://127.0.0.1/hook','https://10.1.2.3/hook','https://169.254.169.254/hook','https://notify.example/a\r\nb','https:\\notify.example/hook'])(
 'rejects unsafe destination shapes before saving: %s',url=>{expect(()=>buildWebhookPatch(saved(),{...webhookDraft(saved()),url})).toThrow('地址');});
it('rejects header injection and excessive token lengths without printing the token',()=>{
 for(const token of ['private\r\nX-Auth: other','x'.repeat(4097),'private\x00token']){
  try{buildWebhookPatch(saved(),{...webhookDraft(saved()),token});throw new Error('accepted');}
  catch(error){expect(webhookErrorMessage(error)).not.toContain(token);expect(webhookErrorMessage(error)).toContain('令牌');}
 }
});
it('distinguishes receiver acceptance from phone read receipts and unknown outcomes',()=>{
 const accepted=webhookResultMessage({status:'sent',accepted:true,read:false});
 expect(accepted.tone).toBe('success');expect(accepted.text).toContain('受理');expect(accepted.text).toContain('不代表');
 const unknown=webhookResultMessage({status:'unknown',error_code:'WEBHOOK_RESULT_UNKNOWN'});
 expect(unknown.tone).toBe('uncertain');expect(unknown.text).toContain('不会自动');
});
it('uses safe Chinese descriptions and hides arbitrary remote bodies and destination secrets',()=>{
 expect(webhookErrorMessage({code:'WEBHOOK_AUTH_FAILED'})).toContain('令牌');
 expect(webhookErrorMessage({code:'VERSION_CONFLICT'})).toContain('刷新');
 for(const raw of [{code:'PRIVATE_URL',message:'PRIVATE_URL'},new Error('PRIVATE_URL')])expect(webhookErrorMessage(raw)).not.toContain('PRIVATE_URL');
 expect(webhookResultMessage({status:'PRIVATE_RESPONSE',body:'PRIVATE_URL'}).text).not.toContain('PRIVATE');
});
