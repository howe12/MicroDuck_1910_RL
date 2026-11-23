"""Build/check V297 Test against the delivered V296; no network or publication."""
import argparse
import ast
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = '1.0.297'
BUILD = '1fa843-feetech-ft6-control.102'
PREFIX = 'Microduck-Console-Studio-V297-Test/'
BASE_SHA = 'c5fc7b131b6b7ce605d42d06c29068e5f710aa7800b56f6c1dd144ac64148b5b'
TAR = 'updates/Microduck-Robotd-FT6-R17.tar.gz'
TAR_SHA = '18cecba4840e70c273c8fc5534cf5bd3e8386666f67b794c954d4ce3d0b74776'
NATIVE_SHA = '65bf917cd05f9304ae1d6602562f9aacc1953ae0b3d4fb067305a8f06678f7a0'
COMPONENT_ID = '20b83930ba7dad8f0753d416dc8156b2a5b7647748ab8b4560db7a92901affe6'
PRODUCTION_SHA = {
    'service_control.py': '1da62e7d3c9d6e4a5e5b141a30591c0fbe68dfed6ce8cb2ff29c6bcb2bae1513',
    'mode_switch_observer.py': '0182985aa6aa46b5a09e1e23cc5d1d2e11821053a5acf508d0cd0f248302881b',
    'static/four-pages.js': '7ba9e2f73a6a242cfbb9c97069847005b65e8bddb28c67ac32465eb51bf7b737',
    'static/assets/asset-sources.json': '83dbaa5c6f0abbb32f774548503a26c2bfba012327877bb79c3b4cd14d6f8333',
}


def sha(data): return hashlib.sha256(data).hexdigest()
def encoded(data): return (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode()
def require(condition, message):
    if not condition: raise ValueError(message)
def safe(name):
    path = PurePosixPath(name)
    require(bool(path.parts) and not path.is_absolute() and '..' not in path.parts and '\\' not in name,
            'Unsafe package path: '+name)
def payload():
    files = {}
    for path in sorted(ROOT.rglob('*')):
        require(not path.is_symlink(), 'Symlink in source')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            name=path.relative_to(ROOT).as_posix();safe(name);files[name]=path.read_bytes()
    return files
def baseline(path):
    require(sha(path.read_bytes()) == BASE_SHA, 'Wrong delivered V296 baseline')
    files = {}; modes = {}
    with zipfile.ZipFile(path) as archive:
        require(archive.testzip() is None, 'Baseline CRC failed')
        for item in archive.infolist():
            require(item.filename.startswith('Microduck-Console-Studio-V296/'), 'Wrong baseline prefix')
            name = item.filename.split('/',1)[1];safe(name)
            require(name not in files and not item.is_dir(), 'Duplicate baseline path')
            files[name]=archive.read(item);modes[name]=(item.external_attr>>16)&0o777 or 0o644
    require(len(files)==324, 'Baseline member count differs')
    return files,modes
def removals(old):
    return {n for n in old if n.startswith(('verification/','docs/releases/')) or n in {
        'docs/UPDATE-V292.zh-CN.md', 'static/assets/tutorials/numbering-wiring-source-v267.jpg',
        'static/assets/tutorials/hardware-wiring-source-v267.jpg','static/assets/tutorials/numbering-wiring-v267.png',
        'static/assets/tutorials/hardware-wiring-v267.png'}}


def check(files, old):
    removed=removals(old)
    require(len(removed)==149, 'Cleanup scope differs')
    changed=set(PRODUCTION_SHA)|{'release_identity.py','README.zh-CN.md','BUILD_MANIFEST.json'}
    for name,data in old.items():
        if name in removed: require(name not in files, 'Obsolete file retained: '+name)
        elif name not in changed: require(files.get(name)==data, 'Protected baseline changed: '+name)
    extra=set(files)-set(old)
    require(all(n in {'mode_switch_observer.py','docs/UPDATE-V297.zh-CN.md'} or n.startswith('verification/v297/')
                for n in extra), 'Unexpected added file')
    require(len(files)<=1024 and not any(n.endswith(('.log','.pyc')) for n in files), 'Invalid runtime artifacts')
    for name,digest in PRODUCTION_SHA.items(): require(sha(files[name])==digest, 'Frozen source changed: '+name)
    require(files['release_identity.py']==old['release_identity.py'].replace(b"CONSOLE_VERSION = '1.0.296'",b"CONSOLE_VERSION = '1.0.297'"), 'Identity differs')
    require(sha(files[TAR])==TAR_SHA and files[TAR]==old[TAR], 'Firmware changed')
    with tarfile.open(fileobj=io.BytesIO(files[TAR])) as archive:
        binary=next(i for i in archive.getmembers() if i.name.endswith('/robotd') and i.isfile())
        require(sha(archive.extractfile(binary).read())==NATIVE_SHA, 'Native control.102 changed')
    for name in files:
        if name.endswith('.py'): ast.parse(files[name].decode('utf-8-sig'), filename=name)
    for report_name in ('mode-switch-results.json','lifecycle-results.json'):
        report=json.loads(files['verification/v297/'+report_name]);require(report['passed'] is True,'Regression failed')
        for name,digest in report['source_sha256'].items():require(sha(files[name])==digest,'Tested source changed: '+name)
    for name in json.loads(files['static/assets/asset-sources.json'])['assets']:
        require('static/assets/'+name in files, 'Missing current image')
    require(not any(n.startswith('static/assets/tutorials/') and 'v267' in n for n in files),'Obsolete photo')
    return removed


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args();old,modes=baseline(args.baseline)
    files=payload();removed=check(files,old);old_meta=json.loads(old['BUILD_MANIFEST.json'])
    report={name:json.loads(files['verification/v297/'+name]) for name in ('mode-switch-results.json','lifecycle-results.json')}
    count=report['mode-switch-results.json']['cases']+len(report['lifecycle-results.json']['cases'])
    keep=('official_baseline','hardware_base','models','global_settings','protocol','startup_flow','web_input',
          'media_build','toolchain','hd1910_runtime_settings','hotspot_revision','component_limits')
    manifest={key:old_meta[key] for key in keep if key in old_meta}
    manifest.update(console_version=VERSION, service_build=BUILD, release_label='V297 Test (Beta)',
        release_channel='test',publication_hold=True,repository_published=False,generated_at_utc='2026-11-17T17:00:00Z',
        hardware_verified=False,firmware_reinstall_required=False,service_package_sha256=TAR_SHA,
        maintenance_component_id=COMPONENT_ID,package_baseline='V296 / control.102',baseline_zip_sha256=BASE_SHA,
        console_source_distribution='Microduck-Console-Studio-V297-Test.zip',native_binaries_changed=False,
        native_payload_changed=False,native_logic_changed=False,native_instructions_changed=False,native_recompiled=False,
        production_scope={'pc_backend_changes':['mode_switch_observer.py','service_control.py'],
                          'pc_metadata_cleanup':['static/four-pages.js','static/assets/asset-sources.json'],
                          'zero_logic_changes':[],'native_logic_changes':[]},
        release_changes=['硬件初始化等待时及时释放模式管理任务，测试与编号入口可用；包括缺 IMU、缺单颗或多颗舵机。',
                         '保留具体中文原因与原始错误；硬件就绪仍看实际反馈。',
                         '清理旧验证归档、堆叠历史文档及弃用接线图片；保留当前说明、验证源码和必要运行文件。'],
        cleanup={'removed_files':{n:{'bytes':len(old[n]),'sha256':sha(old[n])} for n in sorted(removed)},
                 'removed_bytes':sum(len(old[n]) for n in removed)},
        validation={'passed':True,'controlled_cases_passed':count,'reports':report,'hardware_tested':False,
                    'browser_pixels_tested':False,'known_good_promoted':False},
        source_review={'passed':True,'reviewer':'primary agent source review',
             'checks':['Only newly created service.sh/observer process group is signalled; systemd robotd is independent.',
                       'Owned waiter descendants are reaped before the management task completes and releases busy state.',
                       'Pending hardware is distinct from actual mode/process identity and from ready motion control.',
                       'Original stop/serial-release/owned-lease entry checks retained; no retries, HOME or Policy starts added.',
                       'Entire firmware and all protected baseline runtime bytes unchanged.']},
        known_good='Existing Reference Duck Known-Good retained; V297 test candidate is not physically accepted.',
        next_task='实机确认缺 IMU、缺单颗舵机时测试与编号入口及时可用；用户确认后再发布仓库 Beta 版。')
    manifest['files']={n:sha(data) for n,data in sorted(files.items()) if n!='BUILD_MANIFEST.json'}
    (ROOT/'BUILD_MANIFEST.json').write_bytes(encoded(manifest));files=payload();check(files,old)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(PREFIX+name,(2026,11,18,1,0,0));info.create_system=3
            info.external_attr=(stat.S_IFREG|modes.get(name,0o644))<<16
            archive.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    with zipfile.ZipFile(args.output) as archive:
        require(archive.testzip() is None,'Output CRC failure')
        require(len(archive.infolist())==len(files),'Output member count differs')
        require({i.filename[len(PREFIX):]:archive.read(i) for i in archive.infolist()}==files,'Output source mismatch')
    probe='''import base64,json,sys
from pathlib import Path
import firmware_packages,maintenance_components
info=firmware_packages.bundle_info()
assert info['verified'] and info['console_version']=='1.0.297' and info['sha256']==sys.argv[2]
b=maintenance_components.component_bundle();assert b['component_id']==sys.argv[3]
for n,row in b['files'].items():assert base64.b64decode(row['data'],validate=True)==Path(n).read_bytes()
p=Path(sys.argv[1])
with p.open('rb') as stream:prepared=firmware_packages.PreparedFirmware(p.name,stream,p.stat().st_size,'release-verification')
try:assert prepared.path.read_bytes()==Path('updates/'+firmware_packages.ARCHIVE_NAME).read_bytes()
finally:prepared.close()
print('Runtime bundle, components and Studio upload reader passed')
'''
    subprocess.run([sys.executable,'-B','-c',probe,str(args.output.resolve()),TAR_SHA,COMPONENT_ID],cwd=ROOT,check=True)
    result={'passed':True,'console_version':VERSION,'controlled_cases':count,'files':len(files),
            'removed_files':len(removed),'bytes':args.output.stat().st_size,'sha256':sha(args.output.read_bytes()),
            'firmware_sha256':TAR_SHA,'repository_published':False,'hardware_tested':False}
    args.output.with_suffix('.verification.json').write_bytes(encoded(result));print(json.dumps(result))


if __name__=='__main__': main()
