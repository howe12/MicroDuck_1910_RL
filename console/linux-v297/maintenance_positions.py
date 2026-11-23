"""Numbering feedback reuses the existing worker's cached sync-read results.

The finite remote process only sends tokenless status requests to the worker's
root-owned socket. It never opens a UART or renews its lease. Owner heartbeats
and commands remain in MaintenanceController; stream callbacks never take its
lock or issue another management request.
"""
import collections
import copy
import json
import threading
import time

POSITION_PERIOD = .04
POSITION_LIFETIME = 110.
POSITION_RENEW_AT = 105.
POSITION_STALE_MS = 150.

# This is executed through the existing management channel, not installed on
# ZERO. No owner token enters this read-only bridge.
POSITION_BRIDGE_SOURCE = r'''
import json,socket,sys,time
r=json.loads(sys.stdin.readline())
sid=r.get('session_id')
if set(r)!={'session_id'} or not isinstance(sid,str) or not 0<len(sid)<=128:raise ValueError('invalid position session')
start=time.monotonic();due=start;sequence=0
while time.monotonic()-start<110:
    began=time.monotonic_ns()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(5);s.connect('/run/microduck-maintenance.sock')
        s.sendall(b'{"action":"status"}\n')
        with s.makefile('rb') as f:raw=f.readline(65537)
    finished=time.monotonic_ns()
    if len(raw)>65536 or not raw.endswith(b'\n'):raise ValueError('invalid position status reply')
    v=json.loads(raw)
    if not isinstance(v,dict) or v.get('error'):raise ValueError('position status rejected')
    if v.get('active') is not True or v.get('mode')!='numbering' or v.get('session_id')!=sid:break
    scan=v.get('scan') or {};rows=v.get('devices') or [];positions=[]
    if not isinstance(scan,dict) or not isinstance(rows,list) or len(rows)>254:raise ValueError('invalid position status')
    ready=(scan.get('completed') is True and scan.get('running') is False and scan.get('requires_rescan') is False)
    conflicts=scan.get('conflict_ids') or []
    counts={}
    for row in rows:
        if isinstance(row,dict) and type(row.get('id')) is int:counts[row['id']]=counts.get(row['id'],0)+1
    if ready:
        for row in rows:
            if not isinstance(row,dict):continue
            i=row.get('id')
            if (type(i) is int and 0<=i<254 and i!=200 and counts.get(i)==1 and i not in conflicts
                and row.get('kind')=='servo' and row.get('identity_verified') is True
                and row.get('register_id')==i and not row.get('error') and not row.get('suspected_duplicate')):
                raw=row.get('position_raw');error=row.get('position_error')
                if type(raw) is not int or not 0<=raw<=65535 or raw&0x7fff>4095:
                    raw=None;error=error or '位置读取无效'
                positions.append({'id':i,'position_raw':raw,**({'position_error':str(error)} if error else {})})
    sequence+=1
    print(json.dumps({'kind':'positions','session_id':sid,'scan_generation':scan.get('generation'),
        'ready':ready,'sequence':sequence,'poll_started_ns':began,'poll_finished_ns':finished,
        'positions':positions},ensure_ascii=False,separators=(',',':'),allow_nan=False),flush=True)
    now=time.monotonic();due+=(int(max(0,now-due)/.04)+1)*.04
    time.sleep(max(0,due-time.monotonic()))
print(json.dumps({'kind':'positions_end','reason':'lifetime' if time.monotonic()-start>=109 else 'closed'}),flush=True)
'''

# Same worker.execute result/error contract, with a private read-after-command
# barrier. The PC removes it before remembering or returning the result.
POSITION_CALL_SOURCE = r'''
import json,runpy,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
w=runpy.run_path(sys.argv[1]);request=json.loads(sys.stdin.buffer.readline(8193))
try:answer=w['execute'](request)
except Exception as e:answer={'error':str(e),'code':getattr(e,'code',503),**getattr(e,'detail',{})}
if (isinstance(answer,dict) and answer.get('error') and answer.get('code')==409
        and 'active' not in answer and isinstance(request,dict) and request.get('action')=='center-calibrate'):
    try:answer['_position_failure_state']=w['execute']({'action':'status','token':request.get('token')})
    except Exception:pass
if isinstance(answer,dict):answer['_position_barrier_ns']=time.monotonic_ns()
print(json.dumps(answer,ensure_ascii=False,allow_nan=False),flush=True)
'''


def eligible(meta):
    scan = meta.get('scan') or {}
    if not (meta.get('available') is True and meta.get('active') is True
            and meta.get('owned') is True and meta.get('mode') == 'numbering'
            and isinstance(meta.get('session_id'), str) and meta['session_id']
            and isinstance(scan, dict) and type(scan.get('generation')) is int
            and scan.get('completed') is True and scan.get('running') is False
            and scan.get('requires_rescan') is False and not meta.get('error')):
        return None
    rows = meta.get('devices')
    if not isinstance(rows, list): return None
    counts = collections.Counter(r.get('id') for r in rows if isinstance(r, dict) and type(r.get('id')) is int)
    conflicts = scan.get('conflict_ids') or []
    ids = tuple(sorted(r['id'] for r in rows if isinstance(r, dict)
        and type(r.get('id')) is int and 0 <= r['id'] < 254 and r['id'] != 200
        and counts[r['id']] == 1 and r['id'] not in conflicts
        and r.get('kind') == 'servo' and r.get('identity_verified') is True
        and r.get('register_id') == r['id'] and not r.get('error') and not r.get('suspected_duplicate')))
    return ids or None


class PositionFeedback:
    def __init__(self, core, clock=time.monotonic):
        self.core, self.clock = core, clock
        self.lock = threading.RLock()
        self.generation = self.revision = self.channel_generation = self.sequence = 0
        self.context = self.group = None
        self.meta = {}
        self.ids = ()
        self.scan_generation = None
        self.state, self.error = 'stopped', ''
        self.positions, self.at, self.rpc_ms = [], None, None
        self.barrier_ns = self.watermark_ns = 0
        self.hold = False
        self.failed_revision = None
        self.id_states = {}
        self.lanes = []
        self.times = collections.deque(maxlen=100)

    def invalidate(self):
        with self.lock:
            self.channel_generation += 1
            self.generation += 1
            self.context = self.group = None
            self.meta, self.ids, self.positions = {}, (), []
            self.at, self.rpc_ms = None, None
            self.state, self.error, self.hold = 'stopped', '', False
            self.id_states = {}

    def begin_revision(self):
        with self.lock:
            self.revision += 1
            self.generation += 1
            self.hold = True
            self.positions, self.at = [], None
            self.state, self.error = 'waiting', ''
            self.id_states = {}

    def begin_selected(self, ident):
        """Fence one midpoint write without pausing the other cached positions."""
        with self.lock:
            if ident not in self.ids or self.context is None: return None
            revision = self.id_states.get(ident, {}).get('revision', 0)+1
            self.id_states[ident] = {'revision':revision, 'pending':True,
                'ready':False, 'barrier_ns':None, 'error':''}
            return revision

    def _current(self, group):
        c, ctx = self.core, self.context
        return (ctx is not None and group is self.group and not group['failed']
            and not c.closed and c.token == ctx[3] and c.worker_path == ctx[2]
            and c.observed_transport is ctx[0] and c.observed_target == ctx[1]
            and c._target(ctx[0]) == ctx[1]
            and (c.connection is None or c.connection.active))

    def observe(self, meta, barrier_ns=None, selected_id=None):
        start = None
        with self.lock:
            self.meta = copy.deepcopy(meta)
            ids = eligible(meta)
            if not (meta.get('active') is True and meta.get('owned') is True and meta.get('mode') == 'numbering'):
                if self.context is not None: self.invalidate()
                return
            if not ids:
                self.hold, self.state, self.positions, self.at = True, 'waiting', [], None
                return
            c = self.core
            ctx = (c.observed_transport, c.observed_target, c.worker_path, c.token, meta['session_id'])
            if ctx[0] is None or not ctx[3]: return
            if self.context != ctx:
                self.context = ctx
                self.channel_generation += 1
                self.generation += 1
                self.group = None
                self.failed_revision = None
                self.barrier_ns = self.watermark_ns = 0
                self.positions, self.at = [], None
                self.id_states = {}
            if self.ids != ids or self.scan_generation != meta['scan']['generation']:
                self.generation += 1
                self.positions, self.at = [], None
                self.id_states = {}
            self.ids, self.scan_generation = ids, meta['scan']['generation']
            fresh_barrier = type(barrier_ns) is int and barrier_ns > self.barrier_ns
            selected = selected_id if isinstance(selected_id,tuple) else (selected_id,)
            targeted = [self.id_states[i] for i in selected if i in self.id_states]
            if targeted and type(barrier_ns) is int:
                for state in targeted:state.update(pending=False, ready=False, barrier_ns=barrier_ns, error='')
            elif type(barrier_ns) is int: self.barrier_ns = max(self.barrier_ns, barrier_ns)
            self.hold = False
            if self.failed_revision == self.revision:
                self.state = 'error'
                return
            # A local validation failure does not terminate the finite remote
            # process. Reuse that same session lane only after an explicit new
            # command revision and its fresh remote acknowledgement barrier.
            # Otherwise repeated manual recovery would accumulate old readers.
            if self.group is not None and self.group['failed']:
                candidates = [self.group.get('active'),self.group.get('pending')]
                alive = [lane for lane in candidates if lane is not None
                         and lane['thread'].is_alive() and not lane['expired']]
                if alive and not fresh_barrier:
                    self.state = 'error'
                    self.error = '等待当前编号操作的新鲜回执；未重开位置通道。'
                    return
                if alive:
                    self.group['active'] = alive[0]
                    self.group['pending'] = alive[1] if len(alive)>1 else None
                    self.group['failed'] = False
                    self.group['last_revision'] = self.revision
                    self.state, self.error = 'waiting', ''
                    return
            if self.group is None or self.group['failed']:
                self.group = {'active':None, 'pending':None, 'failed':False,
                              'last_revision':self.revision, 'context':ctx}
                self.state, self.error = 'waiting', ''
                start = self._prepare(self.group)
                self.group['active'] = start
            elif self.at is None: self.state = 'waiting'
        if start is not None: start['thread'].start()

    def _fail(self, group, message):
        group['failed'] = True
        # An explicit command already owns a newer revision when its in-flight
        # work makes the former read-only lane end. That is not a failed retry.
        self.failed_revision = group['last_revision']
        self.positions, self.at = [], None
        self.state = 'error'
        self.error = '编号位置通道中断：'+message+'；未自动重连，请重新扫描或重新进入编号。'

    def _prepare(self, group):
        if len(self.lanes) >= 2:
            self._fail(group, '上一条位置读取尚未结束')
            return None
        lane = {'started':self.clock(), 'expired':False, 'thread':None, 'sequence':0}
        ctx = group['context']

        def output(line):
            try: event = json.loads(line)
            except (TypeError, ValueError): return
            if not isinstance(event, dict): return
            replacement = None
            with self.lock:
                if not self._current(group): return
                if event.get('kind') == 'positions_end':
                    lane['expired'] = event.get('reason') == 'lifetime'
                    return
                if event.get('kind') != 'positions' or self.hold: return
                sequence = event.get('sequence')
                if type(sequence) is not int or sequence <= lane['sequence']: return
                began, ended = event.get('poll_started_ns'), event.get('poll_finished_ns')
                if (type(began) is not int or type(ended) is not int or began <= max(self.barrier_ns,self.watermark_ns)
                        or ended < began or (ended-began)/1e6 > POSITION_STALE_MS): return
                values = event.get('positions')
                if (event.get('session_id') != ctx[4] or event.get('ready') is not True
                        or type(event.get('scan_generation')) is not int
                        or event.get('scan_generation') != self.scan_generation or not isinstance(values,list)):
                    return
                found = [r.get('id') for r in values if isinstance(r,dict)]
                if (len(found) != len(values) or any(type(i) is not int for i in found)
                        or len(set(found)) != len(found) or set(found) != set(self.ids)):
                    self._fail(group, '已扫描舵机身份或列表发生变化')
                    return
                for row in values:
                    raw = row.get('position_raw')
                    if raw is not None and (type(raw) is not int or not 0 <= raw <= 65535 or raw & 0x7fff > 4095):
                        self._fail(group, '收到无效位置值'); return
                if lane is group['pending']:
                    group['active'], group['pending'] = lane, None
                if lane is not group['active']: return
                lane['sequence'] = sequence
                self.watermark_ns, self.rpc_ms = ended, (ended-began)/1e6
                self.sequence += 1
                self.positions, self.at = copy.deepcopy(values), self.clock()
                for ident, state in self.id_states.items():
                    if not state['pending'] and state['barrier_ns'] is not None and began > state['barrier_ns']:
                        state.update(ready=True, error='')
                self.state, self.error = 'live', ''
                self.times.append(self.at)
                group['last_revision'] = self.revision
                if (group['pending'] is None and self.clock()-lane['started'] >= POSITION_RENEW_AT
                        and self.clock()-self.core.last_client_seen <= 10):
                    replacement = self._prepare(group)
                    group['pending'] = replacement
            if replacement is not None: replacement['thread'].start()

        def run():
            try:
                with self.lock:
                    if not self._current(group): return
                ctx[0]._manage(['python3','-u','-c',POSITION_BRIDGE_SOURCE],
                    input_data=json.dumps({'session_id':ctx[4]})+'\n',record_output=False,
                    raw_output_handler=output,progress=lambda _line:None,result_format='text')
                with self.lock:
                    if self._current(group) and lane in (group['active'],group['pending']):
                        if not lane['expired'] or group['pending'] is None:
                            self._fail(group, '只读位置通道提前关闭' if not lane['expired'] else '正常通道接续未完成')
            except Exception as problem:
                with self.lock:
                    if self._current(group) and lane in (group['active'],group['pending']):self._fail(group,str(problem))
            finally:
                with self.lock:
                    if lane in self.lanes:self.lanes.remove(lane)
        lane['thread'] = threading.Thread(target=run,name='maintenance-position-stream',daemon=True)
        self.lanes.append(lane)
        return lane

    def snapshot(self):
        with self.lock:
            ctx, meta = self.context, self.meta
            connected = bool(ctx and not self.core.closed and self.core.token == ctx[3]
                and self.core.observed_transport is ctx[0] and self.core.observed_target == ctx[1]
                and self.core._target(ctx[0]) == ctx[1]
                and (self.core.connection is None or self.core.connection.active))
            owned = connected and meta.get('active') is True and meta.get('owned') is True and meta.get('mode') == 'numbering'
            age = max(0.,(self.clock()-self.at)*1000) if self.at is not None else None
            state = self.state
            if not owned:state = 'stopped'
            elif self.hold:state = 'waiting'
            elif state == 'live' and (age is None or age > POSITION_STALE_MS):state = 'stale'
            positions = copy.deepcopy(self.positions) if state == 'live' else [
                {'id':i,'position_raw':None,'position_error':self.error or '等待新鲜位置快照'} for i in self.ids]
            for row in positions:
                selected = self.id_states.get(row['id'])
                row['position_revision'] = selected['revision'] if selected else 0
                if selected and not selected['ready']:
                    row.update(position_raw=None, position_pending=True,
                        position_error=selected['error'] or '等待所选舵机的新鲜位置')
            rate = ((len(self.times)-1)/(self.times[-1]-self.times[0])
                    if len(self.times)>1 and self.times[-1]>self.times[0] else 0.)
            target = ctx[1][0] if ctx and ctx[1] else None
            stream = {'state':state,'generation':self.generation,'revision':self.revision,
                'target':target,'session_id':ctx[4] if ctx else None,'scan_generation':self.scan_generation,
                'sequence':self.sequence,'transport_age_ms':age,'rpc_ms':self.rpc_ms,
                'sample_id':None,'sample_age_ms':None,'target_hz':1/POSITION_PERIOD,
                'refresh_hz':rate if state=='live' else 0.,'error':self.error,
                'id_revisions':{str(i):s['revision'] for i,s in self.id_states.items()},
                'source':'existing-worker-cached-sync-read','transport':'shared-management-ssh'}
            return {'active':owned,'owned':owned,'mode':'numbering' if owned else 'idle',
                'session_id':stream['session_id'],'target':target,'scan':{'generation':self.scan_generation},
                'positions':positions,'position_stream':stream}

    def metadata(self):
        return self.snapshot()['position_stream']
