"""Exercise the real home conversation/history JS using isolated browser state.

The small DOM below only implements form rendering and selectors. Draft storage,
history grouping, selection, and switching are loaded from production source.
No application server, real browser session, or user database is touched.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which('node')
ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.argv[1],scenario=process.argv[2];
const state={authenticated:true,csrf:'synthetic-session',view:'dashboard',discussionNavigationGeneration:0,detail:null};
const listeners=new Map(),storage=new Map(),notifications=[],calls=[];
let serial=0,refreshes=0,currentHTML='',currentForm=null,editForm=null,planGate=null,readGate=null;
const timers=[],pollNodes=[];
const recentDisplay={outerHTML:''};
const turns=[
  {id:12,record_id:102,plan_id:1,text:'synthetic plan latest supplement',reply:'saved supplement',status:'done',created_at:120,result:{}},
  {id:11,record_id:101,plan_id:1,text:'synthetic plan first utterance',reply:'saved original',status:'done',created_at:110,result:{}},
  {id:20,record_id:200,plan_id:null,text:'synthetic unrelated thought',reply:'saved research receipt',status:'done',created_at:200,result:{intent:'research'}}
];
const plans=[
  {id:1,record_id:101,revision:2,title:'synthetic first plan',goal:'first goal',date:'2026-10-08',status:'preparing',updated_at:120,turns:turns.filter(t=>t.plan_id===1),history:[]},
  {id:2,record_id:201,revision:1,title:'synthetic second plan',goal:'second goal',date:'2026-10-09',status:'preparing',updated_at:180,turns:[],history:[]}
];
const records=[
  {id:101,title:'synthetic first source',content:'plan source',status:'following',created_at:110},
  {id:102,title:'synthetic supplement source',content:'plan supplement',status:'following',created_at:120},
  {id:200,title:'synthetic thought',content:'unrelated thought',status:'unfiled',created_at:200},
  {id:301,title:'synthetic imported record',content:'imported content',status:'unfiled',created_at:300}
];
const matters=[{id:7,revision:3,title:'synthetic continuing goal',customer_id:9,opportunity_id:8,visibility:'active',status:'following'}];
const main={forms:[],querySelector(selector){if(selector==='#flow-home-form'||selector==='[data-flow-form]')return currentForm;return null;},querySelectorAll(selector){return selector==='form'||selector==='[data-flow-form]'||selector==='[data-flow-form],[data-chat-attachment-form]'?this.forms:[];}};
const editRoot={forms:[],querySelector(selector){if(selector==='[data-flow-form]')return editForm;return null;},querySelectorAll(selector){return selector==='form'||selector==='[data-flow-form]'||selector==='[data-flow-form],[data-chat-attachment-form]'?this.forms:[];}};
function field(name,value=''){return {name,value,type:name==='text'?'textarea':'hidden',defaultValue:value,focus(){this.focused=true;},setAttribute(){},removeAttribute(){}};}
function formFromHTML(html){
  const raw=html.match(/<form\s+id="flow-(?:home|conversation)-form"[^>]*>/)?.[0];assert(raw,'the real flow composer must render');
  const dataset={};for(const m of raw.matchAll(/data-([\w-]+)="([^"]*)"/g))dataset[m[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=m[2];
  const names=['text','request_id','request_text','attachment_items','attachment_attempts'];
  const fields=new Map(names.map(name=>[name,field(name,name==='attachment_items'?'[]':name==='attachment_attempts'?'{}':'')]));
  const error={textContent:''},submit={disabled:false,isConnected:true};
  const form={id:raw.match(/id="([^"]+)"/)[1],dataset,isConnected:true,fields,error,submit,notes:[],parentElement:main,
    elements:{namedItem(name){return fields.get(name)||null;}},
    querySelectorAll(selector){if(selector==='input[name],textarea[name],select[name]'||selector==='[name]')return [...fields.values()];return [];},
    querySelector(selector){if(selector==='.form-error')return error;if(selector==='[type="submit"]')return submit;if(selector.startsWith('[name="'))return fields.get(selector.match(/name="([^"]+)"/)[1])||null;return null;},
    closest(selector){if(selector==='dialog')return null;return main;},
    prepend(note){this.notes.unshift(note);},matches(selector){return selector==='[data-flow-form]';}};
  for(const [name,el] of fields){form[name]=el;el.form=form;}
  return form;
}
function mount(html){if(currentForm){currentForm.isConnected=false;currentForm.submit.isConnected=false;}for(const node of pollNodes)node.isConnected=false;currentHTML=html;currentForm=formFromHTML(html);main.forms=[currentForm];context.restoreDrafts(main);context.flowRestore(main);return currentForm;}
Object.defineProperty(main,'innerHTML',{get(){return currentHTML;},set(html){mount(html);}});
function $(selector,parent){
  if(parent?.querySelector)return parent.querySelector(selector);
  if(selector==='#flow-home-form')return currentForm;
  if(selector==='#flow-home-text')return currentForm?.text;
  if(selector==='#main')return main;
  if(selector==='#edit-content')return editRoot;
  if(selector==='[data-flow-recent]')return recentDisplay;
  return null;
}
function $$(selector,parent){if(parent?.querySelectorAll)return parent.querySelectorAll(selector);if(selector.includes('form')||selector.includes('data-flow-form'))return currentForm?[currentForm]:[];return [];}
const context={state,console,URLSearchParams,Map,WeakMap,Set,Number,String,Boolean,Date,JSON,Promise,
  document:{addEventListener(type,handler){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(handler);},getElementById(){return main;},querySelector(){return null;},
    createElement(){return {className:'',dataset:{},setAttribute(){},append(){},remove(){},textContent:'',firstElementChild:{}};}},
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  MutationObserver:class{observe(){}},setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},
  $, $$,e:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  icon:()=>'',formatDate:value=>String(value),clock:value=>String(value),recordStatus:record=>record.status,
  newVisitRequestKey:()=>`synthetic-key-${++serial}`,notify:(message,error=false)=>notifications.push({message,error}),
  discussionDraftValues:(form,values)=>values,clearAudioRecoveries(){},
  async api(url,options={}){calls.push({url,options});if(readGate?.url===url){const gate=readGate;readGate=null;gate.enter();return await gate.promise;}if(url==='/api/secretary/turns'){if(options.method==='POST')return {turn:{id:99,record_id:990,text:options.body.text,status:'queued',matter:matters[0],matter_id:options.body.matter_id}};return {items:turns};}if(url==='/api/secretary/plans')return {items:plans};
    if(url.startsWith('/api/matters/')){const matter=matters.find(item=>item.id===Number(url.split('/').pop()));if(!matter)throw Object.assign(new Error('missing synthetic matter'),{status:404});return {matter};}
    if(url.startsWith('/api/secretary/plans/')){if(planGate){const wait=planGate;planGate=null;return await wait;};const plan=plans.find(p=>p.id===Number(url.split('/').pop()));if(!plan)throw Object.assign(new Error('missing synthetic plan'),{status:404});return {plan};}
    if(url.startsWith('/api/secretary/turns/')){const turn=turns.find(t=>t.id===Number(url.split('/').pop()));if(!turn)throw Object.assign(new Error('missing synthetic turn'),{status:404});return {turn};}
    throw new Error('Unexpected production request: '+url);},
  async loadView(){refreshes++;mount(await context.loadSecretaryHome(records));},
  flowAttachmentPicker:()=>'<input name="attachment_items" type="hidden" value="[]"><input name="attachment_attempts" type="hidden" value="{}">',
  flowAttachmentItems:form=>JSON.parse(form.attachment_items.value||'[]'),flowRefreshSelectedFiles(){},
  openDialog(kind,html){if(kind==='edit'){if(editForm)editForm.isConnected=false;editForm=formFromHTML(html);editForm.parentElement=editRoot;editRoot.forms=[editForm];context.restoreDrafts(editRoot);}},closeDialog(){},dialogHeader:()=>'',openSecretaryCapture(){},openRecord(){},
};
vm.createContext(context);
const app=fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8');
const start=app.indexOf("const draftPrefix='secretary-form-v2:';"),end=app.indexOf('function closeDialog(',start);
assert(start>=0&&end>start,'production draft persistence functions must be present');vm.runInContext(app.slice(start,end),context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/secretary-flow.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/secretary-history.js'),'utf8'),context);
function evaluate(source){return vm.runInContext(source,context);}
function stored(key){return JSON.parse(storage.get('secretary-form-v2:'+key)||'null');}
function typeForm(form,text,{files=[],request=''}={}){form.text.value=text;form.attachment_items.value=JSON.stringify(files);form.request_id.value=request;form.request_text.value=request?'synthetic request signature':'';context.saveDraft(form);return form;}
function type(text,options={}){return typeForm(currentForm,text,options);}
async function clickButton(dataset,conversationRoot=null){const button={dataset,closest:selector=>selector==='form'?currentForm:selector==='.flow-conversation,.flow-home'?conversationRoot:null};for(const handler of listeners.get('click')||[])await handler({target:{closest:selector=>selector==='button'||(selector.includes('data-flow-home')&&Object.keys(dataset).some(k=>k.startsWith('flowHome')))?button:null},preventDefault(){},stopImmediatePropagation(){}});}
async function clickSeparate(){return await clickButton({flowSeparate:''});}
function gateRead(url){let release,enter;const gate={url,promise:new Promise(resolve=>release=resolve),started:new Promise(resolve=>enter=resolve),enter:()=>enter(),release:value=>release(value)};readGate=gate;return gate;}
async function run(){
  if(scenario==='recent_closed_grouped'){
    const html=await context.loadSecretaryHome(records);const tag=html.match(/<details[^>]*data-flow-recent[^>]*>/)?.[0];assert(tag,'recent records must be available');assert(!/\sopen(?:\s|>)/.test(tag),'recent records are collapsed by default');
    assert(html.indexOf('data-flow-composer-slot')<html.indexOf('data-flow-recent'),'history must follow the composer');
    assert.equal((html.match(/data-flow-home-plan="1"/g)||[]).length,1,'a plan with multiple utterances must have one history entry');
    assert(html.includes('synthetic thought')&&html.includes('synthetic imported record'),'non-plan and imported records must remain reachable');
    const current=html.slice(html.indexOf('data-flow-results'),html.indexOf('data-flow-recent'));assert(!current.includes('data-flow-turn='),'old unrelated turns must not be expanded as current discussion');return;
  }
  if(scenario==='selected_only_current'){
    context.writeDraft('secretary-form-v2:flow-selected-plan',{plan_id:1});const html=await context.loadSecretaryHome(records);
    const current=html.slice(html.indexOf('data-flow-results'),html.indexOf('data-flow-recent'));assert(current.includes('data-flow-turn="11"')&&current.includes('data-flow-turn="12"'));assert(!current.includes('data-flow-turn="20"'));
    assert(!html.includes('data-flow-home-plan="1"'),'the expanded current plan must not also occupy the history list');assert(html.includes('data-flow-home-plan="2"'));return;
  }
  if(scenario==='separate_preserves_plan_draft'){
    await context.loadView();await context.flowSelectHomePlan(1);const old=type('unsent first-plan supplement',{files:[{material_id:901,title:'synthetic attachment'}],request:'synthetic-retry'}),oldKey=context.draftKey(old);
    await clickSeparate();assert.equal(currentForm.dataset.planId,'0');assert.equal(currentForm.text.value,'');assert.equal(currentForm.request_id.value,'');assert.equal(currentForm.attachment_items.value,'[]');assert.notEqual(currentForm.dataset.id,old.dataset.id);
    assert.equal(context.readDraft(oldKey).values.text,'unsent first-plan supplement','the old plan draft must stay at its original key');assert.equal(context.readDraft(oldKey).values.attachment_items,old.attachment_items.value);
    assert.equal(stored('flow-selected-plan'),null);assert(currentHTML.includes('data-flow-home-plan="1"'),'the former current plan must now be reachable in recent records');
    await context.flowSelectHomePlan(2);assert.equal(currentForm.text.value,'','a different plan must not inherit the first plan draft');
    await context.flowSelectHomePlan(1);assert.equal(currentForm.text.value,'unsent first-plan supplement');assert.equal(currentForm.request_id.value,'synthetic-retry');assert.equal(currentForm.attachment_items.value,old.attachment_items.value);return;
  }
  if(scenario==='separate_preserves_unplanned_draft'){
    await context.loadView();const old=type('unsent independent thought',{files:[{material_id:902}],request:'synthetic-unsent'}),oldId=old.dataset.id;
    await clickSeparate();assert.equal(currentForm.text.value,'');assert.equal(currentForm.attachment_items.value,'[]');assert.notEqual(currentForm.dataset.id,oldId);assert(currentHTML.includes('data-flow-home-draft="'+oldId+'"'));
    await context.flowSelectHomeDraft(oldId);assert.equal(currentForm.text.value,'unsent independent thought');assert.equal(currentForm.request_id.value,'synthetic-unsent');assert.equal(currentForm.attachment_items.value,'[{"material_id":902}]');
    await context.loadView();assert.equal(currentForm.text.value,'unsent independent thought','an ordinary refresh must keep the selected scratch draft');return;
  }
  if(scenario==='separate_busy_upload'||scenario==='separate_busy_save'){
    await context.loadView();const form=type('synthetic input still being saved'),oldRefresh=refreshes;form.dataset[scenario.endsWith('upload')?'uploading':'saving']='yes';
    await clickSeparate();assert.equal(currentForm,form);assert.equal(currentForm.text.value,'synthetic input still being saved');assert.equal(refreshes,oldRefresh);assert(notifications.some(n=>n.message.includes('完成后再切换')));return;
  }
  if(scenario==='late_plan_cannot_recapture'){
    await context.loadView();const previous=currentForm;context.flowHistorySavedTurn({id:77,record_id:777,plan_id:null,status:'queued'},previous);assert.equal(evaluate('flowHomeActiveTurn'),77);
    await clickSeparate();assert.equal(evaluate('flowHomeActiveTurn'),0);
    assert.equal(context.flowHomeRecognizePlan({id:77,plan:plans[0]},currentForm),false,'a folded pending utterance cannot recapture the new composer');assert.equal(evaluate('flowHomePlan'),null);assert.equal(stored('flow-selected-plan'),null);
    context.flowHistorySavedTurn({id:78,record_id:778,plan_id:null,status:'queued'},currentForm);assert.equal(context.flowHomeRecognizePlan({id:78,plan:plans[1]},currentForm),true);assert.equal(stored('flow-selected-plan').plan_id,2);return;
  }
  if(scenario==='nonplan_turn_receipt'){
    await context.loadView();assert(currentHTML.includes('data-flow-home-turn="20"'));
    await clickButton({flowHomeTurn:'20'});
    const current=currentHTML.slice(currentHTML.indexOf('data-flow-results'),currentHTML.indexOf('data-flow-recent'));
    assert(current.includes('synthetic unrelated thought')&&current.includes('saved research receipt'),'reopening must show both the original utterance and its secretary reply');
    assert(current.includes('data-flow-research="20"'),'the research action must remain usable on replay');
    assert(currentHTML.includes('补充 / 处理原记录')&&currentHTML.includes('data-record="200"'));
    assert.equal(currentForm.dataset.planId,'0','a plain note must not masquerade as a plan');
    assert(!currentHTML.includes('data-flow-home-turn="20"'),'current note is removed from folded list');
    await clickSeparate();assert(currentHTML.includes('data-flow-home-turn="20"'));assert(!currentHTML.slice(currentHTML.indexOf('data-flow-results'),currentHTML.indexOf('data-flow-recent')).includes('data-flow-turn="20"'));return;
  }
  if(scenario==='nonplan_matter_continuation'){
    turns.find(turn=>turn.id===20).matter=matters[0];await context.loadView();await context.flowSelectHomeTurn(20);
    assert.equal(currentForm.dataset.planId,'0');assert.equal(currentForm.dataset.matterId,'7');assert.equal(currentForm.dataset.matterRevision,'3');assert.equal(currentForm.dataset.customerId,'9');assert.equal(currentForm.dataset.projectId,'8');assert.equal(currentForm.dataset.matterMode,'auto');
    assert(currentHTML.includes('正在补充：synthetic continuing goal'));assert(currentHTML.includes('继续补充这件事'));assert(!currentHTML.includes('<strong>新的一件事 · 随口告诉秘书</strong>'));assert(currentHTML.includes('saved research receipt'));return;
  }
  if(scenario==='nonplan_matter_payload'){
    turns.find(turn=>turn.id===20).matter=matters[0];await context.loadView();await context.flowSelectHomeTurn(20);const form=type('synthetic continued progress',{files:[{material_id:910}]});form.isConnected=false;
    await context.submitSecretaryFlow(form,{preventDefault(){}});const post=calls.find(call=>call.url==='/api/secretary/turns'&&call.options.method==='POST');assert(post);assert.equal(post.options.body.matter_id,7);assert.equal(post.options.body.matter_revision,3);assert.equal(post.options.body.matter_mode,'auto');assert.equal(post.options.body.customer_id,9);assert.equal(post.options.body.opportunity_id,8);assert.deepEqual(JSON.parse(JSON.stringify(post.options.body.material_ids)),[910]);assert(!post.options.body.plan_id);return;
  }
  if(scenario==='matter_new_boundary_preserves_draft'){
    turns.find(turn=>turn.id===20).matter=matters[0];await context.loadView();await context.flowSelectHomeTurn(20);const old=type('synthetic unsent matter supplement',{files:[{material_id:911}],request:'synthetic-continuation-request'}),oldId=old.dataset.id;
    await clickSeparate();assert.equal(currentForm.dataset.matterId,undefined);assert.equal(currentForm.dataset.matterMode,'fresh');assert.equal(currentForm.text.value,'');assert.equal(currentForm.attachment_items.value,'[]');assert(currentHTML.includes('data-flow-home-turn="20"'));assert(!currentHTML.slice(currentHTML.indexOf('data-flow-results'),currentHTML.indexOf('data-flow-recent')).includes('data-flow-turn="20"'));
    const scratch=context.flowScratchHistory().find(item=>item.id===oldId);assert.equal(scratch.matter.id,7);assert.equal(scratch.values.text,'synthetic unsent matter supplement');assert.equal(scratch.values.attachment_items,'[{"material_id":911}]');
    matters[0].revision=6;await context.flowSelectHomeDraft(oldId);assert.equal(currentForm.dataset.matterId,'7');assert.equal(currentForm.dataset.matterRevision,'6');assert.equal(currentForm.dataset.matterMode,'auto');assert.equal(currentForm.text.value,'synthetic unsent matter supplement');assert.equal(currentForm.attachment_items.value,'[{"material_id":911}]');assert.equal(currentForm.request_id.value,'synthetic-continuation-request');
    await context.loadView();assert.equal(currentForm.dataset.matterId,'7');assert.equal(currentForm.text.value,'synthetic unsent matter supplement');return;
  }
  if(scenario==='fresh_boundary_survives_refresh'){
    await context.loadView();await context.flowStartNew(currentForm);type('synthetic fresh independent goal',{files:[{material_id:912}]});assert.equal(currentForm.dataset.matterMode,'fresh');await context.loadView();assert.equal(currentForm.dataset.matterMode,'fresh');assert.equal(currentForm.dataset.matterId,undefined);assert.equal(currentForm.text.value,'synthetic fresh independent goal');assert.equal(currentForm.attachment_items.value,'[{"material_id":912}]');return;
  }
  if(scenario==='closed_matter_not_silently_new'){
    turns.find(turn=>turn.id===20).matter={...matters[0],status:'ended'};await context.loadView();await context.flowSelectHomeTurn(20);assert.equal(currentForm.dataset.matterId,'7');assert(currentHTML.includes('这件事已结束'));assert(currentHTML.includes('<button type="submit" disabled'));assert(currentHTML.includes('data-matter-open="7"'));assert(!currentHTML.includes('<strong>新的一件事 · 随口告诉秘书</strong>'));return;
  }
  if(scenario==='matter_draft_read_cannot_recapture_new'){
    turns.find(turn=>turn.id===20).matter=matters[0];await context.loadView();await context.flowSelectHomeTurn(20);const oldId=type('synthetic stored matter draft',{files:[{material_id:913}]}).dataset.id;await context.flowStartNew(currentForm);
    const gate=gateRead('/api/matters/7'),opening=context.flowSelectHomeDraft(oldId);await gate.started;await context.flowStartNew(currentForm);const fresh=currentForm;type('synthetic new goal during matter read');gate.release({matter:matters[0]});await opening;
    assert.equal(currentForm,fresh);assert.equal(currentForm.dataset.matterId,undefined);assert.equal(currentForm.dataset.matterMode,'fresh');assert.equal(currentForm.text.value,'synthetic new goal during matter read');return;
  }
  if(scenario==='matter_context_title_escaped'){
    const html=context.flowHomeComposerHTML(null,[{matter:{...matters[0],title:'<img src=x onerror=attack()> "quoted"'}}]);assert(!html.includes('<img '));assert(html.includes('&lt;img'));assert(html.includes('data-matter-id="7"'));return;
  }
  if(scenario==='missing_plan_fallback'){
    const absent=plans.splice(0,1)[0];turns[0].plan=absent;turns[1].plan=absent;
    const html=await context.loadSecretaryHome(records);
    assert.equal((html.match(/data-flow-home-plan="1"/g)||[]).length,1,'turns belonging to a plan beyond the plans window must have one reachable grouped entry');
    assert(html.includes('synthetic first plan'));return;
  }
  if(scenario==='real_poll_after_new'){
    await context.loadView();const source=currentForm;context.flowHistorySavedTurn({id:77,record_id:777,status:'queued'},source);
    const node={dataset:{flowStatus:'queued'},isConnected:true,replaced:false,closest:selector=>selector==='dialog'?null:main,replaceWith(){this.replaced=true;}};pollNodes.push(node);
    const gate=gateRead('/api/secretary/turns/77');context.flowPoll(77,node);const polling=timers.pop()();await gate.started;
    await clickSeparate();const fresh=currentForm;type('synthetic new input during old interpretation');
    gate.release({turn:{id:77,record_id:777,plan_id:1,plan:plans[0],text:'old synthetic input',reply:'late synthetic reply',status:'done',result:{}}});await polling;
    assert.equal(node.replaced,false,'the production poll must not update a detached old conversation');assert.equal(currentForm,fresh);assert.equal(currentForm.text.value,'synthetic new input during old interpretation');assert.equal(currentForm.dataset.planId,'0');assert.equal(stored('flow-selected-plan'),null);return;
  }
  if(scenario==='new_input_during_slow_refresh'){
    await context.loadView();await context.flowSelectHomePlan(1);const old=currentForm,gate=gateRead('/api/secretary/turns'),switching=context.flowStartNew(old);await gate.started;
    assert.notEqual(currentForm,old,'new input should appear immediately, before the network refresh');assert.equal(currentForm.dataset.planId,'0');assert.equal(currentForm.text.value,'');
    type('synthetic new thought typed during refresh',{files:[{material_id:903}]});gate.release({items:turns});await switching;
    assert.equal(currentForm.text.value,'synthetic new thought typed during refresh');assert.equal(currentForm.attachment_items.value,'[{"material_id":903}]');assert.equal(currentForm.dataset.planId,'0');
    assert(!calls.some(call=>call.options.method&&call.options.method!=='GET'),'folding a discussion is presentation state and must not archive or cancel server records');return;
  }
  if(scenario==='nonhome_separate_fresh'){
    context.openSecretaryCapture();const old=typeForm(editForm,'synthetic popup thought',{files:[{material_id:904}],request:'synthetic-popup-retry'}),oldKey=context.draftKey(old);
    await context.flowStartNew(old);assert.notEqual(editForm.dataset.id,old.dataset.id,'new popup must have an independent composer key');assert.equal(editForm.text.value,'');assert.equal(editForm.attachment_items.value,'[]');assert.equal(editForm.request_id.value,'');
    const entries=context.flowScratchHistory();assert.equal(entries.length,1);assert(entries[0].id.startsWith('home-'));assert.equal(entries[0].values.text,'synthetic popup thought');assert.equal(context.readDraft(oldKey).values.text,'synthetic popup thought');
    await context.flowSelectHomeDraft(entries[0].id);assert.equal(currentForm.text.value,'synthetic popup thought');assert.equal(currentForm.attachment_items.value,'[{"material_id":904}]');assert.equal(currentForm.request_id.value,'synthetic-popup-retry');return;
  }
  if(scenario==='nonhome_repeated_source'){
    context.openSecretaryCapture();const old=typeForm(editForm,'synthetic popup repeatable thought');await context.flowStartNew(old);const entryId=context.flowScratchHistory()[0].id;
    context.openSecretaryCapture();assert.equal(editForm.text.value,'synthetic popup repeatable thought','original popup source has its recoverable independent draft');const reopened=editForm;await context.flowStartNew(reopened);
    const entries=context.flowScratchHistory();assert.equal(entries.length,1,'folding the same popup source again must update its existing scratch entry');assert.equal(entries[0].id,entryId);assert.equal(editForm.text.value,'');return;
  }
  if(scenario==='nonhome_closed_draft_recovery'){
    await context.loadView();context.openSecretaryCapture({},true);const popup=typeForm(editForm,'synthetic popup closed before sending',{files:[{material_id:905}]});
    const dialog={matches:selector=>selector==='dialog',querySelectorAll:selector=>selector==='[data-flow-form]'?[popup]:[]};for(const handler of listeners.get('close')||[])handler({target:dialog});
    const entries=context.flowScratchHistory();assert.equal(entries.length,1);assert.equal(entries[0].values.text,'synthetic popup closed before sending');assert(recentDisplay.outerHTML.includes('data-flow-home-draft="'+entries[0].id+'"'),'closing must immediately expose its unsent draft in recent records');
    await context.flowSelectHomeDraft(entries[0].id);assert.equal(currentForm.text.value,'synthetic popup closed before sending');assert.equal(currentForm.attachment_items.value,'[{"material_id":905}]');return;
  }
  if(scenario==='flow_new_in_conversation'){
    context.openSecretaryCapture();const old=typeForm(editForm,'synthetic conversation draft before new',{files:[{material_id:907}]}),key=context.draftKey(old);
    await clickButton({flowNew:''},editRoot);assert.notEqual(editForm.dataset.id,old.dataset.id);assert.equal(editForm.text.value,'');assert.equal(editForm.attachment_items.value,'[]');
    assert.equal(context.readDraft(key).values.text,'synthetic conversation draft before new');assert.equal(context.flowScratchHistory().length,1);assert.equal(context.flowScratchHistory()[0].values.attachment_items,'[{"material_id":907}]');return;
  }
  if(scenario==='flow_new_scoped_shortcut'){
    await context.loadView();type('synthetic unrelated home draft');await clickButton({flowNew:'',customerId:'9',contactId:'7',projectId:'8'});
    assert.equal(editForm.dataset.customerId,'9');assert.equal(editForm.dataset.contactId,'7');assert.equal(editForm.dataset.projectId,'8');assert.equal(editForm.dataset.planId,'0');assert.equal(editForm.text.value,'');
    assert.equal(currentForm.text.value,'synthetic unrelated home draft','an external client/project shortcut must not clear the home conversation');return;
  }
  if(['popup_sent_draft_retired','popup_next_draft_preserved','home_sent_draft_retired','home_next_draft_preserved'].includes(scenario)){
    const home=scenario.startsWith('home_');if(home)await context.loadView();else context.openSecretaryCapture({},true);
    const form=typeForm(home?currentForm:editForm,'synthetic sent utterance A',{request:'synthetic-request-A'});context.flowSaveScratch(form);assert.equal(context.flowScratchHistory().length,1);
    // submitSecretaryFlow clears exactly the submitted values; a newer B is
    // intentionally kept by the existing production save completion branch.
    const newer=scenario.endsWith('preserved');if(newer)typeForm(form,'synthetic next utterance B',{files:[{material_id:906}],request:'synthetic-request-A'});
    else{form.text.value='';form.request_id.value='';form.request_text.value='';form.attachment_items.value='[]';form.attachment_attempts.value='{}';context.clearDraft(form);}
    context.flowHistorySavedTurn({id:88,record_id:880,text:'synthetic sent utterance A',status:'queued'},form);
    const entries=context.flowScratchHistory();if(newer){assert.equal(entries.length,1);assert.equal(entries[0].values.text,'synthetic next utterance B');assert.equal(entries[0].values.attachment_items,'[{"material_id":906}]');assert(!entries.some(entry=>entry.values.text==='synthetic sent utterance A'),'the receipt must retire only A and keep the new unsent B');assert.equal(context.readDraft(context.draftKey(form)).values.text,'synthetic next utterance B');}
    else{assert.equal(entries.length,0,'a successful receipt must remove its folded unsent draft and prevent duplicate sending');assert.equal(context.readDraft(context.draftKey(form)),undefined);}
    return;
  }
  if(scenario==='select_plan_navigation'||scenario==='select_turn_relogin'){
    await context.loadView();const original=currentForm,url=scenario.startsWith('select_plan')?'/api/secretary/plans/1':'/api/secretary/turns/20',gate=gateRead(url);
    const selecting=scenario.startsWith('select_plan')?context.flowSelectHomePlan(1):context.flowSelectHomeTurn(20);await gate.started;
    if(scenario.endsWith('navigation')){state.view='records';state.discussionNavigationGeneration++;}else{state.csrf='another-synthetic-session';}
    gate.release(scenario.startsWith('select_plan')?{plan:plans[0]}:{turn:turns.find(t=>t.id===20)});await selecting;
    assert.equal(currentForm,original);assert.equal(stored('flow-selected-plan'),null);assert.equal(stored('flow-selected-turn'),null,'a stale read must not restore the old selection');return;
  }
  if(scenario==='history_navigation'||scenario==='history_relogin'){
    const gate=gateRead('/api/secretary/turns'),reading=context.loadSecretaryHome(records);await gate.started;
    if(scenario.endsWith('navigation')){state.view='records';state.discussionNavigationGeneration++;}else state.csrf='another-synthetic-session';
    gate.release({items:turns});assert.equal(await reading,'','stale history responses must not render into a different page/session');assert.equal(stored('flow-selected-plan'),null);assert.equal(stored('flow-selected-turn'),null);return;
  }
  if(scenario==='stale_plan_selection'){
    await context.loadView();let release;planGate=new Promise(resolve=>release=resolve);const pending=context.flowSelectHomePlan(1);await Promise.resolve();await context.flowStartNew(currentForm);release({plan:plans[0]});await pending;
    assert.equal(currentForm.dataset.planId,'0');assert.equal(evaluate('flowHomePlan'),null);assert.equal(stored('flow-selected-plan'),null,'an old plan read may not restore a selection after new input intent');return;
  }
  if(scenario==='escape_history'){
    records.push({id:999,title:'<script>synthetic</script>',content:'<img src=x onerror=synthetic()>',status:'unfiled',created_at:999});
    const html=await context.loadSecretaryHome(records);assert(html.includes('&lt;script&gt;synthetic&lt;/script&gt;'));assert(!html.includes('<script>synthetic</script>'));assert(!html.includes('<img src=x'));return;
  }
  assert.fail('unknown production-history scenario '+scenario);
}
run().then(()=>process.stdout.write(JSON.stringify({passed:scenario}))).catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason='Node.js is required for the home history regression harness')
@pytest.mark.parametrize('scenario', [
    'recent_closed_grouped', 'selected_only_current', 'separate_preserves_plan_draft',
    'separate_preserves_unplanned_draft', 'separate_busy_upload', 'separate_busy_save',
    'late_plan_cannot_recapture', 'stale_plan_selection', 'escape_history',
    'nonplan_turn_receipt', 'missing_plan_fallback', 'real_poll_after_new',
    'nonplan_matter_continuation', 'nonplan_matter_payload', 'matter_new_boundary_preserves_draft',
    'fresh_boundary_survives_refresh', 'closed_matter_not_silently_new',
    'matter_draft_read_cannot_recapture_new', 'matter_context_title_escaped',
    'select_plan_navigation', 'select_turn_relogin', 'history_navigation', 'history_relogin',
    'new_input_during_slow_refresh',
    'nonhome_separate_fresh', 'nonhome_repeated_source', 'nonhome_closed_draft_recovery',
    'popup_sent_draft_retired', 'popup_next_draft_preserved', 'home_sent_draft_retired', 'home_next_draft_preserved',
    'flow_new_in_conversation', 'flow_new_scoped_shortcut',
])
def test_recent_history_browser_state(scenario):
    result = subprocess.run([NODE, '-e', HARNESS, str(ROOT), scenario], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"passed":' in result.stdout and scenario in result.stdout
