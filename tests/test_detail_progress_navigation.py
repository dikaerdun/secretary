"""D4: typed navigation, local refresh and late-result isolation.

These use the D3 semantic DOM with production scripts. Focus and selection
identity are modeled; visible viewport and soft-keyboard behavior need a browser.
"""
import pytest

from test_detail_progress_events import run_event_scenario


def test_partial_refresh_keeps_current_form_nodes_focus_files_folds_and_old_cas():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','刷新中保留的原版本文字');const text=form.elements.namedItem('text');text.focus();text.setSelectionRange(2,6);const file=form.querySelector('input[type="file"]');file.files=[{name:'刷新中保留的文件.txt',size:12,type:'text/plain'}];set(form,'attachment_items',JSON.stringify([{material_id:701,filename:'已上传的材料.pdf',parse_status:'ready'}]));root.querySelector('[data-arrangement-section="support"]').open=true;root.querySelector('[data-arrangement-section="history"]').open=true;root.querySelector('.detail-progress-scroll').scrollTop=160;const beforeBaseline={...root.detailSession.baseline};planDTO=plan({revision:5,title:'最新安排摘要',blocking_reasons:['missing_clock','missing_location']},{revision:5});await run("refreshDetailSubject({type:'arrangement',id:201})");console.log(JSON.stringify({...observed(),sameForm:root.detailSession.form===form,sameText:root.detailSession.form.elements.namedItem('text')===text,focus:document.activeElement===text,selection:[text.selectionStart,text.selectionEnd],files:file.files.map(n=>n.name),values:run('formValues(__root.detailSession.form)'),supportOpen:root.querySelector('[data-arrangement-section="support"]').open,historyOpen:root.querySelector('[data-arrangement-section="history"]').open,scroll:root.querySelector('.detail-progress-scroll').scrollTop,baseline:root.detailSession.baseline,beforeBaseline,revision:form.dataset.planRevision,latestRevision:root.detailSession.vm.baseline.plan_revision,notice:root.querySelector('[data-detail-draft-notice]').textContent}));
""")
    assert result["writes"] == [] and result["uploads"] == []
    assert result["sameForm"] and result["sameText"] and result["focus"]
    assert result["selection"] == [2, 6]
    assert result["files"] == ["刷新中保留的文件.txt"]
    assert result["values"]["text"] == "刷新中保留的原版本文字"
    assert result["supportOpen"] and result["historyOpen"] and result["scroll"] == 160
    assert result["baseline"] == result["beforeBaseline"]
    assert result["revision"] == "3" and result["latestRevision"] == 5
    assert "核对" in result["notice"]


@pytest.mark.parametrize("change", ["close", "object", "logout", "reauth"])
def test_late_activity_response_cannot_reopen_or_mark_a_different_session(change):
    result = run_event_scenario("""
const root=mount(),form=choose(root,'activity');input(form,'content','迟到保存的合成进展');writeGate=defer();submit(form);await started();
const mode=CHANGE;
let newRoot=null,newForm=null;
if(mode==='close')document.querySelector('#detail-dialog').open=false;
if(mode==='object'){newRoot=mount('arrangement');newForm=choose(newRoot,'supplement');input(newForm,'text','新对象正在编辑');}
if(mode==='logout'){state.authenticated=false;state.csrf='';}
if(mode==='reauth'){state.csrf='new-synthetic-session';newRoot=mount('arrangement');newForm=choose(newRoot,'supplement');input(newForm,'text','新会话正在编辑');}
writeGate.resolve();await drain();console.log(JSON.stringify({...observed(),dialogOpen:document.querySelector('#detail-dialog').open,currentType:state.detail?.type,currentId:state.detail?.id,newText:newForm?.text.value,newReceipt:newRoot?.querySelector('[data-detail-receipt]').textContent,oldText:form.elements.namedItem('content').value,authenticated:state.authenticated,csrf:state.csrf}));
""".replace("CHANGE", repr(change)))
    assert len(result["writes"]) == 1
    assert result["writes"][0]["url"] == "/api/records/101/activities"
    if change == "close":
        assert result["dialogOpen"] is False
    if change in {"object", "reauth"}:
        assert result["currentType"] == "arrangement" and result["currentId"] == 201
        assert result["newText"] == ("新会话正在编辑" if change == "reauth" else "新对象正在编辑")
        assert result["newReceipt"] == ""
    if change == "logout":
        assert result["authenticated"] is False and result["csrf"] == ""


@pytest.mark.parametrize("change", ["close", "object", "reauth"])
def test_late_local_read_cannot_replace_current_detail_summary(change):
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','原安排未提交文字');const readGate=defer(),base=run('api');context.__delayedRead=async(url,options={})=>{if(url==='/api/secretary/plans/201'&&!options.method){await readGate.promise;return {plan:plan({revision:9,title:'旧对象迟到摘要'},{revision:9})};}return base(url,options);};run('api=__delayedRead');const refresh=run("refreshDetailSubject({type:'arrangement',id:201})");await Promise.resolve();let current=null;
if(CHANGE==='close')document.querySelector('#detail-dialog').open=false;
else {if(CHANGE==='reauth')state.csrf='new-synthetic-session';current=mount('action');input(current.detailSession.form,'content','当前对象新草稿');}
readGate.resolve();await refresh;await drain();console.log(JSON.stringify({...observed(),oldText:form.text.value,currentType:state.detail.type,currentId:state.detail.id,currentText:current?.detailSession.form.elements.namedItem('content').value,currentSummary:current?.querySelector('[data-detail-current]').textContent,closed:!document.querySelector('#detail-dialog').open}));
""".replace("CHANGE", repr(change)))
    assert result["writes"] == []
    assert result["oldText"] == "原安排未提交文字"
    if change == "close":
        assert result["closed"] is True
    else:
        assert result["currentType"] == "record" and result["currentId"] == 101
        assert result["currentText"] == "当前对象新草稿"
        assert "旧对象迟到摘要" not in result["currentSummary"]


def test_closed_queued_turn_poll_has_no_request_or_receipt_mutation():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','原话已保存');submit(form);await drain();const before=calls.length,receipt=root.querySelector('[data-detail-receipt]').textContent;document.querySelector('#detail-dialog').open=false;turnStatus='failed';await timers.find(t=>t.delay===1800).fn();await drain();console.log(JSON.stringify({...observed(),before,after:calls.length,receipt,afterReceipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    assert len(result["writes"]) == 1
    assert result["before"] == result["after"]
    assert result["receipt"] == result["afterReceipt"]


def test_real_action_to_arrangement_button_and_return_restore_exact_nodes_and_context():
    result = run_event_scenario("""
recordDTO=action({secretary_plan:planDTO});await run('openRecord(101)');const original=document.querySelector('.detail-progress'),form=choose(original,'activity'),text=input(form,'content','返回保留的待办进展草稿');text.focus();text.setSelectionRange(2,8);original.querySelector('.detail-progress-scroll').scrollTop=145;const folded=original.querySelector('[data-detail-zone="D"] details');folded.open=true;const outgoing=original.querySelector('[data-arrangement-open="201"]');assert(outgoing);outgoing.click();await drain();const arrangement=document.querySelector('.detail-progress');input(arrangement.detailSession.form,'text','安排未提交草稿');const returnButton=arrangement.querySelector('[data-detail-return]');assert(returnButton);returnButton.click();await drain();const restored=document.querySelector('.detail-progress');console.log(JSON.stringify({...observed(),sameRoot:restored===original,sameForm:restored.detailSession.form===form,sameText:form.elements.namedItem('content')===text,text:text.value,focus:document.activeElement===text,selection:[text.selectionStart,text.selectionEnd],scroll:restored.querySelector('.detail-progress-scroll').scrollTop,folded:restored.querySelector('[data-detail-zone="D"] details').open,currentType:state.detail.type,currentId:state.detail.id,stack:run('detailReturnStack'),returnButton:!!restored.querySelector('[data-detail-return]')}));
""")
    assert result["writes"] == []
    assert result["sameRoot"] and result["sameForm"] and result["sameText"] and result["focus"]
    assert result["text"] == "返回保留的待办进展草稿" and result["selection"] == [2, 8]
    assert result["scroll"] == 145 and result["folded"]
    assert result["currentType"] == "record" and result["currentId"] == 101
    assert result["stack"] == [] and result["returnButton"] is False
    assert [read["url"] for read in result["reads"]] == ["/api/records/101", "/api/secretary/plans/201", "/api/records/101"]


def test_typed_return_stack_has_only_navigation_identity_deduplication_and_maximum_five():
    result = run_event_scenario("""
for(const ref of [{type:'action',id:101},{type:'arrangement',id:201},{type:'record',id:301},{type:'material',id:'synthetic-material'},{type:'visit',id:'synthetic-visit'},{type:'customer',id:7}]){context.__ref=ref;run('pushDetailReturnContext(__ref)');}context.__ref={type:'arrangement',id:201};run('pushDetailReturnContext(__ref)');const stack=structuredClone(run('detailReturnStack'));console.log(JSON.stringify({...observed(),stack,invalid:[run('pushDetailReturnContext(null)'),run('pushDetailReturnContext({type:"action"})')]}));
""")
    stack = result["stack"]
    assert len(stack) == 5 and result["invalid"] == [False, False]
    assert stack[-1] == {"type": "arrangement", "id": 201, "csrf": "synthetic-session"}
    assert len({(ref["type"], ref["id"]) for ref in stack}) == 5
    assert all(set(ref) == {"type", "id", "csrf"} for ref in stack)
    assert not result["writes"] and not result["reads"]


def test_same_object_reopen_and_round_trip_never_create_self_return_loop():
    result = run_event_scenario("""
await run('openRecord(101)');await run('openRecord(101)');const same=structuredClone(run('detailReturnStack'));await run('openArrangement(201)');await run('openRecord(101)');const back=structuredClone(run('detailReturnStack'));await run('restoreDetailReturnContext()');const after=structuredClone(run('detailReturnStack'));console.log(JSON.stringify({...observed(),same,back,after,currentType:state.detail.type,currentId:state.detail.id}));
""")
    assert result["same"] == []
    assert result["back"] == [{"type": "arrangement", "id": 201, "csrf": "synthetic-session"}]
    assert result["after"] == [] and result["currentType"] == "arrangement" and result["currentId"] == 201
    assert result["writes"] == []


@pytest.mark.parametrize("hidden", [False, True])
def test_return_refreshes_latest_summary_without_upgrading_dirty_snapshot_or_hidden_write_permission(hidden):
    result = run_event_scenario("""
await run('openRecord(101)');const original=document.querySelector('.detail-progress'),form=choose(original,'outcome');input(form,'result','返回时保留旧影响结果');const baseline={...original.detailSession.baseline},snapshot=form.dataset.expectedSnapshot;await run('openArrangement(201)');recordDTO=action({completion_snapshot:'completion-new',completion_effects:{scope:'record_completion',record_id:101,snapshot:'completion-new',pending_tasks:[],pending_proposals:[],pending_task_count:0,pending_proposal_count:0},record:{hidden:HIDDEN?1:0,title:'最新来源标题'}});await run('restoreDetailReturnContext()');const restored=document.querySelector('.detail-progress');forceSubmit(form);await drain();console.log(JSON.stringify({...observed(),same:restored===original,sameForm:restored.detailSession.form===form,value:form.elements.namedItem('result').value,snapshot:form.dataset.expectedSnapshot,initialSnapshot:snapshot,baseline:restored.detailSession.baseline,initialBaseline:baseline,visible:restored.detailSession.vm.visible,disabled:form.querySelector('[type="submit"]').disabled,title:restored.querySelector('.detail-progress-title').textContent,notice:restored.querySelector('[data-detail-draft-notice]').textContent,latestSnapshot:restored.detailSession.vm.baseline.completion_snapshot}));
""".replace("HIDDEN", "true" if hidden else "false"))
    assert result["writes"] == [] and result["same"] and result["sameForm"]
    assert result["value"] == "返回时保留旧影响结果"
    assert result["snapshot"] == result["initialSnapshot"] == "completion-seen-1"
    assert result["baseline"] == result["initialBaseline"]
    assert result["latestSnapshot"] == "completion-new" and result["title"] == "最新来源标题"
    assert result["visible"] is (not hidden)
    assert "核对" in result["notice"] or "当前状态已改变" in result["notice"]
    if hidden:
        assert result["disabled"] is True


def test_return_404_uses_readonly_lifecycle_exact_record_and_retains_result_draft():
    result = run_event_scenario("""
await run('openRecord(101)');const original=document.querySelector('.detail-progress'),form=choose(original,'outcome');input(form,'result','归档后仍保留的实际结果');await run('openArrangement(201)');const base=run('api');context.__hiddenApi=async(url,options={})=>{if(url==='/api/records/101'){calls.push({url,method:'GET'});const error=new Error('已归档');error.status=404;throw error;}if(url==='/api/records/101/lifecycle'){calls.push({url,method:'GET'});return {record:{...recordDTO.record,hidden:1},visibility:'archived'};}return base(url,options);};run('api=__hiddenApi');const returned=await run('restoreDetailReturnContext()');forceSubmit(form);await drain();console.log(JSON.stringify({...observed(),returned,same:document.querySelector('.detail-progress')===original,value:form.elements.namedItem('result').value,disabled:form.querySelector('[type="submit"]').disabled,visible:original.detailSession.vm.visible,currentType:state.detail.type,currentId:state.detail.id}));
""")
    assert result["returned"] and result["same"]
    assert result["value"] == "归档后仍保留的实际结果"
    assert result["disabled"] and result["visible"] is False
    assert result["currentType"] == "record" and result["currentId"] == 101
    assert result["writes"] == []
    assert [read["url"] for read in result["reads"]][-2:] == ["/api/records/101", "/api/records/101/lifecycle"]


def test_latest_arrangement_open_wins_when_previous_record_read_resolves_first():
    result = run_event_scenario("""
const recordGate=defer(),planGate=defer(),base=run('api');context.__openingApi=async(url,options={})=>{if(url==='/api/records/101')await recordGate.promise;if(url==='/api/secretary/plans/201')await planGate.promise;return base(url,options);};run('api=__openingApi');const older=run('openRecord(101)'),latest=run('openArrangement(201)');recordGate.resolve();await older;planGate.resolve();await latest;await drain();console.log(JSON.stringify({...observed(),currentType:state.detail?.type,currentId:state.detail?.id,subject:document.querySelector('.detail-progress')?.detailSession.subjectRef}));
""")
    assert result["writes"] == []
    assert result["currentType"] == "arrangement" and result["currentId"] == 201
    assert result["subject"] == {"type": "arrangement", "id": 201}


@pytest.mark.parametrize("change", ["close", "object", "reauth"])
def test_late_return_read_never_replaces_new_navigation_or_new_login(change):
    result = run_event_scenario("""
await run('openRecord(101)');const original=document.querySelector('.detail-progress');input(original.detailSession.form,'content','原待办返回草稿');await run('openArrangement(201)');const gate=defer(),base=run('api');context.__returnGateApi=async(url,options={})=>{if(url==='/api/records/101'){await gate.promise;return structuredClone(recordDTO);}return base(url,options);};run('api=__returnGateApi');const returning=run('restoreDetailReturnContext()');await Promise.resolve();if(CHANGE==='close')run("closeDialog('detail',{force:true})");else {if(CHANGE==='reauth')state.csrf='new-synthetic-session';await run('openArrangement(201)');input(document.querySelector('.detail-progress').detailSession.form,'text','最新对象或会话草稿');}gate.resolve();const accepted=await returning;await drain();console.log(JSON.stringify({...observed(),accepted,open:document.querySelector('#detail-dialog').open,currentType:state.detail?.type,currentId:state.detail?.id,csrf:state.csrf,text:document.querySelector('.detail-progress')?.detailSession.form.text?.value}));
""".replace("CHANGE", repr(change)))
    assert result["accepted"] is False and result["writes"] == []
    if change == "close":
        assert result["open"] is False
    else:
        assert result["currentType"] == "arrangement" and result["currentId"] == 201
        assert result["text"] == "最新对象或会话草稿"


def test_generic_source_cache_tracks_outgoing_typed_source_even_after_state_changes():
    result = run_event_scenario("""
state.detail={type:'material',id:'synthetic-material',data:{material:{id:'synthetic-material',title:'合成材料来源'}}};context.__sourceHtml=run("dialogHeader('合成材料来源','完整来源回看')")+'<div class="dialog-body"><details open><summary>原件</summary><p>合成材料内容</p></details></div>';run("openDialog('detail',__sourceHtml)");const sourceRoot=document.querySelector('#detail-content').children[0];await run('openRecord(101)');const stack=structuredClone(run('detailReturnStack')),base=run('api');context.__materialApi=async(url,options={})=>{if(url==='/api/materials/synthetic-material'){calls.push({url,method:'GET'});return {material:{id:'synthetic-material',title:'合成材料来源'}};}return base(url,options);};run('api=__materialApi');await run('restoreDetailReturnContext()');console.log(JSON.stringify({...observed(),stack,currentType:state.detail.type,currentId:state.detail.id,same:document.querySelector('#detail-content').children[0]===sourceRoot,after:run('detailReturnStack')}));
""")
    assert result["stack"] == [{"type": "material", "id": "synthetic-material", "csrf": "synthetic-session"}]
    assert result["currentType"] == "material" and result["currentId"] == "synthetic-material"
    assert result["same"] and result["after"] == [] and result["writes"] == []
    assert result["reads"][-1]["url"] == "/api/materials/synthetic-material"


def test_hidden_arrangement_404_return_checks_exact_source_lifecycle_then_restores_readonly_draft():
    result = run_event_scenario("""
await run('openArrangement(201)');const original=document.querySelector('.detail-progress'),form=choose(original,'progress');input(form,'progress_text','隐藏安排原进展草稿');const baseline={...original.detailSession.baseline};await run('openRecord(101)');const base=run('api');context.__hiddenPlanApi=async(url,options={})=>{if(url==='/api/secretary/plans/201'){calls.push({url,method:'GET'});const error=new Error('来源隐藏安排不可见');error.status=404;throw error;}if(url==='/api/records/301/lifecycle'){calls.push({url,method:'GET'});return {record:{id:301,kind:'memo',title:'合成已归档原话',hidden:1},visibility:'archived'};}return base(url,options);};run('api=__hiddenPlanApi');const returned=await run('restoreDetailReturnContext()');forceSubmit(form);await drain();console.log(JSON.stringify({...observed(),returned,same:document.querySelector('.detail-progress')===original,sameForm:original.detailSession.form===form,value:form.elements.namedItem('progress_text').value,disabled:form.querySelector('[type="submit"]').disabled,baseline:original.detailSession.baseline,initialBaseline:baseline,visible:original.detailSession.vm.visible,currentType:state.detail.type,currentId:state.detail.id,mutable:original.querySelectorAll('[data-arrangement-operation],[data-arrangement-select]').length}));
""")
    assert result["returned"] and result["same"] and result["sameForm"]
    assert result["value"] == "隐藏安排原进展草稿"
    assert result["disabled"] and result["visible"] is False
    assert result["baseline"] == result["initialBaseline"] and result["mutable"] == 0
    assert result["currentType"] == "arrangement" and result["currentId"] == 201
    assert result["writes"] == []
    assert [read["url"] for read in result["reads"]][-2:] == ["/api/secretary/plans/201", "/api/records/301/lifecycle"]


def test_latest_record_open_wins_when_previous_arrangement_read_resolves_first():
    result = run_event_scenario("""
const recordGate=defer(),planGate=defer(),base=run('api');context.__openingApi=async(url,options={})=>{if(url==='/api/records/101')await recordGate.promise;if(url==='/api/secretary/plans/201')await planGate.promise;return base(url,options);};run('api=__openingApi');const older=run('openArrangement(201)'),latest=run('openRecord(101)');planGate.resolve();await older;recordGate.resolve();await latest;await drain();console.log(JSON.stringify({...observed(),currentType:state.detail?.type,currentId:state.detail?.id,subject:document.querySelector('.detail-progress')?.detailSession.subjectRef}));
""")
    assert result["writes"] == []
    assert result["currentType"] == "record" and result["currentId"] == 101
    assert result["subject"] == {"type": "action", "id": 101}


def test_return_button_double_click_during_visibility_read_has_one_get_and_no_loop():
    result = run_event_scenario("""
await run('openRecord(101)');const original=document.querySelector('.detail-progress');await run('openArrangement(201)');const gate=defer(),base=run('api');context.__returnGateApi=async(url,options={})=>{if(url==='/api/records/101')await gate.promise;return base(url,options);};run('api=__returnGateApi');const button=document.querySelector('[data-detail-return]');button.click();button.click();gate.resolve();await drain();console.log(JSON.stringify({...observed(),same:document.querySelector('.detail-progress')===original,stack:run('detailReturnStack')}));
""")
    assert result["writes"] == [] and result["same"] and result["stack"] == []
    assert [read["url"] for read in result["reads"]].count("/api/records/101") == 2


def test_clean_reopen_uses_current_paused_lifecycle_focus_instead_of_previous_purpose():
    result = run_event_scenario("""
const old=mount('arrangement');assert.equal(old.detailSession.purpose,'supplement');const current=mount('arrangement',plan({settling_state:'paused'}));console.log(JSON.stringify({...observed(),purpose:current.detailSession.purpose,operation:current.detailSession.operation,formOperation:current.detailSession.form.dataset.arrangementForm}));
""")
    assert result["purpose"] == "decision" and result["operation"] == "resume"
    assert result["formOperation"] == "resume" and result["writes"] == []


def test_dirty_reopen_keeps_prior_draft_but_paused_lifecycle_blocks_its_previous_write():
    result = run_event_scenario("""
const old=mount('arrangement'),form=old.detailSession.form;input(form,'text','暂停前未提交的原话草稿');const current=mount('arrangement',plan({settling_state:'paused'}));forceSubmit(current.detailSession.form);await drain();console.log(JSON.stringify({...observed(),same:current.detailSession.form===form,purpose:current.detailSession.purpose,value:form.text.value,disabled:form.querySelector('[type="submit"]').disabled,notice:current.querySelector('[data-detail-draft-notice]').textContent}));
""")
    assert result["same"] and result["purpose"] == "supplement" and result["disabled"]
    assert result["value"] == "暂停前未提交的原话草稿" and result["writes"] == []
    assert "不允许" in result["notice"] or "核对" in result["notice"]


def test_edit_suspension_resumes_queued_turn_read_and_failed_retry_uses_original_turn_once():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','编辑前已保存原话');submit(form);await drain();context.__editHtml=run("dialogHeader('合成短编辑','保存原详情上下文','edit')")+'<div class="dialog-body"><p>合成编辑内容</p></div>';run("openDialog('edit',__editHtml)");const firstPoll=timers.find(t=>t.delay===1800);await firstPoll.fn();await drain();const before=calls.length;turnStatus='failed';run("closeDialog('edit',{force:true})");const resumed=timers.filter(t=>t.delay===1800).at(-1);assert.notEqual(resumed,firstPoll);await resumed.fn();await drain();const receipt=root.querySelector('[data-detail-receipt]').textContent,retry=root.querySelector('[data-detail-retry-turn]');assert(retry);const base=run('api');context.__retryApi=async(url,options={})=>{if(url==='/api/secretary/turns/801/retry'){calls.push({url,method:options.method,body:options.body});return {turn:{id:801,status:'queued'}};}return base(url,options);};run('api=__retryApi');retry.click();await drain();console.log(JSON.stringify({...observed(),before,receipt,same:document.querySelector('.detail-progress')===root,sameForm:root.detailSession.form===form,open:document.querySelector('#detail-dialog').open,editOpen:document.querySelector('#edit-dialog').open}));
""")
    assert result["same"] and result["sameForm"] and result["open"] and result["editOpen"] is False
    assert "原话已保存" in result["receipt"] and "未成功" in result["receipt"]
    assert [write["url"] for write in result["writes"]] == ["/api/secretary/turns", "/api/secretary/turns/801/retry"]
    assert [read["url"] for read in result["reads"]].count("/api/secretary/turns/801") == 1


def test_record_plan_discovery_paginates_uses_exact_relations_deduplicates_and_keeps_c_form():
    result = run_event_scenario(r"""
const root=mount(),form=choose(root,'activity');input(form,'content','查找全部活动时的未提交进展');const baseline={...root.detailSession.baseline};let concurrent=0,maximum=0;const base=run('api');context.__planSearchApi=async(url,options={})=>{if(url.startsWith('/api/secretary/arrangements?')){calls.push({url,method:'GET'});const offset=Number(url.match(/offset=(\d+)/)[1]);return {total:202,items:offset===0?Array.from({length:200},(_,i)=>({plan_id:201+i,title:'所有卡片都同名'})):[{plan_id:201,title:'所有卡片都同名'},{plan_id:403,title:'所有卡片都同名'}]};}const match=url.match(/^\/api\/secretary\/plans\/(\d+)$/);if(match){calls.push({url,method:'GET'});const id=Number(match[1]);concurrent++;maximum=Math.max(maximum,concurrent);await Promise.resolve();concurrent--;return {plan:plan({plan_id:id,title:'所有卡片都同名'},{id,title:'所有卡片都同名',record_id:id===201?101:999,turns:id===403?[{plan_id:403,record_id:101}]:id===202?[{plan_id:999,record_id:101}]:[{plan_id:id,record_id:102}]})};}return base(url,options);};run('api=__planSearchApi');root.querySelector('[data-detail-find-plans]').click();await drain();console.log(JSON.stringify({...observed(),same:root.detailSession.form===form,value:form.elements.namedItem('content').value,baseline:root.detailSession.baseline,initialBaseline:baseline,plans:root.detailSession.vm.plans.map(p=>p.id),authority:root.detailSession.vm.scheduleAuthority,links:root.querySelectorAll('[data-arrangement-open]').map(n=>Number(n.dataset.arrangementOpen)),maximum}));
""")
    assert result["writes"] == [] and result["uploads"] == []
    assert result["same"] and result["value"] == "查找全部活动时的未提交进展"
    assert result["baseline"] == result["initialBaseline"]
    assert result["plans"] == result["links"] == [201, 403]
    assert result["authority"] == "choose_plan" and result["maximum"] <= 4
    pages = [read["url"] for read in result["reads"] if read["url"].startswith("/api/secretary/arrangements?")]
    assert pages == ["/api/secretary/arrangements?view=all&state=all&limit=200&offset=0", "/api/secretary/arrangements?view=all&state=all&limit=200&offset=200"]


@pytest.mark.parametrize("problem", ["wrong_id", "read_error", "delayed_other_root"])
def test_plan_discovery_problem_preserves_original_associations_and_draft(problem):
    result = run_event_scenario("""
const root=mount('action',action({secretary_plan:planDTO})),form=choose(root,'activity');input(form,'content','失败时保留的当前对象草稿');const gate=defer(),base=run('api');context.__planProblemApi=async(url,options={})=>{if(url.startsWith('/api/secretary/arrangements?')){calls.push({url,method:'GET'});return {total:1,items:[{plan_id:202,title:'同名但需核对编号'}]};}if(url==='/api/secretary/plans/202'){calls.push({url,method:'GET'});if(PROBLEM==='delayed_other_root')await gate.promise;if(PROBLEM==='read_error')throw new Error('合成读取失败');return {plan:plan({plan_id:PROBLEM==='wrong_id'?999:202},{id:PROBLEM==='wrong_id'?999:202,record_id:101})};}return base(url,options);};run('api=__planProblemApi');root.querySelector('[data-detail-find-plans]').click();await Promise.resolve();let other=null;if(PROBLEM==='delayed_other_root'){for(let i=0;i<20&&!calls.some(c=>c.url==='/api/secretary/plans/202');i++)await Promise.resolve();assert(calls.some(c=>c.url==='/api/secretary/plans/202'),'late response must already be in flight');other=mount('arrangement');input(other.detailSession.form,'text','最新对象原话草稿');gate.resolve();}await drain();console.log(JSON.stringify({...observed(),oldValue:form.elements.namedItem('content').value,oldPlans:root.detailSession.vm.plans.map(p=>p.id),currentType:state.detail.type,currentId:state.detail.id,newValue:other?.detailSession.form.text.value,newPlans:other?.detailSession.vm.plans.map(p=>p.id)}));
""".replace("PROBLEM", repr(problem)))
    assert result["writes"] == [] and result["oldValue"] == "失败时保留的当前对象草稿"
    assert result["oldPlans"] == [201]
    if problem == "delayed_other_root":
        assert result["currentType"] == "arrangement" and result["currentId"] == 201
        assert result["newValue"] == "最新对象原话草稿" and result["newPlans"] == []
    else:
        assert any("尚未查全" in notice["message"] for notice in result["notices"])


def test_discovered_multiple_record_plans_survive_ordinary_latest_only_record_refresh():
    result = run_event_scenario(r"""
const root=mount('action',action({secretary_plan:planDTO})),form=choose(root,'activity');input(form,'content','多活动核对之后保留的进展草稿');const baseline={...root.detailSession.baseline},base=run('api');context.__multiPlanApi=async(url,options={})=>{if(url.startsWith('/api/secretary/arrangements?')){calls.push({url,method:'GET'});return {total:2,items:[{plan_id:201,title:'合成同名活动'},{plan_id:202,title:'合成同名活动'}]};}if(url.startsWith('/api/secretary/plans/')){calls.push({url,method:'GET'});const id=Number(url.split('/').at(-1));return {plan:plan({plan_id:id,title:'合成同名活动'},{id,title:'合成同名活动',record_id:101})};}return base(url,options);};run('api=__multiPlanApi');root.querySelector('[data-detail-find-plans]').click();await drain();const discovered=root.detailSession.vm.plans.map(p=>p.id),firstAuthority=root.detailSession.vm.scheduleAuthority;await run("refreshDetailSubject({type:'action',id:101})");await drain();console.log(JSON.stringify({...observed(),discovered,firstAuthority,after:root.detailSession.vm.plans.map(p=>p.id),authority:root.detailSession.vm.scheduleAuthority,links:root.querySelectorAll('[data-arrangement-open]').map(n=>Number(n.dataset.arrangementOpen)),options:root.querySelector('[data-detail-purpose-choice]').querySelectorAll('option').map(n=>n.value),same:root.detailSession.form===form,value:form.elements.namedItem('content').value,baseline:root.detailSession.baseline,initialBaseline:baseline,serverLatestOnly:recordDTO.secretary_plan.id}));
""")
    assert result["discovered"] == [201, 202] and result["firstAuthority"] == "choose_plan"
    assert result["serverLatestOnly"] == 201
    assert result["after"] == result["links"] == [201, 202]
    assert result["authority"] == "choose_plan" and "legacy_schedule" not in result["options"]
    assert result["same"] and result["value"] == "多活动核对之后保留的进展草稿"
    assert result["baseline"] == result["initialBaseline"] and result["writes"] == []


@pytest.mark.parametrize("problem", ["read_error", "wrong_id", "delayed_other_root"])
def test_revalidation_problem_cannot_replace_discovered_associations_or_draft(problem):
    result = run_event_scenario("""
const root=mount('action',action({secretary_plan:planDTO})),form=choose(root,'activity');
input(form,'content','association draft');
const initialBaseline={...root.detailSession.baseline},gate=defer(),base=run('api');
let revalidating=false;
context.__revalidationApi=async(url,options={})=>{
  if(url.startsWith('/api/secretary/arrangements?')){
    calls.push({url,method:'GET'});
    return {total:2,items:[{plan_id:201,title:'same title'},{plan_id:202,title:'same title'}]};
  }
  if(url.startsWith('/api/secretary/plans/')){
    calls.push({url,method:'GET'});
    const requestedId=Number(url.split('/').at(-1));
    if(revalidating&&requestedId===202){
      if(PROBLEM==='read_error')throw new Error('synthetic association read failure');
      if(PROBLEM==='delayed_other_root')await gate.promise;
    }
    const returnedId=revalidating&&requestedId===202&&PROBLEM==='wrong_id'?999:requestedId;
    return {plan:plan({plan_id:returnedId,title:'same title'},{id:returnedId,title:'same title',record_id:101})};
  }
  return base(url,options);
};
run('api=__revalidationApi');
root.querySelector('[data-detail-find-plans]').click();await drain();
const discovered=root.detailSession.vm.plans.map(p=>p.id);
revalidating=true;
let error='',other=null;
const refresh=run("refreshDetailSubject({type:'action',id:101})").catch(problem=>{error=problem.message;});
if(PROBLEM==='delayed_other_root'){
  for(let i=0;i<30&&calls.filter(c=>c.url==='/api/secretary/plans/202').length<2;i++)await Promise.resolve();
  assert(calls.filter(c=>c.url==='/api/secretary/plans/202').length===2,'association revalidation must be in flight');
  other=mount('arrangement');input(other.detailSession.form,'text','new object draft');gate.resolve();
}
await refresh;await drain();
console.log(JSON.stringify({...observed(),discovered,error,same:root.detailSession.form===form,value:form.elements.namedItem('content').value,baseline:root.detailSession.baseline,initialBaseline,after:root.detailSession.vm.plans.map(p=>p.id),authority:root.detailSession.vm.scheduleAuthority,currentType:state.detail.type,currentId:state.detail.id,newValue:other?.detailSession.form.text.value,newPlans:other?.detailSession.vm.plans.map(p=>p.id)}));
""".replace("PROBLEM", repr(problem)))
    assert result["discovered"] == result["after"] == [201, 202]
    assert result["authority"] == "choose_plan"
    assert result["same"] and result["value"] == "association draft"
    assert result["baseline"] == result["initialBaseline"]
    assert result["writes"] == [] and result["uploads"] == []
    assert all(read["method"] == "GET" for read in result["reads"])
    if problem == "delayed_other_root":
        assert result["error"] == ""
        assert result["currentType"] == "arrangement" and result["currentId"] == 201
        assert result["newValue"] == "new object draft" and result["newPlans"] == []
    else:
        assert result["error"]
        assert result["currentType"] == "record" and result["currentId"] == 101


def test_source_return_restores_focus_to_rebuilt_e_button_and_original_draft_scroll():
    result = run_event_scenario("""
await run('openArrangement(201)');
const original=document.querySelector('.detail-progress'),form=choose(original,'supplement');
input(form,'text','source roundtrip draft');
const baseline={...original.detailSession.baseline},scroll=original.querySelector('.detail-progress-scroll');
scroll.scrollTop=175;
const outgoing=original.querySelector('[data-detail-zone="E"] [data-raw-record="301"]');
assert(outgoing,'actual E source button missing');
outgoing.closest('details').open=true;outgoing.focus();
const base=run('api');
context.__sourceApi=async(url,options={})=>{
  if(url==='/api/records/301'){
    calls.push({url,method:'GET'});
    return {record:{id:301,kind:'memo',status:'following',source:'web',title:'source memo',original_content:'synthetic source',created_at:NOW/1000},activities:[]};
  }
  return base(url,options);
};
run('api=__sourceApi');outgoing.click();await drain();
assert.equal(state.detail.type,'record');assert.equal(Number(state.detail.id),301);
const returnButton=document.querySelector('[data-detail-return]');assert(returnButton);
returnButton.click();await drain();
const restored=document.querySelector('.detail-progress'),replacement=restored.querySelector('[data-detail-zone="E"] [data-raw-record="301"]');
console.log(JSON.stringify({...observed(),sameRoot:restored===original,sameForm:restored.detailSession.form===form,value:form.text.value,baseline:restored.detailSession.baseline,initialBaseline:baseline,oldDetached:!outgoing.isConnected,newButton:replacement!==outgoing,focus:document.activeElement===replacement,focusZone:document.activeElement.closest('[data-detail-zone]')?.dataset.detailZone,foldOpen:replacement.closest('details').open,scroll:scroll.scrollTop,currentType:state.detail.type,currentId:state.detail.id}));
""")
    assert result["sameRoot"] and result["sameForm"]
    assert result["oldDetached"] and result["newButton"] and result["focus"]
    assert result["focusZone"] == "E" and result["foldOpen"]
    assert result["scroll"] == 175 and result["value"] == "source roundtrip draft"
    assert result["baseline"] == result["initialBaseline"]
    assert result["currentType"] == "arrangement" and result["currentId"] == 201
    assert result["writes"] == []
    assert [read["url"] for read in result["reads"]] == ["/api/secretary/plans/201", "/api/records/301", "/api/secretary/plans/201"]


def test_read_section_refresh_focus_replacement_preserves_scroll_anchor_in_same_e_zone():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','anchor draft');
const outgoing=root.querySelector('[data-detail-zone="E"] [data-raw-record="301"]'),scroll=root.querySelector('.detail-progress-scroll');
outgoing.closest('details').open=true;outgoing.focus();scroll.scrollTop=175;
const normalRect=Element.prototype.getBoundingClientRect;
outgoing.getBoundingClientRect=()=>({left:0,right:400,top:160,bottom:190});
Element.prototype.getBoundingClientRect=function(){
  if(this.matches('[data-detail-zone="E"] [data-raw-record="301"]'))return {left:0,right:400,top:240,bottom:270};
  return normalRect.call(this);
};
await run("refreshDetailSubject({type:'arrangement',id:201})");
const replacement=root.querySelector('[data-detail-zone="E"] [data-raw-record="301"]');
console.log(JSON.stringify({...observed(),oldDetached:!outgoing.isConnected,newButton:replacement!==outgoing,focus:document.activeElement===replacement,zone:document.activeElement.closest('[data-detail-zone]')?.dataset.detailZone,scroll:scroll.scrollTop,foldOpen:replacement.closest('details').open,sameForm:root.detailSession.form===form,value:form.text.value}));
""")
    assert result["oldDetached"] and result["newButton"] and result["focus"]
    assert result["zone"] == "E" and result["foldOpen"]
    assert result["scroll"] == 255
    assert result["sameForm"] and result["value"] == "anchor draft" and result["writes"] == []


@pytest.mark.parametrize("operation,prompt,label", [
    ("set_deadline", "这一次安排需要何时前定下来？执行日程保持原时间。", "保存确定期限"),
    ("set_check", "下次何时再问、核对什么？这不是活动的执行时间。", "保存下一推进点"),
    ("pause", "本轮先暂停协调；已生效的日程继续保留。", "暂缓本轮协调"),
    ("resume", "明确恢复这一轮协调，重新核对需要的推进点。", "恢复本轮协调"),
    ("abandon_coordination", "放弃本轮协调；原有效日程继续，事项和项目不因此结束。", "放弃本轮协调"),
    ("start_reschedule", "商量新的执行时间；新方案落实前原日程继续。", "开始商量改期"),
    ("continue_set_time", "日期已经定下，继续在同一次活动中确定具体钟点。", "继续确定钟点"),
    ("withdraw_execution", "只撤销当前有效日程，仍可继续协调新的时间。", "撤销当前日程"),
    ("cancel_activity", "取消这一次活动的日程和提示，事项与项目继续保留。", "确认取消这次活动"),
    ("select_candidate", "先保存所选方案，再核对其他缺口；选择不代表已经约好。", "保存所选方案"),
    ("confirm_arrangement", "核对当前方案后明确授权落实；进入此处还不代表已经安排。", "按当前核对内容落实"),
])
def test_owned_operation_change_and_refresh_keep_selected_question_and_submit_label(operation, prompt, label):
    result = run_event_scenario("""
const op=OPERATION;
const config={decision_mode:'self',agreement_status:'not_required',settlement_scope:'execution_time',proposed_execution:{time_spec:{precision:'instant',date:'2026-10-09',at:NOW/1000+3*86400}},blocking_reasons:['user_review_required'],can_apply:false,followup_enabled:op!=='resume',active_schedule:{id:501,status:'pending',revision:4,remind_at:NOW/1000+86400},candidates:[{id:'a',time_spec:{precision:'date',date:'2026-10-09'}},{id:'b',time_spec:{precision:'date',date:'2026-10-10'}}],selected_candidate_id:'a'};
if(op==='continue_set_time')Object.assign(config,{settling_state:'settled',settlement_scope:'date_only',proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},blocking_reasons:['missing_clock']});
const root=mount('arrangement',plan(config));
choose(root,'progress');
const form=choose(root,'decision',op),baseline={...root.detailSession.baseline};
if(form.elements.namedItem('reason'))input(form,'reason','selected operation draft');
if(op==='set_deadline')input(form,'deadline_date','2026-10-08');
if(op==='set_check')input(form,'check_action','synthetic selected check');
const before={prompt:root.querySelector('[data-detail-zone="C"] > .detail-focus-prompt').textContent,label:form.querySelector('[type="submit"]').textContent};
const values=run('formValues(__root.detailSession.form)');
context.__legacyOp=op;
const legacy=new Element('div');legacy.innerHTML=run('arrangementDecisionFormHTML(__dto,__legacyOp)');
const legacyLabel=legacy.querySelector('[type="submit"]').textContent;
planDTO=plan({...config,revision:5},{revision:5});
await run("refreshDetailSubject({type:'arrangement',id:201})");
const after={prompt:root.querySelector('[data-detail-zone="C"] > .detail-focus-prompt').textContent,label:form.querySelector('[type="submit"]').textContent};
console.log(JSON.stringify({...observed(),before,after,legacyLabel,sameForm:root.detailSession.form===form,purpose:root.detailSession.purpose,operation:root.detailSession.operation,selected:root.querySelector('[data-detail-purpose-choice]').value,baseline:root.detailSession.baseline,initialBaseline:baseline,values:run('formValues(__root.detailSession.form)'),originalValues:values,oldTask:planDTO.arrangement.active_schedule}));
""".replace("OPERATION", repr(operation)))
    assert result["before"] == result["after"] == {"prompt": prompt, "label": label}
    assert result["sameForm"] and result["purpose"] == "decision" and result["operation"] == operation
    assert result["selected"] == "decision:" + operation
    assert result["baseline"] == result["initialBaseline"] and result["values"] == result["originalValues"]
    assert result["oldTask"] == {"id": 501, "status": "pending", "revision": 4, "remind_at": 1791338400}
    assert result["writes"] == [] and result["uploads"] == []
    assert [read["url"] for read in result["reads"]] == ["/api/secretary/plans/201"]
    if operation not in {"cancel_activity", "select_candidate", "confirm_arrangement"}:
        assert result["legacyLabel"] == "保存本次选择"
