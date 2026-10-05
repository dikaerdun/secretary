"""Check the real agenda renderer with fictional data and a read-only transport.

The harness supplies browser state, not a second calendar implementation.  It
checks ordering, preservation of existing action targets, and date-only meaning.
Browser layout and click verification are performed separately on an isolated DB.
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
const state = {view:'agenda',period:'week',date:'2026-10-05'};
const calls = [], responses = [], listeners = new Map();
let openMenus=[];
const context = {state,Intl,URLSearchParams,Number,Boolean,Promise,Map,Set,Array,
  Date,Math,String,Object,JSON,
  document:{addEventListener:(name,handler)=>{if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(handler);}},
  $$:selector=>selector==='[data-agenda-more][open]'?openMenus:[],
  icon:()=>'<svg aria-hidden="true"></svg>',
  api:async url=>{calls.push(url);assert(responses.length,'unexpected extra API request');return responses.shift();}
};
vm.createContext(context);
function slice(start,end){
  const a=app.indexOf(start),b=app.indexOf(end,a);
  assert(a>=0&&b>a,'real production function block must be available: '+start);
  vm.runInContext(app.slice(a,b),context);
}
slice('const escapeHTML =','const paths =');
slice('function shanghaiParts(','function notify(');
slice('function heading(','function sourceIcon(');
slice('function scheduleCompletionActions(','async function loadView(');
const planningSource=app.split('\n').find(line=>line.startsWith('function planningNodesHTML('));
assert(planningSource,'real planning-nodes renderer must be available');
vm.runInContext(planningSource,context);
const flow = fs.readFileSync(path.join(root,'secretary/static/secretary-flow.js'),'utf8');
const labels=flow.split('\n').find(line=>line.startsWith('const flowLabels='));
assert(labels,'real plan-status labels must be available');
vm.runInContext(labels,context);
vm.runInContext(flow.slice(flow.indexOf('function flowScopeAttrs('),flow.indexOf('function flowPlanCard(')),context);
const lifecycle = fs.readFileSync(path.join(root,'secretary/static/record-lifecycle.js'),'utf8');
vm.runInContext(lifecycle.slice(lifecycle.indexOf('function lifecycleActions('),lifecycle.indexOf('function lifecycleManageButton(')),context);

const ts=value=>Date.parse(value)/1000;
const start=ts('2026-10-05T00:00:00+08:00'),end=ts('2026-10-12T00:00:00+08:00');
function task(id,at,extra={}){
  return {id,title:'Synthetic task '+id,remind_at:ts(at),duration_minutes:30,
    status:'pending',record_id:id+100,record_kind:'action',record_status:'following',
    customer_name:'Synthetic customer',opportunity_name:'Synthetic project',...extra};
}
function plan(id,date,extra={}){
  return {id,title:'Synthetic date-only plan '+id,date,start_at:null,status:'preparing',
    record_id:id+200,customer_name:'Synthetic customer',goal:'Synthetic meeting goal',...extra};
}
function agenda(items=[],plans=[],extra={}){
  return {items,secretary_plans:plans,start,end,total:items.length,page:1,pages:1,
    planning_nodes:[],...extra};
}
function structure(groups){
  return JSON.parse(JSON.stringify(groups.map(([date,entries])=>[date,entries.map(entry=>[entry.kind,entry.value.id])])));
}
async function run(){
  if(scenario==='chronological_combined_groups'){
    const tasks=[task(3,'2026-10-09T15:00:00+08:00'),task(2,'2026-10-07T15:00:00+08:00'),task(1,'2026-10-07T09:00:00+08:00')];
    const plans=[plan(12,'2026-10-11'),plan(11,'2026-10-07'),plan(10,'2026-10-06')];
    assert.deepEqual(structure(context.agendaDateGroups(tasks,plans,start)),[
      ['2026-10-06',[['plan',10]]],['2026-10-07',[['task',1],['task',2],['plan',11]]],
      ['2026-10-09',[['task',3]]],['2026-10-11',[['plan',12]]]
    ]);
    responses.push(agenda(tasks,plans));const html=await context.renderAgenda();
    const titles=['Synthetic date-only plan 10','Synthetic task 1','Synthetic task 2','Synthetic date-only plan 11','Synthetic task 3','Synthetic date-only plan 12'];
    for(let i=1;i<titles.length;i++)assert(html.indexOf(titles[i-1])<html.indexOf(titles[i]),'confirmed and date-only plans must share chronological order');
    for(const title of titles)assert.equal(html.split('>'+title+'<').length-1,1,'each event appears once');
    assert.equal(html.split('data-agenda-date="2026-10-07"').length-1,1,'the same date has one shared group');
    assert(!html.includes('当天的待约 / 时间待定计划'),'the previous out-of-order second section must be gone');return;
  }
  if(scenario==='date_only_plan_never_midnight'){
    const item=plan(7,'2026-10-08');const html=context.agendaPlanHTML(item);
    assert(html.includes('时间待定'));assert(!html.includes('00:00'),'a known date cannot be converted to a known time');
    assert(html.includes('Synthetic meeting goal'),'meeting purpose remains visible');
    assert(html.includes('data-flow-plan="7"'),'existing conversation target must remain correct');
    assert(html.includes('data-lifecycle-record="207"'),'archive remains tied to the original plan record');
    assert(!html.includes('data-complete-task='),'an unscheduled plan has no executable task');return;
  }
  if(scenario==='timed_unconfirmed_plan_keeps_clock'){
    const item=plan(8,'2026-10-08',{start_at:ts('2026-10-08T18:30:00+08:00')});
    const html=context.agendaPlanHTML(item);assert(html.includes('18:30'));assert(!html.includes('时间待定'));assert(!html.includes('data-complete-task='));return;
  }
  if(scenario==='pending_task_targets_and_optional_result'){
    const html=context.agendaTaskHTML(task(8,'2026-10-08T18:30:00+08:00'),start,end);
    for(const attr of ['data-record="108"','data-complete-outcome="108"','data-complete-task="8"','data-next-visit="108"'])assert(html.includes(attr),'preserve supported action target '+attr);
    const noOutcome=context.agendaTaskHTML(task(9,'2026-10-08T18:30:00+08:00',{record_kind:'memo'}),start,end);
    assert(!noOutcome.includes('data-complete-outcome='));assert(noOutcome.includes('data-complete-task="9"'),'task-only completion remains possible');return;
  }
  if(scenario==='completed_cancelled_task_guards'){
    for(const status of ['completed','cancelled']){
      const html=context.agendaTaskHTML(task(5,'2026-10-08T10:00:00+08:00',{status}),start,end);
      assert(!html.includes('data-complete-task=')&&!html.includes('data-complete-outcome='),'closed task cannot be completed again');
      assert(html.includes(status==='completed'?'已完成':'已取消'));assert(html.includes('data-record="105"'),'history remains readable');
    }return;
  }
  if(scenario==='cross_window_continuation_is_preserved'){
    const item=task(4,'2026-10-04T23:30:00+08:00',{duration_minutes:60});
    assert.deepEqual(structure(context.agendaDateGroups([item],[],start)),[['2026-10-05',[['task',4]]]]);
    const html=context.agendaTaskHTML(item,start,end);
    assert(html.includes('继续'));assert(html.includes('上个时间段')||html.includes('跨时段')||html.includes('延续'));assert(html.includes('23:30'),'original start stays inspectable');
    const after=context.agendaTaskHTML(task(6,'2026-10-11T23:30:00+08:00',{duration_minutes:60}),start,end);
    assert(after.includes('下个时段')||after.includes('下个时间段'));assert(after.includes('00:30'),'original end stays inspectable');return;
  }
  if(scenario==='task_without_record_has_no_dead_link'){
    const html=context.agendaTaskHTML(task(9,'2026-10-08T10:00:00+08:00',{record_id:null,customer_name:null,opportunity_name:null}),start,end);
    assert(!html.includes('data-record=')&&!html.includes('data-next-visit=')&&!html.includes('data-complete-outcome='));
    assert(html.includes('data-complete-task="9"'));assert(html.includes('Synthetic task 9'));return;
  }
  if(scenario==='project_attribution_remains_honest'){
    const stale=context.agendaTaskHTML(task(9,'2026-10-08T10:00:00+08:00',{project_link_stale:true,opportunity_name:'Synthetic obsolete project'}),start,end);
    assert(stale.includes('项目归属待核对'));assert(!stale.includes('Synthetic obsolete project'),'a stale project link cannot be presented as current attribution');
    const archived=context.agendaTaskHTML(task(9,'2026-10-08T10:00:00+08:00',{opportunity_archived:true}),start,end);
    assert(archived.includes('Synthetic project')&&archived.includes('已归档'),'valid historical project attribution stays readable');return;
  }
  if(scenario==='escaped_event_and_plan_text'){
    const unsafe='<img src=x onerror=synthetic()>',escaped='&lt;img src=x onerror=synthetic()&gt;';
    const html=context.agendaTaskHTML(task(1,'2026-10-08T10:00:00+08:00',{title:unsafe,customer_name:unsafe,opportunity_name:unsafe}),start,end)+context.agendaPlanHTML(plan(1,'2026-10-08',{title:unsafe,goal:unsafe,customer_name:unsafe}));
    assert(html.includes(escaped));assert(!html.includes(unsafe));assert(!html.includes('data-flow-plan="&lt;'));return;
  }
  if(scenario==='page_aggregation_does_not_repeat_plan'){
    const p=plan(1,'2026-10-07'),a=task(1,'2026-10-08T10:00:00+08:00'),b=task(2,'2026-10-06T10:00:00+08:00');
    responses.push(agenda([a],[p],{total:2,pages:2}),agenda([b],[p],{total:2,page:2,pages:2}));
    const html=await context.renderAgenda();assert.equal(calls.length,2);
    for(let i=0;i<2;i++){
      const query=new URLSearchParams(calls[i].split('?')[1]);assert.equal(query.get('page'),String(i+1));assert.equal(query.get('page_size'),'200');assert.equal(query.get('period'),'week');assert.equal(query.get('date'),'2026-10-05');
    }
    assert.equal(html.split('>Synthetic date-only plan 1<').length-1,1);
    assert(html.indexOf('Synthetic task 2')<html.indexOf('Synthetic date-only plan 1'));assert(html.indexOf('Synthetic date-only plan 1')<html.indexOf('Synthetic task 1'));return;
  }
  if(scenario==='planning_checks_are_not_calendar_events'){
    const node={record_id:71,title:'Synthetic deadline check',kind:'deadline',date:'2026-10-08',date_only:true,evidence:'Synthetic original date'};
    responses.push(agenda([],[],{planning_nodes:[node]}));const html=await context.renderAgenda();
    assert(html.includes('Synthetic deadline check')&&html.includes('data-record="71"'));assert(html.includes('未因此启用提醒'));assert(html.includes('不计入执行容量'));
    assert(!structure(context.agendaDateGroups([],[],start)).length);assert(!html.includes('data-complete-task='));return;
  }
  if(scenario.startsWith('empty_')){
    state.period=scenario.slice(6);responses.push(agenda());const html=await context.renderAgenda();
    const label={day:'当日',week:'本周',month:'本月'}[state.period];
    assert(new RegExp('<h2[^>]*>[^<]*'+label+'[^<]*日程').test(html),'period heading must describe the selected calendar window');
    assert(html.includes('empty-state'));assert(html.includes('data-flow-new')||html.includes('data-go="dashboard"')||html.includes('data-go="records"'),'empty state offers a next step');
    assert(!html.includes('当天的待约 / 时间待定计划'));return;
  }
  if(scenario==='plans_only_are_events_not_empty'){
    responses.push(agenda([],[plan(1,'2026-10-07')]));const html=await context.renderAgenda();
    assert(html.includes('Synthetic date-only plan 1'));assert(!html.includes('empty-state'),'date-only plans count as visible calendar entries');return;
  }
  if(scenario==='known_clocks_sort_before_date_only'){
    const groups=context.agendaDateGroups([
      task(2,'2026-10-08T20:00:00+08:00'),task(1,'2026-10-08T09:00:00+08:00')
    ],[plan(1,'2026-10-08'),plan(2,'2026-10-08',{start_at:ts('2026-10-08T18:30:00+08:00')})],start);
    assert.deepEqual(structure(groups),[['2026-10-08',[['task',1],['plan',2],['task',2],['plan',1]]]]);return;
  }
  if(scenario==='summary_counts_keep_execution_separate'){
    const items=[task(1,'2026-10-07T10:00:00+08:00'),task(2,'2026-10-08T10:00:00+08:00'),task(3,'2026-10-08T12:00:00+08:00',{status:'completed'})];
    const html=context.agendaBoardHTML(agenda(items,[plan(1,'2026-10-07')],{planning_nodes:[{date:'2026-10-09',title:'Synthetic check only'}]}),items);
    const summary=html.slice(html.indexOf('aria-label="本时段事项数量"'),html.indexOf('</div>',html.indexOf('aria-label="本时段事项数量"')));
    assert(summary.includes('已安排 <strong>2</strong>'));assert(summary.includes('待约 / 待确认 <strong>1</strong>'));assert(summary.includes('日程已完成 <strong>1</strong>'));
    assert(!summary.includes('跟进已完成'),'calendar completion cannot claim the customer follow-up was completed');return;
  }
  if(scenario==='menus_close_outside_but_not_inside'){
    const inside={},outside={};const menu={open:true,contains:target=>target===inside};openMenus=[menu];
    for(const listener of listeners.get('click')||[])listener({target:inside});assert.equal(menu.open,true);
    for(const listener of listeners.get('click')||[])listener({target:outside});assert.equal(menu.open,false);return;
  }
  if(scenario==='escape_closes_menu_and_returns_focus'){
    let focusCount=0;const menu={open:true,querySelector:selector=>selector==='summary'?{focus:()=>focusCount++}:null};openMenus=[menu];
    for(const listener of listeners.get('keydown')||[])listener({key:'Enter'});assert.equal(menu.open,true);assert.equal(focusCount,0);
    for(const listener of listeners.get('keydown')||[])listener({key:'Escape'});assert.equal(menu.open,false);assert.equal(focusCount,1);return;
  }
  assert.fail('unknown agenda scenario '+scenario);
}
run().then(()=>process.stdout.write(JSON.stringify({passed:scenario}))).catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason="Node.js is required for the agenda renderer regression harness")
@pytest.mark.parametrize("scenario", [
    "chronological_combined_groups", "date_only_plan_never_midnight",
    "timed_unconfirmed_plan_keeps_clock", "pending_task_targets_and_optional_result",
    "completed_cancelled_task_guards", "cross_window_continuation_is_preserved",
    "task_without_record_has_no_dead_link", "project_attribution_remains_honest", "escaped_event_and_plan_text",
    "page_aggregation_does_not_repeat_plan", "planning_checks_are_not_calendar_events",
    "empty_day", "empty_week", "empty_month", "plans_only_are_events_not_empty",
    "known_clocks_sort_before_date_only", "summary_counts_keep_execution_separate",
    "menus_close_outside_but_not_inside", "escape_closes_menu_and_returns_focus",
])
def test_agenda_layout_frontend(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, str(ROOT), scenario], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"passed":' in result.stdout and scenario in result.stdout
