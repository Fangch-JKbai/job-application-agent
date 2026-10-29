import {RecruitmentCategory} from './ui';
import {useState} from 'react';
import {api,type Extraction,type S} from './api';
import {Alert,BusyButton,Select,TimeFields,changed,errorText,initialTime,label,noticeTypes,taskTypes,timeFields,useRequest,type Submit,type TimeDraft} from './ui';
type Result=NonNullable<Extraction['result']>;
type Draft=Result['tasks'][number];
type EditableTask={key:string;original:Draft|null;type:Draft['type'];title:string;round:string;dates:TimeDraft};

export function ExtractionEditor({extraction,onSaved,onCancel}:{extraction:Extraction;onSaved:()=>void;onCancel:()=>void}){
  const original=extraction.result!;
  const [company,setCompany]=useState(original.company_name || '');const [position,setPosition]=useState(original.position_name || '');const [batch,setBatch]=useState(original.batch || '');const [kind,setKind]=useState(original.notification_type);
  const [tasks,setTasks]=useState<EditableTask[]>(original.tasks.map(task=>({key:crypto.randomUUID(),original:task,type:task.type,title:task.title,round:task.round?.toString() || '',dates:initialTime(task)})));
  const [resolved,setResolved]=useState<string[]>([]);const [issues,setIssues]=useState(original.issues.join('\n'));const [reason,setReason]=useState('');const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  const update=(key:string,values:Partial<EditableTask>)=>setTasks(items=>items.map(item=>item.key===key?{...item,...values}:item));
  async function submit(event:Submit){event.preventDefault();setError('');setBusy(true);
    const result:Result=structuredClone(original);const added:string[]=[];
    function fact(key:string,value:unknown,evidence:Record<string,S['Evidence']>){
      if(value===null||value===undefined||value===''){delete evidence[key];return}
      const text=key==='type'?taskTypes[String(value) as keyof typeof taskTypes]:key==='notification_type'?noticeTypes[String(value) as keyof typeof noticeTypes]:String(value);
      evidence[key]={source:'review',quote:text};added.push(label(key)+'：'+text);
    }
    for(const [field,value] of Object.entries({company_name:company || null,position_name:position || null,batch:batch || null,notification_type:kind})){
      if(result[field as keyof Result]!==value){(result as unknown as Record<string,unknown>)[field]=value;fact(field,value,result.evidence)}
    }
    result.tasks=tasks.map(task=>{
      const fields:Omit<Draft,'evidence'|'time_basis'>={type:task.type,title:task.title,round:task.round?Number(task.round):null,...timeFields(task.dates)};
      if(!fields.opens_at&&!fields.starts_at&&!fields.ends_at&&!fields.due_at)fields.source_timezone=task.original?.source_timezone || null;
      const base:Draft=task.original?structuredClone(task.original):{type:task.type,title:task.title,evidence:{},time_basis:{},opens_at:null,starts_at:null,ends_at:null,due_at:null,starts_on:null,due_on:null,round:null,source_timezone:null,time_note:null};
      const delta=changed(task.original || {},fields);
      for(const [key,value] of Object.entries(delta)){
        fact(key,value,base.evidence);
        if(['opens_at','starts_at','ends_at','due_at','starts_on','due_on'].includes(key)){
          if(value)base.time_basis[key as keyof typeof base.time_basis]='explicit';else delete base.time_basis[key as keyof typeof base.time_basis];
        }
      }
      if(!task.original){fact('type',fields.type,base.evidence);fact('title',fields.title,base.evidence)}
      return {...base,...fields};
    });
    result.missing_fields=result.missing_fields.filter(item=>!resolved.includes(item.field)&&!(item.field==='company_name'&&company)&&!(item.field==='position_name'&&position)&&!(item.field==='batch'&&batch));
    result.issues=issues.split('\n').map(v=>v.trim()).filter(Boolean);
    const review_text=reason+'\n'+added.join('\n');const body={version:extraction.version,review_text,result};
    try{await api('/api/notifications/{notification_id}/extractions/{extraction_id}','patch',{params:{notification_id:extraction.notification_id,extraction_id:extraction.id},body});onSaved()}catch(error){setError(errorText(error))}finally{setBusy(false)}
  }
  return <div className="extraction-editor"><div className="section-title"><h3>人工核对提取</h3><button onClick={onCancel} type="button">取消核对</button></div><p className="form-help">保留原文和模型原结果，另存你的核对版本。保存后需要重新匹配并生成方案。</p><form onSubmit={submit}><Alert>{error}</Alert><div className="form-grid"><label>核对公司<input value={company} onChange={e=>setCompany(e.target.value)}/></label><label>核对岗位<input value={position} onChange={e=>setPosition(e.target.value)}/></label><RecruitmentCategory value={batch} onChange={setBatch}/><Select label="核对通知类型" value={kind} options={noticeTypes} onChange={value=>setKind(value as Result['notification_type'])}/></div>{tasks.map((task,index)=><fieldset key={task.key}><legend>草稿事项 {index+1}</legend><label>核对事项名称<input required value={task.title} onChange={e=>update(task.key,{title:e.target.value})}/></label><div className="form-grid"><Select label="核对事项类型" value={task.type} options={taskTypes} onChange={type=>update(task.key,{type:type as Draft['type']})}/><label>核对轮次<input type="number" min="1" value={task.round} onChange={e=>update(task.key,{round:e.target.value})}/></label></div><TimeFields value={task.dates} onChange={dates=>update(task.key,{dates})}/><button type="button" className="text-button" onClick={()=>setTasks(items=>items.filter(item=>item.key!==task.key))}>移除此草稿事项</button></fieldset>)}<button type="button" onClick={()=>setTasks(items=>[...items,{key:crypto.randomUUID(),original:null,type:'other',title:'',round:'',dates:initialTime({})}])}>添加草稿事项</button>{original.missing_fields.length>0&&<fieldset><legend>已解决的问题</legend>{original.missing_fields.map(item=><label className="check-label" key={item.field}><input type="checkbox" checked={resolved.includes(item.field)} onChange={e=>setResolved(v=>e.target.checked?[...v,item.field]:v.filter(key=>key!==item.field))}/>{item.question}</label>)}</fieldset>}<label>仍需保留的提示（每行一条）<textarea rows={2} value={issues} onChange={e=>setIssues(e.target.value)}/></label><label>核对说明<textarea required rows={2} value={reason} onChange={e=>setReason(e.target.value)} placeholder="说明核对依据，例如：已向HR确认截止时间"/></label><p className="form-help">你修改的字段值会一并写入核对说明，作为用户补充依据。</p><BusyButton busy={busy}>保存核对版本</BusyButton></form></div>;
}
