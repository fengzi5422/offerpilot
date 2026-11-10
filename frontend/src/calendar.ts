export function dayKey(value:string|Date,zone:string){return new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));}
export function shiftDate(day:string,offset:number){const d=new Date(day+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10);}
export function weekStart(day:string){return shiftDate(day,-((new Date(day+'T12:00:00Z').getUTCDay()+6)%7));}
export function occursOn(task:{starts_at?:string|null;due_at?:string|null;ends_at?:string|null},day:string,zone:string){
 const start=task.starts_at??task.due_at;if(!start)return false;
 const first=dayKey(start,zone),last=task.ends_at?dayKey(new Date(Date.parse(task.ends_at)-1),zone):first;
 return first<=day&&day<=last;
}
