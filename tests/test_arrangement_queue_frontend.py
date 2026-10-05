"""Delivered arrangement functions run in Node VM with no real customer data."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.argv[1],scenario=process.argv[2],listeners=new Map(),calls=[],responses=[],dialogs=[],notices=[];
const NOW=Date.parse('2026-10-05T10:00:00+08:00')/1000;
class FixedDate extends Date{static now(){return NOW*1000;}}
const state={authenticated:true,csrf:'fictional-session',view:'agenda',agendaMode:'arrangements',dialogIntentGeneration:0,detail:null};
let request=0,loads=0,clears=0,saves=0,currentRoot=null;
const context={state,Date:FixedDate,Math,Number,String,Boolean,JSON,Map,Set,Object,Array,Promise,URLSearchParams,Intl,
 document:{addEventListener:(name,fn,capture)=>{if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push({fn,capture});},createElement:()=>({set innerHTML(value){this.firstElementChild={innerHTML:value};}})},
 icon:()=>'<svg></svg>',formatDate:at=>`TIME-${at}`,dateValue:at=>new Date(at*1000).toISOString().slice(0,16),
 dialogHeader:(title,subtitle,kind)=>`<h2>${title}</h2><p>${subtitle}</p>`,heading:(title,subtitle,extra)=>`<h1>${title}</h1><p>${subtitle}</p>${extra}`,
 flowComposer:(plan,scope)=>`<form data-flow-form data-id="plan-${plan.id}" data-plan-id="${plan.id}" data-plan-revision="${plan.revision}"><div class="flow-current"><strong>旧标题</strong></div><textarea name="text"></textarea><input name="attachment_items"><button type="submit">告诉秘书</button></form>`,
 flowTurnHTML:turn=>`<article data-flow-turn="${turn.id}">${turn.text}</article>`,flowRestore:()=>{},restoreDrafts:()=>{},
 newVisitRequestKey:()=>`fictional-request-${++request}`,formValues:form=>({...form.values}),saveDraft:()=>{saves++;},clearDraft:()=>{clears++;},saveVisibleDrafts:()=>{},
 $:(selector,node)=>{if(selector==='[data-arrangement-detail]')return currentRoot;if(node?.nodes?.has(selector))return node.nodes.get(selector);if(selector==='.form-error')return node?.error;if(selector==='button[type="submit"]')return node?.submit;if(selector==='[name="request_id"]')return node?.requestId;if(selector==='[name="request_text"]')return node?.requestText;if(selector==='[data-flow-form]')return node?.composer;if(selector==='[data-arrangement-read-state]')return node?.readLabel;return null;},
 $$:(selector,node)=>node?.inputs||[],
 openDialog:(kind,html)=>{state.dialogIntentGeneration++;dialogs.push({kind,html});},closeDialog:()=>{state.dialogIntentGeneration++;},
 notify:(text,error)=>notices.push({text,error}),loadView:async()=>{loads++;},navigate:async view=>{state.view=view;loads++;},
 api:async(url,options={})=>{calls.push({url,options});assert(responses.length,'unexpected request '+url);const response=responses.shift();if(response instanceof Error)throw response;return typeof response==='function'?await response():response;}
};
vm.createContext(context);
const app=fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8');
for(const line of app.split('\n').filter(line=>line.startsWith('const escapeHTML =')||line.startsWith('const e =')))vm.runInContext(line,context);
const flow=fs.readFileSync(path.join(root,'secretary/static/secretary-flow.js'),'utf8');
vm.runInContext(flow.slice(flow.indexOf('function flowIdentityHTML('),flow.indexOf('function flowTurnHTML(')),context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/arrangement-queue.js'),'utf8'),context);
function plain(value){return JSON.parse(JSON.stringify(value));}
function item(extra={}){return {plan_id:1,revision:3,title:'虚构拜访<方案>',settling_state:'pending',customer_id:7,opportunity_id:8,
 settle_deadline:{strength:'required',time_spec:{precision:'date',date:'2026-10-11'}},
 next_check:{id:'check-1',status:'planned',action:'询问李总',origin:'user',time_spec:{precision:'window',date:'2026-10-09',window:'上午',start_at:NOW+4*86400,end_at:NOW+4*86400+3600}},
 proposed_execution:{time_spec:{precision:'instant',date:'2026-11-05',at:NOW+31*86400}},blocking_reasons:['waiting_for_agreement'],followup_enabled:true,...extra};}
function plan(extra={}){return {id:1,revision:3,title:'虚构活动',status:'preparing',record_id:20,turns:[],arrangement:item(),...extra};}
function form(operation,values={},extra={}){const result={values:{request_id:'',request_text:'',...values},error:{textContent:''},submit:{disabled:false,isConnected:true},isConnected:true,nodes:new Map(),closest:()=>({open:true}),...extra,dataset:{arrangementForm:operation,planId:'1',planRevision:'3',revision:'3',...extra.dataset}};result.requestId={get value(){return result.values.request_id;},set value(value){result.values.request_id=value;}};result.requestText={get value(){return result.values.request_text;},set value(value){result.values.request_text=value;}};return result;}
async function run(){
 if(scenario==='date_and_window_never_invent_clock'){
  let html=context.arrangementTimeHTML({precision:'date',date:'2026-10-08',start_at:NOW});assert.equal(html,'2026-10-08');assert(!html.includes('TIME-'));
  html=context.arrangementTimeHTML({precision:'window',date:'2026-10-09',window:'上午',start_at:NOW,end_at:NOW+3600});assert.equal(html,'2026-10-09 · 上午');assert(!html.includes('09:00'));
  html=context.arrangementTimeHTML({precision:'window',window:'calendar',date:'2026-11-01',end_date:'2026-12-01',raw_text:'下月',start_at:NOW,end_at:NOW+30*86400});assert.equal(html,'下月 · 2026-11-01 — 2026-11-30 · 具体日期待选');assert(!html.includes('TIME-'));assert(!html.includes('时段待核对'));
 }
 else if(scenario==='three_times_and_old_schedule_coexist'){
  const html=context.arrangementSnapshotHTML(item({active_schedule:{id:30,revision:4,remind_at:NOW+3*86400},attention_flags:['deadline_overdue','check_after_deadline']}));
  assert(html.includes('最晚确定：2026-10-11'));assert(html.includes('2026-10-09 · 上午'));assert(html.includes(`当前有效安排`));assert(html.includes(`TIME-${NOW+3*86400}`));assert(html.includes('拟改安排：TIME-'));assert(html.includes('原日程仍生效'));assert(html.includes('最晚确定期限已过'));assert(html.includes('两项选择均已保留'));
  assert(context.arrangementSnapshotHTML(item({settle_deadline:{strength:'target',time_spec:{precision:'date',date:'2026-10-11'}},attention_flags:['deadline_overdue']})).includes('超过希望确定期限'));
 }
 else if(scenario==='date_settled_does_not_claim_timed_schedule'){
  const html=context.arrangementDetailHTML(plan({arrangement:item({settling_state:'settled',settlement_scope:'date_only',active_schedule:null})}));assert(html.includes('日期已定，钟点待定；不占具体时段'));assert(html.includes('data-arrangement-operation="continue_set_time"'));assert(!html.includes('当前有效安排'));
  const dto=item({settling_state:'settled',settlement_scope:'date_only',proposed_execution:{time_spec:{precision:'instant',date:'2026-10-09',at:NOW+4*86400+8*3600}},blocking_reasons:['coordination_inactive'],followup_enabled:false,followup_disable_reason:'settled'}),snapshot=context.arrangementSnapshotHTML(dto);assert(snapshot.includes('已确定日期'));assert(snapshot.includes('2026-10-09 · 日期已定，钟点待定'));assert(!snapshot.includes('TIME-'));assert(!snapshot.includes('本轮协调已停止'));assert(!snapshot.includes('网页推进提示已停用'));assert(context.arrangementStateHTML(dto).includes('>日期已定<'));
  const full=context.arrangementSnapshotHTML({...dto,settlement_scope:'execution_time',active_schedule:{id:5,remind_at:NOW+86400}});assert(full.includes('TIME-'));assert(full.includes('时间已落实'));assert(!full.includes('本轮协调已停止'));assert(context.arrangementCandidateHTML(dto).includes('data-arrangement-section="candidates" hidden'));
 }
 else if(scenario==='candidate_choice_not_agreement_or_schedule'){
  const html=context.arrangementCandidateHTML(item({selected_candidate_id:'c1',candidates:[{id:'c1',time_spec:{precision:'date',date:'2026-10-08'}},{id:'c2',label:'新方案<引用>',time_spec:{precision:'window',date:'2026-10-09',window:'上午'}}]}));
  assert(html.includes('data-arrangement-select="c2"'));assert(html.includes('不代表对方同意'));assert(html.includes('不会新建日程'));assert(html.includes('新方案&lt;引用&gt;'));
 }
 else if(scenario==='all_default_full_counts_and_filtered_transport'){
  responses.push({items:[item()],counts:{total:231,pending:231,undated:5,today:1},total:231},{items:[],unread:0});
  const html=await context.renderArrangementQueue({customer_id:7,contact_id:9,opportunity_id:8,matter_id:11});const params=new URL('http://test'+calls[0].url).searchParams;assert(html.includes('全部对象的网页提醒，不受当前客户或项目范围限制'));
  assert.equal(params.get('view'),'all');assert.equal(params.get('state'),'pending');assert.equal(params.get('limit'),'20');assert.equal(params.get('offset'),'0');assert.equal(params.get('matter_id'),'11');assert.equal(params.get('contact_id'),'9');assert.equal(params.get('opportunity_id'),'8');assert(html.includes('231 条匹配安排'));assert(html.includes('共 231 条安排'));
 }
 else if(scenario==='undated_visible_and_notice_failure_honest'){
  responses.push({items:[item({settle_deadline:null})],counts:{total:1,undated:1}},{items:[],error:'暂时无法读取'});const html=await context.renderArrangementQueue();assert(html.includes('期限待定'));assert(html.includes('先保留活动，不猜测确定期限'));assert(html.includes('提醒暂未刷新'));assert(html.includes('当前未接入手机推送'));
  const suggestion={time_spec:{precision:'date',date:'2026-10-07'},action:'核对当前安排进展',origin:'suggestion'},pending=item({next_check:null,suggested_next_check:suggestion,followup_enabled:false});
  const check=context.arrangementCheckHTML(pending);assert(check.includes('下一推进点待定'));assert(check.includes('秘书建议：2026-10-07'));assert(check.includes('调整并采用建议'));
  const editor=context.arrangementDecisionHTML(plan({arrangement:pending}),'set_check',{useSuggestedCheck:true});assert(editor.includes('value="2026-10-07"'));assert(!editor.includes('name="followup_enabled" checked'));
  responses.push({plan:plan({arrangement:pending})});await context.handleArrangementAction({dataset:{arrangementSuggestCheck:'1'}});assert.equal(calls.length,3);assert(calls.every(call=>!call.options.method));assert(dialogs.at(-1).html.includes('value="2026-10-07"'));
 }
 else if(scenario==='pagination_clamps_to_last_page_in_scope'){
  responses.push({items:[],counts:{total:20}},{items:[],unread:0},{items:[item()],counts:{total:20}});const html=await context.renderArrangementQueue({offset:20,opportunity_id:8});assert.equal(calls.length,3);assert(calls[2].url.includes('offset=0'));assert(calls[2].url.includes('opportunity_id=8'));assert(html.includes('data-arrangement-card="1"'));
 }
 else if(scenario==='today_month_process_once_refreshes_same_plan_and_server_order'){
  let page='',committed=false,commits=0;
  const before=item({settle_deadline:{strength:'required',time_spec:{precision:'date',date:'2026-10-05'}},next_check:{id:'check-1',status:'planned',action:'询问一次',time_spec:{precision:'instant',date:'2026-10-05',at:NOW-3600}}}),other=item({plan_id:2,title:'虚构另一次活动',settle_deadline:before.settle_deadline,next_check:{id:'check-2',status:'planned',action:'稍后询问',time_spec:{precision:'instant',date:'2026-10-05',at:NOW+3600}}});
  const after={...before,revision:4,last_progress:'已经询问，对方将在下午回复',next_check:{...before.next_check,status:'handled'}};
  context.api=async(url,options={})=>{calls.push({url,options});if(url==='/api/secretary/arrangement-notices')return {items:[],unread:0};if(url.startsWith('/api/secretary/arrangements?')){const params=new URL('http://test'+url).searchParams;assert(['today','month'].includes(params.get('view')));assert.equal(params.get('customer_id'),'7');assert.equal(params.get('opportunity_id'),'8');assert.equal(params.get('matter_id'),'11');return {items:committed?[other,after]:[before,other],total:2,counts:{total:2,today:2,month:2,undated:0}};}assert.equal(url,'/api/secretary/plans/1/arrangement-decisions');assert.equal(options.method,'POST');assert.equal(options.body.operation,'update_progress');assert.equal(options.body.expected_revision,3);assert.equal(options.body.check_id,'check-1');assert.equal(options.body.check_handled,true);assert(options.body.request_id);assert.equal(++commits,1);committed=true;return {plan_id:1,revision:4,receipt:'进展已保存'};};
  context.navigate=async view=>{state.view=view;page=await context.renderArrangementQueue();};context.loadView=async()=>{page=await context.renderArrangementQueue();};
  const click=listeners.get('click').find(listener=>listener.capture).fn,press=async dataset=>{const button={dataset};await click({target:{closest:()=>button},preventDefault(){},stopImmediatePropagation(){}});assert.equal(notices.filter(item=>item.error).length,0);},ids=()=>Array.from(page.matchAll(/data-arrangement-card="(\d+)"/g),match=>Number(match[1]));
  await context.showArrangementQueue({view:'today',customer_id:7,opportunity_id:8,matter_id:11});assert.deepEqual(ids(),[1,2]);assert.equal(page.split('data-arrangement-card="1"').length-1,1);
  await press({arrangementView:'month'});assert.deepEqual(ids(),[1,2]);await press({arrangementView:'today'});assert.deepEqual(ids(),[1,2]);await press({arrangementView:'month'});assert.equal(commits,0);
  const progress=form('update_progress',{progress_text:'已经询问，对方将在下午回复',check_handled:true,check_id:'check-1'});await context.submitArrangementDecision(progress);assert.equal(commits,1);assert.deepEqual(ids(),[2,1]);assert(page.includes('已经询问，对方将在下午回复'));assert(page.includes('data-arrangement-card="1" data-arrangement-revision="4"'));assert(!page.includes('data-arrangement-card="1" data-arrangement-revision="3"'));
  await press({arrangementView:'today'});assert.deepEqual(ids(),[2,1]);assert(page.includes('已经询问，对方将在下午回复'));assert.equal(page.split('data-arrangement-card="1"').length-1,1);
  await press({arrangementView:'month'});assert.deepEqual(ids(),[2,1]);assert(page.includes('data-arrangement-card="1" data-arrangement-revision="4"'));assert.equal(commits,1);assert.equal(calls.filter(call=>call.options.method==='POST').length,1);
 }
 else if(scenario==='today_empty_click_all_keeps_scope_and_undated_plan'){
  let page='';const future=item({title:'虚构未来拜访',settle_deadline:null,next_check:null,proposed_execution:{time_spec:{precision:'date',date:'2026-11-10'}}});
  context.api=async(url,options={})=>{calls.push({url,options});assert(!options.method);if(url==='/api/secretary/arrangement-notices')return {items:[],unread:0};const params=new URL('http://test'+url).searchParams;for(const [key,value] of [['customer_id','7'],['contact_id','9'],['opportunity_id','8'],['matter_id','11']])assert.equal(params.get(key),value);assert.equal(params.get('state'),'pending');assert.equal(params.get('offset'),'0');const view=params.get('view');assert(['today','all'].includes(view));return {items:view==='today'?[]:[future],total:view==='today'?0:1,counts:{total:1,today:0,undated:1}};};
  context.navigate=async view=>{state.view=view;page=await context.renderArrangementQueue();};context.loadView=async()=>{page=await context.renderArrangementQueue();};
  await context.showArrangementQueue({view:'today',customer_id:7,contact_id:9,opportunity_id:8,matter_id:11});assert(page.includes('今天暂无需要推进的安排'));assert(page.includes('1 条匹配安排'));assert(!page.includes('data-arrangement-card='));assert(page.match(/<button[^>]*data-arrangement-queue[^>]*data-arrangement-view="all"[^>]*>查看全部队列<\/button>/));
  const button={dataset:{arrangementQueue:'',arrangementView:'all'}},click=listeners.get('click').find(listener=>listener.capture).fn;await click({target:{closest:()=>button},preventDefault(){},stopImmediatePropagation(){}});assert.equal(notices.length,0);assert(page.includes('data-arrangement-view="all" aria-pressed="true"'));assert.equal(page.split('data-arrangement-card="1"').length-1,1);assert(page.includes('期限待定'));assert(page.includes('虚构未来拜访'));assert(page.includes('1 条匹配安排'));const queues=calls.filter(call=>call.url.startsWith('/api/secretary/arrangements?'));assert.equal(queues.length,2);assert(new URL('http://test'+queues[1].url).searchParams.get('view')==='all');
 }
 else if(scenario==='scope_links_only_use_named_entities_and_escape_labels'){
  const named=item({contact_id:9,customer_name:'虚构公司<一>',contact_name:'王经理"甲"',opportunity_name:'方案 & 试点'}),html=context.arrangementSnapshotHTML(named);for(const kind of ['customer','contact','opportunity'])assert(html.includes(`data-arrangement-scope-kind="${kind}"`));assert(html.includes('虚构公司&lt;一&gt;'));assert(html.includes('王经理&quot;甲&quot;'));assert(html.includes('方案 &amp; 试点'));assert(!html.includes('虚构公司<一>'));
  assert(!context.arrangementSnapshotHTML(item()).includes('data-arrangement-scope-kind='));const unbound=context.arrangementScopeLinksHTML({person:'只记得口述名字'});assert(unbound.includes('只记得口述名字'));assert(!unbound.includes('data-arrangement-scope-id='));
  const matter=context.arrangementMatterSchedulesHTML({id:11,title:'虚构长期目标',plans:[plan()],tasks:[]});assert(matter.includes('data-arrangement-scope-kind="matter"'));assert(matter.includes('data-arrangement-scope-id="11"'));assert(matter.includes('data-arrangement-scope-name="虚构长期目标"'));assert(matter.includes('查看本事项安排'));assert(!context.arrangementMatterSchedulesHTML({id:11,plans:[plan()],tasks:[]}).includes('data-arrangement-scope-kind="matter"'));
 }
 else if(scenario==='scope_clicks_replace_incompatible_ranges_and_keep_names_when_empty'){
  let page='',dialogClosed=0,visibleDraftSaves=0;const named=item({contact_id:9,customer_name:'虚构单位甲',contact_name:'虚构王经理',opportunity_name:'虚构密码试点',settle_deadline:null,next_check:null});
  context.api=async(url,options={})=>{calls.push({url,options});assert(!options.method);if(url==='/api/secretary/arrangement-notices')return {items:[],unread:0};assert(url.startsWith('/api/secretary/arrangements?'));const params=new URL('http://test'+url).searchParams;assert.equal(params.get('state'),'pending');assert.equal(params.get('offset'),'0');assert(!Array.from(params.keys()).some(key=>key.endsWith('_name')));return {items:params.get('view')==='today'?[]:[named],total:params.get('view')==='today'?0:1,counts:{total:1,today:0,undated:1}};};
  context.navigate=async view=>{state.view=view;page=await context.renderArrangementQueue();};context.loadView=async()=>{page=await context.renderArrangementQueue();};context.saveVisibleDrafts=()=>{visibleDraftSaves++;};context.closeDialog=(kind,options)=>{assert.equal(kind,'detail');assert.equal(options.force,true);dialogClosed++;};
  const click=listeners.get('click').find(listener=>listener.capture).fn,press=async(dataset,dialog=null)=>{const button={dataset,closest:selector=>selector==='dialog'?dialog:null};await click({target:{closest:()=>button},preventDefault(){},stopImmediatePropagation(){}});assert.equal(notices.filter(item=>item.error).length,0);},scopeButton=(html,kind)=>{const markup=html.match(new RegExp(`<button[^>]*data-arrangement-scope-kind="${kind}"[^>]*>`))?.[0];assert(markup,'visible scope button missing');return Object.fromEntries(Array.from(markup.matchAll(/data-([a-z-]+)="([^"]*)"/g),match=>[match[1].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()),match[2]]));},params=()=>new URL('http://test'+calls.filter(call=>call.url.startsWith('/api/secretary/arrangements?')).at(-1).url).searchParams;
  await context.showArrangementQueue({view:'today',customer_id:7,contact_id:9,opportunity_id:8,matter_id:11});
  await press(scopeButton(context.arrangementCardHTML(named),'customer'));assert.equal(params().get('customer_id'),'7');for(const key of ['contact_id','opportunity_id','matter_id'])assert(!params().has(key));assert.equal(params().get('view'),'today');assert(page.includes('今天暂无需要推进的安排'));assert(page.includes('客户：虚构单位甲'));assert(page.includes('时间视角切换会保留此范围'));
  await press({arrangementQueue:'',arrangementView:'all'});assert.equal(params().get('view'),'all');assert.equal(params().get('customer_id'),'7');assert(page.includes('客户：虚构单位甲'));assert(page.includes('data-arrangement-card="1"'));
  await press(scopeButton(context.arrangementCardHTML(named),'opportunity'));assert.equal(params().get('customer_id'),'7');assert.equal(params().get('opportunity_id'),'8');assert(!params().has('contact_id'));assert(!params().has('matter_id'));assert(page.includes('项目：虚构密码试点'));
  await press(scopeButton(context.arrangementCardHTML(named),'contact'));assert.equal(params().get('customer_id'),'7');assert.equal(params().get('contact_id'),'9');assert(!params().has('opportunity_id'));assert(!params().has('matter_id'));assert(page.includes('联系人：虚构王经理'));
  const matterHTML=context.arrangementMatterSchedulesHTML({id:11,title:'虚构长期目标',plans:[plan()],tasks:[]});await press(scopeButton(matterHTML,'matter'),{id:'detail-dialog',open:true});assert.equal(params().get('matter_id'),'11');for(const key of ['customer_id','contact_id','opportunity_id'])assert(!params().has(key));assert(page.includes('事项：虚构长期目标'));assert.equal(dialogClosed,1);assert.equal(visibleDraftSaves,1);
  await press({arrangementView:'today'});assert.equal(params().get('matter_id'),'11');assert(page.includes('事项：虚构长期目标'));assert(page.includes('今天暂无需要推进的安排'));await press({arrangementClearScope:''});for(const key of ['customer_id','contact_id','opportunity_id','matter_id'])assert(!params().has(key));assert(!page.includes('class="arrangement-current-scope"'));assert(!page.includes('事项：虚构长期目标'));assert.equal(params().get('view'),'today');
 }
 else if(scenario==='source_hidden_is_read_only'){
  const html=context.arrangementDetailHTML(plan({arrangement:item({source_visible:false,candidates:[{id:'c1',time_spec:{precision:'date',date:'2026-10-08'}}]})}));assert(html.includes('这里仅回看'));assert(!html.includes('data-flow-form'));assert(!html.includes('data-arrangement-select='));assert(!html.includes('data-arrangement-operation='));
 }
 else if(scenario==='visible_pending_disabled_followup_has_explicit_resume'){
  const dto=item({source_visible:true,source_hidden:false,followup_enabled:false,followup_disable_reason:'source_hidden'}),current=plan({arrangement:dto}),controls=context.arrangementControlsHTML(dto),html=context.arrangementDetailHTML(current);
  assert(controls.includes('data-arrangement-operation="resume"'));assert(controls.includes('>恢复推进提示</button>'));assert(html.includes(controls));assert.equal(html.split('data-arrangement-operation="resume"').length-1,1);
  const nodes=new Map([['[data-arrangement-section="controls"]',{innerHTML:'old controls'}]]);currentRoot={dataset:{arrangementDetail:'1'},isConnected:true,nodes,closest:()=>({open:true})};state.detail={type:'arrangement',id:1};responses.push({plan:current});await context.refreshArrangementAfterTurn({plan_id:1});assert.equal(nodes.get('[data-arrangement-section="controls"]').innerHTML,controls);
  responses.push({plan:current});await context.handleArrangementAction({dataset:{arrangementOperation:'resume',planId:'1'}});assert(calls.every(call=>!call.options.method));const editor=dialogs.at(-1).html;assert(editor.includes('name="resume_choice" required'));assert(editor.includes('<option value="">请选择</option>'));assert(editor.includes('明确重新开启网页推进提示'));assert(!editor.includes('name="followup_enabled" checked'));assert.throws(()=>context.arrangementDecisionBody(form('resume',{})),/请选择/);
 }
 else if(scenario==='hidden_pending_disabled_followup_stays_read_only'){
  for(const hidden of [{source_visible:false},{source_hidden:true}]){const dto=item({...hidden,followup_enabled:false,followup_disable_reason:'source_hidden'}),controls=context.arrangementControlsHTML(dto),html=context.arrangementDetailHTML(plan({arrangement:dto}));assert(controls.includes('这里仅回看'));assert(!controls.includes('data-arrangement-operation='));assert(!html.includes('data-arrangement-operation="resume"'));assert(!html.includes('data-flow-form'));}
 }
 else if(scenario==='settled_disabled_followup_has_no_resume_prompt'){
  for(const scope of ['date_only','execution_time']){const dto=item({settling_state:'settled',settlement_scope:scope,source_visible:true,followup_enabled:false,followup_disable_reason:'settled'});for(const html of [context.arrangementControlsHTML(dto),context.arrangementDetailHTML(plan({arrangement:dto}))]){assert(!html.includes('data-arrangement-operation="resume"'));assert(!html.includes('>恢复推进提示</button>'));}}
  assert(!context.arrangementControlsHTML(item({followup_enabled:true})).includes('data-arrangement-operation="resume"'));
 }
 else if(scenario==='open_exact_plan_bypasses_matter_redirect'){
  context.openResolvedMatter=()=>{throw new Error('must not redirect');};responses.push({plan:plan({question:'要先聊什么<目标>？',goal:'虚构合作<目标>',topics:['汇报准备'],preparation:{objective:'秘书准备<建议>'},identity_candidates:[{id:7,field:'customer_id',name:'虚构客户<甲>',customer_id:7}]})});await context.openArrangement(1);assert.equal(calls[0].url,'/api/secretary/plans/1');assert.equal(state.detail.type,'arrangement');assert(dialogs[0].html.includes('data-plan-id="1"'));assert(dialogs[0].html.includes('data-arrangement-composer'));assert(dialogs[0].html.includes('要先聊什么&lt;目标&gt;？'));assert(dialogs[0].html.includes('虚构合作&lt;目标&gt;'));assert(dialogs[0].html.includes('秘书准备&lt;建议&gt;'));assert(dialogs[0].html.includes('data-flow-discussion-plan="1"'));assert(dialogs[0].html.includes('data-flow-identity="7"'));
 }
 else if(scenario==='late_detail_does_not_steal_focus'){
  let resolve;responses.push(()=>new Promise(r=>resolve=r));const waiting=context.openArrangement(1);state.dialogIntentGeneration++;resolve({plan:plan()});await waiting;assert.equal(dialogs.length,0);assert.equal(state.detail,null);
 }
 else if(scenario==='pause_does_not_cancel_schedule'){
  const body=context.arrangementDecisionBody(form('pause',{reason:'下周再推进'},{dataset:{taskRevision:'5'}}));assert.deepEqual(plain(body),{operation:'pause',expected_revision:3,reason:'下周再推进'});const html=context.arrangementDecisionHTML(plan(),'pause');assert(html.includes('当前有效日程及其执行提醒继续保留'));
 }
 else if(scenario==='confirm_candidate_and_external_attestation_strict'){
  const body=context.arrangementDecisionBody(form('confirm_arrangement',{candidate_id:'c1',settlement_scope:'date_only',agreement_attested:true,execution_date:'2026-10-08',execution_precision:'date'},{dataset:{taskRevision:'4'}}));assert.equal(body.candidate_id,'c1');assert.equal(body.expected_task_revision,4);assert.equal(body.settlement_scope,'date_only');assert(!body.proposed_execution);assert.deepEqual(plain(body.agreement_attestation),{status:'reported',scope:'date_only',evidence:'用户在页面明确转述对方已同意本次核对的时段'});
 }
 else if(scenario==='agreement_fields_are_visible_independent_and_not_prechecked'){
  const dto=item({decision_mode:'external',proposed_execution:{time_spec:{precision:'instant',date:'2026-10-09',at:NOW+4*86400},place:'虚构会议室<一>',conditions:['携带脱敏材料','仅讨论技术方案']},agreement_field_signatures:{place:'server-place-signature',conditions:'server-conditions-signature'}}),html=context.arrangementAgreementFieldsHTML(dto);
  assert(html.includes('对方也确认地点：虚构会议室&lt;一&gt;'));assert(html.includes('对方也确认条件：携带脱敏材料；仅讨论技术方案'));assert(html.includes('value="server-place-signature"'));assert(!html.includes(' checked'));assert(html.includes('未勾选的继续待确认'));
  const self=context.arrangementAgreementFieldsHTML({...dto,decision_mode:'self'});assert(self.includes('地点：虚构会议室&lt;一&gt;'));assert(self.includes('条件：'));assert(!self.includes('agreement_place_attested'));
 }
 else if(scenario==='time_attestation_does_not_approve_place_or_conditions'){
  let body=context.arrangementAgreementBody({agreement_attested:true,agreement_place_attested:false,agreement_conditions_attested:false},'date_only');assert.equal(body.scope,'date_only');assert(!Object.hasOwn(body,'fields'));assert(!Object.hasOwn(body,'time'));
  assert.throws(()=>context.arrangementAgreementBody({agreement_place_attested:true},'execution_time'),/先核对本次日期或时段/);
 }
 else if(scenario==='explicit_agreement_uses_current_server_field_signatures'){
  const values={agreement_attested:true,agreement_place_attested:true,agreement_conditions_attested:true,agreement_place_signature:'server-place-signature',agreement_conditions_signature:'server-conditions-signature',agreement_place_value:'虚构会议室',agreement_conditions_value:'["携带脱敏材料","仅讨论技术方案"]',place:'虚构会议室'},body=context.arrangementAgreementBody(values,'date_only');
  assert.equal(body.fields.place.signature,'server-place-signature');assert.equal(body.fields.place.evidence,'用户在页面明确核对地点：虚构会议室');assert.equal(body.fields.conditions.signature,'server-conditions-signature');assert(body.fields.conditions.evidence.includes('携带脱敏材料；仅讨论技术方案'));assert(!body.fields.date);assert(!body.fields.time);assert.equal(calls.length,0);
 }
 else if(scenario==='changed_place_or_candidate_cannot_reuse_old_agreement'){
  const dto=item({decision_mode:'external',proposed_execution:{place:'原会议室',conditions:['仅交流技术']},agreement_field_signatures:{place:'old-place-signature',conditions:'original-conditions-signature'}}),values={agreement_attested:true,agreement_place_attested:true,agreement_place_signature:'old-place-signature',agreement_place_value:'原会议室',place:'新会议室'};
  assert.throws(()=>context.arrangementAgreementBody(values,'execution_time'),/地点已有改动/);
  const html=context.arrangementAgreementFieldsHTML(dto,{place:'新会议室',conditions:['新条件']},values);assert(html.includes('地点：新会议室'));assert(html.includes('条件：新条件'));assert(html.includes('disabled'));assert(!html.includes(' checked'));assert(html.includes('先保存本次修改或选择候选'));assert(html.includes('name="agreement_place_signature" value=""'));assert.equal(calls.length,0);
 }
 else if(scenario==='manual_execution_preserves_duration_and_place'){
  const body=context.arrangementDecisionBody(form('confirm_arrangement',{candidate_id:'',settlement_scope:'execution_time',execution_date:'2026-10-08',execution_precision:'instant',execution_time:'18:00',duration_minutes:'60',place:'会议室'}));assert.equal(body.proposed_execution.duration_minutes,60);assert.equal(body.proposed_execution.place,'会议室');assert.equal(body.proposed_execution.time_spec.at,Date.parse('2026-10-08T18:00:00+08:00')/1000);assert(!body.agreement_attestation);
  for(const duration_minutes of ['4','721','1.5','abc'])assert.throws(()=>context.arrangementDuration({duration_minutes}),/5–720/);assert.equal(context.arrangementDuration({duration_minutes:''}),undefined);assert.equal(context.arrangementDuration({duration_minutes:'720'}),720);assert(context.arrangementDecisionHTML(plan(),'confirm_arrangement').includes('min="5" max="720" step="1"'));
 }
 else if(scenario==='window_input_leaves_policy_boundaries_to_service'){
  const value=context.arrangementTimeFromValues({check_date:'2026-10-09',check_precision:'window',check_window:'上午'},'check');assert.equal(value.window,'上午');assert.equal(value.raw_text,'2026-10-09 上午');assert(!Object.hasOwn(value,'at'));assert(!Object.hasOwn(value,'start_at'));assert(!Object.hasOwn(value,'policy_source'));
  const range=context.arrangementTimeFromValues({execution_date:'2026-10-05',execution_original_date:'2026-10-05',execution_precision:'window',execution_window:'calendar',execution_end_date:'2026-10-12',execution_raw_text:'本周'},'execution');assert.equal(range.window,'calendar');assert.equal(range.end_date,'2026-10-12');assert.equal(range.raw_text,'本周');assert(!Object.hasOwn(range,'at'));assert(!Object.hasOwn(range,'start_at'));assert(context.arrangementTimeFields('execution',range).includes('<option value="calendar" selected>本周 · 日期待选</option>'));
  assert.throws(()=>context.arrangementTimeFromValues({execution_date:'2026-10-12',execution_precision:'window',execution_window:'calendar',execution_end_date:'2026-10-12'},'execution'),/结束日/);
 }
 else if(scenario==='deadline_and_check_clearing_are_separate'){
  let body=context.arrangementDecisionBody(form('set_deadline',{deadline_clear:true}));assert.deepEqual(plain(body),{operation:'set_deadline',expected_revision:3,settle_deadline:null});body=context.arrangementDecisionBody(form('set_check',{check_clear:true,followup_enabled:true}));assert.equal(body.next_check,null);assert.equal(body.followup_enabled,true);assert(!Object.hasOwn(body,'settle_deadline'));
 }
 else if(scenario==='progress_consumes_check_only_when_explicit'){
  let body=context.arrangementDecisionBody(form('update_progress',{progress_text:'还在等',check_handled:false,check_id:'check-1'}));assert(!body.check_handled);assert(!body.check_id);body=context.arrangementDecisionBody(form('update_progress',{progress_text:'问过了，仍没回',check_handled:true,check_id:'check-1'}));assert.equal(body.check_id,'check-1');assert.equal(body.check_handled,true);
 }
 else if(scenario==='resume_requires_explicit_choice_and_gate'){
  let body=context.arrangementDecisionBody(form('resume',{resume_choice:'reuse',followup_enabled:true}));assert.equal(body.reuse_next_check,true);assert(!Object.hasOwn(body,'next_check'));body=context.arrangementDecisionBody(form('resume',{resume_choice:'none',followup_enabled:false}));assert.equal(body.next_check,null);assert.equal(body.followup_enabled,false);assert.throws(()=>context.arrangementDecisionBody(form('resume',{})),/请选择/);assert(!context.arrangementCheckReusable({status:'planned',time_spec:{precision:'instant',at:NOW}}));assert(context.arrangementCheckReusable({status:'planned',time_spec:{precision:'date',date:'2026-10-05'}}));
 }
 else if(scenario==='withdraw_and_cancel_bind_current_task_version'){
  const body=context.arrangementDecisionBody(form('withdraw_execution',{continue_coordination:true,reason:'原时间不去'},{dataset:{taskRevision:'5'}}));assert.equal(body.continue_coordination,true);assert.equal(body.expected_task_revision,5);const html=context.arrangementDecisionHTML(plan({active_schedule:{id:30,revision:5,remind_at:NOW+3600}}),'cancel_activity');assert(html.includes('其他活动、事项和项目继续保留'));assert(html.includes('data-task-revision="5"'));
  const continuing=context.arrangementDecisionBody(form('continue_set_time',{}, {dataset:{taskRevision:'5'}}));assert.deepEqual(plain(continuing),{operation:'continue_set_time',expected_revision:3,expected_task_revision:5});assert(!Object.hasOwn(context.arrangementDecisionBody(form('continue_set_time')), 'expected_task_revision'));
 }
 else if(scenario==='stable_retry_survives_form_reopen'){
  const current=form('pause',{reason:'先缓缓'});responses.push(new Error('网络失败'));await context.submitArrangementDecision(current);const first=plain(calls[0].options.body);assert(current.values.request_id);assert(current.values.request_text);const reopened=form('pause',{...current.values},{isConnected:false});responses.push({plan_id:1,revision:4,receipt:'已暂缓'});await context.submitArrangementDecision(reopened);assert.deepEqual(plain(calls[1].options.body),first);assert.equal(clears,0);
 }
 else if(scenario==='different_input_has_new_request_id'){
  const current=form('update_progress',{progress_text:'问过了'});responses.push(new Error('网络失败'));await context.submitArrangementDecision(current);const first=calls[0].options.body.request_id;current.values.progress_text='已经回复';current.isConnected=false;responses.push({plan_id:1,revision:4});await context.submitArrangementDecision(current);assert.notEqual(calls[1].options.body.request_id,first);
 }
 else if(scenario==='double_submission_one_request'){
  const current=form('pause',{}, {isConnected:false});let resolve;responses.push(()=>new Promise(r=>resolve=r));const waiting=context.submitArrangementDecision(current);await context.submitArrangementDecision(current);assert.equal(calls.length,1);resolve({plan_id:1,revision:4});await waiting;
 }
 else if(scenario==='409_keeps_inputs_and_does_not_auto_confirm'){
  const current=form('pause',{reason:'手机上的未发送原因'}),slot={innerHTML:''};current.nodes.set('[data-arrangement-latest]',slot);const error=new Error('版本已更新');error.status=409;responses.push(error,{plan:plan({revision:5,arrangement:item({revision:5})})});await context.submitArrangementDecision(current);assert.equal(current.values.reason,'手机上的未发送原因');assert.equal(current.dataset.planRevision,'3');assert.equal(current.error.textContent,'版本已更新');assert(slot.innerHTML.includes('保留输入，重新核对'));assert.equal(calls.length,2);assert.equal(calls[1].options.method,undefined);assert.equal(dialogs.length,0);assert.equal(clears,0);
 }
 else if(scenario==='matter_old_task_and_reschedule_remain_one_card'){
  const old={id:30,title:'原活动',remind_at:NOW+3600,status:'pending'},html=context.arrangementMatterSchedulesHTML({plans:[plan({task_id:30,active_schedule:old,arrangement:item({active_schedule:old})})],tasks:[old]});assert.equal(html.split('data-arrangement-card=').length-1,1);assert(html.includes('原日程仍生效'));assert(html.includes('拟改安排'));
 }
 else if(scenario==='reminder_read_only_marks_seen'){
  const summary={textContent:''},group={dataset:{arrangementUnreadCount:'2'},nodes:new Map([['summary',summary]])},card={readLabel:{textContent:''},closest:()=>group},button={dataset:{arrangementRead:'9'},disabled:false,isConnected:true,closest:()=>card,remove(){this.isConnected=false;}};responses.push({read:true});await context.arrangementReadNotice(button);assert.equal(calls[0].url,'/api/secretary/arrangement-notices/9/read');assert.deepEqual(plain(calls[0].options.body),{});assert(card.readLabel.textContent.includes('处理前仍保留'));assert.equal(calls.length,1);assert.equal(loads,0);assert.equal(group.dataset.arrangementUnreadCount,1);assert.equal(summary.textContent,'网页推进提醒 · 1 条未读');
  const html=context.arrangementNoticeHTML({id:9,plan_id:1,title:'待推进',unread:false,causes:[{id:9,kind:'check',text:'询问李总<附件>'},{id:10,kind:'deadline_near',text:'希望确定期限将到'}]});assert(html.includes('询问李总&lt;附件&gt;'));assert(html.includes('希望确定期限将到'));assert(html.includes('已看过，处理前仍保留'));assert(!html.includes('data-arrangement-read='));
 }
 else if(scenario==='forms_have_stable_proxy_and_draft_ids'){
  for(const operation of ['pause','resume','set_deadline','set_check','update_progress','confirm_arrangement','start_reschedule','continue_set_time','withdraw_execution','cancel_activity','abandon_coordination']){const html=context.arrangementDecisionHTML(plan(),operation);assert(html.includes(`id="arrangement-${operation}-form"`));assert(html.includes('data-id="1"'));assert(html.includes('data-revision="3"'));assert(html.includes('name="request_text"'));}
 }
 else if(scenario==='refresh_keeps_composer_attachments_and_history_open'){
  const sections=new Map(['summary','controls','candidates','support','question','history'].map(name=>[`[data-arrangement-section="${name}"]`,{innerHTML:'old',open:true}])),composer={dataset:{planRevision:'3'},text:{value:'接着说的新内容'},attachment_items:{value:'[fictional-material]'},insertAdjacentHTML:()=>{}};
  composer.nodes=new Map([['.flow-current',{innerHTML:'old'}]]);
  currentRoot={dataset:{arrangementDetail:'1',arrangementRevision:'3'},isConnected:true,nodes:sections,composer,closest:()=>({open:true})};state.detail={type:'arrangement',id:1};responses.push({plan:plan({revision:4,question:'新的问题<问>',goal:'新的目标',arrangement:item({revision:4,settling_state:'paused'})})});await context.refreshArrangementAfterTurn({plan_id:1,revision:4});assert.equal(composer.text.value,'接着说的新内容');assert.equal(composer.attachment_items.value,'[fictional-material]');assert.equal(composer.dataset.planRevision,4);assert.equal(sections.get('[data-arrangement-section="history"]').open,true);assert(sections.get('[data-arrangement-section="controls"]').innerHTML.includes('data-arrangement-operation="resume"'));assert(sections.get('[data-arrangement-section="question"]').innerHTML.includes('新的问题&lt;问&gt;'));assert(sections.get('[data-arrangement-section="support"]').innerHTML.includes('新的目标'));assert.equal(sections.get('[data-arrangement-section="support"]').open,true);assert(composer.nodes.get('.flow-current').innerHTML.includes('暂缓推进'));assert.equal(dialogs.length,0);
 }
 else if(scenario==='late_refresh_preserves_new_focus'){
  currentRoot={dataset:{arrangementDetail:'1'},isConnected:true,nodes:new Map(),closest:()=>({open:true})};state.detail={type:'arrangement',id:1};let resolve;responses.push(()=>new Promise(r=>resolve=r));const waiting=context.refreshArrangementAfterTurn({plan:{id:1}});state.detail={type:'matter',id:99};resolve({plan:plan({revision:8})});await waiting;assert.equal(state.detail.id,99);assert.equal(currentRoot.dataset.arrangementRevision,undefined);assert.equal(dialogs.length,0);
 }
 else if(['app_restore_old_candidate_renews_visible_confirmation','app_restore_old_manual_input_preserves_draft'].includes(scenario)){
  // Execute the shipped app click handler and applyDraftValues, rather than
  // calling only the arrangement helper: the regression was at this seam.
  for(const name of ['resetVisitSourceReason','syncVisitSourceTime','applyDraftValues']){
   const line=app.split('\n').find(line=>line.startsWith(`function ${name}(`));assert(line,'production draft function missing');vm.runInContext(line,context);
  }
  const clickStart=app.indexOf("document.addEventListener('click',async event=>{"),clickEnd=app.indexOf("document.addEventListener('keydown'",clickStart);
  assert(clickStart>=0&&clickEnd>clickStart,'production app click branch missing');vm.runInContext(app.slice(clickStart,clickEnd),context);
  context.dispatchFeatureAction=async()=>false;
  const candidateA={id:'a',label:'方案 A',time_spec:{precision:'instant',date:'2026-10-08',at:NOW+3*86400}},candidateB={id:'b',label:'方案 B',time_spec:{precision:'instant',date:'2026-10-09',at:NOW+4*86400},place:'当前会议室 B',conditions:['当前条件 B']};
  context.restorePlan=plan({revision:5,arrangement:item({revision:5,decision_mode:'external',selected_candidate_id:'a',candidates:[candidateA,candidateB],proposed_execution:candidateA,agreement_field_signatures:{}})});vm.runInContext('arrangementPlans.set(1,restorePlan)',context);
  const manual=scenario==='app_restore_old_manual_input_preserves_draft',draft={revision:'3',values:{candidate_id:manual?'':'b',settlement_scope:'date_only',execution_date:'2026-10-10',execution_precision:'date',place:'草稿地点',agreement_attested:true,agreement_place_attested:true,agreement_conditions_attested:true,agreement_place_signature:'old-place',agreement_conditions_signature:'old-conditions',agreement_place_value:'旧地点',agreement_conditions_value:'["旧条件"]',text:'手机上还未发送的准备想法',attachment_items:'[{"material_id":42,"name":"虚构资料.pdf"}]',request_id:'old-request',request_text:'old-body'}};
  const current=form('confirm_arrangement',{...draft.values,candidate_id:'a',text:'',attachment_items:'[]'}, {dataset:{revision:'5',planRevision:'5',pendingDraft:'yes',arrangementRequestText:'old-body'}});current.id='arrangement-confirm_arrangement-form';
  const checkboxNames=['agreement_attested','agreement_place_attested','agreement_conditions_attested'];
  current.inputs=Object.keys(current.values).map(name=>({name,type:checkboxNames.includes(name)?'checkbox':'text',get value(){return current.values[name];},set value(value){current.values[name]=value;},get checked(){return Boolean(current.values[name]);},set checked(value){current.values[name]=Boolean(value);}}));
  for(const field of current.inputs)current.nodes.set(`[name="${field.name}"]`,field);
  const custom={hidden:true,inputs:[{disabled:true},{disabled:true}]},preview={innerHTML:`本次核对方案：TIME-${candidateA.time_spec.at}`},options={innerHTML:'old agreement'};current.nodes.set('[data-arrangement-custom-execution]',custom);current.nodes.set('[data-arrangement-selected-confirmation]',preview);current.nodes.set('[data-arrangement-agreement-options]',options);
  const note={textContent:''},button={dataset:{restoreDraft:'fictional-old-arrangement-draft'},disabled:false,matches:()=>false,hasAttribute:()=>false,closest:selector=>selector==='form'?current:selector==='.draft-restore-note'?note:null};
  context.readDraft=key=>{assert.equal(key,button.dataset.restoreDraft);return draft;};
  const appClick=listeners.get('click').at(-1).fn;await appClick({target:{closest:()=>button},preventDefault(){}});
  assert.equal(notices.length,0);assert.equal(calls.length,0);assert.equal(current.dataset.planRevision,'5');assert.equal(current.dataset.revision,'5');assert(!Object.hasOwn(current.dataset,'pendingDraft'));
  assert.equal(current.values.candidate_id,manual?'':'b');assert.equal(current.values.text,draft.values.text);assert.equal(current.values.attachment_items,draft.values.attachment_items);assert.equal(current.values.execution_date,'2026-10-10');
  for(const name of checkboxNames)assert.equal(current.values[name],false);assert.equal(current.values.request_id,'');assert.equal(current.values.request_text,'');assert.equal(current.dataset.arrangementRequestText,'');assert.equal(saves,1);assert.equal(clears,0);assert(note.textContent.includes('核对最新资料'));
  const body=context.arrangementDecisionBody(current);assert.equal(body.expected_revision,5);assert(!body.agreement_attestation);
  if(manual){assert.equal(preview.innerHTML,'');assert.equal(custom.hidden,false);assert(custom.inputs.every(field=>!field.disabled));assert.equal(body.proposed_execution.time_spec.date,'2026-10-10');assert.equal(body.settlement_scope,'date_only');}
  else{assert(preview.innerHTML.includes(`TIME-${candidateB.time_spec.at}`));assert(!preview.innerHTML.includes(`TIME-${candidateA.time_spec.at}`));assert.equal(custom.hidden,true);assert(custom.inputs.every(field=>field.disabled));assert.equal(body.candidate_id,'b');assert(options.innerHTML.includes('当前会议室 B'));assert(!options.innerHTML.includes(' checked'));}
 }
 else throw new Error('unknown scenario '+scenario);
}
run().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
'''

SCENARIOS = [
    'date_and_window_never_invent_clock', 'three_times_and_old_schedule_coexist',
    'date_settled_does_not_claim_timed_schedule', 'candidate_choice_not_agreement_or_schedule',
    'all_default_full_counts_and_filtered_transport', 'undated_visible_and_notice_failure_honest',
    'pagination_clamps_to_last_page_in_scope', 'source_hidden_is_read_only',
    'today_month_process_once_refreshes_same_plan_and_server_order',
    'today_empty_click_all_keeps_scope_and_undated_plan',
    'scope_links_only_use_named_entities_and_escape_labels',
    'scope_clicks_replace_incompatible_ranges_and_keep_names_when_empty',
    'visible_pending_disabled_followup_has_explicit_resume',
    'hidden_pending_disabled_followup_stays_read_only',
    'settled_disabled_followup_has_no_resume_prompt',
    'open_exact_plan_bypasses_matter_redirect', 'late_detail_does_not_steal_focus',
    'pause_does_not_cancel_schedule', 'confirm_candidate_and_external_attestation_strict',
    'agreement_fields_are_visible_independent_and_not_prechecked',
    'time_attestation_does_not_approve_place_or_conditions',
    'explicit_agreement_uses_current_server_field_signatures',
    'changed_place_or_candidate_cannot_reuse_old_agreement',
    'manual_execution_preserves_duration_and_place', 'window_input_leaves_policy_boundaries_to_service',
    'deadline_and_check_clearing_are_separate', 'progress_consumes_check_only_when_explicit',
    'resume_requires_explicit_choice_and_gate', 'withdraw_and_cancel_bind_current_task_version',
    'stable_retry_survives_form_reopen', 'different_input_has_new_request_id',
    'double_submission_one_request', '409_keeps_inputs_and_does_not_auto_confirm',
    'matter_old_task_and_reschedule_remain_one_card', 'reminder_read_only_marks_seen',
    'forms_have_stable_proxy_and_draft_ids', 'refresh_keeps_composer_attachments_and_history_open',
    'late_refresh_preserves_new_focus',
    'app_restore_old_candidate_renews_visible_confirmation',
    'app_restore_old_manual_input_preserves_draft',
]


@pytest.mark.skipif(not NODE, reason='Node.js needed for production script VM')
@pytest.mark.parametrize('scenario', SCENARIOS)
def test_arrangement_queue_frontend(scenario):
    # Keep Windows command lines bounded as the real-function scenarios grow.
    result = subprocess.run([NODE, '-e', "eval(require('node:fs').readFileSync(0,'utf8'))", str(ROOT), scenario],
                            input=HARNESS, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
