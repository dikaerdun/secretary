"""Run real lifecycle JS against deterministic, isolated browser intent races."""
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which('node')
ROOT = Path(__file__).resolve().parents[1]

# The DOM and transport are fakes; lifecycle handlers and navigation are loaded
# directly from the production files so removing their guards fails these tests.
HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[1], scenario = process.argv[2];
const state = {authenticated:true,csrf:'original-session',view:'dashboard',
  dialogIntentGeneration:0,detailLoadGeneration:0,discussionNavigationGeneration:0};
const opened = [], notifications = [], calls = [];
const elements = new Map();
let clickHandler, gatePath = null, blockedRefresh = false, refreshCount = 0;
function gate() {
  let release, entered;
  return {promise:new Promise(resolve=>release=resolve),
    started:new Promise(resolve=>entered=resolve),
    enter(){entered();}, release(value){release(value);}};
}
const transportGate = gate(), refreshGate = gate();
const record = {record:{id:1,title:'synthetic source',content:'synthetic original',
  original_content:'synthetic original'},root_record_id:1,visibility:'active',revision:0,
  snapshot:'a'.repeat(64),record_count:1,pending_tasks:[],pending_task_count:0,
  pending_proposal_count:0,shared_tasks:0};
const empty = {items:[],total:0,page:1,pages:1};
function responseFor(url) {
  if(url==='/api/records/1/lifecycle')return record;
  if(url==='/api/priority-archives/restore')return {restored:true,key:'priority:synthetic'};
  return empty;
}
const context = {state,Number,Boolean,Promise,
  document:{addEventListener(name,handler){if(name==='click')clickHandler=handler;}},
  e:String,formatDate:String,dialogHeader:title=>title,
  api(url,options={}){
    calls.push({url,options});
    if(url===gatePath){gatePath=null;transportGate.enter();return transportGate.promise;}
    return Promise.resolve(responseFor(url));
  },
  openDialog(kind,html){state.dialogIntentGeneration++;opened.push({kind,html});},
  closeDialog(){state.dialogIntentGeneration++;},
  notify(message,error=false){notifications.push({message,error});},
  async mutateButton(button,action){button.disabled=true;try{await action();}finally{button.disabled=false;}},
  async loadView(){refreshCount++;if(blockedRefresh){blockedRefresh=false;refreshGate.enter();await refreshGate.promise;}},
  $(key){if(!elements.has(key))elements.set(key,{value:'',textContent:'',setAttribute(){},removeAttribute(){}});return elements.get(key);},
  $$:()=>[],labels:{dashboard:'dashboard',records:'records'},
  invalidateDiscussionIntent(){state.discussionNavigationGeneration++;},
  stopMaterialPoll(){},stopVisitPoll(){},stopCapturePoll(){},stopDiscussionPoll(){},stopCaptureDetailPoll(){},
  rememberMaterial(){},rememberVisit(){}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root,'secretary/static/record-lifecycle.js'),'utf8'),context);
const app = fs.readFileSync(path.join(root,'secretary/static/app.js'),'utf8');
const start = app.indexOf('async function navigate('), end = app.indexOf('function dialogHeader(',start);
assert(start>=0&&end>start,'production navigation function must be present');
vm.runInContext(app.slice(start,end),context);

function relogin(){state.authenticated=false;state.csrf='';state.authenticated=true;state.csrf='new-session';}
function restoreClick(){
  const button={disabled:false,dataset:{lifecycleRestoreTip:'priority:synthetic',lifecycleTipRevision:'1'},hasAttribute:()=>false};
  return clickHandler({target:{closest:()=>button}});
}
function assertArchiveOpened(){
  assert.equal(opened.length,1);
  assert.equal(opened[0].kind,'edit');
  assert(opened[0].html.includes('data-lifecycle-tab'),'the retained-item dialog must open');
}
async function run(){
  if(scenario==='record_get_ok'){
    await context.openRecordLifecycle(1);
    assert.equal(opened.length,1);
    assert(opened[0].html.includes('data-lifecycle-action="archive"'));
    assert(opened[0].html.includes('synthetic original'));
    return;
  }
  if(scenario==='archive_get_ok'){
    await context.openLifecycleArchive('archived');assertArchiveOpened();return;
  }
  if(scenario.startsWith('record_get_')||scenario==='archive_get_navigation'){
    const archive=scenario==='archive_get_navigation';
    gatePath=archive?'/api/record-lifecycle?visibility=archived&page=1&page_size=20':'/api/records/1/lifecycle';
    const opening=archive?context.openLifecycleArchive('archived'):context.openRecordLifecycle(1);
    await transportGate.started;
    if(scenario.endsWith('navigation'))await context.navigate('records');
    else if(scenario.endsWith('navigation_same_view'))await context.navigate('dashboard');
    else if(scenario.endsWith('relogin'))relogin();
    else if(scenario.endsWith('detail_changed'))state.detailLoadGeneration++;
    else assert.fail('unknown GET race');
    transportGate.release(archive?empty:record);await opening;
    assert.equal(opened.length,0,'a stale GET must not open a dialog');
    assert.equal(notifications.length,0,'old reads must remain quiet');
    return;
  }
  // Start in a real archive dialog, establishing the handler's page context.
  await context.openLifecycleArchive('archived');opened.length=0;calls.length=0;
  if(scenario==='tip_restore_ok'){
    await restoreClick();assertArchiveOpened();
    assert.equal(refreshCount,1);assert.equal(notifications.length,1);
    assert(calls.some(call=>call.url==='/api/priority-archives/restore'&&call.options.method==='POST'));
    return;
  }
  if(scenario==='tip_restore_refresh_new_input'){
    blockedRefresh=true;const restoring=restoreClick();await refreshGate.started;
    context.openDialog('edit','new unsent input');refreshGate.release();await restoring;
  }else{
    gatePath='/api/priority-archives/restore';const restoring=restoreClick();await transportGate.started;
    if(scenario==='tip_restore_new_input')context.openDialog('edit','new unsent input');
    else if(scenario==='tip_restore_relogin')relogin();
    else assert.fail('unknown restore race');
    transportGate.release(responseFor('/api/priority-archives/restore'));await restoring;
  }
  if(scenario==='tip_restore_relogin'){
    assert.equal(opened.length,0,'a previous session must not reopen the archive');
    assert.equal(notifications.length,0,'a previous session must not publish a receipt');
  }else{
    assert.equal(opened.length,1);assert.equal(opened[0].html,'new unsent input',
      'restore completion must preserve the current input window');
    assert.equal(notifications.length,1,'the original operation may give a same-session receipt');
  }
}
run().then(()=>process.stdout.write(JSON.stringify({passed:scenario}))).catch(error=>{
  process.stderr.write(error.stack||String(error));process.exitCode=1;
});
'''


@pytest.mark.skipif(NODE is None, reason='Node.js is required for the browser intent regression harness')
@pytest.mark.parametrize('scenario', [
    'record_get_navigation', 'record_get_navigation_same_view', 'archive_get_navigation', 'record_get_detail_changed',
    'record_get_relogin', 'tip_restore_new_input', 'tip_restore_relogin',
    'tip_restore_refresh_new_input', 'record_get_ok', 'archive_get_ok', 'tip_restore_ok',
])
def test_lifecycle_browser_intents(scenario):
    result = subprocess.run([NODE, '-e', HARNESS, str(ROOT), scenario], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"passed":' in result.stdout and scenario in result.stdout
