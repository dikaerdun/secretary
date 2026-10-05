'use strict';

// Visibility is separate from business completion. All mutations are explicit
// and use the server's source/schedule snapshot; originals remain recoverable.
let lifecycleEpoch=0,lifecycleArchiveView='archived',lifecycleArchivePage=1,lifecycleTipPage=1,lifecyclePageContext=null;
function lifecycleActions(id){return Number(id)>0?`<button type="button" class="button small" data-lifecycle-record="${Number(id)}">归档 / 删除</button>`:'';}
function lifecycleManageButton(){return '<button type="button" class="text-button" data-lifecycle-manage>已归档 / 回收站</button>';}
function lifecycleNavigation(){return {view:state.view,navigation:Number(state.discussionNavigationGeneration||0),detail:Number(state.detailLoadGeneration||0)};}
function lifecycleCurrent(epoch,intent,csrf){const page=lifecyclePageContext;return Boolean(page&&lifecycleEpoch===epoch&&state.authenticated&&state.csrf===csrf&&Number(state.dialogIntentGeneration||0)===intent&&state.view===page.view&&Number(state.discussionNavigationGeneration||0)===page.navigation&&Number(state.detailLoadGeneration||0)===page.detail);}
async function openRecordLifecycle(id){
  const epoch=++lifecycleEpoch,intent=state.dialogIntentGeneration=Number(state.dialogIntentGeneration||0)+1,csrf=state.csrf;
  lifecyclePageContext=lifecycleNavigation();
  const data=await api(`/api/records/${Number(id)}/lifecycle`);if(!lifecycleCurrent(epoch,intent,csrf))return;
  const r=data.record,active=data.visibility==='active',blocked=Boolean(data.shared_tasks),tasks=Number(data.pending_task_count||0),proposals=Number(data.pending_proposal_count||0);
  const changes=tasks||proposals?`<div class="analysis-warning"><strong>这次会同时停止安排</strong><p>${tasks} 个有效日程 / 提醒，${proposals} 个待确认安排。恢复事项后，需重新安排提醒。</p>${(data.pending_tasks||[]).map(t=>`<p>${e(t.title)}${t.remind_at?' · '+e(formatDate(t.remind_at,true)):''}</p>`).join('')}</div>`:'<p class="form-hint">没有需要停止的有效安排。</p>';
  openDialog('edit',dialogHeader(active?'整理这条事项':'回看保留的事项',r.title,'edit')+`<div class="dialog-body"><p>${active?'归档：暂不推进，保留以后再用。删除：移到回收站，适合没有价值的临时想法。两种都可恢复。':data.visibility==='trash'?'当前在回收站，原话与关联资料仍保留。':'当前已归档，原话与关联资料仍保留。'}</p>${data.record_count>1?`<p>这次交流的 ${Number(data.record_count)} 条输入一起处理，保留原话和修改历史。</p>`:''}${active?changes:'<p class="form-hint">恢复只让事项重新显示；此前停止的日程、提醒需重新安排。</p>'}${blocked?`<p class="form-error">${e(data.shared_task_message||'安排还由其他事项共用，请先核对关联安排。')}</p>`:''}<details class="review-disclosure"><summary>回看原话</summary><div class="detail-text">${e(r.original_content||r.content||r.title)}</div></details><p class="form-hint">客户资料与附件原件继续保留；另行采纳的后续待办可分别处理。</p><p class="form-error" role="alert" data-lifecycle-error></p><div class="form-footer">${active?`<button type="button" class="button" data-lifecycle-action="archive" data-lifecycle-id="${Number(data.root_record_id)}" data-lifecycle-snapshot="${e(data.snapshot)}" ${blocked?'disabled':''}>归档事项</button><button type="button" class="button" data-lifecycle-action="trash" data-lifecycle-id="${Number(data.root_record_id)}" data-lifecycle-snapshot="${e(data.snapshot)}" ${blocked?'disabled':''}>移到回收站</button>`:`<button type="button" class="button primary" data-lifecycle-action="restore" data-lifecycle-id="${Number(data.root_record_id)}" data-lifecycle-snapshot="${e(data.snapshot)}">恢复事项</button>${data.visibility==='archived'?`<button type="button" class="button" data-lifecycle-action="trash" data-lifecycle-id="${Number(data.root_record_id)}" data-lifecycle-snapshot="${e(data.snapshot)}">移到回收站</button>`:''}`}<button type="button" class="button" data-close="edit">先保留</button></div></div>`);
}
function lifecycleRetainedRow(entry){const r=entry.record;return `<article class="review-card"><h3>${e(r.title)}</h3><p>${e(r.customer_name||'未关联客户')} · ${entry.visibility==='trash'?'回收站':'已归档'}${entry.record_count>1?' · '+Number(entry.record_count)+' 条交流输入':''}</p><p>${e((r.content||r.original_content||'').slice(0,160))}</p><button type="button" class="button small" data-lifecycle-record="${Number(entry.root_record_id)}">回看 / 恢复</button></article>`;}
function lifecyclePages(data,kind){return data.pages>1?`<div class="pagination"><button type="button" class="button small" data-lifecycle-page="${Number(data.page)-1}" data-lifecycle-list="${kind}" ${data.page<=1?'disabled':''}>上一页</button><span>${Number(data.page)} / ${Number(data.pages)}</span><button type="button" class="button small" data-lifecycle-page="${Number(data.page)+1}" data-lifecycle-list="${kind}" ${data.page>=data.pages?'disabled':''}>下一页</button></div>`:'';}
async function openLifecycleArchive(view=lifecycleArchiveView,{page=1,tipPage=1}={}){
  if(!['archived','trash'].includes(view))throw new Error('请选择已归档或回收站。');
  const epoch=++lifecycleEpoch,intent=state.dialogIntentGeneration=Number(state.dialogIntentGeneration||0)+1,csrf=state.csrf;
  lifecyclePageContext=lifecycleNavigation();
  const [records,tips]=await Promise.all([api(`/api/record-lifecycle?visibility=${view}&page=${page}&page_size=20`),view==='archived'?api(`/api/priority-archives?page=${tipPage}&page_size=20`):Promise.resolve({items:[],total:0})]);
  if(!lifecycleCurrent(epoch,intent,csrf))return;lifecycleArchiveView=view;lifecycleArchivePage=page;lifecycleTipPage=tipPage;
  openDialog('edit',dialogHeader('已归档 / 回收站','腾出工作空间，想继续时再恢复。','edit')+`<div class="dialog-body"><div class="material-action-buttons"><button type="button" class="button ${view==='archived'?'primary':''}" data-lifecycle-tab="archived" aria-pressed="${view==='archived'}">已归档</button><button type="button" class="button ${view==='trash'?'primary':''}" data-lifecycle-tab="trash" aria-pressed="${view==='trash'}">回收站</button></div><p>原始记录仍保留。恢复后可以继续补充，提醒需重新安排。</p><h3>${view==='trash'?'回收站事项':'归档事项'} · ${Number(records.total||0)}</h3>${records.items?.length?records.items.map(lifecycleRetainedRow).join(''):'<p class="form-hint">这里还没有事项。</p>'}${lifecyclePages(records,'records')}${view==='archived'?`<h3>归档的推进提示 · ${Number(tips.total||0)}</h3><p class="form-hint">这里只收起提示，客户资料与原事项继续保留。</p>${tips.items?.length?tips.items.map(t=>`<article class="review-card"><h4>${e(t.item.talk||'推进提示')}</h4><p>${e(t.item.customer_name||'')}${t.archived_at?' · '+e(formatDate(t.archived_at,true)):''}</p><button type="button" class="button small" data-lifecycle-restore-tip="${e(t.key)}" data-lifecycle-tip-revision="${Number(t.revision)}">恢复推进提示</button></article>`).join(''):'<p class="form-hint">这里还没有提示。</p>'}${lifecyclePages(tips,'tips')}`:''}<p class="form-error" role="alert" data-lifecycle-error></p></div>`);
}
async function lifecycleMutate(button){
  const epoch=lifecycleEpoch,intent=Number(state.dialogIntentGeneration||0),csrf=state.csrf,action=button.dataset.lifecycleAction;
  const dialog=button.closest('dialog'),controls=$$('[data-lifecycle-action]',dialog);controls.forEach(b=>b.disabled=true);
  try{
    const result=await api(`/api/records/${Number(button.dataset.lifecycleId)}/lifecycle`,{method:'POST',body:{action,snapshot:button.dataset.lifecycleSnapshot}});
    if(!lifecycleCurrent(epoch,intent,csrf)){if(state.authenticated&&state.csrf===csrf)notify(result.message||'事项处理已保存');return;}
    closeDialog('edit',{force:true});if($('#detail-dialog').open)closeDialog('detail',{force:true});state.detail=null;
    if(typeof flowHomePlan!=='undefined'&&flowHomePlan?.record_id===result.root_record_id){flowHomePlan=null;removeDraft(`${draftPrefix}flow-selected-plan`);}
    notify(result.message||(action==='restore'?'已恢复，提醒需重新安排':action==='archive'?'已归档，可从已归档入口恢复':'已移到回收站，可恢复'));
    await loadView(true);
  }catch(error){if(lifecycleCurrent(epoch,intent,csrf)){const message=$('[data-lifecycle-error]',dialog);if(message)message.textContent=error.message;controls.forEach(b=>b.disabled=false);}}
}
document.addEventListener('click',async event=>{
  const button=event.target.closest?.('[data-lifecycle-record],[data-lifecycle-manage],[data-lifecycle-action],[data-lifecycle-tab],[data-lifecycle-page],[data-lifecycle-archive-tip],[data-lifecycle-restore-tip]');
  if(!button||button.disabled)return;
  try{
    if(button.dataset.lifecycleRecord){await openRecordLifecycle(button.dataset.lifecycleRecord);return;}
    if(button.hasAttribute('data-lifecycle-manage')){await openLifecycleArchive('archived');return;}
    if(button.dataset.lifecycleTab){await openLifecycleArchive(button.dataset.lifecycleTab);return;}
    if(button.dataset.lifecyclePage){await openLifecycleArchive(lifecycleArchiveView,{page:button.dataset.lifecycleList==='records'?Number(button.dataset.lifecyclePage):lifecycleArchivePage,tipPage:button.dataset.lifecycleList==='tips'?Number(button.dataset.lifecyclePage):lifecycleTipPage});return;}
    if(button.dataset.lifecycleAction){await lifecycleMutate(button);return;}
    if(button.dataset.lifecycleArchiveTip){await mutateButton(button,async()=>{await api('/api/priority-decisions',{method:'POST',body:{key:button.dataset.lifecycleArchiveTip,signature:button.dataset.lifecycleTipSignature,decision:'archive'}});if(button.isConnected&&typeof priorityRemoveRendered==='function')priorityRemoveRendered(button.dataset.lifecycleArchiveTip);notify('推进提示已归档，可以恢复');await loadView(true);});return;}
    if(button.dataset.lifecycleRestoreTip){const epoch=lifecycleEpoch,intent=Number(state.dialogIntentGeneration||0),csrf=state.csrf;await mutateButton(button,async()=>{await api('/api/priority-archives/restore',{method:'POST',body:{key:button.dataset.lifecycleRestoreTip,revision:Number(button.dataset.lifecycleTipRevision)}});if(!lifecycleCurrent(epoch,intent,csrf)){if(state.authenticated&&state.csrf===csrf)notify('推进提示已恢复；当前输入保留');return;}notify('推进提示已恢复，会按当前依据展示');await loadView(true);if(lifecycleCurrent(epoch,intent,csrf))await openLifecycleArchive('archived',{page:lifecycleArchivePage,tipPage:lifecycleTipPage});});}
  }catch(error){notify(error.message,true);}
});
