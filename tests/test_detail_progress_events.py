"""D3 business-write ownership through the actual production event chain.

The small DOM below models form controls and DOM event propagation, not layout.
It executes app, flow, attachment, arrangement and detail scripts in their
shipped order; only the network bootstrap is omitted. Layout evidence is separate.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
DOM_HARNESS = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=process.argv[1],NOW=Date.parse('2026-10-06T10:00:00+08:00');
const decode=s=>String(s).replace(/&(?:amp|lt|gt|quot|#39);/g,m=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[m]));
const encode=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dataName=n=>n.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
const attrName=n=>'data-'+n.replace(/[A-Z]/g,c=>'-'+c.toLowerCase());
function splitSelectors(s){let a=[],start=0,depth=0,quote='';for(let i=0;i<s.length;i++){const c=s[i];if(quote){if(c===quote)quote='';continue;}if(c==='"'||c==="'"){quote=c;continue;}if(c==='['||c==='(')depth++;if(c===']'||c===')')depth--;if(c===','&&!depth){a.push(s.slice(start,i));start=i+1;}}a.push(s.slice(start));return a;}
function selectorParts(s){let a=[],start=0,depth=0,quote='';for(let i=0;i<s.length;i++){const c=s[i];if(quote){if(c===quote)quote='';continue;}if(c==='"'||c==="'"){quote=c;continue;}if(c==='['||c==='(')depth++;if(c===']'||c===')')depth--;if((c===' '||c==='>')&&!depth){if(s.slice(start,i).trim())a.push(s.slice(start,i).trim());if(c==='>')a.push('>');start=i+1;}}if(s.slice(start).trim())a.push(s.slice(start).trim());return a;}
function simple(node,s){if(node.localName==='#document')return false;s=s.replace(/:not\(([^()]*)\)/g,(_,v)=>node.matches(v)?'!NEVER!':'');if(s.includes('!NEVER!'))return false;for(const pseudo of ['checked','disabled','last-child','first-child'])if(s.includes(':'+pseudo)){if(pseudo==='checked'&&!node.checked||pseudo==='disabled'&&!node.disabled||pseudo==='last-child'&&node.parentElement?.children.at(-1)!==node||pseudo==='first-child'&&node.parentElement?.children[0]!==node)return false;s=s.replace(':'+pseudo,'');}let ok=true;s=s.replace(/\[([^\]~^$*|=\s]+)(?:\s*([~^$*|]?=)\s*["']?([^\]"']*)["']?)?\]/g,(_,name,op,value)=>{const found=node.getAttribute(name);if(op){value=value.trim();ok&&=found!==null&&(op==='='?found===value:op==='^='?found.startsWith(value):op==='*='?found.includes(value):op==='$='?found.endsWith(value):found.split(' ').includes(value));}else ok&&=found!==null;return '';});if(!ok)return false;let tag=s.match(/^[\w*-]+/);if(tag&&tag[0]!=='*'&&node.localName!==tag[0].toLowerCase())return false;for(const m of s.matchAll(/([#.])([\w-]+)/g)){if(m[1]==='#'&&node.id!==m[2]||m[1]==='.'&&!node.classList.contains(m[2]))return false;}return true;}
function matches(node,s){return splitSelectors(s).some(part=>{const parts=selectorParts(part.trim());let cursor=node,i=parts.length-1;if(!simple(cursor,parts[i]||'*'))return false;while(i>0){i--;if(parts[i]==='>'){i--;cursor=cursor.parentElement;if(!cursor||!simple(cursor,parts[i]))return false;}else{cursor=cursor.parentElement;while(cursor&&!simple(cursor,parts[i]))cursor=cursor.parentElement;if(!cursor)return false;}}return true;});}
class DOMEvent {constructor(type,options={}){Object.assign(this,{type,bubbles:true,cancelable:true,defaultPrevented:false,submitter:null,eventPhase:0},options);}preventDefault(){if(this.cancelable)this.defaultPrevented=true;}stopPropagation(){this.stopped=true;}stopImmediatePropagation(){this.stopped=true;this.immediate=true;}}
function track(p){if(p&&typeof p.then==='function'){const q=Promise.resolve(p);pending.add(q);q.finally(()=>pending.delete(q)).catch(()=>{});}}
let source='harness',activeListener=null,pending=new Set(),trace=[],calls=[],handlerCalls=[],timers=[],notices=[],uuid=0;
class Element {
 constructor(tag,attrs={}){this.localName=tag.toLowerCase();this.attrs={...attrs};this.children=[];this.parentNode=null;this._text='';this._value=null;this.listeners=new Map();this.files=[];this.style={setProperty(){}};this.scrollTop=0;this.clientHeight=40;this.scrollHeight=80;this.classList={contains:c=>(this.attrs.class||'').split(/\s+/).includes(c),add:(...c)=>{this.attrs.class=[...new Set([...(this.attrs.class||'').split(/\s+/).filter(Boolean),...c])].join(' ');},remove:(...c)=>{this.attrs.class=(this.attrs.class||'').split(/\s+/).filter(v=>!c.includes(v)).join(' ');},toggle:(c,force)=>{const on=force??!this.classList.contains(c);on?this.classList.add(c):this.classList.remove(c);return on;}};this.dataset=new Proxy({}, {get:(_,key)=>this.attrs[attrName(key)],set:(_,key,value)=>{this.attrs[attrName(key)]=String(value);return true;},deleteProperty:(_,key)=>{delete this.attrs[attrName(key)];return true;},ownKeys:()=>Object.keys(this.attrs).filter(v=>v.startsWith('data-')).map(dataName),getOwnPropertyDescriptor:(_,key)=>Object.hasOwn(this.attrs,attrName(key))?{enumerable:true,configurable:true,value:this.attrs[attrName(key)]}:undefined});return new Proxy(this,{get:(target,key,receiver)=>{if(Reflect.has(target,key))return Reflect.get(target,key,receiver);if(target.localName==='form'&&typeof key==='string')return target.elements.namedItem(key)||undefined;}});}
 get tagName(){return this.localName.toUpperCase();}get parentElement(){return this.parentNode?.localName==='#document'?null:this.parentNode;}get id(){return this.attrs.id||'';}set id(v){this.attrs.id=v;}get name(){return this.attrs.name||'';}get type(){return this.attrs.type||(this.localName==='button'?'submit':this.localName==='input'?'text':this.localName);}set type(v){this.attrs.type=v;}get className(){return this.attrs.class||'';}set className(v){this.attrs.class=v;}
 get value(){if(this._value!==null)return this._value;if(this.localName==='select'){const opts=this.querySelectorAll('option');return (opts.find(n=>n.hasAttribute('selected'))||opts[0])?.getAttribute('value')||'';}if(this.localName==='textarea')return this.textContent;return this.attrs.value||'';}set value(v){this._value=String(v??'');if(this.type==='file'&&this._value==='')this.files=[];}get checked(){return this._checked??this.hasAttribute('checked');}set checked(v){this._checked=Boolean(v);}get disabled(){return this.hasAttribute('disabled');}set disabled(v){v?this.setAttribute('disabled',''):this.removeAttribute('disabled');}get hidden(){return this.hasAttribute('hidden');}set hidden(v){v?this.setAttribute('hidden',''):this.removeAttribute('hidden');}get open(){return this.hasAttribute('open');}set open(v){v?this.setAttribute('open',''):this.removeAttribute('open');}
 get form(){return this.closest('form')||(this.attrs.form?document.querySelector('#'+this.attrs.form):null);}get elements(){const a=this.querySelectorAll('input,textarea,select,button');a.namedItem=name=>a.find(n=>n.name===name)||null;return a;}get isConnected(){let n=this;while(n.parentNode)n=n.parentNode;return n===document;}get lastElementChild(){return this.children.at(-1)||null;}get firstElementChild(){return this.children[0]||null;}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}set textContent(v){this.children.forEach(n=>n.parentNode=null);this.children=[];this._text=String(v??'');}get childNodes(){return this.children;}get innerHTML(){return encode(this._text)+this.children.map(n=>n.outerHTML).join('');}get outerHTML(){const attrs=Object.entries(this.attrs).map(([k,v])=>' '+k+'="'+encode(v)+'"').join('');return '<'+this.localName+attrs+'>'+this.innerHTML+(['input','br','hr','img','meta','link','source','wbr'].includes(this.localName)?'':'</'+this.localName+'>');}set innerHTML(v){this.textContent='';parseHTML(v,this);}replaceChildren(...nodes){for(const n of [...this.children])n.remove();this._text='';this.append(...nodes);}
 hasAttribute(n){return Object.hasOwn(this.attrs,n);}getAttribute(n){return this.hasAttribute(n)?String(this.attrs[n]):null;}setAttribute(n,v){this.attrs[n]=String(v);}removeAttribute(n){delete this.attrs[n];}
 matches(s){return matches(this,s);}closest(s){let n=this;while(n&&n.localName!=='#document'){if(n.matches(s))return n;n=n.parentElement;}return null;}querySelectorAll(s){let a=[];for(const n of this.children){if(n.matches(s))a.push(n);a.push(...n.querySelectorAll(s));}return a;}querySelector(s){return this.querySelectorAll(s)[0]||null;}contains(n){while(n){if(n===this)return true;n=n.parentNode;}return false;}
 append(...nodes){for(let n of nodes){if(typeof n==='string'){this._text+=n;continue;}n.remove();n.parentNode=this;this.children.push(n);}}appendChild(n){this.append(n);return n;}prepend(n){n.remove();n.parentNode=this;this.children.unshift(n);}remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}}replaceWith(n){const p=this.parentNode,i=p?.children.indexOf(this);if(p&&i>=0){n.remove();p.children[i]=n;n.parentNode=p;this.parentNode=null;}}insertAdjacentHTML(where,html){if(where==='beforeend')parseHTML(html,this);else if(where==='afterbegin'){const holder=new Element('div');parseHTML(html,holder);for(const n of [...holder.children].reverse())this.prepend(n);}else throw new Error('Unsupported insertion '+where);}
 focus(){document.activeElement=this;}scrollIntoView(){}setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}showModal(){this.open=true;}close(){this.open=false;}getBoundingClientRect(){return {left:0,right:400,top:0,bottom:500};}
 addEventListener(type,fn,options=false){const capture=typeof options==='boolean'?options:Boolean(options.capture);if(!this.listeners.has(type))this.listeners.set(type,[]);this.listeners.get(type).push({fn,capture,source});}dispatchEvent(event){event.target=this;const ancestors=[];let p=this.parentNode;while(p){ancestors.push(p);p=p.parentNode;}const invoke=(node,capture,phase)=>{event.currentTarget=node;event.eventPhase=phase;event.immediate=false;for(const entry of node.listeners.get(event.type)||[]){if(entry.capture!==capture)continue;trace.push({source:entry.source,type:event.type,phase,defaultPreventedBefore:event.defaultPrevented});activeListener=entry.source;try{track(entry.fn(event));}finally{activeListener=null;}if(event.immediate)break;}};for(const n of [...ancestors].reverse()){invoke(n,true,1);if(event.stopped)return !event.defaultPrevented;}invoke(this,true,2);if(!event.immediate)invoke(this,false,2);if(event.bubbles&&!event.stopped)for(const n of ancestors){invoke(n,false,3);if(event.stopped)break;}event.eventPhase=0;return !event.defaultPrevented;}
 click(){if(this.disabled)return;const allowed=this.dispatchEvent(new DOMEvent('click'));if(allowed&&this.localName==='button'&&this.type==='submit')this.form?.requestSubmit(this);}requestSubmit(submitter=null){this.dispatchEvent(new DOMEvent('submit',{submitter:submitter||this.querySelector('[type="submit"]')}));}
}
function parseHTML(html,parent){const voids=new Set(['input','br','hr','img','meta','link','source','wbr','area','base','col','embed','param','track']);let stack=[parent],cursor=0;for(const m of String(html).matchAll(/<\/?([A-Za-z][\w:-]*)([^>]*?)\/?\s*>/g)){stack.at(-1)._text+=decode(String(html).slice(cursor,m.index).replace(/<!--[\s\S]*?-->/g,''));cursor=m.index+m[0].length;if(m[0].startsWith('</')){for(let i=stack.length-1;i>0;i--)if(stack[i].localName===m[1].toLowerCase()){stack.length=i;break;}continue;}let attrs={};for(const a of m[2].matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+)))?/g))attrs[a[1]]=decode(a[2]??a[3]??a[4]??'');const node=new Element(m[1],attrs);stack.at(-1).append(node);if(!voids.has(node.localName)&&!m[0].endsWith('/>'))stack.push(node);}stack.at(-1)._text+=decode(String(html).slice(cursor));}
const document=new Element('#document');document.documentElement=new Element('html');document.append(document.documentElement);document.body=new Element('body');document.documentElement.append(document.body);document.activeElement=document.body;document.createElement=tag=>new Element(tag);document.getElementById=id=>document.querySelector('#'+id);document.hidden=false;
document.body.innerHTML='<dialog id="detail-dialog" open><div id="detail-content"></div></dialog><dialog id="edit-dialog"><div id="edit-content"></div></dialog><div id="toast"></div><div id="content"></div>';
class FixedDate extends Date {constructor(...a){super(...(a.length?a:[NOW]));}static now(){return NOW;}}
class SyntheticFormData {constructor(){this.fields=[];}append(name,value,filename){this.fields.push({name,value:filename?{filename,size:value.size,type:value.type}:value});}}
const storage=new Map();const context={document,Event:DOMEvent,SubmitEvent:DOMEvent,HTMLElement:Element,Date:FixedDate,Math,Number,String,Boolean,JSON,Map,Set,WeakMap,Object,Array,Promise,URLSearchParams,Intl,structuredClone,console,crypto:{randomUUID:()=>`synthetic-request-${++uuid}`},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)},setInterval:()=>0,clearInterval(){},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},requestAnimationFrame:fn=>fn(),MutationObserver:class{observe(){}disconnect(){}},fetch:()=>{throw new Error('Unexpected real fetch');},navigator:{},location:{hash:'',pathname:'/'},window:{innerHeight:800,addEventListener(){},confirm:()=>true},ResizeObserver:class{observe(){}disconnect(){}}};context.window.document=document;vm.createContext(context);
context.FormData=SyntheticFormData;
const index=fs.readFileSync(path.join(ROOT,'secretary/static/index.html'),'utf8');const order=[...index.matchAll(/<script[^>]*src="([^"]+)"[^>]*>/g)].map(m=>m[1].split('?')[0].split('/').at(-1)).filter(n=>['app.js','secretary-flow.js','secretary-attachments.js','arrangement-queue.js','detail-progress.js'].includes(n));assert.deepEqual(order,['app.js','secretary-flow.js','secretary-attachments.js','arrangement-queue.js','detail-progress.js']);
for(const file of order){let code=fs.readFileSync(path.join(ROOT,'secretary/static',file),'utf8');if(file==='app.js'){const bootstrap=code.lastIndexOf("(async()=>{try{const data=await api('/api/session');");assert(bootstrap>0);code=code.slice(0,bootstrap);}source=file;vm.runInContext(code,context,{filename:file});}source='harness';
const run=code=>vm.runInContext(code,context);const state=run('state');Object.assign(state,{authenticated:true,csrf:'synthetic-session',view:'records',runtime:{mode:'local'},customers:[]});
for(const name of ['submitForm','submitSecretaryFlow','submitArrangementDecision','submitDetailPurpose']){const original=run(name);context['__wrapped_'+name]=function(...a){handlerCalls.push({name,listener:activeListener,purpose:a[0]?.dataset?.detailPurpose});const result=original(...a);track(result);return result;};run(name+'=__wrapped_'+name);}
context.__notify=(message,bad=false)=>notices.push({message,bad});run('notify=__notify');context.__loadView=async()=>{};run('loadView=__loadView');
function action(extra={}){return {activities:[],completion_snapshot:'completion-seen-1',schedule_snapshot:'schedule-seen-1',completion_effects:{scope:'record_completion',record_id:101,snapshot:'completion-seen-1',pending_task_count:1,pending_proposal_count:1,pending_tasks:[{id:501,title:'合成旧日程',remind_at:NOW/1000+86400,duration_minutes:30}],pending_proposals:[{id:601,title:'合成待确认提案',change_kind:'move',remind_at:NOW/1000+172800}]},...extra,record:{id:101,title:'合成待办',kind:'action',status:'following',source:'web',created_at:NOW/1000,original_content:'合成原话',customer_id:7,...extra.record}};}
function plan(extra={},outer={}){return {id:201,title:'合成安排',revision:3,record_id:301,turns:[],...outer,arrangement:{plan_id:201,title:'合成安排',revision:3,settling_state:'pending',source_visible:true,decision_mode:'self',blocking_reasons:['missing_clock'],can_apply:false,followup_enabled:true,proposed_execution:{time_spec:{precision:'date',date:'2026-10-09'}},next_check:{id:'check-old',status:'planned',time_spec:{precision:'date',date:'2026-10-07'},action:'合成推进点'},...extra}};}
let recordDTO=action(),planDTO=plan(),failures=[],writeGate=null,turnStatus='queued';
context.__api=async(url,options={})=>{const method=options.method||'GET',body=options.body?structuredClone(options.body):undefined;calls.push({url,method,body});if(method!=='GET'){if(writeGate)await writeGate.promise;if(failures.length){const f=failures.shift();const error=new Error(f.message||'合成保存错误');if(f.status)error.status=f.status;throw error;}if(url==='/api/secretary/turns')return {turn:{id:801,record_id:301,plan_id:201,status:turnStatus,text:body.text,created_at:NOW/1000}};if(url.endsWith('/arrangement-decisions'))return {plan_id:201,revision:4,receipt:'这次选择已保存，拟定方案仍需对方核对。',arrangement:planDTO.arrangement};if(url.endsWith('/complete-outcome'))return {record:{...recordDTO.record,status:'done'},message:'本次结果已保存',next_record:body.next_title?{id:102,title:body.next_title}:null};if(url.includes('/activities')){recordDTO.activities.unshift({id:901,content:body.content,created_at:NOW/1000});return {record:recordDTO.record};}if(url.endsWith('/schedule'))return {proposal:{id:602,status:'pending'},message:'合成待确认安排'};if(url.endsWith('/terms'))return {record:recordDTO.record};throw new Error('Unclassified business write '+method+' '+url);}if(url==='/api/records/101')return structuredClone(recordDTO);if(url==='/api/secretary/plans/201')return {plan:structuredClone(planDTO)};if(url==='/api/secretary/turns/801')return {turn:{id:801,record_id:301,plan_id:201,status:turnStatus,text:'合成已保存原话',error:'合成整理失败'}};throw new Error('Unexpected read '+url);};run('api=__api');
const basicApi=context.__api;context.__api=async(url,options={})=>{if(url!=='/api/materials/upload')return basicApi(url,options);assert(options.body instanceof SyntheticFormData,'upload must use original multipart contract');calls.push({url,method:options.method,body:structuredClone(options.body.fields)});return {material:{id:702,title:'请求中新增文件.txt'},attachment:{material_id:702,filename:'请求中新增文件.txt',parse_status:'ready'}};};run('api=__api');
function mount(type='action',dto=null){const container=document.querySelector('#detail-content');if(type==='action'){recordDTO=dto||action();state.detail={type:'record',id:101,data:recordDTO};context.__dto=recordDTO;container.innerHTML=run("detailActionHTML(__dto,'','')");}else{planDTO=dto||plan();context.__dto=planDTO;run('arrangementPlans.set(201,__dto)');state.detail={type:'arrangement',id:201,data:planDTO};container.innerHTML=run('arrangementDetailHTML(__dto)');}const root=container.querySelector('.detail-progress');assert(root,'actual renderer produced no detail root');context.__root=root;run('initializeDetailShell(document.querySelector("#detail-content"))');assert(root.detailSession,'initializer did not mount detail session');return root;}
function choose(root,purpose,operation=''){const select=root.querySelector('[data-detail-purpose-choice]');assert(select,'purpose select missing');select.value=purpose==='decision'?purpose+':'+operation:purpose;select.dispatchEvent(new DOMEvent('change'));const form=root.detailSession.form;assert.equal(form?.dataset.detailPurpose,purpose);return form;}
function set(form,name,value){let n=form.elements.namedItem(name);assert(n,'Missing actual field '+name+' in '+form.dataset.detailPurpose);if(n.type==='checkbox')n.checked=Boolean(value);else n.value=value;return n;}
function input(form,name,value,type='input'){const n=set(form,name,value);n.dispatchEvent(new DOMEvent(type));return n;}
function submit(form){form.querySelector('[type="submit"]').click();}function forceSubmit(form){form.requestSubmit(form.querySelector('[type="submit"]'));}
async function drain(){for(let i=0;i<30;i++){await Promise.resolve();if(!pending.size){await Promise.resolve();if(!pending.size)return;}await Promise.allSettled([...pending]);}throw new Error('Unsettled handler promises');}
async function started(){for(let i=0;i<30;i++){await Promise.resolve();if(calls.some(c=>c.method!=='GET'))return;}throw new Error('Write did not start');}
function defer(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function writes(){return calls.filter(c=>c.method!=='GET'&&c.url!=='/api/materials/upload');}function observed(){return {writes:writes(),uploads:calls.filter(c=>c.url==='/api/materials/upload'),reads:calls.filter(c=>c.method==='GET'),trace,handlers:handlerCalls,notices};}
function snapshot(form){return {values:run('formValues(__form)'),dataset:{...form.dataset},baseline:{...form.detailSession.baseline},notice:form.detailSession.root.querySelector('[data-detail-draft-notice]').textContent,error:form.querySelector('.form-error')?.textContent,receipt:form.detailSession.root.querySelector('[data-detail-receipt]').textContent};}
'''


def run_event_scenario(script):
    if not NODE:
        pytest.skip("Node runtime unavailable")
    completed = subprocess.run([NODE, "-e", DOM_HARNESS + "\n(async()=>{\n" + script + "\n})().catch(error=>{console.error(error.stack);process.exit(1);});", str(ROOT)], cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def assert_one_write(result, path, operation=None):
    assert len(result["writes"]) == 1, result
    write = result["writes"][0]
    assert write["method"] == "POST"
    assert write["url"] == path
    if operation:
        assert write["body"]["operation"] == operation
    return write


def assert_owned_chain(result, original_handler):
    submit_trace = [entry for entry in result["trace"] if entry["type"] == "submit"]
    assert [entry["source"] for entry in submit_trace] == ["secretary-flow.js", "arrangement-queue.js", "detail-progress.js"]
    assert all(entry["phase"] == 1 for entry in submit_trace)
    assert all(not entry["defaultPreventedBefore"] for entry in submit_trace)
    handlers = result["handlers"]
    assert [entry["name"] for entry in handlers] == ["submitDetailPurpose", original_handler]
    assert all(entry["listener"] == "detail-progress.js" for entry in handlers)


def test_harness_capture_bubble_and_immediate_stop_semantics():
    result = run_event_scenario("""
const host=new Element('section'),form=new Element('form');document.body.append(host);host.append(form);const sequence=[];
form.addEventListener('synthetic',()=>sequence.push('target-bubble'));
document.addEventListener('synthetic',()=>sequence.push('document-bubble'));
document.addEventListener('synthetic',()=>sequence.push('document-capture'),true);
host.addEventListener('synthetic',event=>{sequence.push('host-capture');event.stopImmediatePropagation();},true);
host.addEventListener('synthetic',()=>sequence.push('forbidden-second'),true);
form.dispatchEvent(new DOMEvent('synthetic'));
console.log(JSON.stringify(sequence));
""")
    assert result == ["document-capture", "host-capture"]


@pytest.mark.parametrize("purpose,endpoint", [("activity", "/api/records/101/activities"), ("organize", "/api/records/101/activities/organize")])
def test_owned_action_submission_real_chain_uses_selected_business_endpoint_once(purpose, endpoint):
    result = run_event_scenario(f"""
const root=mount(),form=choose(root,{json.dumps(purpose)});input(form,'content','合成实际进展');submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    assert assert_one_write(result, endpoint)["body"] == {"content": "合成实际进展"}
    assert_owned_chain(result, "submitForm")


def test_owned_supplement_capture_chain_saves_turn_once_and_receipt_is_only_queued():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','合成补充原话');submit(form);await drain();console.log(JSON.stringify({...observed(),receipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    write = assert_one_write(result, "/api/secretary/turns")
    assert write["body"]["text"] == "合成补充原话"
    assert write["body"]["plan_id"] == 201
    assert write["body"]["expected_revision"] == 3
    assert_owned_chain(result, "submitSecretaryFlow")
    assert "原话已保存" in result["receipt"] and "正在整理" in result["receipt"]


def test_owned_pure_progress_ignores_stale_check_fields_and_writes_only_progress():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'progress');form.insertAdjacentHTML('beforeend','<input name="check_handled" type="checkbox" checked><input name="check_id" value="stale-check">');input(form,'progress_text','合成仍在等答复');submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    write = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "update_progress")
    assert write["body"]["progress_text"] == "合成仍在等答复"
    assert "check_handled" not in write["body"] and "check_id" not in write["body"]
    assert write["body"]["expected_revision"] == 3
    assert_owned_chain(result, "submitArrangementDecision")


def test_candidate_change_is_local_then_explicit_submit_sends_only_candidate_decision():
    result = run_event_scenario("""
const root=mount('arrangement',plan({candidates:[{id:'candidate-a',label:'合成方案甲'},{id:'candidate-b',label:'合成方案乙'}],blocking_reasons:['choice_required']})),form=choose(root,'decision','select_candidate');input(form,'candidate_id','candidate-b','change');await drain();const before=writes().length;submit(form);await drain();console.log(JSON.stringify({...observed(),before,receipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    assert result["before"] == 0
    body = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "select_candidate")["body"]
    assert set(body) == {"operation", "candidate_id", "expected_revision", "request_id"}
    assert body["candidate_id"] == "candidate-b" and body["expected_revision"] == 3
    assert_owned_chain(result, "submitArrangementDecision")
    assert "仍需对方核对" in result["receipt"]


@pytest.mark.parametrize("kind,purpose,operation,field,text,endpoint", [("action", "activity", "", "content", "合成进展", "/api/records/101/activities"), ("arrangement", "supplement", "", "text", "合成补充", "/api/secretary/turns"), ("arrangement", "progress", "", "progress_text", "合成推进", "/api/secretary/plans/201/arrangement-decisions")])
def test_double_submit_before_first_response_has_one_write(kind, purpose, operation, field, text, endpoint):
    result = run_event_scenario(f"""
const root=mount({json.dumps(kind)}),form=choose(root,{json.dumps(purpose)},{json.dumps(operation)});input(form,{json.dumps(field)},{json.dumps(text)});writeGate=defer();submit(form);submit(form);await started();const inFlight=writes().length;writeGate.resolve();await drain();console.log(JSON.stringify({{...observed(),inFlight}}));
""")
    assert result["inFlight"] == 1
    assert_one_write(result, endpoint)


def test_action_background_duplicate_id_cannot_supply_selected_form_payload():
    result = run_event_scenario("""
const background=new Element('form',{id:'activity-form','data-record-id':'999'});background.innerHTML='<textarea id="activity-content" name="content">后台其他待办</textarea><button type="submit">后台保存</button>';document.body.prepend(background);
const root=mount(),form=choose(root,'activity');input(form,'content','当前详情真实进展');submit(form);await drain();console.log(JSON.stringify({...observed(),background:background.elements.namedItem('content').value}));
""")
    assert assert_one_write(result, "/api/records/101/activities")["body"]["content"] == "当前详情真实进展"
    assert result["background"] == "后台其他待办"


def test_activity_unknown_result_blocks_automatic_or_unreviewed_append():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'activity');input(form,'content','网络结果未知的合成进展');failures.push({message:'合成网络中断'});submit(form);await drain();const first=writes().length;submit(form);await drain();context.__form=form;console.log(JSON.stringify({...observed(),first,snapshot:snapshot(form)}));
""")
    assert result["first"] == 1
    assert_one_write(result, "/api/records/101/activities")
    assert result["snapshot"]["values"]["content"] == "网络结果未知的合成进展"
    assert "可能已经保存" in result["snapshot"]["notice"]
    assert "先看进展历史核对" in result["snapshot"]["notice"]


@pytest.mark.parametrize("operation,fields,paused", [
    ("pause", {"reason": "合成暂缓协调"}, False),
    ("abandon_coordination", {"reason": "合成放弃本轮"}, False),
    ("withdraw_execution", {"reason": "合成仅撤销当前日程", "continue_coordination": True}, False),
    ("cancel_activity", {"reason": "合成取消本次活动"}, False),
    ("start_reschedule", {}, False),
    ("set_deadline", {"deadline_date": "2026-10-08", "deadline_precision": "date", "deadline_strength": "required"}, False),
    ("set_check", {"check_date": "2026-10-07", "check_precision": "date", "check_action": "合成问客户进展", "followup_enabled": True}, False),
    ("resume", {"resume_choice": "none", "followup_enabled": False}, True),
])
def test_explicit_structure_purpose_has_exact_operation_and_one_write(operation, fields, paused):
    result = run_event_scenario(f"""
const root=mount('arrangement',plan({{settling_state:{json.dumps('paused' if paused else 'pending')},active_schedule:{{id:501,status:'pending',revision:4,remind_at:NOW/1000+86400}}}})),form=choose(root,'decision',{json.dumps(operation)});
for(const [name,value] of Object.entries({json.dumps(fields, ensure_ascii=False)}))input(form,name,value);submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    body = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", operation)["body"]
    assert body["expected_revision"] == 3
    assert_owned_chain(result, "submitArrangementDecision")
    if operation in {"withdraw_execution", "cancel_activity", "start_reschedule"}:
        assert body["expected_task_revision"] == 4
    if operation == "set_deadline":
        assert body["settle_deadline"]["time_spec"]["date"] == "2026-10-08"
        assert "proposed_execution" not in body and "next_check" not in body
    elif operation == "set_check":
        assert body["next_check"]["time_spec"]["date"] == "2026-10-07"
        assert "proposed_execution" not in body and "settle_deadline" not in body
    elif operation == "resume":
        assert body["next_check"] is None and body["followup_enabled"] is False


def test_explicit_complete_outcome_uses_visible_effect_snapshot_and_single_endpoint():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'outcome');input(form,'result','合成实际结果');input(form,'note','合成补充说明');input(form,'next_title','合成下一步');input(form,'next_content','合成下一步依据');input(form,'next_executor_kind','self','change');submit(form);await drain();console.log(JSON.stringify({...observed(),impact:form.querySelector('.detail-impact').textContent,receipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    body = assert_one_write(result, "/api/records/101/complete-outcome")["body"]
    assert body["expected_snapshot"] == "completion-seen-1"
    assert body["result"] == "合成实际结果\n合成补充说明"
    assert body["next_title"] == "合成下一步" and body["next_executor_kind"] == "self"
    assert "合成旧日程" in result["impact"] and "合成待确认提案" in result["impact"]
    assert_owned_chain(result, "submitForm")


def test_missing_completion_projection_prevents_request_even_on_synthetic_submit_event():
    result = run_event_scenario("""
const root=mount('action',action({completion_effects:null})),form=choose(root,'outcome');input(form,'result','保留实际结果草稿');forceSubmit(form);await drain();context.__form=form;console.log(JSON.stringify({...observed(),snapshot:snapshot(form),disabled:form.querySelector('[type="submit"]').disabled}));
""")
    assert result["writes"] == []
    assert result["disabled"] is True
    assert result["snapshot"]["values"]["result"] == "保留实际结果草稿"


def test_legacy_schedule_remains_one_pending_proposal_request_not_confirmation():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'legacy_schedule');input(form,'remind_at','2026-10-09T10:30');input(form,'duration_minutes','45');submit(form);await drain();console.log(JSON.stringify({...observed(),receipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    body = assert_one_write(result, "/api/records/101/schedule")["body"]
    assert body["expected_schedule_snapshot"] == "schedule-seen-1"
    assert body["duration_minutes"] == 45
    assert "待确认" in result["receipt"] and "尚未生效" in result["receipt"]
    assert_owned_chain(result, "submitForm")


@pytest.mark.parametrize("status", [409, 422])
@pytest.mark.parametrize("purpose,field,text", [("progress", "progress_text", "保留旧版本进展"), ("supplement", "text", "保留旧版本原话")])
def test_rejected_submission_preserves_baseline_fields_and_never_auto_resends(status, purpose, field, text):
    result = run_event_scenario(f"""
const root=mount('arrangement'),form=choose(root,{json.dumps(purpose)});input(form,{json.dumps(field)},{json.dumps(text)});const baseline={{...root.detailSession.baseline}},revision=form.dataset.planRevision;planDTO=plan({{revision:5}},{{revision:5}});failures.push({{status:{status},message:'合成具体{status}错误'}});submit(form);await drain();context.__form=form;console.log(JSON.stringify({{...observed(),snapshot:snapshot(form),baseline,revision,currentRevision:form.dataset.planRevision,latestRevision:root.detailSession.vm.baseline.plan_revision}}));
""")
    endpoint = "/api/secretary/turns" if purpose == "supplement" else "/api/secretary/plans/201/arrangement-decisions"
    assert_one_write(result, endpoint)
    assert result["snapshot"]["values"][field] == text
    assert result["snapshot"]["baseline"] == result["baseline"]
    assert result["currentRevision"] == result["revision"] == "3"
    assert f"{status}错误" in result["snapshot"]["error"]


@pytest.mark.parametrize("purpose,field,endpoint", [("supplement", "text", "/api/secretary/turns"), ("progress", "progress_text", "/api/secretary/plans/201/arrangement-decisions")])
def test_ledger_same_payload_retry_reuses_id_but_changed_payload_gets_new_id(purpose, field, endpoint):
    result = run_event_scenario(f"""
const root=mount('arrangement'),form=choose(root,{json.dumps(purpose)});input(form,{json.dumps(field)},'相同合成内容');failures.push({{message:'结果未知甲'}},{{message:'结果未知乙'}},{{message:'结果未知丙'}});submit(form);await drain();submit(form);await drain();input(form,{json.dumps(field)},'已修改的合成内容');submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    assert len(result["writes"]) == 3
    assert all(write["url"] == endpoint and write["method"] == "POST" for write in result["writes"])
    ids = [write["body"]["request_id"] for write in result["writes"]]
    assert ids[0] == ids[1] and ids[1] != ids[2]


@pytest.mark.parametrize("kind,purpose,field", [("action", "activity", "content"), ("arrangement", "supplement", "text"), ("arrangement", "progress", "progress_text")])
def test_text_entered_while_save_in_flight_remains_in_same_form(kind, purpose, field):
    result = run_event_scenario(f"""
const root=mount({json.dumps(kind)}),form=choose(root,{json.dumps(purpose)});input(form,{json.dumps(field)},'本次发送的合成文字');writeGate=defer();submit(form);await started();input(form,{json.dumps(field)},'请求中新增的合成文字');writeGate.resolve();await drain();context.__form=form;console.log(JSON.stringify({{...observed(),same:root.detailSession.form===form,snapshot:snapshot(form)}}));
""")
    assert len(result["writes"]) == 1
    assert result["same"] is True
    assert result["snapshot"]["values"][field] == "请求中新增的合成文字"


def test_independent_purpose_drafts_restore_nodes_values_baselines_and_send_only_selected():
    result = run_event_scenario("""
const root=mount('arrangement'),original=choose(root,'supplement');input(original,'text','补充安排未提交草稿');const baseline={...root.detailSession.baseline};const progress=choose(root,'progress');input(progress,'progress_text','本次仅提交纯进展');submit(progress);await drain();const restored=choose(root,'supplement');context.__form=restored;console.log(JSON.stringify({...observed(),same:original===restored,forms:root.querySelectorAll('form').length,snapshot:snapshot(restored),baseline}));
""")
    assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "update_progress")
    assert result["same"] is True and result["forms"] == 1
    assert result["snapshot"]["values"]["text"] == "补充安排未提交草稿"
    assert result["snapshot"]["baseline"] == result["baseline"]
    assert "独立保留" in result["snapshot"]["notice"]


@pytest.mark.parametrize("kind,purpose,field", [("action", "activity", "content"), ("arrangement", "supplement", "text"), ("arrangement", "progress", "progress_text")])
def test_success_clears_only_unchanged_sent_text(kind, purpose, field):
    result = run_event_scenario(f"""
const root=mount({json.dumps(kind)}),form=choose(root,{json.dumps(purpose)});input(form,{json.dumps(field)},'本次已保存且未变化');submit(form);await drain();context.__form=form;console.log(JSON.stringify({{...observed(),snapshot:snapshot(form)}}));
""")
    assert len(result["writes"]) == 1
    assert result["snapshot"]["values"][field] == ""


def test_attachment_draft_survives_unsupported_purpose_and_is_not_sent_with_progress():
    result = run_event_scenario("""
const root=mount('arrangement'),original=choose(root,'supplement');input(original,'text','含附件的未提交补充');set(original,'attachment_items',JSON.stringify([{material_id:701,filename:'合成材料.pdf',parse_status:'ready'}]));const raw=original.querySelector('input[type="file"]');raw.files=[{name:'尚未上传的新材料.txt',size:10,type:'text/plain'}];const progress=choose(root,'progress');input(progress,'progress_text','只保存进展');submit(progress);await drain();const notice=root.querySelector('[data-detail-draft-notice]').textContent,restored=choose(root,'supplement');console.log(JSON.stringify({...observed(),notice,same:restored===original,files:restored.querySelector('input[type="file"]').files.map(n=>n.name),attachments:JSON.parse(restored.elements.namedItem('attachment_items').value),text:restored.text.value}));
""")
    body = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "update_progress")["body"]
    assert "material_ids" not in body and "attachment_items" not in body
    assert result["same"] is True and result["text"] == "含附件的未提交补充"
    assert result["files"] == ["尚未上传的新材料.txt"]
    assert result["attachments"][0]["material_id"] == 701
    assert "不随本次提交发送" in result["notice"]


def test_new_file_selected_during_turn_save_is_retained_and_not_sent_in_previous_payload():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','提交中的合成原话');set(form,'attachment_items',JSON.stringify([{material_id:701,filename:'发送的合成材料.pdf',parse_status:'ready'}]));writeGate=defer();submit(form);await started();input(form,'text','下一份合成原话');const raw=form.querySelector('input[type="file"]');raw.files=[{name:'请求中新增文件.txt',size:10,type:'text/plain'}];raw.dispatchEvent(new DOMEvent('change'));const duringUploads=calls.filter(c=>c.url==='/api/materials/upload').length;writeGate.resolve();await drain();console.log(JSON.stringify({...observed(),duringUploads,text:form.text.value,files:raw.files.map(n=>n.name),attachments:JSON.parse(form.elements.namedItem('attachment_items').value),same:root.detailSession.form===form}));
""")
    body = assert_one_write(result, "/api/secretary/turns")["body"]
    assert body["text"] == "提交中的合成原话" and body["material_ids"] == [701]
    assert result["text"] == "下一份合成原话"
    assert result["duringUploads"] == 0 and len(result["uploads"]) == 1
    assert result["uploads"][0]["method"] == "POST"
    assert {item["material_id"] for item in result["attachments"]} == {701, 702}
    assert result["files"] == [] and result["same"] is True


@pytest.mark.parametrize("kind,expected_handler,endpoint", [("action", "submitForm", "/api/records/101/activities"), ("flow", "submitSecretaryFlow", "/api/secretary/turns"), ("arrangement", "submitArrangementDecision", "/api/secretary/plans/201/arrangement-decisions")])
def test_unmarked_legacy_form_keeps_original_listener_owner(kind, expected_handler, endpoint):
    result = run_event_scenario(f"""
const slot=document.querySelector('#edit-content');document.querySelector('#edit-dialog').open=true;
if({json.dumps(kind)}==='action')slot.innerHTML='<form id="activity-form" data-record-id="101"><textarea name="content">旧入口进展</textarea><p class="form-error"></p><button type="submit">保存</button></form>';
else if({json.dumps(kind)}==='flow'){{context.__dto=plan();slot.innerHTML=run('flowComposer(__dto)');set(slot.querySelector('form'),'text','旧入口原话');}}
else{{context.__dto=plan();slot.innerHTML=run("arrangementDecisionFormHTML(__dto,'update_progress')");set(slot.querySelector('form'),'progress_text','旧入口安排进展');}}
const form=slot.querySelector('form');failures.push({{status:422,message:'合成验证错误'}});submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    assert_one_write(result, endpoint)
    assert [entry["name"] for entry in result["handlers"]] == [expected_handler]
    listener = result["handlers"][0]["listener"]
    assert listener == {"action": "app.js", "flow": "secretary-flow.js", "arrangement": "arrangement-queue.js"}[kind]


def test_failed_processing_receipt_retains_saved_utterance_and_offers_retry_not_resave():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'supplement');input(form,'text','已保存但整理失败的合成原话');submit(form);await drain();turnStatus='failed';const poll=timers.find(t=>t.delay===1800);assert(poll,'queued turn must schedule its result poll');await poll.fn();await drain();const retry=root.querySelector('[data-detail-retry-turn],[data-flow-retry]');console.log(JSON.stringify({...observed(),receipt:root.querySelector('[data-detail-receipt]').textContent,retry:retry?.dataset.detailRetryTurn||retry?.dataset.flowRetry}));
""")
    assert_one_write(result, "/api/secretary/turns")
    assert result["retry"] == "801"
    assert "原话已保存" in result["receipt"] and "未成功" in result["receipt"]
    assert any(read["url"] == "/api/secretary/turns/801" for read in result["reads"])


def test_completion_words_in_activity_are_plain_content_with_no_completion_write():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'activity');input(form,'content','这个完成了，实际仍需客户验收');submit(form);await drain();console.log(JSON.stringify({...observed(),recordStatus:recordDTO.record.status}));
""")
    assert assert_one_write(result, "/api/records/101/activities")["body"]["content"] == "这个完成了，实际仍需客户验收"
    assert result["recordStatus"] == "following"


@pytest.mark.parametrize("projection", [{"pending_tasks": None}, {"pending_proposals": None}, {"pending_task_count": 2}, {"pending_proposal_count": 0}, {"snapshot": "different-snapshot"}, {"record_id": 999}, {"scope": "other_scope"}])
def test_incomplete_or_mismatched_completion_effects_never_permit_outcome_write(projection):
    result = run_event_scenario(f"""
const dto=action();Object.assign(dto.completion_effects,{json.dumps(projection)});const root=mount('action',dto),form=choose(root,'outcome');input(form,'result','影响不完整时保留的实际结果');forceSubmit(form);await drain();console.log(JSON.stringify({{...observed(),value:form.elements.namedItem('result').value,disabled:form.querySelector('[type="submit"]').disabled,preview:form.textContent}}));
""")
    assert result["writes"] == [] and result["disabled"] is True
    assert result["value"] == "影响不完整时保留的实际结果"
    assert "完整完成影响" in result["preview"]


def test_outcome_ledger_retry_same_scope_reuses_id_changed_result_gets_new_id():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'outcome');input(form,'result','相同的实际结果');failures.push({message:'未知结果甲'},{message:'未知结果乙'},{message:'未知结果丙'});submit(form);await drain();submit(form);await drain();input(form,'result','修改后的实际结果');submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    assert len(result["writes"]) == 3
    assert all(write["url"] == "/api/records/101/complete-outcome" for write in result["writes"])
    ids = [write["body"]["request_id"] for write in result["writes"]]
    assert ids[0] == ids[1] and ids[1] != ids[2]


def test_explicit_recheck_keeps_inputs_loads_new_baseline_and_clears_old_attestation():
    result = run_event_scenario("""
const complete={decision_mode:'external',agreement_status:'agreed',settlement_scope:'execution_time',blocking_reasons:['user_review_required'],can_apply:false,proposed_execution:{time_spec:{precision:'instant',date:'2026-10-09',at:NOW/1000+3*86400}},candidates:[{id:'candidate-a',time_spec:{precision:'instant',date:'2026-10-09',at:NOW/1000+3*86400}},{id:'candidate-b',time_spec:{precision:'instant',date:'2026-10-10',at:NOW/1000+4*86400}}],selected_candidate_id:'candidate-a'};
const root=mount('arrangement',plan(complete)),form=choose(root,'decision','confirm_arrangement');input(form,'candidate_id','candidate-b','change');input(form,'agreement_attested',true,'change');planDTO=plan({...complete,revision:5},{revision:5});failures.push({status:409,message:'合成版本变化'});submit(form);await drain();submit(form);await drain();const originalRequest=form.elements.namedItem('request_id').value;root.querySelector('[data-detail-recheck]').click();await drain();const fresh=root.detailSession.form;context.__form=fresh;console.log(JSON.stringify({...observed(),newNode:fresh!==form,originalRequest,snapshot:snapshot(fresh),revision:fresh.dataset.planRevision,attestation:fresh.elements.namedItem('agreement_attested').checked,customHidden:fresh.querySelector('[data-arrangement-custom-execution]').hidden}));
""")
    assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "confirm_arrangement")
    assert result["newNode"] is True and result["revision"] == "5"
    assert result["snapshot"]["baseline"]["plan_revision"] == 5
    assert result["snapshot"]["values"]["candidate_id"] == "candidate-b"
    assert result["originalRequest"] and result["snapshot"]["values"]["request_id"] == ""
    assert result["snapshot"]["values"]["request_text"] == ""
    assert result["attestation"] is False and result["customHidden"] is True


def test_edited_structure_and_text_date_conflict_is_visible_and_neither_draft_overwritten():
    result = run_event_scenario("""
const root=mount('arrangement'),text=choose(root,'supplement');input(text,'text','2026-10-09去客户现场');const structure=choose(root,'decision','start_reschedule');input(structure,'execution_date','2026-10-10');const originalText=choose(root,'supplement');const notice=root.querySelector('[data-detail-draft-notice]').textContent,restoredStructure=choose(root,'decision','start_reschedule');console.log(JSON.stringify({...observed(),notice,text:originalText.text.value,date:restoredStructure.elements.namedItem('execution_date').value,sameText:text===originalText,sameStructure:structure===restoredStructure}));
""")
    assert result["writes"] == []
    assert "日期不一致" in result["notice"] and "核对" in result["notice"]
    assert result["text"] == "2026-10-09去客户现场" and result["date"] == "2026-10-10"
    assert result["sameText"] is True and result["sameStructure"] is True


def test_remounted_dirty_arrangement_draft_keeps_old_revision_and_blocks_write_until_recheck():
    result = run_event_scenario("""
const root=mount('arrangement'),form=choose(root,'progress');input(form,'progress_text','重开时保留的旧版本草稿');const newer=mount('arrangement',plan({revision:5},{revision:5})),restored=newer.detailSession.form;submit(restored);await drain();context.__form=restored;console.log(JSON.stringify({...observed(),same:restored===form,snapshot:snapshot(restored),revision:restored.dataset.planRevision,latestRevision:newer.detailSession.vm.baseline.plan_revision}));
""")
    assert result["writes"] == [] and result["same"] is True
    assert result["revision"] == "3" and result["latestRevision"] == 5
    assert result["snapshot"]["values"]["progress_text"] == "重开时保留的旧版本草稿"
    assert result["snapshot"]["baseline"]["plan_revision"] == 3
    assert "核对" in result["snapshot"]["notice"]


def test_activity_success_snapshot_change_does_not_block_next_independent_append():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'activity');input(form,'content','第一份实际进展');writeGate=defer();submit(form);await started();recordDTO.completion_snapshot='completion-seen-2';recordDTO.completion_effects.snapshot='completion-seen-2';writeGate.resolve();await drain();input(form,'content','下一份独立实际进展');submit(form);await drain();console.log(JSON.stringify(observed()));
""")
    assert len(result["writes"]) == 2
    assert [write["body"]["content"] for write in result["writes"]] == ["第一份实际进展", "下一份独立实际进展"]
    assert all(write["url"] == "/api/records/101/activities" for write in result["writes"])


def test_candidate_button_then_primary_click_preserves_real_click_and_submit_ownership():
    result = run_event_scenario("""
const root=mount('arrangement',plan({candidates:[{id:'candidate-a',label:'方案甲'},{id:'candidate-b',label:'方案乙'}]}));const candidate=root.querySelector('[data-detail-candidate="candidate-b"]');assert(candidate,'actual candidate shortcut missing');candidate.click();await drain();const form=root.detailSession.form,before=writes().length,selected=form.elements.namedItem('candidate_id').value;form.querySelector('[type="submit"]').click();await drain();console.log(JSON.stringify({...observed(),before,selected}));
""")
    assert result["before"] == 0 and result["selected"] == "candidate-b"
    assert assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "select_candidate")["body"]["candidate_id"] == "candidate-b"
    assert_owned_chain(result, "submitArrangementDecision")


def test_date_settled_continue_set_time_submits_same_plan_one_operation_no_new_plan():
    result = run_event_scenario("""
const root=mount('arrangement',plan({settling_state:'settled',settlement_scope:'date_only',blocking_reasons:['missing_clock']})),form=root.detailSession.form;assert.equal(form.dataset.detailOperation,'continue_set_time');form.querySelector('[type="submit"]').click();await drain();console.log(JSON.stringify(observed()));
""")
    body = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "continue_set_time")["body"]
    assert set(body) == {"operation", "expected_revision", "request_id"}
    assert body["expected_revision"] == 3
    assert_owned_chain(result, "submitArrangementDecision")


def test_unknown_activity_requires_acknowledgement_and_native_retry_click_works_after_purpose_switch():
    result = run_event_scenario("""
const root=mount(),form=choose(root,'activity');input(form,'content','结果未知后明确决定再次追加');failures.push({message:'合成网络未知'});submit(form);await drain();const first=writes().length;choose(root,'outcome');const restored=choose(root,'activity');const blocked=restored.querySelector('[type="submit"]').disabled;submit(restored);await drain();const beforeAck=writes().length;const acknowledge=root.querySelector('[data-detail-append-again]');assert(acknowledge,'unknown append must remain actionable after switching purpose');acknowledge.click();await drain();const enabled=!restored.querySelector('[type="submit"]').disabled,warning=root.querySelector('[data-detail-draft-notice]').textContent;submit(restored);await drain();console.log(JSON.stringify({...observed(),first,beforeAck,blocked,enabled,same:restored===form,warning}));
""")
    assert result["first"] == result["beforeAck"] == 1
    assert result["blocked"] and result["enabled"] and result["same"]
    assert len(result["writes"]) == 2
    assert all(write["url"] == "/api/records/101/activities" for write in result["writes"])
    assert "可能重复" in result["warning"]


@pytest.mark.parametrize("kind,purpose,field", [("action", "activity", "content"), ("arrangement", "supplement", "text"), ("arrangement", "progress", "progress_text")])
def test_source_becomes_hidden_while_save_in_flight_and_finally_never_reenables_submit(kind, purpose, field):
    result = run_event_scenario(f"""
const root=mount({kind!r}),form=choose(root,{purpose!r});input(form,{field!r},'写入中保留的合成输入');writeGate=defer();submit(form);await started();if({kind!r}==='action')recordDTO=action({{record:{{hidden:1}}}});else planDTO=plan({{source_visible:false,source_hidden:true}});context.__ref=root.detailSession.subjectRef;await run('refreshDetailSubject(__ref)');const before=form.querySelector('[type="submit"]').disabled;writeGate.resolve();await drain();console.log(JSON.stringify({{...observed(),before,after:form.querySelector('[type="submit"]').disabled,visible:root.detailSession.vm.visible}}));
""")
    assert len(result["writes"]) == 1
    assert result["before"] and result["after"] and result["visible"] is False


@pytest.mark.parametrize("mode", ["self", "external"])
def test_owned_confirm_when_only_user_review_missing_uses_explicit_current_authority_once(mode):
    result = run_event_scenario(f"""
const dto=plan({{decision_mode:{mode!r},agreement_status:{'agreed' if mode == 'external' else 'not_required'!r},settlement_scope:'execution_time',proposed_execution:{{time_spec:{{precision:'instant',date:'2026-10-09',at:Date.parse('2026-10-09T10:30:00+08:00')/1000}},duration_minutes:45}},blocking_reasons:['user_review_required'],can_apply:false,application_authority:{{kind:'none'}},active_schedule:{{id:501,status:'pending',revision:4,remind_at:NOW/1000+86400}}}}),root=mount('arrangement',dto),form=root.detailSession.form;assert.equal(root.detailSession.purpose,'decision');assert.equal(root.detailSession.operation,'confirm_arrangement');if({mode!r}==='external')input(form,'agreement_attested',true,'change');submit(form);await drain();console.log(JSON.stringify({{...observed(),canApply:dto.arrangement.can_apply,oldAuthority:dto.arrangement.application_authority,oldTask:dto.arrangement.active_schedule}}));
""")
    body = assert_one_write(result, "/api/secretary/plans/201/arrangement-decisions", "confirm_arrangement")["body"]
    assert body["expected_revision"] == 3 and body["expected_task_revision"] == 4
    assert body["settlement_scope"] == "execution_time" and body["request_id"]
    assert body["proposed_execution"]["time_spec"] == {"precision": "instant", "date": "2026-10-09", "timezone": "Asia/Shanghai", "raw_text": "2026-10-09 10:30", "at": 1791513000}
    assert body["proposed_execution"]["duration_minutes"] == 45
    assert result["canApply"] is False and result["oldAuthority"] == {"kind": "none"}
    assert result["oldTask"]["status"] == "pending"
    if mode == "external":
        assert body["agreement_attestation"] == {"status": "reported", "scope": "execution_time", "evidence": "用户在页面明确转述对方已同意本次核对的时段"}
    else:
        assert "agreement_attestation" not in body
    assert_owned_chain(result, "submitArrangementDecision")


@pytest.mark.parametrize("gap", ["missing_clock", "missing_date", "missing_location", "waiting_for_agreement", "decision_mode_unknown", "invalid_duration"])
def test_owned_confirm_is_unavailable_when_another_necessary_gap_remains(gap):
    result = run_event_scenario(f"""
const dto=plan({{decision_mode:{'unknown' if gap == 'decision_mode_unknown' else 'external'!r},agreement_status:{'waiting' if gap == 'waiting_for_agreement' else 'agreed'!r},settlement_scope:'execution_time',proposed_execution:{{time_spec:{{precision:'instant',date:'2026-10-09',at:NOW/1000+3*86400}}}},blocking_reasons:['user_review_required',{gap!r}],can_apply:false}}),root=mount('arrangement',dto),chooser=root.querySelector('[data-detail-purpose-choice]'),options=chooser.querySelectorAll('option').map(n=>n.value),form=choose(root,'decision','confirm_arrangement');submit(form);await drain();const afterClick=writes().length;forceSubmit(form);await drain();console.log(JSON.stringify({{...observed(),options,afterClick,defaultPurpose:root.detailSession.vm.focus.purpose,disabled:form.querySelector('[type="submit"]').disabled}}));
""")
    assert result["defaultPurpose"] == "supplement"
    assert "decision:confirm_arrangement" not in result["options"]
    assert result["disabled"] and result["afterClick"] == 0 and result["writes"] == []


def test_owned_terms_patch_uses_original_baseline_and_distinct_time_objects_once():
    result = run_event_scenario("""
const original={executor_kind:'self',execution_at:Date.parse('2026-10-12T10:00:00+08:00')/1000,deadline_date:'2026-10-08',deadline_at:Date.parse('2026-10-08T18:00:00+08:00')/1000,check_date:'2026-10-07',check_at:Date.parse('2026-10-07T15:00:00+08:00')/1000,duration_minutes:30,executor_evidence:'合成原负责人依据',execution_evidence:'合成原执行依据',deadline_evidence:'合成原期限依据',check_evidence:'合成原检查依据',duration_evidence:'合成原用时依据'},root=mount('action',action({record:{terms_updated_at:17,action_terms:original},active_reminder:{id:501,status:'pending',remind_at:NOW/1000+8*86400,revision:4}})),form=choose(root,'terms');input(form,'executor_kind','team','change');input(form,'deadline_date','2026-10-09');input(form,'check_at','2026-10-07T16:00');input(form,'evidence','我核对后的独立资料');submit(form);await drain();console.log(JSON.stringify({...observed(),original,baseline:JSON.parse(form.dataset.termsBaseline),revision:form.dataset.revision,activeTask:recordDTO.active_reminder,receipt:root.querySelector('[data-detail-receipt]').textContent}));
""")
    assert len(result["writes"]) == 1
    write = result["writes"][0]
    assert write["method"] == "PATCH" and write["url"] == "/api/records/101/terms"
    body = write["body"]
    assert body["expected_updated_at"] == 17 and body["executor_kind"] == "team"
    assert body["execution_at"] == result["original"]["execution_at"]
    assert body["deadline_date"] == "2026-10-09" and body["deadline_at"] is None
    assert body["check_date"] is None and body["check_at"] == 1791360000
    assert body["duration_minutes"] == 30
    assert body["execution_evidence"] == "合成原执行依据" and body["duration_evidence"] == "合成原用时依据"
    for family in ("executor", "deadline", "check"):
        assert body[family + "_evidence"] == "用户核对 / 后补：我核对后的独立资料"
    assert result["baseline"] == result["original"] and result["revision"] == "17"
    assert result["activeTask"]["status"] == "pending" and result["activeTask"]["revision"] == 4
    assert "未建立或移动执行日程" in result["receipt"]
    assert_owned_chain(result, "submitForm")


def test_owned_terms_stale_cas_retains_old_fields_and_does_not_retry_with_latest_revision():
    result = run_event_scenario("""
const original={executor_kind:'self',deadline_date:'2026-10-08',check_date:'2026-10-07'},root=mount('action',action({record:{terms_updated_at:17,action_terms:original}})),form=choose(root,'terms');input(form,'check_date','2026-10-09');input(form,'evidence','旧版本独立检查依据');recordDTO=action({record:{terms_updated_at:18,action_terms:{...original,check_date:'2026-10-10'}}});failures.push({status:409,message:'行动资料版本已有变化'});submit(form);await drain();submit(form);forceSubmit(form);await drain();context.__form=form;console.log(JSON.stringify({...observed(),snapshot:snapshot(form),revision:form.dataset.revision,oldBaseline:JSON.parse(form.dataset.termsBaseline),currentTerms:root.detailSession.vm.entity.action_terms,latestRevision:root.detailSession.vm.baseline.terms_updated_at,disabled:form.querySelector('[type="submit"]').disabled}));
""")
    assert len(result["writes"]) == 1
    write = result["writes"][0]
    assert write["method"] == "PATCH" and write["url"] == "/api/records/101/terms"
    assert write["body"]["expected_updated_at"] == 17 and write["body"]["check_date"] == "2026-10-09"
    assert result["revision"] == "17" and result["latestRevision"] == 18
    assert result["snapshot"]["baseline"]["terms_updated_at"] == 17
    assert result["oldBaseline"]["check_date"] == "2026-10-07"
    assert result["currentTerms"]["check_date"] == "2026-10-10"
    assert result["snapshot"]["values"]["check_date"] == "2026-10-09"
    assert result["snapshot"]["values"]["evidence"] == "旧版本独立检查依据"
    assert result["disabled"] and "核对" in result["snapshot"]["notice"]
