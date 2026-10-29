import {useState} from 'react';
import {api,requestId} from './api';
import {Alert,FormShell,errorText,navigate,type Submit} from './ui';
export function NoticeIntake({onCancel}:{onCancel:()=>void}){
 const [text,setText]=useState('');const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [noticeId,setNoticeId]=useState('');const [id]=useState(requestId);
 async function submit(e:Submit){e.preventDefault();setBusy(true);setError('');try{
  const n=noticeId?{id:noticeId}:await api('/api/notifications','post',{body:{request_id:id,text,source:'manual',calendar_year:new Date().getFullYear(),timezone_hint:'Asia/Shanghai'}});setNoticeId(n.id);
  await api('/api/jobs','post',{body:{operation:'process_notification',request_id:requestId(),notification_id:n.id}});navigate('/notifications/'+n.id);
 }catch(e){setError(errorText(e))}finally{setBusy(false)}}
 return <FormShell title="粘贴通知，自动记录" onCancel={onCancel}><form onSubmit={submit}><Alert>{error}</Alert><label>招聘通知原文<textarea rows={6} required value={text} disabled={!!noticeId} onChange={e=>setText(e.target.value)} placeholder="粘贴投递回执、笔试、测评或面试通知…"/></label><p className="form-help">自动识别公司、岗位和进度并保存，不需要逐条确认。岗位可留空，日期不强制到时刻；缺年按今年。保存后可编辑或撤销。</p><div className="form-actions"><button type="submit" className="primary" disabled={busy}>{busy?'正在识别并记录…':'自动识别并记录'}</button>{noticeId&&<a href={'#/notifications/'+noticeId}>查看已保存的原通知</a>}</div></form></FormShell>
}
