"""开机自启管理 (macOS launchd LaunchAgent)

供 Web 设置面板「开机自启」开关调用:
  - enable()   写入 ~/Library/LaunchAgents/<label>.plist 并 launchctl load
  - disable()  launchctl unload 并删除 plist
  - status()   返回 supported / installed / enabled

仅 macOS 支持; 其它平台返回 supported=False 并提示。
plist 使用当前运行的 venv 解释器与项目根目录动态生成, 因此克隆到任何用户目录都能用
(不依赖写死的绝对路径)。RunAtLoad=true 实现登录/开机自启; KeepAlive=false 避免在
端口被占用(如手动实例仍在跑)时崩溃重启循环 —— 当前手动运行的实例不受影响。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.binance-quant.trading"
PLIST_NAME = f"{LABEL}.plist"


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _loaded() -> bool:
    """服务是否已被 launchd 加载 (兼容 load 与 bootstrap 两种注册方式)"""
    # 方式1: 旧版 launchctl list <label> 命中即已加载
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    if r.returncode == 0:
        return True
    # 方式2: 新版 bootstrap 到 gui 域, 用 launchctl print 校验
    r = subprocess.run(["launchctl", "print", f"{_gui_domain()}/{LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / PLIST_NAME


def is_supported() -> bool:
    return sys.platform == "darwin"


def _generate_plist_xml() -> str:
    py = sys.executable
    run_py = ROOT / "run.py"
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / "launchd.out.log"
    err = logs / "launchd.err.log"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{run_py}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>{out}</string>
    <key>StandardErrorPath</key>
    <string>{err}</string>
</dict>
</plist>
'''


def enable() -> dict:
    if not is_supported():
        return {"ok": False, "msg": "❌ 当前系统不支持开机自启 (仅 macOS 支持 launchd)"}
    path = plist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_generate_plist_xml(), encoding="utf-8")
        # 先清掉旧注册, 避免 "already loaded" 报错
        subprocess.run(["launchctl", "bootout", f"{_gui_domain()}/{LABEL}"],
                       capture_output=True, text=True)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, text=True)
        # 优先用新版 bootstrap (macOS 10.11+), 失败回退旧版 load
        r = subprocess.run(["launchctl", "bootstrap", _gui_domain(), str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "msg": f"❌ launchctl 加载失败: {r.stderr.strip() or r.stdout.strip()}"}
    except Exception as e:
        return {"ok": False, "msg": f"❌ 写入/加载 plist 失败: {e}"}
    return {"ok": True, "msg": "✅ 开机自启已开启 (下次登录/开机自动启动)"}


def disable() -> dict:
    if not is_supported():
        return {"ok": False, "msg": "❌ 当前系统不支持开机自启 (仅 macOS 支持 launchd)"}
    path = plist_path()
    try:
        # 优先 bootout (对应 bootstrap), 失败回退 unload
        subprocess.run(["launchctl", "bootout", f"{_gui_domain()}/{LABEL}"],
                       capture_output=True, text=True)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, text=True)
        if path.exists():
            path.unlink()
    except Exception as e:
        return {"ok": False, "msg": f"❌ 卸载失败: {e}"}
    return {"ok": True, "msg": "✅ 开机自启已关闭"}


def status() -> dict:
    supported = is_supported()
    path = plist_path()
    installed = path.exists()
    loaded = bool(supported and installed and _loaded())
    return {"supported": supported, "installed": installed, "enabled": loaded,
            "label": LABEL, "path": str(path)}
