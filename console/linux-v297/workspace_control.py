"""Four workspaces share one measured robot state and one ZERO maintenance owner."""
import threading
import time
import collections
import json

from controls import ControlError
from maintenance_components import BenchComponents
from workspace_policy import calibrated, numbered_chain, numbered_scan, page_permissions
from telemetry import IDS


def motion_calibration_reason(snapshot, pending_ids=()):
    """Explain the existing calibration redirect; never grant motion access."""
    data = (snapshot.get('calibration') or {}).get('calibration') or {}
    if not isinstance(data.get('joints'), list):
        return '尚未读取到完整有效的标定数据，未进入运动模式。请先在标定页面读取并保存关节和 IMU 标定。'
    saved = {row.get('id') for row in data['joints']
             if isinstance(row, dict) and type(row.get('id')) is int and row.get('calibrated') is True}
    missing = [sid for sid in IDS if sid not in saved or sid in pending_ids]
    details = []
    if missing:
        details.append('待标定关节：' + '、'.join('ID ' + str(sid) for sid in missing) + '。')
    if data.get('imu_mount_verified') is not True:
        details.append('IMU 安装标定尚未确认。')
    if not details:
        details.append('已保存的标定数据尚未通过校验。')
    return '尚未完成有效标定，未进入运动模式。\n' + '\n'.join(details) + '\n请先在标定页面完成并保存标定，再切换到运动模式。'


class WorkspaceController:
    def __init__(self, store, service, controls=None, connection=None, ai=None, core=None, components=None):
        if core is None:
            from maintenance_control import MaintenanceController
            core = MaintenanceController(store=store, service_controller=service,
                                         controls=controls, connection=connection)
        self.store, self.service, self.controls = store, service, controls
        self.connection, self.ai, self.core = connection, ai, core
        self.components = components or BenchComponents(lambda: self.service.transport)
        self.lock = threading.RLock()
        self.last = {'available': False, 'active': False, 'mode': 'idle',
                     'robotd_running': None, 'serial_available': False}
        self.pending = False
        self.chain_evidence = None
        self.client = 'workspace-v261'
        self.seq = 0
        self.closed = False
        self.recalibration_lock=threading.RLock()
        self.recalibration_context=None
        self.recalibration_pending=set()
        self.recalibration_seen=collections.deque(maxlen=64)

    def _connected(self):
        return self.service is not None and (self.connection is None or self.connection.active)

    def busy_or_active(self):
        return self.pending or self.last.get('active') is True

    def _target(self):
        return getattr(getattr(self.service, 'transport', None), 'target', None)

    def _entry_target(self):
        return str(self._target() or 'local-zero')

    def _evidence_valid(self):
        evidence = self.chain_evidence
        return bool(evidence and evidence['target'] == self._target()
                    and time.monotonic()-evidence['at'] < 120)

    def _service_stop_available(self, current):
        """Stopping the service is a recovery action, independent of bench data.

        A missing maintenance helper or an unknown worker status must not hide
        the existing SSH service-management entry point. The stop command still
        confirms the actual systemd state on ZERO before reporting success.
        """
        transport = getattr(self.service, 'transport', None)
        return bool(not self.closed and self._connected()
                    and not getattr(self.service, 'closed', False)
                    and callable(getattr(transport, 'manage', None))
                    and not getattr(self.service, 'busy', False)
                    and not getattr(self.service, 'stopping', False)
                    and not self.pending and not current.get('active')
                    and current.get('robotd_running') is not False)

    def status(self, refresh=False):
        if not self._connected():
            current = {'available': False, 'active': False, 'mode': 'idle',
                       'robotd_running': None, 'serial_available': False,
                       'error': '先连接 ZERO'}
        else:
            try:
                current = self.core.status(refresh=refresh)
            except Exception as exc:
                current = {**self.last, 'available': False, 'error': str(exc),
                           'robotd_running': None, 'serial_available': False}
        self.last = dict(current)
        receipt=current.get('receipt') if isinstance(current,dict) else None
        if (isinstance(receipt,dict) and current.get('active') is True and current.get('owned') is True
                and current.get('mode')=='numbering' and receipt.get('swapped') in (True,False)):
            self._remember_swap_calibration(current,receipt)
        return self._status_fields(current)

    def _observed_service_mode(self, snapshot):
        """Reuse the existing read-only process query, never a UART sample gate."""
        if (not self._connected() or snapshot.get('connection_paused') is True
                or self.connection is not None and getattr(self.connection,'ssh_authenticated',False) is not True):
            return None
        cached=getattr(self.service,'connection_cache',None)
        key=(id(getattr(self.service,'transport',None)),getattr(self.store,'robotd_epoch',0))
        if not isinstance(cached,tuple) or len(cached)!=2 or cached[0]!=key:return None
        report=cached[1]
        if (not isinstance(report,dict) or report.get('running') is not True
                or type(report.get('pid')) is not int or report['pid']<=0 or report.get('error')
                or report.get('actual_mode') not in ('commissioning','motion')):return None
        return report['actual_mode']

    def _status_fields(self, current):
        pending_calibration=self._apply_recalibration_pending()
        snapshot = self.store.peek()
        pages = page_permissions(snapshot, current, self._connected(), self.pending)
        chain = current.get('chain') or current.get('chain_verification') or {}
        bus=snapshot.get('bus') or {}
        live=(snapshot.get('service_sample_fresh') is True
              and (snapshot.get('channels',{}).get('bus') or {}).get('status')=='live')
        # Store.peek has no browser connection overlay. Reuse the observed SSH
        # login flag directly; taking Connection.status locks here would invert
        # the connection/workspace shutdown lock order.
        routing_connected=(self._connected() and snapshot.get('connection_paused') is not True
            and (self.connection is None or getattr(self.connection,'ssh_authenticated',False) is True))
        process_mode=self._observed_service_mode(snapshot)
        initial_page=None
        if not routing_connected:
            pass
        elif (current.get('available') is True and current.get('active') is True
                and current.get('owned') is True and not current.get('error')):
            initial_page={'numbering':'numbering','hardware-test':'hardware','hardware_test':'hardware'}.get(current.get('mode'))
        elif live and bus.get('mode') in ('motion','commissioning'):
            initial_page='robot' if bus['mode']=='motion' else 'calibration'
        elif process_mode:
            initial_page='robot' if process_mode=='motion' else 'calibration'
        elif (current.get('active') is False and current.get('robotd_running') is False
                and current.get('serial_available') is True and not current.get('error')):
            initial_page='calibration' if (numbered_chain(snapshot) or numbered_scan(current) or self._evidence_valid()
                or chain.get('numbering_verified') is True or calibrated(snapshot)
                or (snapshot.get('calibrated_count') or 0)>0) else 'numbering'
        return {**current, 'allowed_pages': pages, 'pending': self.pending, 'initial_page':initial_page,
                'service_process_mode':process_mode,
                'service_stop_available': self._service_stop_available(current),
                'numbering_verified': numbered_chain(snapshot) or numbered_scan(current) or self._evidence_valid()
                    or chain.get('numbering_verified') is True,
                'calibration_valid': calibrated(snapshot) and not pending_calibration,
                'service_mode': (snapshot.get('bus') or {}).get('mode')}

    def _pause_robotd(self):
        pause = getattr(self.store, 'pause_robotd_connections', None)
        if pause: pause()
        if self.ai and self.ai.worker and self.ai.worker.is_alive():
            self.ai.stop()
        if self.controls:
            self.controls.pause_input()

    def _remember_swap_calibration(self, result, payload):
        """Apply only acknowledged invalidations, without inventing a new sample."""
        receipt=result.get('receipt') or {}
        ids=receipt.get('needs_recalibration_ids')
        old,new=receipt.get('old_id'),receipt.get('new_id')
        if (type(old) is not int or not 1<=old<=250 or old==200 or type(new) is not int or new not in IDS or old==new
                or not isinstance(result.get('session_id'),str) or not result['session_id']
                or old!=payload.get('old_id') or new!=payload.get('new_id')
                or not isinstance(ids,list) or len(ids)>2
                or any(type(i) is not int or i not in IDS or i not in (payload['old_id'],payload['new_id']) for i in ids)):
            return
        with self.recalibration_lock:
            self._recalibration_current()
            key=(result.get('session_id'),json.dumps(receipt,sort_keys=True,ensure_ascii=False))
            if key in self.recalibration_seen:return
            self.recalibration_seen.append(key)
            self.recalibration_pending.update(ids)
        self._apply_recalibration_pending()

    def _uncertain_swap_calibration(self, payload):
        ids={i for i in (payload.get('old_id'),payload.get('new_id')) if type(i) is int and i in IDS}
        with self.recalibration_lock:
            self._recalibration_current();self.recalibration_pending.update(ids)
        self._apply_recalibration_pending()

    def _recalibration_current(self):
        context=(getattr(self.service,'transport',None),self._target(),getattr(self.connection,'stop',None))
        prior=self.recalibration_context
        if prior is None or prior[0] is not context[0] or prior[1]!=context[1] or prior[2] is not context[2]:
            self.recalibration_context=context;self.recalibration_pending.clear();self.recalibration_seen.clear()

    def _apply_recalibration_pending(self):
        with self.recalibration_lock:
            self._recalibration_current()
            ids=set(self.recalibration_pending)
            self._mark_cached_calibration(ids)
            return ids

    def _mark_cached_calibration(self, ids):
        data=getattr(self.store,'data',None);lock=getattr(self.store,'lock',None)
        if not isinstance(data,dict) or lock is None:return
        with lock:
            report=data.get('calibration') or {};calibration=report.get('calibration') or {}
            for row in calibration.get('joints') or []:
                if isinstance(row,dict) and row.get('id') in ids:row['calibrated']=False

    def capture_completed(self, command, answer, transport, epoch):
        """Only a current, saved capture ACK can release its pending joint."""
        ident=command.get('id');report=answer.get('calibration') if isinstance(answer,dict) else None
        data=report.get('calibration',report) if isinstance(report,dict) else {}
        rows=data.get('joints') if isinstance(data,dict) else None
        found=[r for r in rows if isinstance(r,dict) and r.get('id')==ident] if isinstance(rows,list) else []
        if (command.get('action')!='capture' or type(ident) is not int or not isinstance(answer,dict)
                or answer.get('saved') is not True or answer.get('id')!=ident or len(found)!=1 or found[0].get('calibrated') is not True):return
        with self.recalibration_lock:
            self._recalibration_current()
            if (self.closed or not self._connected() or getattr(self.service,'closed',False) or transport is not getattr(self.service,'transport',None)
                    or epoch!=getattr(self.service,'cancel_epoch',None) or ident not in self.recalibration_pending):return
            self.recalibration_pending.discard(ident)
            cached=getattr(self.store,'data',None);lock=getattr(self.store,'lock',None)
            if isinstance(cached,dict) and lock is not None:
                with lock:
                    calibration=(cached.get('calibration') or {}).get('calibration') or {}
                    joints=calibration.get('joints') or []
                    for index,row in enumerate(joints):
                        if isinstance(row,dict) and row.get('id')==ident:joints[index]=dict(found[0])
            self._mark_cached_calibration(self.recalibration_pending)

    @staticmethod
    def _entry_payload(payload):
        allowed = {'action', 'mode', 'sudo_password'}
        if payload['action'] == 'enter': allowed.update(('stop_confirmed', 'entry_target'))
        if set(payload)-allowed or payload.get('mode') not in ('numbering', 'hardware-test'):
            raise ControlError('维护页面或进入参数无效', 400)
        if 'stop_confirmed' in payload and type(payload['stop_confirmed']) is not bool:
            raise ControlError('停止确认参数必须为布尔值', 400)
        if payload['action'] == 'enter' and not isinstance(payload.get('entry_target'), str):
            raise ControlError('进入前请先重新点击菜单检查连接目标', 409)
        password = payload.get('sudo_password')
        if password is not None and (not isinstance(password, str) or not 0 < len(password) <= 256
                                    or any(c in password for c in '\r\n\0')):
            raise ControlError('ZERO sudo 密码格式无效', 400)

    def _entry_state(self, mode, password=None):
        inspected = self.components.inspect(password=password, token=getattr(self.core, 'token', None))
        if (type(inspected.get('robotd_running')) is not bool
                or type(inspected.get('active')) is not bool
                or type(inspected.get('requires_component_install')) is not bool
                or inspected.get('serial_state') not in ('free', 'busy', 'unknown', 'missing')):
            raise ControlError('维护预检返回了无法确认的工作状态', 503)
        active, own = inspected['active'], inspected.get('owned') is True
        requires_stop = inspected['robotd_running'] is True
        requires_install = inspected['requires_component_install']
        reason = ''
        if self.service.busy or self.service.stopping or self.pending:
            reason = '先等待当前服务或维护操作完成'
        elif active and not own:
            reason = '另一个中控正在维护，请先由对应中控退出测试'
        elif active and requires_stop:
            reason = '机器人服务与维护状态冲突，请重新检查实际状态'
        elif not active and not requires_stop and inspected['serial_state'] == 'busy':
            reason = inspected.get('serial_reason') or '串口正被其他程序占用'
        elif not active and not requires_stop and inspected['serial_state'] == 'missing':
            reason = inspected.get('serial_reason') or 'HAT 串口不存在'
        elif not active and not requires_stop and inspected['serial_state'] == 'unknown':
            reason = inspected.get('serial_reason') or '无法确认 HAT 串口占用情况，未开始维护'
        can_enter = not bool(reason)
        notice = []
        if requires_stop: notice.append('机器人总线仍在运行，确认后停止并进入')
        if requires_install: notice.append('首次进入将准备本版维护测试组件，不替换机器人控制固件')
        if active and own and inspected.get('mode') != mode:
            notice.append('进入后结束当前维护测试并切换页面，机器人服务保持停止')
        current = {**inspected, 'available': not requires_install, 'mode': inspected.get('mode') or 'idle',
                   'needs_firmware': False, 'needs_components': requires_install, 'error': ''}
        return {**self._status_fields(current), 'accepted': True, 'can_enter': can_enter,
                'requires_stop': requires_stop, 'requires_component_install': requires_install,
                'reason': reason or '；'.join(notice), 'requested_mode': mode,
                'entry_target': self._entry_target()}

    def _enter(self, payload):
        mode, password = payload['mode'], payload.get('sudo_password')
        # Connection target changes and service management use this same lock.
        # The authoritative recheck, confirmation check and all mutations stay
        # inside it, so a preflight for one ZERO cannot stop another target.
        with self.service.lock:
            if self.closed or not self._connected():
                raise ControlError('先连接 ZERO', 409)
            if payload.get('entry_target') != self._entry_target():
                raise ControlError('连接目标已改变，请重新点击菜单检查后进入', 409)
            if self.service.busy or self.service.stopping:
                raise ControlError('先等待当前服务操作完成', 409)
            checked = self._entry_state(mode, password)
            if not checked['can_enter']:
                raise ControlError(checked['reason'], 409)
            if checked['requires_stop'] and payload.get('stop_confirmed') is not True:
                raise ControlError('机器人总线仍在运行，请先确认是否停止并进入', 409)
            # Cancelled preflight cannot reach pause/stop/prepare/begin.
            self.pending = True
            self.store.service_operation = True
            try:
                self._pause_robotd()
                reuse = checked.get('active') and checked.get('owned') and checked.get('mode') == mode
                if reuse:
                    result = self.core.status(refresh=True)
                    if not result.get('active') or result.get('owned') is not True or result.get('mode') != mode:
                        raise ControlError('维护会话已经改变，请重新进入页面', 409)
                else:
                    if checked.get('active') and checked.get('owned'):
                        self.core.handle({'action': 'end'})
                    if checked['requires_stop']:
                        self.components.stop(password=password)
                    latest = self.components.inspect(password=password, token=getattr(self.core, 'token', None))
                    if latest.get('active'):
                        raise ControlError('维护会话已被其他中控占用，未开始新的测试', 409)
                    if latest.get('robotd_running') is not False:
                        raise ControlError('机器人服务尚未确认停止，未开始维护测试', 409)
                    if latest.get('serial_state') != 'free':
                        raise ControlError(latest.get('serial_reason') or 'HAT 串口当前不可用', 409)
                    if latest.get('requires_component_install'):
                        prepared = self.components.prepare(password=password)
                        worker_path = prepared.get('worker_path')
                        latest = self.components.inspect(password=password, token=getattr(self.core, 'token', None))
                        if (latest.get('active') or latest.get('robotd_running') is not False
                                or latest.get('serial_state') != 'free'
                                or latest.get('requires_component_install') is not False
                                or latest.get('worker_path') != worker_path):
                            raise ControlError('组件已准备，但当前服务、维护会话或串口状态已改变，未开始测试', 409)
                    else:
                        worker_path = latest.get('worker_path')
                    if not worker_path:
                        raise ControlError('维护组件程序尚未准备完成', 503)
                    self.core.use_worker(worker_path, component_id=latest['component_id'])
                    result = self.core.handle({'action': 'begin', 'mode': mode,
                                               **({'sudo_password': password} if password is not None else {})})
                    if result.get('active') is not True or result.get('owned') is not True or result.get('mode') != mode:
                        raise ControlError('没有确认维护会话成功建立，请检查实际状态', 503)
                    self.chain_evidence = None
                self.last = dict(result)
            finally:
                self.pending = False
                self.store.service_operation = False
        # Build success state only after clearing pending, including idempotent
        # re-entry into the currently owned maintenance page.
        return {**self._status_fields(result), 'accepted': True, 'result': result}

    def _service_mode(self, target, password=None):
        requested_target = target
        redirect_reason = ''
        if not self.service:
            raise ControlError('当前启动方式不提供机器人服务管理', 409)
        transport=getattr(self.service,'transport',None)
        expected_target=self._target()
        with getattr(self.service, 'lock', self.lock):
            if (self.closed or not self._connected() or getattr(self.service,'closed',False)
                    or getattr(self.service,'transport',None) is not transport or self._target()!=expected_target):
                raise ControlError('连接状态已改变，未切换机器人模式，请重新确认目标 ZERO',409)
            snapshot = self.store.peek()
            if getattr(self.service,'busy',False) or getattr(self.service,'stopping',False):
                raise ControlError('请先等待当前服务操作完成，再切换模式',409)
            bus=snapshot.get('bus') or {}
            live=(snapshot.get('service_sample_fresh') is True
                  and (snapshot.get('channels',{}).get('bus') or {}).get('status')=='live'
                  and bus.get('mode') in ('commissioning','motion'))
            process_mode=self._observed_service_mode(snapshot)
            observed_mode=bus['mode'] if live else process_mode
            if target == 'motion' and observed_mode!='motion':
                pending_calibration = self._apply_recalibration_pending()
                if pending_calibration or not calibrated(snapshot):
                    target='commissioning'
                    redirect_reason = motion_calibration_reason(snapshot, pending_calibration)
            redirect = {'requested_target': requested_target, 'redirect_reason': redirect_reason}
            # A live daemon already owns the UART. Opening its current page
            # requires neither a bench helper nor another service operation.
            if observed_mode==target:
                current={'available':True,'active':False,'owned':False,'mode':'idle',
                         'robotd_running':True,'serial_available':False}
                self.last=dict(current)
                return {**self._status_fields(current),'accepted':True,'unchanged':True,'target':target,**redirect}
            current = ({'available':True,'active':False,'owned':False,'mode':'idle',
                        'robotd_running':True,'serial_available':False}
                       if observed_mode else self.core.status(refresh=True))
            if isinstance(current,dict):self.last=dict(current)
            # Native mode management works on first startup, before a bench
            # component is prepared. Its canonical service script owns the
            # management lock and start/stop/UART checks; do not turn a missing
            # legacy helper into a separate whole-chain admission gate.
            if not isinstance(current,dict):current={}
            if (target=='commissioning' and current.get('active') is True and current.get('owned') is True
                    and current.get('mode')=='numbering' and (current.get('scan') or {}).get('running') is True):
                raise ControlError('请先等待舵机扫描完成，再进入标定模式',409)
            if current.get('active'):
                self.last = dict(current)
                if current.get('owned') is not True:
                    raise ControlError('其他中控正在维护，请先由对应中控退出', 409)
                ended = self.core.handle({'action': 'end'})
                if (not isinstance(ended,dict) or ended.get('available') is not True
                        or ended.get('active') is not False or ended.get('error')):
                    raise ControlError('退出维护的回执未确认，未切换机器人模式，请检查实际状态',503)
                self.last = dict(ended)
                released = self.core.status(refresh=True)
                if isinstance(released,dict):self.last = dict(released)
                if (not isinstance(released,dict) or released.get('available') is not True
                        or released.get('active') is not False or released.get('robotd_running') is not False
                        or released.get('serial_available') is not True or released.get('error')):
                    raise ControlError('维护串口尚未确认释放，未切换机器人模式，请检查实际状态后手动切换',503)
            self.seq += 1
            command = {'action': 'switch-mode', 'target': target, 'supported': True}
            if password is not None:
                command['sudo_password'] = password
            return {**self.service.handle({'client': self.client, 'seq': self.seq, 'command': command}),
                    'target':target,**redirect}

    def handle(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('action'), str):
            raise ControlError('维护请求必须包含操作名称', 400)
        action = payload['action']
        if self.closed or not self._connected():
            raise ControlError('先连接 ZERO', 409)
        if self.pending:
            raise ControlError('已有维护操作正在执行', 409)
        with self.lock:
            if self.pending:
                raise ControlError('已有维护操作正在执行', 409)
            if action in ('preflight', 'enter'):
                self._entry_payload(payload)
                if action == 'preflight':
                    with self.service.lock:
                        if self.closed or not self._connected():
                            raise ControlError('先连接 ZERO', 409)
                        return self._entry_state(payload['mode'], payload.get('sudo_password'))
                return self._enter(payload)
            if action in ('to-calibration', 'to-motion'):
                if set(payload)-{'action', 'sudo_password'}:
                    raise ControlError('模式切换参数无效', 400)
                return self._service_mode('motion' if action == 'to-motion' else 'commissioning',
                                          payload.get('sudo_password'))
            if action == 'stop-service':
                if set(payload)-{'action', 'sudo_password'}:
                    raise ControlError('停止服务参数无效', 400)
                with self.service.lock:
                    if self.service.busy or self.service.stopping:
                        raise ControlError('先等待当前服务操作完成', 409)
                    if self.last.get('active'):
                        raise ControlError('先退出当前维护测试', 409)
                    self.pending = True
                    self.store.service_operation = True
                    try:
                        self._pause_robotd()
                        answer = self.components.stop(password=payload.get('sudo_password'))
                        self.chain_evidence = None
                    finally:
                        self.pending = False
                        self.store.service_operation = False
                self.status(refresh=True)
                return {'accepted': True, 'result': answer, **self.status()}
            if action == 'begin':
                if self.service.busy or self.service.stopping:
                    raise ControlError('先等待当前服务操作完成', 409)
                state = self.core.status(refresh=True)
                if state.get('available') is not True:
                    raise ControlError(state.get('error') or '维护接口尚未就绪，请先检查配套固件',
                                       409 if state.get('needs_firmware') or state.get('needs_components') else 503)
                if state.get('robotd_running') is not False:
                    raise ControlError('先停止机器人总线服务，再进入维护', 409)
                self._pause_robotd()
            if action in ('scan', 'write', 'center-calibrate'):
                self.chain_evidence = None
            self.pending = True
            try:
                result = self.core.handle(payload)
                if action == 'verify-chain':
                    report = result.get('result', result)
                    report = report.get('chain', report)
                    if report.get('numbering_verified') is True:
                        self.chain_evidence = {'target': self._target(), 'at': time.monotonic()}
                    else:
                        self.chain_evidence = None
            except Exception as problem:
                if action=='write' and payload.get('swap') is True and getattr(problem,'swap_uncertain',False):
                    self._uncertain_swap_calibration(payload)
                raise
            finally:
                self.pending = False
            if action=='write' and payload.get('swap') is True:
                # The transaction receipt already contains its verified state.
                # Do not replace its two-ID barrier with an unrelated status call.
                self._remember_swap_calibration(result,payload)
                self.last=dict(result)
                current=self._status_fields(result)
            else:current = self.status(refresh=True)
            return {**current, 'accepted': result.get('accepted', True), 'result': result}

    def before_service(self, command):
        """Existing service endpoints enforce the same gate as the new menus."""
        action = command.get('action')
        if self.busy_or_active():
            if action == 'relax':
                return self.handle({'action': 'end'})
            raise ControlError('先退出编号或硬件测试，再操作机器人服务', 409)
        if action == 'switch-mode':
            snap = self.store.peek()
            if command.get('target') == 'motion' and (self._apply_recalibration_pending() or not calibrated(snap)):
                raise ControlError('先完成 15 个关节及 IMU 标定', 409)
        return None

    def disconnect(self):
        self.chain_evidence = None
        with self.recalibration_lock:
            self.recalibration_pending.clear();self.recalibration_seen.clear();self.recalibration_context=None
        try:
            if self.closed:
                self.core.close()
            else:
                self.core.disconnect()
        finally:
            self.last = {'available': False, 'active': False, 'mode': 'idle',
                         'robotd_running': None, 'serial_available': False}

    def close(self):
        self.closed = True
        self.disconnect()
