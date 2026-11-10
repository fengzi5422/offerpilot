import {expect, it} from 'vitest';
import {formFromTemporal, reviewTemporal, setDisplayTimezone, taskGroup} from './domain';
import type {Temporal} from './types';

it('preserves source evidence and event end when confirming an untouched proposal',()=>{
 const original:Temporal={kind:'absolute',raw_text:'面试原文',timezone:'Asia/Shanghai',timezone_source:'unknown',
  precision:'minute',needs_confirmation:true,starts_at:'2030-01-02T07:00:00Z',ends_at:'2030-01-02T08:00:00Z'};
 const accepted=reviewTemporal(original,formFromTemporal(original),'interview');
 expect(accepted.raw_text).toBe(original.raw_text);
 expect(accepted.ends_at).toBe(original.ends_at);
 expect(accepted.timezone_source).toBe('user_confirmed');
 expect(accepted.needs_confirmation).toBe(false);
});

it('retains relative reference candidates after a user supplies a precise deadline',()=>{
 const original:Temporal={kind:'relative',raw_text:'收到后48小时内',timezone:'Asia/Shanghai',timezone_source:'unknown',
  precision:'unknown',needs_confirmation:true,reference_kind:'internal_date',reference_at:'2030-01-01T00:00:00Z',
  candidates:[{reference_kind:'internal_date',due_at:'2030-01-03T00:00:00Z'}]};
 const accepted=reviewTemporal(original,{date:'2030-01-03',time:'08:00',timezone:'Asia/Shanghai'},'assessment');
 expect(accepted.raw_text).toBe('收到后48小时内');
 expect(accepted.reference_at).toBe(original.reference_at);
 expect(accepted.candidates).toEqual(original.candidates);
 expect(accepted.due_at).toBe('2030-01-03T00:00:00.000Z');
});

it('clears obsolete precise instants when confirming a date-only correction',()=>{
 const original:Temporal={kind:'absolute',raw_text:'原文',timezone:'UTC',timezone_source:'explicit',
  precision:'minute',needs_confirmation:true,due_at:'2030-01-02T00:00:00Z'};
 const accepted=reviewTemporal(original,{date:'2030-01-03',time:'',timezone:'UTC'},'materials');
 expect(accepted.due_at).toBeNull();
 expect(accepted.local_date).toBe('2030-01-03');
 expect(accepted.needs_confirmation).toBe(true);
});

it('groups date-only tasks using their date without inventing a cutoff',()=>{
 setDisplayTimezone('Asia/Shanghai');
 const now=new Date('2030-01-02T06:00:00Z');
 expect(taskGroup({status:'pending',due_date:'2030-01-01'},now)).toBe('overdue');
 expect(taskGroup({status:'pending',due_date:'2030-01-02'},now)).toBe('today');
 expect(taskGroup({status:'pending',due_date:'2030-01-03'},now)).toBe('upcoming');
});
