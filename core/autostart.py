"""开机自启管理 (跨平台)

供 Web 设置面板「开机自启」开关调用:
  - macOS: 生成 ~/Library/LaunchAgents/<label>.plist 并用 launchctl 注册
  - Windows: 在用户「启动」文件夹写入调用 start.bat 的批处理, 登录时自动运行
  - enable()   开启开机自启
  - disable()  关闭开机自启
  - status()   返回 supported / installed / enabled

仅 macOS / Windows 支持; 其它平台返回 supported=False 并提示。
plist / 启动项均使用项目根目录下的 start 脚本 (可移植, 克隆到任何用户目录都能用,
不依赖写死的绝对路径)。RunAtLoad=true (mac) 实现登录/开机自启; KeepAlive=false
避免在端口被占用(如手动实例仍在跑)时崩溃重启循环 —— 当前手动运行的实例不受影响。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.binance-quant.trading"
PLIST_NAME = f"{LABEL}.plist"
WIN_AUTOSTART_BAT = "binance-quant-autostart.bat"


# ---------------- 平台探测 ----------------
def is_supported() -> bool:
    return sys.platform == "darwin" or sys.platform == "win32"


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


def _startup_folder_win() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# ---------------- macOS (launchd) ----------------
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


def _enable_mac() -> dict:
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


def _disable_mac() -> dict:
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


def _status_mac() -> dict:
    path = plist_path()
    installed = path.exists()
    loaded = bool(installed and _loaded())
    return {"supported": True, "installed": installed, "enabled": loaded,
            "label": LABEL, "path": str(path)}


# ---------------- Windows (启动文件夹) ----------------
def _enable_win() -> dict:
    folder = _startup_folder_win()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        bat = folder / WIN_AUTOSTART_BAT
        # 调用项目根目录的 start.bat (自动启动服务 + 浏览器); 路径带空格也无妨 (已加引号)
        content = f'@echo off\n"{ROOT / "start.bat"}"\n'
        bat.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "msg": f"❌ 写入开机启动项失败: {e}"}
    return {"ok": True, "msg": "✅ 开机自启已开启 (下次登录自动启动)"}


def _disable_win() -> dict:
    bat = _startup_folder_win() / WIN_AUTOSTART_BAT
    try:
        if bat.exists():
            bat.unlink()
    except Exception as e:
        return {"ok": False, "msg": f"❌ 移除开机启动项失败: {e}"}
    return {"ok": True, "msg": "✅ 开机自启已关闭"}


def _status_win() -> dict:
    bat = _startup_folder_win() / WIN_AUTOSTART_BAT
    installed = bat.exists()
    return {"supported": True, "installed": installed, "enabled": installed,
            "label": LABEL, "path": str(bat)}


# ---------------- 统一入口 ----------------
def enable() -> dict:
    if not is_supported():
        return {"ok": False, "msg": "❌ 当前系统不支持开机自启 (仅 macOS / Windows 支持)"}
    if sys.platform == "win32":
        return _enable_win()
    return _enable_mac()


def disable() -> dict:
    if not is_supported():
        return {"ok": False, "msg": "❌ 当前系统不支持开机自启 (仅 macOS / Windows 支持)"}
    if sys.platform == "win32":
        return _disable_win()
    return _disable_mac()


def status() -> dict:
    if not is_supported():
        return {"supported": False, "installed": False, "enabled": False,
                "label": LABEL, "path": ""}
    if sys.platform == "win32":
        return _status_win()
    return _status_mac()
