"""订阅中心：用 GitHub Issue 当「免后端」的订阅存储。

解决的问题是：看板是静态页、监控跑在 Actions 里，中间没有服务器，
用户在面板上填的条件怎么让监控拿到？

做法：面板「一键创建订阅」= 打开一个预填好的 GitHub Issue（标题以 ``[订阅]`` 开头，
正文带一段 JSON）；监控每轮开始时用公开 API 读取仓库的开放 Issue，把 JSON 解析成
订阅规则。**关闭 Issue 即退订**，改条件就编辑 Issue —— 不用碰 config.json，也不用 git。

邮件提醒有两条路，都不需要用户再配置什么：
1. 监控在订阅 Issue 下回执命中结果 —— GitHub 自动向订阅者发邮件（零配置）；
2. 若仓库 Secrets 里配了 SMTP，则再把邮件发到订阅里填的 ``email`` 地址。

公开仓库读 Issue 不需要 token；回执（写评论）需要 Actions 自带的 ``GITHUB_TOKEN``。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.github.com"
SUBSCRIBE_TITLE_PREFIX = "[订阅]"
SUBSCRIBE_MARKER = "szhitch-subscription-v1"
TIMEOUT = 15

#: 允许从 Issue JSON 进入规则的键（白名单，防止塞进任意配置）
ALLOWED_KEYS = {
    "name", "email",
    "pickup_cities", "return_cities", "any_cities", "routes",
    "brands", "exclude_brands", "models", "exclude_models",
    "max_price", "min_price", "max_price_per_day",
    "only_special", "special_price", "min_days", "max_days",
    "date_from", "date_to",
}

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


def is_subscription_title(title: str) -> bool:
    return str(title or "").lstrip().startswith(SUBSCRIBE_TITLE_PREFIX)


def parse_issue_body(body: str) -> dict[str, Any] | None:
    """从 Issue 正文提取订阅 JSON。接受单个对象；返回 None 表示没有有效订阅。

    只保留 ALLOWED_KEYS 里的键，其余一律丢弃。
    """
    for block in _FENCE.findall(str(body or "")):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            data = data[0] if data else None
        cleaned = clean_profile(data)
        if cleaned:
            return cleaned
    return None


def clean_profile(data: Any) -> dict[str, Any] | None:
    """只保留 ALLOWED_KEYS；没有任何筛选条件时返回 None（避免订阅全量）。"""
    if not isinstance(data, dict):
        return None
    cleaned = {k: v for k, v in data.items() if k in ALLOWED_KEYS}
    has_filter = any(
        k in cleaned and cleaned[k]
        for k in ("pickup_cities", "return_cities", "any_cities", "routes",
                  "brands", "models", "max_price", "max_price_per_day")
    )
    return cleaned if has_filter else None


def parse_issue(issue: dict[str, Any]) -> dict[str, Any] | None:
    """Issue API 对象 → 订阅档案（带 __issue / __title / __email 元数据）。"""
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    if not is_subscription_title(title) and SUBSCRIBE_MARKER not in body:
        return None
    profile = parse_issue_body(body)
    if not profile:
        return None
    out = dict(profile)
    out["__issue"] = int(issue.get("number") or 0)
    out["__title"] = title.strip()
    if profile.get("email"):
        out["__email"] = str(profile["email"]).strip()
    return out


def fetch_subscriptions(repo: str, *, token: str = "", timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    """读取仓库开放的订阅 Issue，返回订阅档案列表。"""
    if "/" not in str(repo or ""):
        raise ValueError(f"repo 需形如 owner/name，收到: {repo!r}")
    url = f"{API}/repos/{repo}/issues?state=open&per_page=50&sort=created&direction=desc"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "szhitch-monitor"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        issues = json.loads(resp.read().decode("utf-8", "replace"))

    subs: list[dict[str, Any]] = []
    for issue in issues if isinstance(issues, list) else []:
        if issue.get("pull_request"):  # PR 不是订阅
            continue
        parsed = parse_issue(issue)
        if parsed:
            subs.append(parsed)
    return subs


def fetch_worker_subscriptions(api: str, token: str, *, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    """读取邮箱订阅 Worker（cloud/cf_worker）里已确认的订阅，返回订阅档案列表。

    档案带 __email / __id / __unsub（该订阅的退订链接），没有 __issue。
    """
    api = str(api or "").rstrip("/")
    req = urllib.request.Request(
        f"{api}/subscriptions",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "szhitch-monitor"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))

    records = (data.get("subscriptions") if isinstance(data, dict) else None) or []
    subs: list[dict[str, Any]] = []
    for rec in records:
        profile = clean_profile(rec.get("profile"))
        email = str(rec.get("email") or "").strip()
        if not profile or not email:
            continue
        profile.pop("email", None)
        query = urllib.parse.urlencode({"id": rec.get("id", ""), "t": rec.get("token", "")})
        subs.append({**profile, "__email": email, "__id": str(rec.get("id") or ""),
                     "__unsub": f"{api}/unsubscribe?{query}"})
    return subs


def comment_on_issue(repo: str, number: int, body: str, *, token: str = "",
                     timeout: float = TIMEOUT) -> None:
    if not token:
        raise ValueError("回执 Issue 需要 token（Actions 里用自带的 GITHUB_TOKEN）")
    url = f"{API}/repos/{repo}/issues/{int(number)}/comments"
    data = json.dumps({"body": str(body)[:20000]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "szhitch-monitor",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def new_issue_url(repo: str, profile: dict[str, Any]) -> str:
    """生成「一键创建订阅」的 Issue 预填链接。"""
    clean = {k: v for k, v in (profile or {}).items() if k in ALLOWED_KEYS and v not in ("", None, [], {})}
    name = str(clean.get("name") or "我的订阅")
    title = f"{SUBSCRIBE_TITLE_PREFIX} {name}"
    body = (
        f"<!-- {SUBSCRIBE_MARKER} -->\n"
        "本 Issue 是一条预警订阅：**开启 = 订阅，关闭 = 退订**；改条件直接编辑下面的 JSON。\n\n"
        "```json\n"
        f"{json.dumps(clean, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "> 每轮监控会读取本 Issue：命中的车会推送到订阅里的邮箱（如填写），"
        "并在本 Issue 下回执记录（GitHub 也会因此给你发邮件）。"
    )
    return (
        f"https://github.com/{repo}/issues/new"
        f"?title={urllib.parse.quote(title)}&body={urllib.parse.quote(body)}"
    )
