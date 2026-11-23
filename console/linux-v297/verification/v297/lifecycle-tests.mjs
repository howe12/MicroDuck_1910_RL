import {audioLevelBars} from '../../static/audio-level.js';
import {busResponseProblem,modeSwitchError} from '../../static/messages.js';
import {connectionNotice,connectionWorkspaceState} from '../../static/startup-status.js';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
import * as positions from '../../static/numbering-position-stream.js';

const ROOT=new URL('../../',import.meta.url),read=n=>fs.readFileSync(new URL(n,ROOT),'utf8');
const source=read('static/four-pages.js'),app=read('static/app.js');
const sha=n=>crypto.createHash('sha256').update(fs.readFileSync(new URL(n,ROOT))).digest('hex');
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
const clone=v=>structuredClone(v),cases=[];
class Node {
  constructor(){this._text='';this._title='';this._disabled=false;this.attrs={};this.children=[];this.dataset={};this.writes=0;this.value='draft';this.scrollTop=37;this.hidden=false;this.style={setProperty(){}};this.active=false;
    this.classList={contains:n=>n==='active'&&this.active,toggle:(n,on)=>{this.writes++;if(n==='active')this.active=on;}};}
  get textContent(){return this._text;}set textContent(v){this._text=String(v);this.writes++;}
  get title(){return this._title;}set title(v){this._title=v;this.writes++;}
  get disabled(){return this._disabled;}set disabled(v){this._disabled=v;this.writes++;}
  getAttribute(k){return this.attrs[k]??null;}setAttribute(k,v){this.attrs[k]=String(v);this.writes++;}
  querySelector(s){return s==='small'?this.small:s==='.task-lock'?this.marker:new Node();}
  append(...children){this.children.push(...children);}replaceChildren(...children){this.writes++;this.children=children;}
}
function events(){const map=new Map();return {map,on(n,fn){const list=map.get(n)||[];list.push(fn);map.set(n,list);},async fire(n,e={}){for(const fn of map.get(n)||[])await fn(e);await flush();}};}
function healthy(){return {available:true,active:true,owned:true,mode:'numbering',pending:false,error:'',session_id:'owned-test',calibration_valid:false,
 scan:{state:'completed',generation:1,completed:true,running:false,requires_rescan:false,scanned:250,total:250,found:2},
 devices:[{id:10,kind:'servo',register_id:10,identity_verified:true,position_raw:1900},{id:20,kind:'servo',register_id:20,identity_verified:true,position_raw:2100}],
 position_stream:{target:'fixture',session_id:'owned-test',scan_generation:1,generation:1,revision:0,state:'live'},
 allowed_pages:{numbering:{allowed:true,can_start:true},hardware:{allowed:false},calibration:{allowed:false},robot:{allowed:false}}};}
function fourHarness(){
 const c={config:0,get:0,post:0,paint:0,posStart:0,posStop:0,imuStart:0,imuStop:0,intervalStarts:0,reload:0};
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
 const nav=new Map(['hardware','numbering','calibration','robot'].map(page=>{const b=new Node();b.dataset.consolePage=page;b.small=new Node();b.marker=new Node();return [page,b];}));
 const win=events(),doc=events(),timers=new Map();let now=100,nextTimer=1;
 const ctx={audioLevelBars,modeSwitchError,...positions,console,AbortController,AbortSignal,Headers,performance:{now:()=>now},setTimeout,clearTimeout,
 setInterval(fn){const id=nextTimer++;timers.set(id,fn);c.intervalStarts++;return id;},clearInterval:id=>timers.delete(id),requestAnimationFrame:()=>1,
 window:{addEventListener:(n,fn)=>win.on(n,fn),cancelAnimationFrame(){},location:{reload(){c.reload++;}}},
 document:{hidden:false,getElementById:node,querySelector:s=>nav.get(s.match(/data-console-page="([^"]+)"/)?.[1])||null,querySelectorAll:()=>[...nav.values()],addEventListener:(n,fn)=>doc.on(n,fn)},
 localStorage:{getItem(){return null;},setItem(){}},getControlToken:()=> 'test-token',refreshControlSession:async()=>{c.config++;return {};},
 consumeMaintenanceEvents:async()=>{},consumeImuEvents:async()=>{},showMessageDialog:async(message,options)=>{h.dialogs.push({message,options});},
 fetch:async()=>{c.get++;return {ok:true,json:async()=>clone(h.getReply??h.p.state)};},
 controlFetch:async(_url,options)=>{c.post++;h.posted.push(JSON.parse(options.body));if(h.postGate)await h.postGate;return {ok:true,status:200,json:async()=>clone(h.postReply??{accepted:true,state:h.p.state})};}};
 vm.createContext(ctx);vm.runInContext(source.replace(/^import .*?;\n/gm,'').replace(/export /g,'')+'\nglobalThis.P=FourPageConsole;',ctx);
 const P=ctx.P,realRefresh=P.prototype.refresh,realPaintModes=P.prototype.paintWorkspaceModes;
 P.prototype.mount=function(){this.pages={};this.empty=node('empty');this.rowTargets=new Map();};P.prototype.refresh=function(){c.get++;return Promise.resolve();};
 const p=new P({service_enabled:true,connection_ui:true,ssh_target:'fixture',ids:[10,20],labels:['right','left']},{servicePanel:{posting:false,centerConfirmation:null,client:'client'},controls:{suspend(){}},notify(){}});
 P.prototype.refresh=realRefresh;p.state=healthy();p.receivedAt=now;p.permissions=ctx.normalizePermissions(p.state);p.page='numbering';p.options={};p.snapshot={connection:{active:true,connected:true,target:'fixture'},service_control:{},service_operation:true};p.workspaceSnapshotAt=now;p.connectionTarget='fixture';p.selectedCenterId=20;p.currentServoId=20;p.rowTargets.set(20,10);p.positionContextKey=positions.numberingPositionKey(positions.numberingPositionContext(p.state));p.paint=function(){c.paint++;realPaintModes.call(p);p.paintScanFeedback();};p.paintSpeakerLevel=()=>{};p.queuePositionPaint=()=>{};p.sizeNumberingCards=()=>{};p.workspaceModeFeedback=message=>{h.feedback=message;};
 const stopPos=P.prototype.stopPositionStream,stopImu=P.prototype.stopImuStream;p.stopPositionStream=function(...args){c.posStop++;return stopPos.apply(p,args);};p.stopImuStream=function(...args){c.imuStop++;return stopImu.apply(p,args);};p.startPositionStream=()=>{c.posStart++;};p.startImuStream=()=>{c.imuStart++;};
 const h={p,P,ctx,c,node,nav,win,doc,timers,dialogs:[],posted:[],advance:n=>{now+=n;},navWrites:()=>[...nav.values()].reduce((sum,b)=>sum+b.writes+b.small.writes+b.marker.writes,0),resetNavWrites:()=>{for(const b of nav.values())b.writes=b.small.writes=b.marker.writes=0;}};return h;
}
function appHarness(){
 const c={config:0,open:0,close:0,camera:0},win=events(),doc=events();
 const observer={closed:false,socket:{readyState:1},close(){this.closed=true;this.socket.readyState=3;c.close++;}};
 const ctx={window:{addEventListener:(n,fn)=>win.on(n,fn)},document:{hidden:false,addEventListener:(n,fn)=>doc.on(n,fn)},h:{observer,c},refreshControlSession:async()=>{c.config++;},console};vm.createContext(ctx);
 const begin=app.indexOf('async function resumePageObserver(){'),end=app.indexOf('function valueCell',begin),unload=app.split('\n').find(line=>line.startsWith("window.addEventListener('beforeunload'"));
 assert.ok(begin>=0&&end>begin&&unload);
 vm.runInContext("let cfg={raw_telemetry:true,control_token:'test',control_session_id:'backend'};let rawTelemetry=h.observer,replay=null,observerResume=null,telemetryEpoch=0;const wifiPanel={stopJobPoll(){}};const camera={disconnect(){h.c.camera++;}};function openRawTelemetry(){h.c.open++;rawTelemetry={closed:false,socket:{readyState:1}};}\n"+app.slice(begin,end)+unload+"\nglobalThis.appState=()=>({rawTelemetry,observerResume});",ctx);
 return {ctx,c,win,doc,observer,state:()=>ctx.appState()};
}
async function check(name,fn){await fn();cases.push(name);}
await check('ordinary focus and hide/show keep actual constructor state, drafts, DOM and existing position reader without requests or card writes',async()=>{
 const h=fourHarness(),p=h.p;p.paintWorkspaceModes();h.resetNavWrites();const initial={get:h.c.get,paint:h.c.paint,config:h.c.config,post:h.c.post},state=p.state,draft=p.rowTargets,table=h.node('numbering-device-rows'),input=h.node('service-position');
 p.positionAbort=new AbortController();p.positionStreamExpected=true;const reader=p.positionAbort;
 await h.win.fire('focus');h.ctx.document.hidden=true;await h.doc.fire('visibilitychange');p.syncPositionStream();h.ctx.document.hidden=false;await h.doc.fire('visibilitychange');await h.win.fire('focus');
 assert.equal(p.state,state);assert.equal(p.rowTargets,draft);assert.equal(p.rowTargets.get(20),10);assert.equal(p.selectedCenterId,20);assert.equal(p.positionAbort,reader);assert.equal(table.scrollTop,37);assert.equal(input.value,'draft');assert.equal(h.navWrites(),0);assert.equal(h.c.posStart,0);assert.equal(h.c.posStop,0);assert.deepEqual({get:h.c.get,paint:h.c.paint,config:h.c.config,post:h.c.post},initial);assert.equal(h.c.reload,0);assert.equal(h.timers.size,1);
});
await check('existing normal maintenance poll continues after focus without an additional loop or configuration refresh',async()=>{
 const h=fourHarness(),before=h.c.get;await h.win.fire('focus');h.advance(600);await [...h.timers.values()][0]();await flush();assert.equal(h.c.get,before+1);assert.equal(h.c.config,0);assert.equal(h.c.post,0);assert.equal(h.timers.size,1);assert.equal(h.c.intervalStarts,1);
});
await check('hidden legitimate swap and IMU observations retain their actual reader eligibility while new hidden writes remain guarded',async()=>{
 const n=fourHarness();n.p.busy=true;n.p.swapPending={id:20};n.p.positionAbort=new AbortController();n.p.positionStreamExpected=true;const pos=n.p.positionAbort;n.ctx.document.hidden=true;n.p.syncPositionStream();assert.equal(n.p.shouldStreamPositions(),true);assert.equal(n.p.positionAbort,pos);assert.equal(n.c.posStop,0);assert.equal(n.c.posStart,0);await n.p.swapNumberingIds({old_id:20,new_id:10,swap:true});assert.equal(n.c.post,0);
 const i=fourHarness();i.p.page='hardware';i.p.state={...healthy(),mode:'hardware-test',imu_running:true};i.p.imuAbort=new AbortController();i.p.imuStreamExpected=true;const imu=i.p.imuAbort;i.ctx.document.hidden=true;i.p.syncImuStream();assert.equal(i.p.shouldStreamImu(),true);assert.equal(i.p.imuAbort,imu);assert.equal(i.c.imuStart,0);assert.equal(i.c.imuStop,0);
});
await check('real failed position and IMU readers stay latched through ordinary focus and visibility changes without reconnects',async()=>{
 const h=fourHarness();h.p.positionAbort=null;h.p.positionStreamBlocked=true;h.p.positionStreamError='real failure';h.p.syncPositionStream();h.ctx.document.hidden=true;await h.doc.fire('visibilitychange');h.ctx.document.hidden=false;await h.win.fire('focus');h.p.syncPositionStream();assert.equal(h.c.posStart,0);assert.equal(h.p.positionStreamBlocked,true);
 h.p.page='hardware';h.p.state.mode='hardware-test';h.p.state.imu_running=true;h.p.imuStreamBlocked=true;h.p.imuAbort=null;h.p.syncImuStream();await h.win.fire('focus');assert.equal(h.c.imuStart,0);assert.equal(h.p.imuStreamBlocked,true);assert.equal(h.c.config,0);
});
await check('actual pagehide keeps displayed state and drafts but invalidates freshness, and real pageshow restores one read loop without reentering or scanning',async()=>{
 const h=fourHarness(),p=h.p,state=p.state,drafts=p.rowTargets;p.positionAbort=new AbortController();p.positionStreamExpected=true;p.localActionFeedback={'numbering-feedback':{action:'write',phase:'completed',message:'saved'}};
 await h.win.fire('pagehide');assert.equal(p.state,state);assert.equal(p.rowTargets,drafts);assert.equal(p.selectedCenterId,20);assert.equal(p.receivedAt,-Infinity);assert.equal(p.stopped,true);assert.equal(h.node('service-position').value,'draft');assert.equal(h.timers.size,0);assert.deepEqual(h.posted.map(x=>x.action),['end']);const before=h.c.get;
 await h.win.fire('pageshow',{persisted:true});assert.equal(h.c.get,before+1);assert.equal(h.c.config,1);assert.equal(h.timers.size,1);assert.equal(p.lifecycleResuming,false);assert.equal(p.stopped,false);assert.equal(p.selectedCenterId,20);assert.equal(p.rowTargets.get(20),10);assert.deepEqual(h.posted.map(x=>x.action),['end']);
});
await check('a pageshow on an already running page performs no maintenance refresh or view reset',async()=>{
 const h=fourHarness(),before=h.c.get,state=h.p.state;await h.win.fire('pageshow',{persisted:false});await h.p.resumePageObservations();assert.equal(h.c.get,before);assert.equal(h.c.config,0);assert.equal(h.p.state,state);assert.equal(h.p.lifecycleResuming,undefined);assert.equal(h.c.paint,0);
});
await check('actual APP registrations do not refresh config or recreate a healthy observer on ordinary focus visibility or initial pageshow',async()=>{
 const h=appHarness();await h.win.fire('focus');h.ctx.document.hidden=true;await h.doc.fire('visibilitychange');h.ctx.document.hidden=false;await h.doc.fire('visibilitychange');await h.win.fire('focus');await h.win.fire('pageshow',{persisted:false});await h.win.fire('pageshow',{persisted:true});assert.equal(h.c.config,0);assert.equal(h.c.open,0);assert.equal(h.c.close,0);assert.equal(h.state().rawTelemetry,h.observer);
});
await check('actual APP restores only its own unloaded cached-page observer once, and never retries a genuinely failed observer',async()=>{
 const h=appHarness();h.observer.socket.readyState=3;await h.win.fire('focus');await h.win.fire('pageshow',{persisted:true});assert.equal(h.c.config,0);assert.equal(h.c.open,0);
 await h.win.fire('beforeunload');assert.equal(h.c.close,1);await Promise.all([h.win.fire('pageshow',{persisted:true}),h.win.fire('pageshow',{persisted:true})]);assert.equal(h.c.config,1);assert.equal(h.c.open,1);assert.equal(h.state().rawTelemetry.closed,false);await h.win.fire('focus');assert.equal(h.c.open,1);
});
await check('a genuine cached-page restore initially hidden completes only its own paused observation when visible, without touching healthy or failed readers',async()=>{
 const h=fourHarness();await h.win.fire('pagehide');const before=h.c.get;h.ctx.document.hidden=true;await h.win.fire('pageshow',{persisted:true});assert.equal(h.p.stopped,true);assert.equal(h.c.config,0);h.ctx.document.hidden=false;await h.doc.fire('visibilitychange');assert.equal(h.p.stopped,false);assert.equal(h.c.config,1);assert.equal(h.c.get,before+1);assert.deepEqual(h.posted.map(x=>x.action),['end']);
 const a=appHarness();await a.win.fire('beforeunload');a.ctx.document.hidden=true;await a.win.fire('pageshow',{persisted:true});assert.equal(a.c.config,0);a.ctx.document.hidden=false;await a.doc.fire('visibilitychange');assert.equal(a.c.config,1);assert.equal(a.c.open,1);await a.doc.fire('visibilitychange');assert.equal(a.c.config,1);assert.equal(a.c.open,1);
});
await check('actual scan click ACK progress and completion leave all four mode-card DOM attributes and copy unchanged',async()=>{
 const h=fourHarness(),p=h.p;const running={...healthy(),scan:{...healthy().scan,generation:2,state:'running',completed:false,running:true,scanned:0}};h.postReply={accepted:true,state:running};h.getReply=running;p.paintWorkspaceModes();h.resetNavWrites();const cards=[...h.nav.values()];
 let finish;h.postGate=new Promise(resolve=>{finish=resolve;});const pending=h.node('numbering-scan').onclick();await flush();h.advance(3000);p.paintWorkspaceModes();assert.equal(h.navWrites(),0);finish();await pending;assert.deepEqual(h.posted.map(x=>x.action),['scan']);assert.equal(h.navWrites(),0);assert.equal(h.c.post,1);
 p.accept({...running,scan:{...running.scan,scanned:120}});assert.equal(h.navWrites(),0);assert.equal([...h.nav.values()].every((b,i)=>b===cards[i]),true);await h.nav.get('calibration').onclick();assert.match(h.feedback,/先停止扫描/);assert.equal(h.c.post,1);assert.equal(h.navWrites(),0);
 p.accept({...running,scan:{...running.scan,state:'completed',completed:true,running:false,scanned:250}});assert.equal(h.navWrites(),0);assert.match(h.node('numbering-feedback').textContent,/扫描已完成/);assert.equal(h.c.post,1);
});
await check('real accepted mode transitions update navigation while local center and swap locks remain enforced only at action dispatch',async()=>{
 const h=fourHarness(),p=h.p;p.paintWorkspaceModes();h.resetNavWrites();p.busy=true;p.centerPending={id:20};p.patchCenterModeLocks();assert.equal(h.navWrites(),0);await h.nav.get('hardware').onclick();assert.equal(h.c.post,0);p.centerPending=null;p.busy=false;
 p.entryPending=true;p.paintWorkspaceModes();assert.ok(h.navWrites()>0);assert.equal([...h.nav.values()].every(b=>b.disabled),true);p.entryPending=false;p.state={...healthy(),mode:'hardware-test'};p.page='hardware';p.paintWorkspaceModes();assert.equal(h.nav.get('hardware').active,true);assert.equal(h.nav.get('numbering').active,false);assert.equal([...h.nav.values()].every(b=>!b.disabled),true);
});

await check('real switch failure unlocks all mode cards and displays one readable internal dialog without retrying or replacing the selected servo',async()=>{
 const h=fourHarness(),p=h.p;const context=p.workspaceModeContext(),sourceJob=JSON.stringify(p.snapshot.service_control?.job);
 p.actualWorkspaceMode=()=>null;p.busy=true;p.entryPending=true;p.workspaceModeErrorGeneration=1;
 p.workspaceModeAwaiting={target:'commissioning',context,sourceJob,sourceSnapshotEpoch:0,started:0,errorGeneration:1};
 p.workspaceModeLastCommand={target:'commissioning',context,sourceJob,errorGeneration:1};
 const error='服务操作失败：'+('old service journal line\n'.repeat(80))+'Feetech ID 200 response timeout';
 p.snapshot.service_control={job:{action:'switch-mode',command:{target:'commissioning'},status:'error',error,startup:{steps:{2:{status:'waiting',detail:'bus transaction failed: Feetech ID 200 response timeout'}}}}};
 p.workspaceModeFeedback=h.P.prototype.workspaceModeFeedback;
 p.paintWorkspaceModes();p.paintWorkspaceModes();
 assert.equal(p.workspaceModeAwaiting,null);assert.equal(p.busy,false);assert.equal(p.entryPending,false);assert.equal([...h.nav.values()].every(b=>!b.disabled),true);
 assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/IMU（ID 200）没有响应/);assert.ok(h.dialogs[0].message.length<300);
 assert.equal(p.selectedCenterId,20);assert.equal(p.rowTargets.get(20),10);assert.equal(h.c.post,0);assert.equal(h.c.reload,0);
 p.workspaceModeFeedback('请先等待舵机扫描完成','error','unrelated-error');assert.equal(h.dialogs.at(-1).message,'请先等待舵机扫描完成');
});


function requestFixture(actual='commissioning'){
 const h=fourHarness(),p=h.p;h.opened=[];
 p.state={...healthy(),active:false,owned:false,mode:'idle',calibration_valid:false,numbering_verified:true,initial_page:'calibration'};
 p.actualWorkspaceMode=()=>actual;p.workspaceModeReason=()=>'';p.workspaceModeFeedback=h.P.prototype.workspaceModeFeedback;
 p.page='calibration';p.snapshot.service_operation=false;p.paint=()=>{h.c.paint++;};
 p.open=page=>{h.opened.push(page);p.page=page;return true;};p.refresh=async()=>{h.c.get++;};return h;
}
await check('uncalibrated motion click already in calibration displays one Chinese internal error after the real unchanged API reply instead of clearing it',async()=>{
 const h=requestFixture();h.postReply={accepted:true,target:'commissioning',requested_target:'motion',unchanged:true,redirect_reason:'尚未完成有效标定，未进入运动模式。\n待标定关节：ID 10、ID 32。\nIMU 安装标定尚未确认。'};
 assert.equal(await h.p.requestWorkspaceMode('motion'),false);assert.deepEqual(h.posted.map(x=>x.action),['to-motion']);
 assert.deepEqual(h.opened,['calibration']);assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/未进入运动模式/);assert.match(h.dialogs[0].message,/ID 32/);
 assert.equal(h.p.workspaceModeAwaiting,null);assert.equal(h.p.busy,false);assert.equal(h.p.entryPending,false);assert.equal(h.p.selectedCenterId,20);assert.equal(h.p.rowTargets.get(20),10);assert.equal(h.c.reload,0);
 h.p.paintWorkspaceModes();h.p.paintWorkspaceModes();assert.equal(h.dialogs.length,1);
});
await check('a fallback that must start calibration explains the original motion rejection without claiming success or submitting a second operation',async()=>{
 const h=requestFixture(null);h.postReply={accepted:true,target:'commissioning',requested_target:'motion',redirect_reason:'尚未完成有效标定，未进入运动模式。请先保存标定。'};
 assert.equal(await h.p.requestWorkspaceMode('motion'),false);assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/未进入运动模式/);
 assert.equal(h.p.workspaceModeAwaiting.target,'commissioning');assert.deepEqual(h.posted.map(x=>x.action),['to-motion']);assert.equal(h.c.reload,0);
});
await check('a pre-V294 backend target-only redirect still produces a readable rejection',async()=>{
 const h=requestFixture();h.postReply={accepted:true,target:'commissioning',unchanged:true};assert.equal(await h.p.requestWorkspaceMode('motion'),false);
 assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/未进入运动模式/);assert.match(h.dialogs[0].message,/关节与 IMU/);
});
await check('already selected numbering explains why motion was not entered without a scan, native request or page reload',async()=>{
 const h=requestFixture('numbering');h.p.page='numbering';h.p.state={...healthy(),initial_page:'numbering',numbering_verified:false};
 assert.equal(await h.p.requestWorkspaceMode('motion'),false);assert.deepEqual(h.opened,['numbering']);assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/尚未确认舵机编号/);assert.equal(h.c.post,0);assert.equal(h.c.reload,0);
});
await check('entering numbering as the prerequisite keeps one explicit entry and one rejection of the original motion click',async()=>{
 const h=requestFixture(null);h.p.state.initial_page='numbering';h.p.state.numbering_verified=false;let entries=0;h.p.requestPage=async page=>{assert.equal(page,'numbering');entries++;return true;};
 assert.equal(await h.p.requestWorkspaceMode('motion'),false);assert.equal(entries,1);assert.equal(h.dialogs.length,1);assert.match(h.dialogs[0].message,/未进入运动模式/);assert.equal(h.c.post,0);
});
await check('valid new motion entry and opening an already running motion page remain successful without spurious calibration errors',async()=>{
 const h=requestFixture(null);h.p.state.calibration_valid=true;h.postReply={accepted:true,target:'motion',requested_target:'motion',redirect_reason:''};
 assert.equal(await h.p.requestWorkspaceMode('motion'),true);assert.equal(h.dialogs.length,0);assert.equal(h.p.workspaceModeAwaiting.target,'motion');assert.deepEqual(h.posted.map(x=>x.action),['to-motion']);
 const running=requestFixture('motion');assert.equal(await running.p.requestWorkspaceMode('motion'),true);assert.deepEqual(running.opened,['robot']);assert.equal(running.dialogs.length,0);assert.equal(running.c.post,0);
});


await check('V297 completed management with explicit IMU wait unlocks real menu dispatch into both bench modes without waiting for hardware readiness',async()=>{
 for(const [target,mode] of [['numbering','numbering'],['hardware','hardware-test']]){
  const h=fourHarness(),p=h.p;p.page='calibration';p.state={available:true,active:false,mode:'idle',pending:false,error:'',allowed_pages:{}};
  p.actualWorkspaceMode=()=>null;p.snapshot.service_operation=false;
  p.snapshot.service_control={busy:false,stopping:false,job:{action:'switch-mode',command:{target:'commissioning'},status:'completed',result:{mode:'commissioning',startup_pending:true},startup:{steps:{'1':{status:'success',detail:'started'},'2':{status:'waiting',detail:'bus transaction failed: Feetech ID 200 response timeout'}}}}};
  p.workspaceModeAwaiting={target:'commissioning',context:p.workspaceModeContext(),sourceJob:'null',sourceSnapshotEpoch:0,started:0};p.busy=true;p.entryPending=true;
  p.paintWorkspaceModes();assert.equal(p.workspaceModeAwaiting.commandCompleted,true);assert.equal(p.busy,false);assert.equal(p.entryPending,false);
  assert.equal([...h.nav.values()].every(b=>!b.disabled),true);assert.equal(p.workspaceModeReason(target),'');
  const own={...healthy(),mode,session_id:'owned-bench',scan:{state:'idle',running:false}};
  h.ctx.controlFetch=async(_url,options)=>{h.c.post++;const q=JSON.parse(options.body);h.posted.push(q);
   return {ok:true,status:200,json:async()=>q.action==='preflight'?{accepted:true,can_enter:true,requires_stop:true,entry_target:'fixture'}:{accepted:true,state:own}};};
  h.getReply=own;const opened=[];p.open=(page)=>{opened.push(page);p.page=page;return true;};
  assert.equal(await p.requestWorkspaceMode(target),true);assert.deepEqual(h.posted.map(q=>q.action),['preflight','enter']);
  assert.equal(h.posted[1].stop_confirmed,true);assert.equal(p.state.owned,true);assert.deepEqual(opened,[target]);
  assert.equal(h.c.reload,0);assert.equal(h.dialogs.length,0);assert.equal(p.workspaceModeAwaiting,null);assert.equal(p.busy,false);
 }
});
await check('V297 no ready feedback does not bypass an actually running service action or enable motion inputs',async()=>{
 const h=fourHarness(),p=h.p;p.state={available:true,active:false,pending:false,mode:'idle'};p.snapshot.service_operation=true;
 p.snapshot.service_control={busy:true,job:{action:'switch-mode',command:{target:'commissioning'},status:'running'}};
 p.paintWorkspaceModes();assert.equal([...h.nav.values()].every(b=>b.disabled),true);
 await p.requestWorkspaceMode('hardware');assert.equal(h.c.post,0);assert.match(h.feedback,/服务操作/);
 assert.equal(p.snapshot.service_sample_fresh,undefined);assert.equal(h.c.reload,0);
});


await check('V297 missing IMU, one servo and several servos retain Chinese identifiers and exact original startup errors',async()=>{
 for(const [detail,label] of [['bus transaction failed: Feetech ID 200 response timeout','IMU（ID 200）'],['bus transaction failed: Feetech ID 20 response timeout','舵机（ID 20）'],['bus transaction failed: Feetech Sync Read timeout; missing IDs [10, 24]','舵机（ID 10）、舵机（ID 24）']]){
  const problem=busResponseProblem(detail);assert.ok(problem);assert.ok(problem.message.includes(label),problem.message);
  const formatted=modeSwitchError(detail,{steps:{'2':{status:'waiting',detail}}});assert.ok(formatted.includes(label));assert.ok(formatted.includes(detail));
  const startup={steps:{'1':{status:'success',detail:'started'},'2':{status:'waiting',detail}}};
  const snapshot={mode:'service',connection:{active:true,connected:true,target:'fixture'},channels:{startup:{status:'live'}},startup,service_sample_fresh:false,service_control:{job:{action:'switch-mode',command:{target:'commissioning'},status:'completed',startup,result:{startup_pending:true}}}};
  const workspace={page:'calibration',cfg:{control_session_id:'backend'},sessionEpoch:1,state:{},options:{},workspaceModeAwaiting:{target:'commissioning',commandCompleted:true,context:{}},workspaceModeContextMatches:()=>true};
  const notice=connectionNotice(snapshot,{connectionUI:true,...connectionWorkspaceState(workspace,snapshot)});
  assert.equal(notice.hidden,false);assert.ok(notice.message.includes(label));assert.equal(notice.rawError,detail);
  workspace.page='numbering';workspace.workspaceModeAwaiting=null;workspace.state={available:true,active:true,owned:true,mode:'numbering',session_id:'bench'};
  assert.equal(connectionNotice(snapshot,{connectionUI:true,...connectionWorkspaceState(workspace,snapshot)}).hidden,true);
 }
});

const report={passed:true,cases,source_sha256:Object.fromEntries(['static/app.js','static/four-pages.js','static/four-pages.css','static/numbering-position-stream.js'].map(n=>[n,sha(n)])),test_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL('./lifecycle-tests.mjs',import.meta.url))).digest('hex'),style_review:'A stable joint wrapper separates feedback/curve sizing from AI. Existing controlled tests cover navigation, request feedback and lifecycle; CSS is source-reviewed, with no browser pixels or computed geometry claimed.',boundary:'Actual shipped constructor registrations, action/scan/maintenance/stream eligibility methods and APP lifecycle callbacks run with controlled DOM/API/timers/observer factory. Ordinary focus/visibility perform no refresh or reader restart; existing periodic reads remain. Actual pagehide cleanup and explicit hardware/input blur safety paths remain separate. No browser-engine BFCache, real WS/SSE transport, SSH/UART, 3D pixels or hardware acceptance.'};
fs.writeFileSync(new URL('./lifecycle-results.json',import.meta.url),JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify({passed:true,cases:cases.length,source_sha256:report.source_sha256}));
