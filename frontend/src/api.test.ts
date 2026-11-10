import {afterEach,expect,it,vi} from 'vitest';
import {request,waitMail,waitJob} from './api';
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
it('shows AI progress and an actionable failure instead of a generic diagnostic error',async()=>{
 vi.useFakeTimers();
 const jobs=[{kind:'ai_parse',status:'queued'},{kind:'ai_parse',status:'running'},{kind:'ai_parse',status:'failed',error_code:'AI_OUTPUT_INVALID'}];
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,status:200,json:async()=>({data:jobs.shift()})})));
 const states:string[]=[];
 const result=waitJob('example',{onProgress:job=>states.push(job.status)}).catch(error=>error);
 await vi.runAllTimersAsync();expect((await result).message).toContain('原文');
 expect(states).toEqual(['queued','running','failed']);
});
it('stops waiting when the original mail view has closed',async()=>{
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
 expect(await waitJob('example',{isCurrent:()=>false})).toBeNull();
 expect(fetch).not.toHaveBeenCalled();
});
