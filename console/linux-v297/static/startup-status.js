import {messageZh,formatError,busResponseProblem} from './messages.js';

function startupProgress(report){
  const pending=Object.entries(report?.steps||{}).find(([,row])=>['starting','waiting','failed'].includes(row?.status));
  if(!pending)return null;
  const detail=String(pending[1].detail||''),issue=busResponseProblem(detail);
  if(issue)return {hidden:false,tone:'warning',message:`启动第 ${pending[0]}/4 步：${issue.message}`,rawError:issue.rawError};
  const message=detail&&/[\u3400-\u9fff]/.test(detail)?detail:'等待关节与 IMU 反馈。';
  return {hidden:false,tone:pending[1].status==='failed'?'warning':'neutral',message:`启动第 ${pending[0]}/4 步：${message}`,
    rawError:pending[1].status!=='starting'&&detail!==message?detail:''};
}

export function startupFailure(s){
  if(s?.connection_paused||s?.connection?.active===false)return '';
  if(s?.channels?.startup?.status==='live'){
    const failed=Object.entries(s.startup?.steps||{}).find(([,row])=>row.status==='failed');
    if(failed)return `启动第 ${failed[0]}/4 步失败：${formatError(failed[1].detail)||'请导出启动诊断'}`;
  }
  const bus=s?.bus||{};
  // A failed startup emits one bus report; the live health heartbeat keeps it
  // authoritative even though there can never be a fresh motion sample.
  if(bus.phase==='configuration_error'&&s.channels?.health?.status==='live')
    return `总线启动失败：${formatError(bus.error||s.health?.reason)||'请导出启动诊断'}`;
  return '';
}

const nativeModes = new Set(['commissioning','motion']);
const benchModes = new Set(['numbering','hardware-test','hardware_test','hardware']);
const pageModes = {numbering:'numbering',hardware:'hardware-test',calibration:'commissioning',robot:'motion'};
const connectionModes = new WeakMap();

/** Mode meaning survives sample loss, but never crosses a connection context. */
export function connectionWorkspaceState(workspace,s,{hidden=false}={}){
  const unknown={workspaceMode:null,transitioning:false};
  if(!workspace)return unknown;
  if(s?.connection_paused||s?.connection?.active===false){connectionModes.delete(workspace);return unknown;}
  if(workspace.stopped||workspace.options?.replay||workspace.options?.offline)return unknown;
  const backend=workspace.cfg?.control_session_id,target=s?.connection?.target??workspace.connectionTarget??'',client=workspace.servicePanel?.client;
  let observed=connectionModes.get(workspace);
  if(!observed||observed.backend!==backend||observed.target!==target||observed.client!==client){observed={backend,target,client,mode:null};connectionModes.set(workspace,observed);}
  const waiting=workspace.workspaceModeAwaiting;
  const accepted=waiting&&nativeModes.has(waiting.target)&&workspace.workspaceModeContextMatches?.(waiting.context)===true?waiting:null;
  const pageMode=pageModes[workspace.page]??null,state=workspace.state||{};
  const ownedBench=benchModes.has(pageMode)&&state.active===true&&state.owned===true&&benchModes.has(state.mode)&&typeof state.session_id==='string'&&state.session_id.trim();
  const mode=ownedBench?pageMode:accepted?.target??pageMode;
  const transitioning=Boolean(workspace.entryPending||!ownedBench&&accepted&&accepted.commandCompleted!==true);
  if(benchModes.has(mode)){observed.mode=mode;return hidden?unknown:{workspaceMode:mode,transitioning};}
  if(!nativeModes.has(mode))return unknown;
  const liveNative=s?.service_sample_fresh===true&&s?.channels?.bus?.status==='live'&&nativeModes.has(s?.bus?.mode);
  const processNative=state.service_process_mode===mode&&workspace.actualWorkspaceMode?.()===mode;
  if(accepted||liveNative||processNative)observed.mode=mode;
  return hidden?unknown:{workspaceMode:nativeModes.has(observed.mode)?mode:null,transitioning};
}

/** Only modes that run robotd require a live robot data connection. */
export function connectionNotice(s,{connectionUI=false,maintenance=null,maintenanceFresh=true,workspaceMode=null,transitioning=false}={}){
  const quiet={hidden:true,tone:'neutral',message:''};
  if(s?.connection_paused||(connectionUI&&s?.connection?.active!==true))return quiet;
  if(!nativeModes.has(workspaceMode))return quiet;
  const job=s?.service_control?.job;
  if(job?.action==='switch-mode'&&job.status==='running'&&job.command?.target===workspaceMode){
    const progress=startupProgress(job.startup);if(progress)return progress;
  }
  if(transitioning)return quiet;
  const channels=s?.channels||{},service=s?.mode==='service';
  const channel=channels[service?'bus':'state']||{};
  const failed=startupFailure(s);
  if(failed)return {hidden:false,tone:'warning',message:failed};
  const componentError=row=>row?.status==='error'&&!['ssh','local'].includes(row.error_source)?row.error:'';
  const error=componentError(channel)||componentError(channels.state)||(service&&channels.bus?.status==='live'?(s.bus?.error||s.bus?.last_cycle_error):'');
  if(error)return {hidden:false,tone:'warning',message:`机器人服务连接异常：${messageZh(error)}`};
  const stopped=maintenanceFresh&&maintenance?.available===true&&maintenance.robotd_running===false&&maintenance.pending!==true&&!maintenance.error;
  if(stopped)return {hidden:false,tone:'warning',message:'机器人总线意外停止；请检查机器人服务与实际运行状态。'};
  if(s?.robotd_manual_stop===true)return {hidden:false,tone:'warning',message:'当前模式的机器人数据采集已暂停；请恢复连接。'};
  if(channels.startup?.status==='live'){
    const pending=startupProgress(s.startup);if(pending)return pending;
  }
  const health=s?.health||{};
  if(channels.health?.status==='live'&&health.healthy===false&&health.reason&&health.reason!=='control loop has not completed a cycle yet')
    return {hidden:false,tone:'warning',message:`机器人服务报告异常：${messageZh(health.reason)}`};
  if(service?s?.service_sample_fresh===true:channel.status==='live')return quiet;
  if(channel.status==='stale'||channels.state?.status==='stale')
    return {hidden:false,tone:'warning',message:'机器人状态已停止更新，当前为旧反馈；等待实时反馈恢复。'};
  if(connectionUI&&s?.connection?.connected!==true)return quiet;
  return {hidden:false,tone:'neutral',message:'机器人服务尚未返回首次完整反馈。'};
}
