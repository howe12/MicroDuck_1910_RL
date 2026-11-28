"""头撑地逃逸率：从头撑地位姿重置，看策略能否自己站起来。"""
import numpy as np, torch, onnxruntime as ort
from mjlab.envs import ManagerBasedRlEnv
from mjlab.tasks.registry import load_env_cfg
import mjlab_microduck.tasks

s = ort.InferenceSession('policies/hd1910_headplant.onnx')
inp, out = s.get_inputs()[0].name, s.get_outputs()[0].name
batch = s.get_inputs()[0].shape[0]
N = 64
cfg = load_env_cfg('Mjlab-HeadPlant-MicroDuck-HD1910')
cfg.scene.num_envs = N
cfg.events['head_plant_init'].params['head_plant_prob'] = 1.0
cfg.curriculum.pop('head_plant_prob', None)
cfg.auto_reset = False
cfg.terminations = {}
cfg.episode_length_s = 20.0
env = ManagerBasedRlEnv(cfg=cfg, device='cuda:0')
env.reset()
robot = env.scene['robot']
upright = np.zeros(N, bool)
first_up = np.full(N, -1)
try:
    for step in range(400):          # 8 s
        obs = env.obs_buf['actor'].detach().cpu().numpy().astype(np.float32)
        act = (np.concatenate([s.run([out], {inp: obs[i:i+1]})[0] for i in range(N)])
               if batch == 1 else s.run([out], {inp: obs})[0]).astype(np.float32)
        env.step(torch.from_numpy(act).to(env.device))
        g = robot.data.projected_gravity_b.detach().cpu().numpy()
        tilt = np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1)))
        now_up = tilt < 20
        newly = now_up & ~upright
        first_up[newly] = step
        upright |= now_up
finally:
    env.close()
ok = first_up[first_up >= 0]
print(f"头撑地姿态重置 {N} 个环境，8 秒内站起 {upright.mean():.0%}")
if len(ok):
    print(f"  站起时间中位 {np.median(ok)/50:.2f} s，p90 {np.percentile(ok,90)/50:.2f} s")
print(f"  4 秒内站起 {((first_up>=0)&(first_up<200)).mean():.0%}")
