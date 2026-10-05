'use strict';

function flowAttachmentItems(form){
  try{return JSON.parse(form.elements.namedItem('attachment_items')?.value||'[]').filter(a=>Number.isInteger(a.material_id)&&a.material_id>0).slice(0,10);}catch{return [];}
}
function flowAttachmentPicker(){
  return `<div class="flow-attachment-picker"><input type="hidden" name="attachment_items" value="[]"><input type="hidden" name="attachment_attempts" value="{}"><label class="button flow-file-label">添加附件<input class="flow-file-input" type="file" multiple accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv,.png,.jpg,.jpeg,.doc,.ppt,.xls" aria-label="添加交流附件"></label><small>项目材料一起发，秘书结合准备；每份20MB，最多10份。</small><div data-flow-selected-files aria-live="polite"></div></div>`;
}
async function flowUploadKey(form,file){
  let identity=JSON.stringify([file.name,file.size,file.lastModified]);
  if(globalThis.crypto?.subtle){const digest=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());identity=file.name+':'+Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');}
  let attempts={};try{attempts=JSON.parse(form.elements.namedItem('attachment_attempts').value||'{}');}catch{}
  if(!attempts[identity])attempts[identity]=newVisitRequestKey();
  form.elements.namedItem('attachment_attempts').value=JSON.stringify(attempts);saveDraft(form);return attempts[identity];
}
async function flowWaitAttachment(attachment,session){
  const deadline=Date.now()+35000;
  while(attachment.parse_status==='parsing'&&Date.now()<deadline&&state.authenticated&&state.csrf===session){
    await new Promise(resolve=>setTimeout(resolve,350));
    const data=await api('/api/materials/'+attachment.material_id);if(data.document)Object.assign(attachment,data.document);
  }
  return attachment;
}
function flowAttachmentsHTML(items=[]){
  if(!items.length)return '';
  return `<div class="flow-attached-materials"><strong>交流材料 · ${items.length} 份</strong>${items.map(a=>`<div class="flow-attached-file"><button type="button" class="text-button" data-material="${Number(a.material_id||a.id)}">${e(a.filename||a.title||'附件材料')}</button><small>${e(a.parse_status==='needs_text'||a.readable===false?(a.parse_error||a.error||'正文待补充'):a.parse_error? a.parse_error:a.truncated?'已读取部分正文，原件完整保留':'材料已保存，原件可回看')}</small>${a.download_url?`<a href="${e(a.download_url)}" download>下载原件</a>`:''}</div>`).join('')}<p class="form-hint">文件记载作为准备与画像候选的来源，实际情况仍需核实。</p></div>`;
}
function flowAttachmentReferences(refs=[]){
  return refs.length?`<details class="flow-material-references"><summary>查看材料依据 · ${refs.length} 处</summary>${refs.map(ref=>`<blockquote><button type="button" class="text-button" data-material="${Number(ref.material_id)}">材料 #${Number(ref.material_id)}${ref.version_id?' · 正文版本 '+Number(ref.version_id):''}</button><p>${e(ref.quote)}</p></blockquote>`).join('')}</details>`:'';
}
function flowRefreshSelectedFiles(form){
  const slot=form.querySelector('[data-flow-selected-files]');if(!slot)return;
  const items=flowAttachmentItems(form);
  slot.innerHTML=items.map((a,i)=>`<div class="flow-selected-file"><button type="button" class="text-button" data-material="${a.material_id}">${e(a.filename||a.title)}</button><small>${a.parse_status==='needs_text'?'正文未读出，可在材料页补文字':a.parse_status==='parsing'?'原件已保存，正在读取正文':e(a.parse_error||'已上传，可结合准备')}</small><button type="button" class="text-button" data-flow-remove-attachment="${i}" aria-label="取消附加 ${e(a.filename||a.title)}">移出这次输入</button></div>`).join('');
  if(form.dataset.uploading)slot.insertAdjacentHTML('beforeend','<p class="form-hint" role="status">正在上传附件，请稍候；原话可以继续输入。</p>');
  form.text.required=!items.length;
}
async function flowUploadAttachments(input){
  const form=input.closest('[data-flow-form],[data-chat-attachment-form]');if(!form||form.dataset.uploading||form.dataset.saving)return;
  const selected=Array.from(input.files||[]),items=flowAttachmentItems(form),error=form.querySelector('.form-error');
  if(!selected.length)return;
  if(items.length+selected.length>10){error.textContent='每次最多附加10份材料。';input.value='';return;}
  if(selected.some(file=>file.size>20*1024*1024||file.size===0)){error.textContent='文件不能为空，且每份不能超过20MB。';input.value='';return;}
  const session=state.csrf,button=form.querySelector('[type="submit"]');form.dataset.uploading='yes';button.disabled=true;input.disabled=true;error.textContent='';flowRefreshSelectedFiles(form);
  try{
    for(const file of selected){
      const data=new FormData();data.append('request_id',await flowUploadKey(form,file));data.append('file',file,file.name);
      const response=await api('/api/materials/upload',{method:'POST',body:data});
      if(!state.authenticated||state.csrf!==session)return;
      const attachment={...response.attachment,title:response.material.title,material_id:response.material.id};
      if(!items.some(item=>item.material_id===attachment.material_id))items.push(attachment);form.elements.namedItem('attachment_items').value=JSON.stringify(items);saveDraft(form);
      flowRefreshSelectedFiles(form);
      await flowWaitAttachment(attachment,session);
      if(!state.authenticated||state.csrf!==session)return;
      form.elements.namedItem('attachment_items').value=JSON.stringify(items);saveDraft(form);flowRefreshSelectedFiles(form);
    }
    if(!form.isConnected)notify('附件已保存，可从录音与材料回看；回到原输入继续发送。');
  }catch(problem){if(state.csrf===session){error.textContent=problem.message+' 已成功上传的材料保留，可重新选择未成功的文件。';saveDraft(form);}}
  finally{delete form.dataset.uploading;input.disabled=false;input.value='';if(button.isConnected)button.disabled=Boolean(form.dataset.saving||(form.id==='discussion-message-form'&&(state.discussionSaving||state.discussion?.generating)));flowRefreshSelectedFiles(form);if(form.isConnected&&form.flowNextComposer){const next=form.flowNextComposer;delete form.flowNextComposer;flowReplaceComposer(form,next.plan,next.scope);}}
}
document.addEventListener('change',event=>{if(event.target.matches('.flow-file-input')){event.stopImmediatePropagation();const form=event.target.closest('form');if(form?.dataset.detailSubmitOwner==='detail-progress'&&form.dataset.saving){form.detailQueuedFileInput=event.target;return;}flowUploadAttachments(event.target);}},true);
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-flow-remove-attachment]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();const form=button.closest('[data-flow-form],[data-chat-attachment-form]');if(form.dataset.uploading||form.dataset.saving)return;
  const items=flowAttachmentItems(form);items.splice(Number(button.dataset.flowRemoveAttachment),1);form.elements.namedItem('attachment_items').value=JSON.stringify(items);saveDraft(form);flowRefreshSelectedFiles(form);
},true);
