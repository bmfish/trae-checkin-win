"""今日用量查询（Trae 官方 API，按账号汇总）

只读：用本机 Trae 客户端 JWT 调用量接口，不写任何状态。
  - 会话明细：query_user_usage_group_by_session（今日 00:00 至今）
  - 积分余额：ide_user_ent_usage（consumed_amount / total_amount）
"""
from datetime import datetime
import json
import os
import time

import trae_client_credentials
import trae_sync

# 窗口打开时优先用 60 秒内的缓存，避免每次点开都打接口
CACHE_TTL = 60


def today_range(now=None):
    """今日本地零点 → 当前时刻（epoch 秒）"""
    dt = now if isinstance(now, datetime) else datetime.now()
    start = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return start, int(dt.timestamp())


def query(now=None):
    """逐账号查询今日用量。

    返回列表，每项：
      {uid, app, ok, sessions, credits, tokens, consumed, total, message}
    """
    dt = now if isinstance(now, datetime) else datetime.now()
    start, end = today_range(dt)
    items = []
    for acc in trae_client_credentials.discover(now=dt):
        item = {"uid": acc.uid, "app": acc.source_app or "Trae", "ok": False,
                "sessions": 0, "credits": 0.0, "tokens": 0,
                "consumed": None, "total": None, "balance": None, "message": ""}
        if not trae_client_credentials.jwt_usable(acc, now=end):
            refreshed = trae_client_credentials.refresh(acc, now=end)
            if refreshed:
                acc.jwt = refreshed["jwt"]
                acc.jwt_exp = refreshed["jwt_exp"]
            else:
                item["message"] = "JWT 已过期，请先打开 Trae 客户端"
                items.append(item)
                continue
        result = trae_sync.run("", jwt=acc.jwt, from_epoch=start, to_epoch=end,
                               fetch_ent=True, now=dt)
        if result.is_ok:
            item["ok"] = True
            item["sessions"] = len(result.rows)
            item["credits"] = round(sum(r.credits for r in result.rows), 2)
            item["tokens"] = sum(r.input + r.output + r.cache_read + r.cache_write
                                 for r in result.rows)
            item["consumed"] = result.consumed
            item["total"] = result.total
            item["balance"] = result.balance
        else:
            item["message"] = result.message or result.status
        items.append(item)
    return items


def cache_file(directory):
    return os.path.join(directory, "usage_cache.json")


def load(directory, force=False, now=None):
    """带缓存的今日用量：60 秒内且同一天直接返回缓存，force=True 强制查询"""
    dt = now if isinstance(now, datetime) else datetime.now()
    today = dt.strftime("%Y-%m-%d")
    path = cache_file(directory)
    if not force:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            fresh = time.time() - float(cached.get("ts") or 0) < CACHE_TTL
            items = cached.get("items")
            if cached.get("date") == today and fresh and isinstance(items, list) and items:
                return items
        except (OSError, ValueError, TypeError):
            pass
    items = query(now=dt)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "date": today, "items": items},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
    return items
