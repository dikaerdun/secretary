'use strict';
let matterCorrectionEpoch=0;
async function openMatterCorrection(turnId,selectedId=0,fresh=false){
  const epoch=++matterCorrectionEpoch,session=state.csrf;
  const [receipt,list]=await Promise.all([api(`/api/secretary/turns/${Number(turnId)}/matter`),api('/api/matters?page_size=100')]);
  if(epoch!==matterCorrectionEpoch||!state.authenticated||state.csrf!==session)return;
  const turn=receipt.turn,candidates=(list.items||[]).filter(m=>m.status!=='ended');
  const options=candidates.map(m=>`<option value="${Number(m.id)}" ${Number(m.id)===Number(selectedId)?'selected':''}>${e(m.title)}${m.customer_name?' · '+e(m.customer_name):''}</option>`).join('');
  openDialog('edit',dialogHeader('核对这句话归哪件事','原话继续保留，归属可以再调整。','edit')+`<div class="dialog-body"><blockquote>${e(turn.text)}</blockquote><form id="matter-correction-form" data-id="${Number(turn.id)}" data-revision="${e(receipt.expected_turn_snapshot)}" data-turn-id="${Number(turn.id)}"><label for="matter-correction-target">补入已有事项，或另立一个目标</label><select id="matter-correction-target" name="target_id"><option value="fresh" ${fresh?'selected':''}>这是另一件事</option>${options}</select><div class="form-field" data-correction-title><label for="matter-correction-title">新事项名称</label><input id="matter-correction-title" name="title" maxlength="120" value="${e(turn.text.slice(0,80))}"></div><p class="form-hint">只调整本次原话和新建步骤的关联。已存在步骤的进展继续留在原处；实际日程保持原时间和提醒，共同相关的活动可同时引用。不会重新调用 AI 或重复生成提醒。</p><p class="form-error" role="alert"></p><div class="form-footer"><button type="button" class="button" data-close="edit">先不改</button><button type="submit" class="button primary">确认归属</button></div></form></div>`);
  const form=$('#matter-correction-form');form._receipt=receipt;form._matters=candidates;form._requestId=newVisitRequestKey();
  const sync=()=>{$('[data-correction-title]',form).hidden=form.elements.namedItem('target_id').value!=='fresh';};
  form.elements.namedItem('target_id').addEventListener('change',sync);sync();
}
async function submitMatterCorrection(form,event){
  event.preventDefault();const button=event.submitter||$('[type="submit"]',form),error=$('.form-error',form);if(button.disabled)return;
  const target=form.elements.namedItem('target_id').value,receipt=form._receipt;
  const body={request_id:form._requestId,expected_turn_snapshot:receipt.expected_turn_snapshot};
  if(target==='fresh')Object.assign(body,{matter_mode:'fresh',title:form.elements.namedItem('title').value.trim()});
  else {const matter=form._matters.find(m=>Number(m.id)===Number(target));if(!matter){error.textContent='请选择事项，或另立一个目标。';return;}Object.assign(body,{matter_id:matter.id,expected_matter_revision:matter.revision});}
  button.disabled=true;error.textContent='';const session=state.csrf;
  try{
    const result=await api(`/api/secretary/turns/${form.dataset.turnId}/matter`,{method:'POST',body});if(session!==state.csrf||!state.authenticated)return;
    clearDraft(form);closeDialog('edit',{force:true});const turn=result.turn;
    for(const old of $$(`[data-flow-turn="${Number(turn.id)}"]`))old.outerHTML=flowTurnHTML(turn);
    if(typeof flowHomeHistoryCache!=='undefined')flowHomeHistoryCache.turns=[turn,...flowHomeHistoryCache.turns.filter(t=>t.id!==turn.id)];
    if(turn.matter_id)await openMatter(turn.matter_id);else notify('归属已核对，原话和现有安排保留。');
    const warnings=(result.warnings||[]).map(matterText).filter(Boolean);if(result.needs_review)warnings.push('本次只确认了归属，请继续补充要执行的动作或安排。');if(warnings.length){const body=$('[data-matter-detail]');if(body)body.insertAdjacentHTML('afterbegin',`<p class="analysis-warning" role="status">${e(warnings.join('；'))}</p>`);else notify(warnings.join('；'));}
  }catch(err){error.textContent=err.message;}finally{button.disabled=false;}
}
document.addEventListener('click',async event=>{
  const button=event.target.closest?.('[data-matter-route-choice],[data-matter-route-fresh],[data-matter-route-correct]');if(!button||button.disabled)return;
  event.preventDefault();event.stopImmediatePropagation();
  try {await openMatterCorrection(button.dataset.matterTurn||button.dataset.matterRouteFresh||button.dataset.matterRouteCorrect,button.dataset.matterRouteChoice||0,Boolean(button.dataset.matterRouteFresh));}catch(error){notify(error.message,true);}
},true);
document.addEventListener('submit',event=>{if(event.target.id==='matter-correction-form'){event.stopImmediatePropagation();submitMatterCorrection(event.target,event);}},true);
