'use strict';

// Durable server conversations. The browser draft is only an unsent scratchpad.
let flowHomePlan=null,flowHomeTurns=[],flowOpenEpoch=0;
const flowDrafts=new Map(),flowTimers=new Map();
const flowLabels={preparing:'待约 / 待完善',scheduled:'已安排',needs_attention:'时间需核对',recapped:'已复盘',cancelled:'已取消'};

function flowScopeAttrs(scope={}){
  return `data-customer-id="${Number(scope.customer_id||0)}" data-contact-id="${Number(scope.contact_id||0)}" data-project-id="${Number(scope.opportunity_id||0)}"`;
}
function flowScope(element){const scope=Object.fromEntries([['customer_id',element.dataset.customerId],['contact_id',element.dataset.contactId],['opportunity_id',element.dataset.projectId]].filter(([,v])=>Number(v)>0).map(([k,v])=>[k,Number(v)]));if(element.elements?.namedItem('customer_id')){const value=Number(element.elements.namedItem('customer_id').value);if(value)scope.customer_id=value;else delete scope.customer_id;}return scope;}
function flowShortcut(scope={},label='告诉秘书 / 安排下一步'){
  return `<button type="button" class="button primary" data-flow-new ${flowScopeAttrs(scope)}>${e(label)}</button>`;
}
function flowPlanCard(plan){
  const time=plan.start_at?formatDate(plan.start_at,true):`${plan.date||'日期待定'} · 时间待定`;
  return `<article class="flow-plan-card"><button type="button" class="inline-link" data-flow-plan="${Number(plan.id)}">${e(plan.title)}</button><p>${e(time)} · ${e(flowLabels[plan.status]||'已记录')}</p>${plan.goal?`<p>目标：${e(plan.goal)}</p>`:''}${plan.customer_name?`<small>${e(plan.customer_name)}</small>`:''}<div><button type="button" class="button small" data-flow-plan="${Number(plan.id)}">${plan.status==='recapped'?'回看交流':'补充 / 改期 / 复盘'}</button>${typeof lifecycleActions==='function'?lifecycleActions(plan.record_id):''}</div></article>`;
}
function flowOverviewHTML(plans=[]){return plans.length?`<section class="overview-section"><h2>待约 / 待完善的计划 · ${plans.length}</h2><p>日期先记下，目标、钟点和约定状态可以直接继续说。</p><div class="flow-plan-grid">${plans.map(flowPlanCard).join('')}</div></section>`:'';}
function flowGuideHTML(){
  return dialogHeader('像对真实秘书一样使用','一句话开始，继续说就能补充、改期和复盘。','edit')+`<div class="dialog-body usage-guide"><section class="guide-intro"><h3>先说一句话，别逐项填表</h3><p>首页点“告诉秘书”。先保存原话，再给回执；明确的安排直接执行。只有日期时先留在当天的“待约 / 时间待定”计划。</p>${flowShortcut({},'现在试一句话')}</section><ol class="guide-steps"><li><h3>1 · 记下安排</h3><blockquote>10月8日约林博士吃饭。</blockquote><p>秘书先记下日期，再问这次要谈什么。无需先补单位和电话。</p></li><li><h3>2 · 一次回答，自动填好</h3><blockquote>主要聊后量子平台合作，晚上六点，已经约好了，提前一个小时提醒。</blockquote><p>自动保存目标、议题和安排：18:00会面，17:00提醒。原话和修改历史保留。</p></li><li><h3>带上材料，让秘书一起准备</h3><p>在同一个输入框点“添加附件”，上传项目方案或汇报材料，再说“约何川吃饭，聊这份项目材料，帮我准备”。上传完成后只发送一次。后续计划和 AI 讨论可继续参考，材料原件在“录音与材料”保留。扫描件需补充可复制文字。</p></li><li><h3>3 · 临时改变，继续说</h3><blockquote>改到9号晚上六点半，title先不谈。</blockquote><p>更新同一件事。说“改期，时间还没定”会保留当前有效日程，再商量新安排；明确说“原时间不去了”才撤销旧日程。说“取消这次饭局”会取消整个活动。</p></li><li><h3>4 · 会后复盘，留下长期资料</h3><blockquote>饭吃完了，复盘：更关心技术落地，下一步先看试点方案。</blockquote><p>在同一次交流里保存结果，回到客户或联系人历程能继续回看。AI建议只有采纳后才成为待办。</p></li></ol><section><h3>少输入，也能清楚归属</h3><p>输入框上方显示“正在补充哪件事”。想记新事情时点“另记一件事”。称呼不完整时尽力识别；有同名或归属歧义时，给你候选对象点选。</p><p>单位未知的新联系人也可口述记录，后面从“新认识的人”归档。我方产品、案例和提醒习惯只需告诉秘书一次。</p><button type="button" class="button" data-flow-settings>设置我的业务与提醒习惯</button></section><p class="form-hint">当前本机版在网页提醒。企业微信真实送达、聆记全部录音自动获取，以实际连接能力为准。</p><p><a href="/guide#quick-capture" target="_blank" rel="noopener">完整使用流程与案例 ↗</a></p><button type="button" class="button" data-close="edit">关闭指引</button></div>`;
}
function flowIdentityHTML(plan){
  if(!plan?.identity_candidates?.length)return '';
  return `<div class="flow-identity"><p>秘书识别到的可能对象，点一下核对：</p>${plan.identity_candidates.map(c=>`<button type="button" class="button small" data-flow-identity="${c.id}" data-field="${e(c.field)}" data-plan="${plan.id}" data-customer-id="${Number(c.customer_id||0)}">${e(c.name)}</button>`).join('')}</div>`;
}
function flowPreparation(value={}){
  const questions=value.questions||[],materials=value.materials||[];
  if(!questions.length&&!materials.length&&!value.objective)return '';
  return `<details class="flow-preparation"><summary>秘书为这次交流准备的建议</summary>${value.objective?`<p>${e(value.objective)}</p>`:''}${questions.length?`<h4>可以聊的重点</h4><ul>${questions.map(q=>`<li>${e(q)}</li>`).join('')}</ul>`:''}${typeof flowAttachmentReferences==='function'?flowAttachmentReferences(value.references||[]):''}${materials.length?`<h4>出发前准备</h4><ul>${materials.map(q=>`<li>${e(q)}</li>`).join('')}</ul>`:''}<p class="form-hint">这是建议提纲，可直接告诉秘书调整。</p></details>`;
}
function flowTurnHTML(turn){
  const waiting=['queued','processing'].includes(turn.status),bad=['failed','needs_attention'].includes(turn.status);
  const suggestions=turn.result?.suggestions||[];
  return `<article class="flow-turn" data-flow-turn="${Number(turn.id)}" data-flow-status="${e(turn.status)}"><div class="flow-utterance"><small>我 · ${e(formatDate(turn.created_at,true))}</small><p>${e(turn.text)}</p></div><div class="flow-reply" role="status">${typeof matterTurnHTML==='function'?matterTurnHTML(turn):''}<small>秘书${turn.result?.mode==='basic'?' · 基础理解':''}</small><p>${e(waiting?(turn.attachment_message||'原话已保存，正在整理…'):bad?turn.error:turn.reply)}</p>${!waiting&&turn.result?.summary?`<p>${e(turn.result.summary)}</p>`:''}${bad?`<button type="button" class="button small" data-flow-retry="${Number(turn.id)}">重试整理</button>`:''}${typeof flowAttachmentsHTML==='function'?flowAttachmentsHTML(turn.attachments||[]):''}${flowPreparation(turn.result?.preparation)}${suggestions.length?`<details><summary>可选下一步 · ${suggestions.length} 条</summary><p class="form-hint">采纳后才进入你的跟进事项。</p>${suggestions.map((s,i)=>`<div class="flow-suggestion"><strong>${e(s.title)}</strong><p>${e(s.reason)}</p>${turn.adoptions?.[String(i+1)]?`<button type="button" class="text-button" data-record="${Number(turn.adoptions[String(i+1)])}">已采纳，查看待办</button>`:`<button type="button" class="button small" data-flow-adopt="${Number(turn.id)}" data-index="${i+1}">加入我的跟进</button>`}</div>`).join('')}</details>`:''}${!waiting?flowIntentActions(turn):''}<button type="button" class="text-button" data-raw-record="${Number(turn.record_id)}">回看原话</button>${typeof lifecycleActions==='function'?lifecycleActions(turn.record_id):''}${turn.plan_id?`<button type="button" class="text-button" data-flow-plan="${Number(turn.plan_id)}">查看这次计划</button>`:''}</div></article>`;
}
function flowIntentActions(turn){
  const intent=turn.result?.intent;
  if(intent==='research')return `<button type="button" class="button small" data-flow-research="${Number(turn.id)}">研究这个单位</button>`;
  if(intent==='discussion')return `<button type="button" class="button small" data-flow-discuss="${Number(turn.id)}">继续讨论下一步</button>`;
  if(intent==='work_plan')return `<button type="button" class="button small" data-secretary-progress="plan" data-period="week">查看并整理我的工作</button>`;
  if(intent==='contact')return '<button type="button" class="button small" data-flow-prospects>查看新认识的人</button>';
  return '';
}
function flowComposer(plan=null,scope={},home=false,composeKey=''){
  const key=plan?`plan-${plan.id}`:composeKey||(home&&typeof flowHomeComposeId==='function'?flowHomeComposeId():`scope-${JSON.stringify(scope)}`);
  return `<form id="${home?'flow-home-form':'flow-conversation-form'}" class="flow-composer" data-flow-form data-id="${e(key)}" data-home="${home?'yes':''}" data-plan-id="${Number(plan?.id||0)}" data-plan-revision="${Number(plan?.revision||0)}" ${flowScopeAttrs(scope)}><div class="flow-current">${plan?`<strong>正在补充：${e(plan.title)}</strong><span>${e(plan.date||'日期待定')} · ${e(flowLabels[plan.status]||'已记录')}</span><button type="button" class="text-button" data-flow-separate>另记一件事</button>`:`<strong>新的一件事 · 随口告诉秘书</strong>${home?'<button type="button" class="text-button" data-flow-separate>另记一件事</button>':''}`}</div>${flowIdentityHTML(plan)}${plan?.question?`<p class="flow-question">${e(plan.question)}</p>`:''}<label for="${home?'flow-home-text':'flow-conversation-text'}">${plan?'继续补充这次安排':'想到什么，先告诉秘书'}</label><textarea id="${home?'flow-home-text':'flow-conversation-text'}" name="text" rows="3" maxlength="6000" required placeholder="${plan?'如：主要聊平台合作，晚上六点，已经约好，提前一小时提醒。也可以说：改到9号晚上六点半。':'如：10月8日约林博士吃饭。或：刚和客户聊完，想法先记在这里。'}"></textarea>${typeof flowAttachmentPicker==='function'?flowAttachmentPicker():''}<input name="request_id" type="hidden"><input name="request_text" type="hidden"><div class="flow-submit-row"><div><button type="button" class="button" data-audio-entry="${Number(scope.customer_id||plan?.customer_id||0)}">${icon('mic')} 语音 / 转写</button><button type="button" class="button" data-import-material="listen_note">聆记录音</button></div><button type="submit" class="button primary">告诉秘书</button></div><p class="form-hint">原话先保存；明确的安排直接执行，必要信息再追问。会面时间与提前提醒分别记录。</p><p class="form-error" role="alert"></p></form>`;
}
async function openFlowAudio(sourceForm){
  const scope=flowScope(sourceForm),planId=Number(sourceForm.dataset.planId),revision=Number(sourceForm.dataset.planRevision),session=state.csrf;
  saveDraft(sourceForm);
  await openAudio(scope.customer_id||null);
  if(!state.authenticated||state.csrf!==session)return;
  const form=$('#audio-capture-form,#customer-command-form',$('#edit-content'));if(!form)return;
  const root=form.parentElement;root.classList.add('flow-conversation');
  const inheritedAttachments=sourceForm.elements.namedItem('attachment_items')?.value||'[]';
  if(typeof flowAttachmentPicker==='function'){form.insertAdjacentHTML('beforeend',flowAttachmentPicker());form.elements.namedItem('attachment_items').value=inheritedAttachments;}
  const wrapper=document.createElement('div');wrapper.dataset.flowComposerSlot='';form.before(wrapper);wrapper.append(form);
  form.setAttribute('data-flow-form','');form.dataset.planId=planId;form.dataset.planRevision=revision;form.dataset.id=planId?`plan-${planId}-audio`:`audio-${JSON.stringify(scope)}`;
  form.dataset.customerId=scope.customer_id||'';form.dataset.contactId=scope.contact_id||'';form.dataset.projectId=scope.opportunity_id||'';
  if(!$('[name="request_id"]',form))form.insertAdjacentHTML('beforeend','<input name="request_id" type="hidden"><input name="request_text" type="hidden">');
  form.insertAdjacentHTML('afterbegin',`<div class="flow-current"><strong>${e(planId?'语音补充当前计划':'语音记录一件新事')}</strong></div>`);
  for(const key of ['matterId','matterRevision','matterMode'])if(sourceForm.dataset[key])form.dataset[key]=sourceForm.dataset[key];
  if(form.dataset.matterId)$('.flow-current strong',form).textContent='语音补充当前事项';
  const slot=$('#audio-capture-results,#customer-command-result',root);slot.dataset.flowResults='';
  flowRestore(root);
}
async function loadSecretaryHome(records=[]){return await flowHistoryHome(records);}
function flowRestore(root=document){
  for(const form of $$('[data-flow-form]',root)){
    if(form.dataset.detailSubmitOwner==='detail-progress')continue;
    const draft=flowDrafts.get(form.dataset.id);if(draft&&!form.text.value){form.text.value=draft.text||'';form.request_id.value=draft.request_id||'';form.request_text.value=draft.request_text||'';}
  }
  if(typeof flowRefreshSelectedFiles==='function')for(const form of $$('[data-flow-form]',root))flowRefreshSelectedFiles(form);
  for(const node of $$('[data-flow-turn]',root))flowPoll(Number(node.dataset.flowTurn),node);
}
function flowPoll(id,node){
  if(!['queued','processing'].includes(node.dataset.flowStatus))return;
  if(flowTimers.has(node))return;
  const key=state.csrf;
  const timer=setTimeout(async()=>{
    flowTimers.delete(node);
    if(!node.isConnected||!state.authenticated||state.csrf!==key||node.closest('dialog')?.open===false)return;
    try{
      const turn=(await api(`/api/secretary/turns/${id}`)).turn;
      if(!node.isConnected||state.csrf!==key)return;
      if(['queued','processing'].includes(turn.status)){flowPoll(id,node);return;}
      const root=node.closest('.flow-home,.flow-conversation');
      const fresh=document.createElement('div');fresh.innerHTML=flowTurnHTML(turn);node.replaceWith(fresh.firstElementChild);
      const form=root?$('[data-flow-form]',root):null;
      if(turn.plan&&form&&!form.hasAttribute('data-arrangement-composer')&&!Number(form.dataset.matterId)&&Number(form.dataset.planId)===0&&!form.text.value&&(typeof flowHomeRecognizePlan!=='function'||flowHomeRecognizePlan(turn,form))){
        flowReplaceComposer(form,turn.plan,flowScope(form));
      }else if(turn.plan&&form&&!form.hasAttribute('data-arrangement-composer')&&!Number(form.dataset.matterId)&&Number(form.dataset.planId)===turn.plan.id&&(typeof flowHomeRecognizePlan!=='function'||flowHomeRecognizePlan(turn,form))){flowReplaceComposer(form,turn.plan,flowScope(form));}
      if(typeof refreshArrangementAfterTurn==='function')await refreshArrangementAfterTurn(turn);
      if(Number(form?.dataset.matterId)&&typeof refreshMatterAfterTurn==='function')await refreshMatterAfterTurn(turn,form);
      if(turn.plan&&state.detail?.type==='secretary-flow'&&state.detail.id===turn.plan.id&&$('#detail-dialog').open){
        const info=$('[data-flow-plan-summary]');if(info)info.innerHTML=flowPlanSummary(turn.plan);
        const history=$('[data-flow-history]');
        if(history){const full=(await api(`/api/secretary/plans/${turn.plan.id}`)).plan;if(history.isConnected&&state.detail?.id===full.id)history.innerHTML=flowHistoryHTML(full);}
      }
      // Refresh the visible work lists after processing, preserving unsent input.
      if(state.authenticated&&state.csrf===key&&['dashboard','overview','agenda'].includes(state.view)&&!document.querySelector('[data-flow-form][data-uploading]'))await loadView(true);
    }catch(error){if(node.isConnected){$('.flow-reply p',node).textContent='原话已保存，暂时无法读取整理结果。重新打开这次计划可继续。';}}
  },1800);
  flowTimers.set(node,timer);
}
function flowReplaceComposer(form,plan,scope){
  if(form.dataset.uploading){form.flowNextComposer={plan,scope};return;}
  const attachments=form.elements.namedItem('attachment_items')?.value||'[]';
  const attempts=form.elements.namedItem('attachment_attempts')?.value||'{}';
  const text=form.text.value,request=form.request_id.value,signature=form.request_text.value,home=form.dataset.home==='yes';
  clearDraft(form);flowDrafts.delete(form.dataset.id);
  const parent=form.parentElement;parent.innerHTML=flowComposer(plan,scope,home);
  const next=$('[data-flow-form]',parent);
  restoreDrafts(parent);
  next.text.value=text;next.request_id.value=request;next.request_text.value=signature;
  if(next.elements.namedItem('attachment_items'))next.elements.namedItem('attachment_items').value=attachments;
  if(next.elements.namedItem('attachment_attempts'))next.elements.namedItem('attachment_attempts').value=attempts;
  if(typeof flowRefreshSelectedFiles==='function')flowRefreshSelectedFiles(next);
  saveDraft(next);

}
function flowPlanSummary(plan){
  const notify=plan.reminder_at?formatDate(plan.reminder_at,true):'未启用';
  const stopped=['recapped','cancelled'].includes(plan.status);
  const reminder=stopped?`提醒已停止${plan.reminder_at?' · 原提前提醒：'+notify:''}`:plan.active_schedule?`提前提醒：${notify}${state.runtime?.wecom_connected?'':' · 当前在网页提醒'}`:`提醒尚未启用${plan.reminder_at?' · 拟提前提醒：'+notify:''}`;
  return `<div class="flow-plan-summary">${plan.status==='needs_attention'&&plan.active_schedule?`<p class="flow-question">原日程仍生效：${e(formatDate(plan.active_schedule.remind_at,true))}。下面是你提出的修改，解决冲突后再落实。</p>`:''}<p><strong>${e(plan.start_at?formatDate(plan.start_at,true):(plan.date||'日期待定')+' · 时间待定')}</strong> · ${e(flowLabels[plan.status]||'已记录')}</p><p>交流目标：${e(plan.goal||'等待你补充')}</p>${plan.topics?.length?`<p>谈什么：${e(plan.topics.join('；'))}</p>`:''}${plan.place?`<p>地点：${e(plan.place)}</p>`:''}<p>${e(reminder)}</p><p class="form-hint">${!stopped&&plan.duration_estimated?'日程暂按30分钟占用；可直接说预计聊多久。':''}</p>${plan.customer_id?`<button type="button" class="text-button" data-customer="${Number(plan.customer_id)}">${e(plan.customer_name||'客户资料')}</button>`:''}${plan.visit_id?`<button type="button" class="text-button" data-visit="${Number(plan.visit_id)}">交流与录音</button>`:''}${typeof flowAttachmentsHTML==='function'?flowAttachmentsHTML(plan.attachments||[]):''}${flowPreparation(plan.preparation)}${typeof lifecycleActions==='function'?lifecycleActions(plan.record_id):''}</div>`;
}
function flowHistoryHTML(plan){return `<summary>修改历史 · ${plan.history.length} 次</summary>${plan.history.map(h=>`<p>第 ${h.revision} 次 · ${e(formatDate(h.created_at,true))} · ${e(h.data.date||'日期待定')} ${h.data.start_at?e(clock(h.data.start_at)):'时间待定'}</p>`).join('')}`;}
async function openSecretaryPlan(id,options={}){
  if(typeof openArrangement==='function'&&!options.legacy)return await openArrangement(Number(id));
  if(!options.raw&&typeof openResolvedMatter==='function'&&await openResolvedMatter('plan',Number(id),'schedules'))return;
  const epoch=++flowOpenEpoch,session=state.csrf;
  const plan=(await api(`/api/secretary/plans/${Number(id)}`)).plan;
  if(epoch!==flowOpenEpoch||!state.authenticated||session!==state.csrf)return;
  state.detail={type:'secretary-flow',id:plan.id};
  openDialog('detail',dialogHeader(plan.title,'安排、准备和复盘，都留在这一次交流里。','detail')+`<div class="dialog-body flow-conversation"><section data-flow-plan-summary>${flowPlanSummary(plan)}</section><div class="flow-turns" data-flow-results>${plan.turns.map(flowTurnHTML).join('')}</div><div data-flow-composer-slot>${flowComposer(plan,{})}</div><div class="flow-home-tools">${plan.customer_id?`<button type="button" class="button" data-flow-discussion-plan="${Number(plan.id)}">与 AI 讨论如何推进</button>`:''}<button type="button" class="text-button" data-flow-new>另记一件事</button></div><details data-flow-history>${flowHistoryHTML(plan)}</details></div>`);
  flowRestore($('#detail-content'));
}
function openSecretaryCapture(scope={},fresh=false){
  ++flowOpenEpoch;
  openDialog('edit',dialogHeader('告诉秘书','随口说，先记下，再安排。','edit')+`<div class="dialog-body flow-conversation"><div data-flow-results></div><div data-flow-composer-slot>${flowComposer(null,scope,false,fresh?`capture-${newVisitRequestKey()}`:'')}</div></div>`);
  flowRestore($('#edit-content'));
  if(fresh)$('[data-flow-form]',$('#edit-content')).dataset.matterMode='fresh';
}
async function submitSecretaryFlow(form,event,options={}){
  event.preventDefault();
  const text=form.text.value.trim(),error=$('.form-error',form),button=$('[type="submit"]',form);
  const attachments=typeof flowAttachmentItems==='function'?flowAttachmentItems(form):[];
  if((!text&&!attachments.length)||form.dataset.saving||form.dataset.uploading)return;
  const payload={text,...flowScope(form),...(attachments.length?{material_ids:attachments.map(a=>a.material_id)}:{})};
  if(form.original_transcript?.value?.trim())payload.original_transcript=form.original_transcript.value;
  if(Number(form.dataset.matterId))Object.assign(payload,{matter_id:Number(form.dataset.matterId),matter_revision:Number(form.dataset.matterRevision)});
  if(form.dataset.matterMode)payload.matter_mode=form.dataset.matterMode;
  if(Number(form.dataset.planId))Object.assign(payload,{plan_id:Number(form.dataset.planId),expected_revision:Number(form.dataset.planRevision)});
  const signature=JSON.stringify(payload),session=state.csrf;
  if(form.request_text.value!==signature||!form.request_id.value)form.request_id.value=newVisitRequestKey();
  form.request_text.value=signature;payload.request_id=form.request_id.value;
  form.dataset.saving='yes';button.disabled=true;if(!options.detailProgress)for(const input of form.querySelectorAll('.flow-file-input'))input.disabled=true;error.textContent='';saveDraft(form);
  try{
    const turn=(await api('/api/secretary/turns',{method:'POST',body:payload})).turn;
    if(!state.authenticated||state.csrf!==session)return;
    if(options.detailProgress){await detailFlowAccepted(form,turn);return;}
    // Preserve a new utterance typed while the durable save was in flight.
    if(form.text.value.trim()===text&&JSON.stringify(flowAttachmentItems(form).map(a=>a.material_id))===JSON.stringify(attachments.map(a=>a.material_id))){form.text.value='';form.request_id.value='';form.request_text.value='';if(form.elements.namedItem('attachment_items'))form.elements.namedItem('attachment_items').value='[]';if(form.elements.namedItem('attachment_attempts'))form.elements.namedItem('attachment_attempts').value='{}';if(typeof flowRefreshSelectedFiles==='function')flowRefreshSelectedFiles(form);clearDraft(form);flowDrafts.delete(form.dataset.id);}
    notify('原话已保存，秘书正在整理。');
    if(typeof flowHistorySavedTurn==='function')flowHistorySavedTurn(turn,form);
    if(!form.isConnected||form.closest('dialog')?.open===false)return;
    const root=form.closest('.flow-home,.flow-conversation'),slot=$('[data-flow-results]',root);
    slot.insertAdjacentHTML('beforeend',flowTurnHTML(turn));flowPoll(turn.id,slot.lastElementChild);
  }catch(problem){if(options.detailProgress)await detailHandleError(form,problem);else error.textContent=problem.message;saveDraft(form);}
  finally{delete form.dataset.saving;for(const input of form.querySelectorAll('.flow-file-input'))input.disabled=false;if(button.isConnected)button.disabled=Boolean(form.dataset.uploading||(options.detailProgress&&detailWriteBlocked(form)));if(options.detailProgress&&form.detailQueuedFileInput){const input=form.detailQueuedFileInput;delete form.detailQueuedFileInput;await flowUploadAttachments(input);}}
}
async function openSecretarySettings(){
  const values=await api('/api/secretary/settings');
  openDialog('edit',dialogHeader('让秘书了解我','告诉秘书你的能力和习惯，后续建议更贴近你的业务。','edit')+`<div class="dialog-body"><form id="flow-settings-form" data-revision="${values.revision}"><label>我方业务、产品、优势与可用案例<textarea name="business_context" rows="7" maxlength="12000" placeholder="如：主要做数据安全和商用密码，擅长密钥管理；可做两周试点，有政务行业案例。">${e(values.business_context)}</textarea></label><label>默认提前提醒<select name="remind_minutes">${[['','不默认提前提醒'],['0','到点提醒'],['15','提前15分钟'],['30','提前30分钟'],['60','提前1小时'],['1440','提前1天'],...(values.remind_minutes!==null&&![0,15,30,60,1440].includes(values.remind_minutes)?[[String(values.remind_minutes),`提前${values.remind_minutes}分钟`]]:[])].map(([v,l])=>`<option value="${v}" ${String(values.remind_minutes??'')===v?'selected':''}>${l}</option>`).join('')}</select></label><p class="form-hint">用于执行日程的提前提醒。没有默认提醒也可以加入日程；已有提醒保持。</p><label class="checkbox-label"><input type="checkbox" name="arrangement_auto_check" ${values.arrangement_auto_check?'checked':''}>为新的待落实安排自动设置推进点</label><p class="form-hint">默认关闭。开启后根据确定期限建议推进日期；你明确说的“周五再问”始终优先。已有安排不会被批量修改。</p><p class="form-error" role="alert"></p><button class="button primary" type="submit">保存习惯</button></form></div>`);
}
async function openSecretaryProspects(){
  const [data,customers]=await Promise.all([api('/api/secretary/prospects'),api('/api/customers?page_size=100')]);
  openDialog('edit',dialogHeader('新认识的人','单位还不知道，也可以留下联系方式和认识背景。','edit')+`<div class="dialog-body"><p>直接告诉秘书：“今天新认识李工，电话是……，单位暂时不知道。”</p>${flowShortcut({},'记下新认识的人')}${data.items.map(p=>`<article class="flow-plan-card"><h3>${e(p.name)}</h3><p>${e([p.phone,p.wechat?'微信：'+p.wechat:''].filter(Boolean).join(' · '))}</p><p>${e(p.notes)}</p><button type="button" class="text-button" data-record="${Number(p.source_record_id)}">原话</button>${p.status==='linked'?`<button type="button" class="button small" data-customer="${Number(p.customer_id)}">已归入客户档案</button>`:`<form data-flow-prospect-form data-id="${p.id}" data-updated="${p.updated_at}"><label>后来确认的单位<select name="customer_id" required><option value="">选择已有单位</option>${customers.items.map(c=>`<option value="${c.id}">${e(c.name)}</option>`).join('')}</select></label><label>关联已有联系人，或新建<select name="contact_id"><option value="">新建一位联系人</option></select></label><p class="form-error" role="alert"></p><button type="submit" class="button small">归入这个单位</button></form>`}</article>`).join('')||'<p class="muted">暂时没有新联系人线索。</p>'}</div>`);
}
document.addEventListener('submit',event=>{
  const form=event.target;
  if(form.dataset?.detailSubmitOwner==='detail-progress')return;
  if(form.matches('[data-flow-form]')){event.preventDefault();event.stopImmediatePropagation();submitSecretaryFlow(form,event);}
  if(form.id==='flow-settings-form'){
    event.preventDefault();event.stopImmediatePropagation();const button=$('[type="submit"]',form);button.disabled=true;
    api('/api/secretary/settings',{method:'PATCH',body:{business_context:form.business_context.value,remind_minutes:form.remind_minutes.value===''?null:Number(form.remind_minutes.value),expected_revision:Number(form.dataset.revision),arrangement_auto_check:form.arrangement_auto_check.checked}}).then(()=>{clearDraft(form);closeDialog('edit',{force:true});notify('秘书已记住你的业务和提醒习惯。');}).catch(error=>{$('.form-error',form).textContent=error.message;}).finally(()=>button.disabled=false);
  }
  if(form.matches('[data-flow-prospect-form]')){
    event.preventDefault();event.stopImmediatePropagation();const payload={customer_id:Number(form.customer_id.value),expected_updated_at:Number(form.dataset.updated)};if(form.contact_id.value)payload.contact_id=Number(form.contact_id.value);
    api(`/api/secretary/prospects/${form.dataset.id}/link`,{method:'POST',body:payload}).then(openSecretaryProspects).catch(error=>$('.form-error',form).textContent=error.message);
  }
},true);
document.addEventListener('input',event=>{const form=event.target.closest('[data-flow-form]');if(form)flowDrafts.set(form.dataset.id,{text:form.text.value,request_id:form.request_id.value,request_text:form.request_text.value});});
document.addEventListener('change',async event=>{
  const form=event.target.closest('[data-flow-prospect-form]');
  if(!form||event.target.name!=='customer_id')return;
  const selected=form.customer_id.value,session=state.csrf;
  form.contact_id.innerHTML='<option value="">新建一位联系人</option>';
  if(!selected)return;
  try{
    const profile=await api(`/api/customers/${Number(selected)}/profile`);
    if(!form.isConnected||form.customer_id.value!==selected||state.csrf!==session)return;
    form.contact_id.innerHTML='<option value="">新建一位联系人</option>'+(profile.contacts||[]).filter(c=>!c.archived).map(c=>`<option value="${c.id}">${e(c.name)}${c.department?' · '+e(c.department):''}${c.phone?' · '+e(c.phone):''}</option>`).join('');
  }catch(error){if(form.isConnected)$('.form-error',form).textContent=error.message;}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  if(Object.hasOwn(button.dataset,'detailOwnedButton')||button.closest('[data-detail-submit-owner]'))return;
  if('audioEntry' in button.dataset&&button.closest('[data-flow-form]')){
    event.preventDefault();event.stopImmediatePropagation();
    try{await openFlowAudio(button.closest('form'));}catch(error){notify(error.message);}return;
  }
  if('quickCapture' in button.dataset){event.preventDefault();event.stopImmediatePropagation();openSecretaryCapture();return;}
  const handled=['flowPlan','flowNew','flowSeparate','flowSettings','flowProspects','flowRetry','flowAdopt','flowDiscuss','flowResearch','flowDiscussionPlan','flowIdentity'];
  if(!handled.some(k=>k in button.dataset))return;
  event.preventDefault();event.stopImmediatePropagation();
  try{
    if('flowPlan' in button.dataset)return await openSecretaryPlan(button.dataset.flowPlan);
    if('flowNew' in button.dataset){const root=button.closest('.flow-conversation,.flow-home'),current=root?$('[data-flow-form]',root):null;if(current)return await flowStartNew(current);return openSecretaryCapture(flowScope(button));}
    if('flowSeparate' in button.dataset)return await flowStartNew(button.closest('form'));
    if('flowIdentity' in button.dataset){
      const form=button.closest('form'),plan=(await api(`/api/secretary/plans/${button.dataset.plan}`)).plan;
      const body={text:`确认关联：${button.textContent.trim()}`,request_id:newVisitRequestKey(),plan_id:plan.id,expected_revision:plan.revision,[button.dataset.field]:Number(button.dataset.flowIdentity)};
      if(Number(button.dataset.customerId))body.customer_id=Number(button.dataset.customerId);
      const turn=(await api('/api/secretary/turns',{method:'POST',body})).turn;
      const slot=$('[data-flow-results]',form.closest('.flow-home,.flow-conversation'));slot.insertAdjacentHTML('beforeend',flowTurnHTML(turn));flowPoll(turn.id,slot.lastElementChild);return;
    }
    if('flowSettings' in button.dataset)return await openSecretarySettings();
    if('flowProspects' in button.dataset)return await openSecretaryProspects();
    if('flowRetry' in button.dataset){const node=button.closest('[data-flow-turn]'),turn=(await api(`/api/secretary/turns/${button.dataset.flowRetry}/retry`,{method:'POST',body:{}})).turn;node.outerHTML=flowTurnHTML(turn);flowRestore();return;}
    if('flowAdopt' in button.dataset){const result=await api(`/api/secretary/turns/${button.dataset.flowAdopt}/adopt`,{method:'POST',body:{index:Number(button.dataset.index)}});button.dataset.record=result.record.id;delete button.dataset.flowAdopt;button.textContent='已采纳，查看待办';notify('已加入跟进；未指定时间，暂不提醒。');return;}
    if('flowDiscussionPlan' in button.dataset){const plan=(await api(`/api/secretary/plans/${button.dataset.flowDiscussionPlan}`)).plan;return await openDiscussionContext({customer_id:plan.customer_id,contact_id:plan.contact_id,contact_name:plan.person||'',opportunity_id:plan.opportunity_id,source_record_id:plan.record_id});}
    if('flowResearch' in button.dataset){const turn=(await api(`/api/secretary/turns/${button.dataset.flowResearch}`)).turn;openSecretaryCapture();const form=$('[data-flow-form]',$('#edit-content'));form.text.value=turn.text;form.dataset.captureResult='flow-research-result';form.insertAdjacentHTML('afterend','<div id="flow-research-result"></div>');return await prepareHomepageResearch(form,turn.text);}
    if('flowDiscuss' in button.dataset){const turn=(await api(`/api/secretary/turns/${button.dataset.flowDiscuss}`)).turn;return await openDiscussionContext({...turn.result?.scope,customer_id:turn.plan?.customer_id||turn.result?.scope?.customer_id,source_record_id:turn.record_id});}
  }catch(error){notify(error.message);}
},true);

// Restore after ordinary page renders; never replace an active typed draft.
new MutationObserver(()=>flowRestore()).observe(document.getElementById('main'),{childList:true});
