"""PC-side owner of a ZERO maintenance lease, over the existing SSH channel."""
import copy
import collections
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time

from controls import ControlError
from telemetry import IDS
from maintenance_positions import PositionFeedback, POSITION_CALL_SOURCE, eligible

WORKER = "/opt/robot/feetech-ft5-r5/maintenance_worker.py"
MISSING_MESSAGE = "维护测试组件尚未准备，请点击舵机编号或 IMU/HAT/HUB 测试，检查并准备所需组件"
CAPABILITY_TTL = 5.0
NUMBERING_IDS = frozenset(IDS)
IMU_STREAM_LIFETIME = 110.0
IMU_STREAM_RENEW_AT = IMU_STREAM_LIFETIME - 5.0
# Read-only compatibility probe for older ZERO installations. It neither imports
# nor executes the worker and succeeds (exit 0) when that file is absent.
WORKER_PROBE = (
    "import json\nfrom pathlib import Path\n"
    "try:\n    installed=Path(" + repr(WORKER) + ").is_file()\n"
    "except OSError:\n    installed=None\n"
    "print(json.dumps({'maintenance_installed':installed}))\n"
)
ACTIONS = {"begin", "status", "scan", "scan-stop", "write", "center-calibrate", "verify-chain", "imu-start", "imu-stop",
           "mic-start", "mic-stop", "duck-play", "duck-stop", "end"}


def empty_status(error=""):
    return {"available": not bool(error), "active": False, "owned": False, "mode": "idle",
            "robotd_running": None, "serial_available": False, "imu_running": False,
            "imu": {"status": "stopped", "ready": False, "reason": error or "尚未开始 IMU 测试",
                    "roll_deg": None, "pitch_deg": None, "gravity": None,
                    "gyro": None, "quat": None, "sequence": None, "age_ms": None},
            "audio": {}, "devices": [], "receipt": None, "center_receipt": None, "scan": None,
            "numbered_ids": [], "session_id": None, "error": error}


def missing_status():
    # Missing bench helpers do not require the full-robot firmware installer.
    return {**empty_status(MISSING_MESSAGE), "needs_firmware": False,
            "needs_components": True,
            "reason_code": "maintenance_not_installed"}


class MaintenanceNotInstalled(ControlError):
    def __init__(self):
        super().__init__(MISSING_MESSAGE, 409)


class MaintenanceController:
    def __init__(self, store=None, service_controller=None, controls=None, connection=None,
                 transport_provider=None, clock=time.monotonic):
        self.store, self.service_controller, self.controls = store, service_controller, controls
        self.connection, self.transport_provider, self.clock = connection, transport_provider, clock
        self.lock = threading.RLock()
        self.token = self.session_target = None
        self.observed_target = self.observed_transport = None
        self.worker_path = WORKER
        self.capability_installed, self.capability_at = None, float("-inf")
        self.cache, self.cache_at = empty_status(), -1.0
        self.last_client_seen = clock()
        self.error = ""
        self.closed = False
        self.stop_event = threading.Event()
        self.stream_lock = threading.Lock()
        self.stream_event = None
        self.stream_expired = False
        self.stream_generation = 0
        self.stream_thread = None
        self.stream_at = None
        self.stream_received = 0
        self.stream_times = collections.deque(maxlen=100)
        self.stream_coalesced = 0
        self.stream_state = "stopped"
        self.stream_error = ""
        self.stream_lanes = []
        self.position_barrier_ns = None
        self.positions = PositionFeedback(self, clock)
        self.heartbeat = threading.Thread(target=self._heartbeat, name="maintenance-lease", daemon=True)
        self.heartbeat.start()

    def _transport(self):
        if self.closed:
            raise ControlError("维护连接已关闭", 503)
        if self.connection is not None and not self.connection.active:
            raise ControlError("SSH 尚未连接，请先连接 ZERO", 503)
        transport = self.transport_provider() if self.transport_provider else getattr(self.service_controller, "transport", None)
        if transport is None:
            raise ControlError("当前数据源不支持硬件维护", 503)
        return transport

    @staticmethod
    def _target(transport):
        return (getattr(transport, "target", None), getattr(transport, "path", None))

    def _clear_capability(self):
        self.capability_installed, self.capability_at = None, float("-inf")

    def invalidate_capability(self):
        """Called after an explicit firmware installation; no remote mutation."""
        with self.lock:
            self._clear_capability()
            self.cache, self.cache_at = empty_status(), -1.0

    def _observe_transport(self, transport):
        target = self._target(transport)
        if self.observed_target != target or self.observed_transport is not transport:
            # Include disconnected/reconnected transports at the same address.
            # A preceding ZERO's missing/present capability is never reused.
            self._invalidate_stream()
            self.positions.invalidate()
            self.token, self.session_target = None, None
            self.cache, self.cache_at = empty_status(), -1.0
            self._clear_capability()
            self.worker_path = WORKER
            self.observed_target, self.observed_transport = target, transport
        return target

    def use_worker(self, path, component_id):
        """Select only a verified fixed-location component from entry preparation."""
        if not isinstance(component_id, str) or not re.fullmatch(r'[a-f0-9]{64}', component_id):
            raise ControlError('维护组件身份无效', 409)
        component_path = '/opt/robot/feetech-ft5-r5/maintenance-components/' + component_id + '/maintenance_worker.py'
        if path not in (WORKER, component_path):
            raise ControlError('维护组件程序路径无效', 409)
        with self.lock:
            self._observe_transport(self._transport())
            if self.worker_path != path:
                self.positions.invalidate()
                self.worker_path = path
                self._clear_capability()
                self.cache, self.cache_at = empty_status(), -1.0

    def _require_helper(self, transport, force=False):
        self._observe_transport(transport)
        if force or self.capability_installed is None or self.clock()-self.capability_at >= CAPABILITY_TTL:
            probe = WORKER_PROBE if self.worker_path == WORKER else WORKER_PROBE.replace(repr(WORKER), repr(self.worker_path))
            result = transport._manage(["python3", "-c", probe],
                                       privileged=False, record_output=False, result_format="json")
            if not isinstance(result, dict) or type(result.get("maintenance_installed")) is not bool:
                self._clear_capability()
                raise ControlError("无法确认 ZERO 维护模块状态，请检查 SSH 连接", 503)
            self.capability_installed = result["maintenance_installed"]
            self.capability_at = self.clock()
        if not self.capability_installed:
            raise MaintenanceNotInstalled()

    def _call(self, request, password=None):
        transport = self._transport()
        target = self._observe_transport(transport)
        self._require_helper(transport)
        if self.token is not None and self.session_target != target:
            # Never carry a token or a preceding robot's observations to a new ZERO.
            self.token, self.session_target = None, None
            self.cache = empty_status()
            self.cache_at = -1.0
        outgoing = dict(request)
        if self.token is not None:
            outgoing["token"] = self.token
        numbering = (self.cache.get('mode') == 'numbering'
                     or request.get('action') == 'begin' and request.get('mode') == 'numbering')
        command = (["python3", "-u", "-c", POSITION_CALL_SOURCE, self.worker_path] if numbering
                   else ["python3", "-u", self.worker_path])
        self.position_barrier_ns = None
        result = transport._manage(command, password=password,
                                   input_data=json.dumps(outgoing, ensure_ascii=False)+"\n",
                                   record_output=False, result_format="json")
        if not isinstance(result, dict):
            raise ControlError("维护回执不是有效对象", 503)
        barrier = result.pop('_position_barrier_ns', None)
        if numbering:
            if type(barrier) is not int or barrier <= 0:
                raise ControlError('编号位置读取屏障无效，操作结果请核对实际状态', 503)
            self.position_barrier_ns = barrier
        failure_state = result.pop('_position_failure_state', None)
        if result.get("error") and "active" not in result:
            problem = ControlError(str(result["error"]), int(result.get("code", 503)))
            problem.position_failure_state = failure_state
            problem.uncertain=result.get('uncertain') is True
            raise problem
        result.pop("token", None)
        result.setdefault("needs_firmware", False)
        result.setdefault("needs_components", False)
        return result

    def _remember(self, result, selected_id=None):
        self.cache = copy.deepcopy(result)
        self.cache_at = self.clock()
        self.error = str(result.get("error") or "")
        if not result.get("active") or result.get("owned") is False:
            self._invalidate_stream()
            self.token, self.session_target = None, None
        elif not result.get("imu_running"):
            self._invalidate_stream()
        self.positions.observe(self.cache, self.position_barrier_ns, selected_id)
        return self._position_metadata(self.cache)

    def _selected_precheck(self, state, ident):
        """A failed write is local only when a fresh owner snapshot proves it."""
        if not isinstance(state, dict): return False
        prior, scan = self.cache, state.get('scan')
        receipt = state.get('center_receipt')
        keys = ('id','kind','register_id','firmware','model_bytes','secondary_id',
                'identity_verified','suspected_duplicate','conflict_reasons','error')
        def identities(value):
            rows = value.get('devices')
            return [{k:r.get(k) for k in keys} for r in rows] if isinstance(rows,list) and all(isinstance(r,dict) for r in rows) else None
        ids = eligible(state)
        return (state.get('available') is True and state.get('active') is True
            and state.get('owned') is True and state.get('mode') == 'numbering'
            and state.get('session_id') == prior.get('session_id') and not state.get('error')
            and isinstance(scan, dict) and scan.get('completed') is True and scan.get('running') is False
            and scan.get('requires_rescan') is False and scan.get('generation') == (prior.get('scan') or {}).get('generation')
            and not scan.get('error') and scan.get('conflict_ids') == (prior.get('scan') or {}).get('conflict_ids')
            and ids is not None and ids == eligible(prior) and ident in ids
            and identities(state) == identities(prior)
            and isinstance(receipt, dict) and receipt.get('servo_id') == ident
            and receipt.get('precheck_failed') is True and receipt.get('command_sent') is False
            and receipt.get('command_count') == 0 and receipt.get('uncertain') is False
            and self.token is not None and self.session_target == self.observed_target
            and not self.closed and (self.connection is None or self.connection.active)
            and self.observed_transport is not None and self._target(self.observed_transport) == self.observed_target)

    def _position_metadata(self, result):
        return {**copy.deepcopy(result), 'position_stream':self.positions.metadata()}

    def position_stream_snapshot(self):
        """Local-only latest-position cache; never renew a lease or wait for I/O."""
        return self.positions.snapshot()

    def _invalidate_stream(self):
        # Generation fence: old target/stop/end callbacks cannot restore live data.
        with self.stream_lock:
            self.stream_generation += 1
            self.stream_at = None
            self.stream_event = None
            self.stream_state = "stopped"
            self.stream_error = ""

    def _start_imu_stream(self):
        with self.stream_lock:
            if (self.stream_thread is not None and self.stream_thread.is_alive()
                    and self.stream_state in ("live","waiting")):
                return
        transport = self._transport()
        token, path = self.token, self.worker_path
        with self.stream_lock:
            self.stream_generation += 1
            generation = self.stream_generation
            self.stream_at = self.stream_event = None
            self.stream_received = self.stream_coalesced = 0
            self.stream_times.clear()
            self.stream_state = "waiting"
            self.stream_error = ""
            self.stream_expired = False
        group = {"active":None,"pending":None,"failed":False,"sample_id":None}
        def current():
            return (not self.closed and not group["failed"] and generation == self.stream_generation
                and token == self.token and self.observed_transport is transport
                and self._target(transport) == self.observed_target)
        def fail(message):
            group["failed"] = True
            self.stream_state, self.stream_at = "error", None
            self.stream_error = "IMU 实时通道中断："+message+"；请停止读取后重新开始，未自动重连"
            self.error = self.stream_error
        def healthy(event):
            imu = event.get("imu") or {}
            gravity, ident = imu.get("gravity"), imu.get("sample_id")
            return (event.get("active") is True and event.get("owned") is True
                and event.get("mode") == "hardware-test" and event.get("imu_running") is True
                and imu.get("status") == "live" and imu.get("ready") is True
                and type(ident) is int and ident >= 0 and isinstance(gravity,list) and len(gravity)==3
                and all(type(v) in (int,float) and math.isfinite(v) for v in gravity)
                and type(imu.get("age_ms",0)) in (int,float) and 0 <= imu.get("age_ms",0) <= 50
                and type(imu.get("sample_age_ms",0)) in (int,float) and 0 <= imu.get("sample_age_ms",0) <= 150)
        def prepare():
            # Called under stream_lock; the new thread runs _manage separately.
            # Its creation never waits for a reply on the sole response reader.
            if len(self.stream_lanes) >= 2:
                fail("上一条只读订阅尚未结束，无法安全续租")
                return None
            lane = {"started":self.clock(),"expired":False,"thread":None}
            def output(line):
                try: event = json.loads(line)
                except (TypeError, ValueError): return
                if not isinstance(event,dict) or event.get("kind") not in ("imu","imu_stream_end"): return
                next_lane = None
                # Never wait for core.lock or an SSH reply on its sole reader.
                with self.stream_lock:
                    if not current(): return
                    if event.get("kind") == "imu_stream_end":
                        lane["expired"] = event.get("reason") == "lifetime"
                        return
                    if not isinstance(event.get("imu"),dict): return
                    valid = healthy(event)
                    ident = event["imu"].get("sample_id")
                    newer = type(ident) is int and (group["sample_id"] is None or ident > group["sample_id"])
                    if lane is group["pending"]:
                        if not valid or not newer: return
                        group["active"], group["pending"] = lane, None
                        self.stream_thread = lane["thread"]
                    if lane is not group["active"]: return
                    # Same sensor sample from overlapping subscribers is not a
                    # new frame and cannot refresh an old attitude's timestamp.
                    if not newer and event["imu"].get("status") == "live": return
                    self.stream_event, self.stream_at = event, self.clock()
                    if newer:
                        group["sample_id"] = ident
                        self.stream_received += 1
                        self.stream_times.append(self.clock())
                    self.stream_coalesced = event.get("stream_coalesced_frames",0)
                    self.stream_state = "live" if event.get("imu_running") else "stopped"
                    if (valid and group["pending"] is None and self.clock()-lane["started"] >= IMU_STREAM_RENEW_AT
                            and self.clock()-self.last_client_seen <= 10):
                        next_lane = prepare()
                        group["pending"] = next_lane
                if next_lane is not None: next_lane["thread"].start()
            def run():
                try:
                    with self.stream_lock:
                        if not current(): return
                    transport._manage(["python3","-u",path,"--imu-stream"],
                        input_data=json.dumps({"token":token})+"\n",record_output=False,
                        raw_output_handler=output, progress=lambda _line:None, result_format="text")
                    with self.stream_lock:
                        if current() and lane in (group["active"],group["pending"]):
                            fail("正常续租未完成交接" if lane["expired"] else "只读订阅提前关闭")
                except Exception as problem:
                    with self.stream_lock:
                        if current() and lane in (group["active"],group["pending"]): fail(str(problem))
                finally:
                    with self.stream_lock:
                        if lane in self.stream_lanes:self.stream_lanes.remove(lane)
            lane["thread"] = threading.Thread(target=run,name="maintenance-imu-stream",daemon=True)
            self.stream_lanes.append(lane)
            return lane
        with self.stream_lock:
            lane = prepare()
            group["active"] = lane
            if lane is not None:self.stream_thread = lane["thread"]
        if lane is not None:lane["thread"].start()

    def imu_stream_snapshot(self):
        """Local-only SSE data; this never waits for a remote command/core.lock."""
        self.last_client_seen = self.clock()
        with self.stream_lock:
            cache = self.stream_event or self.cache
            result = {key:copy.deepcopy(cache.get(key)) for key in
                      ("active","owned","mode","imu_running","imu")}
            result["imu"] = result.get("imu") or empty_status()["imu"]
            age = (max(0.,self.clock()-self.stream_at)*1000 if self.stream_at is not None else None)
            state = self.stream_state
            disconnected = (self.closed or (self.connection is not None and not self.connection.active)
                or self.observed_transport is None
                or self._target(self.observed_transport) != self.observed_target)
            if disconnected:
                result.update(active=False,owned=False,mode="idle",imu_running=False)
            if (age is None or age > 150 or state != "live"
                    or not result.get("active") or not result.get("imu_running")):
                imu = result["imu"]
                if result.get("imu_running"):
                    imu.update(status="error" if state == "error" else "stale",ready=False,
                               reason=self.stream_error if state == "error" else "等待新鲜 IMU 实时数据")
                    if state != "error":state = "waiting" if state == "waiting" else "stale"
                else:
                    imu.update(status="stopped",ready=False)
                    state = "stopped"
                for key in ("roll_deg","pitch_deg","gravity","gyro","quat"): imu[key] = None
            elif age is not None:
                result["imu"]["sample_age_ms"] = (result["imu"].get("sample_age_ms") or 0)+age
            rate = ((len(self.stream_times)-1)/(self.stream_times[-1]-self.stream_times[0])
                    if len(self.stream_times)>1 and self.stream_times[-1]>self.stream_times[0] else 0.0)
            result["stream"] = {"state":state,"age_ms":age,"received_frames":self.stream_received,
                "coalesced_frames":self.stream_coalesced,"transport":"shared-management-ssh",
                "target_hz":25,"refresh_hz":rate if state == "live" else 0.0,
                "generation":self.stream_generation}
            return result

    def status(self, refresh=False, _heartbeat=False):
        with self.lock:
            if not _heartbeat:
                self.last_client_seen = self.clock()
            if self.closed:
                return empty_status("维护连接已关闭")
            try:
                transport = self._transport()
                self._observe_transport(transport)
                capability_fresh = (self.capability_installed is not None
                                    and self.clock()-self.capability_at < CAPABILITY_TTL)
                # Reuse the existing lease snapshot, never start per-ID work.
                # Valid numbering positions follow the browser's 100 ms poll;
                # scan progress and all other pages retain their prior cache.
                scan = self.cache.get("scan")
                positions = (self.cache.get("active") is True and self.cache.get("owned") is True
                             and self.cache.get("mode") == "numbering" and isinstance(scan, dict)
                             and scan.get("completed") is True and scan.get("running") is False
                             and scan.get("requires_rescan") is False)
                cache_ttl = .2 if isinstance(scan, dict) and scan.get("running") is True else .01 if positions else .8
                if not refresh and capability_fresh and self.clock()-self.cache_at < cache_ttl:
                    return self._position_metadata(self.cache)
                self._require_helper(transport, force=refresh and not _heartbeat)
                return self._remember(self._call({"action": "status"}))
            except MaintenanceNotInstalled:
                return self._remember(missing_status())
            except Exception as problem:
                # A disconnected service must not continue presenting an old live IMU.
                self._clear_capability()
                self._invalidate_stream()
                self.positions.invalidate()
                self.token, self.session_target = None, None
                self.error = str(problem)
                self.cache, self.cache_at = empty_status(self.error), self.clock()
                return self._position_metadata(self.cache)

    def handle(self, payload):
        if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
            raise ControlError("不支持的维护操作", 400)
        action = payload["action"]
        allowed = {"action", "sudo_password"}
        if action == "begin": allowed |= {"mode"}
        if action == "write": allowed |= {"old_id", "new_id", "single_servo_confirmed", "unique_id1_confirmed", "selected_servo_confirmed", "swap"}
        if action == "center-calibrate": allowed |= {"servo_id", "single_servo_confirmed", "selected_servo_confirmed", "center_confirmed"}
        if set(payload)-allowed:
            raise ControlError("维护请求包含不支持的参数", 400)
        password = payload.get("sudo_password")
        if password is not None and (not isinstance(password, str) or not 0 < len(password) <= 256
                                     or any(c in password for c in "\r\n\0")):
            raise ControlError("ZERO sudo 密码格式无效", 400)
        if action == "begin" and payload.get("mode") not in ("numbering", "hardware-test"):
            raise ControlError("维护模式无效", 400)
        if action == "write":
            old, new = payload.get("old_id"), payload.get("new_id")
            if type(old) is not int or not 0 <= old < 254 or old == 200 or type(new) is not int or new not in NUMBERING_IDS:
                raise ControlError("请输入有效原 ID，并选择本机 15 个目标关节 ID", 400)
            confirmations = set(payload) & {"single_servo_confirmed", "unique_id1_confirmed", "selected_servo_confirmed"}
            if len(confirmations) != 1 or payload[next(iter(confirmations))] is not True:
                raise ControlError("编号连接确认方式必须且只能选择一种，并确认所选舵机", 400)
            if "unique_id1_confirmed" in confirmations:
                if old != 1 or payload["unique_id1_confirmed"] is not True:
                    raise ControlError("请确认总线上只有一颗物理 ID 1 待编号舵机", 400)
            if 'swap' in payload and (payload['swap'] is not True or confirmations!={'selected_servo_confirmed'} or old==new):
                raise ControlError('互换编号需要明确选择另一颗已编号舵机',400)
        if action == "center-calibrate":
            ident = payload.get("servo_id")
            if type(ident) is not int or not 0 <= ident < 254 or ident == 200:
                raise ControlError("请输入有效舵机 ID，IMU 200 不能进行中位校准", 400)
            confirmations=set(payload)&{'single_servo_confirmed','selected_servo_confirmed'}
            if len(confirmations)!=1 or payload[next(iter(confirmations))] is not True:
                raise ControlError('请单独确认所选舵机；新旧中位确认方式不能混用',400)
            if payload.get("center_confirmed") is not True:
                raise ControlError("请先确认将当前位置设为 2048 中位", 400)
        request = {key: value for key, value in payload.items() if key != "sudo_password"}
        with self.lock:
            self.last_client_seen = self.clock()
            if action == "status": return self.status(refresh=True)
            selected_id = ((request['old_id'],request['new_id']) if action=='write' and request.get('swap') is True
                           else request.get('servo_id') if action == 'center-calibrate' else None)
            if selected_id is not None:
                for ident in selected_id if isinstance(selected_id,tuple) else (selected_id,):self.positions.begin_selected(ident)
            if action in ('begin','scan','scan-stop','write','end') and not (action=='write' and request.get('swap') is True):
                self.positions.begin_revision()
            try:
                self._require_helper(self._transport(), force=True)
            except MaintenanceNotInstalled:
                self._remember(missing_status())
                raise
            if action == "begin":
                if self.token is not None:
                    state = self._call({"action": "status"}, password)
                    self._remember(state)
                    if state.get("active") and state.get("owned"):
                        if state.get("mode") == request["mode"]:
                            return state
                        raise ControlError("请先退出当前维护会话，再切换编号或板卡测试", 409)
                self.token = secrets.token_hex(32)
                self.session_target = self._target(self._transport())
                try:
                    return self._remember(self._call(request, password))
                except Exception:
                    self.token, self.session_target = None, None
                    self.cache, self.cache_at = empty_status(), -1.0
                    raise
            if action == "end" and self.token is None:
                return self.status(refresh=True)
            if self.token is None:
                raise ControlError("请先进入编号或板卡测试维护会话", 409)
            try:
                result = self._call(request, password)
            except Exception as problem:
                if action=='write' and request.get('swap') is True:
                    problem.swap_uncertain=(not isinstance(problem,ControlError)
                        or getattr(problem,'status',503)>=500 or getattr(problem,'uncertain',False))
                failure_state = getattr(problem, 'position_failure_state', None)
                if (type(selected_id) is int and getattr(problem, 'status', None) == 409
                        and self._selected_precheck(failure_state, selected_id)):
                    self._remember(failure_state, selected_id)
                    raise
                self.error = str(problem)
                # Preserve the session but invalidate the sample after an uncertain call.
                self._invalidate_stream()
                self.cache = {**empty_status(self.error), "active": True, "owned": True,
                              "mode": self.cache.get("mode", "idle")}
                self.cache_at = self.clock()
                self.positions.observe(self.cache)
                raise
            remembered = self._remember(result, selected_id)
            if action == "imu-start" and result.get("imu_running"):
                self._start_imu_stream()
            return remembered

    def _heartbeat(self):
        while not self.stop_event.wait(3):
            self._heartbeat_once()

    def _heartbeat_once(self):
        with self.lock:
            if self.closed or self.token is None: return
            if self.clock()-self.last_client_seen > 10:
                self._release_lease()
            else:
                self.status(refresh=True, _heartbeat=True)

    def _release_lease(self):
        """Release the UART owner while retaining this transport's verified path."""
        with self.lock:
            self._invalidate_stream()
            self.positions.invalidate()
            if self.token is not None:
                try: self._call({"action": "end"})
                except Exception: pass  # The ZERO-side lease still has a bounded lifetime.
            self.token = self.session_target = None
            self._clear_capability()
            self.cache, self.cache_at = empty_status(), -1.0
            self.error = ""

    def disconnect(self):
        """Release the old target before Connection replaces its transport."""
        with self.lock:
            self._release_lease()
            self.observed_target = self.observed_transport = None
            self.worker_path = WORKER

    def close(self):
        self.disconnect()
        with self.lock:
            self.closed = True
            self.stop_event.set()
        if threading.current_thread() is not self.heartbeat:
            self.heartbeat.join(timeout=1)
