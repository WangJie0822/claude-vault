'use strict';
const thread = document.body.dataset.thread;
const storageKey = 'context-vault:'+thread;
let capability = location.hash.slice(1);
try { if(capability)sessionStorage.setItem(storageKey,capability);else capability=sessionStorage.getItem(storageKey)||''; } catch (_) {}
history.replaceState(null, '', location.pathname);
const $ = id => document.getElementById(id);
let state = null, submitting = false, lastQueued = 0;
const labels = {coarse_inject:'粗排清单',fine_rank:'模型精排',fine_supplement:'逐轮补充',auto_fulltext:'自动全文',
  'user_prompt_submit.max_notes':'清单篇数',fine_pin_max:'首轮点名上限',fine_max_notes:'补充上限',fine_model:'Codex 模型 ID'};
const booleans = new Set(['coarse_inject','fine_rank','fine_supplement','auto_fulltext']);
const kinds = {admitted_list:'粗排清单',admitted_fulltext:'全文',llm_pinned:'模型点名',llm_supplement:'模型补充',user_pinned:'手动钉住'};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el; }
async function api(path, body) {
  if(!capability)throw Error('请从 vault-injected 的 open 入口打开面板。');
  const options={headers:{Authorization:'Bearer '+capability,'X-Context-Vault-Thread':thread},cache:'no-store'};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch(path,options);const data=await response.json();
  if(!response.ok){if(response.status===401)try{sessionStorage.removeItem(storageKey);}catch(_){}throw Error(data.detail||data.error||'服务不可用');}return data;
}
async function submit(ops) {
  if(!state||submitting)return;
  submitting=true;
  try{const result=await api('/api/ops',{epoch:state.epoch,operation_id:crypto.randomUUID(),...ops});
    lastQueued=result.queued_revision;$('message').textContent='已排队 revision '+lastQueued+'，等待下一次提问应用。';await refresh();
  }catch(error){$('message').textContent=error.message;}finally{submitting=false;}
}
function listIds(paths){return paths.map(path=>{const found=Object.entries(state.items||{}).find(([,v])=>v.path===path);if(!found)throw Error('条目身份已过期，请先提交一次提问刷新。');return found[0];});}
function toggle(key, item, undo=false){try{const paths=new Set(state.intent?.[key]||[]);if(undo)paths.delete(item.path);else paths.add(item.path);submit({[key]:listIds([...paths])});}catch(error){$('message').textContent=error.message;}}
function valueText(v){return v===null||v===undefined?'未知':Array.isArray(v)?v.join('、')||'无':typeof v==='boolean'?(v?'是':'否'):String(v);}
function noteDetail(item){
  const lines=[['录取通路',item.admit_arm],['总分',item.score],['主题相关分',item.topical],
    ['提问命中',item.hits],['主题词命中',item.topic_hits],['来源理由',item.why],
    ['粗排名次',item.rank],['去重裁决',item.dedup],['来源轮次',item.src_seq],['已投递',item.rendered]];
  return lines.filter(([,v])=>v!==undefined).map(([k,v])=>k+'：'+valueText(v)).join('\n')||'本条目没有详细命中信息。';
}
function renderNotes(){
  const area=$('notes');area.replaceChildren();const items=Object.entries(state.items||{});
  const groups=[['coarse','脚本粗排'],['llm','LLM 点名'],['supps','精排补充'],['user','手动钉住'],['excluded','本会话排除'],['session_start','会话开始注入']];
  const source=item=>item._group||(item._event==='SessionStart'?'session_start':item.kind==='llm_pinned'?'llm':item.kind==='llm_supplement'?'supps':item.kind==='user_pinned'?'user':'coarse');
  for(const [group,title] of groups){const section=node('section',undefined,'note-group');section.dataset.group=group;section.append(node('h2',title));
  const selected=items.filter(([,item])=>item.kind==='user_intent'?
    (group==='user'&&(state.intent?.pin||[]).includes(item.path)||group==='excluded'&&['exclude','unpin_llm'].some(key=>(state.intent?.[key]||[]).includes(item.path))):source(item)===group);
  const seen=new Set(),entries=selected.filter(([,item])=>{if(seen.has(item.path))return false;seen.add(item.path);return true;});
  if(!entries.length)section.append(node('p',group==='coarse'&&state.display?.gate_now?'本轮未召回；保留上次打分结果。':'暂无条目','hint'));
  for(const [id,item] of entries){const row=node('article',undefined,'note');row.dataset.item=id;
    const excluded=(state.intent?.exclude||[]).includes(item.path),pinned=(state.intent?.pin||[]).includes(item.path);
    const core=state.display||state.snapshot?.core,appliedExcluded=(core?.excluded||[]).some(v=>v.path===item.path),appliedPin=(core?.pins?.user||[]).some(v=>v.path===item.path);
    row.append(node('span',kinds[item.kind]||(appliedExcluded?'已排除':appliedPin?'手动钉住':item.kind||'来源未知'),'kind'),node('h3',item.title||item.path),node('p',item.path,'path'));
    if(item.summary)row.append(node('p',item.summary));
    const actions=node('nav');
    const button=(label,fn)=>{const b=node('button',label);b.type='button';b.onclick=fn;actions.append(b);};
    if(group!=='session_start')button(excluded?'撤销排除':'排除',()=>toggle('exclude',item,excluded));
    button(pinned?'取消手动钉住':'手动钉住',()=>toggle('pin',item,pinned));
    if((state.intent?.unpin_llm||[]).includes(item.path))button('恢复模型点名',()=>toggle('unpin_llm',item,true));
    else if(item.kind==='llm_pinned')button('取消模型点名',()=>toggle('unpin_llm',item));
    if(core?.metrics_on&&item.annotation_id&&['admitted_list','admitted_fulltext','llm_pinned','llm_supplement'].includes(item.kind)){
      for(const [verdict,label] of [['relevant','相关'],['irrelevant','不相关'],['unsure','不确定']])button(label,()=>submit({annotations:[{item:id,verdict,annotation_id:item.annotation_id}]}));}
    const detail=node('details');detail.append(node('summary','查看命中词、分数与来源解释'),node('pre',noteDetail(item)));
    const raw=node('details');raw.append(node('summary','原始条目'),node('pre',JSON.stringify(item,null,2)));
    row.append(actions,detail,raw);section.append(row);
  }area.append(section);}
}
function renderSettings(){
  const snapshot=(state.display||state.snapshot?.core)?.settings;const fields=$('fields');
  if(fields.childElementCount&&document.activeElement?.closest('#settings'))return;
  fields.replaceChildren();
  for(const [key,title] of Object.entries(labels)){const label=node('label');label.append(node('span',title));const input=document.createElement('input');input.name=key;input.id='setting-'+key;
    const value=(state.intent?.settings&&key in state.intent.settings)?state.intent.settings[key]:snapshot?.effective?.[key];
    const inheritedModel=key==='fine_model'&&value===null&&Object.prototype.hasOwnProperty.call(snapshot?.effective||{},key)&&['session','config','default'].includes(snapshot?.source?.[key]);
    const known=booleans.has(key)?typeof value==='boolean':key==='fine_model'?(typeof value==='string'||inheritedModel):typeof value==='number'&&Number.isFinite(value);
    if(booleans.has(key)){input.type='checkbox';input.checked=value===true;input.indeterminate=!known;}else if(key==='fine_model'){input.type='text';input.maxLength=128;input.value=value??'';input.placeholder='继承插件 / Codex 配置';}else{input.type='number';input.min='0';input.max='8';input.value=value??'';}
    const locked=(snapshot?.locked||[]).includes(key),pending=state.intent?.settings&&key in state.intent.settings&&state.queued_revision>state.applied_revision;
    input.disabled=!snapshot||!known||locked;label.append(input,node('small',(known?'':'值未知 · ')+'来源：'+valueText(snapshot?.source?.[key])+(locked?' · 已锁定':'')+(pending?' · 待应用':'')));fields.append(label);}
  for(const b of $('settings').querySelectorAll('button'))b.disabled=!snapshot;
}
function settings(){const result={};for(const key of Object.keys(labels)){const input=document.getElementById('setting-'+key);if(input.disabled)continue;if(key==='fine_model'&&!input.value.trim())continue;result[key]=booleans.has(key)?input.checked:key==='fine_model'?input.value.trim():Number(input.value);}return result;}
async function refresh(){try{state=await api('/api/state');$('thread').textContent='线程 '+thread+' · 上下文 epoch '+state.epoch;
  const queued=state.queued_revision||0,applied=state.applied_revision||0;
  $('status').textContent=queued>applied?'待应用 · '+applied+' / '+queued:'已应用 revision '+applied;
  if(state.snapshot?.core?.rejected?.length)$('message').textContent='存在被拒绝的操作，请展开诊断查看原因。';
  else if(lastQueued&&applied>=lastQueued)$('message').textContent='revision '+lastQueued+' 已应用。';
  $('stamp').textContent=state.snapshot_ts?'上次真实快照：'+new Date(state.snapshot_ts*1000).toLocaleString()+' · 状态 '+(state.snapshot?.core?.status||'未知'):'尚无真实轮次快照';
  $('diagnostic').textContent=JSON.stringify({llm:state.snapshot?.core?.llm??'未知',thresholds:state.snapshot?.core?.thresholds??'未知',rejected:state.snapshot?.core?.rejected??[],read_coverage:'仅结构化成功读取；其他读取未知'},null,2);
  const display=state.display||state.snapshot?.core, llm=display?.llm, th=display?.thresholds;
  const llmStates={off:'未开启',idle:'尚未运行',unavailable:'无法发起',pending:'运行中',ok:'完成',failed:'失败'};
  $('overview').textContent=(display?.gate_now?'本轮未召回，以下保留上次打分结果。\n':'')+
    'LLM 精排：'+(llmStates[llm?.state]||'未知')+' · 失败原因：'+valueText(llm?.fail)+'\n'+
    '主题词：'+valueText(llm?.words)+' · 已用于打分：'+valueText(llm?.in_scoring)+' · 点名篇数：'+valueText(llm?.n_pinned)+' · 调用次数：'+valueText(llm?.calls)+'\n'+
    '录取阈值：'+valueText(th?.min_topical)+' · 全文阈值：'+valueText(th?.ft_topical)+' · 主题词加分：'+valueText(th?.topic_hit_weight);
  renderNotes();renderSettings();
}catch(error){$('status').textContent='连接不可用';$('message').textContent=error.message;}}
$('refresh').onclick=refresh;
$('settings').onsubmit=event=>{event.preventDefault();submit({settings:settings()});};
$('persist').onclick=()=>{const values=settings();submit({settings:values,persist:Object.keys(values).filter(k=>k!=='fine_model')});};
$('close').onclick=async()=>{try{await api('/api/close',{});try{sessionStorage.removeItem(storageKey);}catch(_){}$('status').textContent='面板服务已关闭';clearInterval(timer);}catch(error){$('message').textContent=error.message;}};
addEventListener('pagehide',()=>{if(capability)fetch('/api/detach',{method:'POST',keepalive:true,headers:{Authorization:'Bearer '+capability,'X-Context-Vault-Thread':thread,'Content-Type':'application/json'},body:'{}'}).catch(()=>{});});
const timer=setInterval(refresh,4000);refresh();
