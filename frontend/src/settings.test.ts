import {expect,it,vi} from 'vitest';
import {reminderDraft,reminderChanges,saveAndTestMailbox} from './settings';

it('converts reminder minutes and quiet hours to one settings patch',()=>{
 const draft=reminderDraft({reminder_offsets_seconds:{custom:[3600],interview:[86400,900]},quiet_hours:{enabled:false,start:'23:00',end:'08:00'},notification_privacy:'generic'});
 draft.offsets.custom='60, 10';draft.offsets.interview='';draft.quiet.enabled=true;
 const changes=reminderChanges(draft);
 expect(changes.reminder_offsets_seconds.custom).toEqual([3600,600]);
 expect(changes.reminder_offsets_seconds.interview).toEqual([]);
 expect(changes.quiet_hours).toEqual({enabled:true,start:'23:00',end:'08:00'});
});
it('rejects invalid, duplicate or excessive reminder offsets and equal quiet boundaries',()=>{
 for(const value of ['0','1.5','10081','10,10','abc',Array.from({length:11},(_,i)=>i+1).join(',')]){
  const draft=reminderDraft({});draft.offsets.custom=value;
  expect(()=>reminderChanges(draft)).toThrow();
 }
 const draft=reminderDraft({});draft.quiet={enabled:true,start:'08:00',end:'08:00'};
 expect(()=>reminderChanges(draft)).toThrow();
});
it('retains the saved account when connection fails and replaces credentials on retry',async()=>{
 const state={accountId:''};
 const call=vi.fn().mockResolvedValueOnce({id:'saved'}).mockResolvedValueOnce({job_id:'test'});
 const wait=vi.fn().mockRejectedValueOnce(new Error('network unavailable'));
 const input={address:'example@qq.com',code:'sample',initialScan:'14'};
 await expect(saveAndTestMailbox(state,input,call,wait)).rejects.toThrow();
 expect(state.accountId).toBe('saved');
 call.mockResolvedValueOnce({id:'saved'}).mockResolvedValueOnce({job_id:'second'});
 wait.mockResolvedValueOnce({status:'completed'});
 await saveAndTestMailbox(state,input,call,wait);
 expect(call.mock.calls.map(c=>[c[0],c[1]])).toEqual([
  ['/accounts','POST'],['/accounts/saved/test','POST'],
  ['/accounts/saved/credential','PUT'],['/accounts/saved/test','POST']]);
});
