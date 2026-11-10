import {expect,it} from 'vitest';
import {dayKey,shiftDate,weekStart,occursOn} from './calendar';

it('uses the display region and Monday even across a year boundary',()=>{
 expect(dayKey('2030-01-01T17:00:00Z','Asia/Shanghai')).toBe('2030-01-02');
 expect(weekStart('2030-01-01')).toBe('2029-12-31');
 expect(shiftDate('2029-12-31',7)).toBe('2030-01-07');
});
it('shows overnight events on both days and excludes the exact end boundary',()=>{
 const task={starts_at:'2030-01-01T15:00:00Z',ends_at:'2030-01-02T16:00:00Z'};
 expect(occursOn(task,'2030-01-01','Asia/Shanghai')).toBe(true);
 expect(occursOn(task,'2030-01-02','Asia/Shanghai')).toBe(true);
 expect(occursOn(task,'2030-01-03','Asia/Shanghai')).toBe(false);
 expect(occursOn({due_at:'2030-01-02T16:00:00Z'},'2030-01-03','Asia/Shanghai')).toBe(true);
});
