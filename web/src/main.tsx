import {useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Applications,ApplicationDetail} from './applications';
import {Inbox,NoticeDetail,ImportNotice} from './notifications';
import {Tasks} from './tasks';
import './style.css';
import {Mailboxes} from './mailboxes';
import {AuthGate} from './auth';

function route(){const [path,query='']=location.hash.slice(1).split('?');return {path:path || '/applications',query:new URLSearchParams(query)}}
function App(){
  const [current,setCurrent]=useState(route);const [menu,setMenu]=useState(false);
  useEffect(()=>{const update=()=>{setCurrent(route());setMenu(false);window.scrollTo(0,0)};window.addEventListener('hashchange',update);return()=>window.removeEventListener('hashchange',update)},[]);
  const parts=current.path.split('/').filter(Boolean);const nav=parts[0];
  return <div className="app-shell"><header className="mobile-bar"><span>招聘通知 Agent</span><button onClick={()=>setMenu(!menu)} aria-expanded={menu}>导航</button></header><aside className={menu?'sidebar open':'sidebar'}><a href="#/applications" className="brand"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 4h13v16H4zM8 9h5M8 13h3M14 15l2 2 5-6"/></svg>招聘通知 Agent</a><p className="workspace">本地个人空间</p><nav aria-label="主导航">{[['applications','申请记录'],['notifications','通知收件箱'],['tasks','待办事项'],['mailboxes','邮箱接入']].map(([key,text])=><a key={key} href={'#/'+key} aria-current={nav===key?'page':undefined}>{text}</a>)}</nav><div className="sidebar-note"><p>通知 → 自动记录 → 待办</p><a href="/docs">接口文档</a></div></aside><main className="canvas" key={location.hash}>{nav==='applications'?(parts[1]?<ApplicationDetail id={parts[1]}/>:<Applications query={current.query}/>):nav==='notifications'?(parts[1]==='new'?<ImportNotice/>:parts[1]?<NoticeDetail id={parts[1]}/>:<Inbox query={current.query}/>):nav==='tasks'?<Tasks query={current.query}/>:nav==='mailboxes'?<Mailboxes/>:<p>没有这个页面。<a href="#/applications">返回申请记录</a></p>}</main></div>;
}
createRoot(document.getElementById('root')!).render(<AuthGate><App/></AuthGate>);
