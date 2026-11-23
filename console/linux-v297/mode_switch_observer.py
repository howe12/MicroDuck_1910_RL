"""Finish mode management without holding recovery menus on missing hardware.

Executed through the existing authenticated sudo management channel. Only the
child service.sh process group belongs to this observer. robotd is owned by
systemd and is never stopped, restarted or signalled by this module.
"""
import ctypes
import json
import os
import re
import signal
import subprocess
import sys

SCRIPT = '/opt/robot/feetech-ft5-r5/service.sh'
DAEMON = '/opt/robot/feetech-ft5-r5/robotd'


def stop_observer(process):
    """Release our script, passive startup waiter and management lock only."""
    if process.returncode is not None:
        return
    try:
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError('模式观察进程归属无法确认，未发送停止信号')
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=.75)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A shell may exit before its passive waiter. Release both, including
        # an inherited flock descriptor; never signal the systemd-owned daemon.
        try:os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:pass
    process.wait(timeout=1.5)
    # The one-shot wrapper adopts only its own orphaned descendants. Waiting
    # for them confirms inherited lock descriptors have actually been closed.
    while True:
        try:os.waitpid(-process.pid, 0)
        except ChildProcessError:break


def verify_mode(target, runner=subprocess.run):
    result = runner(['systemctl', 'show', 'robotd.service', '-p', 'ActiveState',
                     '-p', 'MainPID', '-p', 'ExecStart'], capture_output=True,
                    text=True, timeout=4)
    if result.returncode:
        raise RuntimeError('无法确认机器人服务当前模式：' + result.stderr.strip())
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    command = state.get('ExecStart', '')
    actual = 'commissioning' if re.search(r'(?<!\S)--commissioning(?=[;\s]|$)', command) else 'motion'
    if (state.get('ActiveState') != 'active' or not state.get('MainPID', '').isdigit()
            or int(state['MainPID']) <= 0
            or not re.search(r'(?:^|[=\s])' + re.escape(DAEMON) + r'(?=[;\s]|$)', command)
            or actual != target):
        raise RuntimeError('机器人服务当前模式尚未确认，未报告切换完成')
    return {'mode': actual, 'pid': int(state['MainPID']), 'service_running': True}


def observe_mode(target, *, command=None, emit=None, verify=verify_mode):
    if target not in ('motion', 'commissioning'):
        raise ValueError('机器人模式无效')
    if command is None:
        command = ['/bin/bash', SCRIPT, 'enable' if target == 'motion' else 'disable']
    if emit is None:
        emit = lambda line: print(line, end='', flush=True)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), '无法建立本次模式观察进程的回收归属')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors='replace', bufsize=1, start_new_session=True,
                               env={**os.environ, 'MICRODUCK_INSTALL_LOG': '1'})
    last = {}
    try:
        for line in process.stdout:
            emit(line)
            try:report = json.loads(line).get('robotd_startup')
            except (ValueError, AttributeError):continue
            if not isinstance(report, dict) or not isinstance(report.get('steps'), dict):
                continue
            steps = report['steps']
            if (not steps or any(n not in {'1', '2', '3', '4'} or not isinstance(row, dict)
                    or row.get('status') not in {'starting', 'waiting', 'success', 'failed'}
                    or not isinstance(row.get('detail', ''), str) for n, row in steps.items())):
                continue
            last = report
            hardware = steps.get('2')
            if (steps.get('1', {}).get('status') == 'success'
                    and not any(row['status'] == 'failed' for row in steps.values())
                    and isinstance(hardware, dict) and hardware.get('status') == 'waiting'
                    and isinstance(hardware.get('detail'), str)):
                # This is the installed script's passive startup observer, after
                # systemd has started the intended service. Do not wait 60 seconds
                # for unplugged hardware, or report the hardware as ready.
                stop_observer(process)
                actual = verify(target)
                return {**actual, 'startup_pending': True, 'startup': report,
                        'message': '服务模式已设置；硬件尚未准备好，可直接切换到测试或编号模式。'}
        code = process.wait()
        if code:
            failed = next((row.get('detail') for row in last.get('steps', {}).values()
                           if isinstance(row, dict) and row.get('status') == 'failed'), '')
            raise RuntimeError(f'模式设置未完成（退出码 {code}）' + ('：' + failed if failed else '；完整原因见服务日志'))
        return {**verify(target), 'startup_pending': False,
                'message': '服务模式设置已完成；就绪状态以实时反馈为准。'}
    finally:
        stop_observer(process)
        process.stdout.close()


if __name__ == '__main__':
    try:
        answer = observe_mode(sys.argv[1])
        print(json.dumps({'mode_switch_result': answer}, ensure_ascii=False), flush=True)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
