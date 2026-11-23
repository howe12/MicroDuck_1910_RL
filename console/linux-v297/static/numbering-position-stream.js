// Only the current numbering lease/scan may supply display positions.
// Sequence numbers identify PC bridge snapshots, never UART sensor samples.
export const POSITION_TRANSPORT_FRESH_MS = 150;
const finite = value => typeof value === 'number' && Number.isFinite(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const text = value => typeof value === 'string' ? value : '';

export function numberingPositionContext(state) {
  const meta=state?.position_stream,scan=state?.scan,session=text(state?.session_id);
  if(state?.available!==true||state.active!==true||state.owned!==true||state.mode!=='numbering'||!session
    ||scan?.state!=='completed'||scan.completed!==true||scan.running!==false||scan.requires_rescan===true
    ||!integer(scan.generation)||!meta||!integer(meta.generation)||!integer(meta.revision)
    ||meta.session_id!==session||meta.scan_generation!==scan.generation||!text(meta.target))return null;
  return {target:meta.target,session_id:session,scan_generation:scan.generation,generation:meta.generation,revision:meta.revision};
}

export function numberingPositionKey(context) {
  return context ? JSON.stringify([context.target,context.session_id,context.scan_generation,context.generation,context.revision]) : null;
}

export function normalizeNumberingPositionFrame(state, frame) {
  const context=numberingPositionContext(state),meta=frame?.position_stream;
  if(!context||!meta||frame.active!==true||frame.owned!==true||frame.mode!=='numbering'
    ||frame.session_id!==context.session_id||frame.target!==context.target||frame.scan?.generation!==context.scan_generation
    ||meta.target!==context.target||meta.session_id!==context.session_id||meta.scan_generation!==context.scan_generation
    ||meta.generation!==context.generation||meta.revision!==context.revision||!integer(meta.sequence)
    ||!['waiting','live','stale','error','stopped'].includes(meta.state)||!Array.isArray(frame.positions))return null;
  const positions=new Map();
  for(const row of frame.positions){
    if(!row||!Number.isInteger(row.id)||row.id<1||row.id>250||positions.has(row.id))return null;
    const valid=Number.isInteger(row.position_raw)&&row.position_raw>=0&&row.position_raw<=65535&&(row.position_raw&0x7fff)<=4095;
    const revision=Object.hasOwn(row,'position_revision')?row.position_revision:0,versioned=integer(revision),pending=row.position_pending===true;
    const error=text(row.position_error);
    positions.set(row.id,{position_raw:valid&&versioned&&!error&&!pending?row.position_raw:null,
      position_revision:versioned?revision:null,position_pending:pending,
      position_error:!versioned?'位置反馈版本无效':pending?'':error||(!valid&&row.position_raw!=null?'位置反馈无效':''),
      position_note:pending?error||'等待当前舵机的新位置反馈':''});
  }
  return {key:numberingPositionKey(context),sequence:meta.sequence,state:meta.state,positions,
    transport_age_ms:finite(meta.transport_age_ms)&&meta.transport_age_ms>=0?meta.transport_age_ms:null,
    rpc_ms:finite(meta.rpc_ms)&&meta.rpc_ms>=0?meta.rpc_ms:null,error:text(meta.error)};
}

export function numberingPositionFresh(frame, receivedAt, now) {
  return frame?.state==='live'&&frame.transport_age_ms!==null&&frame.rpc_ms!==null
    &&finite(receivedAt)&&finite(now)&&now>=receivedAt
    &&frame.transport_age_ms+now-receivedAt<=POSITION_TRANSPORT_FRESH_MS
    &&frame.rpc_ms<=POSITION_TRANSPORT_FRESH_MS;
}

export function numberingPositionRevision(state,id,floors={}) {
  const value=state?.position_stream?.id_revisions?.[String(id)],lower=floors[String(id)];
  const declared=value===undefined?0:value,floor=lower===undefined?0:lower;
  return integer(declared)&&integer(floor)?Math.max(declared,floor):null;
}

export function numberingPositionRows(state, frame, receivedAt, now, {expected=false,error='',revisionFloors={}}={}) {
  const rows=Array.isArray(state?.devices)?state.devices:[],key=numberingPositionKey(numberingPositionContext(state));
  if(!expected)return rows;
  const current=frame?.key===key&&numberingPositionFresh(frame,receivedAt,now),conflicts=state.scan?.conflict_ids||[];
  return rows.map(row=>{
    if(row.id===200&&row.kind==='imu')return row;
    if(row.id===200||row.kind!=='servo'||row.identity_verified!==true||row.register_id!==row.id
      ||row.error||row.suspected_duplicate||conflicts.includes(row.id))return {...row,position_raw:null,
      position_error:text(row.error)||(row.suspected_duplicate||conflicts.includes(row.id)?'设备编号存在冲突，位置不可用':'设备身份未确认，位置不可用')};
    const value=current?frame.positions.get(row.id):null,revision=numberingPositionRevision(state,row.id,revisionFloors),matching=revision!==null&&value?.position_revision===revision;
    const result={...row,position_raw:matching?value.position_raw:null,position_revision:value?.position_revision??null,position_pending:matching&&value.position_pending===true};
    delete result.position_error;delete result.position_note;
    const detail=revision===null||value?.position_revision===null?'位置反馈版本无效':current?(matching?value.position_error:!value?text(row.position_error):''):error||frame?.error||text(row.position_error);
    if(detail)result.position_error=detail;
    if(matching&&value.position_note)result.position_note=value.position_note;
    else if(current&&value&&!matching)result.position_note='等待当前舵机的新位置反馈';
    return result;
  });
}
