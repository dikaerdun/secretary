'use strict';

// The selected conversation is presentation state, never business archive state.
let flowHomeActiveTurn=0,flowHomeEpoch=0,flowHomeHistoryOpen=false,flowHomeComposeKey='';
let flowHomeHistoryCache={plans:[],turns:[],records:[]};
let flowHomeDraftMatter=null,flowHomeMatterMode='auto';
function flowHomeSetContext(matter=null,mode='auto'){
  flowHomeDraftMatter=matter;flowHomeMatterMode=mode==='fresh'?'fresh':'auto';
  writeDraft(`${draftPrefix}flow-home-context`,{matter,mode:flowHomeMatterMode});
}
function flowHomeComposeId(){
  if(!flowHomeComposeKey){flowHomeComposeKey=readDraft(`${draftPrefix}flow-home-compose-key`)?.key||newVisitRequestKey();writeDraft(`${draftPrefix}flow-home-compose-key`,{key:flowHomeComposeKey});}
  return `home-${flowHomeComposeKey}`;
}
function flowScratchHistory(){const saved=readDraft(`${draftPrefix}flow-home-scratch-history`);return Array.isArray(saved?.items)?saved.items:[];}
function flowSaveScratch(form){
  saveDraft(form);
  if(Number(form.dataset.planId))return;
  const values=formValues(form),hasFiles=String(values.attachment_items||'[]')!=='[]';
  if(!String(values.text||'').trim()&&!hasFiles)return;
  const id=form.dataset.home==='yes'?form.dataset.id:(form.dataset.homeScratchId||=flowScratchHistory().find(d=>d.source_form_id===form.dataset.id)?.id||`home-${newVisitRequestKey()}`);
  const items=flowScratchHistory().filter(d=>d.id!==id);
  const matterId=Number(form.dataset.matterId||0),matter=matterId?{id:matterId,revision:Number(form.dataset.matterRevision||0),title:form.dataset.matterTitle||'',customer_id:Number(form.dataset.customerId||0)||null,opportunity_id:Number(form.dataset.projectId||0)||null}:null;
  items.unshift({id,source_form_id:form.dataset.id,values,updated_at:Date.now()/1000,...(matter?{matter}:{}),matter_mode:form.dataset.matterMode==='fresh'?'fresh':'auto'});
  writeDraft(`${draftPrefix}flow-home-scratch-history`,{items});
}
function flowForgetScratch(id){writeDraft(`${draftPrefix}flow-home-scratch-history`,{items:flowScratchHistory().filter(d=>d.id!==id)});}
function flowHomeBusy(form){if(form?.dataset.uploading||form?.dataset.saving){notify('这句话或附件正在保存，完成后再切换；当前输入会保留。');return true;}return false;}
function flowHomeIntent(){return {epoch:flowHomeEpoch,csrf:state.csrf,navigation:Number(state.discussionNavigationGeneration||0)};}
function flowHomeCurrent(intent){return Boolean(state.authenticated&&state.view==='dashboard'&&intent.epoch===flowHomeEpoch&&intent.csrf===state.csrf&&intent.navigation===Number(state.discussionNavigationGeneration||0));}
function flowHistorySavedTurn(turn,form){
  flowHomeTurns=[turn,...flowHomeTurns.filter(t=>t.id!==turn.id)];
  flowHomeHistoryCache.turns=flowHomeTurns;
  // Retire the saved utterance from folded drafts, even when it was sent in a popup.
  const items=flowScratchHistory().filter(d=>d.id!==form.dataset.id&&d.id!==form.dataset.homeScratchId&&d.source_form_id!==form.dataset.id);
  writeDraft(`${draftPrefix}flow-home-scratch-history`,{items});
  // A different utterance may have been typed while the save request was pending.
  if(String(form.text.value||'').trim()||String(form.elements.namedItem('attachment_items')?.value||'[]')!=='[]')flowSaveScratch(form);
  if(form.dataset.home!=='yes'||!form.isConnected)return;
  flowHomeActiveTurn=turn.id;writeDraft(`${draftPrefix}flow-selected-turn`,{turn_id:turn.id});
}
function flowHomeRecognizePlan(turn,form){
  if(form.dataset.home!=='yes')return true;
  const selected=Number(form.dataset.planId);
  if(!selected&&Number(flowHomeActiveTurn)!==Number(turn.id))return false;
  if(selected&&selected!==turn.plan?.id)return false;
  flowHomePlan=turn.plan;flowHomeHistoryCache.plans=[turn.plan,...flowHomeHistoryCache.plans.filter(p=>p.id!==turn.plan.id)];flowHomeHistoryCache.turns=[turn,...flowHomeHistoryCache.turns.filter(t=>t.id!==turn.id)];writeDraft(`${draftPrefix}flow-selected-plan`,{plan_id:turn.plan.id});return true;
}
async function flowStartNew(form){
  if(!form||flowHomeBusy(form))return;
  flowSaveScratch(form);
  if(form.dataset.home!=='yes'){openSecretaryCapture({},true);return;}
  ++flowHomeEpoch;flowHomePlan=null;flowHomeActiveTurn=0;flowHomeHistoryOpen=false;
  flowHomeSetContext(null,'fresh');
  removeDraft(`${draftPrefix}flow-selected-plan`);removeDraft(`${draftPrefix}flow-selected-turn`);
  flowHomeComposeKey=newVisitRequestKey();writeDraft(`${draftPrefix}flow-home-compose-key`,{key:flowHomeComposeKey});
  const root=form.closest('.flow-home');if(root){root.innerHTML=flowHomeBody(null,[]);restoreDrafts(root);flowRestore(root);$('#flow-home-text')?.focus({preventScroll:true});}
  const intent=flowHomeIntent();await loadView(true);
  if(flowHomeCurrent(intent)){$('#flow-home-text')?.focus({preventScroll:true});notify('已开始新的一件事。之前的对话在下方最近记录里。');}
}
async function flowSelectHomePlan(id){
  const form=$('#flow-home-form');if(flowHomeBusy(form))return;if(form)flowSaveScratch(form);
  ++flowHomeEpoch;const intent=flowHomeIntent();
  const plan=(await api(`/api/secretary/plans/${Number(id)}`)).plan;if(!flowHomeCurrent(intent))return;
  flowHomePlan=plan;flowHomeActiveTurn=0;flowHomeHistoryOpen=false;
  flowHomeSetContext();
  writeDraft(`${draftPrefix}flow-selected-plan`,{plan_id:plan.id});removeDraft(`${draftPrefix}flow-selected-turn`);
  await loadView(true);if(flowHomeCurrent(intent))$('#flow-home-text')?.focus({preventScroll:true});
}
async function flowSelectHomeTurn(id){
  const form=$('#flow-home-form');if(flowHomeBusy(form))return;if(form)flowSaveScratch(form);
  ++flowHomeEpoch;const intent=flowHomeIntent();
  const turn=(await api(`/api/secretary/turns/${Number(id)}`)).turn;if(!flowHomeCurrent(intent))return;
  let plan=null;if(turn.plan_id){plan=(await api(`/api/secretary/plans/${Number(turn.plan_id)}`)).plan;if(!flowHomeCurrent(intent))return;}
  flowHomePlan=plan;flowHomeActiveTurn=turn.id;flowHomeHistoryOpen=false;
  flowHomeSetContext();
  flowHomeComposeKey=newVisitRequestKey();writeDraft(`${draftPrefix}flow-home-compose-key`,{key:flowHomeComposeKey});
  writeDraft(`${draftPrefix}flow-selected-turn`,{turn_id:turn.id});if(plan)writeDraft(`${draftPrefix}flow-selected-plan`,{plan_id:plan.id});else removeDraft(`${draftPrefix}flow-selected-plan`);
  await loadView(true);
}
async function flowSelectHomeDraft(id){
  const entry=flowScratchHistory().find(d=>d.id===id);if(!entry)throw new Error('这份草稿已处理，请刷新最近记录。');
  const form=$('#flow-home-form');if(flowHomeBusy(form))return;if(form)flowSaveScratch(form);
  ++flowHomeEpoch;flowHomePlan=null;flowHomeActiveTurn=0;flowHomeHistoryOpen=false;
  const intent=flowHomeIntent();let matter=entry.matter||null;
  if(Number(matter?.id)>0){try{matter=(await api(`/api/matters/${Number(matter.id)}`)).matter;}catch(error){if(error.status!==404)throw error;notify('这份草稿所属的事项暂时无法读取，原输入保留。请核对事项后继续。');}if(!flowHomeCurrent(intent))return;}
  flowHomeSetContext(matter,entry.matter_mode||'auto');
  removeDraft(`${draftPrefix}flow-selected-plan`);removeDraft(`${draftPrefix}flow-selected-turn`);
  flowHomeComposeKey=id.startsWith('home-')?id.slice(5):newVisitRequestKey();writeDraft(`${draftPrefix}flow-home-compose-key`,{key:flowHomeComposeKey});
  await loadView(true);if(!flowHomeCurrent(intent))return;
  const next=$('#flow-home-form');if(!next)return;
  applyDraftValues(next,entry.values);flowDrafts.set(next.dataset.id,{text:entry.values.text||'',request_id:entry.values.request_id||'',request_text:entry.values.request_text||''});
  if(typeof flowRefreshSelectedFiles==='function')flowRefreshSelectedFiles(next);saveDraft(next);next.text.focus({preventScroll:true});
}
function flowRecentEntries(){
  const {plans,turns,records}=flowHomeHistoryCache,known=new Set([...plans.map(p=>Number(p.record_id)),...turns.map(t=>Number(t.record_id))]);
  const entries=plans.filter(p=>Number(p.id)!==Number(flowHomePlan?.id||0)).map(p=>({key:`plan-${p.id}`,title:p.title,time:p.updated_at,status:flowLabels[p.status]||'已记录',preview:p.goal||`${p.date||'日期待定'} · ${p.start_at?clock(p.start_at):'时间待定'}`,attr:`data-flow-home-plan="${Number(p.id)}"`,label:'继续记录'}));
  const represented=new Set(plans.map(p=>Number(p.id)));if(flowHomePlan)represented.add(Number(flowHomePlan.id));
  for(const turn of turns){if(turn.plan_id&&!represented.has(Number(turn.plan_id))){represented.add(Number(turn.plan_id));if(turn.plan?.record_id)known.add(Number(turn.plan.record_id));entries.push({key:`plan-${turn.plan_id}`,title:turn.plan?.title||turn.text?.slice(0,80)||'交流计划',time:turn.updated_at||turn.created_at,status:flowLabels[turn.plan?.status]||'已记录',preview:turn.text,attr:`data-flow-home-plan="${Number(turn.plan_id)}"`,label:'继续记录'});}}
  for(const turn of turns){if(turn.plan_id||Number(turn.id)===Number(flowHomeActiveTurn))continue;const record=records.find(r=>Number(r.id)===Number(turn.record_id));entries.push({key:`turn-${turn.id}`,title:record?.title||turn.text?.slice(0,80)||'随手记录',time:turn.updated_at||turn.created_at,status:['queued','processing'].includes(turn.status)?'秘书正在整理':turn.status==='failed'||turn.status==='needs_attention'?'需补充 / 重试':'已记录',preview:turn.text,attr:`data-flow-home-turn="${Number(turn.id)}"`,label:'查看记录'});}
  for(const record of records){if(known.has(Number(record.id)))continue;entries.push({key:`record-${record.id}`,title:record.title,time:record.updated_at||record.created_at,status:recordStatus(record),preview:record.content,attr:`data-record="${Number(record.id)}"`,label:'查看记录'});}
  const currentId=flowHomeComposeId();for(const draft of flowScratchHistory()){if(!flowHomePlan&&draft.id===currentId)continue;entries.push({key:`draft-${draft.id}`,title:String(draft.values.text||'附件待发送').slice(0,80),time:draft.updated_at,status:'未发送草稿',preview:'留在此浏览器里，点击继续编辑。',attr:`data-flow-home-draft="${e(draft.id)}"`,label:'继续草稿'});}
  return entries.sort((a,b)=>Number(b.time||0)-Number(a.time||0));
}
function flowRecentHTML(){
  const entries=flowRecentEntries();return `<details class="flow-recent" data-flow-recent ${flowHomeHistoryOpen?'open':''}><summary>最近记录 <span>· ${entries.length}</span><small>点开记录，继续补充或处理</small></summary><div class="flow-recent-list">${entries.slice(0,12).map(item=>`<article class="flow-recent-item"><button type="button" class="flow-recent-title" ${item.attr} aria-label="${e(item.label+'：'+item.title)}">${e(item.title)} ${icon('arrow')}</button><p class="flow-recent-meta">${e(item.status)} · ${e(formatDate(item.time,true))}</p><p class="flow-recent-preview">${e(String(item.preview||'').slice(0,160))}</p></article>`).join('')||'<p class="form-hint">记下一句话后会留在这里。点“另记一件事”，就可以开始下一条。</p>'}<div class="flow-recent-footer"><button type="button" class="text-button" data-go="records">查看全部记录</button>${typeof lifecycleManageButton==='function'?lifecycleManageButton():''}</div></div></details>`;
}
function flowHomeComposerHTML(plan,currentTurns=[]){
  const matter=!plan?(currentTurns.find(turn=>Number(turn.matter?.id)>0)?.matter||flowHomeDraftMatter):null;
  let html=flowComposer(plan,matter?{customer_id:matter.customer_id,opportunity_id:matter.opportunity_id}:{},true);
  if(Number(matter?.id)>0){
    html=html.replace('data-flow-form',`data-flow-form data-matter-id="${Number(matter.id)}" data-matter-revision="${Number(matter.revision||0)}" data-matter-title="${e(matter.title||'当前事项')}" data-matter-mode="auto"`)
      .replace('<strong>新的一件事 · 随口告诉秘书</strong>',`<strong>正在补充：${e(matter.title||'当前事项')}</strong>`)
      .replace('想到什么，先告诉秘书','继续补充这件事')
      .replace('如：10月8日约林博士吃饭。或：刚和客户聊完，想法先记在这里。','如：这部分已经改好了，还缺一个案例。可以继续讨论、安排时间或添加附件。');
    if(matter.visibility&&matter.visibility!=='active'||matter.status==='ended')html=html.replace('<button type="submit"','<button type="submit" disabled').replace('<textarea ','<textarea disabled ')+`<p class="form-hint">这件事${matter.status==='ended'?'已结束':'已收起'}，请先恢复跟进后继续补充，或点“另记一件事”。<button type="button" class="text-button" data-matter-open="${Number(matter.id)}">查看这件事</button></p>`;
  }else if(!plan&&flowHomeMatterMode==='fresh')html=html.replace('data-flow-form','data-flow-form data-matter-mode="fresh"');
  return html;
}
function flowHomeBody(plan,currentTurns){return `<div class="capture-search-heading"><h2>想到什么，先告诉秘书。</h2><p>一句话记下安排，继续说就能补充、改期和复盘。</p></div><div data-flow-composer-slot>${flowHomeComposerHTML(plan,currentTurns)}</div><div class="flow-home-tools"><button type="button" class="text-button" data-flow-settings>我的业务 / 提醒习惯</button><button type="button" class="text-button" data-flow-prospects>新认识的人</button>${typeof lifecycleManageButton==='function'?lifecycleManageButton():''}</div>${!plan&&currentTurns.length?`<p class="form-hint">这条记录的原话与秘书回执在下面。<button type="button" class="text-button" data-record="${Number(currentTurns[0].record_id)}">补充 / 处理原记录</button></p>`:''}<div class="flow-turns" data-flow-results>${currentTurns.map(flowTurnHTML).join('')}</div>${flowRecentHTML()}`;}
async function flowHistoryHome(recentRecords=[]){
  const context=readDraft(`${draftPrefix}flow-home-context`);if(context){flowHomeDraftMatter=context.matter||null;flowHomeMatterMode=context.mode==='fresh'?'fresh':'auto';}
  const intent=flowHomeIntent();const [turnData,planData]=await Promise.all([api('/api/secretary/turns'),api('/api/secretary/plans')]);if(!flowHomeCurrent(intent))return '';
  let selectedPlan=Number(flowHomePlan?.id||readDraft(`${draftPrefix}flow-selected-plan`)?.plan_id||0),selectedTurn=Number(flowHomeActiveTurn||readDraft(`${draftPrefix}flow-selected-turn`)?.turn_id||0),currentTurns=[];
  const plans=planData.items||[],turns=turnData.items||[];
  if(!selectedPlan&&selectedTurn){let turn=turns.find(t=>t.id===selectedTurn);if(!turn){try{turn=(await api(`/api/secretary/turns/${selectedTurn}`)).turn;}catch(error){if(error.status!==404)throw error;selectedTurn=0;}}if(turn){if(turn.plan_id)selectedPlan=turn.plan_id;else currentTurns=[turn];}}
  let plan=null;if(selectedPlan){try{plan=(await api(`/api/secretary/plans/${selectedPlan}`)).plan;currentTurns=plan.turns||[];}catch(error){if(error.status!==404)throw error;selectedPlan=0;selectedTurn=0;}}
  if(!flowHomeCurrent(intent))return '';
  flowHomePlan=plan;flowHomeActiveTurn=selectedTurn;flowHomeTurns=turns;flowHomeHistoryCache={plans,turns,records:recentRecords};
  if(plan)writeDraft(`${draftPrefix}flow-selected-plan`,{plan_id:plan.id});else removeDraft(`${draftPrefix}flow-selected-plan`);
  if(selectedTurn)writeDraft(`${draftPrefix}flow-selected-turn`,{turn_id:selectedTurn});else removeDraft(`${draftPrefix}flow-selected-turn`);
  return `<section class="homepage-capture flow-home">${flowHomeBody(plan,currentTurns)}</section>`;
}
document.addEventListener('toggle',event=>{if(event.target.matches?.('[data-flow-recent]'))flowHomeHistoryOpen=event.target.open;},true);
document.addEventListener('close',event=>{if(!event.target.matches?.('dialog'))return;for(const form of $$('[data-flow-form]',event.target))flowSaveScratch(form);if(state.authenticated&&state.view==='dashboard'){const recent=$('[data-flow-recent]');if(recent)recent.outerHTML=flowRecentHTML();}},true);
document.addEventListener('click',async event=>{
  const button=event.target.closest?.('[data-flow-home-plan],[data-flow-home-turn],[data-flow-home-draft]');if(!button||button.disabled)return;
  event.preventDefault();event.stopImmediatePropagation();
  try{if(button.dataset.flowHomePlan)await flowSelectHomePlan(button.dataset.flowHomePlan);else if(button.dataset.flowHomeTurn)await flowSelectHomeTurn(button.dataset.flowHomeTurn);else await flowSelectHomeDraft(button.dataset.flowHomeDraft);}catch(error){notify(error.message,true);}
},true);
