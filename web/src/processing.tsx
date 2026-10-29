import {useEffect,useState} from 'react';
import {api,type S} from './api';
import {Alert,Loading,Pager,Status,errorText,time,useRequest,useResource} from './ui';

type Attempt=S['AttemptView'];
const tools:Record<string,string>={search_applications:'匹配申请',get_application:'读取申请',select_application:'选择申请',get_application_tasks:'读取待办',get_upcoming_tasks:'读取近期事项',record_answer:'记录补充',revise_extraction:'核对字段',choose_new_application:'准备新申请',ask_user:'准备追问',prepare_update:'准备方案'};
function phase(value:string){if(value.startsWith('tool')){const [step,name]=value.split(':');return (step==='tool_completed'?'已完成：':'正在：')+(tools[name] || name)}return ({model:'等待模型响应',model_received:'已收到模型响应',validating:'检查提取结果',starting:'准备处理',completed:'已完成',awaiting_user:'等待补充',ready:'方案已准备',failed:'本次失败',model_error:'模型调用未完成',model_or_validation_error:'调用或结果检查失败',unexpected_error:'内部错误',interrupted:'已中断',legacy_processing:'旧版处理记录'} as Record<string,string>)[value] || value}
function tokens(row:Attempt){const n=row.usage?.total_tokens;return typeof n==='number'?`${n} Token${row.usage?.complete===false?'（已取得部分）':''}`:'用量未取得'}

export function ProcessingHistory({notificationId,revision,polling,onChanged,onBusy,onOrphan,readOnly=false}:{readOnly?:boolean;notificationId:string;revision:string;polling:boolean;onChanged:()=>void;onBusy:(text:string)=>void;onOrphan:(value:boolean)=>void}){
  const [offset,setOffset]=useState(0);const [error,setError]=useState('');const [busy,setBusy]=useState('');const [detail,setDetail]=useState<S['AttemptDetail']>();const id=useRequest();
  const r=useResource(()=>api('/api/processing','get',{query:{notification_id:notificationId,limit:10,offset}}),[notificationId,revision,offset]);
  useEffect(()=>{if(r.data)onOrphan(r.data.items.some(row=>row.recoverable))},[r.data]);
  useEffect(()=>{if(!polling)return;const timer=setInterval(r.refresh,1500);return()=>clearInterval(timer)},[polling]);
  async function act(row:Attempt,action:'recover'|'retry'){
    setError('');setBusy(row.id);onBusy(action==='recover'?'正在解除中断状态…':'正在重试这次处理…');
    try{if(action==='recover')await api('/api/processing/{kind}/{attempt_id}/recover','post',{params:{kind:row.kind,attempt_id:row.id}});
      else await api('/api/processing/{kind}/{attempt_id}/retry','post',{params:{kind:row.kind,attempt_id:row.id},body:{request_id:id({kind:row.kind,id:row.id})}});
    }catch(error){setError(errorText(error))}finally{setBusy('');onBusy('');r.refresh();onChanged()}
  }
  async function inspect(row:Attempt){setError('');try{setDetail(await api('/api/processing/{kind}/{attempt_id}','get',{params:{kind:row.kind,attempt_id:row.id}}))}catch(error){setError(errorText(error))}}
  return <section id="processing-history" className="panel"><div className="section-title"><h2>处理记录与恢复</h2><button onClick={r.refresh} disabled={!!busy}>刷新记录</button></div><p className="muted">{readOnly?'后台任务会自动处理中断；需要时在上方任务卡片重试或取消。':'中断后先解除状态，再重试。提取成功后自动记录，处理结果可编辑或撤销。'}</p><Alert>{error}</Alert><Loading {...r}/>{r.data&&<>{r.data.items.map(row=><article className="event" key={row.id}><div><strong>{row.kind==='extraction'?'通知提取':'匹配与补充'} · <Status value={row.recoverable?'已中断':row.status}/></strong><time>{time(row.created_at)}</time></div><p>{phase(row.phase)}{row.active?' · 仍在运行':''}{row.recoverable?' · 执行已中断，可解除状态':''}</p><p>{row.elapsed_ms===null?'尚未结束':`${(row.elapsed_ms/1000).toFixed(1)} 秒`} · {tokens(row)} · 费用未知</p>{row.error_message&&<p className="preserve">{row.error_message}</p>}{row.retry_of&&<p>重试来源：{row.retry_of.slice(0,8)}</p>}<div className="row-actions">{!readOnly&&row.recoverable&&<button disabled={!!busy} onClick={()=>act(row,'recover')}>解除中断状态</button>}{!readOnly&&row.can_retry&&<button disabled={!!busy} onClick={()=>act(row,'retry')}>重试这次处理</button>}<button disabled={!!busy} onClick={()=>inspect(row)}>查看记录</button></div>{detail?.attempt.id===row.id&&<details open><summary>阶段、用量与调用记录</summary><p className="muted">{detail.attempt.cost_basis.basis as string}。记录编号：{row.id}</p><pre className="original">{JSON.stringify({usage:detail.attempt.usage,trace:detail.trace},null,2)}</pre></details>}</article>)}{r.data.total===0&&<p className="muted">尚未开始提取或匹配。</p>}{r.data.total>10&&<Pager page={r.data} onChange={setOffset}/>}</>}</section>;
}
