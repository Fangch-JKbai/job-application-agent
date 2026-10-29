/* Values come from the review API; raw notifications are always rendered as text. */
const proposalId = location.pathname.split('/').at(-1);
const $ = id => document.getElementById(id);
const labels = {company_name:'公司',company_aliases:'公司别名',position_name:'岗位',batch:'招聘批次',stage:'申请阶段',outcome:'申请结果',applied_at:'投递时间',applied_on:'投递日期',notes:'备注',type:'事项类型',title:'事项名称',status:'完成状态',round:'轮次',opens_at:'开放时间',starts_at:'开始时间',ends_at:'结束时间',due_at:'截止时间',starts_on:'开始日期',due_on:'截止日期',source_timezone:'来源时区',time_note:'时间说明',sent_at:'原发送时间',timezone_hint:'时区'};
const words = {submitted:'已投递',screening:'筛选中',assessment:'测评',written_test:'笔试',interview:'面试',offer:'收到 Offer',active:'进行中',rejected:'未通过',withdrawn:'已撤回',accepted:'已接受 Offer',closed:'已结束',pending:'待完成',in_progress:'进行中',completed:'已完成',cancelled:'已取消',material_submission:'提交材料',offer_response:'回复 Offer',other:'其他',create:'新增',update:'更新',cancel:'取消',skip:'保留'};
labels.notification_type='通知类型';

function factLabel(key) {
  const task=key.match(/^tasks\.(\d+)\.(.+)$/);
  return task ? '事项'+(Number(task[1])+1)+' · '+(labels[task[2]] || '补充说明') : (labels[key] || '补充说明');
}
let review;
let saving = false;

function element(tag,text,className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function format(key,value,zone) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.length ? value.join('、') : '—';
  if (key.endsWith('_at')) return new Intl.DateTimeFormat('zh-CN',{timeZone:zone || 'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value)) + ' · ' + (zone || 'Asia/Shanghai');
  return words[value] || String(value);
}

function same(key,a,b) {
  if (a && b && key.endsWith('_at')) return new Date(a).getTime() === new Date(b).getTime();
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function diffTable(changes,zone) {
  const root = element('div');
  const header = element('div',undefined,'diff diff-head');
  for (const title of ['字段','原值','拟保存']) header.append(element('span',title));
  root.append(header);
  for (const [key,change] of Object.entries(changes)) {
    const row = element('div',undefined,'diff');
    row.append(element('span',labels[key] || key,'field'),element('span',format(key,change.before,zone),'value before'),element('span',format(key,change.after,zone),'value after'));
    root.append(row);
  }
  return root;
}

function notice(text,kind='') { $('notices').append(element('p',text,'notice '+kind)); }

function renderSaved(result) {
  $('step-review').removeAttribute('aria-current');
  $('step-save').setAttribute('aria-current','step');
  $('step-save').textContent='已保存';
  $('confirm-form').hidden = true;
  $('saved-result').hidden = false;
  $('saved-note').textContent = '已保存至 '+result.application.company_name+' · '+result.application.position_name+'。'+format('saved_at',result.saved_at,'Asia/Shanghai')+'。';
  $('result-links').replaceChildren();
  for (const [text,url] of [['查看申请记录','/#/applications/'+result.application.id],['查看待办事项','/#/tasks'],['返回通知处理','/#/notifications/'+result.notification_id]]) {
    const link=element('a',text); link.href=url; $('result-links').append(link);
  }
  $('lead').textContent = '这份方案已保存。下方保留确认时的变更内容。';
}

function render(data) {
  review=data;
  $('back-notice').href='/#/notifications/'+data.notification.id;
  $('step-review').setAttribute('aria-current','step');
  $('step-save').removeAttribute('aria-current');
  $('step-save').textContent='确认保存';
  const payload=data.proposal.payload;
  const application=payload.application_before || payload.new_application;
  $('target').replaceChildren();
  const title=element('div',application.company_name,'target-name');
  title.append(element('span',payload.application_id ? '已有申请' : '新建申请','kind'));
  $('target').append(title,element('p',application.position_name+' · '+(application.batch || '批次未提供'),'target-detail'));
  $('application-changes').replaceChildren(Object.keys(data.application_diff).length ? diffTable(data.application_diff,data.notification.timezone_hint) : element('p','申请字段保持不变。','empty'));
  $('tasks').replaceChildren();
  if (!data.task_actions.length) $('tasks').append(element('p','这份方案没有事项变更。','empty'));
  for (const action of data.task_actions) {
    const task=element('article',undefined,'task');
    const fields=action.fields;
    task.append(element('h3',(words[action.action] || action.action)+' · '+(fields.title || action.before?.title || '事项')));
    task.append(element('p',action.reason,'meta'));
    const changes={};
    for (const [key,value] of Object.entries(fields)) {
      const before=action.before?.[key] ?? null;
      if (labels[key] && !same(key,before,value)) changes[key]={before,after:value};
    }
    task.append(Object.keys(changes).length ? diffTable(changes,fields.source_timezone || action.before?.source_timezone || data.notification.timezone_hint) : element('p','保留现有事项，不新增或重开。','empty'));
    $('tasks').append(task);
  }
  const source={manual:'手动粘贴',email:'邮件原文',sms:'短信原文',other:'其他来源'};
  $('source-meta').textContent=(source[data.notification.source] || '通知原文')+' · 原发送时间：'+format('sent_at',data.notification.sent_at,data.notification.timezone_hint);
  $('original').textContent=data.notification.text;
  $('evidence').replaceChildren();
  for (const [key,fact] of Object.entries(payload.confirmed_fields)) $('evidence').append(element('p',factLabel(key)+'：'+format(key,fact.value,data.notification.timezone_hint)+'；用户补充：“'+fact.quote+'”','evidence-line'));
  const result=payload.extraction_result;
  const evidenceGroups=[result.evidence,...result.tasks.map(task=>task.evidence)];
  for (const evidence of evidenceGroups) for (const [key,fact] of Object.entries(evidence)) $('evidence').append(element('p',factLabel(key)+' · '+(fact.source==='review' ? '用户核对' : fact.source==='text' ? '原文' : '补充信息')+'：“'+fact.quote+'”','evidence-line'));
  $('notices').replaceChildren();
  data.warnings.forEach(text=>notice(text));
  if (!data.execution) data.blockers.forEach(text=>notice(text,'blocked'));
  $('lead').textContent=data.can_confirm ? '核对这一条通知将带来的变更，确认后一次保存。' : '这份方案暂不能保存，请查看下方原因。';
  $('acknowledge').checked=false;
  $('acknowledge').disabled=!data.can_confirm;
  $('confirm').disabled=true;
  $('confirm-form').hidden=false;
  $('saved-result').hidden=true;
  if (data.execution) renderSaved(data.execution);
  $('content').setAttribute('aria-busy','false');
}

async function load() {
  if (saving) return;
  $('refresh').disabled=true;
  $('acknowledge').disabled=true;
  $('confirm').disabled=true;
  $('load-error').hidden=true;
  try {
    const response=await fetch('/api/proposals/'+proposalId+'/review',{cache:'no-store'});
    if (!response.ok) throw new Error('核对内容读取失败，请确认方案仍可访问后重试。');
    render(await response.json());
  } catch(error) {
    review=undefined;
    $('load-error').textContent=error.message;
    $('load-error').hidden=false;
    $('content').setAttribute('aria-busy','false');
  } finally { $('refresh').disabled=false; }
}

$('acknowledge').addEventListener('change',()=>{ $('confirm').disabled=!(review?.can_confirm && $('acknowledge').checked) || saving; });
$('refresh').addEventListener('click',load);
$('confirm-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if (!review?.can_confirm || !$('acknowledge').checked || saving) return;
  saving=true;
  $('confirm').disabled=true;
  $('confirm').textContent='正在保存…';
  $('refresh').disabled=true;
  $('acknowledge').disabled=true;
  const key='proposal-confirm:'+proposalId+':'+review.confirmation.proposal_digest;
  let requestId=sessionStorage.getItem(key);
  if (!requestId) { requestId=crypto.randomUUID(); sessionStorage.setItem(key,requestId); }
  try {
    const response=await fetch('/api/proposals/'+proposalId+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId,...review.confirmation})});
    if (!response.ok) {
      const detail=response.headers.get('Content-Type')?.includes('application/json') ? (await response.json()).detail : '保存失败，请检查服务后重新核对。';
      notice(typeof detail==='string' ? detail : '确认信息无效，请重新核对。','blocked');
      review.can_confirm=false;
    } else {
      const result=await response.json();
      $('notices').replaceChildren();
      renderSaved(result);
      review.can_confirm=false;
    }
  } catch(error) {
    notice('未收到保存响应。可以再次提交同一次确认，服务会返回已有结果，避免重复保存。','blocked');
  } finally {
    saving=false;
    $('confirm').textContent='确认并保存';
    $('refresh').disabled=false;
    $('acknowledge').disabled=!review?.can_confirm;
    $('confirm').disabled=!review?.can_confirm || !$('acknowledge').checked;
  }
});
load();
