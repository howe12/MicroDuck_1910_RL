#!/usr/bin/env python3
"""Finite bench maintenance on ZERO's HAT; no policy or position commands.

The socket owner holds the same management lock as service.sh and TIOCEXCL on
ttyS2. Normal robotd must be stopped *before* opening this session. A forgotten
session expires without restarting robotd. JSON enters on stdin; tokens never
appear in argv or in the public browser response.

Packet/checksum and FT6 diagnostic layout follow duck-control/src/feetech.rs;
gyro/quaternion units follow duck-control/src/imu.rs in the frozen V260 source.
ID(5)/secondary ID(7)/lock(55) follow the matched HLS profile and official SDK:
https://www.feetechrc.com/Data/feetechrc/upload/file/20240702/舵机协议手册-磁编码版本.pdf
https://github.com/ftservo/FTServo_Arduino/blob/main/src/HLSCL.h
https://ftservo.github.io/ftservo-wiki/en/reference/parameter/memory-hls/
This is an explicit HD1910 bench operation, never a generic register editor.
Readback is not a substitute for the user's subsequent power-cycle verification.
"""
import collections
import ast
import copy
import datetime
import stat
import tempfile
import tomllib
import fcntl
import json
import math
import os
from pathlib import Path
import select
import secrets
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

PORT = "/dev/ttyS2"
SOCKET_PATH = "/run/microduck-maintenance.sock"
LOCK_PATH = "/run/microduck-service-management.lock"
LEASE_SECONDS = 20.0
MAX_SESSION_SECONDS = 1800.0
SERVO_IDS = tuple(range(10, 15)) + tuple(range(20, 25)) + tuple(range(30, 35))
IMU_ID = 200
IMU_IDENTITY = bytes((6, 0, 0, 0, 0xF2))
ID_ADDRESS, SECONDARY_ID_ADDRESS, LOCK_ADDRESS, TORQUE_ADDRESS = 5, 7, 55, 40
IMU_DIAGNOSTIC_ADDRESS, IMU_DIAGNOSTIC_LENGTH = 124, 20
IMU_POLL_SECONDS = .02
IMU_STREAM_SECONDS = .04
IMU_STALE_SECONDS = .15
IMU_STREAM_LIFETIME = 110.0
REQUEST_RECEIVE_SECONDS = 2.0
REPLY_FLUSH_SECONDS = 2.0
MAX_REPLY_BYTES = 65536
SCAN_START_ID, SCAN_END_ID = 1, 250
CENTER_DIRECTORY = Path("/etc/robot/feetech-ft5")
CENTER_SERIALIZER = Path("/opt/robot/feetech-ft5-r5/configure.py")


def blank_scan(generation=0):
    return {"state":"idle", "running":False, "completed":False, "cancelled":False,
            "current_id":None, "last_completed_id":None, "scanned":0, "total":250,
            "found":0, "servo_count":0, "imu_count":0, "start_id":1, "end_id":250,
            "generation":generation, "error":"", "requires_rescan":True,
            "invalidation_reason":"尚未完成本次扫描", "conflict_ids":[], "uniqueness_proven":False}



class MaintenanceError(RuntimeError):
    def __init__(self, message, code=409, **detail):
        super().__init__(message)
        self.code, self.detail = code, detail


class BusTimeout(MaintenanceError):
    pass


def robotd_state():
    result = subprocess.run(["systemctl", "show", "robotd.service", "-p", "ActiveState", "-p", "MainPID"],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise MaintenanceError("无法确认机器人服务已停止，请先检查 ZERO 服务状态", 503)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    state = values.get("ActiveState")
    if state not in ("active", "inactive", "failed", "activating", "deactivating", "reloading"):
        raise MaintenanceError("机器人服务状态未知，维护操作未开始", 503)
    running = state not in ("inactive", "failed") or values.get("MainPID", "0") != "0"
    return {"robotd_running": running, "robotd_state": state}


class ServiceProbe:
    """One bounded read-only observer; never owns or touches the motor bus."""
    def __init__(self, check):
        self.check=check;self.lock=threading.Lock();self.check_lock=threading.Lock();self.stop=threading.Event()
        self.value=self._read();self.at=time.monotonic()  # Initial authority stays synchronous.
        self.thread=threading.Thread(target=self._run,name='bench-service-state',daemon=True)
        self.thread.start()
    def _read(self):
        try:
            value=self.check().get('robotd_running')
            return value if type(value) is bool else None
        except Exception:return None
    def fresh(self):
        with self.check_lock:
            with self.lock:
                if self.value is not False:return self.value
            value=self._read()
            with self.lock:self.value=value;self.at=time.monotonic()
            return value
    def _run(self):
        while self.value is False and not self.stop.wait(.5):
            self.fresh()
    def state(self):
        with self.lock:return self.value if time.monotonic()-self.at<=3.7 else None
    def close(self):
        self.stop.set()
        if threading.current_thread() is not self.thread:self.thread.join(3.2)


def serial_available(path=PORT):
    """Inspect ownership without opening or configuring the UART. Unknown is busy."""
    if not Path(path).exists():
        return False
    try:
        result = subprocess.run(["fuser", path], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def packet(ident, instruction, params=b""):
    if (type(ident) is not int or not (0<=ident<254 or ident==254 and instruction==0x82 and len(params)>2 and params[:2]==b'\x38\x02')
            or len(params)>253):
        raise MaintenanceError("维护指令只允许单播有效设备 ID", 400)
    body = bytes((ident, len(params) + 2, instruction)) + bytes(params)
    return b"\xff\xff" + body + bytes((~sum(body) & 255,))


class SerialBus:
    """Standard-library 1Mbps UART; writes are single-shot and bounded."""
    def __init__(self, path=PORT, clock=time.monotonic):
        self.clock, self.fd = clock, None
        self.bad_checksums = 0
        self.last_probe = None
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attr = termios.tcgetattr(fd)
            attr[0] = attr[1] = attr[3] = 0
            attr[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attr[4] = attr[5] = termios.B1000000
            attr[6][termios.VMIN] = attr[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attr)
            termios.tcflush(fd, termios.TCIOFLUSH)
            self.fd = fd
        except Exception:
            try: fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError: pass
            os.close(fd)
            raise

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            try: fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError: pass
            os.close(fd)

    def exchange(self, ident, instruction, params=b"", length=0, timeout=.035, accepted_ids=None, collect=False):
        accepted = set(accepted_ids or (ident,))
        batch=instruction==0x82;replies={}
        output, sent = packet(ident, instruction, params), 0
        deadline = self.clock() + timeout
        first = None
        if collect:
            self.last_probe = {"id":ident,"valid_replies":0,"inconsistent":False,"unexpected":0}
        termios.tcflush(self.fd, termios.TCIFLUSH)
        while sent < len(output):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise BusTimeout("串口发送超时；指令未重发", 503)
            if select.select([], [self.fd], [], remaining)[1]:
                try: sent += os.write(self.fd, output[sent:])
                except BlockingIOError: pass
        buffer = bytearray()
        while self.clock() < deadline:
            if select.select([self.fd], [], [], max(0, deadline-self.clock()))[0]:
                try: buffer.extend(os.read(self.fd, 512))
                except BlockingIOError: continue
                while len(buffer) >= 4:
                    if buffer[:2] != b"\xff\xff" or buffer[2] >= 254 or not 2 <= buffer[3] <= 253:
                        if batch:raise MaintenanceError('同步读帧头无效',503)
                        del buffer[0]
                        continue
                    width = buffer[3] + 4
                    if len(buffer) < width: break
                    row = bytes(buffer[:width])
                    if sum(row[2:]) & 255 != 255:
                        self.bad_checksums += 1
                        if batch:raise MaintenanceError('同步读校验失败',503)
                        del buffer[0]
                        continue
                    del buffer[:width]
                    if row[2] not in accepted or len(row[5:-1]) != length:
                        if batch:raise MaintenanceError('同步读地址或长度异常',503)
                        if collect:self.last_probe["unexpected"] += 1
                        continue
                    if not collect:
                        if row[4]:raise MaintenanceError(f"ID {row[2]} 返回设备状态 0x{row[4]:02X}；未继续写入",409,**({'failed_ids':[row[2]]} if batch else {}))
                        if batch:
                            if row[2] in replies:raise MaintenanceError('同步读重复回应',503)
                            replies[row[2]]=row[5:-1];continue
                        return row[5:-1]
                    signature=(row[4],row[5:-1])
                    self.last_probe["valid_replies"] += 1
                    if first is None:
                        first=signature
                        deadline=min(deadline,self.clock()+.002)
                    elif signature!=first:self.last_probe["inconsistent"]=True
                if batch and len(replies)==len(accepted):
                    if buffer or self.clock()>=deadline:raise MaintenanceError('同步读不完整或超时',503)
                    return replies
        if batch:raise BusTimeout('同步读缺少回应',503,failed_ids=sorted(accepted-replies.keys()))
        if collect and first is not None:
            if buffer:self.last_probe["unexpected"] += 1
            if first[0]:raise MaintenanceError(f"ID {ident} 返回设备状态 0x{first[0]:02X}；未继续写入",409)
            return first[1]
        raise BusTimeout(f"未收到 ID {ident} 的有效回复", 503)

    def ping(self, ident, timeout=.008):
        self.exchange(ident, 1, timeout=timeout)

    def read(self, ident, address, width):
        return self.exchange(ident, 2, bytes((address, width)), width)

    def sync_read(self, ids):
        if (not ids or len(ids)>251 or len(set(ids))!=len(ids)
                or any(type(i) is not int or not 0<=i<254 or i==IMU_ID for i in ids)):
            raise MaintenanceError('同步读舵机地址无效',400)
        return self.exchange(254,0x82,b'\x38\x02'+bytes(ids),2,.02,ids)

    def scan_ping(self, ident):
        return self.exchange(ident,1,timeout=.008,collect=True)

    def scan_read(self, ident, address, width):
        return self.exchange(ident,2,bytes((address,width)),width,collect=True)

    def calibrate_center(self, ident):
        if type(ident) is not int or not 0 <= ident < 254 or ident == IMU_ID:
            raise MaintenanceError("中位校准仅允许指定有效舵机地址",400)
        # Official FT-SCS 4.9 HLS >=3.43 Recal; never STS torque=128 or goal=2048.
        self.exchange(ident,0x0B)

    def write(self, ident, address, value, accepted_ids=None):
        if ident == IMU_ID or address not in (ID_ADDRESS, LOCK_ADDRESS, TORQUE_ADDRESS):
            raise MaintenanceError("维护程序拒绝此寄存器写入", 400)
        if address in (LOCK_ADDRESS, TORQUE_ADDRESS) and value not in ((0, 1) if address == LOCK_ADDRESS else (0,)):
            raise MaintenanceError("维护程序不允许上力或移动舵机", 400)
        if address == ID_ADDRESS and value not in SERVO_IDS:
            raise MaintenanceError("目标编号不是本机 15 个关节 ID", 400)
        self.exchange(ident, 3, bytes((address, value)), accepted_ids=accepted_ids)

    def _swap_id(self, ident, value, old, new, temporary):
        # Only the internal three-move transaction may use its checked spare ID.
        ids=(old,new,temporary)
        if (len(set(ids))!=3 or any(type(i) is not int or not 1<=i<=250 or i==IMU_ID for i in ids)
                or new not in SERVO_IDS or temporary in SERVO_IDS or ident not in ids or value not in ids or ident==value):
            raise MaintenanceError('互换编号内部地址无效',400)
        self.exchange(ident,3,bytes((ID_ADDRESS,value)),accepted_ids=(ident,value))

    def _numbering_secondary(self, ident):
        # HLS SECOND_ID is a separate EEPROM address. Confirm its actual value
        # instead of assuming MAIN_ID mirrored it. Retire the old write alias before
        # reusing the main address for the other participant of an exchange.
        # This private operation can only set the addressed servo's own alias;
        # it cannot assign an arbitrary alias or change the baud register (6).
        if type(ident) is not int or not 1<=ident<=250 or ident==IMU_ID:
            raise MaintenanceError('编号副地址无效',400)
        self.exchange(ident,3,bytes((SECONDARY_ID_ADDRESS,ident)))


def blank_imu(status="stopped", reason="尚未开始 IMU 测试"):
    return {"status": status, "reason": reason, "ready": False, "id": IMU_ID,
            "roll_deg": None, "pitch_deg": None, "gravity": None, "gyro": None,
            "quat": None, "sequence": None, "age_ms": None, "sample_age_ms": None,
            "refresh_hz": 0.0, "consecutive_stale_blocks": 0, "communication_errors": 0}


def decode_imu(raw):
    if len(raw) != 20 or raw[19] != 1 or raw[18] & ~1:
        raise MaintenanceError("IMU 诊断数据格式不匹配，请检查 FT6 固件", 409)
    gyro = [v * .0175 * math.pi / 180 for v in struct.unpack("<3h", raw[:6])]
    x, y, z = struct.unpack("<3e", raw[6:12])
    norm_sq = x*x + y*y + z*z
    if not all(math.isfinite(v) for v in (x, y, z)) or norm_sq > 1.02:
        raise MaintenanceError("IMU 四元数无效，等待新的融合数据", 409)
    w = math.sqrt(max(0.0, 1.0-norm_sq))
    norm = math.sqrt(w*w + norm_sq)
    w, x, y, z = (v/norm for v in (w, x, y, z))
    gravity = [2*(w*y-x*z), -2*(w*x+y*z), 2*(x*x+y*y)-1]
    roll = math.degrees(math.atan2(-gravity[1], -gravity[2]))
    pitch = math.degrees(math.atan2(gravity[0], math.hypot(gravity[1], gravity[2])))
    sequence, age = struct.unpack("<IH", raw[12:18])
    ready = bool(raw[18] & 1)
    return {"status": "live" if ready and age <= 50 else "waiting" if not ready else "stale",
            "reason": "传感器坐标系姿态" if ready and age <= 50 else "融合尚未就绪" if not ready else "IMU 样本已过期",
            "ready": ready and age <= 50, "id": IMU_ID, "roll_deg": roll,
            "pitch_deg": pitch, "gravity": gravity, "gyro": gyro, "quat": [w, x, y, z],
            "sequence": sequence, "age_ms": age, "sample_age_ms": 0.0}


def _protected_read(path, maximum=131072):
    try: before = path.lstat()
    except FileNotFoundError: return None
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
            or before.st_size > maximum):
        raise MaintenanceError("中位校准保护文件权限或格式不安全，未执行校准",409)
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        after = os.fstat(stream.fileno())
        if (after.st_ino,after.st_dev) != (before.st_ino,before.st_dev):
            raise MaintenanceError("中位校准保护文件已变化，未执行校准",409)
        value = stream.read(maximum+1)
    if len(value)>maximum: raise MaintenanceError("中位校准保护文件过长",409)
    return value


def _center_atomic(path, data):
    previous = path.lstat() if path.exists() else None
    fd,name = tempfile.mkstemp(prefix="."+path.name+"-",dir=path.parent)
    try:
        os.fchmod(fd,stat.S_IMODE(previous.st_mode) if previous else 0o644)
        if previous: os.fchown(fd,previous.st_uid,previous.st_gid)
        with os.fdopen(fd,"wb") as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def _center_profile_shape(data):
    names=("left_hip_yaw","left_hip_roll","left_hip_pitch","left_knee","left_ankle",
        "neck_pitch","head_pitch","head_yaw","head_roll","mouth",
        "right_hip_yaw","right_hip_roll","right_hip_pitch","right_knee","right_ankle")
    def finite(value):return type(value) in (int,float) and math.isfinite(value)
    for key in ("motion_enabled","imu_mount_verified","units_verified"):
        if key in data and type(data[key]) is not bool: return False
    quat=data.get("imu_mount_quat",[1,0,0,0])
    if (not isinstance(quat,list) or len(quat)!=4 or not all(finite(v) for v in quat)
            or abs(sum(v*v for v in quat)-1)>1e-6): return False
    if data.get("motion_enabled") and (not data.get("imu_mount_verified")
            or any(r.get("calibrated") is not True for r in data["joints"])):return False
    current=data.get("current_ma_per_count",0)
    if not finite(current) or (data.get("units_verified") and current<=0): return False
    for index,row in enumerate(data["joints"]):
        if row["name"]!=names[index]: return False
        lower,upper=row.get("min_rad"),row.get("max_rad")
        if lower is None and upper is None:
            if row.get("calibrated"):return False
        elif not (finite(lower) and finite(upper) and lower<upper):return False
        else:
            for angle in (lower,upper):
                raw=row["zero_raw"]+angle*row["direction"]*4096/math.tau
                if not 0<=math.copysign(math.floor(abs(raw)+.5),raw)<=4095:return False
    return True


def midpoint_invalidated_ids():
    if any(parent.is_symlink() for parent in (CENTER_DIRECTORY,*CENTER_DIRECTORY.parents)):
        raise MaintenanceError("中位保护目录不允许符号链接",409)
    existing_marker = _protected_read(CENTER_DIRECTORY/"hd1910-midpoint-invalidated.json",4096)
    marked = set()
    if existing_marker is not None:
        data = json.loads(existing_marker)
        ids = data.get("invalidated_ids") if isinstance(data,dict) else None
        if (not isinstance(data,dict) or set(data)!={"version","invalidated_ids"}
                or type(data["version"]) is not int or data["version"]!=1 or not isinstance(ids,list)
                or any(type(v) is not int or not 0<=v<254 or v==200 for v in ids)
                or len(set(ids))!=len(ids)):
            raise MaintenanceError("已有中位校准保护记录格式未知，未执行校准",409)
        marked.update(ids)
    return marked

def invalidate_center_calibration(ident):
    """Persist conservative flags before an origin-changing 0B; no zero rewrite.

    Only fixed root-owned paths and the fixed installed serializer are used.
    Extract its pure serializer functions rather than importing user/CWD code.
    Marker first, then saved/main; a failure never restores a calibrated flag.
    """
    directory = CENTER_DIRECTORY
    for parent in (directory,*directory.parents):
        if parent.is_symlink(): raise MaintenanceError("中位校准保护目录不允许符号链接",409)
    directory.mkdir(parents=True,exist_ok=True)
    meta = directory.stat()
    if meta.st_uid != 0 or meta.st_mode & 0o022:
        raise MaintenanceError("中位校准保护目录权限不安全",409)
    main = directory/"hd1910-calibration.toml"
    saved = directory/"hd1910-calibration.saved.toml"
    marker = directory/"hd1910-midpoint-invalidated.json"
    config_raw = _protected_read(directory/"robotd.toml")
    if config_raw is not None:
        config = tomllib.loads(config_raw.decode())
        if config.get("bus",{}).get("feetech_calibration") != str(main):
            raise MaintenanceError("当前服务标定路径不匹配，未执行中位校准",409)
    marked = midpoint_invalidated_ids()
    values=[]
    top = {"motion_enabled","imu_mount_verified","imu_mount_quat","joints","current_ma_per_count","units_verified"}
    joint = {"name","id","zero_raw","direction","calibrated","min_rad","max_rad"}
    for path in (saved,main):
        raw = _protected_read(path)
        if raw is None: continue
        original = tomllib.loads(raw.decode())
        rows = original.get("joints")
        if (set(original)-top or not isinstance(rows,list) or len(rows)!=15
                or any(not isinstance(r,dict) or set(r)-joint or type(r.get("id")) is not int
                       or not 0<=r["id"]<254 or r["id"]==200 or not isinstance(r.get("name"),str)
                       or type(r.get("zero_raw")) is not int or not 0<=r["zero_raw"]<=4095
                       or type(r.get("direction")) is not int or r["direction"] not in (-1,1)
                       or ("calibrated" in r and type(r["calibrated"]) is not bool) for r in rows)
                or len({r["id"] for r in rows})!=15 or len({r["name"] for r in rows})!=15):
            raise MaintenanceError("已有标定文件结构未知，未执行中位校准",409)
        if not _center_profile_shape(original):
            raise MaintenanceError("已有标定字段类型或数值未知，未执行中位校准",409)
        changed = copy.deepcopy(original)
        matches = [r for r in changed["joints"] if r["id"]==ident]
        if matches:
            matches[0]["calibrated"]=False;changed["motion_enabled"]=False
        values.append((path,original,changed))
    updates=[]
    need_saved = (not any(path==saved for path,_,_ in values)
        and any(path==main and any(r["id"]==ident for r in changed["joints"]) for path,_,changed in values))
    if need_saved or any(original!=changed for _,original,changed in values):
        if any(parent.is_symlink() for parent in CENTER_SERIALIZER.parents):
            raise MaintenanceError("固定标定组件路径不允许符号链接",409)
        source = _protected_read(CENTER_SERIALIZER)
        if source is None: raise MaintenanceError("固定标定序列化组件缺失，未执行中位校准",409)
        functions = [node for node in ast.parse(source).body
                     if isinstance(node,ast.FunctionDef) and node.name in ("key","atom","dumps")]
        if {node.name for node in functions}!={"key","atom","dumps"} or len(functions)!=3:
            raise MaintenanceError("固定标定序列化组件格式未知，未执行中位校准",409)
        namespace={"json":json,"math":math,"datetime":datetime}
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(CENTER_SERIALIZER),"exec"),namespace)
        for path,original,changed in values:
            if original==changed and not (need_saved and path==main): continue
            encoded=namespace["dumps"](changed).encode()
            if tomllib.loads(encoded.decode()) != changed:
                raise MaintenanceError("标定保护序列化校验失败，未执行中位校准",409)
            if need_saved and path==main:updates.insert(0,(saved,encoded))
            if original!=changed:updates.append((path,encoded))
    marked.add(ident)
    expected={"version":1,"invalidated_ids":sorted(marked)}
    _center_atomic(marker,(json.dumps(expected,sort_keys=True)+"\n").encode())
    if json.loads(_protected_read(marker,4096))!=expected:
        raise MaintenanceError("中位保护记录持久化读回不一致，未执行校准",503)
    for path,encoded in updates:
        _center_atomic(path,encoded)
        if tomllib.loads(_protected_read(path).decode())!=tomllib.loads(encoded.decode()):
            raise MaintenanceError("标定失效保护读回不一致，未执行校准",503)
    return {"invalidated_ids":sorted(marked),"calibration_files_updated":len(updates),
            "requires_new_capture":True,"marker_persisted":True}


class Session:
    def __init__(self, config, bus, service_check=robotd_state, audio_factory=None, clock=time.monotonic, calibration_invalidator=None):
        self.token, self.mode = config["token"], config["mode"]
        self.bus, self.service_check, self.clock = bus, service_check, clock
        self.started = self.last_seen = clock()
        self.last_service_check = 0.0
        self.service_probe=None
        self.ended = False
        self.end_reason = None
        self.error = ""
        self.servo_rows = []
        self.session_id = secrets.token_hex(16)  # Public identity, never the owner token.
        self.scan_progress = blank_scan()
        self.scan_phase = self.scan_row = None
        self.scan_identity_raw = None
        self.probe_conflicts = {}
        self.position_failed=set()
        self.position_due=0.0
        self.position_index=0
        self.numbering_evidence = {}
        self.chain = None
        self.receipt = None
        self.center_receipt = None
        self.calibration_invalidator = calibration_invalidator or invalidate_center_calibration
        self.imu_running, self.imu = False, blank_imu()
        self.imu_at = self.imu_tick = self.imu_last_sequence = None
        self.imu_errors = self.imu_stale = 0
        self.imu_times = collections.deque(maxlen=150)
        self.imu_read_times = collections.deque(maxlen=150)
        self.imu_sample_id = self.imu_duplicates = self.imu_sequence_gaps = 0
        self.audio = None
        self.audio_factory = audio_factory

    def audio_device(self):
        if self.audio is None:
            if self.audio_factory is None:
                from board_audio import BoardAudio
                self.audio_factory = BoardAudio
            self.audio = self.audio_factory()
        return self.audio

    def imu_snapshot(self, owned=False):
        imu = dict(self.imu)
        if self.imu_at is not None:
            imu["sample_age_ms"] = max(0.0, (self.clock()-self.imu_at)*1000)
            if self.clock()-self.imu_at > IMU_STALE_SECONDS:
                imu.update(status="stale", ready=False, reason="IMU 数据已停止更新")
                for key in ("roll_deg", "pitch_deg", "gravity", "gyro", "quat"):
                    imu[key] = None
        return {"active": not self.ended, "owned": owned,
                "mode": self.mode if not self.ended else "idle",
                "imu_running": self.imu_running and not self.ended, "imu": imu}

    def snapshot(self, owned=False):
        imu = self.imu_snapshot(owned)["imu"]
        audio = self.audio.status() if self.audio else {"microphone": {"state": "stopped"}, "speaker": {"state": "stopped"}}
        return {"available": True, "active": not self.ended, "owned": owned,
                "mode": self.mode if not self.ended else "idle", "robotd_running": False,
                "serial_available": False, "imu_running": self.imu_running,
                "imu": imu, "audio": audio, "devices": self.servo_rows,
                "chain": self.chain, "receipt": self.receipt, "center_receipt":self.center_receipt,
                "error": self.error,
                "end_reason": self.end_reason, "session_id":self.session_id,
                "scan":dict(self.scan_progress), "numbered_ids":sorted(self.numbering_evidence),
                "numbering_evidence":dict(self.numbering_evidence),
                "hub": {"automatic_detection": False, "message": "HUB 为被动供电分线板，请按实拍接线检查供电与连接"}}

    def _check_owner(self, request):
        token = request.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
            raise MaintenanceError("维护会话正由另一个中控使用", 409)
        self.last_seen = self.clock()

    @property
    def scan_running(self):
        return self.scan_progress["running"]

    def _remember_numbered(self, info, source):
        ident = info.get("id")
        if (type(ident) is int and ident in SERVO_IDS and info.get("kind") == "servo"
                and info.get("identity_verified") is True and info.get("register_id") == ident
                and not info.get("error")):
            self.numbering_evidence[ident] = {"id":ident, "source":source, "verified":True,
                "identity_verified":True, "register_id":ident, "checked_at":time.time()}

    @staticmethod
    def _identity(ident, version):
        info = {"id":ident, "kind":"unknown", "identity_verified":False,
                "firmware":list(version[:3]), "model_bytes":list(version[3:5])}
        if ident == IMU_ID:
            if version != IMU_IDENTITY:
                raise MaintenanceError("ID 200 身份与 FT6 IMU 不匹配，未认定为舵机或 IMU",409)
            info.update(kind="imu", identity_verified=True)
        else:
            if len(version) not in (6,8) or version[5] != ident:
                raise MaintenanceError("设备身份或编号读回不一致，未确认舵机身份",409)
            info.update(kind="servo", register_id=version[5])
            if len(version)==8:info['secondary_id']=version[7]
        return info

    def _probe(self, method, ident, *args):
        before=self.bus.bad_checksums
        diagnostic=getattr(self.bus,"scan_"+method,None)
        try:return (diagnostic or getattr(self.bus,method))(ident,*args)
        finally:
            reasons=self.probe_conflicts.setdefault(ident,[])
            detail=getattr(self.bus,"last_probe",None) if diagnostic else None
            codes=[]
            if self.bus.bad_checksums>before:codes.append("crc")
            if detail and detail.get("id")==ident:
                if detail.get("valid_replies",0)>1:codes.append("multi")
                if detail.get("inconsistent"):codes.append("identity")
                if detail.get("unexpected"):codes.append("reply")
            for code in codes:
                if code not in reasons and len(reasons)<2:reasons.append(code)

    def _conflict_row(self, info, ident):
        reasons=self.probe_conflicts.get(ident,[])
        if reasons:
            identified = {key:info[key] for key in ('id','kind','register_id') if key in info}
            info.clear()
            info.update(id=ident,kind="unknown",**{k:v for k,v in identified.items() if k not in ('id','kind')})
            info['kind'] = identified.get('kind','unknown')
            info.update(identity_verified=False,suspected_duplicate=True,
                conflict_reasons=list(dict.fromkeys(reasons))[:2],error=f"ID {ident} 疑似冲突")
        return info

    def _read_device_info(self, ident, diagnostics=False):
        read=(lambda address,width:self._probe("read",ident,address,width)) if diagnostics else (lambda address,width:self.bus.read(ident,address,width))
        raw=read(0,5 if ident==IMU_ID else 6)
        if diagnostics and read(0,len(raw))!=raw:
            self.probe_conflicts.setdefault(ident,[]).append("identity")
        info=self._identity(ident,raw)
        if ident!=IMU_ID:
            info["torque_on"]=read(TORQUE_ADDRESS,1)[0]!=0
            info["identity_verified"]=True
        return self._conflict_row(info,ident) if diagnostics else info

    def scan(self):
        """Start a read-only job; one bounded UART transaction per event-loop tick."""
        if self.scan_running:
            raise MaintenanceError("扫描正在进行，请先停止当前扫描",409)
        self.chain = self.receipt = None
        self.servo_rows = []
        self.scan_progress = blank_scan(self.scan_progress["generation"]+1)
        self.scan_progress.update(state="running",running=True,current_id=SCAN_START_ID,
                                  invalidation_reason="扫描尚未完成")
        self.scan_phase, self.scan_row = "ping", None
        self.scan_identity_raw=None
        self.probe_conflicts={}
        self.position_failed.clear()
        self.position_index=0
        self.position_due=0.0
        return dict(self.scan_progress)

    def stop_scan(self, error=""):
        if self.scan_running:
            self.scan_progress.update(state="cancelled",running=False,cancelled=True,
                completed=False,error=error,requires_rescan=True,
                invalidation_reason=error or "扫描已停止，未扫完的 ID 尚未检测")
            if self.scan_row is not None and not self.scan_row.get("identity_verified"):
                self.scan_row.setdefault("error","扫描已停止，设备身份尚未完成确认")
            self.scan_phase = self.scan_row = None
        return dict(self.scan_progress)

    def _finish_scan_id(self):
        progress = self.scan_progress
        progress["scanned"] += 1
        progress["last_completed_id"] = progress["current_id"]
        progress["found"] = len(self.servo_rows)
        progress["conflict_ids"] = sorted(r["id"] for r in self.servo_rows if r.get("suspected_duplicate"))
        progress["servo_count"] = sum(r.get("kind")=="servo" and r.get("identity_verified") is True
                                      and not r.get("error") for r in self.servo_rows)
        progress["imu_count"] = sum(r.get("kind")=="imu" and r.get("identity_verified") is True
                                    and not r.get("error") for r in self.servo_rows)
        self.scan_row = None
        self.scan_identity_raw = None
        if progress["current_id"] == SCAN_END_ID:
            progress.update(state="completed",running=False,completed=True,cancelled=False,
                            requires_rescan=bool(progress["conflict_ids"] or progress["error"]),
                            invalidation_reason="疑似编号/通信冲突，请核对；无提示也不能证明唯一" if progress["conflict_ids"] or progress["error"] else "")
            self.scan_phase = None
        else:
            progress["current_id"] += 1
            self.scan_phase = "ping"

    def _scan_step(self):
        ident=self.scan_progress["current_id"]
        try:
            if self.scan_phase=="ping":
                self.probe_conflicts[ident]=[]
                try:self._probe("ping",ident)
                except BusTimeout:
                    if self.probe_conflicts[ident]:
                        self.scan_progress['error']=f"探测 ID {ident} 时通信异常，未确认该地址有设备"
                    self._finish_scan_id();return
                self.scan_row={"id":ident,"kind":"unknown","identity_verified":False}
                self.servo_rows.append(self.scan_row)
                self.scan_progress["found"]=len(self.servo_rows)
                if self.probe_conflicts[ident]:
                    self._conflict_row(self.scan_row,ident);self._finish_scan_id();return
                self.scan_phase="identity"
            elif self.scan_phase=="identity":
                raw=self._probe("read",ident,0,5 if ident==IMU_ID else 8)
                self.scan_row.update(self._identity(ident,raw))
                self.scan_row["identity_verified"]=False
                self.scan_identity_raw=raw
                self.scan_phase="identity-confirm"
            elif self.scan_phase=="identity-confirm":
                raw=self._probe("read",ident,0,len(self.scan_identity_raw))
                if raw!=self.scan_identity_raw:self.probe_conflicts.setdefault(ident,[]).append("identity")
                if ident==IMU_ID:
                    self.scan_row["identity_verified"]=not self.probe_conflicts.get(ident)
                    self._conflict_row(self.scan_row,ident);self._finish_scan_id()
                else:self.scan_phase="torque"
            elif self.scan_phase=="torque":
                self.scan_row["torque_on"]=self._probe("read",ident,TORQUE_ADDRESS,1)[0]!=0
                self.scan_row["identity_verified"]=True
                self._conflict_row(self.scan_row,ident)
                self._remember_numbered(self.scan_row,"scan_identity")
                if self.scan_row.get('identity_verified'):self.scan_phase='position'
                else:self._finish_scan_id()
            elif self.scan_phase=='position':
                self._read_position(self.scan_row)
                self._finish_scan_id()
        except MaintenanceError as problem:
            if self.scan_row is None:
                self.scan_row={"id":ident,"kind":"unknown","identity_verified":False}
                self.servo_rows.append(self.scan_row)
            if isinstance(problem,BusTimeout) or self.scan_phase in ("identity","identity-confirm"):
                self.probe_conflicts.setdefault(ident,[]).append("reply")
            self.scan_row.update(identity_verified=False,error=str(problem)[:80])
            self._conflict_row(self.scan_row,ident)
            self._finish_scan_id()
        except Exception as problem:
            self.scan_progress.update(state="error",running=False,completed=False,cancelled=False,
                error="串口扫描已停止："+str(problem),requires_rescan=True,
                invalidation_reason="扫描通信失败，请检查串口和接线后重新扫描")
            if self.scan_row is not None:self.scan_row.update(identity_verified=False,error=self.scan_progress["error"])
            self.scan_phase=self.scan_row=None

    def _read_position(self, row):
        ident=row['id'];row['position_raw']=None
        before=self.bus.bad_checksums
        diagnostic=getattr(self.bus,'scan_read',None)
        try:
            raw=struct.unpack('<H',(diagnostic or self.bus.read)(ident,56,2))[0]
            value=-(raw&0x7fff) if raw&0x8000 else raw
            detail=getattr(self.bus,'last_probe',None) if diagnostic else None
            if (abs(value)>4095 or self.bus.bad_checksums!=before or detail and
                    (detail.get('valid_replies')!=1 or detail.get('inconsistent') or detail.get('unexpected'))):
                raise ValueError('position reply invalid')
            row['position_raw']=raw;row.pop('position_error',None)
        except (MaintenanceError,OSError,ValueError,struct.error):
            row['position_error']='读取失败';self.position_failed.add(ident)

    def _sample_position(self, now):
        if now<self.position_due:return
        due=self.position_due or now
        self.position_due=due+(int(max(0,now-due)/.02)+1)*.02
        rows=[r for r in self.servo_rows if r.get('kind')=='servo' and r.get('identity_verified') is True
              and r.get('id')!=IMU_ID and r.get('register_id')==r.get('id') and not r.get('error')
              and not r.get('suspected_duplicate') and r.get('id') not in self.position_failed]
        if not rows:return
        ids=[r['id'] for r in rows]
        try:
            values={i:struct.unpack('<H',v)[0] for i,v in self.bus.sync_read(ids).items()}
            if set(values)!=set(ids) or any((v&0x7fff)>4095 for v in values.values()):
                raise ValueError('position reply invalid')
        except (MaintenanceError,OSError,ValueError,struct.error) as problem:
            self.position_failed.update(getattr(problem,'detail',{}).get('failed_ids',ids))
            for row in rows:row.update(position_raw=None,position_error='读取失败')
        else:
            for row in rows:
                row['position_raw']=values[row['id']];row.pop('position_error',None)
        finished=self.clock()
        if self.position_due<=finished:self.position_due+=(int((finished-self.position_due)/.02)+1)*.02

    def _scan_for_write(self, diagnostics=False):
        """Fresh 0..253 check; never borrow the public scan or history."""
        rows=[]
        for ident in range(254):
            if diagnostics:self.probe_conflicts[ident]=[]
            try:
                if diagnostics:self._probe("ping",ident)
                else:self.bus.ping(ident)
            except BusTimeout:
                if diagnostics and self.probe_conflicts.get(ident):
                    raise MaintenanceError(f"探测 ID {ident} 时通信异常，未确认设备；未执行维护写入",409)
                continue
            except MaintenanceError as problem:
                info={"id":ident,"kind":"unknown","identity_verified":False,"error":str(problem)}
                rows.append(self._conflict_row(info,ident) if diagnostics else info);continue
            try:info=self._read_device_info(ident,diagnostics)
            except MaintenanceError as problem:
                info={"id":ident,"kind":"unknown","identity_verified":False,"error":str(problem)}
                if diagnostics:self.probe_conflicts.setdefault(ident,[]).append("reply")
            rows.append(self._conflict_row(info,ident) if diagnostics else info)
        return rows

    @staticmethod
    def _hls_profile(info):
        fw=info.get("firmware",[]);model=info.get("model_bytes",[])
        return len(fw)==3 and fw[0]==3 and 40<=fw[1]<=59 and fw[2]==0 and len(model)==2 and model[0]==10

    def _chain_write_candidate(self, rows, old, new):
        occupied=[r["id"] for r in rows if r["id"]==new and new!=old]
        if occupied:raise MaintenanceError(f"目标 ID {new} 已有回应，未执行维护写入",409)
        for info in rows:
            ident=info["id"]
            if info.get("suspected_duplicate"):
                raise MaintenanceError(f"ID {ident} 疑似重复/通信冲突，请只接单颗核对；未执行维护写入",409)
            if info.get("error") or not info.get("identity_verified"):
                raise MaintenanceError(f"ID {ident} 身份或回应未确认，未执行维护写入",409)
            if info.get("kind")=="imu" and ident==IMU_ID:continue
            if info.get("kind")!="servo" or info.get("register_id")!=ident or not self._hls_profile(info):
                raise MaintenanceError(f"ID {ident} 的副 ID 协议未确认，请单颗检查",409)
            info["secondary_id"]=self._probe('read',ident,7,1)[0]
            secondary=info["secondary_id"]
            if self.probe_conflicts.get(ident) or not 0<=secondary<=253 or (ident!=old and secondary in (old,new) and secondary!=ident):
                raise MaintenanceError(f"ID {ident} 的副 ID {secondary} 与本次写入地址冲突或回应异常，请单颗检查",409)
        candidate=next((r for r in rows if r["id"]==old and r.get("kind")=="servo"),None)
        if candidate is None:raise MaintenanceError(f'未发现有效的所选舵机 ID {old}，未执行维护写入',409)
        return candidate

    def _center_chain_candidate(self, selected, operation='中位校准'):
        p=self.scan_progress
        rows=self.servo_rows
        if not (self.mode=='numbering' and p.get('state')=='completed' and p.get('completed') is True
                and p.get('running') is False and p.get('requires_rescan') is False and p.get('generation',0)>0
                and p.get('scanned')==p.get('total')==250 and not p.get('error') and not p.get('conflict_ids')
                and len(rows)==p.get('found') and len({r.get('id') for r in rows})==len(rows)):
            raise MaintenanceError('请先完成有效扫描',409)
        candidate=next((r for r in rows if r['id']==selected and r.get('kind')=='servo'),None)
        if candidate is None:raise MaintenanceError(f'未发现有效的所选舵机 ID {selected}，{operation}未执行',409)
        for info in rows:
            ident=info['id']
            if info.get('kind')=='imu' and ident==IMU_ID and info.get('identity_verified') and not info.get('error'):continue
            secondary=info.get('secondary_id')
            if (info.get('suspected_duplicate') or info.get('error') or not info.get('identity_verified')
                    or info.get('register_id')!=ident or not self._hls_profile(info)
                    or type(secondary) is not int or not 0<=secondary<=253):
                raise MaintenanceError(f'ID {ident} 的副 ID 缓存未确认，请重新扫描；{operation}未执行',409)
            if ident!=selected and secondary==selected:
                raise MaintenanceError(f'ID {ident} 的副 ID {secondary} 与所选地址冲突，请单颗检查',409)
        return rows,candidate

    def _refresh_devices(self, rows, renamed=None):
        previous={r.get('id'):r for r in self.servo_rows}
        for row in rows:
            ident=(renamed or {}).get(row.get('id'),row.get('id'))
            old=previous.get(ident,{})
            if ('position_raw' not in row and 'position_error' not in row
                    and row.get('kind')==old.get('kind')=='servo'
                    and row.get('identity_verified') is True and old.get('identity_verified') is True
                    and row.get('register_id')==row.get('id') and old.get('register_id')==ident
                    and not row.get('error') and not old.get('error')
                    and not row.get('suspected_duplicate') and not old.get('suspected_duplicate')
                    and row.get('firmware')==old.get('firmware') and row.get('model_bytes')==old.get('model_bytes')):
                for key in ('position_raw','position_error'):
                    if key in old:row[key]=old[key]
        self.servo_rows=rows
        self.scan_progress.update(found=len(rows),
            servo_count=sum(r.get('kind')=='servo' and r.get('identity_verified') is True and not r.get('error') for r in rows),
            imu_count=sum(r.get('kind')=='imu' and r.get('identity_verified') is True and not r.get('error') for r in rows),
            conflict_ids=sorted(r['id'] for r in rows if r.get('suspected_duplicate')))

    def _require_stopped(self, known=False):
        state=known if known is not False else self.service_probe.state() if self.service_probe is not None else False
        if state is False:state=self.service_probe.fresh() if self.service_probe is not None else self.service_check()["robotd_running"]
        if state is not False:
            self.ended = True
            self.end_reason = "robotd_started" if state is True else 'service_state_unknown'
            self.error = "机器人服务已启动，维护写入已取消" if state is True else '服务状态未知，维护写入已取消'
            raise MaintenanceError(self.error, 409, reason_code=self.end_reason)

    def _write_checked(self, ident, address, value):
        # Lost acknowledgement is resolved by a read, never by repeating the write.
        self._require_stopped()
        try: self.bus.write(ident, address, value)
        except BusTimeout: pass
        if self.bus.read(ident, address, 1) != bytes((value,)):
            raise MaintenanceError(f"ID {ident} 参数读回不一致，操作已停止", 503)

    def _swap_absent(self, ident):
        self.probe_conflicts[ident]=[]
        try:self._probe('ping',ident)
        except BusTimeout:
            if not self.probe_conflicts.get(ident):return
        raise MaintenanceError(f'ID {ident} 仍有回应或通信冲突，请重新扫描核对',409)

    def _swap_identity(self, ident, cached=None):
        self.probe_conflicts[ident]=[]
        info=self._read_device_info(ident,True)
        info['secondary_id']=self._probe('read',ident,7,1)[0]
        info['lock_value']=self._probe('read',ident,LOCK_ADDRESS,1)[0]
        if (info.get('kind')!='servo' or not info.get('identity_verified')
                or info.get('register_id')!=ident or info.get('suspected_duplicate')
                or info.get('error') or not self._hls_profile(info) or self.probe_conflicts.get(ident)):
            raise MaintenanceError(f'ID {ident} 身份或通信未确认，未继续互换',409)
        if cached is not None and any(info.get(k)!=cached.get(k) for k in ('firmware','model_bytes','secondary_id')):
            raise MaintenanceError(f'ID {ident} 身份已变化，请重新扫描后互换',409)
        self._read_position(info)
        return info

    def _numbering_readback(self, ident):
        if (self._probe('read',ident,ID_ADDRESS,1)!=bytes((ident,))
                or self.probe_conflicts.get(ident)):
            raise MaintenanceError(f'ID {ident} 主编号未通过读回',503)
        secondary=self._probe('read',ident,SECONDARY_ID_ADDRESS,1)
        if self.probe_conflicts.get(ident):
            raise MaintenanceError(f'ID {ident} 副编号回应异常',503)
        if secondary!=bytes((ident,)):
            self._require_stopped()
            try:self.bus._numbering_secondary(ident)
            except BusTimeout:pass  # Read to resolve a lost ACK; never replay.
            if (self._probe('read',ident,SECONDARY_ID_ADDRESS,1)!=bytes((ident,))
                    or self.probe_conflicts.get(ident)):
                raise MaintenanceError(f'ID {ident} 副编号未通过读回',503)
        return ident

    def swap_ids(self, old, new):
        """One explicit, verified three-move exchange; no ID write is replayed."""
        steps=[];invalidated=[];attempts=0;temporary=None;calibration_started=False
        receipt={'old_id':old,'new_id':new,'target_old_id':new,'target_new_id':old,
            'swapped':False,'verified':False,'temporary_id':None,'completed_steps':steps,
            'needs_recalibration_ids':invalidated,'id_write_attempts':0,'uncertain':False}
        self.receipt=receipt
        try:
            rows,candidate=self._center_chain_candidate(old,'编号互换')
            target=next((r for r in rows if r.get('id')==new and r.get('kind')=='servo'),None)
            if old==1:
                raise MaintenanceError('ID 1 请直接保存到空闲关节编号；目标已占用时请先选择其他空闲编号',409)
            if old==new or target is None:
                raise MaintenanceError('互换目标必须是另一颗已扫描且身份确认的舵机',409)
            reserved={r['id'] for r in rows}|{r.get('secondary_id') for r in rows}|{IMU_ID}
            temporary=next((i for i in range(SCAN_END_ID,SCAN_START_ID-1,-1)
                if i not in reserved and i not in SERVO_IDS),None)
            if temporary is None:raise MaintenanceError('没有可用临时编号，未执行互换',409)
            receipt['temporary_id']=temporary
            for row in rows:
                if row.get('kind')=='servo' and row.get('secondary_id') in (old,new) and row['secondary_id']!=row['id']:
                    raise MaintenanceError(f"ID {row['id']} 的副 ID 与互换地址冲突，请重新检查",409)
            first=self._swap_identity(old,candidate);second=self._swap_identity(new,target)
            self._swap_absent(temporary)
            # All identity/topology checks precede calibration or register writes.
            for ident in (old,new):
                if ident in SERVO_IDS:
                    calibration_started=True
                    self.calibration_invalidator(ident);invalidated.append(ident)
            self.scan_progress.update(requires_rescan=True,invalidation_reason='编号互换尚未完成')
            def move(source,destination):
                nonlocal attempts
                self._write_checked(source,TORQUE_ADDRESS,0)
                self._write_checked(source,LOCK_ADDRESS,0)
                self._swap_absent(destination)
                self._require_stopped();attempts+=1;receipt['id_write_attempts']=attempts
                if self.center_receipt and self.center_receipt.get('servo_id') in (old,new):self.center_receipt=None
                try:self.bus._swap_id(source,destination,old,new,temporary)
                except BusTimeout:pass
                time.sleep(.02)
                self._numbering_readback(destination)
                self._write_checked(destination,LOCK_ADDRESS,1)
                if self._probe('read',destination,ID_ADDRESS,1)!=bytes((destination,)):
                    raise MaintenanceError(f'ID {destination} 锁定后编号未确认',503)
                self._swap_absent(source)
                steps.append({'from_id':source,'to_id':destination,'verified':True,'relocked':True})
            move(old,temporary);move(new,old);move(temporary,new)
            left=self._swap_identity(new);right=self._swap_identity(old)
            for moved,original in ((left,first),(right,second)):
                if any(moved.get(k)!=original.get(k) for k in ('firmware','model_bytes')):
                    raise MaintenanceError('互换后设备身份未确认',503)
                if moved.get('secondary_id')!=moved['id'] or moved.get('torque_on') is not False:
                    raise MaintenanceError('互换后的副 ID 或卸力状态未确认',503)
            self._swap_absent(temporary)
            self._refresh_devices([left if r['id']==old else right if r['id']==new else r for r in rows])
            self.position_failed.difference_update((old,new,temporary))
            for ident in (old,new):self.numbering_evidence.pop(ident,None)
            self._remember_numbered(left,'verified_swap');self._remember_numbered(right,'verified_swap')
            self.scan_progress.update(requires_rescan=False,invalidation_reason='')
            receipt.update(swapped=True,verified=True,steps=list(steps),uncertain=False,
                needs_power_cycle_verification=True,message=f'已互换 ID {old} 与 ID {new}，两颗均已锁定并读回；对应关节需重新标定')
        except Exception as problem:
            receipt.update(uncertain=attempts>0,error=str(problem),id_write_attempts=attempts,
                precheck_failed=not calibration_started and attempts==0)
            self.scan_progress.update(requires_rescan=True,invalidation_reason='编号互换未完成，请手动扫描确认')
            # Recovery observes only these addresses. It never rolls back or
            # repeats an ID write after an uncertain result.
            recovery=[]
            for ident in dict.fromkeys((old,new,temporary)):
                if ident is None:continue
                try:info=self._swap_identity(ident)
                except Exception:recovery.append({'id':ident,'confirmed':False})
                else:recovery.append({'id':ident,'confirmed':True,**{k:info.get(k) for k in
                    ('position_raw','register_id','secondary_id','lock_value','torque_on')}})
            receipt['recovery']=recovery
            observed='、'.join(str(r['id']) for r in recovery if r['confirmed']) or '尚未确认'
            completed='、'.join(f"{r['from_id']}→{r['to_id']}" for r in steps) or '无'
            self.error=(f'编号互换未完成；临时 ID {temporary if temporary is not None else "未分配"}；'
                f'已完成 {len(steps)}/3 步（{completed}）；已确认当前 ID：{observed}。'
                '未自动重试或回滚，请手动扫描核对。'+str(problem))
            receipt['message']=self.error

    def write_id(self, request):
        self.chain = self.receipt = None
        self._require_stopped()
        old, new = request.get("old_id"), request.get("new_id")
        if type(old) is not int or not 0 <= old < 254 or old == IMU_ID or type(new) is not int or new not in SERVO_IDS:
            raise MaintenanceError("请输入有效原 ID，并选择本机 15 个目标关节 ID", 400)
        confirmations=set(request)&{'selected_servo_confirmed','unique_id1_confirmed','single_servo_confirmed'}
        if (len(confirmations)!=1 or request[next(iter(confirmations))] is not True
                or 'unique_id1_confirmed' in confirmations and old!=1):
            raise MaintenanceError('请选择一种编号确认方式并确认所选舵机；新旧确认方式不能混用',400)
        if 'swap' in request:
            if request['swap'] is not True or confirmations!={'selected_servo_confirmed'}:
                raise MaintenanceError('互换编号需要明确选择当前舵机和已占用目标编号',400)
            self.swap_ids(old,new);return
        chain='single_servo_confirmed' not in confirmations
        self.scan_progress.update(requires_rescan=True,
            invalidation_reason="编号写入正在进行独立安全核验，请以写后读回或重新扫描为准")
        rows=self._scan_for_write(diagnostics=True)
        if chain:candidate=self._chain_write_candidate(rows,old,new)
        else:
            if (len(rows)!=1 or rows[0]["id"]!=old or rows[0].get("error") or rows[0].get("suspected_duplicate")
                    or rows[0].get("kind")!="servo" or rows[0].get("identity_verified") is not True
                    or rows[0].get("register_id")!=old):
                raise MaintenanceError("当前检测结果不是指定的健康单颗舵机，请只接单颗并排查疑似重复 ID",409)
            candidate=rows[0]
        if old == new:
            self.receipt = {"old_id": old, "new_id": new, "verified": True, "unchanged": True,
                            "needs_power_cycle_verification": True, "message": "当前编号已匹配；请重新上电后复查"}
            self._remember_numbered(candidate,"verified_write")
            self._refresh_devices(rows)
            self.scan_progress.update(requires_rescan=False,invalidation_reason="")
            return
        self._write_checked(old, TORQUE_ADDRESS, 0)
        self._write_checked(old, LOCK_ADDRESS, 0)
        try:
            try:
                if (old in midpoint_invalidated_ids() or (self.center_receipt and self.center_receipt.get("verified")
                        and self.center_receipt.get("servo_id")==old)):
                    self.calibration_invalidator(new)
            except Exception as problem:
                raise MaintenanceError("中位后的新编号标定保护未确认，未写入 ID："+str(problem),503) from None
            self._require_stopped()
            if chain:
                try:self._probe("ping",new)
                except BusTimeout:
                    if self.probe_conflicts.get(new):raise MaintenanceError(f"目标 ID {new} 回应异常，未写入编号",409)
                else:raise MaintenanceError(f"目标 ID {new} 已被占用，未写入编号",409)
            try: self.bus.write(old, ID_ADDRESS, new, accepted_ids=(old, new))
            except BusTimeout: pass
            time.sleep(.02)
            self._numbering_readback(new)
        except Exception as problem:
            try:
                if not self.ended and self.bus.read(old, ID_ADDRESS, 1) == bytes((old,)):
                    self._write_checked(old, LOCK_ADDRESS, 1)
            except Exception: pass
            raise MaintenanceError("编号写入结果尚未确认；没有重发写入。请重新检测，必要时断电复查：" + str(problem),
                                   503, uncertain=True) from None
        try:
            self._write_checked(new, LOCK_ADDRESS, 1)
            if self.bus.read(new, ID_ADDRESS, 1) != bytes((new,)):
                raise MaintenanceError('编号锁定后读回失败',503)
            try:self._probe('ping',old)
            except BusTimeout:
                if self.probe_conflicts.get(old):raise MaintenanceError(f'旧 ID {old} 回应异常',503)
            else:raise MaintenanceError(f'旧 ID {old} 仍有回复，疑似同编号设备',503)
            secondary=self.bus.read(new,SECONDARY_ID_ADDRESS,1)[0]
            if secondary!=new:raise MaintenanceError('新主 ID 已读回，但副 ID 未同步确认',503)
        except Exception as problem:
            raise MaintenanceError('编号写后状态尚未确认；没有重发写入，请重新扫描：'+str(problem),503,uncertain=True) from None
        updated = {**candidate, "id":new,"register_id":new,"torque_on":False}
        updated["secondary_id"]=secondary
        self._refresh_devices([updated if row.get('id')==old else row for row in rows],renamed={new:old})
        if old in self.position_failed:self.position_failed.discard(old);self.position_failed.add(new)
        self.numbering_evidence.pop(old,None)
        self._remember_numbered(updated,"verified_write")
        self.scan_progress.update(requires_rescan=False,invalidation_reason="")
        self.receipt = {"old_id": old, "new_id": new, "verified": True,
                        "needs_power_cycle_verification": True,
                        "message": f"已写入 ID {new} 并锁定、读回确认；请重新上电后复查"}

    def calibrate_center(self, request):
        self.center_receipt = None
        if 'unique_id1_confirmed' in request:
            raise MaintenanceError('编号确认不能用于中位校准，请单独确认所选舵机',400)
        ident = request.get("servo_id")
        selected='selected_servo_confirmed' in request
        single='single_servo_confirmed' in request
        if (type(ident) is not int or not 0<=ident<254 or ident==IMU_ID
                or selected==single or request.get('selected_servo_confirmed' if selected else 'single_servo_confirmed') is not True
                or request.get("center_confirmed") is not True):
            raise MaintenanceError('中位校准需要确认所选 HD1910，并确认以当前位置设为 2048；确认方式不能混用',400)
        self._require_stopped()
        self.center_receipt={'servo_id':ident,'verified':False,'command_sent':False,'command_count':0,
                             'uncertain':False,'precheck_failed':True}
        if selected:rows,info=self._center_chain_candidate(ident)
        else:
            rows=self._scan_for_write()
            if (len(rows)!=1 or rows[0].get('id')!=ident or rows[0].get('kind')!='servo'
                    or not rows[0].get('identity_verified') or rows[0].get('error')):
                raise MaintenanceError('旧版单颗确认只允许指定单颗舵机；中位校准未执行',409)
            info=rows[0]
        fw=info["firmware"]
        if not (fw[0]==3 and 43<=fw[1]<=59 and fw[2]==0 and info["model_bytes"][0]==10):
            raise MaintenanceError("舵机固件不在已核对的 HLS 3.43–3.59 小端兼容范围，中位校准未执行",409)
        receipt={"servo_id":ident,"verified":False,"unchanged":False,"uncertain":False,"error":"",
            "command_sent":False,"command_count":0,"ack_received":None,"relocked":False,"torque_off":False,
            "verified_tolerance_counts":0,"needs_power_cycle_verification":True,"needs_recalibration":False,
            "firmware":fw,"model_bytes":info["model_bytes"],"compatibility":"HLS-compatible; operator-confirmed HD1910"}
        self.center_receipt=receipt
        unlock_attempted=False;problem=None
        def read_pair(prefix):
            for name,address in (("position",56),("offset",31)):
                raw=struct.unpack("<H",self.bus.read(ident,address,2))[0]
                value=-(raw&0x7fff) if raw&0x8000 else raw
                if abs(value)>4095 and (prefix!="before" or name!="position"):raise MaintenanceError("中位校准读回数值超出单圈范围",503)
                receipt[prefix+"_"+name+"_raw"]=raw;receipt[prefix+"_"+name]=value
        try:
            self.chain=None
            self.scan_progress.update(requires_rescan=True,invalidation_reason="中位校准结果尚未确认")
            self._write_checked(ident,TORQUE_ADDRESS,0)
            unlock_attempted=True;self._write_checked(ident,LOCK_ADDRESS,0)
            read_pair("before")
            receipt["unchanged"]=receipt["before_position"]==2048
            if not receipt["unchanged"]:
                receipt["needs_recalibration"]=True
                receipt["calibration_invalidation"]=self.calibration_invalidator(ident)
                self._require_stopped()
                receipt.update(command_sent=True,command_count=1,needs_recalibration=True)
                try:self.bus.calibrate_center(ident);receipt["ack_received"]=True
                except BusTimeout:receipt["ack_received"]=False
                time.sleep(.02)
            read_pair("after")
        except Exception as exc:problem=exc
        finally:
            if unlock_attempted and not self.ended:
                try:
                    self._require_stopped()
                    try:self.bus.write(ident,LOCK_ADDRESS,1)
                    except BusTimeout:pass
                    receipt["relocked"]=self.bus.read(ident,LOCK_ADDRESS,1)==b"\x01"
                    receipt["torque_off"]=self.bus.read(ident,TORQUE_ADDRESS,1)==b"\x00"
                    read_pair("relocked_after")
                except Exception as exc:
                    if problem is None:problem=exc
        verified=(problem is None and receipt["relocked"] and receipt["torque_off"]
            and receipt.get("after_position")==2048 and receipt.get("relocked_after_position")==2048
            and receipt.get("after_offset")==receipt.get("relocked_after_offset")
            and (receipt["unchanged"] or receipt.get("after_offset")!=receipt.get("before_offset")))
        receipt["verified"]=verified
        if not verified:
            receipt["uncertain"]=receipt["command_sent"]
            receipt["error"]=("中位校准执行结果尚未确认，未自动重发 0x0B；请先重新读取位置与偏移，必要时断电复查"
                if receipt["command_sent"] else "中位校准未执行；请检查设备与标定保护后重试")
            if not receipt["relocked"]:receipt["error"]+="；EEPROM 回锁状态未确认"
            if problem is not None:receipt["error"]+="："+str(problem)
            raise MaintenanceError(receipt["error"],503,uncertain=receipt["uncertain"],center_receipt=receipt)
        info.update(position_raw=receipt['after_position_raw'],offset_raw=receipt['after_offset_raw'],torque_on=False)
        info.pop('position_error',None);self.position_failed.discard(ident)
        if selected:self.scan_progress.update(requires_rescan=False,invalidation_reason="")
        else:self._refresh_devices(rows)
        return receipt

    def verify_chain(self):
        self.chain = None
        missing, checked = [], []
        for ident in SERVO_IDS:
            try:
                self.bus.ping(ident)
                info = self._read_device_info(ident)
                if info.get("kind") != "servo" or not info.get("identity_verified"):
                    raise MaintenanceError("舵机身份或编号读回不匹配")
                self._remember_numbered(info,"chain_identity")
            except MaintenanceError:
                missing.append(ident)
            else:
                checked.append(ident)
        try: imu_present = self.bus.read(IMU_ID, 0, 5) == IMU_IDENTITY
        except MaintenanceError: imu_present = False
        self.chain = {"numbering_verified": not missing and imu_present, "missing_ids": missing,
                      "imu_present": imu_present, "checked_ids": checked,
                      "checked_at": time.time()}

    def _read_imu(self):
        started = self.clock()
        raw = self.bus.read(IMU_ID, IMU_DIAGNOSTIC_ADDRESS, IMU_DIAGNOSTIC_LENGTH)
        sample = decode_imu(raw)
        sample["transaction_ms"] = (self.clock()-started)*1000
        if sample["age_ms"] + sample["transaction_ms"] > 50:
            sample.update(status="stale", ready=False, reason="IMU 样本已过期")
        self.imu_sample_id += 1
        self.imu_read_times.append(self.clock())
        if sample["sequence"] == self.imu_last_sequence:
            self.imu_stale += 1
            self.imu_duplicates += 1
        else:
            self.imu_stale = 0
            self.imu_times.append(self.clock())
            if self.imu_last_sequence is not None:
                delta = (sample["sequence"] - self.imu_last_sequence) & 0xffffffff
                if 1 < delta < 0x80000000:
                    self.imu_sequence_gaps += delta-1
        self.imu_last_sequence = sample["sequence"]
        if self.imu_stale >= 3:
            sample.update(status="stale", ready=False, reason="IMU 样本序号停止更新")
        if sample["status"] != "live":
            for key in ("roll_deg", "pitch_deg", "gravity", "gyro", "quat"):
                sample[key] = None
        rate = ((len(self.imu_times)-1)/(self.imu_times[-1]-self.imu_times[0])
                if len(self.imu_times) > 1 and self.imu_times[-1] > self.imu_times[0] else 0.0)
        poll_rate = ((len(self.imu_read_times)-1)/(self.imu_read_times[-1]-self.imu_read_times[0])
                     if len(self.imu_read_times) > 1 and self.imu_read_times[-1] > self.imu_read_times[0] else 0.0)
        sample.update(sample_id=self.imu_sample_id, refresh_hz=rate, poll_hz=poll_rate,
                      target_hz=1/IMU_POLL_SECONDS, stream_hz=1/IMU_STREAM_SECONDS,
                      duplicate_reads=self.imu_duplicates, sequence_gap_total=self.imu_sequence_gaps,
                      sequence_gap_note="FT6 序号跨越包含未轮询的融合样本，不等于串口丢包",
                      consecutive_stale_blocks=self.imu_stale,
                      communication_errors=self.imu_errors, bad_checksums=self.bus.bad_checksums)
        self.imu, self.imu_at = sample, self.clock()

    def tick(self):
        if self.ended:return
        now = self.clock()
        if now-self.last_seen > LEASE_SECONDS or now-self.started > MAX_SESSION_SECONDS:
            self.error = "维护会话已到期，串口已释放"
            self.end_reason = "lease_expired" if now-self.last_seen > LEASE_SECONDS else "session_time_limit"
            self.ended = True
            self.stop_scan(self.error)
            return
        state=False
        if self.service_probe is not None:state=self.service_probe.state()
        elif now-self.last_service_check >= .5:
            self.last_service_check = now
            state=self.service_check()["robotd_running"]
        if state is not False:
            self.ended = True
            self.end_reason = "robotd_started" if state is True else 'service_state_unknown'
            self.error = "机器人服务已启动，维护测试已停止" if state is True else '无法确认机器人服务状态，维护测试已停止'
            self.stop_scan(self.error)
            return
        if self.scan_running:
            self._scan_step()
        elif (self.mode=='numbering' and self.scan_progress.get('completed') is True
                and self.scan_progress.get('requires_rescan') is False):
            self._sample_position(now)
        if self.imu_running and (self.imu_tick is None or now >= self.imu_tick):
            due = now if self.imu_tick is None else self.imu_tick
            self.imu_tick = due + (int(max(0, now-due)/IMU_POLL_SECONDS)+1)*IMU_POLL_SECONDS
            try: self._read_imu()
            except Exception as problem:
                self.imu_errors += 1
                self.imu_sample_id += 1
                self.imu = blank_imu("error", str(problem))
                self.imu.update(sample_id=self.imu_sample_id, communication_errors=self.imu_errors,
                                bad_checksums=self.bus.bad_checksums, target_hz=1/IMU_POLL_SECONDS,
                                stream_hz=1/IMU_STREAM_SECONDS)
                self.imu_at = self.clock()
            finished = self.clock()
            if self.imu_tick <= finished:
                self.imu_tick += (int((finished-self.imu_tick)/IMU_POLL_SECONDS)+1)*IMU_POLL_SECONDS

    def handle(self, request):
        action = request.get("action")
        if self.ended and action != "status":
            raise MaintenanceError((self.error or "维护会话已结束")+"；本次操作未执行，请重新进入", 409,
                                   reason_code=self.end_reason or "session_ended")
        if action == "status":
            owned = isinstance(request.get("token"), str) and secrets.compare_digest(request["token"], self.token)
            if owned: self.last_seen = self.clock()
            return self.snapshot(owned)
        self._check_owner(request)
        if action!='end' and self.service_probe is not None:
            known=self.service_probe.state()
            if known is not False:self._require_stopped(known)
        if self.scan_running and action not in ("scan-stop","end"):
            raise MaintenanceError("扫描正在进行，请先停止扫描再执行其他维护操作",409,
                                   reason_code="scan_running")
        self.error = ""
        if action == "end":
            self.close()
        elif action in ("scan", "scan-stop", "write", "verify-chain", "center-calibrate"):
            if self.mode != "numbering":
                raise MaintenanceError("编号操作需要先进入编号维护模式", 409)
            self.imu_running = False
            if action == "scan": self.scan()
            elif action == "scan-stop": self.stop_scan()
            elif action == "write": self.write_id(request)
            elif action == "center-calibrate": self.calibrate_center(request)
            else: self.verify_chain()
        elif action == "imu-start":
            if self.mode != "hardware-test": raise MaintenanceError("请先进入板卡测试模式", 409)
            if self.bus.read(IMU_ID, 0, 5) != IMU_IDENTITY:
                raise MaintenanceError("检测到的设备不是当前 FT6 IMU，请核对固件与接线", 409)
            self.imu_errors = self.imu_stale = 0
            self.imu_last_sequence = None
            self.imu_times.clear()
            self.imu_read_times.clear()
            self.imu_duplicates = self.imu_sequence_gaps = 0
            self.imu = blank_imu("waiting", "等待 IMU 新鲜融合数据")
            self.imu_running, self.imu_tick = True, None
            self._read_imu()
            self.imu_tick = self.clock()+IMU_POLL_SECONDS
        elif action == "imu-stop":
            self.imu_running, self.imu_at = False, None
            self.imu = blank_imu()
        elif action in ("mic-start", "mic-stop", "duck-play", "duck-stop"):
            if self.mode != "hardware-test": raise MaintenanceError("请先进入板卡测试模式", 409)
            method = {"mic-start": "start_microphone", "mic-stop": "stop_microphone",
                      "duck-play": "play_duck", "duck-stop": "stop_speaker"}[action]
            getattr(self.audio_device(), method)()
        else:
            raise MaintenanceError("不支持的维护操作", 400)
        result=self.snapshot(True)
        if action=='write' and request.get('swap') is True and self.receipt and not self.receipt.get('verified'):
            result['accepted']=False
        return result

    def close(self):
        self.end_reason = self.end_reason or "ended"
        self.stop_scan(self.error or "维护会话已结束")
        self.numbering_evidence.clear()
        self.ended, self.imu_running, self.imu_at = True, False, None
        self.imu = blank_imu()
        if self.service_probe is not None:self.service_probe.close()
        if self.audio:
            self.audio.close()


def send_request(request, path=SOCKET_PATH, timeout=8):
    encoded = (json.dumps(request, ensure_ascii=False)+"\n").encode()
    if len(encoded) > 8192: raise MaintenanceError("维护请求过长", 400)
    action = request.get("action")
    uncertain = action != "status"
    if action == "write":
        advice = "；编号写入结果尚未确认，未自动重发。请先重新检测编号并确认实际结果，再决定是否操作"
    elif action == "center-calibrate":
        advice = "；中位结果尚未确认，未自动重发，请勿再次校准，先读回位置与偏移"
    elif action == "status":
        advice = "；当前维护状态未确认，未自动重发，请检查连接后重新查询"
    else:
        advice = "；操作执行结果尚未确认，未自动重发，请先重新查询或检测实际状态"
    def failure(message, reason):
        return MaintenanceError(message+advice, 503, reason_code=reason, uncertain=uncertain)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(path)
            connection.sendall(encoded)
            with connection.makefile("rb") as reader: raw = reader.readline(MAX_REPLY_BYTES+1)
    except (FileNotFoundError, ConnectionRefusedError):
        raise
    except (socket.timeout, TimeoutError):
        raise failure("等待维护回执超时", "maintenance_reply_timeout") from None
    except OSError:
        raise failure("维护连接在返回回执前中断", "maintenance_reply_disconnected") from None
    if not raw:
        raise failure("维护程序关闭了连接，未收到回执", "maintenance_reply_eof")
    if len(raw) > MAX_REPLY_BYTES:
        raise failure("维护回执超过 64 KiB 长度限制", "maintenance_reply_too_large")
    if not raw.endswith(b"\n"):
        raise failure("维护回执在结束前断开（收到 %d 字节）" % len(raw), "maintenance_reply_truncated")
    try: result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise failure("维护回执不是有效 JSON", "maintenance_reply_json") from None
    if not isinstance(result, dict):
        raise failure("维护回执不是对象", "maintenance_reply_type")
    return result


def serve_session(session, server):
    """One UART owner; nonblocking bounded clients cannot delay the sampler.

    Subscribers do not renew the lease. Only authenticated control/status calls
    do so; a forgotten SSH stream therefore still expires and releases the bus.
    """
    clients = {}
    if session.mode in ('hardware-test','numbering'):session.service_probe=ServiceProbe(lambda:session.service_check())
    def drop(connection):
        clients.pop(connection, None)
        connection.close()
    def encoded(value):
        return (json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n").encode()
    def queue_reply(row, value):
        row["output"] = encoded(value)
        row["closing"] = True
        row["reply_deadline"] = session.clock()+REPLY_FLUSH_SECONDS
    def account_busy(started, owner=None):
        elapsed = max(0.0, session.clock()-started)
        for connection, pending in clients.items():
            if connection is owner or pending["subscribed"]: continue
            if pending["closing"]: pending["reply_deadline"] += elapsed
            else: pending["receive_deadline"] += elapsed
    try:
        while True:
            if not session.ended:
                started = session.clock()
                try: session.tick()
                finally: account_busy(started)
            if session.ended:
                for connection, row in list(clients.items()):
                    if row["subscribed"]:
                        drop(connection)
                    elif not row["closing"]:
                        queue_reply(row, {"error":session.error or "维护会话已结束，请重新进入",
                            "code":409, "reason_code":session.end_reason or "session_ended"})
                if not clients: break
            now = session.clock()
            for connection, row in list(clients.items()):
                if row["closing"]:
                    if now >= row["reply_deadline"]: drop(connection)
                    continue
                if row["subscribed"] and now-row["accepted"] > IMU_STREAM_LIFETIME:
                    drop(connection); continue
                if row["subscribed"]:
                    if not session.imu_running or session.ended:
                        drop(connection); continue
                    elif not row["output"] and now >= row["emitted"]:
                        sample_id = session.imu.get("sample_id")
                        if sample_id != row["sample_id"]:
                            if row["sample_id"] is not None:
                                row["coalesced"] += max(0, sample_id-row["sample_id"]-1)
                            row["sample_id"] = sample_id
                            due = row["emitted"] or now
                            row["emitted"] = due+(int(max(0,now-due)/IMU_STREAM_SECONDS)+1)*IMU_STREAM_SECONDS
                            row["output"] = encoded({"kind":"imu", **session.imu_snapshot(True),
                                                     "stream_coalesced_frames":row["coalesced"]})
                    if row["output"] and now-row["last_sent"] > .6:
                        drop(connection)
                elif now >= row["receive_deadline"]:
                    queue_reply(row, {"error":"维护请求未在 2 秒内完整送达，未执行操作",
                                      "code":408, "reason_code":"maintenance_request_timeout"})
            timeout = 0.0 if getattr(session,"scan_running",False) else .02
            if session.imu_running and session.imu_tick is not None:
                timeout = max(0.0, min(timeout, session.imu_tick-session.clock()))
            if (not session.ended and session.mode=='numbering' and session.scan_progress.get('completed') is True
                    and session.scan_progress.get('requires_rescan') is False):
                timeout=max(0.0,min(timeout,session.position_due-session.clock()))
            readers = ([] if session.ended else [server])+[c for c,r in clients.items() if not r["closing"]]
            readable, writable, _ = select.select(readers,
                [c for c,r in clients.items() if r["output"]], [], timeout)
            if server in readable:
                for _ in range(8):
                    try: connection, _ = server.accept()
                    except BlockingIOError: break
                    if len(clients) >= 16:
                        connection.close(); continue
                    connection.setblocking(False)
                    clients[connection] = {"input":bytearray(), "output":b"", "subscribed":False,
                        "accepted":session.clock(), "emitted":0.0, "sample_id":None,
                        "coalesced":0, "last_sent":session.clock(), "closing":False,
                        "reply_deadline":None,
                        "receive_deadline":session.clock()+REQUEST_RECEIVE_SECONDS}
            for connection in readable:
                if connection is server or connection not in clients: continue
                row = clients[connection]
                try:
                    raw = connection.recv(8193)
                    if not raw: drop(connection); continue
                    if row["subscribed"]: raise MaintenanceError("订阅连接只允许读取", 400)
                    row["input"].extend(raw)
                    if len(row["input"]) > 8192: raise MaintenanceError("维护请求过长", 400)
                    if b"\n" not in row["input"]: continue
                    request = json.loads(bytes(row["input"]).split(b"\n",1)[0])
                    if not isinstance(request, dict): raise MaintenanceError("维护请求必须是对象", 400)
                    if session.ended:
                        raise MaintenanceError((session.error or "维护会话已结束")+"；本次操作未执行，请重新进入", 409,
                                               reason_code=session.end_reason or "session_ended")
                    if request.get("action") == "imu-subscribe":
                        token = request.get("token")
                        if not isinstance(token,str) or not secrets.compare_digest(token,session.token):
                            raise MaintenanceError("维护订阅所有权无效", 409)
                        if session.mode != "hardware-test" or not session.imu_running:
                            raise MaintenanceError("请先开始当前维护会话的 IMU 测试", 409)
                        row["subscribed"] = True
                    else:
                        started = session.clock()
                        try: answer = session.handle(request)
                        finally: account_busy(started, connection)
                        queue_reply(row, answer)
                except (BlockingIOError, InterruptedError): pass
                except Exception as problem:
                    queue_reply(row, {"error":str(problem), "code":getattr(problem,"code",503),
                                      **getattr(problem,"detail",{})})
            for connection in writable:
                if connection not in clients: continue
                row = clients[connection]
                try:
                    sent = connection.send(row["output"])
                    row["output"] = row["output"][sent:]
                    if sent: row["last_sent"] = session.clock()
                    if not row["output"] and row["closing"]: drop(connection)
                except (BlockingIOError, InterruptedError): pass
                except OSError: drop(connection)
    finally:
        for connection in list(clients): drop(connection)
        if session.service_probe is not None:
            session.service_probe.close();session.service_probe=None


def stream_imu(request, path=SOCKET_PATH, emit=None):
    """One finite privileged helper per stream, never one process per sample."""
    emit = emit or (lambda value: print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True))
    token = request.get("token")
    if not isinstance(token,str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise MaintenanceError("维护订阅标识无效", 400)
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(path)
        connection.sendall((json.dumps({"action":"imu-subscribe","token":token})+"\n").encode())
        with connection.makefile("rb") as reader:
            while True:
                raw = reader.readline(16385)
                if not raw: break
                if len(raw)>16384 or not raw.endswith(b"\n"):
                    raise MaintenanceError("IMU 订阅回执无效",503)
                value = json.loads(raw)
                if value.get("error"): raise MaintenanceError(value["error"],value.get("code",503))
                emit(value)
                if not value.get("active") or not value.get("imu_running"): break
    emit({"kind":"imu_stream_end", "reason":"lifetime" if time.monotonic()-started >= IMU_STREAM_LIFETIME-1 else "closed"})
    return {"stream_closed":True}


def daemon_main(config):
    lock = open(LOCK_PATH, "a+")
    bus = session = server = None
    owns_socket = False
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise MaintenanceError("其他服务管理或维护操作正在执行", 409) from None
        if robotd_state()["robotd_running"]:
            raise MaintenanceError("请先停止机器人总线服务，再进入编号或板卡测试", 409)
        try: bus = SerialBus(PORT)
        except OSError as problem:
            raise MaintenanceError("HAT 串口不可独占，请检查其他程序是否占用：" + str(problem), 409) from None
        session = Session(config, bus)
        if Path(SOCKET_PATH).exists(): os.unlink(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        owns_socket = True
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(8)
        server.setblocking(False)
        def stop(_signum, _frame): session.ended = True
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            signal.signal(getattr(signal, name), stop)
        serve_session(session, server)

    finally:
        if session: session.close()
        if server: server.close()
        if owns_socket:
            try: os.unlink(SOCKET_PATH)
            except FileNotFoundError: pass
        if bus: bus.close()
        lock.close()


def execute(request):
    if not isinstance(request, dict): raise MaintenanceError("维护请求必须是对象", 400)
    action = request.get("action")
    try:
        active = send_request({"action": "status"})
    except (FileNotFoundError, ConnectionRefusedError):
        active = None
    if action == "begin":
        if active and active.get("active"):
            raise MaintenanceError("已有中控正在进行编号或板卡测试，请先退出该会话", 409)
        if request.get("mode") not in ("numbering", "hardware-test"):
            raise MaintenanceError("维护模式无效", 400)
        token = request.get("token", "")
        if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise MaintenanceError("维护会话标识无效", 400)
        if robotd_state()["robotd_running"]:
            raise MaintenanceError("请先停止机器人总线服务，再进入维护页面", 409)
        proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--daemon"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, start_new_session=True)
        config = {"mode": request["mode"], "token": token}
        proc.stdin.write((json.dumps(config)+"\n").encode()); proc.stdin.close()
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                reason = proc.stderr.read(4096).decode(errors="replace").strip()
                raise MaintenanceError(reason or "维护程序未能启动", 503)
            try:
                answer = send_request({"action": "status", "token": token})
                if answer.get("active") and answer.get("owned"):
                    proc.stderr.close()
                    return answer
                raise MaintenanceError("另一个维护会话先取得了串口", 409)
            except (FileNotFoundError, ConnectionRefusedError): time.sleep(.025)
        proc.terminate()
        try: proc.wait(timeout=2)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        raise MaintenanceError("维护程序启动超时；没有开始测试", 503)
    if active:
        return send_request(request, timeout=12 if action in ("scan", "write", "verify-chain", "center-calibrate") else 5)
    if action == "end":
        return {"available": True, "active": False, "mode": "idle", "imu": blank_imu(), "audio": {}}
    if action == "status":
        state = robotd_state()
        return {"available": True, "active": False, "owned": False, "mode": "idle", **state,
                "serial_available": not state["robotd_running"] and serial_available(),
                "imu": blank_imu(), "audio": {}}
    raise MaintenanceError("维护会话已结束，请重新进入", 409)


def main():
    request = json.loads(sys.stdin.buffer.readline(8193))
    if "--daemon" in sys.argv:
        daemon_main(request)
    elif "--imu-stream" in sys.argv:
        print(json.dumps(stream_imu(request),ensure_ascii=False),flush=True)
    else:
        print(json.dumps(execute(request), ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        answer = {"error": str(exc), "code": getattr(exc, "code", 503), **getattr(exc, "detail", {})}
        if "--daemon" in sys.argv:
            print(str(exc), file=sys.stderr, flush=True)
            sys.exit(1)
        print(json.dumps(answer, ensure_ascii=False), flush=True)
