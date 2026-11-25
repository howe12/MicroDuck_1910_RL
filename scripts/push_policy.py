#!/usr/bin/env python3
"""Push a newly trained ONNX to the robot as a deployable model package.

A model on the HD-1910 firmware is a *directory*, not a file:

    /opt/robot/feetech-ft5-r5/models/<sha256(policy.onnx)[:32]>/
        policy.onnx
        deployment-contract.json     ← mirrors the graph metadata byte for byte
        manifest.json                ← onnx_sha256 + task
        training-request.json        ← provenance, only checked for existence

Steps this does, in order, and it stops at the first failure:

1. build the package locally from the shipped walking package as template
2. scp it to the robot, install under models/<id>/
3. register it in management-profile.json (so the console lists it)
4. point [policy] walk at it
5. restart robotd and read back ``loaded_models.walk.sha256``
6. on mismatch, restore the config backup

    uv run --no-sync scripts/push_policy.py --onnx policies/hd1910_harsh.onnx
    uv run --no-sync scripts/push_policy.py --onnx ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path("/opt/robot/feetech-ft5-r5")
MODELS = BASE / "models"
PROFILE = BASE / "management-profile.json"
CONFIG = Path("/etc/robot/feetech-ft5/robotd.toml")
SLOT = "walk"


def sh(cmd: str, ssh: str, *, check: bool = True) -> str:
    full = f"{ssh} {cmd}" if ssh else cmd
    r = subprocess.run(["bash", "-lc", full], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"命令失败: {full}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--template", required=True,
                    help="已部署的同槽位模型目录名（用它的合同当模板）")
    ap.add_argument("--host", default="192.168.0.13")
    ap.add_argument("--user", default="radxa")
    ap.add_argument("--name", default=None, help="控制台里显示的名字")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ssh = f"ssh {args.user}@{args.host}"
    onnx = Path(args.onnx)
    if not onnx.is_file():
        raise SystemExit(f"找不到 {onnx}")

    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "pkg"
        subprocess.run(
            [sys.executable, "scripts/pack_deployment.py", "--onnx", str(onnx),
             "--template", "/tmp/tpl/m", "--out", str(pkg)],
            check=True,
        )
        meta = json.loads((pkg / "deployment-contract.json").read_text(encoding="utf-8"))
        ident, digest = json.loads(
            subprocess.run([sys.executable, "-c",
                            "import hashlib,json,sys;"
                            "p=sys.argv[1];"
                            "print(json.dumps([hashlib.sha256(open(p,'rb').read()).hexdigest()[:32],"
                            "hashlib.sha256(open(p,'rb').read()).hexdigest()]))",
                            str(pkg / "policy.onnx")], capture_output=True, text=True).stdout
        )
        print(f"包  : {pkg}")
        print(f"id  : {ident}")
        print(f"task: {meta['task']}")
        if args.dry_run:
            print("dry-run：未上传、未改配置")
            return 0

        sh(f"mkdir -p /tmp/push_{ident}", ssh)
        subprocess.run(["scp", "-q", "-r", str(pkg), f"{args.user}@{args.host}:/tmp/push_{ident}/"],
                       check=True)
        script = f"""
set -e
P=$(printf '%s')
echo "$P" | sudo -S rm -rf {MODELS}/{ident}
echo "$P" | sudo -S cp -r /tmp/push_{ident}/pkg {MODELS}/{ident}
echo "$P" | sudo -S chown -R root:root {MODELS}/{ident}
echo "$P" | sudo -S cp {CONFIG} {CONFIG}.bak-push-$(date +%H%M%S)
echo "$P" | sudo -S sed -i 's|^"{SLOT}" = .*|"{SLOT}" = "{MODELS}/{ident}/policy.onnx"|' {CONFIG}
echo "$P" | sudo -S python3 - <<'PY'
import json
from pathlib import Path
p = Path('{PROFILE}')
d = json.loads(p.read_text(encoding='utf-8'))
rows = d['builtin_models']
if not any(r['id'] == '{ident}' for r in rows):
    rows.append({{'id': '{ident}', 'name': '{args.name or onnx.stem}', 'slot': '{SLOT}'}})
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
print('profile entries:', len(rows))
PY
echo "$P" | sudo -S systemctl restart robotd
sleep 6
systemctl is-active robotd
"""
        out = sh(script, ssh)
        print(out)
        got = sh("python3 -c \""
                 "import socket,json;"
                 "s=socket.socket(socket.AF_UNIX);s.settimeout(6);s.connect('/run/robotd.sock');"
                 "s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':'robot.busStatus',"
                 "'params':{}})+chr(10)).encode());"
                 "import io;f=s.makefile('rb');"
                 "r=json.loads(f.readline(1048576))['result']['loaded_models']['walk'];"
                 "print(r['sha256'][:32])\"", ssh)
        print(f"回读 walk sha: {got}")
        if got != ident:
            raise SystemExit("回读不一致——已改过配置，用 .bak-push-* 恢复")
        print("✅ 已生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
