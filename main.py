"""Trae 自动签到（独立 Windows 程序）

功能：自动签到 + 签到记录 + 今日用量查看，不含其他统计。

用法：
    trae-checkin.exe                 打开今日用量窗口（可刷新 / 手动签到）
    trae-checkin.exe --silent        常驻后台，每天 9 点后随机分钟自动签到
    trae-checkin.exe --once          立即签到一次（已签的账号仍然跳过）
    trae-checkin.exe --status        查看今日状态与最近签到记录（不联网）
    trae-checkin.exe --install       注册开机自启（以 --silent 启动）
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


def make_tray_icon():
    """托盘图标：绿色圆 + 深色对勾"""
    from PIL import Image, ImageDraw
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(50, 240, 140, 255))
    draw.line((17, 33, 28, 44, 47, 20), fill=(6, 19, 12, 255), width=7, joint="curve")
    return image


def build_tray(show=None, checkin=None, toggle_autostart=None, quit_app=None):
    """系统托盘（pystray 不可用时返回 None，程序照常运行）"""
    try:
        import pystray
    except Exception:
        return None
    menu = pystray.Menu(
        pystray.MenuItem("打开今日用量", show or (lambda: None), default=True),
        pystray.MenuItem("立即签到", checkin or (lambda: None)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart or (lambda: None),
                         checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app or (lambda: None)),
    )
    return pystray.Icon("trae-checkin", make_tray_icon(), "Trae 签到", menu)


def launch_command():
    """开机自启命令：始终以 --silent 常驻（不弹窗口）"""
    extra = " --silent"
    if getattr(sys, "frozen", False):
        return '"%s"%s' % (sys.executable, extra)
    return '"%s" "%s"%s' % (sys.executable, os.path.abspath(__file__), extra)


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


def run_ui(args, show=True):
    """托盘常驻 + 今日用量窗口；关闭窗口只隐藏到托盘，退出走托盘菜单"""
    import queue
    import threading
    import tkinter as tk

    import today_usage

    hide_console()
    store_dir = checkin_core.CheckinStore(args.state_dir).dir
    # 窗口模式同样在后台跑自动签到，托盘常驻期间不依赖 --silent
    threading.Thread(target=loop, args=(args,), daemon=True).start()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    BG, CARD = "#0f141a", "#161d26"
    FG, MUTED, ACCENT, DANGER = "#e6edf3", "#8b98a5", "#32F08C", "#ff7b72"
    FONT = "Microsoft YaHei UI"

    root = tk.Tk()
    root.title("Trae 今日用量")
    root.geometry("600x500")
    root.minsize(540, 340)
    root.configure(bg=BG)
    if not show:
        root.withdraw()

    ui_queue = queue.Queue()

    def post(fn):
        ui_queue.put(fn)

    def pump():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(100, pump)

    def show_window():
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))

    def hide_window():
        root.withdraw()

    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=20, pady=(18, 4))
    tk.Label(head, text="Trae 今日用量", bg=BG, fg=FG,
             font=(FONT, 15, "bold")).pack(side="left")
    tk.Label(head, text=datetime.now().strftime("%Y-%m-%d"), bg=BG, fg=MUTED,
             font=(FONT, 10)).pack(side="right")

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=20, pady=8)

    foot = tk.Frame(root, bg=BG)
    foot.pack(fill="x", padx=20, pady=(4, 16))
    foot_left = tk.Frame(foot, bg=BG)
    foot_left.pack(side="left", fill="x", expand=True)
    note_label = tk.Label(foot_left, text="", bg=BG, fg=MUTED,
                          font=(FONT, 9), justify="left")
    note_label.pack(anchor="w")
    tip_label = tk.Label(foot_left, text="", bg=BG, fg=MUTED,
                         font=(FONT, 8), justify="left")
    tip_label.pack(anchor="w", pady=(2, 0))

    state = {"busy": False, "signing": False}

    def fmt_num(value):
        return "%.2f" % float(value or 0)

    def fmt_int(value):
        return "{:,}".format(int(value or 0))

    def flat_button(text, command, primary=False):
        return tk.Button(
            foot, text=text, command=command, relief="flat", bd=0,
            cursor="hand2", padx=16, pady=6,
            font=(FONT, 10, "bold" if primary else "normal"),
            bg=ACCENT if primary else CARD,
            fg="#06130c" if primary else FG,
            activebackground="#4dd9a0" if primary else "#222c38",
            activeforeground="#06130c" if primary else FG)

    def render(items):
        for widget in body.winfo_children():
            widget.destroy()
        if not items:
            tk.Label(body, text="未发现 Trae 客户端凭据\n请先登录 Trae 客户端",
                     bg=BG, fg=MUTED, font=(FONT, 10), justify="left").pack(
                anchor="w", pady=12)
        for item in items:
            card = tk.Frame(body, bg=CARD)
            card.pack(fill="x", pady=5)
            inner = tk.Frame(card, bg=CARD)
            inner.pack(fill="x", padx=16, pady=12)
            tk.Label(inner, text=item["uid"], bg=CARD, fg=FG,
                     font=(FONT, 10, "bold")).pack(anchor="w")
            if item["ok"]:
                line = "今日消耗  %s 积分  ·  会话 %d" % (
                    fmt_num(item["credits"]), item["sessions"])
                if item["tokens"]:
                    line += "  ·  %s token" % fmt_int(item["tokens"])
                tk.Label(inner, text=line, bg=CARD, fg=ACCENT,
                         font=(FONT, 10)).pack(anchor="w", pady=(4, 0))
                if item["balance"] is not None:
                    text = "剩余积分  %s" % fmt_num(item["balance"])
                    if item["total"] is not None:
                        text += "   /   总积分  %s" % fmt_num(item["total"])
                    tk.Label(inner, text=text, bg=CARD, fg=ACCENT,
                             font=(FONT, 9, "bold")).pack(anchor="w", pady=(2, 0))
            else:
                tk.Label(inner, text=item["message"] or "查询失败", bg=CARD,
                         fg=DANGER, font=(FONT, 9)).pack(anchor="w", pady=(4, 0))
        # 窗口高度按账号数自适应：单账号只占一个框的高度
        height = 330 + max(1, len(items)) * 95 if items else 370
        root.geometry("600x%d" % min(max(height, 370), 660))

    def sign_summary():
        today = datetime.now().strftime("%Y-%m-%d")
        store = checkin_core.CheckinStore(args.state_dir)
        try:
            import trae_client_credentials
            accounts = trae_client_credentials.discover()
        except Exception:
            accounts = []
        signed = sum(1 for acc in accounts if store.done_today(acc.uid, today))
        return accounts, signed, store.today_credits(today)

    def update_sign_button():
        accounts, signed, gained = sign_summary()
        if not accounts:
            note_label.configure(text="签到：未发现客户端账号")
            sign_btn.configure(state=tk.DISABLED, text="无账号", bg="#1f2732",
                               disabledforeground=MUTED)
            return
        note_label.configure(text="签到：%d/%d 已签 · 今日获得 +%s 积分" % (
            signed, len(accounts), fmt_num(gained)))
        if signed >= len(accounts):
            # 今日全部账号已签：按钮禁用，避免重复领取
            sign_btn.configure(state=tk.DISABLED, text="今日已签", bg="#1f2732",
                               disabledforeground=MUTED)
        else:
            sign_btn.configure(state=tk.NORMAL, text="立即签到", bg=ACCENT)

    def load_usage(force=False):
        if state["busy"]:
            return
        state["busy"] = True
        tip_label.configure(text="查询中…" if force else "")

        def work():
            try:
                items, error = today_usage.load(store_dir, force=force), ""
            except Exception as exc:
                items, error = [], str(exc)
            root.after(0, lambda: done(items, error))

        def done(items, error):
            state["busy"] = False
            render(items)
            update_sign_button()
            tip_label.configure(text=("查询失败：%s" % error) if error else
                                "更新于 " + datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=work, daemon=True).start()

    def do_checkin():
        if state["signing"]:
            return
        state["signing"] = True
        sign_btn.configure(state=tk.DISABLED, text="签到中…")

        def work():
            store = checkin_core.CheckinStore(args.state_dir)
            try:
                with process_lock(store.dir, timeout=10):
                    checkin_core.run_once(store, force=True, enabled=True,
                                          start_hour=args.start_hour,
                                          emit=lambda m: None)
            except Exception:
                pass
            root.after(0, done)

        def done():
            state["signing"] = False
            update_sign_button()
            load_usage(force=True)

        threading.Thread(target=work, daemon=True).start()

    def update_autostart():
        auto_btn.configure(
            text="开机自启：已开启（点击关闭）" if autostart_enabled()
            else "开机自启：未开启（点击开启）")

    def toggle_autostart():
        set_autostart(not autostart_enabled())
        update_autostart()
        try:
            tray.update_menu()
        except Exception:
            pass

    auto_btn = tk.Button(foot_left, text="", command=toggle_autostart, relief="flat",
                         bd=0, cursor="hand2", anchor="w", bg=BG, fg=MUTED,
                         activebackground=BG, activeforeground=FG,
                         font=(FONT, 8), padx=0, pady=0)
    auto_btn.pack(anchor="w", pady=(4, 0))
    update_autostart()

    sign_btn = flat_button("立即签到", do_checkin, primary=True)
    sign_btn.pack(side="right")
    refresh_btn = flat_button("刷新", lambda: load_usage(force=True))
    refresh_btn.pack(side="right", padx=(0, 8))

    def quit_app():
        try:
            tray.stop()
        except Exception:
            pass
        post(root.destroy)

    tray = build_tray(show=lambda: post(show_window),
                      checkin=lambda: post(do_checkin),
                      toggle_autostart=lambda: post(toggle_autostart),
                      quit_app=quit_app)
    if tray:
        try:
            tray.run_detached()
        except Exception:
            tray = None

    # 关闭窗口只隐藏到托盘，真正退出走托盘菜单
    root.protocol("WM_DELETE_WINDOW", hide_window)
    pump()
    update_sign_button()
    root.after(100, load_usage)
    # 双击后置前显示，避免窗口藏在浏览器/IDE 后面
    root.lift()
    root.attributes("-topmost", True)
    root.after(1200, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


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
    parser.add_argument("--silent", action="store_true",
                        help="托盘常驻 + 后台自动签到（不显示窗口）")
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
    if args.silent:
        run_ui(args, show=False)
        return 0
    run_ui(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
