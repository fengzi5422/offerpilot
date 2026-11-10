export class ApiError extends Error { constructor(public code:string,message:string,public status:number){super(message);} }
let csrf='';
const pendingKeys=new Map<string,string>();
export async function request<T=any>(path:string,method='GET',body?:unknown,key?:string,envelope=false):Promise<T>{
 const headers:Record<string,string>={};
 const command=method+path+(body instanceof FormData?'upload':JSON.stringify(body??null));
 if(method!=='GET'){headers['X-CSRF-Token']=csrf;if(!pendingKeys.has(command))pendingKeys.set(command,key??crypto.randomUUID());headers['Idempotency-Key']=key??pendingKeys.get(command)!;}
 if(method!=='GET' && !(body instanceof FormData))headers['Content-Type']='application/json';
 const response=await fetch(`/api/v1${path}`,{method,headers,credentials:'same-origin',body:method==='GET'?undefined:body instanceof FormData?body:JSON.stringify(body??{})});
 const result=await response.json().catch(()=>({}));
 if(response.ok||response.status<500)pendingKeys.delete(command);
 if(!response.ok)throw new ApiError(result.error?.code??'REQUEST_FAILED',result.error?.message??`请求失败（${response.status}）`,response.status);
 return (envelope?result:result.data) as T;
}
export function requestPage<T>(path:string){return request<{data:T[];next_cursor?:string|null}>(path,'GET',undefined,undefined,true);}
export async function connect(){
 const fragment=new URLSearchParams(location.hash.slice(1));
 const code=fragment.get('code')??fragment.get('bootstrap');
 if(code){history.replaceState(null,'',location.pathname+location.search);const data=await request('/session/bootstrap','POST',{code});csrf=data.csrf_token??'';}
 const session=await request('/session');csrf=session.csrf_token;
}
export async function waitJob(id:string){
 for(let i=0;i<90;i++){const job=await request(`/jobs/${id}`);if(job.status==='succeeded'||job.state==='succeeded')return job;if(['failed','interrupted'].includes(job.status??job.state))throw new Error(job.error?.message??job.error_code??'操作没有完成，请重试');await new Promise(r=>setTimeout(r,1000));}
 throw new Error('操作仍在后台进行，请稍后查看状态');
}
export function errorText(e:unknown){return e instanceof Error?e.message:'操作失败，请重试';}
