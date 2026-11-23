"""Finite, local HAT audio checks; PCM stays in memory and no robot motion is used.

The maintenance worker owns the cross-client lease and must call ``close`` when
its lease ends. This module reuses the production HAT capture format and meter.
Playback reads the already installed native chirp, never the mouth-coupled AI
speaker. Tests run on a PC by injecting process/device fixtures.
"""
import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import wave

from ai_audio_meter import AudioMeter
from ai_hat_audio import AudioError, NativeMicrophone, hat_device


INSTALL = Path('/opt/robot/feetech-ft5-r5')
CONFIG = Path('/etc/robot/feetech-ft5/robotd.toml')
FRESH_SECONDS = .6
MAX_MICROPHONE_SECONDS = 120


def duck_asset(config=CONFIG, install=INSTALL):
    """Prefer a 2–4 second installed duck call; never loop or stretch PCM."""
    try:
        audio = tomllib.loads(Path(config).read_text()).get('audio', {})
        configured = audio.get('bank')
        if configured is not None and (not isinstance(configured, str) or not configured):
            raise AudioError('已安装鸭叫音库路径无效')
    except (OSError, ValueError):
        configured = None
    bank = Path(configured) if configured else Path(install) / 'voice-bank'
    choices = [(priority, asset) for priority, (folder, pattern) in enumerate(
        (('wheee', '*.wav'), ('chirp', 'chirp_*.wav'), ('excited', '*.wav')))
        for asset in sorted((bank / folder).glob(pattern))[:32]]
    if not choices:
        raise AudioError('鸭叫音库未就绪，请先安装／修复本版配套固件的音频配置')
    valid, errors = [], []
    for priority, asset in choices:
        try:
            if asset.stat().st_size > 4 * 1024 * 1024:
                raise AudioError('已安装鸭叫 WAV 大小异常，请修复配套音库')
            with wave.open(str(asset), 'rb') as source:
                duration = source.getnframes() / source.getframerate()
                if (source.getcomptype() != 'NONE' or source.getsampwidth() != 2
                        or source.getnchannels() not in (1, 2) or not 0 < duration <= 10):
                    raise AudioError('已安装鸭叫 WAV 格式无效，请修复配套音库')
            valid.append((priority, asset, duration))
        except (OSError, wave.Error, ZeroDivisionError, AudioError) as exc:
            errors.append(str(exc))
    if not valid:
        raise AudioError('无法读取已安装鸭叫 WAV：' + errors[0])
    comfortable = [row for row in valid if 2 <= row[2] <= 4]
    if comfortable:
        _, asset, duration = min(comfortable, key=lambda row: (row[0], abs(row[2]-3), row[1].name))
    else:
        short = [row for row in valid if row[2] <= 4]
        _, asset, duration = max(short, key=lambda row: row[2]) if short else min(valid, key=lambda row: row[2])
    return asset, duration


def duck_envelope(asset):
    """Bounded PCM file levels for playback display, not acoustic feedback."""
    with wave.open(str(asset), 'rb') as source:
        rate, count = source.getframerate(), source.getnframes()
        if (source.getcomptype() != 'NONE' or source.getsampwidth() != 2
                or source.getnchannels() not in (1, 2) or rate <= 0 or not 0 < count / rate <= 10
                or count * source.getnchannels() * 2 > 4 * 1024 * 1024):
            raise AudioError('已安装鸭叫 WAV 格式无效')
        frames = max(1, rate // 20)
        levels = []
        for _ in range(201):
            pcm = source.readframes(frames)
            if not pcm:
                break
            levels.append(AudioMeter().update(pcm)['dbfs'])
        if source.tell() != count:
            raise AudioError('鸭叫电平数据长度异常')
        return levels, frames * 1000 / rate, count * 1000 / rate


def audio_owners(device, capture, *, proc_root=Path('/proc'), device_root=Path('/dev/snd'),
                 runner=subprocess.run, which=shutil.which):
    """Report actual PCM users, not an idle mediad service's ActiveState.

    ALSA exclusive opens remain authoritative if fuser/proc metadata is absent.
    Never signal the reported processes or inspect their environment/arguments.
    """
    executable = which('fuser')
    if not executable:
        return []
    card = device.partition(':')[2]
    try:
        name = (proc_root / 'asound' / card).resolve(strict=True).name
    except OSError:
        return []
    if not re.fullmatch(r'card\d+', name):
        return []
    nodes = sorted(device_root.glob('pcmC' + name[4:] + 'D*' + ('c' if capture else 'p')))
    if not nodes:
        return []
    result = runner([executable, *map(str, nodes)], capture_output=True, text=True,
                    timeout=2, errors='replace')
    if result.returncode not in (0, 1):
        raise AudioError('无法检查 HAT 音频占用：' + result.stderr.strip()[-300:])
    owners = []
    for token in result.stdout.split():
        if not token.isdigit() or int(token) == os.getpid():
            continue
        try:
            label = (proc_root / token / 'comm').read_text().strip()[:80]
        except OSError:
            label = '进程'
        owners.append({'pid': int(token), 'name': label})
    return owners


class BoardAudio:
    def __init__(self, *, clock=time.monotonic, microphone_factory=NativeMicrophone,
                 popen=subprocess.Popen, which=shutil.which, owners=audio_owners,
                 device=None, config=CONFIG, install=INSTALL):
        self.clock, self.microphone_factory = clock, microphone_factory
        self.popen, self.which, self.owners = popen, which, owners
        self.device = device
        self.config, self.install = config, install
        self.lock = threading.RLock()
        self.closed = False
        self.mic_stop = threading.Event()
        self.mic_thread = None
        self.mic = None
        self.speaker_thread = None
        self.speaker = None
        self.playback_started = None
        self.playback_sequence = 0
        self.microphone = self._empty_microphone()
        self.playback = {'state': 'idle', 'asset': None, 'error': '',
                         'message': '点击播放鸭叫，用耳朵确认扬声器出声'}

    @staticmethod
    def _empty_microphone(state='idle', message='点击开始测试，对着麦克风说话'):
        return {'state': state, 'fresh': False, 'dbfs': None, 'rms': None,
                'peak': None, 'samples': 0, 'captured_seconds': 0.,
                'updated_at': None, 'error': '', 'message': message}

    def _device(self, capture):
        if self.closed:
            raise AudioError('音频测试会话已关闭')
        if not sys.platform.startswith('linux'):
            raise AudioError('HAT 音频测试只能在 ZERO 上运行')
        device = self.device or hat_device()
        if not isinstance(device, str) or not re.fullmatch(r'plughw:[A-Za-z0-9_-]+', device):
            raise AudioError('HAT 声卡配置无效')
        owners = self.owners(device, capture)
        if owners:
            names = '、'.join(f"{entry['name']}（{entry['pid']}）" for entry in owners)
            raise AudioError('HAT 声卡正在被其他音频程序占用：' + names + '；请先停止对应 AI／摄像头音频')
        return device

    def start_microphone(self):
        with self.lock:
            if self.mic_thread and self.mic_thread.is_alive():
                return self.status()
            try:
                device = self._device(True)
                if not self.which('arecord'):
                    raise AudioError('ZERO 缺少 arecord，请使用已配置 HAT 音频的系统镜像')
            except Exception as exc:
                self.microphone = self._empty_microphone('error')
                self.microphone.update(error=str(exc), message=str(exc))
                raise
            self.mic_stop = threading.Event()
            self.microphone = self._empty_microphone('starting', '正在等待 HAT 麦克风真实 PCM')
            self.mic_thread = threading.Thread(target=self._capture, args=(device, self.mic_stop),
                                               name='HAT-Microphone-Test', daemon=True)
            self.mic_thread.start()
            return self.status()

    def _capture(self, device, stop):
        meter = AudioMeter(clock=self.clock)
        started = self.clock()
        count = 0
        try:
            with self.microphone_factory(device, stop_event=stop) as microphone:
                with self.lock:
                    self.mic = microphone
                while not stop.is_set() and self.clock() - started < MAX_MICROPHONE_SECONDS:
                    pcm = microphone.read()
                    if not pcm:
                        if stop.is_set():
                            break
                        raise AudioError('HAT 麦克风未返回有效 PCM')
                    if len(pcm) % 2:
                        raise AudioError('HAT 麦克风返回未对齐的 PCM16')
                    count += len(pcm) // 2
                    level = meter.update(pcm)
                    if level:
                        with self.lock:
                            if stop.is_set():
                                break
                            self.microphone.update(state='running', fresh=True,
                                dbfs=level['dbfs'], rms=level['rms'], peak=level['peak'],
                                samples=count, captured_seconds=round(count / 16000, 2),
                                updated_at=self.clock(), error='', message='正在接收真实麦克风输入；说话时观察电平变化')
                if not stop.is_set():
                    with self.lock:
                        self.microphone = self._empty_microphone('completed', '本次麦克风测试已结束，可重新开始')
        except Exception as exc:
            if not stop.is_set():
                with self.lock:
                    self.microphone = self._empty_microphone('error')
                    self.microphone.update(error=str(exc), message=str(exc))
        finally:
            with self.lock:
                self.mic = None

    def stop_microphone(self):
        with self.lock:
            self.mic_stop.set()
            thread = self.mic_thread
        if thread and thread is not threading.current_thread():
            thread.join(1)
        with self.lock:
            # NativeMicrophone polls the stop event at 50 ms. Force-close only
            # our own object if a broken capture stream did not exit promptly.
            if thread and thread.is_alive() and self.mic:
                self.mic.close()
            self.microphone = self._empty_microphone('idle', '麦克风测试已停止，录音进程已释放')
            return self.status()

    def play_duck(self):
        with self.lock:
            if self.speaker is not None and self.speaker.poll() is None:
                return self.status()
            try:
                device = self._device(False)
                executable = self.which('aplay')
                if not executable:
                    raise AudioError('ZERO 缺少 aplay，请使用已配置 HAT 音频的系统镜像')
                asset, duration = duck_asset(self.config, self.install)
                levels, period_ms, duration_ms = duck_envelope(asset)
                proc = self.popen([executable, '-q', '-D', device, '-t', 'wav', str(asset)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, bufsize=0)
            except Exception as exc:
                self.playback.update(state='error', asset=None, error=str(exc), message=str(exc))
                raise
            self.speaker = proc
            self.playback_started = self.clock()
            self.playback_sequence += 1
            self.playback.update(state='playing', asset=asset.name, error='',
                envelope_dbfs=levels, envelope_period_ms=period_ms,
                duration_ms=duration_ms, source='playback-file', playback_sequence=self.playback_sequence,
                message=f'正在播放本机鸭叫（{duration:.1f} 秒）；听到声音后可确认扬声器出声')
            self.speaker_thread = threading.Thread(target=self._playback_done, args=(proc, duration),
                                                   name='HAT-Duck-Test', daemon=True)
            self.speaker_thread.start()
            return self.status()

    def _playback_done(self, proc, duration):
        error = ''
        try:
            _, stderr = proc.communicate(timeout=duration + 3)
            if proc.returncode:
                detail = (stderr or b'').decode(errors='replace').strip()[-1500:]
                error = 'HAT 鸭叫播放失败' + ('：' + detail if detail else '')
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            error = '鸭叫播放等待超时，已停止本次播放进程'
        except (OSError, ValueError) as exc:
            error = 'HAT 鸭叫播放失败：' + str(exc)
        finally:
            if proc.stderr:
                proc.stderr.close()
            with self.lock:
                if self.speaker is proc:
                    self.speaker = None
                    self.playback.update(state='error' if error else 'completed', error=error,
                        message=error or '鸭叫播放完成；是否出声请以实际听到的声音为准')

    @staticmethod
    def _terminate(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=.5)

    def stop_speaker(self):
        with self.lock:
            proc, self.speaker = self.speaker, None
            thread = self.speaker_thread
        if proc is not None:
            self._terminate(proc)
        if thread and thread is not threading.current_thread():
            thread.join(1)
        with self.lock:
            self.playback.update(state='idle', error='', message='鸭叫播放已停止')
            return self.status()

    def status(self):
        with self.lock:
            microphone = copy.deepcopy(self.microphone)
            at = microphone['updated_at']
            if microphone['state'] == 'running' and (at is None or self.clock() - at > FRESH_SECONDS):
                microphone.update(state='waiting', fresh=False, dbfs=None, rms=None, peak=None,
                                  message='等待新的麦克风 PCM，旧电平已清空')
            speaker = dict(self.playback)
            playing = speaker['state'] == 'playing' and self.playback_started is not None
            speaker['elapsed_ms'] = max(0., (self.clock() - self.playback_started) * 1000) if playing else 0.
            if speaker['state'] == 'completed':
                # A short chirp may finish before its first status reply arrives.
                # Keep this one measured file envelope, identified by its play
                # sequence, so the page can display the completed peak once.
                speaker['elapsed_ms'] = speaker.get('duration_ms', 0.)
            elif not playing:
                speaker['envelope_dbfs'] = []
            return {'microphone': microphone, 'speaker': speaker}

    def close(self):
        with self.lock:
            self.closed = True
        self.stop_microphone()
        self.stop_speaker()

