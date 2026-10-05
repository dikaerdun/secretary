"""Detail D1: execute delivered renderers with synthetic DTOs, then inspect trees.

These checks cover static reading/provenance only. They do not claim D3 submit
ownership, D4 navigation or real-browser viewport/soft-keyboard verification.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.argv[1],listeners=new Map(),calls=[];
const NOW=Date.parse('2026-10-06T10:00:00+08:00')/1000;
class FixedDate extends Date {static now(){return NOW*1000;}}
const context={Date:FixedDate,Math,Number,String,Boolean,JSON,Map,Set,Object,Array,Promise,URLSearchParams,Intl,
 state:{authenticated:true,csrf:'synthetic-detail-session',runtime:{mode:'local'},detail:null},
 document:{addEventListener(name,fn,capture){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push({fn,capture});}},
 icon:()=>'<svg aria-hidden="true"></svg>',formatDate:at=>`SYNTHETIC-TIME-${at}`,materialDate:at=>`SYNTHETIC-TIME-${at}`,
 dialogHeader:(title,subtitle)=>`<h2>${title}</h2><p>${subtitle}</p>`,
 recordProjectHTML:()=>'',opportunityLinkButton:()=>'<button data-link-project="101">核对项目归属</button>',
 matterContextHTML:()=>'',captureRecordDetailsHTML:()=>'',lifecycleActions:id=>`<button data-lifecycle-record="${id}">归档或恢复</button>`,
 flowAttachmentsHTML:items=>items.map(item=>`<button data-material="${item.id}">${item.name}</button>`).join(''),
 flowRestore:()=>{},restoreDrafts:()=>{},
 flowComposer:plan=>`<form id="flow-compose-${plan.id}" data-flow-form data-plan-id="${plan.id}" data-plan-revision="${plan.revision}"><div class="flow-current"><strong>${plan.title}</strong></div><textarea name="text" rows="3"></textarea><input type="hidden" name="request_id"><input type="hidden" name="request_text"><input type="hidden" name="attachment_items"><p class="form-error" role="alert"></p><button type="submit">告诉秘书</button></form>`,
 api:async(url,options={})=>{calls.push({url,method:options.method||'GET'});throw new Error('D1 rendering must never request '+url);}
};
vm.createContext(context);
const app=fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8');
for(const prefix of ['const escapeHTML =','const e =','const statuses =','const sources =']){
 const line=app.split('\n').find(line=>line.startsWith(prefix));assert(line,'production helper missing '+prefix);vm.runInContext(line,context);
}
for(const name of ['badge','recordStatus','actionTermsHTML','transcriptComparison','reminderWarnings']){
 const line=app.split('\n').find(line=>line.startsWith(`function ${name}(`));assert(line,'production helper missing '+name);vm.runInContext(line,context);
}
const originStart=app.indexOf('function actionOriginsHTML('),originEnd=app.indexOf('async function openActionDiscussionOrigin(',originStart);
assert(originStart>=0&&originEnd>originStart,'production origins renderer missing');vm.runInContext(app.slice(originStart,originEnd),context);
const flow=fs.readFileSync(path.join(root,'secretary/static/secretary-flow.js'),'utf8');
const identityStart=flow.indexOf('function flowIdentityHTML('),identityEnd=flow.indexOf('function flowTurnHTML(',identityStart);
assert(identityStart>=0&&identityEnd>identityStart,'production identity/preparation renderers missing');vm.runInContext(flow.slice(identityStart,identityEnd),context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/detail-progress.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/arrangement-queue.js'),'utf8'),context);
function freeze(value){if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;}
function readOnly(value,render){const before=JSON.stringify(value);freeze(value);const html=render(value);assert.equal(JSON.stringify(value),before,'renderer mutated authoritative DTO');return html;}
function action(extra={}){return {activities:[],...extra,record:{id:101,kind:'action',status:'following',source:'web',title:'合成待办标题<甲>',content:'合成待办目标',original_content:'原话提到10月9日，具体钟点未知',created_at:NOW,customer_id:7,customer_name:'合成客户',...extra.record}};}
function item(extra={}){return {plan_id:201,revision:3,title:'合成安排标题<乙>',settling_state:'pending',source_visible:true,customer_id:7,customer_name:'合成客户',decision_mode:'unknown',proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},blocking_reasons:['missing_clock'],can_apply:false,followup_enabled:true,...extra};}
function plan(extra={}){return {id:201,revision:3,title:'合成安排标题<乙>',record_id:301,question:'这次具体希望怎么推进？',turns:[],arrangement:item(),...extra};}
const schedule='<form id="schedule-form" data-record-id="101" data-schedule-snapshot="synthetic-seen"><label for="synthetic-schedule-time">计划时间</label><input id="synthetic-schedule-time" name="remind_at"><button type="submit">保存待确认安排</button></form><button data-next-visit="101">安排下一次拜访</button>';
const history=items=>items.map(item=>`<li><time>${item.created_at}</time><p>${item.content}</p></li>`).join('');
const output={};
output.action=readOnly(action(),data=>context.detailActionHTML(data,schedule,history(data.activities)));
const progressed=action({activities:[{created_at:NOW+10,content:'合成最新真实进展'},{created_at:NOW,content:'合成较旧真实进展'}],active_reminder:{id:501,status:'pending',remind_at:NOW+86400,record_id:101}});
output.progressed=readOnly(progressed,data=>context.detailActionHTML(data,schedule,history(data.activities)));
const tiedProgress=action({activities:[{id:901,created_at:NOW+10,content:'tie-old-901'},{id:903,created_at:NOW+10,content:'tie-new-903'},{id:902,created_at:NOW+10,content:'tie-mid-902'}],active_reminder:{id:501,status:'pending',remind_at:NOW+86400,record_id:101}});
output.tiedProgress=readOnly(tiedProgress,data=>context.detailActionHTML(data,schedule,history(data.activities)));
const origins={items:[{type:'record',record_id:301,title:'合成原记录'},
 {type:'discussion',title:'合成讨论',thread_id:11,message_id:12,user_text:'合成用户完整问题',answer_text:'合成AI建议而非客户承诺',adopted_at:NOW},
 {type:'progress',kind:'visit_prepare',run_id:'synthetic-run',scope:{customer_id:7},text:'合成准备稿完整输入',adopted_at:NOW}]};
const sourced=action({action_origins:origins,record:{parent_record_id:301},visit_ref:{id:401},material_ref:{id:402},transcript:{original_text:'合成首次转写',corrected_text:'合成人工核对转写'}});
output.sources=readOnly(sourced,data=>context.detailActionHTML(data,schedule,history(data.activities)));
const warned=action({record_nature:{needs_review:true},action_origins:{items:[{type:'discussion',title:'合成旧讨论',thread_id:11,message_id:12,scope_needs_review:true}]},warnings:['合成必须保留的时间冲突']});
output.warning=readOnly(warned,data=>context.detailActionHTML(data,schedule,''));
const turns=Array.from({length:7},(_,i)=>({id:700+i,created_at:NOW+i,text:`合成历史原话-${i}`,reply:i===0?'旧历史身份追问-需核对哪位李总':`合成历史回复-${i}`}));
const historic=plan({turns,goal:'合成交流目标',preparation:{objective:'合成准备建议'},arrangement_history:[{operation:'set_check',created_at:NOW+20,receipt:'合成结构操作回执'}]});
output.arrangement=readOnly(historic,context.arrangementDetailHTML);
output.list=readOnly(item(),context.arrangementCardHTML);
const old=plan({arrangement:item({decision_mode:'external',active_schedule:{id:501,status:'pending',remind_at:NOW+86400,revision:4},proposed_execution:{time_spec:{precision:'instant',at:NOW+2*86400}},blocking_reasons:[{code:'schedule_conflict',message:'合成完整冲突说明不可折叠'}],attention_flags:['check_after_deadline']})});
output.conflict=readOnly(old,context.arrangementDetailHTML);
output.hidden=readOnly(plan({arrangement:item({source_visible:false,source_hidden:true,candidates:[{id:'candidate-a',time_spec:{precision:'date',date:'2026-10-09'}}]})}),context.arrangementDetailHTML);

// Run the actual initialization against explicit source nodes. D1 does not
// require a new submit router; the legacy primary button remains in its form.
function source(dataset){return {dataset,removed:false,remove(){this.removed=true;}};}
const sourceNodes=[source({record:'301'}),source({record:'301'}),source({rawRecord:'301'}),source({rawRecord:'301'}),source({rawRecord:'302'}),source({visit:'401'}),source({visit:'401'}),source({material:'402'}),source({material:'402'}),source({originDiscussion:'11',originMessage:'12'}),source({originDiscussion:'11',originMessage:'12'}),source({originDiscussion:'11',originMessage:'13'}),source({originProgress:'{"kind":"visit_prepare","run_id":901}'}),source({originProgress:'{"run_id":901,"kind":"visit_prepare"}'})];
const primary={classes:new Set(),classList:{add(value){primary.classes.add(value);}}},footer={attrs:{},setAttribute(name,value){this.attrs[name]=value;}},title={scrollHeight:90,clientHeight:40,classes:new Set(),classList:{toggle(name,on){if(on)title.classes.add(name);else title.classes.delete(name);}}};
const expand={hidden:null,attrs:{'aria-expanded':'false'},textContent:'展开标题',getAttribute(name){return this.attrs[name];},setAttribute(name,value){this.attrs[name]=value;},closest(){return shell;}};
const shell={dataset:{detailSubject:'action',detailId:'101'},querySelector(selector){return {'[data-detail-primary]':footer,'[data-detail-zone="C"] form':{querySelector:()=>primary},'.detail-progress-title':title,'[data-detail-title-expand]':expand}[selector]||null;},querySelectorAll(){return sourceNodes;}};
context.initializeDetailShell({querySelector:()=>shell});
const listenerCount=Array.from(listeners.values()).reduce((sum,value)=>sum+value.length,0);
context.initializeDetailShell({querySelector:()=>shell});
assert.equal(Array.from(listeners.values()).reduce((sum,value)=>sum+value.length,0),listenerCount,'initialization must not add duplicate handlers');
output.initialized={sources:sourceNodes.map(node=>({dataset:node.dataset,removed:node.removed})),primaryClasses:[...primary.classes],footerAttrs:footer.attrs,titleHidden:expand.hidden};
const titleListener=listeners.get('click').find(listener=>!listener.capture).fn;
titleListener({target:{closest:()=>expand}});const expanded={aria:expand.attrs['aria-expanded'],classes:[...title.classes],label:expand.textContent};
titleListener({target:{closest:()=>expand}});output.titleToggle={expanded,collapsed:{aria:expand.attrs['aria-expanded'],classes:[...title.classes],label:expand.textContent}};
const projected={};
function project(key,ref,dto){
 projected[key]=readOnly(dto,data=>{const view=context.buildDetailViewModel(ref,data),before=JSON.stringify(view);freeze(view);const focused=context.resolveDetailFocus(view);assert.equal(JSON.stringify(view),before,'focus resolution changed view/DTO');assert.equal(JSON.stringify(focused),JSON.stringify(view.focus),'focus resolution must be deterministic');return {subjectRef:view.subjectRef,visible:view.visible,codes:view.codes,focus:view.focus,choices:context.detailPurposeOptions(view).map(([value])=>value),scheduleAuthority:view.scheduleAuthority,scheduleConflict:view.scheduleConflict,plans:view.plans.map(value=>Number(value.id||value.plan_id)),baseline:view.baseline,entity:view.entity};});
}
function projectPlan(key,extra={},outer={}){project(key,{type:'arrangement',id:201},plan({...outer,arrangement:item(extra)}));}
const future={precision:'instant',date:'2026-10-09',at:NOW+3*86400};
const complete={decision_mode:'self',settlement_scope:'execution_time',proposed_execution:{time_spec:future},agreement_status:'not_required',agreement_missing_fields:[],application_authority:{kind:'none'},blocking_reasons:['user_review_required'],can_apply:false};
const candidateA={id:'a',time_spec:{precision:'date',date:'2026-10-09'}},candidateB={id:'b',time_spec:{precision:'date',date:'2026-10-10'}};
project('action_default',{type:'action',id:101},action());
project('action_done',{type:'action',id:101},action({record:{status:'done'},completion_snapshot:'synthetic-completion',schedule_snapshot:'synthetic-schedule'}));
project('action_hidden',{type:'action',id:101},action({record:{hidden:1}}));
project('action_readonly',{type:'action',id:101},action({readonly:true}));
project('action_archived',{type:'action',id:101},action({visibility:'archived'}));
projectPlan('missing_clock_review',{blocking_reasons:['missing_clock','user_review_required']});
projectPlan('unknown_mode',{blocking_reasons:['decision_mode_unknown','missing_clock','user_review_required']});
projectPlan('unknown_date_intent',{...complete,date_intent:'unknown',blocking_reasons:['user_review_required']});
projectPlan('identity_first',{...complete,candidates:[candidateA,candidateB],blocking_reasons:[{code:'identity_review'},'schedule_conflict','candidate_selection','missing_clock']});
projectPlan('conflict_first',{...complete,candidates:[candidateA,candidateB],blocking_reasons:['schedule_conflict','candidate_selection','missing_clock'],active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}});
projectPlan('candidates_before_clock',{...complete,candidates:[candidateA,candidateB],selected_candidate_id:null,blocking_reasons:['candidate_selection','missing_clock','user_review_required']});
projectPlan('selected_waiting',{...complete,decision_mode:'external',candidates:[candidateA,candidateB],selected_candidate_id:'a',agreement_status:'pending',agreement_missing_fields:['time'],waiting_for:{text:'合成对方钟点回复'},blocking_reasons:['waiting_for_agreement','user_review_required']});
projectPlan('waiting_missing_clock',{...complete,decision_mode:'external',candidates:[candidateA,candidateB],selected_candidate_id:'a',agreement_status:'pending',agreement_missing_fields:['time'],waiting_for:{text:'still awaiting current proposal'},proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},blocking_reasons:['waiting_for_agreement','missing_clock','user_review_required']});
projectPlan('waiting_missing_location',{...complete,decision_mode:'external',candidates:[candidateA,candidateB],selected_candidate_id:'a',agreement_status:'pending',agreement_missing_fields:['place'],waiting_for:{text:'still awaiting current proposal'},blocking_reasons:['waiting_for_agreement','missing_location','user_review_required']});
projectPlan('review_user',complete);
projectPlan('review_changed_authority',{...complete,blocking_reasons:['authority_changed']});
projectPlan('review_external_complete',{...complete,decision_mode:'external',agreement_status:'agreed',agreement_missing_fields:[],agreement:{status:'reported',fields:{date:{evidence:'合成对方同意日期',signature:'date-signature'},time:{evidence:'合成对方同意钟点',signature:'time-signature'}}}});
projectPlan('review_missing_time',{...complete,proposed_execution:null});
projectPlan('review_missing_intent',{...complete,decision_mode:undefined});
projectPlan('review_missing_external_evidence',{...complete,decision_mode:'external',agreement_status:undefined,agreement_missing_fields:undefined,agreement:undefined});
projectPlan('review_stale_informational_waiting',{...complete,decision_mode:'external',agreement_status:'agreed',waiting_for:{text:'合成旧说明仍等答复'}});
projectPlan('settled_complete',{...complete,settling_state:'settled',can_apply:true,blocking_reasons:['coordination_inactive','missing_clock'],active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}});
projectPlan('settled_date',{...complete,settling_state:'settled',settlement_scope:'date_only',proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},blocking_reasons:['missing_clock','coordination_inactive']});
projectPlan('settled_date_old_schedule',{...complete,settling_state:'settled',settlement_scope:'date',proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},blocking_reasons:['missing_clock','schedule_conflict'],active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}});
projectPlan('paused',{...complete,settling_state:'paused',followup_dirty:true,blocking_reasons:['missing_clock','user_review_required'],active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}});
projectPlan('paused_no_schedule',{...complete,settling_state:'paused',active_schedule:null,blocking_reasons:['missing_clock','user_review_required']});
projectPlan('abandoned',{...complete,settling_state:'abandoned',blocking_reasons:['missing_clock','user_review_required']});
projectPlan('hidden_paused',{...complete,settling_state:'paused',source_visible:false,blocking_reasons:['missing_clock']});
projectPlan('hidden_settled',{...complete,settling_state:'settled',source_hidden:true,blocking_reasons:['missing_clock']});
projectPlan('readonly_arrangement',complete,{readonly:true});
projectPlan('settled_preparation',{...complete,settling_state:'settled',followup_dirty:true,blocking_reasons:['missing_materials','missing_clock'],active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}},{preparation:{materials:['合成未完成准备资料']}});
const linked=plan({arrangement:item({active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400}})});
const legacy={id:601,status:'pending',task_id:null,target_task_id:501,title:'合成旧提案',remind_at:NOW+86400};
project('legacy_authority',{type:'action',id:101},action({proposal:legacy,task:{id:501,status:'pending'},completion_snapshot:'synthetic-completion',schedule_snapshot:'synthetic-schedule',record:{terms_updated_at:17}}));
const linkedAction=action({secretary_plan:linked,proposal:legacy,task:{id:501,status:'pending'},completion_snapshot:'synthetic-completion',schedule_snapshot:'synthetic-schedule',record:{terms_updated_at:17}});
project('single_plan_authority',{type:'action',id:101},linkedAction);
project('duplicate_plan_authority',{type:'action',id:101},action({secretary_plan:linked,secretary_plans:[linked],proposal:legacy}));
const other=plan({id:202,title:linked.title,arrangement:item({plan_id:202,title:linked.title,active_schedule:{id:502,status:'pending',revision:6,remind_at:NOW+2*86400}})});
const multipleAction=action({secretary_plans:[linked,other]});
project('multiple_plan_authority',{type:'action',id:101},multipleAction);
const mismatchedAction=action({secretary_plan:linked,proposal:{...legacy,target_task_id:502},task:{id:502,status:'pending'}});
project('mismatched_plan_authority',{type:'action',id:101},mismatchedAction);
const unlinkedAction=action({secretary_plan:linked,proposal:{...legacy,task_id:null,target_task_id:null},task:null});
project('unverified_plan_authority',{type:'action',id:101},unlinkedAction);
projectPlan('three_times',{...complete,settle_deadline:{strength:'required',time_spec:{precision:'date',date:'2026-10-08'}},next_check:{check_id:'check-x',status:'planned',time_spec:{precision:'window',date:'2026-10-07',window:'上午',start_at:NOW+86400}},active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+10*86400}});
projectPlan('no_execution_reminder',{...complete,settling_state:'settled',execution_reminder:{mode:'none',notification_at:null},active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400},followup_enabled:false,followup_disable_reason:'settled'});
output.projected=projected;
output.authorityRenders={single:context.detailActionHTML(linkedAction,schedule,''),multiple:context.detailActionHTML(multipleAction,schedule,''),mismatched:context.detailActionHTML(mismatchedAction,schedule,''),unverified:context.detailActionHTML(unlinkedAction,schedule,''),hidden:context.detailActionHTML(action({record:{hidden:1}}),schedule,'')};
output.followupRenders={first:context.arrangementControlsHTML(item({followup_enabled:false,followup_disable_reason:'legacy_no_opt_in'})),disabled:context.arrangementControlsHTML(item({followup_enabled:false,followup_disable_reason:'user_disabled'})),visibleRestored:context.arrangementControlsHTML(item({followup_enabled:false,followup_disable_reason:'source_hidden',source_visible:true})),settled:context.arrangementControlsHTML(item({settling_state:'settled',settlement_scope:'execution_time',followup_enabled:false,followup_disable_reason:'settled'}))};
output.reminderOff=readOnly(plan({arrangement:item({settling_state:'settled',execution_reminder:{mode:'none',notification_at:null},active_schedule:{id:501,status:'pending',revision:5,remind_at:NOW+86400},followup_enabled:false,followup_disable_reason:'settled'})}),context.arrangementDetailHTML);
output.hiddenLinked=readOnly(action({secretary_plan:linked,record:{hidden:1}}),data=>context.detailActionHTML(data,schedule,''));
output.readonlyArrangement=readOnly(plan({readonly:true,arrangement:item({candidates:[candidateA,candidateB]})}),context.arrangementDetailHTML);
output.refErrors=[];
for(const ref of [{type:'customer',id:101},{type:'action',id:0},{type:'action',id:1.5},{type:'action',id:102},{type:'arrangement',id:202}]){try{context.buildDetailViewModel(ref,ref.type==='arrangement'?plan():action());output.refErrors.push(false);}catch{output.refErrors.push(true);}}
output.apiCalls=calls;
process.stdout.write(JSON.stringify(output));
'''


class _Element:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    @property
    def text(self):
        return "".join(child if isinstance(child, str) else child.text for child in self.children)

    def elements(self):
        for child in self.children:
            if isinstance(child, _Element):
                yield child
                yield from child.elements()

    def find_all(self, *, tag=None, attr=None, value=None):
        return [node for node in self.elements()
                if (tag is None or node.tag == tag)
                and (attr is None or attr in node.attrs)
                and (value is None or node.attrs.get(attr) == value)]


class _TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Element("root")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@pytest.fixture(scope="module")
def delivered_detail_renderings():
    if not NODE:
        pytest.skip("Node.js is required to execute production detail renderers")
    result = subprocess.run(
        [NODE, "-e", "eval(require('node:fs').readFileSync(0,'utf8'))", str(ROOT)],
        input=HARNESS, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _tree(renderings, key):
    return _TreeParser(renderings[key]).root


def _zone(tree, name):
    matches = tree.find_all(attr="data-detail-zone", value=name)
    assert len(matches) == 1, f"expected exactly one {name} zone"
    return matches[0]


@pytest.mark.parametrize("kind,title", [("action", "合成待办标题<甲>"), ("arrangement", "合成安排标题<乙>")])
def test_d1_five_zones_and_single_primary_title(delivered_detail_renderings, kind, title):
    """DD01–03: shared reading order; title remains in its original header."""
    tree = _tree(delivered_detail_renderings, kind)
    assert [node.attrs["data-detail-zone"] for node in tree.find_all(attr="data-detail-zone")] == list("ABCDE")
    titles = [node for node in tree.find_all(tag="h2") if node.text == title]
    assert len(titles) == 1
    assert titles[0] in list(_zone(tree, "A").elements())
    for name in ("B", "C"):
        assert not any(node.tag == "details" for node in _zone(tree, name).elements())
    for disclosure in _zone(tree, "E").find_all(tag="details"):
        assert "open" not in disclosure.attrs
    assert _zone(tree, "C").find_all(tag="form")


def test_d1_list_snapshot_keeps_its_own_title(delivered_detail_renderings):
    """DD02: detail mode cannot remove the list title from the shared snapshot."""
    headings = _tree(delivered_detail_renderings, "list").find_all(tag="h3")
    assert [node.text for node in headings].count("合成安排标题<乙>") == 1
    assert not _tree(delivered_detail_renderings, "arrangement").find_all(tag="h3", attr="id", value="detail-title")


def test_d1_current_action_uses_latest_saved_progress(delivered_detail_renderings):
    """DD04: latest activity is authoritative even when the DTO list is unsorted."""
    current = _zone(_tree(delivered_detail_renderings, "progressed"), "B")
    assert "合成最新真实进展" in current.text
    assert "合成较旧真实进展" not in current.text
    assert "当前有效日程" in current.text and "待执行" in current.text
    assert "SYNTHETIC-TIME-" in current.text


def test_latest_progress_with_equal_timestamps_uses_highest_id_and_keeps_effective_schedule(delivered_detail_renderings):
    tree = _tree(delivered_detail_renderings, "tiedProgress")
    current = _zone(tree, "B").text
    assert "tie-new-903" in current
    assert "tie-old-901" not in current and "tie-mid-902" not in current
    assert "当前有效日程" in current and "待执行" in current
    assert "SYNTHETIC-TIME-1791338400" in current
    history = _zone(tree, "E").text
    assert all(content in history for content in ["tie-old-901", "tie-new-903", "tie-mid-902"])


def test_d1_original_date_and_unknown_clock_are_not_effective_schedule(delivered_detail_renderings):
    """DD04: raw source dates never become a confirmed appointment in B."""
    action = _tree(delivered_detail_renderings, "action")
    current = _zone(action, "B").text
    assert "原话" in current and "核对" in current
    assert "当前有效日程" not in current and "已加入日程" not in current
    assert "SYNTHETIC-TIME-" not in current
    assert "10月9日" in _zone(action, "E").text
    arrangement = _zone(_tree(delivered_detail_renderings, "arrangement"), "B")
    assert "2026-10-09" in arrangement.text and "钟点待定" in arrangement.text
    assert "当前有效安排" not in arrangement.text
    assert "SYNTHETIC-TIME-" not in arrangement.text


def test_d1_source_conflicts_are_kept_in_current_situation(delivered_detail_renderings):
    """DD05–06: current warnings stay visible while old questions remain history."""
    warning = _zone(_tree(delivered_detail_renderings, "warning"), "B").text
    assert "来源或归属有变化" in warning
    assert "合成必须保留的时间冲突" in warning
    arrangement = _tree(delivered_detail_renderings, "arrangement")
    assert "旧历史身份追问" not in _zone(arrangement, "B").text + _zone(arrangement, "C").text
    assert "旧历史身份追问" in _zone(arrangement, "E").text


def test_d1_sources_keep_original_discussion_preparation_and_adoption(delivered_detail_renderings):
    """DD06/08: all provenance remains reachable without duplicating analysis."""
    tree = _tree(delivered_detail_renderings, "sources")
    source = _zone(tree, "E")
    for sentinel in ("合成用户完整问题", "合成AI建议而非客户承诺", "合成准备稿完整输入", "合成首次转写", "合成人工核对转写"):
        assert sentinel in source.text
        assert sentinel not in _zone(tree, "B").text + _zone(tree, "C").text
    for route in ("data-record", "data-raw-record", "data-origin-discussion", "data-origin-progress", "data-visit", "data-material"):
        assert source.find_all(attr=route), route
    assert "采用于" in source.text and "AI 回复（建议）" in source.text
    assert not tree.find_all(attr="data-organize-record")


def test_d1_full_arrangement_history_is_present_once(delivered_detail_renderings):
    """DD07: complete turn/operation history lives only in E, without recent five."""
    tree = _tree(delivered_detail_renderings, "arrangement")
    for index in range(7):
        assert tree.text.count(f"合成历史原话-{index}") == 1
        assert f"合成历史原话-{index}" in _zone(tree, "E").text
        assert f"合成历史原话-{index}" not in _zone(tree, "B").text + _zone(tree, "C").text
    assert tree.text.count("合成结构操作回执") == 1
    assert "合成结构操作回执" in _zone(tree, "E").text
    assert _zone(tree, "C").find_all(attr="data-detail-receipt")


def test_d1_conflicts_keep_old_and_proposed_time_together(delivered_detail_renderings):
    """DD01/04: restructuring must retain full warnings and old valid execution."""
    current = _zone(_tree(delivered_detail_renderings, "conflict"), "B").text
    assert "当前有效安排" in current
    assert "原日程仍生效" in current and "拟改安排" in current
    assert "合成完整冲突说明不可折叠" in current
    assert "下一推进点晚于确定期限" in current


def test_d1_legacy_forms_and_maintenance_remain_reachable(delivered_detail_renderings):
    """DD46: D1 moves existing controls without inventing new write semantics."""
    action = _tree(delivered_detail_renderings, "action")
    activity = _zone(action, "C").find_all(tag="form", attr="id", value="activity-form")
    assert len(activity) == 1 and activity[0].attrs["data-record-id"] == "101"
    assert activity[0].find_all(tag="button", attr="data-organize-activity")
    assert _zone(action, "D").find_all(tag="form", attr="id", value="schedule-form")
    assert action.find_all(attr="data-next-visit", value="101")
    for route in ("data-complete-outcome", "data-secretary-progress", "data-edit-record", "data-lifecycle-record", "data-timeline-context"):
        assert len(action.find_all(attr=route)) == 1, route
    arrangement = _tree(delivered_detail_renderings, "arrangement")
    flow = _zone(arrangement, "C").find_all(tag="form", attr="data-flow-form")
    assert len(flow) == 1 and flow[0].attrs["data-plan-id"] == "201"
    for name in ("request_id", "request_text", "attachment_items"):
        assert flow[0].find_all(tag="input", attr="name", value=name)
    assert _zone(arrangement, "D").find_all(attr="data-arrangement-operation")


def test_d1_hidden_arrangement_preserves_read_only_boundary(delivered_detail_renderings):
    """Existing domain guard is retained; no new D2 lifecycle rules are asserted."""
    tree = _tree(delivered_detail_renderings, "hidden")
    assert "仅回看" in tree.text
    for route in ("data-flow-form", "data-arrangement-operation", "data-arrangement-select"):
        assert not tree.find_all(attr=route), route


def test_d1_source_dedup_preserves_route_and_target_identity(delivered_detail_renderings):
    """DD08: equal route/ID dedup; raw/associated and distinct messages survive."""
    sources = delivered_detail_renderings["initialized"]["sources"]
    surviving = [node["dataset"] for node in sources if not node["removed"]]
    assert {"record": "301"} in surviving
    assert {"rawRecord": "301"} in surviving
    assert {"rawRecord": "302"} in surviving
    assert {"originDiscussion": "11", "originMessage": "12"} in surviving
    assert {"originDiscussion": "11", "originMessage": "13"} in surviving
    assert len(surviving) == 8
    assert len(surviving) == len({json.dumps(item, sort_keys=True) for item in surviving})


def test_d1_long_title_expands_in_place_with_accessible_state(delivered_detail_renderings):
    """DD03/40 static seam; real focus, font/layout bounds remain D5 evidence."""
    initialized = delivered_detail_renderings["initialized"]
    assert initialized["titleHidden"] is False
    toggle = delivered_detail_renderings["titleToggle"]
    assert toggle["expanded"]["aria"] == "true"
    assert "expanded" in toggle["expanded"]["classes"]
    assert toggle["collapsed"]["aria"] == "false"
    assert "expanded" not in toggle["collapsed"]["classes"]


@pytest.mark.parametrize("kind", ["action", "arrangement"])
def test_d1_no_nested_forms_and_progress_error_region(delivered_detail_renderings, kind):
    """D1 compatibility guard, not the later one-active-purpose form contract."""
    tree = _tree(delivered_detail_renderings, kind)
    for form in tree.find_all(tag="form"):
        parent = form.parent
        while parent is not None:
            assert parent.tag != "form", "moving old forms must not nest them"
            parent = parent.parent
    current = _zone(tree, "C")
    assert current.find_all(attr="role", value="alert")
    assert current.find_all(attr="aria-live", value="polite")
    for label in current.find_all(tag="label", attr="for"):
        assert current.find_all(attr="id", value=label.attrs["for"])


def test_d1_render_and_initialization_make_no_requests(delivered_detail_renderings):
    assert delivered_detail_renderings["apiCalls"] == []


def test_d1_new_assets_are_referenced_once_after_script_dependencies():
    """DD48 static asset seam; authenticated HTTP/version response is separate."""
    tree = _TreeParser((ROOT / "secretary/static/index.html").read_text(encoding="utf-8")).root
    scripts = tree.find_all(tag="script", attr="src")
    paths = [node.attrs["src"].split("?", 1)[0] for node in scripts]
    assert paths.count("/static/detail-progress.js") == 1
    position = paths.index("/static/detail-progress.js")
    for dependency in ("app.js", "secretary-flow.js", "arrangement-queue.js"):
        assert paths.index(f"/static/{dependency}") < position, dependency
    assert "defer" in scripts[position].attrs
    styles = [node.attrs["href"].split("?", 1)[0] for node in tree.find_all(tag="link", attr="href")]
    assert styles.count("/static/detail-progress.css") == 1
    for filename in ("detail-progress.js", "detail-progress.css"):
        assert (ROOT / "secretary/static" / filename).is_file()


@pytest.mark.parametrize("case,purpose,operation", [
    ("action_default", "activity", None),
    ("action_done", "readonly", None),
    ("action_hidden", "readonly", None),
    ("action_readonly", "readonly", None),
    ("action_archived", "readonly", None),
    ("missing_clock_review", "supplement", None),
    ("unknown_mode", "supplement", None),
    ("unknown_date_intent", "supplement", None),
    ("identity_first", "supplement", None),
    ("conflict_first", "supplement", None),
    ("candidates_before_clock", "decision", "select_candidate"),
    ("selected_waiting", "supplement", None),
    ("review_user", "decision", "confirm_arrangement"),
    ("review_changed_authority", "decision", "confirm_arrangement"),
    ("review_external_complete", "decision", "confirm_arrangement"),
    ("review_missing_time", "supplement", None),
    ("review_missing_intent", "supplement", None),
    ("review_missing_external_evidence", "supplement", None),
    ("settled_complete", "progress", "update_progress"),
    ("settled_date", "decision", "continue_set_time"),
    ("settled_date_old_schedule", "decision", "continue_set_time"),
    ("paused", "decision", "resume"),
    ("paused_no_schedule", "decision", "resume"),
    ("abandoned", "decision", "start_reschedule"),
    ("hidden_paused", "readonly", None),
    ("hidden_settled", "readonly", None),
    ("readonly_arrangement", "readonly", None),
    ("settled_preparation", "progress", "update_progress"),
    ("no_execution_reminder", "progress", "update_progress"),
])
def test_d2_focus_prioritizes_visibility_lifecycle_and_current_blockers(delivered_detail_renderings, case, purpose, operation):
    """DD09–16/44–45: later pending rules cannot override stopped/settled states."""
    projected = delivered_detail_renderings["projected"][case]
    assert projected["focus"]["purpose"] == purpose, case
    assert projected["focus"].get("operation") == operation, case
    assert projected["focus"]["prompt"]


def test_d2_focus_names_specific_missing_information(delivered_detail_renderings):
    """DD09–12: concrete prompts, one purpose, no inferred authorization."""
    cases = delivered_detail_renderings["projected"]
    assert "几点" in cases["missing_clock_review"]["focus"]["prompt"]
    assert "对象" in cases["identity_first"]["focus"]["prompt"]
    assert "冲突" in cases["conflict_first"]["focus"]["prompt"]
    assert "选择" in cases["candidates_before_clock"]["focus"]["prompt"]
    assert "不代表对方" in cases["candidates_before_clock"]["focus"]["prompt"]
    assert "回复" in cases["selected_waiting"]["focus"]["prompt"]
    assert any(word in cases["unknown_date_intent"]["focus"]["prompt"] for word in ("日期", "时间", "哪天", "执行", "联系"))
    assert cases["review_user"]["entity"]["can_apply"] is False
    assert cases["review_user"]["entity"]["application_authority"]["kind"] == "none"


@pytest.mark.parametrize("case,authority,ids", [
    ("legacy_authority", "legacy_proposal", []),
    ("single_plan_authority", "plan", [201]),
    ("duplicate_plan_authority", "plan", [201]),
    ("multiple_plan_authority", "choose_plan", [201, 202]),
    ("mismatched_plan_authority", "needs_review", [201]),
    ("unverified_plan_authority", "needs_review", [201]),
])
def test_d2_time_authority_uses_exact_ids_and_requires_activity_choice(delivered_detail_renderings, case, authority, ids):
    """DD25–26: same titles cannot merge activities or override a conflicting task."""
    projected = delivered_detail_renderings["projected"][case]
    assert projected["scheduleAuthority"] == authority
    assert projected["plans"] == ids
    assert projected["subjectRef"] == {"type": "action", "id": 101}


def test_d2_plan_authority_removes_second_legacy_time_editor(delivered_detail_renderings):
    """DD25–26 read-only presentation; actual time writes remain a D3 gate."""
    renders = delivered_detail_renderings["authorityRenders"]
    for case in ("single", "multiple", "mismatched", "unverified"):
        tree = _TreeParser(renders[case]).root
        assert not tree.find_all(tag="form", attr="id", value="schedule-form"), case
        assert tree.find_all(attr="data-next-visit", value="101"), case
    single = _TreeParser(renders["single"]).root
    assert [node.attrs["data-arrangement-open"] for node in single.find_all(attr="data-arrangement-open")] == ["201"]
    multiple = _TreeParser(renders["multiple"]).root
    assert [node.attrs["data-arrangement-open"] for node in multiple.find_all(attr="data-arrangement-open")] == ["201", "202"]
    assert "选择这次活动" in _zone(multiple, "D").text
    for case in ("mismatched", "unverified"):
        assert "核对" in _zone(_TreeParser(renders[case]).root, "B").text


def test_d2_readonly_action_mounts_no_writable_form(delivered_detail_renderings):
    """DD16: hidden/read-only dominates even an available legacy schedule path."""
    tree = _TreeParser(delivered_detail_renderings["authorityRenders"]["hidden"]).root
    assert not tree.find_all(tag="form")


def test_d2_presentation_does_not_claim_nonexistent_old_schedule(delivered_detail_renderings):
    cases = delivered_detail_renderings["projected"]
    assert "原有效日程继续" not in cases["paused_no_schedule"]["focus"]["prompt"]
    assert "原有效日程继续" not in cases["abandoned"]["focus"]["prompt"]


def test_d2_time_values_and_version_baselines_remain_separate(delivered_detail_renderings):
    """DD24/44: time policy values are retained without becoming execution times."""
    cases = delivered_detail_renderings["projected"]
    projected = cases["three_times"]
    entity = projected["entity"]
    assert entity["settle_deadline"]["time_spec"]["date"] == "2026-10-08"
    assert entity["next_check"]["time_spec"]["date"] == "2026-10-07"
    assert entity["next_check"]["time_spec"]["precision"] == "window"
    assert "at" not in entity["next_check"]["time_spec"]
    assert entity["proposed_execution"]["time_spec"]["date"] == "2026-10-09"
    assert projected["baseline"] == {"plan_revision": 3, "task_revision": 5}
    assert cases["legacy_authority"]["baseline"] == {"completion_snapshot": "synthetic-completion", "schedule_snapshot": "synthetic-schedule", "terms_updated_at": 17}
    assert cases["single_plan_authority"]["baseline"] == cases["legacy_authority"]["baseline"]
    assert cases["no_execution_reminder"]["entity"]["active_schedule"]["status"] == "pending"
    assert cases["no_execution_reminder"]["entity"]["execution_reminder"]["mode"] == "none"
    assert "当前有效安排" in _zone(_tree(delivered_detail_renderings, "reminderOff"), "B").text


def test_d2_followup_wording_preserves_persistent_gate(delivered_detail_renderings):
    """DD43: first opt-in, explicit restoration, settled and visibility restoration."""
    renders = delivered_detail_renderings["followupRenders"]
    first = _TreeParser(renders["first"]).root.find_all(tag="button", attr="data-arrangement-operation", value="resume")
    assert len(first) == 1 and "启用" in first[0].text and "恢复" not in first[0].text
    for case in ("disabled", "visibleRestored"):
        buttons = _TreeParser(renders[case]).root.find_all(tag="button", attr="data-arrangement-operation", value="resume")
        assert len(buttons) == 1 and "恢复" in buttons[0].text
    assert not _TreeParser(renders["settled"]).root.find_all(attr="data-arrangement-operation", value="resume")
    assert delivered_detail_renderings["apiCalls"] == []


def test_d2_reference_identity_and_pure_projection_are_strict(delivered_detail_renderings):
    """Frozen inputs and repeated focus calls were checked inside production VM."""
    assert delivered_detail_renderings["refErrors"] == [True] * 5
    assert delivered_detail_renderings["apiCalls"] == []


def test_d2_current_authority_beats_stale_informational_waiting(delivered_detail_renderings):
    focus = delivered_detail_renderings["projected"]["review_stale_informational_waiting"]["focus"]
    assert focus["purpose"] == "decision" and focus["operation"] == "confirm_arrangement"


@pytest.mark.parametrize("case,missing_code,prompt", [
    ("waiting_missing_clock", "missing_clock", "具体几点还没定，可以直接补充时间和约定情况。"),
    ("waiting_missing_location", "missing_location", "请补清本次执行时间及必要信息；未知内容继续保留。"),
])
def test_waiting_and_missing_required_information_prompts_for_gap_before_reply_or_confirmation(delivered_detail_renderings, case, missing_code, prompt):
    projected = delivered_detail_renderings["projected"][case]
    assert projected["codes"] == ["waiting_for_agreement", missing_code, "user_review_required"]
    assert projected["focus"] == {"purpose": "supplement", "label": "提交补充", "prompt": prompt}
    assert "decision:confirm_arrangement" not in projected["choices"]
    assert "回复" not in prompt and "落实" not in prompt
    assert projected["entity"]["can_apply"] is False
    assert projected["entity"]["agreement_status"] == "pending"
    assert projected["entity"]["waiting_for"] == {"text": "still awaiting current proposal"}
    assert projected["entity"]["application_authority"] == {"kind": "none"}
    assert projected["entity"]["selected_candidate_id"] == "a"
    assert delivered_detail_renderings["apiCalls"] == []


def test_d2_readonly_boundary_includes_related_controls(delivered_detail_renderings):
    action_tree = _tree(delivered_detail_renderings, "hiddenLinked")
    assert not action_tree.find_all(attr="data-next-visit")
    assert not action_tree.find_all(tag="form")
    arrangement_tree = _tree(delivered_detail_renderings, "readonlyArrangement")
    for attr in ("data-arrangement-operation", "data-arrangement-select", "data-flow-discussion-plan"):
        assert not arrangement_tree.find_all(attr=attr), attr
    assert not arrangement_tree.find_all(tag="form")
