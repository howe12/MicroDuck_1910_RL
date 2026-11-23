import {controlFetch, getControlToken, refreshControlSession} from './control-session.js';
import {consumeImuEvents, consumeMaintenanceEvents} from './maintenance-imu-stream.js';
import {numberingPositionContext,numberingPositionKey,normalizeNumberingPositionFrame,numberingPositionFresh,numberingPositionRows,numberingPositionRevision} from './numbering-position-stream.js';
import {showMessageDialog} from './dialogs.js';
import {audioLevelBars} from './audio-level.js';
import {modeSwitchError} from './messages.js';
// V262 task pages share the established controls; the Zero owns mode permissions.
const $ = id => document.getElementById(id);
const finite = v => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, n = 1) => finite(v) ? v.toFixed(n) : '—';
const PAGE_KEYS = ['hardware', 'numbering', 'calibration', 'robot'];
const SWITCH_LABELS = {hardware:'切换到测试模式',numbering:'切换到编号模式',calibration:'切换到标定模式',robot:'切换到运动模式'};
const NUMBERING_JOINT_IDS = new Set([10,11,12,13,14,20,21,22,23,24,30,31,32,33,34]);
const MAINTENANCE_MODES = {numbering:'numbering', hardware:'hardware-test'};
const META = {
  numbering: {"title": "舵机编号", "step": "02", "subtitle": "扫描、选择编号与中位校准", "photo": "numbering-wiring-white-v268.png", "width": 1448, "height": 1086, "caption": "HAT / HUB 与单颗舵机接线实拍；其他不同 ID 设备可留在链上", "steps": ["先断电，按实拍核对电源极性与 GND / 电源 / DATA 线序。鳄鱼夹仅为供电引线，电源主体未入镜。", "扫描可接多颗舵机及 IMU。疑似重复或通信冲突会显示具体 ID，请只保留一颗该 ID 后核对；没有提示也不能证明 ID 唯一。", "扫描完成后，在待修改舵机所在行选择目标关节编号；空闲编号直接保存，已占用编号则互换这两颗舵机，其他舵机不改号。", "选择舵机后可点击公共中位校准，把当前位置设为 2048。其他舵机和 IMU 可以保持连接；回执成功后可直接继续选择其他舵机，对应关节需重新标定。"]},
  calibration: {title: '关节标定', step: '03', subtitle: '连接完整机器人后核对零点、方向与限位', photo: 'imu-installed.jpg', caption: 'IMU 安装与总线接线实拍 · 标定时连接完整舵机链', steps: ['断电接回 15 颗舵机与 IMU，按实板丝印核对各接口与供电。', '放稳并支撑机身，确认所有关节编号齐全，再切换到标定模式。', '依次核对实际角度、正方向、机械限位与 IMU 安装，保存有效标定。']},
  robot: {title: '机器人控制', step: '04', subtitle: '运动状态、实物手柄、AI 与实时观测', photo: 'robot-ready.jpg', caption: '完整机器人实拍 · 使用前检查供电、线束与机身支撑', steps: ['接好完整机器人与电池，确认线束不会进入关节运动范围。', '标定完整有效后切换到运动模式，等待实际服务状态确认。', '先手动回 HOME，再启用保持或动作。需要调整标定时先切回标定模式。']},
  hardware: {title: 'IMU / HAT / HUB 测试', step: '01', subtitle: '单独检查姿态、收音、鸭叫与供电接线', photo: 'hardware-wiring-white-v268.png', width:1086, height:1448, caption: 'IMU / HAT / HUB / 扬声器白底提亮实拍 · 姿态、收音与鸭叫测试', steps: ['先断电，按图连接 HAT、HUB、IMU 与扬声器；鳄鱼夹仅为供电引线，电源主体未入镜，保持原有线序。', '核对供电极性、接口方向与实板丝印。单 IMU 测试无需舵机、编号或标定；HUB 是被动供电分线板。', '点击测试菜单，按提示停止总线并进入；转动 IMU 观察姿态，对 HAT 麦克风说话，再播放鸭叫检查扬声器。']}
};
const storage = {
  get(key) {try {return localStorage.getItem(key);} catch {return null;}},
  set(key, value) {try {localStorage.setItem(key, value);} catch {}}
};
const section = (id, className = '') => {const el = document.createElement('section'); el.id = id; el.className = className; return el;};
const append = (host, ...nodes) => nodes.filter(Boolean).forEach(node => host.append(node));

export function tutorialKey(page) {return `microduck-v261-tutorial-${page}`;}
function messageText(value) {return typeof value === 'string' ? value.trim() : typeof value?.message === 'string' ? value.message.trim() : '';}
function numberingPositionText(row) {
  if(row?.id===200&&row?.kind==='imu')return '—';
  if(messageText(row?.position_error))return '读取失败';
  return Number.isInteger(row?.position_raw)&&row.position_raw>=0&&row.position_raw<=65535?String(row.position_raw):'待读取';
}
function maintenanceReason(state, pageReason = '') {
  const detail = messageText(state?.error);
  if (state?.needs_components === true || state?.requires_component_install === true) return '当前缺少编号 / 测试维护组件；点击编号或硬件测试菜单，将检查并准备所需组件。' + (detail ? ` ${detail}` : '');
  if (state?.needs_firmware === true) return '当前固件组件需要修复；请在“固件与电源”安装本版配套固件后重试。' + (detail ? ` ${detail}` : '');
  if (detail) return detail;
  if (pageReason) return pageReason;
  return [...new Set(PAGE_KEYS.map(key => messageText(state?.allowed_pages?.[key]?.reason)).filter(Boolean))].join('；') || '等待 ZERO 工作状态';
}
export function normalizePermissions(state) {
  const source = state?.allowed_pages || {};
  return Object.fromEntries(PAGE_KEYS.map(key => {
    const value = source[key];
    const allowed = value?.allowed === true;
    return [key, {allowed,can_start:value?.can_start===true, browsable: value?.browsable===true || Boolean(MAINTENANCE_MODES[key]) || allowed, reason: messageText(value?.reason) || (allowed ? '' : maintenanceReason(state))}];
  }));
}
export function normalizeTestImu(state, receivedAt, now = performance.now()) {
  const test = state?.imu || {}, value = test.sample || test;
  const gravity = value.gravity;
  const fresh = state?.active === true && ['hardware-test', 'hardware_test'].includes(state.mode)
    && (state.imu_running === true || test.running === true || test.active === true || value.status === 'live')
    && now - receivedAt < 1800 && value.status === 'live'
    && value.ready === true && Array.isArray(gravity) && gravity.length === 3 && gravity.every(finite)
    && (!finite(value.age_ms) || value.age_ms <= 50) && (!finite(value.sample_age_ms) || value.sample_age_ms <= 600);
  return {...value, status: fresh ? 'live' : value.status === 'error' ? 'error' : 'waiting',
    gravity: fresh ? gravity : null, roll_deg: fresh ? value.roll_deg : null, pitch_deg: fresh ? value.pitch_deg : null,
    gyro: fresh ? value.gyro : null, quat: fresh ? value.quat : null, ready: fresh && value.ready === true,
    source: 'HAT 单 IMU 只读测试', reason: fresh ? '' : value.error || value.reason || '等待单 IMU 测试的有效样本'};
}

function playbackEnvelopeValid(speaker){
  return speaker.source==='playback-file'&&Array.isArray(speaker.envelope_dbfs)
    &&speaker.envelope_dbfs.length>0&&speaker.envelope_dbfs.length<=201
    &&speaker.envelope_dbfs.every(value=>finite(value)&&value>=-96&&value<=0)
    &&finite(speaker.envelope_period_ms)&&speaker.envelope_period_ms>0
    &&finite(speaker.duration_ms)&&speaker.duration_ms>0&&finite(speaker.elapsed_ms)&&speaker.elapsed_ms>=0;
}
export function speakerPlaybackLevel(state,receivedAt,now=performance.now(),interval) {
  const audio=state?.audio||{},speaker=audio.speaker||audio.duck||{},levels=speaker.envelope_dbfs;
  const age=now-receivedAt,period=speaker.envelope_period_ms,duration=speaker.duration_ms,elapsed=speaker.elapsed_ms;
  const playing=speaker.state==='playing'||speaker.playing===true||audio.duck_playing===true;
  if(state?.available!==true||state.active!==true||state.owned!==true||!['hardware-test','hardware_test'].includes(state.mode)
    ||!(playing||interval&&speaker.state==='completed')||speaker.error||audio.error
    ||!finite(age)||age<0||age>=1800||!playbackEnvelopeValid(speaker))return null;
  if(interval){
    const start=Math.max(0,interval.from),end=Math.min(duration,interval.to);
    if(!finite(start)||!finite(end)||end<=start)return null;
    const values=levels.slice(Math.floor(start/period),Math.min(levels.length,Math.ceil(end/period)));
    return values.length?Math.max(...values):null;
  }
  if(!playing||elapsed+age>=duration)return null;
  return levels[Math.floor((elapsed+age)/period)]??null;
}

export class FourPageConsole {
  constructor(cfg, {servicePanel, controls, notify}) {
    this.cfg = cfg; this.servicePanel = servicePanel; this.controls = controls; this.notify = notify;
    this.state = {available: false}; this.permissions = normalizePermissions(this.state); this.page = null;
    this.receivedAt = -Infinity; this.imuReceivedAt = -Infinity; this.imuStreamEpoch = 0; this.sessionEpoch = 0; this.stateEpoch = 0; this.busy = false; this.snapshot = null; this.stopped = false;
    this.positionStreamEpoch=0;this.positionReceivedAt=-Infinity;
    this.mount(); this.bind(); this.refresh();
    this.numberingResize = typeof ResizeObserver==='function'?new ResizeObserver(()=>this.sizeNumberingCards()):null;
    this.numberingResize?.observe($('page-numbering').querySelector('.numbering-grid'));
    window.addEventListener('resize',()=>this.sizeNumberingCards());
    this.maintenancePollAt=performance.now();this.startMaintenanceLoop();
    window.addEventListener('pagehide',()=>this.pausePageObservations());
    window.addEventListener('pageshow',()=>{if(this.stopped)void this.resumePageObservations();});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden&&this.stopped)void this.resumePageObservations();});
    window.addEventListener('r17-control-session-changed', () => this.resetSession());
  }
  startMaintenanceLoop() {
    if(this.timer==null)this.timer=setInterval(()=>this.pollMaintenance(),100);
  }
  pausePageObservations() {
    const release=this.state.active===true&&this.state.owned===true;
    this.lifecycleEpoch=(this.lifecycleEpoch||0)+1;this.sessionEpoch++;this.stateEpoch++;
    this.stopped=true;this.resumeObservationPromise=null;this.lifecycleResuming=false;
    this.busy=false;this.entryPending=false;this.scanStopPending=false;this.polling=false;this.maintenanceReadPromise=null;this.maintenanceReadToken={};
    this.workspaceModeRequestToken={};this.workspaceModeAwaiting=null;this.workspaceModeLastCommand=null;
    this.centerPending=null;this.centerPositionAwaiting=null;this.centerReadback=null;this.positionRevisionFloors=null;this.swapPending=null;this.swapPositionAwaiting=null;
    this.speakerMeterRun=null;this.speakerMeterDisplay=null;this.speakerMeterSuppressed=true;
    this.requestAbort?.abort();this.stopImuStream();this.stopPositionStream();clearInterval(this.timer);this.timer=null;
    this.numberingResize?.disconnect();if(this.numberingLayoutRaf!=null)window.cancelAnimationFrame(this.numberingLayoutRaf);this.numberingLayoutRaf=null;
    // Preserve the displayed page, selection and drafts. They are read-only until
    // the returning page has observed current ownership and state again.
    this.receivedAt=-Infinity;this.workspaceSnapshotAt=-Infinity;
    // Keep the existing single safety release. A return only observes its result;
    // it never repeats this POST or enters another robot mode automatically.
    if(release)this.pageLeavePromise=controlFetch('/api/maintenance',{method:'POST',keepalive:true,
      headers:{'Content-Type':'application/json','X-Control-Token':getControlToken(this.cfg)},body:JSON.stringify({action:'end'}),signal:AbortSignal.timeout(3500)}).catch(()=>{});
  }
  async resumePageObservations() {
    if(document.hidden)return false;
    if(this.resumeObservationPromise)return this.resumeObservationPromise;
    if(!this.stopped)return false;
    const restoring=this.stopped,generation=this.lifecycleEpoch||0;
    if(!restoring&&(this.busy||this.centerPending||this.entryPending||this.scanStopPending))return false;
    this.stopped=false;this.lifecycleResuming=true;this.startMaintenanceLoop();
    if(restoring){this.numberingResize?.observe($('page-numbering')?.querySelector('.numbering-grid'));this.paint({route:false});}
    const current=()=>!this.stopped&&!document.hidden&&(this.lifecycleEpoch||0)===generation;
    const task=(async()=>{
      if(restoring){await this.pageLeavePromise;if(!current())return false;}
      await refreshControlSession();if(!current())return false;
      await this.refresh();if(!current())return false;
      this.syncImuStream();this.syncPositionStream();this.sizeNumberingCards();
      if(restoring&&MAINTENANCE_MODES[this.page]&&!(this.state.active===true&&this.state.owned===true)){
        const ended=this.state.available===true&&this.state.active!==true;
        const message=ended?`页面会话已结束，请点击左侧${this.page==='numbering'?'编号':'测试'}模式重新进入。`:maintenanceReason(this.state);
        this.actionFeedback(this.page==='numbering'?'scan':'imu-start',message,!ended);
      }
      return true;
    })();
    this.resumeObservationPromise=task;
    try{return await task;}catch{return false;}
    finally{if(this.resumeObservationPromise===task){this.resumeObservationPromise=null;this.lifecycleResuming=false;if(current())this.paint({route:false});}}
  }
  mount() {
    document.body.classList.add('four-page-console');
    const nav = document.querySelector('.sidebar nav');
    nav.setAttribute('aria-label','工作模式切换');
    const modeLabel=document.querySelector('.sidebar .side-label');if(modeLabel)modeLabel.textContent='模式切换';
    nav.innerHTML = PAGE_KEYS.map(key => `<button type="button" class="task-menu" data-console-page="${key}" aria-controls="page-${key}" aria-disabled="true"><span class="task-number">${META[key].step}</span><span class="task-menu-copy"><b>${SWITCH_LABELS[key]}</b><small>等待 ZERO 工作状态</small></span><span class="task-lock" aria-hidden="true">○</span></button>`).join('');
    const record = document.createElement('button'); record.type = 'button'; record.id = 'open-recording'; record.className = 'button'; record.textContent = '录制与回放';
    record.onclick = () => $('recording-dialog').showModal();
    document.querySelector('.top-actions').prepend(record);
    document.querySelector('.breadcrumb').innerHTML = '我的 Microduck <span>/</span><strong id="task-breadcrumb">连接与准备</strong>';
    const main = document.querySelector('main'), footer = main.querySelector('footer');
    this.common = section('task-common', 'task-common');
    this.common.innerHTML = `<div class="task-state-line"><div><span class="eyebrow">MICRODUCK WORKSPACE</span><h1 id="task-title">连接与准备</h1></div></div><div id="task-feedback" class="panel-note" role="status" aria-live="polite" hidden></div>`;
    main.querySelector('.metrics').after(this.common);
    append(this.common,$('connection-banner'));
    this.mountWorkspaceModes();
    this.pages = section('task-pages'); main.insertBefore(this.pages, footer);
    for (const key of PAGE_KEYS) {const page = section(`page-${key}`, `task-page task-page-${key}`); page.hidden = true; page.setAttribute('aria-labelledby', 'task-title'); this.pages.append(page);}
    this.empty = section('task-empty', 'panel task-empty'); this.empty.innerHTML = '<span class="task-empty-mark">◎</span><h2>准备好，再进入下一步</h2><p id="task-empty-message">先连接 ZERO，页面会根据编号、标定和服务模式开放。</p>'; this.pages.prepend(this.empty);
    this.robotHero = $('service-hero') || document.querySelector('.hero-grid');
    if (this.robotHero) {this.robotHero.classList.remove('r17-three-columns'); this.robotHero.classList.add('robot-operation-grid');}
    this.twin = document.querySelector('.twin-panel'); this.motors = $('motors');
    this.jointChart = $('joint-chart')?.closest('.motor-detail');
    append($('page-robot'), this.robotHero, $('ai-agent-panel'));
    const jointStack = section('robot-joint-stack', 'robot-joint-stack'); $('page-robot').append(jointStack);
    const sharedRobot = section('robot-shared'); jointStack.append(sharedRobot);
    append(sharedRobot, this.motors);
    this.robotCurveHost = section('robot-joint-curve', 'robot-joint-curve-host');
    this.robotCurveHost.setAttribute('aria-label', '选中关节曲线'); jointStack.append(this.robotCurveHost);
    append(this.robotCurveHost, this.jointChart);
    const robotSensors = section('robot-observation', 'task-extra task-observation');
    robotSensors.setAttribute('aria-labelledby', 'robot-observation-title');
    robotSensors.innerHTML = '<div class="task-observation-heading"><h2 id="robot-observation-title">传感器与系统观测</h2></div><div class="robot-observation-grid" id="robot-observation-grid"></div>';
    const imuPanel = $('imu-badge')?.closest('.panel'), tofPanel = $('tof-badge')?.closest('.panel'), systemPanel = $('sys-cpu')?.closest('.panel');
    if (systemPanel) systemPanel.querySelector('h2').textContent = '系统状态';
    append(robotSensors.querySelector('.robot-observation-grid'), $('camera-panel'), tofPanel, systemPanel); $('page-robot').append(robotSensors);
    const calibrationGrid = section('calibration-grid', 'calibration-page-grid'); $('page-calibration').append(calibrationGrid);
    const workbenchStack = section('calibration-workbench-stack', 'calibration-workbench-stack');
    append(workbenchStack, $('service-editor')); calibrationGrid.append(workbenchStack);
    const sharedCalibration = section('calibration-shared'); workbenchStack.append(sharedCalibration);
    const calibrationReadback = section('calibration-readback', 'calibration-readback'); $('page-calibration').append(calibrationReadback);
    append(calibrationReadback, $('service-observation'), $('service-inspection'));
    this.mountNumbering(); this.mountHardware(imuPanel);
    this.sharedDetails = document.createElement('details'); this.sharedDetails.className = 'task-extra shared-maintenance'; this.sharedDetails.open = true;
    this.sharedDetails.innerHTML = '<summary>固件与电源 · 链路与系统日志</summary><div class="task-service-recovery"><button class="button" id="task-stop-service" disabled>仅停止总线服务</button><p class="tiny muted">编号或硬件测试可直接点击左侧菜单；需要单独停止总线服务时使用。</p></div>';
    append(this.sharedDetails, $('diagnostics')); main.insertBefore(this.sharedDetails, footer);
    // Empty historical layout wrappers retain no interactive controls.
    for (const id of ['service-panel', 'sensors']) {const el = $(id); if (el && !el.querySelector('button,input,canvas,video')) el.hidden = true;}
    const observation = document.querySelector('.observation-ai-grid'); if (observation && !observation.querySelector('.panel')) observation.hidden = true;
    this.mountRealGamepad();
  }
  mountWorkspaceModes() {
    const navigation=document.querySelector('.sidebar nav');
    for(const key of ['hardware','numbering','calibration','robot'])navigation.append(document.querySelector(`[data-console-page="${key}"]`));
    // The sidebar cards are the only mode controls. No hidden legacy buttons.
    $('service-mode-switch')?.remove();$('shared-mode-host')?.remove();
    this.servicePanel.workspaceModeController=this;
  }
  workspaceModeFeedback(message, status='running', key) {
    this.workspaceModeStatus={message:String(message||''),status};
    if(status==='error'&&message){
      const errorKey=key??`workspace-mode-request:${this.workspaceModeErrorGeneration||0}`;
      this.workspaceModeShownErrors??=new Set();
      if(this.workspaceModeShownErrors.has(errorKey))return;
      this.workspaceModeShownErrors.add(errorKey);
      const job=this.snapshot?.service_control?.job;
      const startup=job?.action==='switch-mode'&&job.status==='error'&&message===job.error?job.startup:undefined;
      void showMessageDialog(modeSwitchError(message,startup),{title:'模式切换失败',tone:'error',key:errorKey});
    }
  }
  workspaceModeContext() {
    return {epoch:this.sessionEpoch,target:this.connectionTarget??this.cfg.ssh_target??'',client:this.servicePanel?.client};
  }
  workspaceModeContextMatches(context) {
    const current=this.workspaceModeContext();return !this.stopped&&context.epoch===current.epoch&&context.target===current.target&&context.client===current.client;
  }
  actualWorkspaceMode() {
    if(this.options?.replay||this.options?.offline)return null;
    if(this.cfg.connection_ui&&(this.snapshot?.connection?.active!==true||this.snapshot?.connection?.connected!==true))return null;
    if(this.state.active===true)return this.state.available===true&&this.state.owned===true&&performance.now()-this.receivedAt<1800?(this.state.mode==='numbering'?'numbering':['hardware-test','hardware_test'].includes(this.state.mode)?'hardware':null):null;
    const s=this.snapshot;
    if(performance.now()-this.workspaceSnapshotAt<1800&&s?.service_sample_fresh===true&&s?.channels?.bus?.status==='live'&&['commissioning','motion'].includes(s.bus?.mode))return s.bus.mode;
    return performance.now()-this.receivedAt<1800&&['commissioning','motion'].includes(this.state.service_process_mode)?this.state.service_process_mode:null;
  }
  workspaceModeReason(target,{navigation=false}={}) {
    const s=this.snapshot, state=this.state;
    if(this.options?.replay||this.options?.offline||!navigation&&document.hidden||!s||this.cfg.service_enabled!==true)return '当前未处于可操作的实时连接。';
    if(this.cfg.connection_ui&&(s.connection?.active!==true||s.connection?.connected!==true)||s.connection_paused)return '请先连接 ZERO。';
    const ownedBench=state.available===true&&state.active===true&&state.owned===true&&['numbering','hardware-test','hardware_test'].includes(state.mode)&&messageText(state.session_id)&&state.pending===false&&state.scan?.running!==true&&!state.error&&performance.now()-this.receivedAt<1800;
    if(!navigation&&(state.scan?.running===true||this.scanStopPending))return '扫描进行中，请先停止扫描。';
    if(!navigation&&(this.servicePanel.posting||this.servicePanel.centerConfirmation||s.service_operation&&!ownedBench||s.service_control?.busy||s.service_control?.stopping||s.service_control?.job?.status==='running'||state.pending))return '已有服务操作正在执行，请等待实际回执。';
    if(state.active===true&&(state.available!==true||state.owned!==true||!messageText(state.session_id)))return '当前维护会话尚未确认归属，请先核对实际状态。';
    return '';
  }
  paintWorkspaceModes() {
    if(this.centerPending){this.patchCenterModeLocks();return;}
    const actual=this.actualWorkspaceMode(), waiting=this.workspaceModeAwaiting;
    if(waiting){
      const job=this.snapshot?.service_control?.job;
      if(!this.workspaceModeContextMatches(waiting.context)){
        this.workspaceModeAwaiting=null;this.busy=false;this.entryPending=false;this.workspaceModeFeedback('连接上下文已改变，请核对原目标的实际结果；操作未自动重发。','error');
      }else if(actual===waiting.target&&this.workspaceSnapshotEpoch>waiting.sourceSnapshotEpoch){
        this.workspaceModeAwaiting=null;this.busy=false;this.entryPending=false;this.workspaceModeFeedback('');
      }else if(!waiting.commandCompleted&&job?.action==='switch-mode'&&job.command?.target===waiting.target&&job.status==='completed'&&JSON.stringify(job)!==waiting.sourceJob){
        waiting.commandCompleted=true;this.busy=false;this.entryPending=false;this.workspaceModeFeedback('模式操作已完成，等待机器人实时反馈；也可切换其他模式。');
      }else if(job?.action==='switch-mode'&&job.command?.target===waiting.target&&job.status==='error'&&JSON.stringify(job)!==waiting.sourceJob){
        this.workspaceModeAwaiting=null;this.busy=false;this.entryPending=false;this.workspaceModeFeedback(messageText(job.error)||'模式切换失败，请检查实际状态；不会自动重发。','error',`workspace-mode-job:${waiting.errorGeneration}:${JSON.stringify(job)}`);
      }else if(performance.now()-waiting.started>90000){
        this.workspaceModeAwaiting=null;this.busy=false;this.entryPending=false;this.workspaceModeFeedback('尚未收到目标模式的实时确认，请核对实际状态；不会自动重发。','error');
      }
    }
    const job=this.snapshot?.service_control?.job,last=this.workspaceModeLastCommand,jobKey=JSON.stringify(job);
    if(last&&this.workspaceModeContextMatches(last.context)&&job?.action==='switch-mode'&&job.command?.target===last.target&&job.status==='error'&&jobKey!==last.sourceJob){
      this.workspaceModeFeedback(messageText(job.error)||'模式切换失败，请检查实际状态；不会自动重发。','error',`workspace-mode-job:${last.errorGeneration}:${jobKey}`);
    }
    const locked=this.workspaceModeNavigationLocked();
    // A late status read changes action freshness, not the last owned bench
    // mode. Keep its navigation stable until a reply reports a different state.
    const displayMode=actual||(!this.options?.offline&&!this.options?.replay
      &&(!this.cfg.connection_ui||this.snapshot?.connection?.active===true&&this.snapshot?.connection?.connected===true)
      &&this.state.active===true&&this.state.owned===true
      ?this.state.mode==='numbering'?'numbering':['hardware-test','hardware_test'].includes(this.state.mode)?'hardware':null:null);
    for(const [page,target] of [['hardware','hardware'],['numbering','numbering'],['calibration','commissioning'],['robot','motion']]){
      const button=document.querySelector(`[data-console-page="${page}"]`);if(!button)continue;
      const reason=this.workspaceModeReason(target,{navigation:true}),selected=displayMode===target;
      if(button.classList.contains('active')!==selected)button.classList.toggle('active',selected);
      for(const [name,value] of [['aria-pressed',String(selected)],['aria-current',selected?'page':'false'],['aria-disabled',String(locked)]])if(button.getAttribute(name)!==value)button.setAttribute(name,value);
      if(button.disabled!==locked)button.disabled=locked;
      const awaitingThis=this.workspaceModeAwaiting?.commandCompleted===true&&this.workspaceModeAwaiting.target===target;
      const title=reason||(awaitingThis?'模式操作已完成，等待实时反馈；不会重复提交。':'按实际回执切换工作模式');if(button.title!==title)button.title=title;
      const description=button.querySelector('small'),copy=selected?'当前模式':awaitingThis?'等待实时反馈':reason||'切换模式并进入';if(description&&description.textContent!==copy)description.textContent=copy;
      const marker=button.querySelector('.task-lock'),symbol=locked?'○':selected?'●':'›';if(marker&&marker.textContent!==symbol)marker.textContent=symbol;
    }
  }
  workspaceModeNavigationLocked() {
    const job=this.snapshot?.service_control?.job;
    return Boolean(this.lifecycleResuming||this.entryPending||this.state.pending
      ||this.workspaceModeAwaiting&&!this.workspaceModeAwaiting.commandCompleted
      ||job?.action==='switch-mode'&&job.status==='running');
  }
  workspaceModeInFlight() {
    const service=this.snapshot?.service_control;
    return Boolean(this.lifecycleResuming||this.busy||this.centerPending||this.scanStopPending||this.servicePanel?.posting||service?.busy||service?.stopping||service?.job?.status==='running'||this.state.pending);
  }
  async requestWorkspaceMode(target) {
    if(!['hardware','numbering','commissioning','motion'].includes(target))return false;
    const requestedTarget=target;
    let redirectReason='';
    this.workspaceModeErrorGeneration=(this.workspaceModeErrorGeneration||0)+1;
    const errorGeneration=this.workspaceModeErrorGeneration;
    const reportRedirect=()=>{if(!redirectReason)return false;this.workspaceModeFeedback(redirectReason,'error',`workspace-mode-redirect:${errorGeneration}`);return true;};
    if(this.lifecycleResuming||this.centerPending||this.busy||this.workspaceModeAwaiting&&!this.workspaceModeAwaiting.commandCompleted||this.scanStopPending){this.workspaceModeFeedback('当前操作尚未结束，请等待实际回执。','error');return false;}
    if(!this.workspaceModeAwaiting&&this.actualWorkspaceMode()===target)return this.open({hardware:'hardware',numbering:'numbering',commissioning:'calibration',motion:'robot'}[target]);
    if(target==='motion'&&this.state.calibration_valid!==true&&this.state.numbering_verified!==true&&this.state.initial_page==='numbering'){
      target='numbering';redirectReason='尚未确认舵机编号，也未完成有效标定，未进入运动模式。请先在编号页面扫描并完成编号，再进行关节和 IMU 标定。';
    }
    const reason=this.workspaceModeReason(target);
    if(reason){this.workspaceModeFeedback(reason,'error');return false;}
    const superseding=this.workspaceModeAwaiting?.commandCompleted===true;
    if(superseding){if(this.workspaceModeAwaiting.target===target){this.workspaceModeFeedback('模式操作已完成，等待实时反馈；不会重复提交。');return false;}this.workspaceModeAwaiting=null;}
    if(!superseding&&this.actualWorkspaceMode()===target){const opened=this.open({hardware:'hardware',numbering:'numbering',commissioning:'calibration',motion:'robot'}[target]);return reportRedirect()?false:opened;}
    const context=this.workspaceModeContext(),requestToken={};this.workspaceModeRequestToken=requestToken;this.workspaceModeLastCommand=null;
    if(target==='numbering'||target==='hardware'){
      const label=target==='numbering'?'编号':'测试';this.workspaceModeFeedback(`正在检查${label}模式进入条件，请等待实际回执…`);
      const entered=await this.requestPage(target,{requestToken});
      if(requestToken!==this.workspaceModeRequestToken)return false;
      if(!this.workspaceModeContextMatches(context)){this.workspaceModeFeedback('连接上下文已改变，请重新核对实际模式；不会自动重发。','error');return false;}
      this.workspaceModeFeedback(entered?'':$('task-feedback').textContent||`${label}模式尚未进入。`,entered?'success':'error');if(entered&&reportRedirect())return false;return entered;
    }
    const sourceSession=messageText(this.state.session_id), sourceJob=JSON.stringify(this.snapshot?.service_control?.job);
    this.workspaceModeLastCommand={target,context,sourceJob,errorGeneration:this.workspaceModeErrorGeneration};
    this.busy=true;this.entryPending=true;this.controls?.suspend();this.paint();
    this.workspaceModeFeedback(this.state.active===true?'正在结束当前测试或编号并核验串口释放，再切换服务模式；请等待实际回执…':'正在切换服务模式，请等待实际回执…');
    try{
      // The workspace endpoint owns ordered session release and service switching.
      // Never dispatch a separate service request here.
      const data=await this.post(target==='motion'?'to-motion':'to-calibration',{},90000);
      if(requestToken!==this.workspaceModeRequestToken)return false;
      if(!this.workspaceModeContextMatches(context)||this.state.active===true&&messageText(this.state.session_id)!==sourceSession)throw Error('连接或维护会话已改变，请核对原目标实际结果；操作未自动重发。');
      if(data.accepted!==true)throw Error(messageText(data.error)||'未收到服务模式切换接收回执；不会自动重发。');
      this.acceptReply(data);
      if(['commissioning','motion'].includes(data.target))target=data.target;
      if(target!==requestedTarget)redirectReason=messageText(data.redirect_reason)||'尚未完成关节与 IMU 的有效标定，未进入运动模式。请先在标定页面完成并保存标定。';
      if(this.workspaceModeLastCommand)this.workspaceModeLastCommand.target=target;
      if(data.unchanged===true){this.workspaceModeAwaiting=null;this.busy=false;this.open(target==='motion'?'robot':'calibration',{automatic:true});this.workspaceModeFeedback('');return !reportRedirect();}
      this.workspaceModeAwaiting={target,context,sourceJob,requestToken,errorGeneration:this.workspaceModeErrorGeneration,sourceSnapshotEpoch:this.workspaceSnapshotEpoch||0,started:performance.now()};
      this.workspaceModeFeedback('切换请求已接收，正在等待机器人实际模式确认…');
      reportRedirect();await this.refresh();return !redirectReason;
    }catch(error){if(requestToken===this.workspaceModeRequestToken)this.workspaceModeFeedback(error.name==='TimeoutError'?'未收到模式切换回执，请核对实际状态；不会自动重发。':error.message,'error');return false;}
    finally{if(requestToken===this.workspaceModeRequestToken){this.entryPending=false;if(!this.workspaceModeAwaiting)this.busy=false;this.paint({suspendInvalid:false,route:false});}}
  }
  mountNumbering() {
    this.rowTargets=new Map();
    $('page-numbering').innerHTML=`<div class="numbering-grid">
      <section class="panel numbering-scan-card numbering-workbench" id="numbering-scan-card">
        <div class="panel-head"><div><span class="section-index">01</span><h2>扫描与舵机编号</h2></div><span id="numbering-badge" class="badge neutral">未开始</span></div>
        <div class="task-card-body">
          <div class="numbering-toolbar"><div class="task-button-row"><button class="button accent" id="numbering-scan" disabled>扫描</button><button class="button" id="numbering-scan-stop" disabled>停止扫描</button><button class="button accent" id="numbering-write" disabled>保存</button><button class="button" id="numbering-center" disabled>中位校准</button></div><div class="numbering-selection"><span>所选 ID <strong id="numbering-current">—</strong> → <strong id="numbering-selected-target">未选编号</strong></span><span>当前位置 <strong id="numbering-center-current">待读取</strong></span><span class="tiny muted">位置自动更新</span></div></div>
          <p id="numbering-feedback" class="numbering-operation-feedback" role="status" aria-live="polite" hidden></p>
          <div class="numbering-progress-line"><span id="numbering-scan-count">已扫 0 / 250</span><span id="numbering-scan-current">当前 ID —</span><span id="numbering-scan-found">发现设备 0</span><span id="numbering-scan-status" class="muted">尚未扫描</span></div><progress id="numbering-scan-progress" value="0" max="250" aria-label="ID 扫描进度"></progress>
          <div class="numbering-devices"><table id="numbering-devices" class="numbering-device-table" aria-label="扫描设备与逐颗编号"><colgroup><col class="device-id-column"><col class="device-position-column"><col class="device-joint-column"><col class="device-numbering-column"><col class="device-target-column"><col class="device-communication-column"></colgroup><thead><tr><th scope="col">当前 ID</th><th scope="col">当前位置</th><th scope="col">对应关节</th><th scope="col">编号状态</th><th scope="col">目标关节编号</th><th scope="col">通信状态</th></tr></thead><tbody id="numbering-device-rows"></tbody></table><p id="numbering-devices-empty" class="tiny muted">尚未发现设备</p></div>
          <div id="numbering-actions-footer" class="numbering-center-readback" hidden><span>校准前 <strong id="numbering-center-before">—</strong></span><span>校准后 <strong id="numbering-center-after">—</strong></span></div>
        </div>
      </section>
      <section class="panel task-photo-card"><div class="panel-head"><div><span class="section-index">02</span><h2>接线图</h2></div></div><div class="wiring-photo-open"><img src="/assets/tutorials/numbering-wiring-white-v268.png" width="1448" height="1086" alt="HD1910 与 HAT、HUB 的白底接线实拍"></div><div class="task-card-body"><p class="numbering-wiring-guidance"><strong>当舵机尚未编号时，请单独连接单个舵机进行编号，不能串联多个未编号舵机，否则无法识别；</strong><span>已编号舵机可以串联。</span></p></div></section>
    </div>`;
  }
  mountHardware(imuPanel) {
    const page = $('page-hardware');
    page.innerHTML = '<div class="board-test-grid" id="board-test-grid"></div>';
    const grid = $('board-test-grid');
    if (imuPanel) {
      imuPanel.id = 'board-imu-panel'; imuPanel.classList.add('board-test-part');
      imuPanel.querySelector('h2').textContent = 'IMU · 实时姿态';
      imuPanel.querySelector('.section-index').textContent = '01';
      const tools = document.createElement('div'); tools.className = 'task-card-body imu-test-toolbar';
      tools.innerHTML = '<p>轻轻转动 IMU，观察地平仪与角度是否跟随变化。</p><div class="task-button-row"><button class="button accent" id="board-imu-start" disabled>开始读取</button><button class="button" id="board-imu-stop" disabled>停止</button></div><div class="task-mini-status"><span id="board-imu-sequence">样本 —</span><span id="board-imu-age">新鲜度 —</span><span id="board-imu-rate">IMU 读取 — Hz</span><span id="board-imu-stream-rate">数据流 — Hz</span><span id="board-imu-errors">通信错误 —</span></div><p id="board-imu-feedback" class="tiny muted" role="status">尚未开始单 IMU 测试。</p>';
      imuPanel.querySelector('.panel-head').after(tools); grid.append(imuPanel);
    }
    const mic = section('board-mic-panel', 'panel board-test-part');
    mic.innerHTML = `<div class="panel-head"><div><span class="section-index">03</span><h2>HAT · 麦克风</h2></div><span id="board-mic-badge" class="badge neutral">未开始</span></div><div class="task-card-body"><p>开始收音后，对着 HAT 麦克风说话，观察电平变化。</p><div class="mic-level-display"><div id="board-mic-meter" class="mic-level-meter" role="meter" aria-label="麦克风实际音量" aria-valuemin="-90" aria-valuemax="0" aria-valuenow="-90">${Array.from({length:24}, () => '<i></i>').join('')}</div><strong id="board-mic-level">— dBFS</strong></div><div class="task-button-row"><button class="button accent" id="board-mic-start" disabled>开始收音</button><button class="button" id="board-mic-stop" disabled>停止</button></div><p class="task-result" id="board-mic-feedback" role="status">只检测 HAT 实际输入，不上传到语音服务。</p></div>`;
    const speaker = section('board-speaker-panel', 'panel board-test-part');
    speaker.innerHTML = `<div class="panel-head"><div><span class="section-index">02</span><h2>HAT · 扬声器</h2></div><span id="board-speaker-badge" class="badge neutral">未播放</span></div><div class="task-card-body"><div class="mic-level-display"><div id="board-speaker-meter" class="mic-level-meter" role="meter" aria-label="扬声器播放电平" aria-valuemin="-96" aria-valuemax="0" aria-valuenow="-96">${Array.from({length:24}, () => '<i></i>').join('')}</div><strong id="board-speaker-level">— dBFS</strong></div><p>使用机器人现有鸭叫声测试。确认扬声器已接 HAT 喇叭接口。</p><div class="task-button-row"><button class="button accent" id="board-duck-play" disabled>播放鸭叫</button><button class="button" id="board-duck-stop" disabled>停止</button></div><p class="task-result" id="board-speaker-feedback" role="status">播放后，听声音是否正常。</p></div>`;
    const hub = section('board-hub-panel', 'panel board-test-part task-photo-card');
    hub.innerHTML = `<div class="panel-head"><div><span class="section-index">04</span><h2>HUB · 供电与接线</h2></div></div><div class="wiring-photo-open"><img src="/assets/tutorials/hardware-wiring-white-v268.png" width="1086" height="1448" alt="IMU、HAT、HUB 与扬声器的白底提亮接线实拍；鳄鱼夹仅为供电引线"></div><div class="task-card-body"><p>断电核对供电极性与线序；单 IMU 测试无需舵机。</p></div>`;
    const audioStack=section('board-audio-stack','board-audio-stack');append(audioStack,speaker,mic);append(grid,audioStack,hub);
    for(const [card,feedbackId] of [[imuPanel,'board-imu-feedback'],[mic,'board-mic-feedback'],[speaker,'board-speaker-feedback']]){
      if(!card)continue;const controls=document.createElement('div');controls.className='board-test-controls';
      const buttons=card.querySelector('.task-button-row'),feedback=$(feedbackId);append(controls,buttons,feedback);card.querySelector('.panel-head').after(controls);
    }
  }
  selectedCenterServo(id=this.selectedCenterId) {
    const state=this.state,scan=state.scan,row=state.devices?.find(value=>value.id===id);
    if(state.available!==true||state.active!==true||state.owned!==true||state.mode!=='numbering'||!messageText(state.session_id)
      ||scan?.state!=='completed'||scan.completed!==true||scan.running===true||scan.requires_rescan===true
      ||!Number.isSafeInteger(scan.generation)||scan.generation===this.usedScanGeneration
      ||!Number.isInteger(id)||id===200||row?.kind!=='servo'||row.identity_verified!==true||row.register_id!==id
      ||row.error||row.suspected_duplicate||(scan.conflict_ids||[]).includes(id))return null;
    return row;
  }
  centerContextMatches(operation) {
    return this.centerPending===operation&&this.workspaceModeContextMatches(operation.context)
      &&this.state.session_id===operation.session&&this.state.scan?.generation===operation.generation
      &&this.state.available===true&&this.state.active===true&&this.state.owned===true&&this.state.mode==='numbering';
  }
  currentPositionRevisionFloors(key=numberingPositionKey(numberingPositionContext(this.state))) {
    if(this.positionRevisionFloors?.key!==key)this.positionRevisionFloors={key,revisions:{}};
    return this.positionRevisionFloors.revisions;
  }
  patchCenterModeLocks() {
    const locked=this.workspaceModeNavigationLocked();
    for(const page of PAGE_KEYS){const button=document.querySelector(`[data-console-page="${page}"]`);if(!button)continue;
      if(button.disabled!==locked)button.disabled=locked;
      if(button.getAttribute('aria-disabled')!==String(locked))button.setAttribute('aria-disabled',String(locked));
    }
  }
  paintCenterOperation() {
    this.patchCenterModeLocks();this.paintNumberingScan({local:true});this.paintNumberingPositions();
    const locked=this.workspaceModeInFlight(),scan=this.state.scan;
    for(const id of ['numbering-scan','numbering-write','numbering-center']){
      const button=$(id);if(button)button.disabled=locked||(id==='numbering-scan'?scan?.running===true:id==='numbering-write'?!this.numberingTargetAvailable:!Number.isInteger(this.currentCenterServoId));
    }
    for(const [id,row] of this.numberingPositionNodes||[]){
      const pending=this.centerPending?.id===id;
      if(row.getAttribute('aria-busy')!==String(pending))row.setAttribute('aria-busy',String(pending));
    }
  }
  async requestCenterCalibration() {
    return this.calibrateSelectedServo(this.selectedCenterId);
  }
  async calibrateSelectedServo(id) {
    if(this.workspaceModeInFlight())return;
    if(id!==this.selectedCenterId||!this.selectedCenterServo(id)||this.options?.replay||this.options?.offline||document.hidden
      ||this.stopped||performance.now()-this.receivedAt>=1800
      ||this.cfg.connection_ui&&(this.snapshot?.connection?.active!==true||this.snapshot?.connection?.connected!==true)){
      this.actionFeedback('center-calibrate','请先完成扫描并选中一颗身份已确认的舵机。',true);return;
    }
    const positionKey=numberingPositionKey(numberingPositionContext(this.state));
    const operation={id,context:this.workspaceModeContext(),session:this.state.session_id,generation:this.state.scan.generation,positionKey,
      positionRevision:numberingPositionRevision(this.state,id,this.currentPositionRevisionFloors(positionKey))};
    this.centerPending=operation;this.centerPositionAwaiting=operation;this.centerReadback=null;this.stateEpoch++;
    this.actionFeedback('center-calibrate',`正在校准 ID ${id}，请等待该舵机的实际回执…`,false,'running');this.paintCenterOperation();
    try {
      const data=await this.post('center-calibrate',{servo_id:id,selected_servo_confirmed:true,center_confirmed:true});
      if(!this.centerContextMatches(operation))return;
      const reply=data.state||(data.allowed_pages?data:null),receipt=data.center_receipt||data.result?.center_receipt||reply?.center_receipt;
      if(!reply||reply.session_id!==operation.session||reply.scan?.generation!==operation.generation
        ||reply.available!==true||reply.active!==true||reply.owned!==true||reply.mode!=='numbering'
        ||numberingPositionKey(numberingPositionContext(reply))!==operation.positionKey){
        this.actionFeedback('center-calibrate',`ID ${id} 的回执上下文不匹配，请核对实际状态；操作未自动重发。`,true);return;
      }
      const revision=numberingPositionRevision(reply,id),row=reply.devices?.find(value=>value.id===id);
      const validRevision=Number.isSafeInteger(revision)&&Number.isSafeInteger(operation.positionRevision)&&revision>operation.positionRevision;
      if(validRevision){const floors=this.currentPositionRevisionFloors(operation.positionKey);floors[id]=Math.max(floors[id]||0,revision);if(this.centerPositionAwaiting===operation)this.centerPositionAwaiting=null;}
      const validReceipt=validRevision&&this.centerReceiptVerified(receipt,id)
        &&row?.kind==='servo'&&row.identity_verified===true&&row.register_id===id&&!row.error&&!row.suspected_duplicate&&row.position_raw===2048;
      // Keep every other device and the current scan/drafts intact. Only real readback
      // from this selected transaction may replace its row; never synthesize 2048.
      const changes={};
      if(validReceipt&&row?.kind==='servo'&&row.identity_verified===true&&row.register_id===id&&!row.error&&!row.suspected_duplicate)
        changes.devices=this.state.devices.map(value=>value.id===id?{...row}:value);
      if(receipt?.servo_id===id)changes.center_receipt=receipt;
      for(const key of ['position_stream','calibration_valid','calibration','allowed_pages'])if(Object.hasOwn(reply,key))changes[key]=reply[key];
      this.state={...this.state,...changes};this.stateEpoch++;this.receivedAt=performance.now();this.permissions=normalizePermissions(this.state);
      if(validReceipt&&Number.isInteger(row?.position_raw)&&!row.position_error)this.centerReadback={id,position_raw:row.position_raw,position_revision:revision,key:operation.positionKey,receivedAt:this.receivedAt};
      this.actionFeedback('center-calibrate',validReceipt?this.centerReceiptMessage(receipt):`ID ${id} · 中位校准未确认成功：${messageText(receipt?.error)||'所选舵机的实际回读尚未确认'}。请核对实际状态；操作未自动重发。`,!validReceipt);
    } catch(error) {
      if(this.centerContextMatches(operation))this.actionFeedback('center-calibrate',`ID ${id} · `+(error.name==='TimeoutError'?'未收到操作回执，请核对实际状态；写入不会自动重发。':error.message),true);
    } finally {
      if(this.centerPending===operation){this.centerPending=null;this.paintCenterOperation();}
    }
  }
  async requestPage(page,{requestToken={}}={}) {
    if (this.state.scan?.running === true) {this.feedback('扫描进行中，请先停止扫描后再切换页面。',true); return false;}
    if (this.lifecycleResuming || this.centerPending || this.busy || this.options?.replay === true) {this.feedback('当前操作尚未结束，请稍后重试。', true); return false;}
    const mode = MAINTENANCE_MODES[page];
    if (!mode) return this.open(page);
    const sessionEpoch = this.sessionEpoch,entryContext=this.workspaceModeContext();this.workspaceModeRequestToken=requestToken; this.busy = true; this.entryPending = true; this.paint(); this.feedback(`正在检查${META[page].title}的进入条件…`);
    try {
      const preflight = await this.post('preflight', {mode});
      if(requestToken!==this.workspaceModeRequestToken||!this.workspaceModeContextMatches(entryContext))throw Error('连接上下文已改变，未继续进入维护；不会自动重发。');
      this.acceptReply(preflight);
      if (preflight.accepted !== true || preflight.can_enter !== true) throw Error(preflight.reason || '当前条件不允许进入，请检查连接与总线状态。');
      if (typeof preflight.entry_target !== 'string' || !preflight.entry_target.trim()) throw Error('未能确认连接目标，请重新点击菜单检查后进入。');
      const stopConfirmed = preflight.requires_stop === true;
      if (this.stopped || sessionEpoch !== this.sessionEpoch || !this.workspaceModeContextMatches(entryContext)) return false;
      this.controls?.suspend();
      this.feedback(preflight.requires_component_install === true ? '正在准备编号 / 测试组件，不影响标定和模型；请等待实际回执…' : stopConfirmed ? '正在停止总线并释放串口；请等待实际回执…' : '正在进入维护，请等待实际回执…');
      const data = await this.post('enter', {mode, stop_confirmed:stopConfirmed, entry_target:preflight.entry_target}, 90000);
      if(requestToken!==this.workspaceModeRequestToken||!this.workspaceModeContextMatches(entryContext))throw Error('连接上下文已改变，旧目标回执未采用；不会自动重发。');
      if (data.accepted !== true) throw Error('未收到进入维护的确认回执，请检查实际状态后重试。');
      this.acceptReply(data);
      if (this.state.active !== true || this.state.owned !== true || this.state.mode !== mode) throw Error('尚未确认本页维护会话，未启用硬件操作；请检查实际状态后重试。');
      this.imuStreamBlocked=false;
      this.currentServoId = null; this.scannedIds = []; this.open(page, {automatic:true,entered:true});
      this.feedback('');
      await this.refresh(); return true;
    } catch (error) {if(requestToken===this.workspaceModeRequestToken)this.feedback(error.name === 'TimeoutError' ? '进入操作未收到回执，请检查实际状态后重试；不会自动重发。' : error.message, true); return false;}
    finally {if(requestToken===this.workspaceModeRequestToken){this.entryPending = false; this.busy = false; this.paint({suspendInvalid:false, route:false});}}
  }
  mountRealGamepad() {
    const pad = document.querySelector('.official-pad'); if (!pad) return;
    const body = pad.querySelector('.xbox-body'), shoulders = pad.querySelector('.pad-shoulders');
    if (!body || !shoulders) return;
    pad.classList.add('photo-gamepad');
    const surface = document.createElement('div'); surface.className = 'real-pad-surface';
    surface.innerHTML = '<img src="/assets/gamepad-real.png" alt="根据用户实物照片转绘的 GameSir 手柄：左上左摇杆、左下方向键、右下右摇杆、右上 ABXY">';
    const positions = [
      ['[data-stick="l"]',25.1,32.1,16,24,'stick'], ['[data-stick="r"]',62.5,52.2,16,24,'stick'],
      ['[data-button="select"]',39.3,20.6,5.7,10,'small'], ['[data-button="start"]',60.7,20.5,5.7,10,'small'],
      ['[data-button="guide"]',50,18.9,7,10.5,'round'],
      ['[data-button="y"]',74.4,22.7,7,10.5,'round'], ['[data-button="b"]',80.9,31.6,7,10.5,'round'], ['[data-button="a"]',74.5,41,7,10.5,'round'], ['[data-button="x"]',68.7,31.6,7,10.5,'round'],
      ['[data-dpad="dy:-1"]',37.9,45.5,7,8,'dpad'], ['[data-dpad="dy:1"]',37.9,61.5,7,8,'dpad'], ['[data-dpad="dx:-1"]',32.5,53.6,5.5,10,'dpad'], ['[data-dpad="dx:1"]',43.2,53.6,5.5,10,'dpad'],
      ['[data-button="rb"]',74,8.8,13,6,'shoulder'],
      ['[data-button="ls"]',25.1,32.1,6,9,'stickpress'], ['[data-button="rs"]',62.5,52.2,6,9,'stickpress']
    ];
    for (const [selector,x,y,w,h,kind] of positions) {
      const el = pad.querySelector(selector); if (!el) continue;
      el.classList.add('real-pad-hotspot', `real-pad-${kind}`);
      el.style.cssText += `;--pad-x:${x}%;--pad-y:${y}%;--pad-w:${w}%;--pad-h:${h}%;`;
      if (el.tagName === 'BUTTON') el.setAttribute('aria-label', el.title || el.textContent.trim());
      surface.append(el);
    }
    for (const [text,x,y,w,h,label] of [['LB',26,8.7,13,6,'LB：实体手柄左肩键'],['○',50,29.6,7,6,'中部功能键：仅显示实物位置'],['M',50.5,64.5,7,6,'M：实体手柄功能键']]) {
      const button = document.createElement('button'); button.type = 'button'; button.disabled = true; button.dataset.unavailable = 'true';
      button.className = 'real-pad-hotspot real-pad-reference'; button.textContent = text; button.title = label; button.setAttribute('aria-label', label);
      button.style.cssText = `--pad-x:${x}%;--pad-y:${y}%;--pad-w:${w}%;--pad-h:${h}%`; surface.append(button);
    }
    const clickButtons = body.querySelector('.pad-stick-clicks');
    const extra = document.createElement('div'); extra.className = 'real-pad-extra';
    const guide=document.createElement('div');guide.className='real-pad-guide';
    guide.innerHTML=`
      <div class="real-pad-guide-top">
        <div class="real-pad-callout real-pad-guide-select"><h3>Select · 卸力 / 关机</h3><p>短按松开：卸力</p><p>长按 2 秒：坐下、卸力并关机</p></div>
        <div class="real-pad-callout real-pad-guide-start"><h3>Start · 回位 / 保持</h3><p>首次按：回 HOME</p><p>回位完成再按：进入保持</p><p>保持中再按：回 HOME</p></div>
      </div>
      <div class="real-pad-canvas">
        <svg class="real-pad-connectors" viewBox="0 0 100 50.6667" aria-hidden="true" focusable="false">
          <path d="M41.87 -4 V10.44 M58.13 -4 V10.39 M23 11.5 H26 L31.08 16.26 M78 16 H75.4 L73.48 16 M25 27.16 H36.7 M25 35 H40.8 V31.16 M75 37 H68 L59.5 26.45"/>
          <g><circle cx="41.87" cy="10.44" r=".55"/><circle cx="58.13" cy="10.39" r=".55"/><circle cx="31.08" cy="16.26" r=".55"/><circle cx="73.48" cy="16" r=".55"/><circle cx="36.7" cy="27.16" r=".55"/><circle cx="40.8" cy="31.16" r=".55"/><circle cx="59.5" cy="26.45" r=".55"/></g>
        </svg>
        <div class="real-pad-callout real-pad-guide-walk"><h3>左摇杆 · 行走</h3><p>上下：前进 / 后退</p><p>左右：左移 / 右移</p></div>
        <div class="real-pad-callout real-pad-guide-dpad"><p><b>←</b> 短按：装死<small>实体手柄</small></p><p><b>↓</b> 坐下 / 站起</p></div>
        <div class="real-pad-callout real-pad-guide-abxy"><p><b>Y</b><span>切换头部 / 行走模式</span></p><p><b>X</b><span>前滚翻<br>按住连续翻滚</span></p><p><b>A</b><span>低头捡物</span></p><p><b>B</b><span>未分配</span></p></div>
        <div class="real-pad-callout real-pad-guide-turn"><h3>右摇杆 · 转向 / 表情</h3><p>左右：左转 / 右转</p><small>R3：轮播表情 · 实体手柄</small></div>
      </div>`;
    guide.querySelector('.real-pad-canvas').prepend(surface);
    append(extra, clickButtons); body.replaceWith(guide); pad.append(extra);
    shoulders.classList.add('real-pad-triggers');
    const brand = pad.querySelector('.pad-brand'); if (brand) {brand.querySelector('span').textContent = 'GAMESIR'; brand.querySelector('small').textContent = '实物转绘 · 保留真实按键布局';}
  }
  canEnterForPrimaryAction(page) {
    const state=this.state;
    return !!MAINTENANCE_MODES[page]&&state.available===true&&state.active===false&&state.pending===false
      &&this.permissions[page]?.can_start===true&&!state.error&&!this.stopped&&!document.hidden
      &&!this.options?.replay&&!this.options?.offline&&performance.now()-this.receivedAt<1800
      &&this.snapshot?.connection?.active===true&&this.snapshot?.connection?.connected===true
      &&!this.workspaceModeInFlight()&&!this.workspaceModeReason(page);
  }
  async requestPrimaryMaintenanceAction(page,action) {
    if(!({numbering:['scan'],hardware:['imu-start','mic-start','duck-play']}[page]||[]).includes(action))return false;
    const expected=MAINTENANCE_MODES[page],matches=()=>this.state.mode===expected||page==='hardware'&&this.state.mode==='hardware_test';
    if(this.state.available===true&&this.state.active===true&&this.state.owned===true&&matches())return this.command(action);
    if(!this.canEnterForPrimaryAction(page)){this.actionFeedback(action,maintenanceReason(this.state)||'请先点击左侧模式检查当前状态。',true);return false;}
    const context=this.workspaceModeContext();
    const entered=await this.requestWorkspaceMode(page);
    if(!entered||!this.workspaceModeContextMatches(context)||document.hidden||this.options?.offline||this.options?.replay
      ||this.state.available!==true||this.state.active!==true||this.state.owned!==true||!matches()
      ||performance.now()-this.receivedAt>=1800)return false;
    return this.command(action);
  }
  bind() {
    document.querySelectorAll('[data-console-page]').forEach(button => button.onclick = () => this.requestWorkspaceMode({hardware:'hardware',numbering:'numbering',calibration:'commissioning',robot:'motion'}[button.dataset.consolePage]));
    $('task-stop-service').onclick = () => this.command('stop-service');
    $('numbering-scan').onclick = () => this.requestPrimaryMaintenanceAction('numbering','scan');
    $('numbering-scan-stop').onclick = () => this.stopScan();
    $('numbering-center').onclick = () => this.requestCenterCalibration();
    $('numbering-write').onclick = () => {const old=this.currentServoId,target=this.rowTargets.get(old);if(!Number.isInteger(old)||!this.numberingTargetAvailable){this.actionFeedback('write','请完成扫描并在所选行选择目标关节编号。',true);return;}return this.command('write',{old_id:old,new_id:target,selected_servo_confirmed:true,...(this.numberingSwapTarget?{swap:true}:{})});};
    for (const [id, action] of [['board-imu-start','imu-start'],['board-imu-stop','imu-stop'],['board-mic-start','mic-start'],['board-mic-stop','mic-stop'],['board-duck-play','duck-play'],['board-duck-stop','duck-stop']]) $(id).onclick = () => {
      if(action==='imu-start'&&this.imuStreamBlocked&&this.shouldStreamImu()){this.imuStreamBlocked=false;this.syncImuStream();this.paint();return;}
      return ['imu-start','mic-start','duck-play'].includes(action)?this.requestPrimaryMaintenanceAction('hardware',action):this.command(action);
    };
  }
  open(page, {automatic = false,entered = false} = {}) {
    if (!PAGE_KEYS.includes(page) || !this.permissions[page].browsable) {if (!automatic) this.feedback(this.permissions[page]?.reason || '当前状态不能进入此页'); return false;}
    if (this.page === page) {if(entered||!automatic){this.openFirmwarePower(page);if(page==='numbering'){this.positionStreamBlocked=false;this.positionStreamError='';this.syncPositionStream();}}return true;}
    this.controls?.suspend(); this.page = page; storage.set('microduck-v261-page', page);
    for (const key of PAGE_KEYS) $(`page-${key}`).hidden = key !== page;
    this.empty.hidden = true;
    if (page === 'calibration') {append($('calibration-grid'), this.twin); append($('calibration-shared'), this.motors, this.jointChart);}
    else {if (this.robotHero && this.twin) this.robotHero.prepend(this.twin); append($('robot-shared'), this.motors); append(this.robotCurveHost, this.jointChart);}
    this.paintWorkspaceModes();
    $('task-title').textContent = META[page].title; $('task-breadcrumb').textContent = META[page].title; if($('task-subtitle'))$('task-subtitle').textContent=META[page].subtitle;
    this.openFirmwarePower(page);
    if(page==='numbering'&&(entered||!automatic)){this.positionStreamBlocked=false;this.positionStreamError='';}
    this.syncPositionStream();
    this.feedback('');
    requestAnimationFrame(() => window.dispatchEvent(new Event('resize'))); return true;
  }
  openFirmwarePower(page){
    if(!this.sharedDetails)return;
    this.sharedDetails.open=true;
  }
  feedback(message, error = false) {const el = $('task-feedback');el.hidden=true;el.textContent=message;el.classList.toggle('error',error);this.workspaceModeFeedback(message,error?'error':'running');}
  actionFeedback(action,message,error=false,phase=error?'error':'completed'){
    const numberingLabel=({scan:'扫描','scan-stop':'停止扫描',write:'保存','center-calibrate':'中位','verify-chain':'核验','position-stream':'位置'})[action];
    const id=numberingLabel?'numbering-feedback':({'imu-start':'board-imu-feedback','imu-stop':'board-imu-feedback','mic-start':'board-mic-feedback','mic-stop':'board-mic-feedback','duck-play':'board-speaker-feedback','duck-stop':'board-speaker-feedback'})[action];
    if(!id){this.feedback(message,error);return;}
    if(numberingLabel)message=numberingLabel+' · '+message;
    this.localActionFeedback??={};
    this.localActionFeedback[id]={message,phase,action,...(action==='scan'?{session:this.state.session_id,scanGeneration:this.state.scan?.generation}:{})};
    const el=$(id);if(el){el.hidden=!message;el.textContent=message;el.dataset.actionState=phase;}
  }
  paintScanFeedback(force=false){
    const scan=this.state.scan||{},last=this.localActionFeedback?.['numbering-feedback'];
    if(!force&&(this.busy||scan.running===true||last?.action!=='scan'||last.phase!=='running'||last.session!==this.state.session_id
      ||!Number.isSafeInteger(scan.generation)||last.scanGeneration!==scan.generation))return;
    const found=Number.isInteger(scan.found)&&scan.found>=0?scan.found:this.state.devices?.length||0;
    if(scan.running===true)this.actionFeedback('scan','正在扫描 ID 1–250，可随时停止。',false,'running');
    else if(scan.state==='error'||messageText(scan.error))this.actionFeedback('scan',messageText(scan.error)||'扫描未完成，请检查设备状态。',true,'error');
    else if(scan.cancelled===true||scan.state==='cancelled')this.actionFeedback('scan',`扫描已停止，已发现 ${found} 个设备。`,false,'stopped');
    else if(scan.completed===true&&scan.state==='completed')this.actionFeedback('scan',`扫描已完成，发现 ${found} 个设备。`,false,'completed');
    else if(force)this.actionFeedback('scan','扫描状态尚未确认，请检查实际反馈。',true,'error');
  }
  selectNumberingRow(id){
    if(this.selectedCenterId!==id&&!this.centerPending){
      if(this.localActionFeedback)delete this.localActionFeedback['numbering-feedback'];const el=$('numbering-feedback');if(el){el.hidden=true;el.textContent='';delete el.dataset.actionState;}
    }
    if(this.selectedCenterId!==id)this.numberingSelectionEpoch=(this.numberingSelectionEpoch||0)+1;this.selectedCenterId=id;
  }
  paintLocalActionFeedback(){
    for(const [id,value] of Object.entries(this.localActionFeedback||{})){
      if(id==='board-imu-feedback'&&value.phase==='completed'&&value.action==='imu-start'&&this.state.imu_running===true&&!this.imuStreamBlocked)continue;
      if(id==='board-mic-feedback'&&value.phase==='completed'&&(this.state.audio?.microphone?.error||this.state.audio?.error))continue;
      if(id==='board-speaker-feedback'&&value.phase==='completed'&&this.state.audio?.speaker?.error)continue;
      const el=$(id);if(el){el.hidden=!value.message;el.textContent=value.message;el.dataset.actionState=value.phase;}
    }
  }
  async post(action, params = {}, timeout = 25000) {
    const password = $('service-sudo-password')?.value;
    const response = await controlFetch('/api/maintenance', {method:'POST', headers:{'Content-Type':'application/json','X-Control-Token':getControlToken(this.cfg)}, body:JSON.stringify({action,...params,...(password ? {sudo_password:password} : {})}), signal:AbortSignal.timeout(timeout)});
    const data = await response.json();
    const serviceStopped = action === 'stop-service' && response.ok && data.accepted === true;
    const preflight = action === 'preflight' && response.ok && data.accepted === true;
    if (!response.ok || data.accepted === false || data.error && !serviceStopped && !preflight){const error=Error(messageText(data.error)||data.reason||'操作未完成');error.responseReceived=true;error.status=response.status;error.reply=data;throw error;}
    return data;
  }
  acceptReply(data) {this.stateEpoch++; if (data.state) this.accept(data.state); else if (data.allowed_pages) this.accept(data);}
  async command(action, params = {}) {
    if(action==='center-calibrate')return this.calibrateSelectedServo(params.servo_id);
    if(action==='write'&&params.swap===true)return this.swapNumberingIds(params);
    const sessionEpoch=this.sessionEpoch,sourceSession=this.state.session_id,sourceGeneration=this.state.scan?.generation;
    if (this.lifecycleResuming || this.centerPending || this.busy || this.scanStopPending) return;
    if (this.state.scan?.running === true && action !== 'end') {this.actionFeedback(action,'扫描进行中，请先停止扫描。',true);return;}
    if(action==='write'&&(!this.numberingTargetAvailable||this.numberingSwapTarget||params.old_id!==this.currentServoId||params.new_id!==this.rowTargets.get(params.old_id)||params.selected_servo_confirmed!==true)){this.actionFeedback(action,'请完成扫描，选中舵机并选择目标关节编号。',true);return;}
    if(action==='duck-play'){this.speakerPlaybackEpoch=(this.speakerPlaybackEpoch||0)+1;this.speakerMeterSuppressed=true;this.speakerMeterRun=null;this.speakerMeterDisplay=null;}
    if(action==='duck-stop'){this.speakerMeterSuppressed=true;this.speakerMeterRun=null;this.speakerMeterDisplay=null;}
    this.stopPositionStream();
    if(['scan','write','begin','end'].includes(action)){this.positionStreamBlocked=false;this.positionStreamError='';}
    this.workspaceModeErrorGeneration=(this.workspaceModeErrorGeneration||0)+1;
    this.workspaceModeRequestToken={};
    if(this.workspaceModeAwaiting?.commandCompleted)this.workspaceModeAwaiting=null;
    this.controls?.suspend();
    if (action === 'scan') {this.selectedCenterId=null;this.rowTargets.clear();}
    if (['scan','write'].includes(action)) this.usedScanGeneration = this.state.scan?.generation;
    if (['scan','write','begin','end'].includes(action)) {this.currentServoId = null; this.scannedIds = [];}
    this.busy = true; this.actionFeedback(action,'正在执行，请等待实际回执…',false,'running');this.paint();
    try {
      const data = await this.post(action, params, action === 'scan' ? 45000 : 25000);
      const changingSession=['begin','end'].includes(action),replySession=data.state?.session_id??data.session_id??data.result?.session_id;
      if(sessionEpoch!==this.sessionEpoch)return;
      if(!changingSession&&sourceSession&&(sourceSession!==this.state.session_id||replySession&&replySession!==sourceSession)){this.actionFeedback(action,'连接上下文已改变，请核对原目标的实际回执；操作未自动重发。',true);return;}
      const selectionUnchanged=sourceGeneration===this.state.scan?.generation&&(action!=='write'||this.selectedCenterId===params.old_id)&&(action!=='write'||this.rowTargets.get(params.old_id)===params.new_id);
      const serviceStopped = action === 'stop-service' && data.accepted === true;
      if(action==='duck-play')this.speakerMeterSuppressed=false;
      this.acceptReply(data);
      if(['imu-start','imu-stop','begin','end'].includes(action))this.imuStreamBlocked=false;
      const message = (serviceStopped ? messageText(data.result?.message) : '') || data.receipt?.message || data.result?.receipt?.message || data.message || data.result?.message || (action === 'write' ? '已读回新 ID；请断电重连，复查编号保持。' : action === 'end' ? '维护已结束，串口已释放；机器人服务保持停止。' : '操作已完成，状态以实际回读为准。');
      if(action==='scan')this.paintScanFeedback(true);else this.actionFeedback(action,message);
      if (action === 'verify-chain') {const chain=data.chain || data.result?.chain || this.state.chain; this.feedback(chain?.numbering_verified ? '完整编号与 IMU 核验通过，可以进入标定模式。' : `编号核验未完成：缺少 ID ${(chain?.missing_ids||[]).join('、') || '无'}${chain?.imu_present ? '' : '；IMU 未就绪'}`);}
      if (action === 'begin' || action === 'end') {this.usedScanGeneration = null; this.currentServoId = null; this.scannedIds = [];}
      if (action === 'begin') this.open(params.mode === 'numbering' ? 'numbering' : 'hardware', {automatic:true});
      const writeReceipt=data.receipt||data.result?.receipt||data.state?.receipt;
      if(action==='write'){
        const verified=writeReceipt?.verified===true&&!writeReceipt.error&&!writeReceipt.uncertain&&writeReceipt.old_id===params.old_id&&writeReceipt.new_id===params.new_id;
        if(verified){
          this.rowTargets.delete(params.old_id);
          const saved=this.state.devices?.find(row=>row.id===params.new_id&&row.kind==='servo'&&row.identity_verified===true&&row.register_id===params.new_id&&!row.error);
          if(saved){if(this.usedScanGeneration===sourceGeneration)this.usedScanGeneration=null;if(selectionUnchanged)this.selectedCenterId=params.new_id;}
          const label=this.cfg.labels[this.cfg.ids.indexOf(params.new_id)]||'对应关节';
          this.actionFeedback('write',`已保存编号 ID ${params.new_id} · ${label}`+(saved?'':'；列表尚未确认，可手动扫描刷新。'));
        }else this.actionFeedback('write','编号保存尚未确认，请手动扫描核对；操作未自动重发。',true);
      }
      await this.refresh();
    } catch (error) {
      if(sessionEpoch===this.sessionEpoch)this.actionFeedback(action,error.name === 'TimeoutError' ? '未收到操作回执，请检查实际状态；写入不会自动重发。' : error.message,true);
    }
    finally {if(sessionEpoch===this.sessionEpoch){this.busy=false;this.paint();}}
  }
  async swapNumberingIds(params){
    if(this.workspaceModeInFlight()||this.stopped||document.hidden||this.options?.offline||this.options?.replay)return;
    const old=params.old_id,target=params.new_id;
    if(!this.numberingTargetAvailable||!this.numberingSwapTarget||old!==this.currentServoId||target!==this.rowTargets.get(old)
      ||params.selected_servo_confirmed!==true||params.swap!==true||performance.now()-this.receivedAt>=1800
      ||this.cfg.connection_ui&&(this.snapshot?.connection?.active!==true||this.snapshot?.connection?.connected!==true)){
      this.actionFeedback('write','请完成扫描，选中舵机并选择需要互换的关节编号。',true);return;
    }
    const positionKey=numberingPositionKey(numberingPositionContext(this.state)),ids=[old,target];
    const operation={ids,old,target,positionKey,context:this.workspaceModeContext(),session:this.state.session_id,generation:this.state.scan.generation,
      revisions:Object.fromEntries(ids.map(id=>[id,numberingPositionRevision(this.state,id,this.currentPositionRevisionFloors(positionKey))]))};
    if(!positionKey||ids.some(id=>!Number.isSafeInteger(operation.revisions[id])))return;
    this.swapPending=operation;this.swapPositionAwaiting=operation;this.stateEpoch++;this.busy=true;
    this.actionFeedback('write',`正在互换 ID ${old} 和 ID ${target}，请等待两颗舵机的实际回执…`,false,'running');this.paint();
    const current=()=>this.swapPending===operation&&this.workspaceModeContextMatches(operation.context)
      &&this.state.session_id===operation.session&&this.state.scan?.generation===operation.generation;
    try{
      const data=await this.post('write',params,45000);if(!current())return;
      const reply=data.state||(data.allowed_pages?data:null),receipt=data.receipt||data.result?.receipt||reply?.receipt;
      const goodRow=id=>{
        const rows=Array.isArray(reply?.devices)?reply.devices.filter(row=>row.id===id):[],row=rows[0];
        return rows.length===1&&row.kind==='servo'&&row.identity_verified===true&&row.register_id===id&&!row.error&&!row.suspected_duplicate
          &&!row.position_error&&Number.isInteger(row.position_raw)&&row.position_raw>=0&&row.position_raw<=65535
          &&!reply.scan?.conflict_ids?.includes(id)?row:null;
      };
      const rows=ids.map(goodRow),revisions=Object.fromEntries(ids.map(id=>[id,numberingPositionRevision(reply,id)]));
      const verified=reply?.available===true&&reply.active===true&&reply.owned===true&&reply.mode==='numbering'
        &&reply.session_id===operation.session&&reply.scan?.generation===operation.generation&&reply.scan?.completed===true
        &&reply.scan.running===false&&reply.scan.requires_rescan!==true&&numberingPositionKey(numberingPositionContext(reply))===positionKey
        &&receipt?.verified===true&&receipt.swapped===true&&!receipt.error&&!receipt.uncertain
        &&receipt.old_id===old&&receipt.new_id===target&&receipt.target_old_id===target&&receipt.target_new_id===old
        &&rows.every(Boolean)&&ids.every(id=>Number.isSafeInteger(revisions[id])&&revisions[id]>operation.revisions[id]);
      if(!verified)throw Error(messageText(receipt?.error)||'两颗舵机的互换回执尚未完整确认，请手动扫描核对；操作未自动重发。');
      const floors=this.currentPositionRevisionFloors(positionKey);for(const id of ids)floors[id]=Math.max(floors[id]||0,revisions[id]);
      const selected=this.selectedCenterId===old&&this.rowTargets.get(old)===target;
      const devices=this.state.devices.map(row=>ids.includes(row.id)?{...goodRow(row.id)}:row);
      this.state={...this.state,...reply,devices,scan:this.state.scan};this.stateEpoch++;this.receivedAt=performance.now();this.permissions=normalizePermissions(this.state);
      this.swapPositionAwaiting=null;this.rowTargets.delete(old);this.rowTargets.delete(target);if(selected)this.selectedCenterId=target;
      this.actionFeedback('write',`已互换编号：ID ${old} → ${target}，ID ${target} → ${old}。`+(Array.isArray(receipt.needs_recalibration_ids)&&ids.every(id=>receipt.needs_recalibration_ids.includes(id))?'这两个关节需重新标定。':''));
    }catch(error){
      if(current()){
        const failed=error.reply?.state||(error.reply?.allowed_pages?error.reply:null),receipt=error.reply?.receipt||error.reply?.result?.receipt||failed?.receipt;
        if(failed?.session_id===operation.session&&failed.mode==='numbering'&&failed.active===true&&failed.owned===true
          &&failed.scan?.generation===operation.generation&&receipt?.verified===false&&receipt.swapped===false
          &&receipt.old_id===old&&receipt.new_id===target)this.acceptReply(error.reply);
        this.usedScanGeneration=operation.generation;this.positionStreamBlocked=true;this.stopPositionStream();
        this.actionFeedback('write',error.name==='TimeoutError'?'未收到互换回执，请手动扫描核对两颗舵机及临时编号；操作不会自动重发。':messageText(receipt?.message)||error.message,true);
      }
    }finally{
      if(this.swapPending===operation){this.swapPending=null;this.busy=false;this.paint();}
    }
  }
  centerReceiptVerified(receipt,id=receipt?.servo_id){
    return Number.isInteger(id)&&id!==200&&receipt?.servo_id===id&&receipt.verified===true&&!receipt.error&&!receipt.uncertain
      &&receipt.after_position_raw===2048&&receipt.relocked_after_position_raw===2048&&receipt.relocked===true&&receipt.torque_off===true;
  }
  centerReceiptMessage(receipt){
    if(!receipt)return '等待实际中位校准回执。';
    const id=Number.isInteger(receipt.servo_id)?`ID ${receipt.servo_id} · `:'';
    if(this.centerReceiptVerified(receipt))return id+(receipt.unchanged?'原本即为 2048，已核验。':'当前位置已设为 2048，已读回核验。')+(receipt.needs_recalibration?'对应关节需重新标定。':'')+(receipt.needs_power_cycle_verification?'请断电重连复查保持。':'');
    return id+'中位校准未确认成功：'+(messageText(receipt.error)||'回读条件尚未全部满足')+'。请重新检测后核对实际位置。';
  }
  async stopScan() {
    if (this.scanStopPending || this.options?.replay || this.state.active !== true || this.state.owned !== true || this.state.mode !== 'numbering' || this.state.scan?.running !== true) return;
    const epoch=this.sessionEpoch;this.scanStopPending = true; this.paint();
    this.actionFeedback('scan-stop','正在停止扫描…',false,'running');
    try {const data = await this.post('scan-stop');if(epoch!==this.sessionEpoch)return;this.acceptReply(data);this.actionFeedback('scan-stop',data.message || data.result?.message || '已请求停止扫描，保留已经发现的设备。');await this.refresh();}
    catch(error){if(epoch===this.sessionEpoch)this.actionFeedback('scan-stop',error.message,true);}
    finally{if(epoch===this.sessionEpoch){this.scanStopPending=false;this.paint();}}
  }
  resetSession(reason='中控会话已更新，请重新点击菜单检查后进入。') {
    this.page=null;this.snapshot=null;this.workspaceSnapshotAt=-Infinity;
    this.stopPositionStream({forget:true});this.positionStreamBlocked=true;this.positionStreamError='';
    this.workspaceModeRequestToken={};this.busy=false;this.entryPending=false;
    this.centerPending=null;this.centerPositionAwaiting=null;this.centerReadback=null;this.positionRevisionFloors=null;this.swapPending=null;this.swapPositionAwaiting=null;
    this.speakerMeterRun=null;this.speakerMeterDisplay=null;this.speakerMeterSuppressed=false;
    this.localActionFeedback={};this.rowTargets.clear();
    this.imuStreamBlocked=true;
    if(this.workspaceModeAwaiting){this.workspaceModeAwaiting=null;this.busy=false;this.workspaceModeFeedback('连接会话已改变，请核对实际模式；不会自动重发。','error');}
    this.workspaceModeLastCommand=null;
    this.scanFence = null; this.usedScanGeneration = null; this.scanStopPending = false; this.sessionEpoch++; this.requestAbort?.abort(); this.stopImuStream(); this.currentServoId = null; this.scannedIds = [];this.selectedCenterId=null; 
    this.state = {available:false,active:false,owned:false,error:reason}; this.permissions = normalizePermissions(this.state); this.receivedAt = -Infinity; this.paint(); this.refresh();
  }
  accept(state) {
    const scan=state.scan,session=messageText(state.session_id),owned=state.active===true&&state.owned===true&&state.mode==='numbering';
    if (!owned || !session || this.scanFence&&this.scanFence.session!==session) {this.scanFence=null;this.usedScanGeneration=null;}
    if (owned && session && scan && Number.isSafeInteger(scan.generation)) {
      const old=this.scanFence,same=old?.session===session,terminal=scan.running!==true&&['completed','cancelled','error'].includes(scan.state);
      if(same&&(scan.generation<old.generation||scan.generation===old.generation&&(Number(scan.scanned)<old.scanned||old.terminal&&scan.running===true)))state={...state,scan:this.state.scan,devices:this.state.devices,numbered_ids:this.state.numbered_ids,receipt:this.state.receipt,center_receipt:this.state.center_receipt};
      else {if(!same)this.usedScanGeneration=null;this.scanFence={session,generation:scan.generation,scanned:Number(scan.scanned)||0,terminal};}
    }
    this.state = state;this.receivedAt = performance.now(); this.permissions = normalizePermissions(state); this.paint();}
  maintenancePollInterval(){
    const state=this.state;
    return this.page==='numbering'&&state.available===true&&state.active===true&&state.owned===true&&state.mode==='numbering'&&messageText(state.session_id)&&!state.error&&!this.options?.replay&&!this.options?.offline&&!document.hidden?(this.hasFreshNumberingPositions()?500:100):500;
  }
  pollMaintenance(now=performance.now()){
    this.paintSpeakerLevel(now);
    if(this.positionStreamExpected)this.queuePositionPaint();
    if(this.lifecycleResuming||this.centerPending||this.busy||this.scanStopPending||this.entryPending)return;
    if(now-(this.maintenancePollAt??-Infinity)<this.maintenancePollInterval())return;
    this.maintenancePollAt=now;return this.refresh();
  }
  refresh() {
    if(this.centerPending||this.stopped)return;
    if(this.polling)return this.maintenanceReadPromise;
    this.polling=true;
    const epoch=this.sessionEpoch,stateEpoch=this.stateEpoch,readToken={};this.maintenanceReadToken=readToken;
    const abort=new AbortController();this.requestAbort=abort;const timeout=setTimeout(()=>abort.abort(),3500);
    const task=(async()=>{
      try{const response=await fetch('/api/maintenance',{signal:abort.signal});if(!response.ok)throw Error(`HTTP ${response.status}`);const data=await response.json();if(epoch===this.sessionEpoch&&stateEpoch===this.stateEpoch)this.accept(data);}
      catch{if(epoch!==this.sessionEpoch||stateEpoch!==this.stateEpoch)return;this.state={available:false,error:'维护连接未就绪'};this.permissions=normalizePermissions(this.state);this.receivedAt=-Infinity;this.paint();}
      finally{clearTimeout(timeout);if(this.maintenanceReadToken===readToken)this.polling=false;}
    })();
    this.maintenanceReadPromise=task;
    void task.finally(()=>{if(this.maintenanceReadPromise===task)this.maintenanceReadPromise=null;}).catch(()=>{});
    return task;
  }
  paintNumberingScan({local=false}={}) {
    const state=this.state.active===true&&this.state.owned===true&&this.state.mode==='numbering'&&messageText(this.state.session_id)?this.state:{},scan=state.scan||{},rows=this.currentNumberingPositionRows(),total=250,scanned=Math.max(0,Math.min(total,Number(scan.scanned)||0));
    const servo=row=>Number.isInteger(row.id)&&row.id!==200&&row.kind==='servo'&&row.identity_verified===true&&row.register_id===row.id&&!row.error;
    const conflicts=Array.isArray(scan.conflict_ids)?scan.conflict_ids:[],rowSafe=row=>!row.suspected_duplicate&&!conflicts.includes(row.id);
    const complete=scan.state==='completed'&&scan.completed===true&&scan.running!==true&&scan.requires_rescan!==true;
    const chainSafe=conflicts.length===0&&rows.every(row=>(servo(row)||row.id===200&&row.kind==='imu'&&row.identity_verified===true&&!row.error)&&rowSafe(row));
    const draftContext=messageText(state.session_id)&&Number.isSafeInteger(scan.generation)?`${state.session_id}:${scan.generation}`:null;
    if(this.numberingDraftContext!==draftContext){this.numberingDraftContext=draftContext;this.rowTargets.clear();this.selectedCenterId=null;}
    const occupied=new Set([...rows.map(row=>row.id),...conflicts]);
    const targetSafe=(id,target)=>NUMBERING_JOINT_IDS.has(target)&&target!==id&&!conflicts.includes(target)&&(id!==1||!occupied.has(target))
      &&(!occupied.has(target)||rows.some(row=>row.id===target&&servo(row)&&rowSafe(row)));
    for(const [id,target] of this.rowTargets){if(!rows.some(row=>row.id===id&&servo(row)&&rowSafe(row))||!targetSafe(id,target))this.rowTargets.delete(id);}
    const selected=rows.find(row=>row.id===this.selectedCenterId&&servo(row)&&rowSafe(row));
    if(!selected)this.selectedCenterId=null;
    const usable=complete&&chainSafe&&scan.generation!==this.usedScanGeneration&&this.options?.offline!==true;
    const locked=this.lifecycleResuming||this.busy||this.scanStopPending||this.options?.replay===true||this.options?.offline===true;
    this.currentServoId=usable&&selected?selected.id:null;this.currentCenterServoId=complete&&selected&&scan.generation!==this.usedScanGeneration&&!this.options?.offline?selected.id:null;
    const target=selected?this.rowTargets.get(selected.id):null;
    this.numberingTargetAvailable=Number.isInteger(this.currentServoId)&&targetSafe(this.currentServoId,target);
    this.numberingSwapTarget=this.numberingTargetAvailable&&this.currentServoId!==1&&occupied.has(target);
    $('numbering-current').textContent=selected?String(selected.id):'—';$('numbering-selected-target').textContent=Number.isInteger(target)?`ID ${target}`:'未选择目标';
    const readiness=scan.running?'正在扫描，完成后可选择舵机。':!complete?'请先完成扫描，再保存编号或校准中位。':!chainSafe?'存在设备身份或通信异常，请按列表提示检查。':scan.generation===this.usedScanGeneration?'请重新扫描确认设备状态。':!selected?'请选择一行舵机；更改该行下拉框也会选中它。':!this.numberingTargetAvailable?'请选择所选舵机的目标关节编号；中位校准不需更改编号。':this.numberingSwapTarget?`保存将互换 ID ${selected.id} 和 ID ${target}，其他舵机不改号。`:`保存仅把 ID ${selected.id} 改为 ID ${target}，其他行不会保存。`;
    const center=state.center_receipt,selectedReceipt=center?.servo_id===selected?.id?center:null;
    $('numbering-center-current').textContent=numberingPositionText(selected);$('numbering-center-current').title=messageText(selected?.position_error)||messageText(selected?.position_note)||'位置自动更新';
    $('numbering-center-before').textContent=Number.isInteger(selectedReceipt?.before_position_raw)?String(selectedReceipt.before_position_raw):'—';$('numbering-center-after').textContent=Number.isInteger(selectedReceipt?.after_position_raw)?String(selectedReceipt.after_position_raw):'—';
    $('numbering-write').title=readiness;$('numbering-write').textContent='保存';$('numbering-center').title=Number.isInteger(this.currentCenterServoId)?'把所选舵机当前位置设为 2048':'请先完成扫描并选择舵机';$('numbering-actions-footer').hidden=!Number.isInteger(selectedReceipt?.before_position_raw)&&!Number.isInteger(selectedReceipt?.after_position_raw);
    $('numbering-center').disabled=!Number.isInteger(this.currentCenterServoId)||locked||!!this.centerPending;
    this.numberingSelectionAllowed=complete&&!locked;
    if(local){
      for(const [id,item] of this.numberingPositionNodes||[]){
        const chosen=this.selectedCenterId===id;item.setAttribute('aria-selected',String(chosen));item.classList.toggle('selected',chosen);
        const radio=item.querySelector('input[type=radio]'),select=item.querySelector('select');
        if(radio){radio.checked=chosen;radio.disabled=!this.numberingSelectionAllowed;}
        if(select){select.disabled=!this.numberingSelectionAllowed;const target=this.rowTargets.has(id)?String(this.rowTargets.get(id)):'';if(select.value!==target)select.value=target;}
      }
      return;
    }
    this.scannedIds=rows.map(row=>row.id).filter(Number.isInteger);
    $('numbering-scan-progress').value=scanned;$('numbering-scan-count').textContent=`已扫 ${scanned} / ${total}`;$('numbering-scan-current').textContent=`当前 ID ${Number.isInteger(scan.current_id)?scan.current_id:'—'}`;$('numbering-scan-found').textContent=`发现设备 ${Number.isInteger(scan.found)?scan.found:rows.length}`;
    $('numbering-scan-status').textContent=scan.running?'扫描中':scan.cancelled||scan.state==='cancelled'?'已停止':scan.completed||scan.state==='completed'?'已完成':scan.state==='error'?'未完成':'尚未扫描';
    this.paintScanFeedback();
    const identityRows=rows.map(row=>({id:row.id,kind:row.kind,register_id:row.register_id,identity_verified:row.identity_verified,error:row.error,suspected_duplicate:row.suspected_duplicate,conflict_reasons:row.conflict_reasons,model_name:row.model_name,model_number:row.model_number,model:row.model}));
    const list=$('numbering-device-rows'),rowsKey=JSON.stringify([identityRows,conflicts]);
    $('numbering-devices-empty').hidden=rows.length>0;
    $('numbering-devices-empty').textContent=scan.running?'正在扫描，尚未发现设备。':scan.cancelled||scan.state==='cancelled'?'扫描已停止，尚未发现设备。':scan.completed||scan.state==='completed'?'本次扫描未发现设备。':'尚未发现设备。';
    if(rowsKey!==this.scanRowsKey){this.scanRowsKey=rowsKey;list.replaceChildren(...rows.map(row=>{
      const item=document.createElement('tr'),strictServo=servo(row),isServo=Number.isInteger(row.id)&&row.id!==200&&row.kind==='servo'&&row.register_id===row.id,isImu=row.id===200&&row.kind==='imu',mapped=isServo&&NUMBERING_JOINT_IDS.has(row.id),index=this.cfg.ids.indexOf(row.id);
      item.className='numbering-device';item.classList.toggle('selected',this.selectedCenterId===row.id);item.setAttribute('aria-selected',String(this.selectedCenterId===row.id));item.dataset.deviceId=row.id;item.dataset.deviceKind=isServo?'servo':isImu?'imu':'unknown';
      const id=document.createElement('td'),idLabel=document.createElement('strong');idLabel.className='device-id-label';idLabel.textContent=String(row.id);id.append(idLabel);
      const selectRow=()=>{if(!this.numberingSelectionAllowed||!strictServo||!rowSafe(row))return;this.selectNumberingRow(row.id);this.paint();};
      if(strictServo&&rowSafe(row)){
        const choose=document.createElement('input');choose.type='radio';choose.name='numbering-servo-source';choose.checked=this.selectedCenterId===row.id;choose.disabled=!complete||locked;choose.setAttribute('aria-label',`选择 ID ${row.id} 进行操作`);choose.onchange=selectRow;id.prepend(choose);
        item.onclick=event=>{if(event.target.closest('select,option,input,button'))return;selectRow();};item.tabIndex=0;item.onkeydown=event=>{if(event.target===item&&['Enter',' '].includes(event.key)){event.preventDefault();selectRow();}};
      }
      const position=document.createElement('td');position.className='device-position';position.textContent=numberingPositionText(row);position.title=messageText(row.position_error)||messageText(row.position_note)||'位置自动更新';
      const joint=document.createElement('td');joint.textContent=mapped?this.cfg.labels[index]||this.cfg.names?.[index]||'关节名称待提供':isServo?'—':isImu?'IMU':'待确认';
      const numbering=document.createElement('td'),badge=document.createElement('span');badge.className='badge '+(mapped?'':'neutral');badge.textContent=isServo?mapped?'已编号':'未编号':isImu?'IMU':'身份待确认';numbering.append(badge);
      const targetCell=document.createElement('td');targetCell.className='device-target';
      if(strictServo&&rowSafe(row)){
        const select=document.createElement('select');select.className='numbering-row-target';select.dataset.servoId=String(row.id);select.setAttribute('aria-label',`为 ID ${row.id} 选择目标关节编号`);select.disabled=!complete||locked;
        const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='选择目标关节编号';placeholder.disabled=true;select.append(placeholder);
        this.cfg.ids.forEach(ident=>{const option=document.createElement('option');option.value=String(ident);option.disabled=!targetSafe(row.id,ident);option.textContent=`${ident} · ${this.cfg.labels[this.cfg.ids.indexOf(ident)]||'关节'}`;select.append(option);});
        select.value=this.rowTargets.has(row.id)?String(this.rowTargets.get(row.id)):'';
        select.onchange=()=>{if(select.disabled)return;const value=Number(select.value);if(!targetSafe(row.id,value))return;this.rowTargets.set(row.id,value);this.selectNumberingRow(row.id);if(!this.centerPending){if(this.localActionFeedback)delete this.localActionFeedback['numbering-feedback'];$('numbering-feedback').textContent='';$('numbering-feedback').hidden=true;}this.paint();};targetCell.append(select);
      }else targetCell.textContent='—';
      const communication=document.createElement('td');const conflict=row.suspected_duplicate===true||conflicts.includes(row.id);communication.textContent=conflict?`疑似 ID ${row.id} 重复 / 通信冲突`:row.error?'读取异常':strictServo||isImu&&row.identity_verified===true?'身份已确认':isServo||isImu?'身份读取未完全确认':'已响应，待确认';
      if(conflict){communication.dataset.conflict='true';const flag=document.createElement('span');flag.className='badge warn';flag.textContent='疑似冲突';communication.prepend(flag);const detail=document.createElement('small');detail.textContent=(Array.isArray(row.conflict_reasons)?row.conflict_reasons.map(reason=>({multi:'同轮收到多个回应',crc:'校验异常',identity:'身份不一致',reply:'回应异常'}[reason]||'回应异常')).join('；'):'')||messageText(row.error)||`请只保留一颗 ID ${row.id} 核对`;detail.textContent+='。请只保留一颗该 ID 核对。';communication.append(detail);}else if(row.error){const detail=document.createElement('small');detail.textContent=messageText(row.error)||'身份读取未确认';communication.append(detail);}
      const model=row.model_name??row.model_number??row.model;if(typeof model==='string'||typeof model==='number'){const detail=document.createElement('small');detail.textContent=`型号 ${model}`;joint.append(detail);}
      item.append(id,position,joint,numbering,targetCell,communication);return item;
    }));}
    const renderedRows=new Map([...list.children].map(node=>[Number(node.dataset.deviceId),node]));
    this.numberingPositionNodes=renderedRows;
    for(const row of rows){
      const item=renderedRows.get(row.id),cell=item?.querySelector('.device-position');if(cell){cell.textContent=numberingPositionText(row);cell.title=messageText(row.position_error)||messageText(row.position_note)||'位置自动更新';}
      item?.setAttribute('aria-selected',String(this.selectedCenterId===row.id));item?.classList.toggle('selected',this.selectedCenterId===row.id);
      const radio=item?.querySelector('input[type=radio]'),select=item?.querySelector('select');
      if(radio){radio.checked=this.selectedCenterId===row.id;radio.disabled=!this.numberingSelectionAllowed;}
      if(select){select.disabled=!this.numberingSelectionAllowed;const target=this.rowTargets.has(row.id)?String(this.rowTargets.get(row.id)):'';if(select.value!==target)select.value=target;}
    }
    this.sizeNumberingCards();
  }
  sizeNumberingCards(){
    if(this.stopped||this.numberingLayoutRaf!=null)return;
    this.numberingLayoutRaf=requestAnimationFrame(()=>{
      this.numberingLayoutRaf=null;
      const grid=$('page-numbering')?.querySelector('.numbering-grid'),card=$('numbering-scan-card'),list=card?.querySelector('.numbering-devices');
      if(!grid||!card||!list||!card.getBoundingClientRect().width)return;
      const rows=[...$('numbering-device-rows').children].slice(0,16),body=card.querySelector('.task-card-body');
      const cardRect=card.getBoundingClientRect(),scale=card.offsetWidth>0?cardRect.width/card.offsetWidth:1;
      const table=card.querySelector('table'),spacing=parseFloat(getComputedStyle(table).borderSpacing)||5;
      const header=table.querySelector('thead').getBoundingClientRect().height/scale||26;
      const rowTotal=rows.reduce((sum,row)=>sum+Math.max(42,row.getBoundingClientRect().height/scale),0)+(16-rows.length)*42;
      const horizontalScrollbar=Math.max(0,list.offsetHeight-list.clientHeight);
      const footer=$('numbering-actions-footer'),footerHeight=footer?footer.getBoundingClientRect().height/scale:0,footerGap=footer?Math.max(0,(footer.getBoundingClientRect().top-list.getBoundingClientRect().bottom)/scale):0;
      const required=(list.getBoundingClientRect().top-cardRect.top)/scale+body.scrollTop+header+rowTotal+17*spacing+(parseFloat(getComputedStyle(body).paddingBottom)||18)+horizontalScrollbar+footerHeight+footerGap+3;
      const height=Math.ceil(Math.max(1180,required));
      if(this.numberingCardHeight!==height){this.numberingCardHeight=height;grid.style.setProperty('--numbering-card-height',height+'px');}
    });
  }
  shouldStreamPositions() {
    const context=numberingPositionContext(this.state),s=this.snapshot;
    return !!context&&!this.stopped&&!this.options?.replay&&!this.options?.offline&&this.page==='numbering'
      &&(!this.busy||!!this.swapPending)&&!this.scanStopPending&&!this.entryPending&&!this.state.error
      &&!this.servicePanel?.posting&&!this.servicePanel?.centerConfirmation
      &&!s?.service_control?.busy&&!s?.service_control?.stopping&&s?.service_control?.job?.status!=='running'
      &&(!this.cfg?.connection_ui||s?.connection?.active===true&&s?.connection?.connected===true)
      &&(!this.connectionTarget||context.target===this.connectionTarget);
  }
  syncPositionStream() {
    const key=numberingPositionKey(numberingPositionContext(this.state));
    if(key!==this.positionContextKey){this.stopPositionStream({forget:true});this.positionContextKey=key;this.positionStreamBlocked=false;this.positionStreamError='';}
    if(!this.shouldStreamPositions()){if(this.positionAbort||this.positionStreamExpected)this.stopPositionStream();return;}
    if(this.state.position_stream.state==='error'){
      if(!this.positionStreamBlocked)this.failPositionStream(messageText(this.state.position_stream.error)||'实时位置通道已停止');
      return;
    }
    if(!this.positionAbort&&!this.positionStreamBlocked)this.startPositionStream();
  }
  stopPositionStream({forget=false}={}) {
    this.positionStreamEpoch=(this.positionStreamEpoch||0)+1;this.positionAbort?.abort();this.positionAbort=null;
    if(this.positionRaf!=null)window.cancelAnimationFrame(this.positionRaf);this.positionRaf=null;
    this.positionStreamExpected=false;this.positionFrame=null;this.positionReceivedAt=-Infinity;this.positionLastSequence=null;
    if(forget)this.positionContextKey=null;
  }
  failPositionStream(message) {
    const detail=messageText(message)||'实时位置通道中断';
    this.positionStreamBlocked=true;this.positionStreamError=/扫描|重新进入编号/.test(detail)?detail:detail+'；位置读取已停止，请手动扫描或重新进入编号模式。';
    this.stopPositionStream();this.actionFeedback('position-stream',this.positionStreamError,true);this.queuePositionPaint();
  }
  async startPositionStream() {
    const context=numberingPositionContext(this.state),key=numberingPositionKey(context);
    if(!key||!this.shouldStreamPositions())return;
    const epoch=++this.positionStreamEpoch,abort=new AbortController();this.positionAbort=abort;
    this.positionContextKey=key;this.positionStreamExpected=true;this.positionFrame=null;this.positionReceivedAt=-Infinity;this.positionLastSequence=null;this.queuePositionPaint();
    try{
      const response=await controlFetch('/api/maintenance/positions/events',{headers:{Accept:'text/event-stream','X-Control-Token':getControlToken(this.cfg)},signal:abort.signal});
      if(abort.signal.aborted||epoch!==this.positionStreamEpoch)return;
      if(!response.ok)throw Error('实时位置通道未就绪');
      await consumeMaintenanceEvents(response,{signal:abort.signal,eventType:'positions',label:'舵机位置',onFrame:frame=>{
        if(epoch===this.positionStreamEpoch&&key===this.positionContextKey)this.acceptPositionFrame(frame);
      }});
      if(!abort.signal.aborted&&epoch===this.positionStreamEpoch)throw Error('实时位置通道已断开');
    }catch(error){
      if(!abort.signal.aborted&&epoch===this.positionStreamEpoch){this.failPositionStream(error.message);if(error.sessionChanged)this.feedback(error.message,true);}
    }finally{
      if(epoch===this.positionStreamEpoch)this.positionAbort=null;
    }
  }
  acceptPositionFrame(frame) {
    if(!this.shouldStreamPositions()||!this.positionStreamExpected)return;
    const value=normalizeNumberingPositionFrame(this.state,frame);
    if(!value||value.key!==this.positionContextKey)return;
    if(this.positionLastSequence!=null&&(value.sequence<this.positionLastSequence
      ||value.sequence===this.positionLastSequence&&value.state==='live'&&this.positionFrame?.state==='live'))return;
    this.positionLastSequence=value.sequence;
    if(value.state==='error'||value.state==='stopped'){this.failPositionStream(value.error||'实时位置通道已停止');return;}
    this.positionFrame=value;this.positionReceivedAt=performance.now();this.positionStreamError='';this.queuePositionPaint();
  }
  hasFreshNumberingPositions(now=performance.now()) {
    return this.positionStreamExpected&&this.positionFrame?.key===numberingPositionKey(numberingPositionContext(this.state))
      &&numberingPositionFresh(this.positionFrame,this.positionReceivedAt,now);
  }
  currentNumberingPositionRows(now=performance.now()) {
    const state=this.state;
    if(state?.active!==true||state.owned!==true||state.mode!=='numbering'||!messageText(state.session_id))return [];
    const key=numberingPositionKey(numberingPositionContext(state)),eligible=this.shouldStreamPositions(),floors=this.currentPositionRevisionFloors(key);
    const swapWaiting=this.swapPositionAwaiting;
    if(swapWaiting){
      if(swapWaiting.positionKey!==key||!this.workspaceModeContextMatches(swapWaiting.context)||state.session_id!==swapWaiting.session||state.scan?.generation!==swapWaiting.generation)this.swapPositionAwaiting=null;
      else if(!this.swapPending&&eligible&&this.hasFreshNumberingPositions(now)){
        const fresh=swapWaiting.ids.every(id=>{const observed=this.positionFrame.positions.get(id);return Number.isSafeInteger(observed?.position_revision)&&observed.position_revision>swapWaiting.revisions[id]&&observed.position_pending!==true;});
        if(fresh){for(const id of swapWaiting.ids)floors[id]=Math.max(floors[id]||0,this.positionFrame.positions.get(id).position_revision);this.swapPositionAwaiting=null;}
      }
    }
    const waiting=this.centerPositionAwaiting;
    if(waiting){
      if(waiting.positionKey!==key||!this.workspaceModeContextMatches(waiting.context)||state.session_id!==waiting.session||state.scan?.generation!==waiting.generation)this.centerPositionAwaiting=null;
      else {
        const declared=numberingPositionRevision(state,waiting.id),observed=eligible&&this.hasFreshNumberingPositions(now)?this.positionFrame.positions.get(waiting.id):null;
        const newerDeclared=Number.isSafeInteger(declared)&&declared>waiting.positionRevision;
        const newerObserved=Number.isSafeInteger(observed?.position_revision)&&observed.position_revision>waiting.positionRevision;
        if(newerDeclared||newerObserved){
          floors[waiting.id]=Math.max(floors[waiting.id]||0,newerDeclared?declared:0,newerObserved?observed.position_revision:0);
          if(newerDeclared||observed.position_pending!==true)this.centerPositionAwaiting=null;
        }
      }
    }
    let rows=numberingPositionRows(state,eligible?this.positionFrame:null,this.positionReceivedAt,now,
      {expected:!eligible||!!key,error:this.positionStreamError,revisionFloors:floors});
    const swap=this.swapPending||this.swapPositionAwaiting;
    if(swap&&swap.positionKey===key&&this.workspaceModeContextMatches(swap.context)&&state.session_id===swap.session&&state.scan?.generation===swap.generation)
      rows=rows.map(row=>swap.ids.includes(row.id)&&!row.error?{...row,position_raw:null,position_error:'',position_pending:true,position_note:'等待互换编号后的实际位置反馈'}:row);
    const pending=this.centerPending,awaiting=this.centerPositionAwaiting;
    if(pending&&this.centerContextMatches(pending)&&pending.positionKey===key)rows=rows.map(row=>row.id===pending.id&&!row.error
      ?{...row,position_raw:null,position_error:'',position_pending:true,position_note:'当前舵机中位校准中'}:row);
    else if(awaiting&&eligible)rows=rows.map(row=>row.id===awaiting.id&&!row.error&&!row.position_error
      ?{...row,position_raw:null,position_pending:true,position_note:'等待当前舵机的实际位置反馈'}:row);
    const ack=this.centerReadback,selected=ack&&rows.find(row=>row.id===ack.id);
    if(ack){
      const revision=numberingPositionRevision(state,ack.id,floors);
      const freshTarget=this.hasFreshNumberingPositions(now)&&selected?.position_revision===ack.position_revision
        &&Number.isInteger(selected.position_raw)&&!selected.position_error&&!selected.position_pending;
      if(!eligible||ack.key!==key||revision!==ack.position_revision||now<ack.receivedAt||now-ack.receivedAt>150
        ||this.positionStreamError||selected?.error||selected?.position_error||selected?.position_revision>ack.position_revision||freshTarget)this.centerReadback=null;
      else if(!pending)rows=rows.map(row=>row.id===ack.id?{...row,position_raw:ack.position_raw,position_revision:ack.position_revision,
        position_pending:false,position_note:'中位校准实际回读，等待实时位置'}:row);
    }
    return rows;
  }
  queuePositionPaint() {
    if(this.stopped||this.positionRaf!=null)return;
    this.positionRaf=requestAnimationFrame(()=>{this.positionRaf=null;if(this.page==='numbering')this.paintNumberingPositions();});
  }
  paintNumberingPositions() {
    const rows=this.currentNumberingPositionRows();
    for(const row of rows){
      const cell=this.numberingPositionNodes?.get(row.id)?.querySelector('.device-position');
      if(!cell)continue;
      const value=numberingPositionText(row),title=messageText(row.position_error)||messageText(row.position_note)||'位置自动更新';
      if(cell.textContent!==value)cell.textContent=value;if(cell.title!==title)cell.title=title;
      
    }
    const present=new Set(rows.map(row=>row.id));
    for(const [id,item] of this.numberingPositionNodes||[]){
      if(present.has(id))continue;const cell=item.querySelector('.device-position');if(!cell)continue;
      if(cell.textContent!=='待读取')cell.textContent='待读取';if(cell.title!=='实时位置不可用')cell.title='实时位置不可用';
    }
    const selected=rows.find(row=>row.id===this.selectedCenterId),cell=$('numbering-center-current');
    if(cell){const value=numberingPositionText(selected),title=messageText(selected?.position_error)||messageText(selected?.position_note)||'位置自动更新';if(cell.textContent!==value)cell.textContent=value;if(cell.title!==title)cell.title=title;}
  }
  paintSpeakerLevel(now=performance.now()) {
    const state=this.state,audio=state.audio||{},speaker=audio.speaker||audio.duck||{};
    const connected=!this.cfg.connection_ui||this.snapshot?.connection?.active===true&&this.snapshot?.connection?.connected===true;
    let valid=this.page==='hardware'&&!this.options?.replay&&!this.options?.offline&&connected&&!this.stopped&&!this.speakerMeterSuppressed
      &&state.available===true&&state.active===true&&state.owned===true&&['hardware-test','hardware_test'].includes(state.mode)
      &&finite(now-this.receivedAt)&&now>=this.receivedAt&&now-this.receivedAt<1800&&!speaker.error&&!audio.error;
    const context=JSON.stringify([this.sessionEpoch,this.connectionTarget??this.cfg.ssh_target??'',state.session_id]);
    const sequence=Number.isSafeInteger(speaker.playback_sequence)&&speaker.playback_sequence>0?speaker.playback_sequence:null;
    if(valid&&sequence!==null){
      if(this.speakerMeterSequenceContext!==context){this.speakerMeterSequenceContext=context;this.speakerMeterSequence=sequence;}
      else if(sequence<this.speakerMeterSequence)valid=false;
      else this.speakerMeterSequence=sequence;
    }
    const playing=speaker.state==='playing'||speaker.playing===true||audio.duck_playing===true,completed=speaker.state==='completed';
    const key=JSON.stringify([context,this.speakerPlaybackEpoch||0,sequence]);
    let run=this.speakerMeterRun,level=null,notice='';
    if(valid&&run&&run.key!==key){this.speakerMeterRun=run=null;this.speakerMeterDisplay=null;}
    if(!valid){
      // Like the AI monitor, a missing fresh frame clears the display. It does
      // not permanently suppress later valid feedback for this playback.
      this.speakerMeterDisplay=null;
    }else if(!playing&&!completed){this.speakerMeterRun=null;this.speakerMeterDisplay=null;}
    else {
      let sample=state,sampleAt=this.receivedAt,metadata=speaker;
      // Compatibility with an older component whose completed receipt omitted
      // the envelope, when a real playing receipt was already observed.
      if(completed&&!playbackEnvelopeValid(metadata)&&run?.key===key){sample=run.state;sampleAt=run.receivedAt;metadata=sample.audio?.speaker||sample.audio?.duck||{};}
      if(!playbackEnvelopeValid(metadata)){
        this.speakerMeterDisplay=null;
        notice='音频测试组件未提供播放电平，请重新进入测试模式加载随包组件。';
      }else {
        if(!run)run=this.speakerMeterRun={key,elapsed:0,anchorAt:now,anchorElapsed:speaker.elapsed_ms,completedConsumed:false};
        if(!run.completedConsumed){
          const end=completed?metadata.duration_ms:Math.min(metadata.duration_ms,Math.max(run.elapsed,run.anchorElapsed+now-run.anchorAt,speaker.elapsed_ms+now-this.receivedAt));
          if(end>run.elapsed){level=speakerPlaybackLevel(sample,sampleAt,now,{from:run.elapsed,to:end});run.elapsed=end;}
          else if(playing&&!this.speakerMeterDisplay)level=speakerPlaybackLevel(sample,sampleAt,now);
          if(finite(level))this.speakerMeterDisplay={key,level,until:now+300};
          if(completed)run.completedConsumed=true;
        }
        run.state=sample;run.receivedAt=sampleAt;
        if(!finite(level)&&this.speakerMeterDisplay?.key===key&&now<this.speakerMeterDisplay.until)level=this.speakerMeterDisplay.level;
      }
    }
    const meter=$('board-speaker-meter'),label=$('board-speaker-level');if(!meter||!label)return;
    label.textContent=finite(level)?`${fmt(level,1)} dBFS · 峰值`:'— dBFS';
    meter.setAttribute('aria-valuenow',String(finite(level)?level:-96));
    meter.setAttribute('aria-valuetext',finite(level)?`鸭叫文件播放区间峰值 ${fmt(level,1)} dBFS`:'没有可用播放电平');
    meter.title='鸭叫文件播放区间峰值；不代表扬声器实际声压';
    const bars=audioLevelBars(level,meter.children.length,finite(level));
    [...meter.children].forEach((bar,i)=>bar.classList.toggle('on',i<bars));
    const feedback=$('board-speaker-feedback');
    if(feedback){
      if(notice)feedback.textContent=notice;
      else if(this.speakerMeterNotice&&feedback.textContent===this.speakerMeterNotice)feedback.textContent=this.localActionFeedback?.['board-speaker-feedback']?.message||speaker.message||'播放后，听声音是否正常。';
    }
    this.speakerMeterNotice=notice;
  }
  shouldStreamImu() {
    return !this.stopped && !this.options?.replay && !this.options?.offline && this.page === 'hardware'
      && this.state.active === true && this.state.owned === true && ['hardware-test','hardware_test'].includes(this.state.mode) && this.state.imu_running === true;
  }
  syncImuStream() {
    if (!this.shouldStreamImu()) {if (this.imuAbort || this.imuStreamExpected) this.stopImuStream(); return;}
    if (!this.imuAbort && !this.imuStreamBlocked) this.startImuStream();
  }
  stopImuStream() {
    this.imuStreamEpoch++; this.imuAbort?.abort(); this.imuAbort = null;
    clearInterval(this.imuWatchdog); this.imuWatchdog = null;
    if (this.imuRaf != null) window.cancelAnimationFrame(this.imuRaf); this.imuRaf = null;
    this.imuStreamExpected = false; this.imuFrame = null; this.imuReceivedAt = -Infinity; this.imuLastSampleId = null; this.imuGeneration = null;
  }
  async startImuStream() {
    const epoch = ++this.imuStreamEpoch, abort = new AbortController(); this.imuAbort = abort;
    this.imuStreamExpected = true; this.imuStreamError = ''; this.imuFrame = null; this.imuReceivedAt = -Infinity;
    this.imuWatchdog = setInterval(() => this.queueImuPaint(), 100); this.queueImuPaint();
    try {
      const response = await controlFetch('/api/maintenance/imu/events', {headers:{'Accept':'text/event-stream','X-Control-Token':getControlToken(this.cfg)},signal:abort.signal});
      if (!response.ok) throw Error('IMU 实时流连接未就绪');
      await consumeImuEvents(response, {signal:abort.signal,onFrame:frame => {if (epoch === this.imuStreamEpoch) this.acceptImuFrame(frame);}});
      if (!abort.signal.aborted && epoch === this.imuStreamEpoch) throw Error('IMU 实时流已断开，请点击“重新读取”。');
    } catch (error) {
      if (!abort.signal.aborted && epoch === this.imuStreamEpoch) {this.imuStreamBlocked=true;this.imuFrame = null; this.imuReceivedAt = -Infinity; this.imuStreamError = error.message+' 读取已停止，请点击“重新读取”。';this.actionFeedback('imu-start',this.imuStreamError,true);this.queueImuPaint();if(error.sessionChanged)this.feedback(error.message,true);}
    } finally {
      if (epoch === this.imuStreamEpoch) {this.imuAbort = null;clearInterval(this.imuWatchdog);this.imuWatchdog = null;this.paint();}
    }
  }
  acceptImuFrame(frame) {
    if (!this.shouldStreamImu() || !frame || typeof frame !== 'object') return;
    if (frame.active !== true || frame.owned !== true || !['hardware-test','hardware_test'].includes(frame.mode) || frame.imu_running !== true) {
      for (const key of ['active','owned','mode','imu_running']) if (Object.hasOwn(frame,key)) this.state[key] = frame[key];
      this.stopImuStream(); this.paint(); return;
    }
    const generation = frame.stream?.generation;
    if (Number.isSafeInteger(generation) && generation !== this.imuGeneration) {this.imuGeneration = generation; this.imuLastSampleId = null;}
    const imu = frame.imu || {}, sample = imu.sample || imu, sampleId = imu.sample_id ?? sample.sample_id;
    if (sample.status === 'live' && (!Number.isSafeInteger(sampleId) || sampleId < 0)) {this.imuStreamError = 'IMU 实时样本缺少有效序号'; this.imuFrame = null; this.queueImuPaint(); return;}
    if (Number.isSafeInteger(sampleId) && this.imuLastSampleId != null && sampleId < this.imuLastSampleId) return;
    if (Number.isSafeInteger(sampleId) && sampleId === this.imuLastSampleId && sample.status === 'live' && (this.imuFrame?.imu?.sample || this.imuFrame?.imu)?.status === 'live') return;
    this.imuLastSampleId = Number.isSafeInteger(sampleId) ? sampleId : this.imuLastSampleId;
    this.imuFrame = frame; this.imuReceivedAt = performance.now(); this.imuStreamError = ''; this.queueImuPaint();
  }
  queueImuPaint() {
    if (this.imuRaf != null) return;
    this.imuRaf = requestAnimationFrame(() => {this.imuRaf = null; if (this.page === 'hardware') this.paintImuReadback();});
  }
  currentTestImu(now = performance.now()) {
    if (this.options?.replay) return {state:{...this.state,imu:{status:'waiting',ready:false,reason:'历史回放中，不连接 IMU 实时流'}},receivedAt:-Infinity};
    if (this.options?.offline) return {state:{...this.state,imu:{status:'waiting',ready:false,reason:'等待中控连接恢复'}},receivedAt:-Infinity};
    if(this.imuStreamBlocked)return {state:{...this.state,imu:{status:'error',ready:false,error:this.imuStreamError||'IMU 实时流读取已停止，请点击“重新读取”。'}},receivedAt:-Infinity};
    if (!this.imuStreamExpected || !this.shouldStreamImu()) return {state:this.state,receivedAt:this.receivedAt};
    if (!this.imuFrame || now-this.imuReceivedAt > 500) return {state:{...this.state,imu:{status:'waiting',ready:false,reason:this.imuStreamError || '等待 IMU 实时流的有效样本'}},receivedAt:-Infinity};
    const imu = this.imuFrame.imu || {}, value = imu.sample || imu, elapsed = Math.max(0,now-this.imuReceivedAt);
    const sample = {...value,sample_age_ms:(finite(value.sample_age_ms) ? value.sample_age_ms : 0)+elapsed};
    return {state:{...this.state,imu:imu.sample ? {...imu,sample} : sample},receivedAt:this.imuReceivedAt};
  }
  paintImuReadback() {
    const hardware = this.state.active === true && this.state.owned === true && ['hardware-test','hardware_test'].includes(this.state.mode);
    const current = this.currentTestImu(), liveImu = current.state.imu || {}, sample = liveImu.sample || liveImu, test = normalizeTestImu(current.state,current.receivedAt);
    $('board-imu-sequence').textContent = `样本 ${test.status === 'live' ? sample.sequence ?? '—' : '—'}`;
    $('board-imu-age').textContent = `新鲜度 ${test.status === 'live' && finite(sample.sample_age_ms ?? sample.age_ms) ? fmt(sample.sample_age_ms ?? sample.age_ms,0)+' ms' : '—'}`;
    $('board-imu-rate').textContent = `IMU 读取 ${test.status === 'live' ? fmt(liveImu.poll_hz ?? sample.poll_hz ?? liveImu.refresh_hz ?? sample.refresh_hz,1)+' Hz' : '— Hz'}`;
    $('board-imu-rate').title = `目标读取 ${fmt(liveImu.target_hz ?? sample.target_hz,0)} Hz` ;
    $('board-imu-stream-rate').textContent = `数据流 ${test.status === 'live' ? fmt(this.imuFrame?.stream?.refresh_hz,1)+' Hz' : '— Hz'}`; $('board-imu-stream-rate').title = `目标数据流 ${fmt(this.imuFrame?.stream?.target_hz,0)} Hz`;
    $('board-imu-errors').textContent = `通信错误 ${hardware && Number.isInteger(liveImu.communication_errors ?? sample.communication_errors) ? liveImu.communication_errors ?? sample.communication_errors : '—'}`;
    $('board-imu-feedback').textContent = test.status==='live' ? '通信正常 · 融合就绪 · 请转动 IMU 检查姿态随动。' : sample.error || sample.reason || '尚未开始单 IMU 测试。';
    if (this.page !== 'hardware' || this.options?.replay) return;
    const live = test.status === 'live', vector = value => Array.isArray(value) ? value.map(n => fmt(n,3)).join(' / ') : '未导出';
    $('imu-roll').textContent = `${fmt(live ? test.roll_deg : null)}°`; $('imu-pitch').textContent = `${fmt(live ? test.pitch_deg : null)}°`;
    $('imu-ready').textContent = live && test.ready ? '已收敛' : '未收敛'; $('imu-stale').textContent = fmt(test.consecutive_stale_blocks,0);
    $('imu-gravity').textContent = live ? vector(test.gravity) : '— / — / —'; $('imu-gyro').textContent = live ? vector(test.gyro) : '无有效样本'; $('imu-quat').textContent = live ? vector(test.quat) : '无有效样本';
    $('imu-badge').className = 'badge ' + (live ? '' : test.status === 'error' ? 'bad' : 'neutral'); $('imu-badge').textContent = live ? test.gyro ? '含原始遥测' : '重力方向' : test.status === 'error' ? '连接异常' : '等待'; $('imu-badge').title = test.reason || test.source;
    $('horizon').setAttribute('transform',`rotate(${live ? -test.roll_deg : 0} 85 85) translate(0 ${live ? Math.max(-45,Math.min(45,test.pitch_deg)) : 0})`); $('horizon').style.opacity = live ? 1 : .25;
  }
  withMaintenance(s, {replay = this.options?.replay === true} = {}) {
    if (this.page !== 'hardware' || replay) return s;
    const current = this.currentTestImu(); return {...s, imu:normalizeTestImu(current.state,current.receivedAt)};
  }
  render(s, options = {}) {
    const target=s.connection?.target??this.cfg.ssh_target??'',disconnected=this.connectionActive===true&&s.connection?.active===false;
    if(this.connectionTarget!==undefined&&this.connectionTarget!==target||disconnected)this.resetSession('连接目标或连接状态已改变，请重新点击菜单检查后进入。');
    this.connectionTarget=target;this.connectionActive=s.connection?.active;this.snapshot = s;this.workspaceSnapshotAt=performance.now();this.workspaceSnapshotEpoch=(this.workspaceSnapshotEpoch||0)+1; this.options = options; this.paint();}
  paint({suspendInvalid = true, route = true} = {}) {
    if (!this.pages) return;
    if(this.centerPending){this.paintCenterOperation();return;}
    this.paintWorkspaceModes();
    const state = this.state, active = state.active === true, own = active && state.owned === true, number = own && state.mode === 'numbering', hardware = own && ['hardware-test','hardware_test'].includes(state.mode);
    const scanning = number && state.scan?.running === true;
    const locked = this.lifecycleResuming || this.busy || this.scanStopPending || this.options?.replay === true;
    const routingConnected=!!this.snapshot&&!this.stopped&&!this.options?.replay&&!this.options?.offline
      &&(!this.cfg.connection_ui||this.snapshot.connection?.active===true&&this.snapshot.connection?.connected===true);
    if (!this.page) {
      if (!this.entryPending && suspendInvalid) this.controls?.suspend();
      for (const key of PAGE_KEYS) $(`page-${key}`).hidden = true;
      this.empty.hidden = false;
      for(const id of ['task-title','task-breadcrumb']){const el=$(id);if(el&&el.textContent!=='连接与准备')el.textContent='连接与准备';}
      if($('task-subtitle'))$('task-subtitle').textContent='';
      $('task-empty-message').textContent=routingConnected?'SSH 已连接，正在确认机器人状态；也可从左侧选择模式。':'先连接 SSH，连接成功后会根据机器人状态进入对应页面。';
    }
    else if (!this.permissions[this.page].allowed && !this.entryPending && suspendInvalid) this.controls?.suspend();
    const actual=this.actualWorkspaceMode(),preferred={hardware:'hardware',numbering:'numbering',commissioning:'calibration',motion:'robot'}[actual];
    if (route && routingConnected && !this.entryPending && preferred && preferred !== this.page) this.open(preferred, {automatic:true});
    else if (route && routingConnected && !this.entryPending && !this.page && performance.now()-this.receivedAt<1800) {const initial=this.state.initial_page;if(this.permissions[initial]?.browsable)this.open(initial,{automatic:true});}
    else if (this.page) {this.empty.hidden = true; $(`page-${this.page}`).hidden = false;}
    $('task-stop-service').disabled = state.service_stop_available !== true || locked;
    if (active) for (const id of ['management-reconnect','firmware-flash']) if ($(id)) $(id).disabled=true;
    const enterNumber=this.canEnterForPrimaryAction('numbering'),enterHardware=this.canEnterForPrimaryAction('hardware');
    $('numbering-scan').disabled = !(number||enterNumber) || locked || scanning;$('numbering-scan').textContent=enterNumber?'进入并扫描':'扫描';
    $('numbering-scan-stop').disabled = !scanning || this.scanStopPending || this.options?.replay === true;
    this.paintNumberingScan();
    $('numbering-write').disabled = !number || locked || scanning || !Number.isInteger(this.currentServoId) || !this.numberingTargetAvailable;
    const chain = state.chain || {};
    $('numbering-badge').textContent = number ? '编号模式' : '未开始';
    const imu = state.imu || {}, audio = state.audio || {}, mic = audio.microphone || audio.mic || {}, duck = audio.speaker || audio.duck || {};
    const imuRunning = hardware && (state.imu_running === true || imu.running === true || imu.active === true), micRunning = hardware && (['starting','running'].includes(mic.state) || mic.running === true || audio.mic_running === true), playing = hardware && (duck.state === 'playing' || duck.playing === true || audio.duck_playing === true);
    $('board-imu-start').disabled = !(hardware||enterHardware) || locked || imuRunning&&!this.imuStreamBlocked; $('board-imu-start').textContent=enterHardware?'进入并读取':this.imuStreamBlocked?'重新读取':'开始读取';$('board-imu-stop').disabled = !hardware || locked || !imuRunning;
    $('board-mic-start').disabled = !(hardware||enterHardware) || locked || micRunning;$('board-mic-start').textContent=enterHardware?'进入并收音':'开始收音'; $('board-mic-stop').disabled = !hardware || locked || !micRunning;
    $('board-duck-play').disabled = !(hardware||enterHardware) || locked || playing;$('board-duck-play').textContent=enterHardware?'进入并播放':'播放鸭叫'; $('board-duck-stop').disabled = !hardware || locked || !playing;
    $('board-mic-badge').textContent = micRunning ? '收音中' : '未开始'; $('board-speaker-badge').textContent = playing ? '播放鸭叫' : duck.state==='completed'?'已完成':duck.error||duck.state==='error'?'播放异常':'未播放';
    this.paintImuReadback();
    const level = mic.level_dbfs ?? mic.dbfs ?? audio.level_dbfs, freshMic = micRunning && mic.fresh === true && performance.now()-this.receivedAt < 1800 && finite(level), normalized = freshMic ? Math.max(0,Math.min(1,(level+72)/72)) : 0;
    $('board-mic-level').textContent = freshMic ? `${fmt(level,1)} dBFS` : '— dBFS';
    $('board-mic-meter').setAttribute('aria-valuenow', String(freshMic ? Math.max(-90,Math.min(0,level)) : -90));
    [...$('board-mic-meter').children].forEach((bar,i) => bar.classList.toggle('on', i < normalized*24));
    if (mic.error || audio.error) $('board-mic-feedback').textContent = mic.error || audio.error;
    if (duck.error) $('board-speaker-feedback').textContent = duck.error;
    this.paintLocalActionFeedback();
    this.paintSpeakerLevel();
    this.syncImuStream();
    this.syncPositionStream();
    this.paintWorkspaceModes();
  }
}
