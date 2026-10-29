import type {components,paths} from './api-schema';
export type S = components['schemas'];
export type Application = S['ApplicationOut'];
export type Task = S['TaskOut'];
export type Session = S['SessionView'];
export type Extraction = S['ExtractionOut'];
export type Page<T> = {items:T[];total:number;limit:number;offset:number};
type Method<P extends keyof paths> = keyof paths[P] & ('get'|'post'|'patch'|'delete');
type Response<P extends keyof paths,M extends Method<P>> = paths[P][M] extends {responses:infer R} ? R extends {200:{content:{'application/json':infer T}}} ? T : R extends {201:{content:{'application/json':infer T}}} ? T : R extends {202:{content:{'application/json':infer T}}} ? T : never : never;
type Body<P extends keyof paths,M extends Method<P>> = paths[P][M] extends {requestBody:{content:{'application/json':infer T}}} ? T : never;
type Options<P extends keyof paths,M extends Method<P>> = {params?:Record<string,string>;query?:Record<string,string|number|undefined>;body?:Body<P,M>};
export class HttpError extends Error {constructor(public status:number,public data:unknown,message:string){super(message)}}
export async function api<P extends keyof paths,M extends Method<P>>(path:P,method:M,options:Options<P,M>={}):Promise<Response<P,M>> {
  let url:string=path;
  for(const [key,value] of Object.entries(options.params || {})) url=url.replace('{'+key+'}',encodeURIComponent(value));
  const query=new URLSearchParams();
  for(const [key,value] of Object.entries(options.query || {})) if(value!==undefined && value!=='')query.set(key,String(value));
  const response=await fetch(url+(query.size?'?'+query:''),{method:method.toUpperCase(),headers:options.body?{'Content-Type':'application/json'}:undefined,body:options.body?JSON.stringify(options.body):undefined});
  const data=await (response.headers.get('content-type')?.includes('application/json')?response.json():response.text());
  if(!response.ok){
    if(response.status===401)window.dispatchEvent(new Event('job-agent-unauthorized'));
    const detail=data?.detail || data?.error_message || data?.turn?.error_message;
    throw new HttpError(response.status,data,typeof detail==='string'?detail:Array.isArray(detail)?detail.map(item=>item.msg).join('；'):'请求失败，请刷新查看当前状态后重试。');
  }
  return data;
}
export const requestId=()=>crypto.randomUUID();
export function sessionRequest(extraction:Extraction){
  const key='job-agent-session:'+extraction.id+':'+extraction.version;
  let id=localStorage.getItem(key);if(!id){id=requestId();localStorage.setItem(key,id)}return id;
}
