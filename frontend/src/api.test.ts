import {afterEach,expect,it,vi} from 'vitest';
import {request,waitMail} from './api';
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});
it('sends an explicit JSON envelope for actions without parameters',async()=>{
 const fetch=vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>({data:{job_id:'test'}})});
 vi.stubGlobal('fetch',fetch);
 await request('/accounts/example/test','POST');
 const init=fetch.mock.calls[0][1];
 expect(init.headers['Content-Type']).toBe('application/json');
 expect(JSON.parse(init.body)).toEqual({});
});
it('waits for the independent parser after an import finishes',async()=>{
 vi.useFakeTimers();
 const response=(state:string)=>({ok:true,status:200,json:async()=>({data:{id:'mail',state}})});
 vi.stubGlobal('fetch',vi.fn().mockResolvedValueOnce(response('new')).mockResolvedValueOnce(response('review_needed')));
 const pending=waitMail('mail');
 await vi.runAllTimersAsync();
 expect((await pending).state).toBe('review_needed');
});
