"""Trae 自动签到（独立 Windows 程序）

只用两个功能：自动签到 + 签到记录，不含任何用量统计。

用法：
    trae-checkin.exe                 常驻后台，每天 9 点后随机分钟自动签到
    trae-checkin.exe --once          立即签到一次（已签的账号仍然跳过）
    trae-checkin.exe --status        查看今日状态与最近签到记录（不联网）
    trae-checkin.exe --install       注册开机自启（写入 HKCU Run）
    trae-checkin.exe --uninstall     取消开机自启

状态与记录目录：%USERPROFILE%\\.trae-checkin
    state.json    已签水位 / 失败时间 / 最近 100 条记录
    checkin.log   追加式文本日志
"""
import argparse
import ctypes
import msvcrt
import os
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta

import checkin_core

# 管道 / 重定向输出时控制台编码可能是 GBK：非终端场景统一 UTF-8，
# 终端场景只做降级替换（控制台本身用宽字符写入，中文不受影响）
try:
    if sys.stdout.isatty():
        sys.stdout.reconfigure(errors="replace")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_NAME = "TraeAutoCheckin"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_START_HOUR = 9
DEFAULT_INTERVAL = 60


@contextmanager
def process_lock(directory, timeout=30):
    """跨进程签到锁：常驻与手动 --once 不会同时 claim"""
    os.makedirs(directory, exist_ok=True)
    fh = open(os.path.join(directory, "checkin.lock"), "a+b")
    if fh.tell() == 0:
        fh.write(b"0")
        fh.flush()
    deadline = time.time() + timeout
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() >= deadline:
                fh.close()
                raise TimeoutError("另一个签到实例正在运行")
            time.sleep(1)
    try:
        yield
    finally:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()


def hide_console():
    """常驻模式隐藏控制台窗口（打包成 console exe 也能静默运行）"""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def launch_command():
    """开机自启命令：打包后指向 exe，开发态指向 python + main.py"""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))


def set_autostart(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except (OSError, FileNotFoundError):
        return False


def print_status(store):
    today = datetime.now().strftime("%Y-%m-%d")
    print("Trae 自动签到 · %s" % today)
    print("状态目录: %s" % store.dir)
    print("开机自启: %s" % ("已开启" if autostart_enabled() else "未开启"))
    try:
        import trae_client_credentials
        accounts = trae_client_credentials.discover()
    except Exception:
        accounts = []
    if not accounts:
        print("账号: 未发现 Trae 客户端凭据（请先登录 Trae 客户端）")
    for acc in accounts:
        done = store.done_today(acc.uid, today)
        slot = checkin_core.slot_minute(acc.uid, today)
        print("账号 %s（%s）" % (acc.uid, acc.source_app or "Trae"))
        print("  今日: %s" % ("已签" if done else "未签，计划 09:%02d 后签到" % slot))
    credits = store.today_credits(today)
    if credits:
        print("今日获得: +%g 积分" % credits)
    recent = store.recent(10)
    print("最近记录:")
    if not recent:
        print("  （无）")
    for rec in recent:
        print("  %s  uid=%s  %s  %s" % (
            datetime.fromtimestamp(rec["t"]).strftime("%Y-%m-%d %H:%M:%S"),
            rec["uid"],
            ("+%g" % rec["credits"]) if rec["credits"] else ("成功" if rec["ok"] else "失败"),
            rec["msg"]))


def loop(args):
    hide_console()
    interval = max(30, int(args.interval))
    while True:
        try:
            store = checkin_core.CheckinStore(args.state_dir)
            with process_lock(store.dir):
                checkin_core.run_once(
                    store, force=False, enabled=True, start_hour=args.start_hour,
                    emit=lambda m: None)   # 常驻模式不刷屏，记录在 state.json / checkin.log
        except TimeoutError:
            pass
        except Exception:
            try:
                with open(os.path.join(checkin_core.state_dir(), "error.log"),
                          "a", encoding="utf-8") as f:
                    f.write("%s\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                          traceback.format_exc()))
            except OSError:
                pass
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Trae 每日自动签到（自动签到 + 签到记录）")
    parser.add_argument("--once", action="store_true", help="立即签到一次（已签账号仍跳过）")
    parser.add_argument("--status", action="store_true", help="查看今日状态与最近记录")
    parser.add_argument("--install", action="store_true", help="注册开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    parser.add_argument("--start-hour", type=int, default=DEFAULT_START_HOUR,
                        help="每天起始小时（默认 9，整点后随机分钟）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="常驻检查间隔秒数（默认 60）")
    parser.add_argument("--state-dir", default=None, help="状态目录（默认 ~/.trae-checkin）")
    args = parser.parse_args(argv)

    if args.install:
        set_autostart(True)
        print("已开启开机自启：%s" % launch_command())
        return 0
    if args.uninstall:
        set_autostart(False)
        print("已取消开机自启")
        return 0

    store = checkin_core.CheckinStore(args.state_dir)
    if args.status:
        print_status(store)
        return 0
    if args.once:
        with process_lock(store.dir):
            results = checkin_core.run_once(
                store, force=True, enabled=True, start_hour=args.start_hour)
        if not results:
            print("没有可签到的账号")
        return 0
    loop(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
