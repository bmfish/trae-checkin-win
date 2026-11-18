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
import json
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
SETTINGS_FILE = "settings.json"
DEFAULT_START_HOUR = 9
DEFAULT_INTERVAL = 60


def load_settings(directory):
    try:
        with open(os.path.join(directory, SETTINGS_FILE), encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(directory, values):
    merged = load_settings(directory)
    merged.update(values)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, SETTINGS_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(directory, SETTINGS_FILE))
    except OSError:
        pass


def configured_start_hour(state_dir=None):
    """签到起始小时：默认 9，界面/命令行可改"""
    raw = load_settings(state_dir or checkin_core.state_dir()).get("start_hour")
    try:
        return max(0, min(23, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_START_HOUR


def effective_start_hour(args, state_dir=None):
    """命令行显式 --start-hour 优先，否则读界面保存的配置"""
    value = getattr(args, "start_hour", None)
    if value is not None:
        return max(0, min(23, int(value)))
    return configured_start_hour(state_dir)


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


def apply_dark_titlebar(root, caption="#0f141a", text="#e6edf3"):
    """Windows 标题栏改深色：Win10 1903+ 支持暗色模式，Win11 可自定义颜色"""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            return
        enabled = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE（旧系统用 19）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(enabled), ctypes.sizeof(enabled))

        def colorref(value):
            r = int(value[1:3], 16)
            g = int(value[3:5], 16)
            b = int(value[5:7], 16)
            return ctypes.c_int(r | (g << 8) | (b << 16))

        # Win11 22H2+：标题栏 / 文字 / 边框跟随界面配色
        for attr, color in ((35, caption), (36, text), (34, caption)):
            value = colorref(color)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
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


def launch_command(start_hour=None):
    """开机自启命令：--silent 常驻，可带 --start-hour 固定签到起始小时"""
    extra = " --silent"
    if start_hour is not None:
        extra += " --start-hour %d" % int(start_hour)
    if getattr(sys, "frozen", False):
        return '"%s"%s' % (sys.executable, extra)
    return '"%s" "%s"%s' % (sys.executable, os.path.abspath(__file__), extra)


def set_autostart(enable, start_hour=None):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                              launch_command(start_hour))
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
    root.after(60, lambda: apply_dark_titlebar(root, caption=BG, text=FG))
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
        height = 365 + max(1, len(items)) * 95 if items else 405
        root.geometry("600x%d" % min(max(height, 405), 690))

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

    def sign_note(accounts, signed):
        if not accounts:
            return "签到：未发现客户端账号"
        text = "签到：%d/%d 已签" % (signed, len(accounts))
        store = checkin_core.CheckinStore(args.state_dir)
        today = datetime.now().strftime("%Y-%m-%d")
        gained = store.today_credits(today)
        if gained:
            text += " · 今日获得 +%s 积分" % fmt_num(gained)
        if signed < len(accounts):
            hour = effective_start_hour(args, store_dir)
            slots = ["%02d:%02d" % (hour, checkin_core.slot_minute(acc.uid, today))
                     for acc in accounts if not store.done_today(acc.uid, today)]
            if slots:
                text += " · 今天 %s 自动签" % " / ".join(slots)
        return text

    def update_sign_button():
        accounts, signed, gained = sign_summary()
        if not accounts:
            note_label.configure(text="签到：未发现客户端账号")
            sign_btn.configure(state=tk.DISABLED, text="无账号", bg="#1f2732",
                               disabledforeground=MUTED)
            return
        note_label.configure(text=sign_note(accounts, signed))
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
                                          start_hour=effective_start_hour(args, store_dir),
                                          emit=lambda m: None)
            except Exception:
                pass
            root.after(0, done)

        def done():
            state["signing"] = False
            update_sign_button()
            load_usage(force=True)

        threading.Thread(target=work, daemon=True).start()

    hour_var = tk.StringVar(value="%02d" % configured_start_hour(store_dir))

    def on_hour_change(*_):
        try:
            hour = max(0, min(23, int(float(hour_var.get()))))
        except (TypeError, ValueError):
            hour_var.set("%02d" % configured_start_hour(store_dir))
            return
        hour_var.set("%02d" % hour)
        save_settings(store_dir, {"start_hour": hour})
        if autostart_enabled():
            set_autostart(True, hour)   # 自启命令同步新的起始小时
        update_range_text()
        update_sign_button()
        tip_label.configure(text="已保存：%d 点后随机一分钟自动签到" % hour)

    time_row = tk.Frame(foot_left, bg=BG)
    time_row.pack(anchor="w", pady=(4, 0))
    tk.Label(time_row, text="签到时间", bg=BG, fg=MUTED,
             font=(FONT, 8)).pack(side="left")
    hour_box = tk.Spinbox(time_row, from_=0, to=23, width=3, textvariable=hour_var,
                          command=on_hour_change, font=(FONT, 9), justify="center",
                          bg=CARD, fg=FG, insertbackground=FG, relief="flat", bd=0,
                          buttonbackground="#222c38")
    hour_box.pack(side="left", padx=4)
    hour_box.bind("<Return>", on_hour_change)
    hour_box.bind("<FocusOut>", on_hour_change)
    range_label = tk.Label(time_row, text="", bg=BG, fg=MUTED, font=(FONT, 8))
    range_label.pack(side="left")

    def update_range_text():
        hour = configured_start_hour(store_dir)
        range_label.configure(text="点后  %02d:00–%02d:59 随机一分钟" % (hour, hour))

    update_range_text()

    def update_autostart():
        auto_btn.configure(
            text="开机自启：已开启（点击关闭）" if autostart_enabled()
            else "开机自启：未开启（点击开启）")

    def toggle_autostart():
        set_autostart(not autostart_enabled(), configured_start_hour(store_dir))
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
    tip_label.pack(anchor="w", pady=(4, 0))
    tk.Label(foot_left, text="by bmfish · github.com/bmfish/trae-checkin-win",
             bg=BG, fg="#5b6673", font=(FONT, 7)).pack(anchor="w", pady=(6, 0))

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
            # 每轮重新读配置：界面上改了签到时间立即生效
            start_hour = effective_start_hour(args, args.state_dir)
            with process_lock(store.dir):
                checkin_core.run_once(
                    store, force=False, enabled=True, start_hour=start_hour,
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
    parser.add_argument("--start-hour", type=int, default=None,
                        help="每天起始小时（0-23；缺省读界面配置，未配置为 9）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="常驻检查间隔秒数（默认 60）")
    parser.add_argument("--state-dir", default=None, help="状态目录（默认 ~/.trae-checkin）")
    args = parser.parse_args(argv)

    if args.install:
        hour = effective_start_hour(args, args.state_dir)
        set_autostart(True, hour)
        print("已开启开机自启：%s" % launch_command(hour))
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
                store, force=True, enabled=True,
                start_hour=effective_start_hour(args, args.state_dir))
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
