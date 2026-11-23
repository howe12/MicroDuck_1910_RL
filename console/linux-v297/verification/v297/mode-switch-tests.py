"""V297: real child-process/lock handling and production transport/controller.

No real service, SSH, UART, servo, IMU or motion is used.
"""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import mode_switch_observer as observer
from service_control import ServiceController, ServiceTransport
from controls import ControlError
from telemetry import Store

RAW = 'bus transaction failed: Feetech ID 200 response timeout'
WAIT = {'steps': {'1': {'status': 'success', 'detail': '已启动'},
                  '2': {'status': 'waiting', 'detail': RAW}}}
READY = {'steps': {str(n): {'status': 'success', 'detail': 'ok'} for n in range(1, 5)}}
PENDING = {'mode': 'commissioning', 'pid': 1234, 'service_running': True,
           'startup_pending': True, 'startup': WAIT, 'message': '服务模式已设置；硬件尚未准备好，可切换测试或编号。'}


class Log:
    def __init__(self): self.rows = []
    def hide(self, *args): pass
    def add(self, source, text): self.rows.append((source, text))


class Tests(unittest.TestCase):
    def command(self, source):
        return [sys.executable, '-u', '-c', source]

    def run_observer(self, report, tail='', verify=None):
        lines = []
        source = 'import json,time,sys\nprint(json.dumps(' + repr({'robotd_startup': report}) + '),flush=True)\n' + tail
        answer = observer.observe_mode('commissioning', command=self.command(source),
                    emit=lines.append, verify=verify or (lambda target: {'mode': target, 'pid': 1234, 'service_running': True}))
        return answer, lines

    def test_wait_releases_real_parent_and_child_lock_without_touching_daemon(self):
        # Simulate systemd's independent robotd process as a separate sibling.
        daemon = subprocess.Popen(self.command('import time;time.sleep(40)'), start_new_session=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                lock_path = str(Path(directory) / 'management.lock')
                child = "import fcntl,os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);fcntl.flock(9,fcntl.LOCK_EX);os.write(1,b'child holds inherited management lock\\n');time.sleep(40)"
                source = ('import fcntl,os,subprocess,sys,json,time\n'
                    f"fd=os.open({lock_path!r},os.O_CREAT|os.O_RDWR,0o600)\n"
                    'os.dup2(fd,9);fcntl.flock(9,fcntl.LOCK_EX)\n'
                    f'child=subprocess.Popen([sys.executable,"-u","-c",{child!r}],pass_fds=(9,),stdout=subprocess.PIPE)\n'
                    'child.stdout.readline()\n'
                    f'print(json.dumps({{"robotd_startup":{WAIT!r}}}),flush=True)\n'
                    'time.sleep(40)\n')
                before = time.monotonic()
                answer = observer.observe_mode('commissioning', command=self.command(source), emit=lambda _: None,
                         verify=lambda target: {'mode': target, 'pid': daemon.pid, 'service_running': True})
                self.assertLess(time.monotonic() - before, 3)
                self.assertTrue(answer['startup_pending'])
                with open(lock_path, 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIsNone(daemon.poll(), 'No signal may reach the systemd-owned daemon')
        finally:
            daemon.terminate(); daemon.wait(timeout=2)

    def test_normal_ready_finishes_once_with_complete_logs(self):
        answer, lines = self.run_observer(READY, 'print("native diagnostic preserved",flush=True)\n')
        self.assertFalse(answer['startup_pending'])
        self.assertIn('native diagnostic preserved', ''.join(lines))

    def test_wait_returns_exact_reason_before_old_long_timeout(self):
        before = time.monotonic()
        answer, lines = self.run_observer(WAIT, 'time.sleep(40)\n')
        self.assertLess(time.monotonic() - before, 3)
        self.assertEqual(answer['startup']['steps']['2']['detail'], RAW)
        self.assertEqual(len(lines), 1)

    def test_missing_servo_and_partial_chain_use_same_prompt_release_as_imu(self):
        for detail in ('bus transaction failed: Feetech ID 20 response timeout',
                       'bus transaction failed: Feetech Sync Read timeout; missing IDs [10, 24]'):
            with self.subTest(detail=detail):
                report = {'steps': {'1': WAIT['steps']['1'], '2': {'status': 'waiting', 'detail': detail}}}
                before = time.monotonic()
                answer, _ = self.run_observer(report, 'time.sleep(40)\n')
                self.assertLess(time.monotonic() - before, 3)
                self.assertTrue(answer['startup_pending'])
                self.assertEqual(answer['startup']['steps']['2']['detail'], detail)

    def test_parent_exit_does_not_leave_its_waiter_holding_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / 'management.lock')
            child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(40)'
            source = ('import fcntl,os,subprocess,sys,json\n'
                      f'fd=os.open({lock_path!r},os.O_CREAT|os.O_RDWR,0o600);os.dup2(fd,9);fcntl.flock(9,fcntl.LOCK_EX)\n'
                      f'child=subprocess.Popen([sys.executable,"-u","-c",{child!r}],pass_fds=(9,),stdout=subprocess.PIPE)\n'
                      'child.stdout.readline()\n'
                      f'print(json.dumps({{"robotd_startup":{WAIT!r}}}),flush=True)\n')
            answer = observer.observe_mode('commissioning', command=self.command(source), emit=lambda _: None,
                verify=lambda target: {'mode': target, 'pid': 1234, 'service_running': True})
            self.assertTrue(answer['startup_pending'])
            with open(lock_path, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_wrong_actual_mode_cannot_report_completion(self):
        def invalid(_): raise RuntimeError('实际模式不匹配')
        with self.assertRaisesRegex(RuntimeError, '不匹配'):
            self.run_observer(WAIT, 'time.sleep(40)\n', invalid)

    def test_failed_script_is_not_hidden_or_retried(self):
        failed = {'steps': {'1': {'status': 'failed', 'detail': '配置文件不存在'}}}
        with self.assertRaisesRegex(RuntimeError, '配置文件不存在'):
            self.run_observer(failed, 'sys.exit(5)\n')

    def test_invalid_or_unsuccessful_step1_does_not_trigger_pending_success(self):
        for report in ({'steps': {'2': {'status': 'waiting', 'detail': RAW}}},
                       {'steps': {'1': {'status': 'failed', 'detail': 'broken'}, '2': WAIT['steps']['2']}},
                       {'steps': {**WAIT['steps'], '5': {'status': 'waiting', 'detail': RAW}}}):
            with self.subTest(report=report), self.assertRaises(RuntimeError):
                self.run_observer(report, 'sys.exit(5)\n')

    def test_verification_requires_active_real_pid_canonical_path_and_target(self):
        canonical = f'{{ path={observer.DAEMON} ; argv[]={observer.DAEMON} --commissioning ; }}'
        def runner(stdout, code=0):
            return lambda *a, **kw: SimpleNamespace(returncode=code, stdout=stdout, stderr='systemctl failed')
        valid = f'ActiveState=active\nMainPID=1234\nExecStart={canonical}\n'
        self.assertEqual(observer.verify_mode('commissioning', runner(valid))['pid'], 1234)
        for bad in (valid.replace('active', 'inactive'), valid.replace('1234', '0'),
                    valid.replace(observer.DAEMON, observer.DAEMON+'.old'),
                    valid.replace('--commissioning', '--commissioning-other')):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                observer.verify_mode('commissioning', runner(bad))
        with self.assertRaises(RuntimeError): observer.verify_mode('motion', runner(valid))
        with self.assertRaises(RuntimeError): observer.verify_mode('commissioning', runner(valid, 1))

    def test_transport_streams_raw_receipt_without_credential_redaction_damage(self):
        log = Log(); calls = []; progress = []
        class Channel:
            def request(self, request, timeout, progress=None):
                calls.append(request)
                lines = [json.dumps({'robotd_startup': WAIT}), 'diagnostic text\n',
                         json.dumps({'mode_switch_result': PENDING})]
                for line in lines: progress(line)
                return {'returncode': 0, 'stdout': '\n'.join(lines), 'stderr': ''}
        transport = ServiceTransport('fixture', '/run/robotd.sock', log)
        transport.channel = Channel()
        answer = transport.switch_mode('commissioning', password='mode', progress=progress.append)
        self.assertEqual(answer, PENDING)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['argv'][-5:-2], ['python3', '-u', '-c'])
        self.assertEqual(calls[0]['argv'][-1], 'commissioning')
        self.assertIn('start_new_session=True', calls[0]['argv'][-2])
        self.assertEqual(json.loads(progress[0])['robotd_startup'], WAIT)
        self.assertIn('diagnostic text', ''.join(str(row) for row in log.rows))
        self.assertNotIn(observer.SCRIPT, ''.join(str(row) for row in log.rows))

    def test_invalid_receipt_cannot_unlock_as_success(self):
        for bad in ({**PENDING, 'pid': 0}, {**PENDING, 'mode': 'motion'},
                    {**PENDING, 'startup_pending': 'yes'}):
            transport = ServiceTransport(None, '/run/robotd.sock')
            def manage(*a, **kw):
                kw['raw_output_handler'](json.dumps({'mode_switch_result': bad}))
            transport._manage = manage
            with self.subTest(bad=bad), self.assertRaises(ControlError):
                transport.switch_mode('commissioning')

    def test_controller_publishes_wait_and_releases_its_busy_locks_on_pending(self):
        store = Store('service'); store.system_log = Log()
        store.raw_hub = SimpleNamespace(rows=[], event=lambda event: store.raw_hub.rows.append(event))
        calls = []; resumed = []
        store.resume_robotd_connections = lambda: resumed.append('resume existing reader')
        class Transport:
            def switch_mode(self, target, password, progress):
                calls.append(target); progress(json.dumps({'robotd_startup': WAIT}))
                self.callback = progress
                self.started = store.data['startup']
                return dict(PENDING)
        peer = Transport(); controller = ServiceController(store, transport=peer, warm=False)
        command = {'action': 'switch-mode', 'target': 'commissioning', 'supported': True}
        job = {'action': 'switch-mode', 'command': command, 'status': 'running'}
        controller.job = job; controller.busy = store.service_operation = True
        controller._execute(command, job, 0)
        self.assertEqual(peer.started, WAIT)
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['result']['startup_pending'])
        self.assertFalse(controller.busy or controller.stopping or store.service_operation)
        self.assertFalse(store.robotd_restarting.is_set())
        self.assertEqual(calls, ['commissioning'])
        self.assertEqual(resumed, ['resume existing reader'])
        count = len(store.raw_hub.rows)
        peer.callback(json.dumps({'robotd_startup': READY}))
        self.assertEqual(len(store.raw_hub.rows), count, 'Old job must not overwrite the later bench mode')

    def test_real_bench_entry_stops_unready_service_then_opens_each_owned_mode(self):
        from workspace_control import WorkspaceController
        for mode in ('numbering', 'hardware-test'):
            with self.subTest(mode=mode):
                store = Store('service'); store.data['startup'] = WAIT
                service = SimpleNamespace(transport=SimpleNamespace(target='fixture'), lock=threading.RLock(),
                    closed=False, busy=False, stopping=False)
                connection = SimpleNamespace(active=True, ssh_authenticated=True)
                calls = []
                state = {'active': False, 'owned': False, 'robotd_running': True,
                         'serial_state': 'busy', 'serial_available': False,
                         'requires_component_install': False, 'worker_path': '/verified/maintenance_worker.py',
                         'component_id': 'verified-component'}
                def stop(**kw):
                    calls.append('stop')
                    state.update(robotd_running=False, serial_state='free', serial_available=True)
                def handle(payload):
                    calls.append(payload['action'])
                    self.assertEqual(state['serial_state'], 'free')
                    return {**state, 'available': True, 'active': True, 'owned': True, 'mode': payload['mode'], 'session_id': 'owned-bench'}
                components = SimpleNamespace(inspect=lambda **kw: dict(state), stop=stop)
                core = SimpleNamespace(use_worker=lambda *a, **kw: calls.append('use_worker'), handle=handle)
                workspace = WorkspaceController(store, service, connection=connection, core=core, components=components)
                checked = workspace._entry_state(mode)
                self.assertTrue(checked['can_enter'] and checked['requires_stop'])
                answer = workspace._enter({'mode': mode, 'entry_target': checked['entry_target'], 'stop_confirmed': True})
                self.assertTrue(answer['accepted'])
                self.assertEqual(answer['result']['mode'], mode)
                self.assertEqual(calls, ['stop', 'use_worker', 'begin'])
                self.assertFalse(workspace.pending or store.service_operation)

    def test_foreign_serial_owner_and_running_service_operation_still_block_entry(self):
        from workspace_control import WorkspaceController
        state = {'active': False, 'owned': False, 'robotd_running': False,
                 'serial_state': 'busy', 'requires_component_install': False}
        store = Store('service')
        service = SimpleNamespace(transport=SimpleNamespace(target='fixture'), lock=threading.RLock(),
                                  closed=False, busy=False, stopping=False)
        workspace = WorkspaceController(store, service, connection=SimpleNamespace(active=True, ssh_authenticated=True),
            core=SimpleNamespace(), components=SimpleNamespace(inspect=lambda **kw: state))
        self.assertFalse(workspace._entry_state('numbering')['can_enter'])
        state.update(robotd_running=True); service.busy=True
        self.assertFalse(workspace._entry_state('hardware-test')['can_enter'])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful(): sys.exit(1)
    files = ['mode_switch_observer.py', 'service_control.py', 'workspace_control.py']
    report = {'passed': True, 'cases': result.testsRun,
              'source_sha256': {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in files},
              'boundary': __doc__}
    Path(__file__).with_name('mode-switch-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'passed': True, 'cases': result.testsRun}))
