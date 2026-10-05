"""Real project UI functions and navigation, with a controlled VM transport."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=process.argv[1],scenario=process.argv[2],app=fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8'),calls=[],responses=[];
const state={view:'records',authenticated:true,request:1,page:1,q:'',workQueue:'',matterRawRecords:false,detailLoadGeneration:0};
let loads=0;
const nodes={'#global-search':{value:''},'#breadcrumb-view':{textContent:''}};
const context={state,Number,String,Boolean,Array,Object,Date,Map,Set,Promise,JSON,Math,URLSearchParams,Intl,
 document:{addEventListener:()=>{},title:''},
 $:selector=>nodes[selector],$$:()=>[],
 labels:{dashboard:'工作台',records:'跟进事项',overview:'总览'},
 stages:{},money:value=>String(value),
 invalidateDiscussionIntent:()=>{},stopMaterialPoll:()=>{},stopVisitPoll:()=>{},stopCapturePoll:()=>{},stopDiscussionPoll:()=>{},stopCaptureDetailPoll:()=>{},rememberMaterial:()=>{},rememberVisit:()=>{},
 loadView:async()=>{loads++;},icon:()=>'<svg></svg>',formatDate:()=> '虚构时间',
 api:async(url)=>{calls.push(url);assert(responses.length,'unexpected request '+url);const response=responses.shift();if(response instanceof Error)throw response;return typeof response==='function'?response():response;}
};
vm.createContext(context);
for(const line of app.split('\n').filter(line=>line.startsWith('const escapeHTML =')||line.startsWith('const e =')||line.startsWith('function heading(')||line.startsWith('function badge(')))vm.runInContext(line,context);
function loadFunction(start,end){const begin=app.indexOf(start),finish=app.indexOf(end,begin+start.length);assert(begin>=0&&finish>begin);vm.runInContext(app.slice(begin,finish),context);}
loadFunction('function overviewProjectMatterHTML(', 'function overviewProjectHTML(');
loadFunction('function overviewProjectHTML(', 'async function renderOverview(');
loadFunction('async function renderProjectMatters(', 'async function renderRecords(');
loadFunction('async function navigate(', 'function dialogHeader(');
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/matters.js'),'utf8'),context);
const clickStart=app.indexOf("    if(button.hasAttribute('data-project-matters'))"),clickEnd=app.indexOf('    if(await dispatchFeatureAction(button))',clickStart);
assert(clickStart>0&&clickEnd>clickStart);
vm.runInContext('async function projectClick(button){'+app.slice(clickStart,clickEnd)+'}',context);
const project={id:7,customer_id:3,name:'签名<试点>"项目'},matter={id:21,title:'准备<签名>材料',objective:'确认"汇报"范围',status:'following',visibility:'active',next_action:{title:'整理<产品>材料'},action_count:2,completed_action_count:1,customer_name:'虚构单位',project_name:'虚构项目'};
function button(dataset){return {dataset,hasAttribute:name=>Object.prototype.hasOwnProperty.call(dataset,name.replace(/^data-/,'').replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()))};}
async function run(){
 if(scenario==='project_card_goals_status_next_step_and_escape'){
   const html=context.overviewProjectMatterHTML({...project,matters:{items:[matter,{...matter,id:22,status:'waiting'},{...matter,id:23,status:'ended',title:'已结束目标'}],total:3,summary:{following:1,waiting:1,ended:1}}});
   assert(html.includes('跟进事项 · 3'));assert(html.includes('1 件跟进中 · 1 件等待反馈'));
   assert(html.includes('data-matter-open="21"'));assert(html.includes('目标：确认&quot;汇报&quot;范围'));assert(html.includes('下一步：整理&lt;产品&gt;材料'));
   assert(html.includes('等待反馈'));assert(html.includes('下一步：这件事已结束'));
   assert(html.includes('data-project-matters="7"'));assert(html.includes('data-project-customer="3"'));assert(html.includes('data-project-name="签名&lt;试点&gt;&quot;项目"'));
   assert(!html.includes('准备<签名>材料'));
   const empty=context.overviewProjectMatterHTML(project);assert(empty.includes('本项目还没有归组事项'));assert(empty.includes('已有原话和行动继续保留'));
   const grouped=context.overviewProjectHTML({...project,progress:{done:0,total:0},matters:{items:[matter],total:1,summary:{active:1,following:1,action_count:2,completed_action_count:1,schedule_count:1}}});
   assert(grouped.includes('已归组动作：1 / 2 已完成 · 1 件事项'));assert(grouped.includes('1 件仍在跟进 · 1 项已确认安排'));
   const folded=grouped.match(/<details class="project-progress">[\s\S]*?<\/details>/)[0];assert(folded.includes('原项目行动明细'));assert(folded.includes('行动进展：0 / 0'));assert(!folded.includes(' open'));
   assert(!grouped.replace(folded,'').includes('行动进展：0 / 0'));assert(!grouped.replace(folded,'').includes('暂无有效日程'));
   const legacy=context.overviewProjectHTML({...project,progress:{done:1,total:3},open_actions:2});assert(legacy.includes('行动进展：1 / 3'));assert(!legacy.includes('已归组动作'));assert(!legacy.includes('原项目行动明细'));
 }
 else if(scenario==='project_scoped_search_and_pagination_request'){
   state.matterProject=project;state.page=2;state.q='电力 / 材料';responses.push({items:[matter],total:21,page:2,pages:2});
   const html=await context.renderProjectMatters(),params=new URL('http://test'+calls[0]).searchParams;
   assert.equal(params.get('customer_id'),'3');assert.equal(params.get('opportunity_id'),'7');assert.equal(params.get('page'),'2');assert.equal(params.get('q'),'电力 / 材料');
   assert(html.includes('签名&lt;试点&gt;&quot;项目 · 跟进事项'));assert(html.includes('data-matter-open="21"'));assert(html.includes('共 21 件事'));assert(html.includes('data-project-matter-back'));assert(!html.includes('全部客户'));
   responses.push({items:[],total:0,page:2,pages:2});assert((await context.renderProjectMatters()).includes('没有匹配的事项'));
   responses.push(new Error('连接暂时失败'));await assert.rejects(()=>context.renderProjectMatters(),/连接暂时失败/);
 }
 else if(scenario==='project_navigation_and_global_reset'){
   state.q='旧搜索';state.page=5;state.matterRawRecords=true;
   await context.projectClick(button({projectMatters:'7',projectCustomer:'3',projectName:project.name}));
   assert.equal(state.view,'records');assert.equal(state.matterProject.id,7);assert.equal(state.matterProject.customer_id,3);assert.equal(state.matterRawRecords,false);assert.equal(state.page,1);assert.equal(state.q,'');assert.equal(loads,1);
   await context.projectClick(button({projectMatterBack:''}));assert.equal(state.view,'overview');assert.equal(state.overviewTab,'projects');assert.equal(state.matterProject,null);assert.equal(loads,2);
   await context.navigate('records',{matterProject:project});state.q='全局关键词';await context.navigate('records',{keepSearch:true});assert.equal(state.matterProject,null);assert.equal(state.q,'全局关键词');
   await context.navigate('records',{matterProject:project,workQueue:'my_actions'});assert.equal(state.matterProject,null);assert.equal(state.workQueue,'my_actions');
 }
 else if(scenario==='project_last_page_clamp_keeps_scope_and_stale_reply_does_not_navigate'){
   state.matterProject=project;state.page=2;state.q='材料';responses.push({items:[],total:20,page:2,pages:1},{items:[matter],total:20,page:1,pages:1});
   const html=await context.renderProjectMatters();assert.equal(state.page,1);assert.equal(calls.length,2);assert(html.includes('data-matter-open="21"'));assert(!html.includes('没有匹配的事项'));
   const second=new URL('http://test'+calls[1]).searchParams;assert.equal(second.get('page'),'1');assert.equal(second.get('opportunity_id'),'7');assert.equal(second.get('q'),'材料');
   let resolve;state.page=2;responses.push(()=>new Promise(r=>resolve=r));const pending=context.renderProjectMatters();state.matterProject={...project,id:8};state.request++;resolve({items:[],total:20,page:2,pages:1});await pending;assert.equal(state.page,2);assert.equal(calls.length,3);assert.equal(state.matterProject.id,8);
 }
 else throw new Error('unknown scenario '+scenario);
}
run().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
'''


@pytest.mark.skipif(not NODE, reason='Node.js required for production browser functions')
@pytest.mark.parametrize('scenario', [
    'project_card_goals_status_next_step_and_escape',
    'project_scoped_search_and_pagination_request',
    'project_navigation_and_global_reset',
    'project_last_page_clamp_keeps_scope_and_stale_reply_does_not_navigate',
])
def test_matter_project_frontend(scenario):
    result = subprocess.run([NODE, '-e', HARNESS, str(ROOT), scenario],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
