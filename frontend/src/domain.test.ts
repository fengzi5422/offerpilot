import { describe,it,expect } from 'vitest';
import { temporalFromForm, formFromTemporal, taskGroup, changedFields } from './domain';
describe('user deadline precision',()=>{
 it('excludes untouched time evidence from a title-only patch',()=>{expect(changedFields({title:'old',priority:2},{title:'new',priority:2})).toEqual({title:'new'});});
 it('keeps date-only values without an invented instant',()=>{ const t=temporalFromForm('2026-11-12','','custom','Asia/Shanghai'); expect(t.precision).toBe('date');expect(t.due_at).toBeUndefined();expect(t.needs_confirmation).toBe(true); });
 it('converts a confirmed Beijing interview to UTC',()=>{expect(temporalFromForm('2026-11-12','14:00','interview','Asia/Shanghai').starts_at).toBe('2026-11-12T06:00:00.000Z');});
 it('keeps unknown time unknown',()=>{expect(temporalFromForm('','','custom','Asia/Shanghai').kind).toBe('unknown');});
 it('round trips original timezone when editing',()=>{expect(formFromTemporal({kind:'absolute',raw_text:'',timezone:'Asia/Tokyo',timezone_source:'user_confirmed',precision:'minute',needs_confirmation:false,due_at:'2026-11-12T06:00:00Z'})).toEqual({date:'2026-11-12',time:'15:00',timezone:'Asia/Tokyo'});});
 it('places terminal tasks outside overdue',()=>{expect(taskGroup({status:'done',due_at:'2020-01-01T00:00:00Z'},new Date('2026-11-09T00:00:00Z'))).toBe('done');});
 it('rejects nonexistent daylight-saving local time',()=>{expect(()=>temporalFromForm('2026-03-08','02:30','custom','America/New_York')).toThrow();});
});
