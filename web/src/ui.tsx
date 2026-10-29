import {useEffect,useRef,useState,type DependencyList,type FormEvent,type ReactNode} from 'react';
import type {Page} from './api';
export const stages={submitted:'已投递',screening:'筛选中',assessment:'测评',written_test:'笔试',interview:'面试',offer:'收到 Offer'};
export const outcomes={active:'进行中',rejected:'未通过',withdrawn:'已撤回',accepted:'已接受 Offer',closed:'已结束'};
export const taskTypes={assessment:'测评',written_test:'笔试',interview:'面试',material_submission:'提交材料',offer_response:'回复 Offer',other:'其他'};
export const taskStatuses={pending:'待完成',in_progress:'进行中',completed:'已完成',cancelled:'已取消'};
export const sessionStatuses={idle:'待继续',processing:'处理中',awaiting_user:'待补充',ready:'待确认',failed:'失败',saved:'已保存',unresolved:'待整理',ignored:'已保留',undone:'已撤销'};
export const noticeTypes={assessment_invitation:'测评邀请',written_test_invitation:'笔试邀请',interview_invitation:'面试邀请',schedule_change:'改期',rejection:'未通过通知',offer_received:'收到 Offer',offer_accepted:'已接受 Offer',cancellation:'取消安排',reminder:'提醒',conditional_update:'条件性安排',application_received:'投递确认',material_submission:'提交材料通知',other:'其他'};
export const fieldLabels:Record<string,string>={company_name:'公司',position_name:'岗位',batch:'招聘批次',notification_type:'通知类型',type:'事项类型',title:'事项名称',round:'轮次',sent_at:'原发送时间',timezone_hint:'时区',starts_at:'开始时间',starts_on:'开始日期',due_at:'截止时间',due_on:'截止日期',opens_at:'开放时间',ends_at:'结束时间',source_timezone:'来源时区',time_note:'时间说明'};
export function label(key:string){const task=key.match(/^tasks\.(\d+)\.(.+)$/);return task?`事项${Number(task[1])+1} · ${fieldLabels[task[2]] || '补充说明'}`:fieldLabels[key] || '待确认信息'}
export const navigate=(path:string)=>{location.hash=path};
export function errorText(error:unknown){return error instanceof Error?error.message:'操作失败，请刷新查看当前状态。'}
export function time(value:string|null|undefined,zone='Asia/Shanghai'){
  if(!value)return '—';
  if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value+'（仅日期）';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));
}
export function localClock(value:string|null|undefined,zone='Asia/Shanghai'){
  if(!value)return '';
  const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));
  const part=(key:string)=>parts.find(p=>p.type===key)!.value;
  return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}`;
}
export const offsets:Record<string,string>={'Asia/Shanghai':'+08:00',UTC:'+00:00','Asia/Tokyo':'+09:00'};
export function instant(value:string,zone:string){return value?(offsets[zone]?value+(value.length===16?':00':'')+offsets[zone]:value):null}
export function useResource<T>(load:()=>Promise<T>,deps:DependencyList){
  const [data,setData]=useState<T>();const [error,setError]=useState('');const [loading,setLoading]=useState(true);const [revision,setRevision]=useState(0);
  useEffect(()=>{let active=true;setLoading(true);load().then(value=>{if(active){setData(value);setError('')}}).catch(error=>{if(active)setError(errorText(error))}).finally(()=>{if(active)setLoading(false)});return()=>{active=false}},[...deps,revision]);
  return {data,error,loading,refresh:()=>setRevision(value=>value+1)};
}
export function useRequest(){const previous=useRef({body:'',id:''});return(body:unknown)=>{const key=JSON.stringify(body);if(key!==previous.current.body)previous.current={body:key,id:crypto.randomUUID()};return previous.current.id}}
export function Title({title,description,action}:{title:string;description:string;action?:ReactNode}){return <header className="page-title"><div><h1>{title}</h1><p>{description}</p></div>{action}</header>}
export function Alert({children}:{children:ReactNode}){return children?<p className="alert" role="alert">{children}</p>:null}
export function Empty({children,action}:{children:ReactNode;action?:ReactNode}){return <div className="empty"><p>{children}</p>{action}</div>}
export function Status({value}:{value:string}){return <span className={'status '+(value==='failed'?'bad':value==='saved'||value==='completed'?'good':'')}>{sessionStatuses[value as keyof typeof sessionStatuses] || taskStatuses[value as keyof typeof taskStatuses] || value}</span>}
export function Pager({page,onChange}:{page:Page<unknown>;onChange:(offset:number)=>void}){return <div className="pager"><span>{page.total===0?'0 条记录':`${page.offset+1}–${Math.min(page.offset+page.items.length,page.total)} / ${page.total} 条`}</span><div><button disabled={page.offset===0} onClick={()=>onChange(Math.max(0,page.offset-page.limit))}>上一页</button><button disabled={page.offset+page.items.length>=page.total} onClick={()=>onChange(page.offset+page.limit)}>下一页</button></div></div>}
export function Select({value,onChange,options,label:caption,empty,disabled=false}:{value:string;onChange:(value:string)=>void;options:Record<string,string>;label:string;empty?:string;disabled?:boolean}){return <label>{caption}<select disabled={disabled} value={value} onChange={event=>onChange(event.target.value)}>{empty!==undefined&&<option value="">{empty}</option>}{Object.entries(options).map(([key,text])=><option key={key} value={key}>{text}</option>)}</select></label>}
export function BusyButton({busy,children}:{busy:boolean;children:ReactNode}){return <button className="primary" type="submit" disabled={busy}>{busy?'正在保存…':children}</button>}
export function Zone({value,onChange}:{value:string;onChange:(zone:string)=>void}){return <label>来源时区<select value={value} onChange={e=>onChange(e.target.value)}><option value="Asia/Shanghai">北京时间 · Asia/Shanghai</option><option value="UTC">UTC</option><option value="Asia/Tokyo">东京时间 · Asia/Tokyo</option>{!offsets[value]&&<option value={value}>{value}（保留带偏移的原时间）</option>}</select></label>}
export type TimeDraft={zone:string;startMode:string;dueMode:string;start:string;due:string;opens:string;ends:string;time_note:string};
export function initialTime(value:{source_timezone?:string|null;starts_at?:string|null;starts_on?:string|null;due_at?:string|null;due_on?:string|null;opens_at?:string|null;ends_at?:string|null;time_note?:string|null}):TimeDraft{
  const zone=value.source_timezone || 'Asia/Shanghai';const clock=(v?:string|null)=>offsets[zone]?localClock(v,zone):v || '';
  return {zone,startMode:value.starts_at?'clock':value.starts_on?'date':'none',dueMode:value.due_at?'clock':value.due_on?'date':'none',start:value.starts_at?clock(value.starts_at):value.starts_on || '',due:value.due_at?clock(value.due_at):value.due_on || '',opens:clock(value.opens_at),ends:clock(value.ends_at),time_note:value.time_note || ''};
}
export function timeFields(t:TimeDraft){return {starts_at:t.startMode==='clock'?instant(t.start,t.zone):null,starts_on:t.startMode==='date'?t.start || null:null,due_at:t.dueMode==='clock'?instant(t.due,t.zone):null,due_on:t.dueMode==='date'?t.due || null:null,opens_at:instant(t.opens,t.zone),ends_at:instant(t.ends,t.zone),source_timezone:t.zone,time_note:t.time_note || null}}
export function TimeFields({value:t,onChange:set}:{value:TimeDraft;onChange:(value:TimeDraft)=>void}){
  const put=(key:keyof TimeDraft,value:string)=>set({...t,[key]:value});
  const type=offsets[t.zone]?'datetime-local':'text';
  return <><Zone value={t.zone} onChange={zone=>set({...t,zone,start:'',due:'',opens:'',ends:''})}/><div className="form-grid">{(['start','due'] as const).map(key=>{const mode=key==='start'?t.startMode:t.dueMode;const name=key==='start'?'开始':'截止';return <div key={key}><Select label={name+'时间精度'} value={mode} options={{none:'通知未规定',date:'仅日期',clock:'具体时刻'}} onChange={v=>set({...t,[key+'Mode']:v,[key]:''})}/>{mode!=='none'&&<label>{name+(mode==='date'?'日期':'时间')}<input required type={mode==='date'?'date':type} step="1" value={t[key]} onInput={e=>put(key,e.currentTarget.value)} placeholder={mode==='clock'?'带偏移的 ISO 时间':''}/></label>}</div>})}</div><details className="form-details"><summary>开放时间、结束时间与补充说明</summary><div className="form-grid"><label>开放时间<input type={type} step="1" value={t.opens} onInput={e=>put('opens',e.currentTarget.value)}/></label><label>结束时间<input type={type} step="1" value={t.ends} onInput={e=>put('ends',e.currentTarget.value)}/></label></div><label>时间说明<input value={t.time_note} onChange={e=>put('time_note',e.target.value)} placeholder="例如：具体时刻另行通知"/></label></details></>;
}
export function changed(before:Record<string,unknown>,after:Record<string,unknown>){return Object.fromEntries(Object.entries(after).filter(([key,value])=>key.endsWith('_at')&&typeof value==='string'&&typeof before[key]==='string'?new Date(value).getTime()!==new Date(before[key] as string).getTime():JSON.stringify(before[key] ?? null)!==JSON.stringify(value)))}
export function FormShell({title,children,onCancel}:{title:string;children:ReactNode;onCancel:()=>void}){return <section className="panel form-panel"><div className="section-title"><h2>{title}</h2><button type="button" onClick={onCancel}>取消</button></div>{children}</section>}
export function Loading({error,loading}:{error:string;loading:boolean}){return <><Alert>{error}</Alert>{loading&&<p className="muted" role="status">正在读取…</p>}</>}
export type Submit=FormEvent<HTMLFormElement>;

export function RecruitmentCategory({value,onChange}:{value:string;onChange:(value:string)=>void}){return <label>招聘类别<select value={value} onChange={e=>onChange(e.target.value)}><option value="">待确认</option><option value="27秋招">27秋招</option><option value="实习">实习</option>{value&&!['27秋招','实习'].includes(value)&&<option value={value}>{value}（原记录）</option>}</select></label>}
