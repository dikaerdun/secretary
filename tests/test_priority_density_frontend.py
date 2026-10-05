"""Exercise the real priority renderer and disclosure listener with fictional data.

The Node VM loads production helpers instead of rebuilding their decisions.  A
small HTML tree reader checks summary/body boundaries and native disclosure
structure; transport and storage spies reject accidental business side effects.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[1], scenario = process.argv[2];
const app = fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8');
const state = {view:'dashboard',customers:[]};
const calls = [], listeners = [];
const rejectSideEffect = (...args)=>{calls.push(args);assert.fail('disclosure must not request or persist business changes');};
const context = {state,Intl,URLSearchParams,Number,Boolean,Promise,Map,Set,Array,
  Date,Math,String,Object,JSON,
  document:{addEventListener:(name,handler,capture)=>listeners.push({name,handler,capture})},
  icon:()=>'<svg aria-hidden="true"></svg>',api:rejectSideEffect,fetch:rejectSideEffect,
  sessionStorage:{setItem:rejectSideEffect,removeItem:rejectSideEffect},
  localStorage:{setItem:rejectSideEffect,removeItem:rejectSideEffect}
};
vm.createContext(context);
function slice(start,end){
  const a=app.indexOf(start),b=app.indexOf(end,a);
  assert(a>=0&&b>a,'real production function block must be available: '+start);
  vm.runInContext(app.slice(a,b),context);
}
slice('const escapeHTML =','const paths =');
slice('function shanghaiParts(','function notify(');
const sourceText=app.split('\n').find(line=>line.startsWith('function sourceText('));
assert(sourceText,'real source-text helper must be available');
vm.runInContext(sourceText,context);
slice('function evidenceLink(','function customerActionTime(');
const lifecycle=fs.readFileSync(path.join(root,'secretary/static/record-lifecycle.js'),'utf8');
vm.runInContext(lifecycle.slice(lifecycle.indexOf('function lifecycleActions('),lifecycle.indexOf('function lifecycleNavigation(')),context);

// Read the emitted HTML without a DOM dependency or an alternate renderer.
function decode(text){return text.replace(/&(amp|lt|gt|quot|#39);/g,(_,name)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[name]));}
function tree(html){
  const root={tag:'#root',attrs:{},children:[]},stack=[root];
  for(const token of html.match(/<[^>]*>|[^<]+/g)||[]){
    if(!token.startsWith('<')){stack.at(-1).children.push(decode(token));continue;}
    if(token.startsWith('</')){
      const tag=token.slice(2,-1).trim();
      assert.equal(stack.at(-1).tag,tag,'priority HTML must close the current element');
      stack.pop();continue;
    }
    const match=token.match(/^<([\w-]+)([^>]*)>/);assert(match,'valid priority tag');
    const node={tag:match[1],attrs:{},children:[],parent:stack.at(-1)};
    for(const attr of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.attrs[attr[1]]=decode(attr[2]||'');
    node.parent.children.push(node);
    if(!token.endsWith('/>')&&!['br','hr','img','input','meta','link'].includes(node.tag))stack.push(node);
  }
  assert.equal(stack.length,1,'priority HTML must be balanced');return root;
}
function nodes(node,predicate){
  const found=[];for(const child of node.children||[])if(typeof child!=='string'){
    if(predicate(child))found.push(child);found.push(...nodes(child,predicate));
  }return found;
}
const has=(node,name)=>Object.hasOwn(node.attrs,name);
const klass=(node,name)=>(node.attrs.class||'').split(/\s+/).includes(name);
const text=node=>(node.children||[]).map(child=>typeof child==='string'?child:text(child)).join('');
const rows=doc=>nodes(doc,node=>node.tag==='details'&&klass(node,'priority-row'));
const only=(values,message)=>{assert.equal(values.length,1,message);return values[0];};
const summary=row=>only(row.children.filter(node=>typeof node!=='string'&&node.tag==='summary'),'one direct row summary');
const body=row=>only(row.children.filter(node=>typeof node!=='string'&&klass(node,'priority-body')),'one direct row body');
const byClass=(doc,name)=>nodes(doc,node=>klass(node,name));
const byAttr=(doc,name)=>nodes(doc,node=>has(node,name));
const focusCalls=[];
function mount(html){
  const doc=tree(html);
  function decorate(node){
    node.matches=selector=>selector.split(',').some(part=>{
      const match=part.trim().match(/^(?:([\w-]+))?(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/);
      return Boolean(match&&(!match[1]||node.tag===match[1])&&(!match[2]||klass(node,match[2]))&&(!match[3]||has(node,match[3])));
    });
    node.querySelectorAll=selector=>nodes(node,child=>child.matches(selector));
    node.querySelector=selector=>node.querySelectorAll(selector)[0]||null;
    node.closest=selector=>node.matches(selector)?node:node.parent?.closest?.(selector)||null;
    node.hasAttribute=name=>has(node,name);
    node.contains=target=>{for(let current=target;current;current=current.parent)if(current===node)return true;return false;};
    node.remove=()=>{if(node.parent)node.parent.children=node.parent.children.filter(child=>child!==node);node.parent=null;};
    node.focus=options=>{context.document.activeElement=node;focusCalls.push({node,options});};
    node.dataset=new Proxy({}, {
      get:(_,name)=>node.attrs['data-'+String(name).replace(/[A-Z]/g,char=>'-'+char.toLowerCase())],
      set:(_,name,value)=>{node.attrs['data-'+String(name).replace(/[A-Z]/g,char=>'-'+char.toLowerCase())]=String(value);return true;}
    });
    const sibling=offset=>{if(!node.parent)return null;const peers=node.parent.children.filter(child=>typeof child!=='string');return peers[peers.indexOf(node)+offset]||null;};
    Object.defineProperties(node,{
      isConnected:{get:()=>node===doc||Boolean(node.parent?.isConnected)},
      nextElementSibling:{get:()=>sibling(1)},previousElementSibling:{get:()=>sibling(-1)},
      textContent:{get:()=>text(node),set:value=>{node.children=[String(value)];}},
      innerHTML:{set:value=>{const fragment=tree(String(value));node.children=fragment.children;for(const child of node.children)if(typeof child!=='string'){child.parent=node;decorate(child);}}}
    });
    for(const child of node.children)if(typeof child!=='string')decorate(child);
  }
  decorate(doc);context.document.activeElement=null;
  context.document.querySelector=selector=>doc.querySelector(selector);
  context.document.querySelectorAll=selector=>doc.querySelectorAll(selector);
  context.$=(selector,scope=context.document)=>scope.querySelector(selector);
  context.$$=(selector,scope=context.document)=>scope.querySelectorAll(selector);
  return doc;
}
const boards=doc=>byClass(doc,'priority-board');
function assertBoardTotal(board,total){
  assert.equal(board.dataset.priorityTotal,String(total));
  assert.equal(text(only(byClass(board,'priority-count'))),total+' 项');
}
const ts=value=>Date.parse(value)/1000;
function item(extra={}){
  return {key:'priority:record:71',signature:'synthetic-source-signature',record_id:71,
    customer_id:501,customer_name:'Synthetic customer outside cached list',score:100,
    talk:'Synthetic complete action title',progress:'Synthetic recommended next step',
    why:['已确认安排到时仍未完成'],whom:[{name:'Synthetic contact',role:'技术负责人'}],
    uncertainties:['Synthetic uncertainty remains to verify'],
    evidence:[{source_type:'record',record_id:71,title:'Synthetic original record'}],
    material:[],...extra};
}
function card(value){return only(rows(tree(context.priorityCardHTML(value))),'one priority card');}
function assertNoSummaryButtons(doc){
  for(const node of nodes(doc,node=>node.tag==='summary')){
    assert.equal(nodes(node,child=>child.tag==='button').length,0,'native summary cannot nest a business button');
    assert.equal(byAttr(node,'data-record').length,0,'record navigation belongs in the body');
    assert.equal(byAttr(node,'data-customer').length,0,'customer navigation belongs in the body');
  }
}
async function run(){
  if(scenario==='summary_body_layers_default_collapsed'){
    const value=item({talk:'Synthetic full title '+('long title detail '.repeat(35))});
    const row=card(value),head=summary(row),detail=body(row);assertNoSummaryButtons(row);
    assert(!has(row,'open'),'new cards default to collapsed');
    assert.equal(row.attrs['data-priority-disclosure'],value.key);
    assert.equal(text(only(byClass(head,'priority-title'))),value.talk,'complete title stays in the DOM');
    assert(text(head).includes(value.customer_name));assert(text(head).includes('安排已到'));
    for(const hidden of [value.progress,value.whom[0].name,value.uncertainties[0],value.evidence[0].title,'已确认安排到时仍未完成']){
      assert(!text(head).includes(hidden),'detail must not expand the collapsed summary: '+hidden);
      assert(text(detail).includes(hidden),'original detail stays inspectable: '+hidden);
    }
    assert(!text(detail).includes(value.talk),'opening does not repeat the entire title');
    for(const nested of nodes(row,node=>node.tag==='details'))assert(!has(nested,'open'),'secondary disclosures start closed');
    return;
  }
  if(scenario==='customer_name_independent_of_first_200'){
    state.customers=Array.from({length:200},(_,index)=>({id:index+1,name:'Cached synthetic customer '+(index+1)}));
    const value=item(),row=card(value);assert(text(summary(row)).includes(value.customer_name));
    assert.equal(text(only(byAttr(body(row),'data-customer'))),value.customer_name);
    assert.equal(only(byAttr(body(row),'data-customer')).attrs['data-customer'],'501');
    assert(!text(row).includes('客户待核对'),'a server-provided customer name needs no cached lookup');
    const fallback=card(item({customer_id:199,customer_name:null}));assert(text(summary(fallback)).includes('Cached synthetic customer 199'));
    const unknown=card(item({customer_name:null}));assert(text(summary(unknown)).includes('客户待核对'));return;
  }
  if(scenario==='status_follows_saved_reasons_not_score'){
    const cases=[
      ['已确认安排到时仍未完成','安排已到'],['原话截止已到，需核对结果','期限已到'],
      ['约定检查时间已到','该核对反馈'],['正在等待客户反馈','等客户反馈'],
      ['正在等待内部同事反馈','等内部反馈'],['客户有已记录阻力：预算待核实','需核实阻力'],
      ['项目有已记录阻力：接口待核实','需核实阻力'],
      ['距最近交流记录已超过设定的7天联系间隔，实际联系情况需核实','该核对联系']
    ];
    for(const [why,label] of cases){
      const row=card(item({why:['Synthetic additional reason',why],score:0}));
      assert.equal(text(only(byClass(summary(row),'priority-status'))),label);
      assert(text(body(row)).includes(why),'full saved reason remains in detail');
    }
    const neutral=card(item({why:['Synthetic unrecognized reason'],score:100}));
    assert.equal(text(only(byClass(summary(neutral),'priority-status'))),'待推进');
    for(const falseClaim of ['安排已到','期限已到','该核对反馈'])assert(!text(summary(neutral)).includes(falseClaim),'score cannot invent a fact');
    return;
  }
  if(scenario==='date_only_reasons_keep_unknown_clock'){
    for(const [why,label] of [['约定截止日期已到，具体时刻尚未明确','截止日已到'],['约定检查日期已到，具体时刻尚未明确','检查日已到']]){
      const row=card(item({why:[why]}));assert.equal(text(only(byClass(summary(row),'priority-status'))),label);
      assert(text(body(row)).includes(why));assert(!text(row).includes('00:00'),'a date-only reason cannot become a midnight appointment');
    }return;
  }
  if(scenario==='changed_evidence_never_hides_due_status'){
    const row=card(item({source_changed_after_decision:true})),head=summary(row),detail=body(row);
    assert.equal(text(only(byClass(head,'priority-status'))),'安排已到');
    assert.equal(text(only(byClass(head,'priority-change'))),'依据有更新');
    assert(text(detail).includes('之前的处理决定未自动应用'));assert(text(detail).includes('已确认安排到时仍未完成'));
    const unchanged=card(item({source_changed_after_decision:false}));assert.equal(byClass(unchanged,'priority-change').length,0);return;
  }
  if(scenario==='unknown_reasons_and_uncertainties_preserved'){
    const why=['Synthetic previously unknown reason',{quote:'Synthetic original quoted reason'}];
    const row=card(item({why,uncertainties:[{description:'Synthetic uncertainty description'}],progress:{text:'Synthetic progress object'}}));
    const detail=text(body(row));for(const value of ['Synthetic previously unknown reason','Synthetic original quoted reason','Synthetic uncertainty description','Synthetic progress object'])assert(detail.includes(value));
    assert(!text(summary(row)).includes('Synthetic previously unknown reason'));return;
  }
  if(scenario==='human_text_and_action_attributes_are_escaped'){
    const unsafe='<img src=x onerror="synthetic()"> & \'quote\'',key='priority:<"synthetic">&',signature='sig:<"synthetic">&';
    const value=item({key,signature,record_id:null,talk:unsafe,customer_name:unsafe,
      progress:unsafe,why:[unsafe],whom:[{name:unsafe,role:unsafe}],uncertainties:[unsafe],
      evidence:[{source_type:'record',record_id:88,title:unsafe,value:unsafe}],material:[unsafe]});
    const html=context.priorityCardHTML(value),row=only(rows(tree(html)));
    assert(!html.includes('<img'));assert(!html.includes(unsafe));assert(html.includes('&lt;img'));
    assert.equal(row.attrs['data-priority-disclosure'],key);
    assert.equal(text(only(byClass(summary(row),'priority-title'))),unsafe);
    for(const node of byAttr(row,'data-priority-key'))assert.equal(node.attrs['data-priority-key'],key);
    assert.equal(only(byAttr(row,'data-lifecycle-archive-tip')).attrs['data-lifecycle-archive-tip'],key);
    assert.equal(only(byAttr(row,'data-lifecycle-tip-signature')).attrs['data-lifecycle-tip-signature'],signature);
    for(const node of nodes(row,()=>true))assert(!has(node,'onerror'),'escaped text cannot add event attributes');
    assertNoSummaryButtons(row);return;
  }
  if(scenario==='source_union_dedup_retains_distinct_identity'){
    const sharedTitle='Synthetic same source title';
    const value=item({evidence:[
      {source_type:'record',record_id:71,title:sharedTitle,value:'Synthetic richer original evidence'},
      {source_type:'record',record_id:72,title:sharedTitle},
      {source_type:'customer_fact',fact_id:81,record_id:71,title:sharedTitle,value:'Synthetic fact one'},
      {source_type:'customer_fact',fact_id:82,record_id:71,title:sharedTitle,value:'Synthetic fact two'}
    ],material:[
      {source_type:'record',record_id:71,title:sharedTitle},
      {source_type:'material',material_id:71,title:sharedTitle},
      'Synthetic legacy source without an identity','Synthetic legacy source without an identity'
    ]});
    const row=card(value),evidence=only(byClass(body(row),'priority-evidence'));
    assert.equal(nodes(evidence,node=>node.tag==='li').length,6,'merge duplicates while keeping different records, facts and source types');
    assert.equal(byAttr(evidence,'data-record').filter(node=>node.attrs['data-record']==='72').length,1);
    assert.equal(byAttr(evidence,'data-material').filter(node=>node.attrs['data-material']==='71').length,1);
    for(const preserved of ['Synthetic richer original evidence','Synthetic fact one','Synthetic fact two','Synthetic legacy source without an identity'])assert(text(evidence).includes(preserved));
    assert.equal(text(evidence).split('Synthetic legacy source without an identity').length-1,1);
    assert(text(summary(evidence)).includes('6'));return;
  }
  if(scenario==='source_revisions_and_unknown_sources_remain_visible'){
    const row=card(item({evidence:[
      {source_type:'record',record_id:71,title:'Synthetic revision source',revision:1,value:'Synthetic revision one'},
      {source_type:'record',record_id:71,title:'Synthetic revision source',revision:2,value:'Synthetic revision two'},
      {source_type:'record',record_id:71,title:'Synthetic other passage',revision:2},
      {source_type:'future_source',title:'Synthetic future source',description:'Synthetic unknown source detail'}
    ],material:[]}));
    const evidence=only(byClass(body(row),'priority-evidence'));
    assert.equal(nodes(evidence,node=>node.tag==='li').length,4);
    for(const preserved of ['Synthetic revision one','Synthetic revision two','Synthetic other passage','Synthetic future source','Synthetic unknown source detail'])assert(text(evidence).includes(preserved));return;
  }
  if(scenario==='customer_fact_value_and_original_evidence_preserved'){
    const unsafe='<img src=x onerror="synthetic()"> & original quote';
    const row=card(item({evidence:[
      {source_type:'customer_fact',fact_id:81,record_id:null,value:'Synthetic saved profile value',evidence:'Synthetic original customer quote'},
      {source_type:'customer_fact',fact_id:82,record_id:91,title:'Synthetic linked profile fact',value:'Synthetic repeated profile detail',blockers:'Synthetic repeated profile detail',evidence:'Synthetic repeated profile detail'},
      {source_type:'customer_fact',fact_id:83,record_id:92,title:'Synthetic fact with original quote',value:'Synthetic safe profile conclusion',evidence:unsafe}
    ]}));
    const evidence=only(byClass(body(row),'priority-evidence')),entries=nodes(evidence,node=>node.tag==='li');
    assert.equal(entries.length,3);
    assert(text(entries[0]).includes('Synthetic saved profile value'),'an unlinked profile fact keeps its value');
    assert(text(entries[0]).includes('Synthetic original customer quote'),'a profile value cannot replace its original evidence quote');
    assert.equal(byAttr(entries[0],'data-record').length,0,'missing source record does not create a dead link');
    assert.equal(text(entries[1]).split('Synthetic repeated profile detail').length-1,1,'identical value, blockers and quote are shown once');
    assert.equal(only(byAttr(entries[1],'data-record')).attrs['data-record'],'91');
    assert(text(entries[2]).includes('Synthetic safe profile conclusion'));assert(text(entries[2]).includes(unsafe));
    assert.equal(only(byAttr(entries[2],'data-record')).attrs['data-record'],'92','the original evidence target stays unchanged');
    const html=context.priorityCardHTML(item({evidence:[{source_type:'customer_fact',fact_id:83,record_id:92,value:'Synthetic safe profile conclusion',evidence:unsafe}]}));
    assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(!html.includes(unsafe));
    for(const node of nodes(row,()=>true))assert(!has(node,'onerror'),'original quoted evidence remains escaped');
    assertNoSummaryButtons(row);return;
  }
  if(scenario==='source_navigation_preserves_original_targets'){
    const row=card(item({evidence:[
      {source_type:'record',record_id:71,title:'Synthetic original record'},
      {source_type:'visit',visit_id:13,title:'Synthetic original visit'},
      {source_type:'material',material_id:14,title:'Synthetic original material'},
      {source_type:'customer',customer_id:501,title:'Synthetic original customer'}
    ]}));
    const evidence=only(byClass(body(row),'priority-evidence'));
    for(const [attr,id] of [['data-record','71'],['data-visit','13'],['data-material','14'],['data-customer','501']])assert.equal(only(byAttr(evidence,attr)).attrs[attr],id);
    assertNoSummaryButtons(row);return;
  }
  if(scenario==='primary_secondary_actions_keep_targets_and_signature'){
    const value=item(),original=JSON.stringify(value),row=card(value),detail=body(row);
    const actions=only(byClass(detail,'priority-actions')),more=only(byClass(actions,'priority-more'));
    const primary=only(byAttr(actions,'data-record'));assert.equal(primary.attrs['data-record'],'71');assert(text(primary).includes('继续这个事项'));
    const progress=only(byAttr(actions,'data-customer-command'));assert.equal(progress.attrs['data-customer-command'],'501');
    assert(!nodes(more,node=>node===primary||node===progress).length,'main actions stay outside more processing');
    const choices=byAttr(more,'data-priority-choice');assert.deepEqual(choices.map(node=>node.attrs['data-priority-choice']),['defer','dismiss']);
    for(const choice of choices)assert.equal(choice.attrs['data-priority-key'],value.key);
    assert.equal(only(byAttr(more,'data-lifecycle-record')).attrs['data-lifecycle-record'],'71');
    assertNoSummaryButtons(row);assert.equal(JSON.stringify(value),original,'rendering cannot revise records or source signatures');
    const tip=item({record_id:null}),tipBefore=JSON.stringify(tip),tipRow=card(tip);
    assert.equal(byAttr(tipRow,'data-lifecycle-record').length,0);assert.equal(byAttr(only(byClass(body(tipRow),'priority-actions')),'data-record').length,0);
    assert.equal(only(byAttr(tipRow,'data-lifecycle-tip-signature')).attrs['data-lifecycle-tip-signature'],tip.signature);
    assert.equal(JSON.stringify(tip),tipBefore);return;
  }
  if(scenario==='compact_six_full_count_and_empty'){
    const items=Array.from({length:8},(_,index)=>item({key:'priority:record:'+(index+1),record_id:index+1,talk:'Synthetic priority '+index}));
    const data={items,total:17},compact=tree(context.prioritiesHTML(data,true)),full=tree(context.prioritiesHTML(data));
    assert.equal(rows(compact).length,6);assert.equal(rows(full).length,8);
    assert.deepEqual(rows(compact).map(row=>row.attrs['data-priority-disclosure']),items.slice(0,6).map(value=>value.key));
    assert.equal(text(only(byClass(compact,'priority-count'))),'17 项');assert.equal(text(only(byClass(full,'priority-count'))),'17 项');
    assert(text(only(byAttr(compact,'data-all-priorities'))).includes('17'));
    assert.equal(byAttr(full,'data-all-priorities').length,0);
    assert.equal(text(only(byClass(tree(context.prioritiesHTML({items,total:2},true)),'priority-count'))),'8 项');
    assert.equal(context.prioritiesHTML({items:[],total:0},true),'');assert.equal(context.prioritiesHTML({}), '');
    assertNoSummaryButtons(compact);assertNoSummaryButtons(full);return;
  }
  if(scenario==='toggle_refresh_keeps_open_by_item_key_without_mutation'){
    const toggle=only(listeners.filter(entry=>entry.name==='toggle'),'one production priority disclosure listener');
    assert.equal(toggle.capture,true,'toggle does not bubble and requires capture');
    const values=[item(),item({key:'priority:record:72',record_id:72,talk:'Synthetic second action for same customer'})];
    const original=JSON.stringify(values),stateBefore=JSON.stringify(state);
    assert(rows(tree(context.prioritiesHTML({items:values}))).every(row=>!has(row,'open')));
    const mock=(key,open,connected=true)=>({dataset:{priorityDisclosure:key},open,isConnected:connected,
      matches:selector=>selector==='details[data-priority-disclosure]'});
    toggle.handler({target:mock(values[0].key,true)});
    assert(state.priorityExpanded instanceof Set);assert(state.priorityExpanded.has(values[0].key));
    const refreshed=rows(tree(context.prioritiesHTML({items:values.map(value=>({...value,progress:'Synthetic refreshed progress'}))})));
    assert(has(refreshed[0],'open'));assert(!has(refreshed[1],'open'),'another action for the same customer stays collapsed');
    toggle.handler({target:mock(values[0].key,false,false)});assert(state.priorityExpanded.has(values[0].key),'detached old DOM must not erase refreshed expansion');
    toggle.handler({target:{matches:()=>false,isConnected:true,open:true,dataset:{}}});assert.equal(state.priorityExpanded.size,1,'nested more/evidence disclosures do not change row state');
    toggle.handler({target:mock(values[0].key,false)});assert(!state.priorityExpanded.has(values[0].key));
    assert(rows(tree(context.prioritiesHTML({items:values}))).every(row=>!has(row,'open')));
    toggle.handler({target:mock(73,true)});assert(state.priorityExpanded.has('73'),'item keys normalize to the same string as rendered attributes');
    assert(has(card(item({key:73})),'open'));
    assert.equal(JSON.stringify(values),original);const {priorityExpanded,...remaining}=state;
    assert.equal(JSON.stringify(remaining),stateBefore,'disclosure may only update UI expansion memory');assert.equal(calls.length,0);return;
  }
  if(scenario==='archive_removal_syncs_boards_counts_focus_and_payload'){
    const values=Array.from({length:8},(_,index)=>item({key:'priority:record:'+(71+index),record_id:71+index,talk:'Synthetic archive candidate '+index}));
    const payload={items:values,total:8},original=JSON.stringify(payload),key=values[0].key;
    state.priorities=payload;state.priorityExpanded=new Set([key,values[1].key]);
    const doc=mount(context.prioritiesHTML(payload,true)+context.prioritiesHTML(payload)),[compact,full]=boards(doc);
    const activeRow=rows(full)[0];context.document.activeElement=only(byAttr(activeRow,'data-lifecycle-record'));
    context.priorityRemoveRendered(key);
    assert.deepEqual(rows(compact).map(row=>row.dataset.priorityDisclosure),values.slice(1,6).map(value=>value.key));
    assert.deepEqual(rows(full).map(row=>row.dataset.priorityDisclosure),values.slice(1).map(value=>value.key));
    assertBoardTotal(compact,7);assertBoardTotal(full,7);
    assert.equal(text(only(byAttr(compact,'data-priority-visible-count'))),'先看 5 项。');
    assert(text(only(byAttr(compact,'data-all-priorities'))).includes('查看全部 7 项'));
    assert.equal(context.document.activeElement,summary(rows(full)[0]),'focus moves to the next summary in the active board');
    assert.equal(focusCalls.length,1,'the other rendered copy must not steal focus');
    assert.equal(focusCalls[0].options.preventScroll,true);
    assert.deepEqual(state.priorities.items.map(value=>value.key),values.slice(1).map(value=>value.key));
    assert.equal(state.priorities.total,7);assert.notEqual(state.priorities,payload,'cached state is replaced without changing the original response');
    assert(!state.priorityExpanded.has(key));assert(state.priorityExpanded.has(values[1].key));
    assert.equal(JSON.stringify(payload),original,'original records, source signatures and API payload stay intact');
    context.priorityRemoveRendered(key);assertBoardTotal(compact,7);assertBoardTotal(full,7);
    assert.equal(state.priorities.total,7,'repeated cleanup cannot decrement totals twice');assert.equal(focusCalls.length,1);return;
  }
  if(scenario==='archive_removal_last_row_keeps_empty_board_and_focus'){
    const target=item({record_id:null}),other=item({key:'priority:record:99',record_id:99}),payload={items:[target],total:1},original=JSON.stringify(payload);
    state.priorities=payload;state.priorityExpanded=new Set([target.key,other.key]);
    const doc=mount(context.prioritiesHTML(payload,true)+context.prioritiesHTML({items:[other],total:1})),[active,unrelated]=boards(doc);
    context.document.activeElement=only(byAttr(rows(active)[0],'data-lifecycle-archive-tip'));
    context.priorityRemoveRendered(target.key);
    assert.equal(rows(active).length,0);assertBoardTotal(active,0);
    assert(text(only(byClass(active,'priority-list'))).includes('原事项和资料继续保留'));
    assert.equal(context.document.activeElement,only(byAttr(active,'data-lifecycle-manage')),'last-row removal returns focus to board management');
    assert.equal(byAttr(active,'data-all-priorities').length,0);
    assert.equal(rows(unrelated).length,1);assertBoardTotal(unrelated,1);
    assert.equal(state.priorities.items.length,0);assert.equal(state.priorities.total,0);
    assert(!state.priorityExpanded.has(target.key));assert(state.priorityExpanded.has(other.key));
    assert.equal(JSON.stringify(payload),original);assert.equal(calls.length,0);return;
  }
  if(scenario.startsWith('archive_click_')){
    const target=item({key:'priority:customer-blocker:501',record_id:null}),other=item({key:'priority:record:72',record_id:72});
    const payload={items:[target,other],total:2},original=JSON.stringify(payload);
    state.priorities=payload;state.priorityExpanded=new Set([target.key,other.key]);
    const doc=mount(context.prioritiesHTML(payload,true)+context.prioritiesHTML(payload)),allBoards=boards(doc);
    const button=only(byAttr(rows(allBoards[1])[0],'data-lifecycle-archive-tip'));
    context.document.activeElement=button;
    const requests=[],notices=[],refreshes=[];let resolveRequest,rejectRequest;
    context.api=(url,options)=>{requests.push({url,options});return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});};
    context.notify=(message,error=false)=>notices.push({message,error});
    context.loadView=async force=>refreshes.push(force);
    const mutateButton=app.split('\n').find(line=>line.startsWith('async function mutateButton('));
    assert(mutateButton,'real button mutation helper must be available');vm.runInContext(mutateButton,context);
    const clickStart=lifecycle.indexOf("document.addEventListener('click',");assert(clickStart>=0,'real lifecycle click handler must be available');
    vm.runInContext(lifecycle.slice(clickStart),context);
    const click=only(listeners.filter(entry=>entry.name==='click'));
    const pending=click.handler({target:button});
    assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/priority-decisions');
    assert.equal(requests[0].options.method,'POST');
    assert.equal(JSON.stringify(requests[0].options.body),JSON.stringify({key:target.key,signature:target.signature,decision:'archive'}));
    assert.equal(button.disabled,true);
    for(const board of allBoards){assert.equal(rows(board).length,2);assertBoardTotal(board,2);}
    assert.equal(state.priorities,payload,'no local removal before archive succeeds');
    if(scenario==='archive_click_failure')rejectRequest(new Error('Synthetic archive failure'));
    else{if(scenario==='archive_click_detached')button.remove();resolveRequest({saved:true});}
    await pending;assert.equal(button.disabled,false);assert.equal(requests.length,1);
    if(scenario==='archive_click_success'){
      for(const board of allBoards){assert.equal(rows(board).length,1);assert.equal(rows(board)[0].dataset.priorityDisclosure,other.key);assertBoardTotal(board,1);}
      assert.equal(state.priorities.total,1);assert.equal(state.priorities.items.length,1);assert(!state.priorityExpanded.has(target.key));
      assert.equal(context.document.activeElement,summary(rows(allBoards[1])[0]));
      assert.deepEqual(refreshes,[true]);assert.equal(notices.length,1);assert.equal(notices[0].error,false);
    }else{
      for(const board of allBoards){assert.equal(rows(board).length,2);assertBoardTotal(board,2);}
      assert.equal(state.priorities,payload);assert(state.priorityExpanded.has(target.key));assert.equal(focusCalls.length,0);
      if(scenario==='archive_click_failure'){
        assert.equal(refreshes.length,0);assert.equal(notices.length,1);assert.equal(notices[0].error,true);assert.equal(notices[0].message,'Synthetic archive failure');
      }else{assert.equal(scenario,'archive_click_detached');assert.deepEqual(refreshes,[true]);}
    }
    assert.equal(JSON.stringify(payload),original,'archive UI cleanup cannot rewrite original records or signatures');return;
  }
  assert.fail('unknown priority density scenario '+scenario);
}
run().then(()=>{assert.equal(calls.length,0);process.stdout.write(JSON.stringify({passed:scenario}));})
  .catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason="Node.js is required for the priority renderer regression harness")
@pytest.mark.parametrize("scenario", [
    "summary_body_layers_default_collapsed", "customer_name_independent_of_first_200",
    "status_follows_saved_reasons_not_score", "date_only_reasons_keep_unknown_clock",
    "changed_evidence_never_hides_due_status", "unknown_reasons_and_uncertainties_preserved",
    "human_text_and_action_attributes_are_escaped", "source_union_dedup_retains_distinct_identity",
    "source_revisions_and_unknown_sources_remain_visible", "customer_fact_value_and_original_evidence_preserved",
    "source_navigation_preserves_original_targets",
    "primary_secondary_actions_keep_targets_and_signature", "compact_six_full_count_and_empty",
    "toggle_refresh_keeps_open_by_item_key_without_mutation",
    "archive_removal_syncs_boards_counts_focus_and_payload", "archive_removal_last_row_keeps_empty_board_and_focus",
    "archive_click_success", "archive_click_failure", "archive_click_detached",
])
def test_priority_density_frontend(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, str(ROOT), scenario], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"passed":' in result.stdout and scenario in result.stdout
