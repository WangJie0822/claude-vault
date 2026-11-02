'use strict';
// 纯 DOM 单元夹具，不驱动浏览器；浏览器交互另由真实页面验收。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.dataset={};this.disabled=false;this.value='';this.textContent=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  get childElementCount() { return this.children.length; }
  querySelectorAll(tag) { return this.children.flatMap(el=>[...(el.tagName===tag?[el]:[]),...el.querySelectorAll(tag)]); }
}
async function main(){
  if(process.argv[4]==='compact-intent')return compactIntent();
  const source=fs.readFileSync(process.argv[2],'utf8');
  const elements=Object.fromEntries(['message','notes','fields','settings','persist','close','refresh','thread','status','stamp','diagnostic','overview'].map(id=>[id,new Element('div')]));
  const store=new Map(), requests=[];const location={hash:'#synthetic-panel-cap',pathname:'/'};
  const effective={coarse_inject:true,fine_rank:true,fine_supplement:true,auto_fulltext:false,
    'user_prompt_submit.max_notes':3,fine_pin_max:2,fine_max_notes:3,fine_model:'vendor/custom-model'};
  let state={epoch:1,queued_revision:2,applied_revision:1,intent:{},items:{one:{path:'notes/test.md',title:'<img onerror=attack()>',kind:'llm_supplement',annotation_id:'annotation-turn-A'}},
    snapshot_ts:1,snapshot:{core:{status:'scored',metrics_on:true,settings:{effective,locked:[]}}}};
  function create(hash){location.hash=hash;const body=new Element('body');body.dataset.thread='thread-a';
    const context={console,JSON,Date,Set,Error,Number,Object,Array,crypto:{randomUUID:()=> 'synthetic-op'},location,
      document:{body,activeElement:null,createElement:tag=>new Element(tag),getElementById:id=>elements[id]||elements.fields.querySelectorAll('input').find(el=>el.id===id)},
      history:{replaceState:()=>{location.hash='';}},sessionStorage:{setItem:(k,v)=>store.set(k,v),getItem:k=>store.get(k),removeItem:k=>store.delete(k)},
      setInterval:()=>1,clearInterval:()=>{},addEventListener:()=>{},
      fetch:async(path,options)=>{requests.push({path,options});return {ok:true,json:async()=>path==='/api/ops'?{queued_revision:3}:state};}};
    vm.runInNewContext(source,context);return context;
  }
  const flush=async()=>{for(let n=0;n<4;n++)await new Promise(resolve=>setImmediate(resolve));};
  create('#synthetic-panel-cap');await flush();
  assert.equal(location.hash,'');assert.equal(requests[0].options.headers.Authorization,'Bearer synthetic-panel-cap');
  assert.equal(elements.status.textContent,'待应用 · 1 / 2');
  assert.equal(elements.fields.querySelectorAll('input').length,8);
  const model=elements.fields.querySelectorAll('input').find(el=>el.name==='fine_model');
  assert.equal(model.tagName,'input');assert.equal(model.type,'text');assert.equal(model.maxLength,128);
  assert.equal(model.value,'vendor/custom-model');model.value='Other/Exact-Codex-Model';
  elements.settings.onsubmit({preventDefault(){}});await flush();
  const submitted=JSON.parse(requests.find(r=>r.path==='/api/ops').options.body);
  assert.equal(submitted.settings.fine_model,'Other/Exact-Codex-Model');
  assert.match(elements.message.textContent,/已排队 revision 3/);
  const texts=elements.notes.querySelectorAll('h3').map(el=>el.textContent);
  assert.deepEqual(texts,['<img onerror=attack()>']);assert.equal(elements.notes.querySelectorAll('img').length,0);
  assert.equal(elements.notes.querySelectorAll('span')[0].textContent,'模型补充');
  assert.deepEqual(elements.notes.querySelectorAll('h2').map(el=>el.textContent),
    ['脚本粗排','LLM 点名','精排补充','手动钉住','本会话排除','会话开始注入']);
  assert.ok(elements.notes.querySelectorAll('button').some(el=>el.textContent==='相关'));
  // 保存真实渲染产生的旧按钮；后续刷新同篇笔记不能把它的标注绑定到新轮次。
  const oldAnnotationButton=elements.notes.querySelectorAll('button').find(el=>el.textContent==='相关');
  state={...state,items:{one:{...state.items.one,annotation_id:'annotation-turn-B'}}};
  await elements.refresh.onclick();oldAnnotationButton.onclick();await flush();
  const annotationOps=requests.filter(r=>r.path==='/api/ops').map(r=>JSON.parse(r.options.body)).find(op=>op.annotations);
  assert.deepEqual(annotationOps.annotations,[{item:'one',verdict:'relevant',annotation_id:'annotation-turn-A'}]);
  elements.notes.querySelectorAll('button').find(el=>el.textContent==='相关').onclick();await flush();
  const allAnnotations=requests.filter(r=>r.path==='/api/ops').map(r=>JSON.parse(r.options.body)).filter(op=>op.annotations);
  assert.equal(allAnnotations.at(-1).annotations[0].annotation_id,'annotation-turn-B');
  state={...state,queued_revision:3,applied_revision:3};await elements.refresh.onclick();
  assert.equal(elements.status.textContent,'已应用 revision 3');assert.equal(elements.message.textContent,'revision 3 已应用。');
  // 模拟真实刷新：URL 片段已抹除，仍须从本标签会话存储拿到凭证。
  requests.length=0;create('');await flush();
  assert.ok(requests.length>0);assert.equal(requests[0].options.headers.Authorization,'Bearer synthetic-panel-cap');
  state={...state,display:{gate_now:true,settings:{effective:{},source:{},locked:[]}},
    intent:{pin:['notes/a.md','notes/b.md'],exclude:['notes/a.md','notes/b.md']},
    items:{a:{path:'notes/a.md',kind:'user_intent'},b:{path:'notes/b.md',kind:'user_intent'},
      ss:{path:'fixed.md',_event:'SessionStart',_group:'session_start',title:'启动固定项'}}};
  await elements.refresh.onclick();
  const start=elements.notes.querySelectorAll('article').find(el=>el.dataset.item==='ss');
  assert.deepEqual(start.querySelectorAll('button').map(el=>el.textContent),['手动钉住']);
  const pinned=elements.notes.querySelectorAll('article').find(el=>el.dataset.item==='a');
  pinned.querySelectorAll('button').find(el=>el.textContent==='取消手动钉住').onclick();await flush();
  assert.deepEqual(JSON.parse(requests.filter(r=>r.path==='/api/ops').at(-1).options.body).pin,['b']);
  const unknown=elements.fields.querySelectorAll('input').find(el=>el.name==='fine_max_notes');
  assert.equal(unknown.value,'');assert.equal(unknown.disabled,true);
  assert.ok(elements.fields.querySelectorAll('small').some(el=>el.textContent.includes('未知')));
  // 继承宿主模型是可编辑的空值，不能与缺失字段混同而关掉唯一模型输入入口。
  state={...state,display:{...state.display,settings:{effective:{fine_model:null},source:{fine_model:'default'},locked:[]}}};
  await elements.refresh.onclick();
  const inherited=elements.fields.querySelectorAll('input').find(el=>el.name==='fine_model');
  assert.equal(inherited.value,'');assert.equal(inherited.disabled,false);
  state=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));await elements.refresh.onclick();
  assert.match(elements.overview.textContent,/LLM 精排：未开启/);
  const groups=elements.notes.querySelectorAll('section');
  assert.deepEqual(groups.find(g=>g.dataset.group==='coarse').querySelectorAll('h3').map(e=>e.textContent),['真实粗排项']);
  assert.deepEqual(groups.find(g=>g.dataset.group==='llm').querySelectorAll('h3').map(e=>e.textContent),['真实点名项']);
  assert.deepEqual(groups.find(g=>g.dataset.group==='session_start').querySelectorAll('h3').map(e=>e.textContent),['真实启动项']);
  console.log('PASS queued-vs-applied, eight settings, arbitrary model, text rendering, provenance, refresh credential');
}
async function compactIntent(){
  const fixture=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
  let state=fixture.state;const operations=[];
  const elements=Object.fromEntries(['message','notes','fields','settings','persist','close','refresh','thread','status','stamp','diagnostic','overview'].map(id=>[id,new Element('div')]));
  const body=new Element('body');body.dataset.thread=state.thread_id;
  const context={console,JSON,Date,Set,Error,Number,Object,Array,crypto:{randomUUID:()=> 'undo-compact-intent'},
    location:{hash:'#synthetic-capability',pathname:'/'},history:{replaceState(){}},
    sessionStorage:{setItem(){},getItem(){return '';},removeItem(){}},
    document:{body,activeElement:null,createElement:tag=>new Element(tag),
      getElementById:id=>elements[id]||elements.fields.querySelectorAll('input').find(el=>el.id===id)},
    setInterval:()=>1,clearInterval(){},addEventListener(){},
    fetch:async(path,options)=>{
      if(path==='/api/ops'){
        const op=JSON.parse(options.body);operations.push(op);
        const intent={...state.intent};
        for(const key of ['pin','exclude','unpin_llm'])if(key in op){
          intent[key]=op[key].map(id=>{assert.ok(state.items[id], 'operation must use a current server identity');return state.items[id].path;});}
        state={...state,intent};return {ok:true,json:async()=>({queued_revision:state.queued_revision+1})};
      }
      return {ok:true,json:async()=>state};
    }};
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  const flush=async()=>{for(let n=0;n<4;n++)await new Promise(resolve=>setImmediate(resolve));};
  await flush();
  const group=name=>elements.notes.querySelectorAll('section').find(el=>el.dataset.group===name);
  const paths=name=>group(name).querySelectorAll('article').map(row=>row.children.find(el=>el.className==='path').textContent);
  const expectedExcluded=intent=>[...new Set([...(intent.exclude||[]),...(intent.unpin_llm||[])])].sort();
  assert.deepEqual(paths('excluded').sort(),expectedExcluded(state.intent),'excluded group must contain exclusion and unpin intents once per path');
  assert.deepEqual(paths('user').sort(),[...(state.intent.pin||[])].sort(),'manual pins remain visible after compact');
  const targetRow=group(fixture.undo_key==='pin'?'user':'excluded').querySelectorAll('article')
    .find(row=>row.children.find(el=>el.className==='path').textContent===fixture.path);
  assert.ok(targetRow,'compacted intent needs a visible action row');
  const buttons=targetRow.querySelectorAll('button');
  assert.ok(!buttons.some(el=>['相关','不相关','不确定'].includes(el.textContent)), 'intent identity must not offer stale annotation');
  const label={pin:'取消手动钉住',exclude:'撤销排除',unpin_llm:'恢复模型点名'}[fixture.undo_key];
  const undo=buttons.find(el=>el.textContent===label);
  assert.ok(undo,'missing undo action: '+label);
  const before=JSON.parse(JSON.stringify(state.intent));undo.onclick();await flush();
  assert.equal(operations.length,1,'undo must submit exactly one operation');
  assert.deepEqual(Object.keys(operations[0]).sort(),['epoch','operation_id',fixture.undo_key].sort(),'undo must only change its own intent');
  for(const key of ['pin','exclude','unpin_llm'])assert.deepEqual(state.intent[key]||[],
    key===fixture.undo_key?(before[key]||[]).filter(path=>path!==fixture.path):(before[key]||[]), 'undo must preserve other paths and intents: '+key);
  assert.deepEqual(paths('excluded').sort(),expectedExcluded(state.intent),'remaining unpin or exclusion intent must stay visible');
  assert.deepEqual(paths('user').sort(),[...(state.intent.pin||[])].sort());
  console.log(JSON.stringify({operations}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
