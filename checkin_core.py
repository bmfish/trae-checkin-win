"""Trae 每日签到核心（独立版，只做签到，不含任何用量统计）

从 ccBar 抽出的最小实现，保留同样的安全策略：
  - 凭据只读本机 Trae 客户端存储（trae_client_credentials）
  - 每天 start_hour 之后按「账号 + 日期」确定性随机一个分钟签到
  - 今日已签（本地水位 / 服务端 did_checked_in）绝不重复 claim
  - 失败 1 小时内不重试；成功与失败都写本地记录 + 文本日志
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime

import trae_checkin
import trae_client_credentials

# 失败后的冷却时间（秒）：acquire/claim 失败不立刻重试，避免刷接口
FAIL_COOLDOWN = 3600
LOG_LIMIT = 100


def state_dir():
    """状态目录：默认 ~/.trae-checkin，可用 TRAE_CHECKIN_HOME 覆盖"""
    override = os.environ.get("TRAE_CHECKIN_HOME")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".trae-checkin")


def slot_minute(uid, day):
    """当天签到分钟（0–59）：按账号 + 日期确定性派生，重启不会重掷"""
    raw = hashlib.sha256(("%s:%s" % (uid, day)).encode("utf-8")).digest()
    return int.from_bytes(raw[:4], "big") % 60


def _day_of(epoch):
    return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d")


class CheckinStore:
    """本地状态（JSON）：已签水位 / 失败时间 / 签到记录"""

    def __init__(self, directory=None):
        self.dir = directory or state_dir()
        self.path = os.path.join(self.dir, "state.json")
        self.text_log = os.path.join(self.dir, "checkin.log")
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                obj.setdefault("done", {})
                obj.setdefault("fail", {})
                obj.setdefault("log", [])
                return obj
        except (OSError, ValueError, TypeError):
            pass
        return {"done": {}, "fail": {}, "log": []}

    def _save(self):
        with self._lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError:
                pass

    # ---- 水位 ----

    def done_today(self, uid, day):
        with self._lock:
            return self.state["done"].get(uid) == day

    def mark_done(self, uid, day):
        with self._lock:
            self.state["done"][uid] = day
            self.state["fail"].pop(uid, None)
            self._save()

    def fail_at(self, uid):
        with self._lock:
            try:
                return float(self.state["fail"].get(uid) or 0)
            except (TypeError, ValueError):
                return 0.0

    def mark_fail(self, uid, epoch):
        with self._lock:
            self.state["fail"][uid] = int(epoch)
            self._save()

    # ---- 记录 ----

    def append_log(self, uid, ok, credits, msg, epoch=None):
        rec = {
            "t": int(epoch if epoch is not None else time.time()),
            "uid": uid,
            "ok": bool(ok),
            "credits": float(credits or 0),
            "msg": msg or "",
        }
        with self._lock:
            log = self.state.setdefault("log", [])
            log.append(rec)
            if len(log) > LOG_LIMIT:
                del log[:-LOG_LIMIT]
            self._save()
        self._write_text(rec)
        return rec

    def _write_text(self, rec):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.text_log, "a", encoding="utf-8") as f:
                f.write("%s\tuid=%s\t%s\t%s\t%s\n" % (
                    datetime.fromtimestamp(rec["t"]).strftime("%Y-%m-%d %H:%M:%S"),
                    rec["uid"],
                    "OK" if rec["ok"] else "FAIL",
                    ("+%g" % rec["credits"]) if rec["credits"] else "-",
                    rec["msg"]))
        except OSError:
            pass

    def logged_today(self, uid, day):
        """今天是否已有该账号的成功记录（跨进程重复 claim 的兜底）"""
        with self._lock:
            log = list(self.state.get("log", []))
        for rec in log:
            if not isinstance(rec, dict) or not rec.get("ok") or rec.get("uid") != uid:
                continue
            t = int(rec.get("t") or 0)
            if t and _day_of(t) == day:
                return True
        return False

    def record_once(self, uid, day, credits, msg, epoch=None):
        """成功记录只写一笔；已有记录时只补金额，不重复记奖励"""
        if self.logged_today(uid, day):
            self._fixup(uid, day, credits)
        else:
            self.append_log(uid, True, credits, msg, epoch=epoch)

    def _fixup(self, uid, day, credits):
        with self._lock:
            log = self.state.get("log", [])
            idx = [i for i, rec in enumerate(log)
                   if isinstance(rec, dict) and rec.get("uid") == uid and rec.get("ok")
                   and int(rec.get("t") or 0) and _day_of(rec["t"]) == day]
            if not idx:
                return
            last = idx[-1]
            if credits > 0 and float(log[last].get("credits") or 0) == 0:
                log[last]["credits"] = float(credits)
                self._save()

    def recent(self, limit=10):
        """最近记录（时间倒序）"""
        with self._lock:
            return list(self.state.get("log", []))[-limit:][::-1]

    def today_credits(self, day):
        with self._lock:
            log = list(self.state.get("log", []))
        total = 0.0
        for rec in log:
            if isinstance(rec, dict) and rec.get("ok") and int(rec.get("t") or 0) \
                    and _day_of(rec["t"]) == day:
                total += float(rec.get("credits") or 0)
        return total


def _now(now=None):
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(float(now))


def _log(msg):
    print("[trae-checkin] %s" % msg, flush=True)


def run_once(store, now=None, force=False, enabled=True, start_hour=9,
             discover=None, emit=None):
    """执行一轮签到，返回本轮结果列表。

    每条结果是 {"uid", "action", "message", "credits"}，action 取值：
    done / already / skipped / failed / error。
    """
    emit = emit or _log
    if enabled is False and not force:
        return []
    now_dt = _now(now)
    now_epoch = now_dt.timestamp()
    start_hour = 9 if start_hour is None else max(0, min(23, int(start_hour)))
    today = now_dt.strftime("%Y-%m-%d")
    day_start = int(now_dt.replace(hour=0, minute=0, second=0,
                                   microsecond=0).timestamp())
    accounts = (discover or trae_client_credentials.discover)(now=now_dt)
    if not accounts:
        emit("未发现 Trae 客户端凭据（请先登录 Trae 客户端）")
        return []
    results = []
    for acc in accounts:
        action, message, credits = _checkin_account(
            store, acc, now_epoch, day_start, today,
            force=force, start_hour=start_hour)
        results.append({"uid": acc.uid, "action": action,
                        "message": message, "credits": credits})
        if action == "done":
            emit("uid %s 签到成功 +%g" % (acc.uid, credits))
        elif action == "already":
            emit("uid %s 今日已签（+%g）" % (acc.uid, credits))
        elif action == "failed":
            emit("uid %s 签到失败：%s" % (acc.uid, message))
        elif action == "skipped":
            emit("uid %s 跳过：%s" % (acc.uid, message))
    return results


def _checkin_account(store, acc, now_epoch, day_start, today,
                     force=False, start_hour=9):
    """单账号签到：返回 (action, message, credits)"""
    # 1) 本地水位：今日已签就彻底跳过，连 status 都不请求
    if store.done_today(acc.uid, today):
        return ("skipped", "今日已签", 0.0)
    # 2) 随机分钟门：起始小时 + 账号当天派生分钟
    slot_epoch = (day_start + start_hour * 3600
                  + slot_minute(acc.uid, today) * 60)
    if not force and now_epoch < slot_epoch:
        return ("skipped", "未到签到时间 %s" % datetime.fromtimestamp(
            slot_epoch).strftime("%H:%M"), 0.0)
    # 3) 失败冷却
    if not force and now_epoch - store.fail_at(acc.uid) < FAIL_COOLDOWN:
        return ("skipped", "失败冷却中", 0.0)
    # 4) JWT 过期先尝试 RT 续签，失败就等客户端自己续
    if not trae_client_credentials.jwt_usable(acc, now=now_epoch):
        refreshed = trae_client_credentials.refresh(acc, now=now_epoch)
        if not refreshed:
            return ("skipped", "JWT 已过期且无法续签", 0.0)
        acc.jwt = refreshed["jwt"]
        acc.jwt_exp = refreshed["jwt_exp"]
    # 5) 先用服务端状态判定是否已签
    st = trae_checkin.status(acc.jwt, acc.uid, acc.device_id)
    if st.code == 0 and not st.checked_in and not st.enable:
        return ("skipped", "活动未开启", 0.0)
    if st.code != 0 and not st.checked_in:
        store.append_log(acc.uid, False, 0, st.message, epoch=now_epoch)
        store.mark_fail(acc.uid, now_epoch)
        return ("failed", st.message or "状态查询失败", 0.0)
    reward = st.reward
    if st.checked_in:
        store.mark_done(acc.uid, today)
        store.record_once(acc.uid, today, reward, st.message or "今日已签",
                          epoch=now_epoch)
        return ("already", st.message or "今日已签", reward)
    # 6) claim 前再核一次水位（可能与另一进程/另一轮并发）
    if store.done_today(acc.uid, today):
        return ("skipped", "今日已签", 0.0)
    cl = trae_checkin.claim(acc.jwt, acc.uid, acc.device_id)
    if cl.ok:
        store.mark_done(acc.uid, today)
        # 服务端返回「今日已签」时只补水位，不再重复记奖励
        store.record_once(acc.uid, today, reward, cl.message or "今日已签",
                          epoch=now_epoch)
        return ("already", cl.message or "今日已签", reward) if cl.already \
            else ("done", cl.message or "success", reward)
    store.append_log(acc.uid, False, 0, cl.message, epoch=now_epoch)
    store.mark_fail(acc.uid, now_epoch)
    return ("failed", cl.message or "签到失败", 0.0)
