#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy / CodeBuddy 多账号自动签到 + 每日活跃消息
=====================================================
功能:
  1. 每日签到领积分 (POST /v2/billing/meter/daily-checkin)
  2. 用 deepseek-v4-flash 发一条消息保持账号活跃 (POST /v2/chat/completions, stream)
  3. token 过期检测 (JWT exp, 剩余 < 7 天告警)
  4. 多账号轮询, 单号失败不影响其他号, 失败自动重试
  5. 结果推送通知 (飞书群机器人 Webhook / Server酱微信, 可选)

配置方式 (二选一):
  A. 环境变量 WORKBUDDY_ACCESS_TOKENS (GitHub Actions 用):
     [{"name":"号1","access_token":"...","domain":"www.codebuddy.cn"}, ...]
  B. 本地文件 config.json (本地测试用, 勿提交 git):
     [{"name":"号1","access_token":"...","domain":"www.codebuddy.cn"}, ...]

可选环境变量:
  FEISHU_APP_ID     飞书应用机器人 App ID (与 FEISHU_APP_SECRET/FEISHU_CHAT_ID 一起填, 推送飞书群)
  FEISHU_APP_SECRET 飞书应用机器人 App Secret
  FEISHU_CHAT_ID    飞书目标群聊 ID (oc_ 开头)
  FEISHU_WEBHOOK    飞书群机器人 Webhook URL (替代上面三个的另一种方式)
  SCKEY             Server酱 SendKey, 填了则推送微信通知 (与飞书二选一或同时)
  MESSAGE_TEXT      每日活跃消息内容, 默认 "你好呀 我来了"
  CHECKIN_HOUR      仅在指定小时(本地时区)执行签到, 留空不限制

零第三方依赖, 纯 Python 标准库. Python 3.8+.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://www.codebuddy.cn"
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-status"
CHECKIN_ACTION_PATH = "/v2/billing/meter/daily-checkin"
CHAT_PATH = "/v2/chat/completions"
DEFAULT_MESSAGE = "你好呀 我来了"
TOKEN_WARN_DAYS = 7
RETRY_TIMES = 2
RETRY_DELAY = 30
JITTER_MIN, JITTER_MAX = 2, 5  # 多账号间随机延迟(秒)

BJ_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 工具函数

def bj_now() -> datetime:
    """北京时间"""
    return datetime.now(BJ_TZ)


def load_accounts() -> list[dict]:
    """优先环境变量, 其次本地 config.json"""
    raw = os.environ.get("WORKBUDDY_ACCESS_TOKENS", "").strip()
    if raw:
        return json.loads(raw)
    # 本地测试模式
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "config.json"), "config.json"):
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.loads(f.read())
    sys.exit("错误: 未找到账号配置。请设置 WORKBUDDY_ACCESS_TOKENS 或提供 config.json")


def decode_jwt_exp(token: str):
    """解析 JWT exp, 返回 (exp_ts, days_left), 解析失败返回 (None, None)"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None, None
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        import base64
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if not exp:
            return None, None
        days = (datetime.fromtimestamp(exp, tz=BJ_TZ) - bj_now()).days
        return exp, days
    except Exception:
        return None, None


def http_post(path: str, token: str, payload: dict | None = None,
              headers: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    """POST 请求, 返回 (status, body)"""
    url = API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:500]
    except Exception as e:
        return -1, str(e)


def retry_call(path: str, token: str, payload=None, headers=None) -> tuple[int, str]:
    """带重试的请求"""
    last = (-1, "")
    for i in range(RETRY_TIMES + 1):
        last = http_post(path, token, payload, headers)
        if last[0] in (200, 201, 400, 401, 403, 404):  # 业务/鉴权类错误不重试
            return last
        if i < RETRY_TIMES:
            time.sleep(RETRY_DELAY)
    return last


# ---------------------------------------------------------------- 业务函数

def checkin_status(token: str) -> dict:
    """查签到状态, 返回 data 字段
    ⚠️ 实测该接口状态不可靠 (与 daily-checkin 判断不一致), 仅作参考, 不用于业务分支"""
    try:
        status, body = retry_call(CHECKIN_STATUS_PATH, token, payload={})
        if status != 200:
            return {}
        j = json.loads(body)
        if j.get("code") != 0:
            return {}
        return j.get("data") or {}
    except Exception:
        return {}


def do_checkin(token: str) -> dict:
    """执行签到, 返回结果描述
    直接调签到接口, 用返回码判断:
      code=0    成功
      code=10001 今天已签到 (视为跳过, 非失败)
    """
    status, body = retry_call(CHECKIN_ACTION_PATH, token, payload={})
    # 注意: "今天已签到" 返回 HTTP 400 + body code 10001, 必须解析 body 而非只看 status
    if status not in (200, 400):
        return {"ok": False, "msg": f"HTTP {status}: {body[:200]}"}
    try:
        j = json.loads(body)
        if j.get("code") == 0:
            d = j.get("data") or {}
            return {"ok": True, "msg": f"+{d.get('credit', '?')}积分, 连签{d.get('streak_days', '?')}天"}
        if j.get("code") == 10001:  # 今天已签到
            return {"ok": True, "already": True, "msg": "今日已签到"}
        return {"ok": False, "msg": f"业务错误: {body[:200]}"}
    except Exception:
        return {"ok": False, "msg": f"响应解析失败: {body[:200]}"}


def send_active_message(token: str, text: str) -> dict:
    """用 deepseek-v4-flash 发一条活跃消息 (stream 模式, 读取首个 content 片段即判定成功)"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 16,          # 只让模型回一个字, 省积分
        "stream": True,
    }
    headers = {"Accept": "text/event-stream"}
    status, body = retry_call(CHAT_PATH, token, payload=payload, headers=headers)
    if status != 200:
        return {"ok": False, "msg": f"HTTP {status}: {body[:200]}"}
    # stream 成功 = 收到 data: 事件流 (即使内容为空也视为请求成功)
    if body.strip().startswith("data:"):
        return {"ok": True, "msg": "活跃消息已发送"}
    return {"ok": False, "msg": f"响应异常: {body[:200]}"}


def notify(summary: str, lines: list[str]) -> None:
    """推送结果通知。支持三种渠道, 填了哪个推哪个, 都填都推:
      1) 飞书应用机器人: FEISHU_APP_ID + FEISHU_APP_SECRET + FEISHU_CHAT_ID (推荐, 与 daily-automations 仓库现有方案一致)
      2) 飞书群机器人 Webhook: FEISHU_WEBHOOK
      3) Server酱(微信): SCKEY
    """
    import urllib.parse
    text = "\n".join(lines)
    title = f"WorkBuddy签到 {summary}"

    # 1) 飞书应用机器人 (tenant_access_token + im/v1/messages)
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if app_id and app_secret and chat_id:
        try:
            # 获取 tenant_access_token
            tok_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            tok_body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
            req = urllib.request.Request(tok_url, data=tok_body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                tok_data = json.loads(resp.read().decode("utf-8"))
            if tok_data.get("code") == 0:
                tenant_token = tok_data["tenant_access_token"]
                # 发送文本消息到群
                msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
                msg_body = json.dumps({
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": f"{title}\n{text}"}, ensure_ascii=False),
                }, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(msg_url, data=msg_body, method="POST", headers={
                    "Authorization": f"Bearer {tenant_token}",
                    "Content-Type": "application/json; charset=utf-8",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
        except Exception:
            pass

    # 2) 飞书群机器人 Webhook
    feishu = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if feishu:
        body = json.dumps({
            "msg_type": "text",
            "content": {"text": f"{title}\n{text}"},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(feishu, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception:
            pass

    # 3) Server酱 (微信)
    key = os.environ.get("SCKEY", "").strip()
    if key:
        payload = f"title={urllib.parse.quote(title)}&desp={urllib.parse.quote(text)}"
        try:
            url = f"https://sctapi.ftqq.com/{key}.send"
            req = urllib.request.Request(url, data=payload.encode("utf-8"),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception:
            pass


# ---------------------------------------------------------------- 主流程

def main() -> int:
    accounts = load_accounts()
    now = bj_now()
    print(f"=== WorkBuddy 自动签到开始 [{now.strftime('%Y-%m-%d %H:%M:%S')}] 共{len(accounts)}个账号 ===")

    # 可选: 指定时段才执行 (防时区/测试干扰)
    hour_str = os.environ.get("CHECKIN_HOUR", "").strip()
    if hour_str and now.hour != int(hour_str):
        print(f"当前小时 {now.hour} != 指定 {hour_str}, 跳过")
        return 0

    results = []
    success, skip, failed, warn = 0, 0, 0, []

    for i, acct in enumerate(accounts):
        name = acct.get("name", f"账号{i+1}")
        token = acct.get("access_token", "").strip()
        domain = acct.get("domain", "www.codebuddy.cn")

        if not token:
            print(f"[{name}] 无 access_token, 跳过")
            failed += 1
            results.append(f"❌ {name}: 无 token")
            continue

        print(f"\n--- [{name}] ---")

        # token 过期检测
        _, days_left = decode_jwt_exp(token)
        if days_left is None:
            warn.append(f"{name}: 无法解析token有效期")
        elif days_left < 0:
            results.append(f"❌ {name}: token已过期({-days_left}天前), 需重新导出")
            failed += 1
            print(f"  [跳过] token 已过期")
            continue
        elif days_left < TOKEN_WARN_DAYS:
            warn.append(f"{name}: token仅剩{days_left}天, 需更新")
            print(f"  [警告] token 剩余 {days_left} 天")

        # 1) 直接执行签到 (status接口不可靠, 不再依赖它做分支)
        r = do_checkin(token)
        if r.get("already"):
            print(f"  [跳过] 今日已签到")
            skip += 1
            results.append(f"⏭ {name}: 今日已签到")
        elif r["ok"]:
            print(f"  [成功] 签到 {r['msg']}")
            success += 1
            results.append(f"✅ {name}: 签到{r['msg']}")
        else:
            print(f"  [失败] 签到 {r['msg']}")
            failed += 1
            results.append(f"❌ {name}: 签到{r['msg']}")

        # 3) 发活跃消息 (无论今日是否已签到都发, 活跃判定独立于签到)
        #    注意: 用 WX-GLP 实测发现 status 接口已签到=false 但实际已签到,
        #    所以这里统一直接调 daily-checkin 判断, 不依赖 status
        msg_text = os.environ.get("MESSAGE_TEXT", DEFAULT_MESSAGE).strip()
        r = send_active_message(token, msg_text)
        if r["ok"]:
            print(f"  [成功] 活跃消息已发送")
            results[-1] += " · 活跃✅"
        else:
            print(f"  [警告] 活跃消息失败: {r['msg']}")
            results[-1] += " · 活跃❌"

        # 4) 多账号随机延迟, 模拟人工节奏
        if i < len(accounts) - 1:
            delay = random.uniform(JITTER_MIN, JITTER_MAX)
            time.sleep(delay)

    # 汇总
    total = len(accounts)
    summary = f"成功{success} 跳过{skip} 失败{failed}"
    print(f"\n=== 完成: {summary} ===")
    for line in results:
        print("  " + line)
    if warn:
        print("\n[告警]")
        for w in warn:
            print("  ⚠️ " + w)

    # 推送通知 (飞书 Webhook / Server酱, 可选)
    lines_out = results + ([f"⚠️ {w}" for w in warn] if warn else [])
    notify(f"{success}/{total}", lines_out)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
