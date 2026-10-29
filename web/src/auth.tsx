import {useEffect,useState,type ReactNode} from 'react';
import {api} from './api';
import {Alert,errorText,type Submit} from './ui';
export function AuthGate({children}:{children:ReactNode}){
 const [ready,setReady]=useState(false);const [mode,setMode]=useState('');const [token,setToken]=useState('');const [error,setError]=useState('');const [busy,setBusy]=useState(false);
 async function load(){try{const s=await api('/api/auth/status','get');setReady(s.authenticated);setMode(s.mode)}catch(e){setError(errorText(e))}}
 useEffect(()=>{load();const invalid=()=>{setReady(false);setToken('')};window.addEventListener('job-agent-unauthorized',invalid);return()=>window.removeEventListener('job-agent-unauthorized',invalid)},[]);
 async function login(e:Submit){e.preventDefault();setBusy(true);setError('');try{await api('/api/auth/login','post',{body:{token}});setToken('');await load()}catch(e){setError(errorText(e))}finally{setBusy(false)}}
 if(ready)return <>{mode==='private'&&<div className="session-bar"><span>个人工作台</span><button onClick={async()=>{await api('/api/auth/logout','post');setReady(false)}}>退出登录</button></div>}{children}</>;
 return <main className="login-page"><section className="panel"><h1>招聘通知 Agent</h1><p className="muted">登录你的个人工作台</p><Alert>{error}</Alert>{mode?<form onSubmit={login}><label>访问口令<input required type="password" autoComplete="current-password" value={token} onChange={e=>setToken(e.target.value)}/></label><div className="form-actions"><button className="primary" disabled={busy}>{busy?'正在登录…':'进入工作台'}</button></div></form>:<button onClick={load}>重新连接</button>}</section></main>
}
