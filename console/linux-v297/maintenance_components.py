"""PC-shipped, bounded preparation of four isolated ZERO bench modules.

The native firmware installer and its full-robot startup gate are deliberately
not used. The remote management snippet never opens a UART. Only the explicit
enter flow may stop robotd or prepare the four verified Python modules.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile

from controls import ControlError
import firmware_packages

FILES = ('maintenance_worker.py', 'board_audio.py', 'ai_hat_audio.py', 'ai_audio_meter.py')
MAX_FILE_BYTES = 64 * 1024
MAX_WORKER_BYTES = 96 * 1024
MAX_COMPONENT_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 256 * 1024
INSTALL_ROOT = '/opt/robot/feetech-ft5-r5'
COMPONENT_ROOT = INSTALL_ROOT + '/maintenance-components'


def component_bundle():
    """Read four exact regular tar members, after the shipped archive SHA check."""
    info = firmware_packages.bundle_info()
    archive = firmware_packages.ROOT / 'updates' / firmware_packages.ARCHIVE_NAME
    raw_archive = archive.read_bytes()
    if len(raw_archive) > firmware_packages.MAX_FIRMWARE or hashlib.sha256(raw_archive).hexdigest() != info['sha256']:
        raise ControlError('随包固件在读取期间发生变化，未准备维护组件', 409)
    root = firmware_packages.ARCHIVE_NAME.removesuffix('.tar.gz')
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(raw_archive), mode='r:gz') as package:
        members = package.getmembers()
        for name in FILES:
            limit = MAX_WORKER_BYTES if name == 'maintenance_worker.py' else MAX_FILE_BYTES
            expected = root + '/' + name
            matches = [item for item in members if item.name == expected]
            if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= limit:
                raise ControlError('随包维护组件缺失或格式无效：' + name, 409)
            with package.extractfile(matches[0]) as stream:
                data = stream.read(limit + 1)
            if len(data) != matches[0].size or len(data) > limit:
                raise ControlError('随包维护组件长度不匹配：' + name, 409)
            selected[name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                              'data': base64.b64encode(data).decode('ascii')}
    if sum(row['size'] for row in selected.values()) > MAX_COMPONENT_BYTES:
        raise ControlError('随包维护组件超过长度限制', 409)
    hashes = {name: {'size': row['size'], 'sha256': row['sha256']} for name, row in selected.items()}
    component_id = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'component_id': component_id, 'files': selected}


# This code is sent through the existing authenticated manage channel, not a
# shell. Payload bytes enter on stdin, below service_rpc's streaming line limit.
# Production callers cannot override these fixed paths or the four-file list.
REMOTE_SOURCE = r'''
import base64,fcntl,hashlib,json,os,re,shutil,socket,stat,subprocess,sys,tempfile,time
from pathlib import Path
INSTALL=Path('/opt/robot/feetech-ft5-r5')
COMPONENTS=INSTALL/'maintenance-components'
LOCK=Path('/run/microduck-service-management.lock')
SOCKET='/run/microduck-maintenance.sock'
PORT='/dev/ttyS2'
FILES=('maintenance_worker.py','board_audio.py','ai_hat_audio.py','ai_audio_meter.py')
MAX_FILE=65536
MAX_WORKER=98304
MAX_TOTAL=131072
MAX_REQUEST=262144

class BenchError(RuntimeError):
    pass

def system_state():
    p=subprocess.run(['systemctl','show','robotd.service','-p','ActiveState','-p','MainPID','-p','ExecStart'],capture_output=True,text=True,timeout=4)
    if p.returncode:raise BenchError('无法确认机器人服务状态：'+(p.stderr.strip() or p.stdout.strip()))
    v=dict(row.split('=',1) for row in p.stdout.splitlines() if '=' in row)
    active=v.get('ActiveState')
    if active not in ('active','inactive','failed','activating','deactivating','reloading'):
        raise BenchError('机器人服务状态未知，未停止或准备维护组件')
    pid=v.get('MainPID','')
    if not pid.isdigit():raise BenchError('机器人进程状态未知，未停止或准备维护组件')
    running=active not in ('inactive','failed') or int(pid)!=0
    return {'robotd_running':running,'robotd_state':active,'robotd_pid':int(pid),'exec_start':v.get('ExecStart','')}

def lease_status(token=None):
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
            peer.settimeout(1)
            peer.connect(SOCKET)
            request={'action':'status'}
            if isinstance(token,str):request['token']=token
            peer.sendall((json.dumps(request)+'\n').encode())
            with peer.makefile('rb') as stream:line=stream.readline(65537)
        if not line.endswith(b'\n') or len(line)>65536:raise BenchError('维护会话回执无效')
        value=json.loads(line)
        if not isinstance(value,dict) or type(value.get('active')) is not bool:
            raise BenchError('无法确认现有维护会话')
        return {key:value.get(key) for key in ('active','owned','mode')}
    except (FileNotFoundError,ConnectionRefusedError):return {'active':False,'owned':False,'mode':'idle'}
    except (OSError,ValueError) as exc:raise BenchError('无法确认现有维护会话：'+str(exc)) from None

def uart_ownership():
    try:
        wanted=os.stat(PORT)
        if not stat.S_ISCHR(wanted.st_mode):return {'serial_state':'missing','serial_reason':'HAT 串口不存在'}
    except FileNotFoundError:return {'serial_state':'missing','serial_reason':'HAT 串口不存在'}
    except OSError:return {'serial_state':'unknown','serial_reason':'无法读取 HAT 串口状态'}
    try:
        p=subprocess.run(['fuser',PORT],capture_output=True,text=True,timeout=2)
        if p.returncode==0 and p.stdout.strip():return {'serial_state':'busy','serial_reason':'串口正被其他程序占用'}
        if p.returncode==1 and not p.stdout.strip() and not p.stderr.strip():
            return {'serial_state':'free','serial_reason':''}
    except (OSError,subprocess.TimeoutExpired):pass
    # Read fd metadata only. No UART open, termios operation or packet is sent.
    uncertain=False
    try:pids=list(Path('/proc').iterdir())
    except OSError:return {'serial_state':'unknown','serial_reason':'无法确认 HAT 串口占用情况'}
    for pid in pids:
        if not pid.name.isdigit():continue
        try:descriptors=list((pid/'fd').iterdir())
        except FileNotFoundError:continue
        except OSError:uncertain=True;continue
        for descriptor in descriptors:
            try:
                value=descriptor.stat()
                if stat.S_ISCHR(value.st_mode) and value.st_rdev==wanted.st_rdev:
                    return {'serial_state':'busy','serial_reason':'串口正被其他程序占用'}
            except FileNotFoundError:continue
            except OSError:uncertain=True
    return {'serial_state':'unknown' if uncertain else 'free','serial_reason':'无法完整确认 HAT 串口占用情况' if uncertain else ''}

def spec(request):
    hashes=request.get('hashes')
    if not isinstance(hashes,dict) or set(hashes)!=set(FILES):raise BenchError('维护组件清单无效')
    for name,row in hashes.items():
        limit=MAX_WORKER if name=='maintenance_worker.py' else MAX_FILE
        if not isinstance(row,dict) or set(row)!=set(('size','sha256')) or type(row['size']) is not int or not 0<row['size']<=limit:
            raise BenchError('维护组件长度无效')
        if not isinstance(row['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',row['sha256']):raise BenchError('维护组件哈希无效')
    if sum(row['size'] for row in hashes.values())>MAX_TOTAL:raise BenchError('维护组件总长度超限')
    ident=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if request.get('component_id')!=ident:raise BenchError('维护组件身份不匹配')
    return hashes,ident

def protected_directory(directory,required=False):
    if directory.is_symlink():raise BenchError('维护组件目录不能是符号链接')
    if not directory.exists():
        if required:raise BenchError('机器人安装目录不存在，无法准备独立维护组件')
        return
    value=directory.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid!=os.geteuid() or value.st_mode&0o022:
        raise BenchError('维护组件父目录权限不安全，未执行其中的程序')

def valid_directory(directory,hashes):
    if directory.is_symlink() or not directory.is_dir():return False
    try:
        value=directory.stat()
        if value.st_uid!=os.geteuid() or value.st_mode&0o022:return False
        for name,row in hashes.items():
            file=directory/name
            if file.is_symlink() or not file.is_file() or file.stat().st_size!=row['size']:return False
            if file.stat().st_uid!=os.geteuid() or file.stat().st_mode&0o022:return False
            if hashlib.sha256(file.read_bytes()).hexdigest()!=row['sha256']:return False
    except OSError:return False
    return True

def inspect(request):
    hashes,ident=spec(request)
    protected_directory(INSTALL,required=True)
    protected_directory(COMPONENTS)
    state=system_state()
    lease=lease_status(request.get('token'))
    target=COMPONENTS/ident
    chosen=target if valid_directory(target,hashes) else INSTALL if valid_directory(INSTALL,hashes) else None
    result={**state,**lease,'requires_component_install':chosen is None,
            'component_id':ident,'worker_path':str(chosen/'maintenance_worker.py') if chosen else None}
    if lease['active'] or state['robotd_running']:
        result.update(serial_state='busy',serial_reason='维护会话正在使用串口' if lease['active'] else '机器人总线服务正在运行')
    else:result.update(uart_ownership())
    result['serial_available']=result['serial_state']=='free'
    return result

def management_lock():
    # Only explicit stop/prepare actions create/acquire the shared lock.
    handle=open(LOCK,'a+')
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close();raise BenchError('其他服务管理或维护操作正在执行') from None
    return handle

def no_lease():
    if lease_status().get('active'):raise BenchError('已有维护会话，请先退出对应中控的测试')

def stop_service(request):
    with management_lock():
        no_lease()
        state=system_state()
        if not state['robotd_running']:return {**state,'accepted':True,'message':'机器人总线服务已经停止'}
        if request.get('stop_confirmed') is not True:raise BenchError('需要确认停止机器人总线服务')
        if not (INSTALL/'robotd').is_file() or str(INSTALL/'robotd') not in state['exec_start']:
            raise BenchError('当前机器人服务路径不匹配，未停止未知服务')
        # Best-effort relax over the existing IPC only; no UART or 15-joint gate.
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                peer.settimeout(2);peer.connect('/run/robotd.sock')
                peer.sendall(b'{"jsonrpc":"2.0","id":1,"method":"robot.relax","params":{}}\n')
                with peer.makefile('rb') as stream:stream.readline(65537)
        except OSError:pass
        result=subprocess.run(['systemctl','stop','robotd.service'],capture_output=True,text=True,timeout=25)
        if result.returncode:raise BenchError('停止机器人服务失败：'+(result.stderr.strip() or result.stdout.strip()))
        actual=system_state()
        if actual['robotd_running']:raise BenchError('机器人服务尚未停止，未开始维护')
        no_lease()
        return {**actual,'accepted':True,'message':'机器人总线服务已停止；未请求恢复运动'}

def prepare(request):
    hashes,ident=spec(request)
    rows=request.get('files')
    if not isinstance(rows,dict) or set(rows)!=set(FILES):raise BenchError('只允许准备固定四个维护模块')
    contents={}
    for name,row in hashes.items():
        encoded=rows[name]
        limit=MAX_WORKER if name=='maintenance_worker.py' else MAX_FILE
        if not isinstance(encoded,str) or len(encoded)>((limit+2)//3)*4:raise BenchError('维护组件编码长度超限')
        try:data=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError):raise BenchError('维护组件编码无效') from None
        if len(data)!=row['size'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise BenchError('维护组件校验失败：'+name)
        compile(data,name,'exec')
        contents[name]=data
    with management_lock():
        no_lease()
        if system_state()['robotd_running']:raise BenchError('先停止机器人服务，再准备维护组件')
        protected_directory(INSTALL,required=True)
        protected_directory(COMPONENTS)
        COMPONENTS.mkdir(mode=0o755,exist_ok=True)
        target=COMPONENTS/ident
        if target.is_symlink():raise BenchError('维护组件目标不能是符号链接')
        if valid_directory(target,hashes):
            return {'accepted':True,'prepared':False,'component_id':ident,'worker_path':str(target/'maintenance_worker.py')}
        if target.exists():raise BenchError('已有维护组件校验失败，未覆盖异常目录')
        stage=Path(tempfile.mkdtemp(prefix='.prepare-',dir=COMPONENTS))
        try:
            for name,data in contents.items():
                with (stage/name).open('xb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.chmod(stage/name,0o644)
            with (stage/'component-manifest.json').open('x',encoding='utf-8') as stream:
                json.dump({'component_id':ident,'hashes':hashes},stream,sort_keys=True)
                stream.write('\n');stream.flush();os.fsync(stream.fileno())
            os.chmod(stage/'component-manifest.json',0o644)
            if not valid_directory(stage,hashes):raise BenchError('暂存维护组件读回校验失败')
            os.chmod(stage,0o755)
            directory=os.open(stage,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            os.rename(stage,target)
            parent=os.open(COMPONENTS,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if stage.exists():shutil.rmtree(stage)
        return {'accepted':True,'prepared':True,'component_id':ident,'worker_path':str(target/'maintenance_worker.py')}

def execute(request):
    if not isinstance(request,dict):raise BenchError('维护管理请求必须是对象')
    action=request.get('action')
    if action=='inspect':return inspect(request)
    if action=='stop':return stop_service(request)
    if action=='prepare':return prepare(request)
    raise BenchError('不支持的维护管理操作')

def main():
    raw=sys.stdin.buffer.readline(MAX_REQUEST+1)
    if len(raw)>MAX_REQUEST or not raw.endswith(b'\n'):raise BenchError('维护管理请求长度无效')
    result=execute(json.loads(raw))
    print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),flush=True)
'''


class BenchComponents:
    def __init__(self, transport_provider):
        self.transport_provider = transport_provider

    def _bundle(self):
        # Immutable bytes are cached only after the complete shipped archive SHA
        # has been checked, not from loose PC-side Python files.
        if not hasattr(self, '_verified_bundle'):
            self._verified_bundle = component_bundle()
        return self._verified_bundle

    def _request(self, action, password=None, token=None, stop_confirmed=False):
        request = {'action': action}
        if action in ('inspect', 'prepare'):
            bundle = self._bundle()
            request.update(component_id=bundle['component_id'], hashes={name: {
                'size': row['size'], 'sha256': row['sha256']} for name, row in bundle['files'].items()})
            if action == 'prepare':
                request['files'] = {name: row['data'] for name, row in bundle['files'].items()}
        if token is not None: request['token'] = token
        if action == 'stop': request['stop_confirmed'] = stop_confirmed
        encoded = json.dumps(request, ensure_ascii=False)+'\n'
        if len(encoded.encode()) > MAX_REQUEST_BYTES:
            raise ControlError('维护管理请求超过长度限制', 409)
        result = self.transport_provider()._manage(['python3', '-u', '-c', REMOTE_SOURCE],
                password=password, privileged=True, record_output=False,
                result_format='json', input_data=encoded)
        if not isinstance(result, dict): raise ControlError('维护管理回执无效', 503)
        if result.get('error'): raise ControlError(str(result['error']), 409)
        if action == 'stop' and (result.get('accepted') is not True or result.get('robotd_running') is not False):
            raise ControlError('未确认机器人服务停止，未继续进入维护', 503)
        if action in ('inspect', 'prepare'):
            if result.get('component_id') != bundle['component_id']:
                raise ControlError('维护组件回执身份与本版随包内容不一致', 503)
            expected = COMPONENT_ROOT + '/' + bundle['component_id'] + '/maintenance_worker.py'
            if action == 'prepare':
                valid_path = (result.get('accepted') is True and result.get('worker_path') == expected
                              and type(result.get('prepared')) is bool)
            else:
                needed = result.get('requires_component_install')
                valid_path = (type(needed) is bool and
                              (result.get('worker_path') is None if needed else
                               result.get('worker_path') in (expected, INSTALL_ROOT + '/maintenance_worker.py')))
            if not valid_path:
                raise ControlError('维护组件回执程序路径无效，未执行任何维护程序', 503)
        return result

    def inspect(self, password=None, token=None):
        return self._request('inspect', password, token)

    def stop(self, password=None):
        return self._request('stop', password, stop_confirmed=True)

    def prepare(self, password=None):
        return self._request('prepare', password)
