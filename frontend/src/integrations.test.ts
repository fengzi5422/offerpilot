import {describe,expect,it} from 'vitest';
import {aiSettingsDraft,buildAIPatch,aiSettingsError,aiRuntimeMessage} from './integrations';
import type {AISettings} from './integrations';
const configured=():AISettings=>({enabled:false,base_url:'https://model.example/v1',model:'mail-model',has_secret:true,version:7,consent_version:'2026-11-10-v1'});
describe('AI configuration consent and credential boundaries',()=>{
 it('turns off immediately without sending unsaved fields or a replacement secret',()=>{
  const saved=configured(),draft={...aiSettingsDraft(saved),enabled:true,baseUrl:'not a URL',apiKey:'new-secret'};
  expect(buildAIPatch(saved,draft,'disable')).toEqual({expected_version:7,changes:{enabled:false}});
 });
 it('requires a current explicit consent action even when previous consent exists',()=>{
  const saved={...configured(),enabled:true};
  expect(()=>buildAIPatch(saved,{...aiSettingsDraft(saved),enabled:true})).toThrow('确认');
  expect(buildAIPatch(saved,{...aiSettingsDraft(saved),enabled:true,consent:true}).changes.consent).toBe(true);
 });
 it('keeps the saved credential when key is blank and never uses a masked placeholder',()=>{
  const saved=configured();expect(aiSettingsDraft(saved).apiKey).toBe('');
  const patch=buildAIPatch(saved,{...aiSettingsDraft(saved),enabled:true,consent:true,apiKey:'   '});
  for(const key of ['api_key','has_secret','consent_version'])expect(patch.changes).not.toHaveProperty(key);
 });
 it('requires an actual first key and includes a nonempty replacement only once',()=>{
  const saved={...configured(),has_secret:false},draft={...aiSettingsDraft({...configured(),has_secret:false}),enabled:true,consent:true};
  expect(()=>buildAIPatch(saved,draft)).toThrow('密钥');
  expect(buildAIPatch(saved,{...draft,apiKey:' new-key '}).changes.api_key).toBe('new-key');
  expect(draft.apiKey).toBe('');
 });
 it('lets an incomplete draft stay disabled without granting consent',()=>{
  const saved={...configured(),base_url:'',model:'',has_secret:false};
  const patch=buildAIPatch(saved,aiSettingsDraft(saved));expect(patch.changes.enabled).toBe(false);
  expect(patch.changes).not.toHaveProperty('consent');
 });
 it('requires endpoint and model before enabling',()=>{
  for(const fields of [{baseUrl:''},{model:''}]){const saved=configured();expect(()=>buildAIPatch(saved,{...aiSettingsDraft(saved),enabled:true,consent:true,...fields})).toThrow();}
 });
 it.each(['http://public.example/v1','https://user:pass@public.example/v1','https://public.example/v1?key=secret','https://public.example/v1#secret','file:///private','http://2130706433/v1','http://0x7f000001/v1','https:\\public.example/v1','https://public.example/v1\nextra'])(
  'rejects unsafe or credential-bearing endpoints: %s',baseUrl=>{const saved=configured();expect(()=>buildAIPatch(saved,{...aiSettingsDraft(saved),baseUrl,enabled:true,consent:true})).toThrow('地址');});
 it.each(['https://api.example/v1','http://localhost:11434/v1','http://127.0.0.1:1234/v1'])(
  'accepts HTTPS or explicit local model endpoints: %s',baseUrl=>{
   const saved=configured(),patch=buildAIPatch(saved,{...aiSettingsDraft(saved),baseUrl:' '+baseUrl+' ',enabled:true,consent:true,model:' model '});
   expect(patch.changes.base_url).toBe(baseUrl);expect(patch.changes.model).toBe('model');
  });
 it('rejects control characters and model/key length limits',()=>{
  const saved=configured(),draft={...aiSettingsDraft(saved),enabled:true,consent:true};
  for(const fields of [{apiKey:'secret\r\nAuthorization: other'},{model:'model\x00name'},{model:'x'.repeat(201)},{apiKey:'x'.repeat(4097)}])expect(()=>buildAIPatch(saved,{...draft,...fields})).toThrow();
 });
});
describe('AI status messages',()=>{
 it('maps known failures to actionable Chinese while rule processing remains available',()=>{
  expect(aiSettingsError({code:'AI_AUTH_FAILED',message:'raw provider body'})).toContain('密钥');
  expect(aiSettingsError({code:'VERSION_CONFLICT'})).toContain('刷新');
  expect(aiRuntimeMessage('AI_RATE_LIMITED')).toContain('规则');expect(aiRuntimeMessage('AI_OUTPUT_INVALID')).toContain('核对');
 });
 it('never displays provider response bodies, arbitrary messages or unknown codes',()=>{
  const raw='secret-token provider response <script>';
  for(const error of [new Error(raw),{code:raw,message:raw},null])expect(aiSettingsError(error)).not.toContain(raw);
  expect(aiRuntimeMessage(raw)).not.toContain(raw);expect(aiRuntimeMessage(null)).toBe('');
 });
});
