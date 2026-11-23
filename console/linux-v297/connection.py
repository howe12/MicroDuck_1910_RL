"""Dashboard ownership of the existing SSH collector and control transports."""
import threading
from contextlib import nullcontext
from controls import ControlError, Transport
from service_control import ServiceTransport
from ssh_connect import normalize_target, saved_target, save_target, set_credential, SSHConnectError
from sudo_credential import SudoCredential


class Connection:
    def __init__(self, store, config, controls, service, collector, agent_config):
        self.store, self.config, self.controls, self.service = store, config, controls, service
        self.collector, self.agent_config = collector, agent_config
        self.lock = threading.RLock()
        self.ssh_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.closed = False
        self.target = config.get('ssh_target') or saved_target()
        self.stop, self.worker = threading.Event(), None
        self.active = False
        self.error = ''
        self.ssh_endpoints = {}
        self.ssh_status_seen = False
        self.ssh_authenticated = False
        self.ssh_error = ''
        config.update(connection_ui=True, ssh_target=self.target)
        controls.transport.target = self.target
        if service:
            service.transport.target = service.transport.channel.target = self.target
            service.transport.credential = SudoCredential(self.target)
        self.store.connection_paused.set()
        controls.suspend_connections()

    def status(self):
        with self.lock, self.store.lock, self.ssh_lock:
            active = self.active and not self.store.connection_paused.is_set()
            # Only observed SSH transports prove login. Native samples and
            # endpoint failures neither authenticate SSH nor supply its error.
            connected = active and self.ssh_status_seen and any(self.ssh_endpoints.values())
            error = self.ssh_error if active and not connected else ''
            return {'target': self.target, 'active': active, 'connected': connected,
                    'error': error, 'camera_host': self.config['camera_host']}

    def _observe_ssh(self, generation, endpoint, connected):
        if endpoint not in ('robotd','tof','management') or type(connected) is not bool:return
        # The sole management reader must never wait for Connection.lock, which
        # can be held by a caller awaiting that reader's final receipt.
        with self.ssh_lock:
            if generation.is_set() or generation is not self.stop:return
            self.ssh_status_seen = True
            self.ssh_endpoints[endpoint] = connected
            self.ssh_authenticated = any(self.ssh_endpoints.values())
            if connected:self.ssh_error = ''

    def handle(self, payload):
        if not isinstance(payload, dict) or payload.get('action') not in ('connect', 'disconnect'):
            raise ControlError('连接操作无效', 400)
        # Serialize target replacement with final shutdown, while leaving the
        # state lock available to the retired collector's final callback.
        with self.operation_lock, self.service.lock if self.service else nullcontext():
            with self.lock:
                if self.closed: raise ControlError('中控已关闭', 409)
                if self.service and self.service.busy:
                    raise ControlError('当前服务操作还在执行，完成后可切换 SSH 连接', 409)
                try: target = normalize_target(payload.get('target', self.target))
                except SSHConnectError as exc: raise ControlError(str(exc), 400) from None
                password = payload.get('password', '')
                if not isinstance(password, str) or len(password) > 256 or any(c in password for c in '\r\n\0'):
                    raise ControlError('SSH 密码格式无效', 400)
                worker = self._disconnect()
            if worker: worker.join(timeout=1)
            if payload['action'] == 'disconnect':
                return {'accepted': True, **self.status()}
            with self.lock:
                return self._connect(target, password)

    def _connect(self, target, password):
        log = self.store.system_log
        credential = self.service.transport.credential if self.service and self.service.transport.target == target else SudoCredential(target)
        if password:
            credential.remember(password)
        if log: log.hide(credential.get())
        set_credential(target, credential)
        previous_target = self.target
        self.target = target
        resume=getattr(self.store,'resume_robotd_connections',None)
        if resume and target != previous_target:resume()
        self.config.update(ssh_target=target, camera_host=target.split('@', 1)[1])
        self.controls.transport = Transport(target, self.controls.transport.socket_path, log)
        self.controls.transport.robotd_manual_stop=getattr(self.store,'robotd_manual_stop',None)
        self.controls.pad = {}; self.controls.error = ''; self.controls.message = '等待网页手柄连接'
        if self.service:
            self.service.transport = ServiceTransport(target, self.service.transport.path, log)
            self.service.transport.credential = credential
            self.controls.attach_service_transport(self.service.transport)
            self.service.stopping = False; self.service.error = ''; self.service.job = None
            self.service.power_boot_id = None; self.service.cancel_epoch += 1
        with self.store.lock:
            self.store.data.clear(); self.store.history.clear(); self.store.service_commands.clear()
            self.store.bus_sample_key = self.store.bus_sample_at = None
            self.store.service_operation = False
            for channel in self.store.channels.values():
                channel.update(status='waiting', at=None, error='', seq=0)
                channel.pop('error_source',None)
        hub = getattr(self.store, 'raw_hub', None)
        if hub: hub.reset()
        with self.ssh_lock:self.stop = threading.Event()
        self.store.install_handoff.complete()
        generation = self.stop
        def emit(event):
            with self.lock:
                if generation.is_set() or generation is not self.stop: return
                if event.get('channel') == 'ssh_transport':
                    data = event.get('data', {})
                    self._observe_ssh(generation,data.get('endpoint'),data.get('connected'))
                    return
                if 'error' in event:
                    if event.get('error_source') == 'ssh':
                        with self.ssh_lock:
                            self.ssh_status_seen = True
                            self.ssh_error = str(event['error'])
                            self.ssh_authenticated = any(self.ssh_endpoints.values())
                self.store.ingest(event)
        if self.service:
            # A real management reply proves SSH authentication even while the
            # deliberately stopped robot data endpoint has no live stream.
            # Never park the sole management reader behind a connection change
            # which may itself be waiting for a final maintenance-end receipt.
            def management_status(connected):
                self._observe_ssh(generation,'management',connected)
            self.service.transport.channel.on_authenticated = management_status
        self.active = True
        self.store.connection_paused.clear()
        self.worker = threading.Thread(target=self.collector,
            args=(target, self.agent_config, emit, generation, log, self.store.connection_paused), daemon=True)
        self.worker.start()
        try: save_target(target)
        except OSError:
            if log: log.add('SSH 地址保存', '当前连接可用，地址未能保存到本机')
        return {'accepted': True, **self.status()}

    def _disconnect(self):
        workspace = getattr(self.store, 'workspace', None)
        if workspace: workspace.disconnect()
        self.active = False
        with self.ssh_lock:
            self.stop.set()
            self.ssh_endpoints.clear()
            self.ssh_status_seen = False
            self.ssh_authenticated = False
            self.ssh_error = ''
        self.store.connection_paused.set()
        self.store.install_handoff.wake()
        hub = getattr(self.store, 'raw_hub', None)
        if hub: hub.reset(clear=False)
        self.controls.suspend_connections()
        # A replaced transport has no owner left to retire a late request. Mark
        # its channel closed so an in-flight maintenance caller cannot reopen it.
        if self.service: self.service.transport.channel.close()
        worker = self.worker
        self.worker = None
        return worker

    def close(self):
        with self.operation_lock:
            with self.lock:
                if self.closed: return
                self.closed = True
                worker = self._disconnect()
            if worker: worker.join(timeout=1)
