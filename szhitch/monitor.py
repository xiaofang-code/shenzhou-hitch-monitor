"""监控主流程：抓取 → 比对生命周期 → 规则筛选 → 推送。

生命周期判定依赖三条事实（均经实测）：
  1. 列表接口只返回「可预订」的车，没有任何状态字段；
  2. 不存在的工单，详情接口报「顺风车工单不存在」；
  3. 工单仍在但不在列表里 → 不可预订，即被锁定（别人已下单）。

因此：
  * 从列表消失 + 详情仍可取  → 被锁定
  * 从列表消失 + 工单不存在  → 已关闭，剔除
  * 重新出现在列表          → 订单被取消，回到订单库
  * 还车日期已过            → 过期，剔除
"""
from __future__ import annotations

import copy
import html as _html
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .catalog import load_cities, normalize_city
from .gateway import MAX_PAGE_SIZE, GatewayClient, GatewayError
from .models import HitchItem, parse_date, parse_int
from .notify import Message, Notifier
from .rules import ProfileSet, RuleSet, build_profiles
from .store import (
    STATUS_AVAILABLE,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_LOCKED,
    BaseStore,
    ListingState,
    create_store,
    utcnow,
)

Log = Callable[[str], None]

KIND_ORDER = ["new", "relisted", "price_drop", "locked", "closed", "expired"]
KIND_LABEL = {
    "new": "新上线",
    "relisted": "回到订单库",
    "price_drop": "降价",
    "locked": "被锁定",
    "closed": "已关闭剔除",
    "expired": "已过期剔除",
}
KIND_ICON = {
    "new": "🆕",
    "relisted": "♻️",
    "price_drop": "⬇️",
    "locked": "🔒",
    "closed": "🧹",
    "expired": "⌛",
}


@dataclass
class Event:
    kind: str
    item: HitchItem
    old_price: int | None = None
    profile: str = ""      # 命中的订阅名（watch_profiles），单规则集时为空

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


@dataclass
class ScanStats:
    total: int = 0
    fetched: int = 0
    new: int = 0
    price_drop: int = 0
    relisted: int = 0
    locked: int = 0
    closed: int = 0
    expired: int = 0
    corrected: int = 0
    pruned: int = 0
    matched: int = 0
    notified: int = 0
    requests: int = 0
    elapsed: float = 0.0
    error: str = ""
    baseline: bool = False
    events: list[Event] = field(default_factory=list)
    by_profile: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "fetched": self.fetched,
            "new": self.new,
            "price_drop": self.price_drop,
            "relisted": self.relisted,
            "locked": self.locked,
            "closed": self.closed,
            "expired": self.expired,
            "corrected": self.corrected,
            "pruned": self.pruned,
            "matched": self.matched,
            "notified": self.notified,
            "requests": self.requests,
            "elapsed": round(self.elapsed, 1),
            "baseline": self.baseline,
            "error": self.error,
            "by_profile": self.by_profile,
        }


def item_from_state(state: ListingState) -> HitchItem:
    return HitchItem(
        hitch_id=state.hitch_id,
        model_name=state.model_name or "未知车型",
        price=state.price,
        pickup_city=state.pickup_city,
        return_city=state.return_city,
        begin_date=parse_date(state.begin_date),
        end_date=parse_date(state.end_date),
    )


def _mask_email(email: str) -> str:
    """日志里给邮箱打码（Actions 日志在公开仓库里人人可见）：ab***@qq.com。"""
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def _with_unsubscribe(msg: Message, links: list[tuple[str, str]]) -> Message:
    """给订阅者邮件追加退订链接（邮箱订阅才有；Issue 订阅关闭 Issue 即退订）。"""
    if not links:
        return msg
    plain = msg.plain + "\n\n" + "\n".join(f"退订「{name}」：{url}" for name, url in links)
    items = " · ".join(
        f'<a href="{_html.escape(url)}" style="color:#90a4ae">退订「{_html.escape(name)}」</a>'
        for name, url in links
    )
    block = (f'<div style="max-width:600px;margin:0 auto;padding:0 16px 24px;'
             f'color:#90a4ae;font-size:12px">{items}</div>')
    html = msg.html.replace("</body>", block + "\n</body>", 1) if msg.html else msg.html
    return Message(title=msg.title, markdown=msg.markdown, plain=plain, html=html)


class Monitor:
    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        client: GatewayClient | None = None,
        store: BaseStore | None = None,
        notifiers: list[Notifier] | None = None,
        log: Log = print,
    ) -> None:
        self.cfg = cfg
        self.log = log
        poll = cfg.get("poll", {})
        self.client = client or GatewayClient(
            timeout=float(poll.get("timeout", 20)),
            retries=int(poll.get("retries", 3)),
            min_interval=float(poll.get("min_request_interval", 1.2)),
        )
        self.store = store or create_store(cfg.get("storage") or "sqlite://data/state.db")
        self.profiles: ProfileSet = build_profiles(cfg)
        self.rules = self.profiles.fallback  # 兼容：单一规则集描述
        self._notifiers = notifiers
        self.fail_streak = 0
        # 供面板导出使用：最近一轮的在售清单与全量状态
        self.last_items: list[HitchItem] = []
        self.last_states: dict[str, ListingState] = {}
        self._city_ids: dict[str, str] | None = None
        # 订阅中心（GitHub Issues）：每轮刷新
        self.subscriptions: list[dict[str, Any]] = []
        self.subscription_repo: str = ""
        self.subscription_emails: list[str] = []

    # ------------------------------------------------------------------ 通知

    @property
    def notifiers(self) -> list[Notifier]:
        if self._notifiers is None:
            from .notify import build_notifiers

            self._notifiers = build_notifiers(self.cfg.get("notify") or {})
        return self._notifiers

    def notify(self, msg: Message) -> int:
        ok = 0
        for n in self.notifiers:
            if getattr(n, "name", "") == "smtp" and not n.to:
                continue  # 没配 SMTP_TO：SMTP 只用于订阅者邮件，不发汇总
            try:
                n.send(msg)
                ok += 1
            except Exception as exc:  # 单个渠道失败不影响其他渠道
                self.log(f"[通知失败] {n}: {exc}")
        return ok

    # -------------------------------------------------------------- 订阅中心

    def _refresh_subscriptions(self) -> None:
        """读取订阅（GitHub Issues + 邮箱订阅 Worker），合并进规则集，并归集邮箱收件人。

        两个来源互不影响；都失败时保留本地配置的规则，不影响本轮监控。
        """
        sub_cfg = self.cfg.get("subscribe") or {}
        if not sub_cfg.get("enabled", True):
            return
        timeout = float(self.cfg.get("poll", {}).get("timeout", 20))
        subs: list[dict[str, Any]] = []

        repo = str(sub_cfg.get("repo") or "") or os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            try:
                from .subscriptions import fetch_subscriptions

                issue_subs = fetch_subscriptions(
                    repo, token=os.environ.get("GITHUB_TOKEN", ""), timeout=timeout
                )
                subs += issue_subs
                self.subscription_repo = repo
                self.log(f"[订阅] 从 Issues 载入 {len(issue_subs)} 条")
            except Exception as exc:
                self.log(f"[订阅] 读取 Issues 失败: {exc}")

        api = os.environ.get("SZHITCH_SUB_API", "")
        api_token = os.environ.get("SZHITCH_SUB_TOKEN", "")
        if api and api_token:
            try:
                from .subscriptions import fetch_worker_subscriptions

                worker_subs = fetch_worker_subscriptions(api, api_token, timeout=timeout)
                subs += worker_subs
                self.log(f"[订阅] 从邮箱订阅载入 {len(worker_subs)} 条")
            except Exception as exc:
                self.log(f"[订阅] 读取邮箱订阅失败: {exc}")

        self.subscriptions = subs
        if not subs:
            return

        extra = [{k: v for k, v in s.items() if not k.startswith("__")} for s in subs]
        merged = dict(self.cfg)
        merged["watch_profiles"] = list(self.cfg.get("watch_profiles") or []) + extra
        self.profiles = build_profiles(merged)
        self.rules = self.profiles.fallback

        emails = sorted({s["__email"] for s in subs if s.get("__email")})
        self.subscription_emails = emails
        self.log(f"[订阅] 共 {len(subs)} 条订阅"
                 + (f"，其中 {len(emails)} 个留了邮箱" if emails else ""))

    def _email_subscribers(self, matched: list[Event], stats: ScanStats) -> None:
        """订阅者邮件：每个邮箱只收自己订阅命中的车，一人一封（需已配置 SMTP）。"""
        smtp = next((n for n in self.notifiers if getattr(n, "name", "") == "smtp"), None)
        if smtp is None or not matched or not self.subscriptions:
            return

        rules_by_email: dict[str, list[RuleSet]] = {}
        unsub_by_email: dict[str, list[tuple[str, str]]] = {}
        for sub in self.subscriptions:
            email = str(sub.get("__email") or "").strip().lower()
            if not email:
                continue
            rule = RuleSet.from_config({k: v for k, v in sub.items() if not k.startswith("__")})
            rules_by_email.setdefault(email, []).append(rule)
            if sub.get("__unsub"):
                unsub_by_email.setdefault(email, []).append(
                    (str(sub.get("name") or "我的订阅"), sub["__unsub"]))

        for email, rules in rules_by_email.items():
            hits = [ev for ev in matched if any(r.evaluate(ev.item)[0] for r in rules)]
            if not hits:
                continue
            mailer = copy.copy(smtp)
            mailer.to = [email]
            msg = _with_unsubscribe(self.build_message(hits, stats), unsub_by_email.get(email, []))
            try:
                mailer.send(msg)
                self.log(f"  [订阅] 已发邮件 {_mask_email(email)}（{len(hits)} 条命中）")
            except Exception as exc:
                self.log(f"  [订阅] 发邮件 {_mask_email(email)} 失败: {exc}")

    def _comment_subscriptions(self, matched: list[Event]) -> None:
        """把命中结果回执到对应的订阅 Issue（GitHub 会给订阅者发邮件）。"""
        sub_cfg = self.cfg.get("subscribe") or {}
        if not matched or not self.subscriptions or not sub_cfg.get("comment_alerts", True):
            return
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            self.log("[订阅] 未设置 GITHUB_TOKEN，跳过 Issue 回执")
            return
        try:
            from .subscriptions import comment_on_issue
        except ImportError:  # pragma: no cover
            return

        for sub in self.subscriptions:
            if not sub.get("__issue"):  # 邮箱订阅没有对应 Issue
                continue
            rule = RuleSet.from_config({k: v for k, v in sub.items() if not k.startswith("__")})
            hits = [ev for ev in matched if rule.evaluate(ev.item)[0]]
            if not hits:
                continue
            lines = [f"🔔 预警 **{len(hits)}** 条 · {utcnow()[:16].replace('T', ' ')}（UTC）", ""]
            for ev in hits[:10]:
                it = ev.item
                tag = f" `{ev.profile}`" if ev.profile else ""
                lines.append(
                    f"- {it.pickup_city} → {it.return_city} · {it.model_name}"
                    f" · **{it.price} 元** · {it.date_range() or '日期待确认'}{tag}"
                    f" · #{it.hitch_id}"
                )
            if len(hits) > 10:
                lines.append(f"- ……另有 {len(hits) - 10} 条，详见面板")
            lines.append("")
            lines.append("*本条由监控自动回执；关闭本 Issue 即退订。*")
            try:
                comment_on_issue(self.subscription_repo, sub["__issue"],
                                 "\n".join(lines), token=token)
                self.log(f"  [订阅] 已回执 Issue #{sub['__issue']}（{len(hits)} 条命中）")
            except Exception as exc:
                self.log(f"  [订阅] 回执 Issue #{sub['__issue']} 失败: {exc}")

    # ------------------------------------------------------------------ 抓取

    def scan(self) -> tuple[list[HitchItem], int]:
        poll = self.cfg.get("poll", {})
        source = self.cfg.get("source", {})
        raw, total = self.client.hitch_all(
            page_size=int(poll.get("page_size", 200)),
            max_pages=int(poll.get("max_pages", 30)),
            pickup_city_id=source.get("pickup_city_id"),
            return_city_id=source.get("return_city_id"),
        )
        items: dict[str, HitchItem] = {}
        for payload in raw:
            item = HitchItem.from_api(payload)
            if item.hitch_id and item.hitch_id not in items:
                items[item.hitch_id] = item
        return list(items.values()), total

    # -------------------------------------------------------------- 生命周期

    def _city_id(self, name: str) -> str | None:
        """城市名 → cityId。用本地字典，避免为复核再多打一次详情接口。"""
        if self._city_ids is None:
            index = load_cities()
            self._city_ids = {
                normalize_city(str(c.get("cityName", ""))): str(c.get("cityId"))
                for c in index.cities
            }
        return self._city_ids.get(normalize_city(name))

    def _route_still_listed(self, state: ListingState) -> bool | None:
        """按取还车城市把这条线路单独查一遍。

        全量翻页用的是偏移分页，翻页期间有新车上架会把某条挤到下一页而漏抓，
        直接据此判定「被锁定」会误报。定向查询结果集很小，能可靠回答：
        这条车到底还在不在售。
        """
        pickup_id = self._city_id(state.pickup_city)
        return_id = self._city_id(state.return_city)
        if not pickup_id or not return_id:
            return None
        try:
            items, total = self.client.hitch_page(
                1, MAX_PAGE_SIZE, pickup_city_id=pickup_id, return_city_id=return_id
            )
        except GatewayError:
            return None
        if not items:
            # 城市 ID 来自官方字典且已校验，线路为空说明这条线确实没车了
            return False
        if total > len(items):
            # 线路超过一页，无法确认
            return None
        return any(str(i.get("hitchId")) == state.hitch_id for i in items)

    def _probe_status(self, state: ListingState) -> str:
        """详情探测：工单是否还存在。存在=被锁定，不存在=已关闭。"""
        try:
            self.client.hitch_detail(state.hitch_id)
            return STATUS_LOCKED
        except GatewayError as exc:
            if "不存在" in str(exc):
                return STATUS_CLOSED
            # 网络抖动/限流等：保守当作锁定，下一轮再判
            self.log(f"  探测 {state.hitch_id} 失败（按锁定处理）: {exc}")
            return STATUS_LOCKED
        except Exception as exc:  # pragma: no cover - 兜底
            self.log(f"  探测 {state.hitch_id} 异常（按锁定处理）: {exc}")
            return STATUS_LOCKED

    # ------------------------------------------------------------------ 主流程

    def run_once(self, *, persist: bool = True, ignore_state: bool = False) -> ScanStats:
        """执行一轮检查。

        persist=False 用于演练：不写状态。
        ignore_state=True 让演练忽略历史记录，把当前在售全量当作「新上线」推演。
        """
        started = time.monotonic()
        stats = ScanStats()
        behavior = self.cfg.get("behavior", {})

        # 订阅中心：每轮先刷新（失败自动退回本地配置）
        self._refresh_subscriptions()

        try:
            items, total = self.scan()
        except GatewayError as exc:
            self.fail_streak += 1
            stats.error = str(exc)
            stats.elapsed = time.monotonic() - started
            self.log(f"[抓取失败#{self.fail_streak}] {exc}")
            return stats
        except Exception as exc:  # pragma: no cover - 兜底
            self.fail_streak += 1
            stats.error = repr(exc)
            stats.elapsed = time.monotonic() - started
            self.log(f"[抓取异常] {exc}\n{traceback.format_exc()}")
            return stats

        self.fail_streak = 0
        stats.total = total
        stats.fetched = len(items)

        previous = {} if ignore_state else self.store.load()
        now = utcnow()
        today = date.today()
        current = {i.hitch_id: i for i in items}

        is_first = not previous and bool(items)
        stats.baseline = is_first and str(behavior.get("first_run", "silent")) == "silent"
        if stats.baseline:
            self.log(f"[基线] 首次运行，已记录 {len(items)} 条作为基线，本轮不推送")

        events: list[Event] = []
        arrivals: list[tuple[str, str]] = []
        updated: dict[str, ListingState] = {}

        # ---- 1) 当前在售 ----
        for hid, item in current.items():
            old = previous.get(hid)
            if old is None:
                if not stats.baseline:
                    events.append(Event("new", item))
                arrivals.append((now, item.pickup_city))
            else:
                if old.status in (STATUS_LOCKED, STATUS_CLOSED):
                    if not stats.baseline:
                        events.append(Event("relisted", item))
                if item.price and old.price and item.price < old.price:
                    events.append(Event("price_drop", item, old_price=old.price))

            updated[hid] = ListingState(
                hitch_id=hid,
                price=item.price,
                signature=item.signature(),
                model_name=item.model_name,
                pickup_city=item.pickup_city,
                return_city=item.return_city,
                begin_date=item.begin_date.isoformat() if item.begin_date else "",
                end_date=item.end_date.isoformat() if item.end_date else "",
                first_seen=old.first_seen if old and old.first_seen else now,
                last_seen=now,
                active=True,
                status=STATUS_AVAILABLE,
                gone_since="",
                orig_price=(old.orig_price if old else 0),
                absent_passes=0,
            )

        # ---- 2) 从列表消失：先判过期，再用「定向复核 + 连续确认」判锁定 ----
        # 只处理上一轮还「可预订」的车；已经锁定的不重复判定，否则每轮都会重复推送
        missing = [
            s for hid, s in previous.items()
            if hid not in current and s.status == STATUS_AVAILABLE
        ]
        missing.sort(key=lambda s: s.last_seen)

        verify = bool(behavior.get("verify_missing", True))
        verify_limit = int(behavior.get("verify_missing_limit", 40))
        confirm_passes = max(1, int(behavior.get("confirm_absence_passes", 2)))

        candidates: list[ListingState] = []
        for state in missing:
            state.active = False
            state.gone_since = state.gone_since or now
            if state.is_expired(today):
                state.status = STATUS_EXPIRED
                state.absent_passes = 0
                if not stats.baseline:
                    events.append(Event("expired", item_from_state(state)))
            else:
                candidates.append(state)
            updated[state.hitch_id] = state

        for idx, state in enumerate(candidates):
            if verify and idx >= verify_limit:
                # 本轮复核额度用完，维持原状，下一轮继续判
                continue

            listed = self._route_still_listed(state) if verify else None

            if listed is True:
                # 定向查询能查到 —— 之前是全量翻页漏抓，纠正回来
                state.status = STATUS_AVAILABLE
                state.active = True
                state.absent_passes = 0
                state.gone_since = ""
                stats.corrected += 1
                self.log(
                    f"  [纠正] {state.pickup_city}→{state.return_city} {state.model_name}"
                    f"（{state.hitch_id}）仍在售，撤销锁定判定"
                )
                continue

            if listed is None:
                # 无法确认（线路为空 / 超过一页 / 请求失败），本轮不下结论
                continue

            state.absent_passes += 1
            if state.absent_passes < confirm_passes:
                self.log(
                    f"  [疑似下架] {state.hitch_id} 第 {state.absent_passes} 次缺席，"
                    f"连续 {confirm_passes} 次才判定"
                )
                continue

            state.status = self._probe_status(state)
            if not stats.baseline:
                events.append(
                    Event(
                        "closed" if state.status == STATUS_CLOSED else "locked",
                        item_from_state(state),
                    )
                )

        # ---- 3) 历史里已关闭/已锁定的，随时间转过期 ----
        for hid, state in previous.items():
            if hid in updated:
                continue
            if state.status in (STATUS_CLOSED, STATUS_LOCKED) and state.is_expired(today):
                state.status = STATUS_EXPIRED
            updated[hid] = state

        # ---- 4) 统计 ----
        stats.new = sum(1 for e in events if e.kind == "new")
        stats.price_drop = sum(1 for e in events if e.kind == "price_drop")
        stats.relisted = sum(1 for e in events if e.kind == "relisted")
        stats.locked = sum(1 for e in events if e.kind == "locked")
        stats.closed = sum(1 for e in events if e.kind == "closed")
        stats.expired = sum(1 for e in events if e.kind == "expired")

        # ---- 5) 规则筛选 ----
        enabled = set()
        if behavior.get("notify_new", True):
            enabled.add("new")
        if behavior.get("notify_relisted", True):
            enabled.add("relisted")
        if behavior.get("notify_price_drop", True):
            enabled.add("price_drop")
        if behavior.get("notify_locked", True):
            enabled.add("locked")
        if behavior.get("notify_closed", False):
            enabled.add("closed")
        if behavior.get("notify_expired", False):
            enabled.add("expired")

        matched: list[Event] = []
        for ev in events:
            if ev.kind not in enabled:
                continue
            ok, profile, reason = self.profiles.evaluate(ev.item)
            if ok:
                ev.profile = profile
                matched.append(ev)
            elif ev.kind == "new":
                self.log(f"  跳过 {ev.item} —— {reason}")

        # 详情补全（原价/门店）：只补「可下单」的，且先排序再补，保证补的是展示的那几条
        actionable = [e for e in matched if e.kind in ("new", "relisted", "price_drop")]
        actionable.sort(key=lambda e: (e.item.price, e.item.begin_date or date.max))
        if behavior.get("enrich_detail", True):
            limit = int(behavior.get("detail_limit", 10))
            for ev in actionable[:limit]:
                try:
                    ev.item = ev.item.merge_detail(self.client.hitch_detail(ev.item.hitch_id))
                    st = updated.get(ev.item.hitch_id)
                    if st and ev.item.original_price:
                        st.orig_price = ev.item.original_price
                except GatewayError as exc:
                    self.log(f"  详情获取失败 {ev.item.hitch_id}: {exc}")

        # 面板用的原价缓存：每轮只补少量最便宜的车，逐步覆盖，避免一次性打满
        dash_limit = int(behavior.get("dashboard_enrich_limit", 15))
        if dash_limit > 0:
            todo = [
                st for st in updated.values()
                if st.status == STATUS_AVAILABLE and not st.orig_price
            ]
            todo.sort(key=lambda s: s.price)
            for st in todo[:dash_limit]:
                try:
                    detail = self.client.hitch_detail(st.hitch_id)
                    st.orig_price = parse_int(detail.get("estTotalPrice")) or 0
                except GatewayError:
                    break

        matched.sort(
            key=lambda e: (KIND_ORDER.index(e.kind) if e.kind in KIND_ORDER else 99,
                           e.item.price, e.item.begin_date or date.max)
        )
        stats.matched = len(matched)
        stats.events = matched
        for ev in matched:
            if ev.profile:
                stats.by_profile[ev.profile] = stats.by_profile.get(ev.profile, 0) + 1

        # 预警流水：落库供面板「预警记录」展示（与是否配置通知渠道无关）。
        # 演练模式（persist=False）不落，避免把推演当真实预警。
        log_event = getattr(self.store, "log_event", None)
        if persist and callable(log_event):
            for ev in matched:
                try:
                    log_event(ev.kind, ev.item, {"profile": ev.profile})
                except Exception as exc:
                    self.log(f"  [预警流水] 写入失败: {exc}")
                    break

        if matched:
            stats.notified = self.notify(self.build_message(matched, stats))

        # 订阅 Issue 回执（GitHub 自动向订阅者发邮件，零配置渠道；演练模式不发）
        if matched and persist:
            self._comment_subscriptions(matched)
            self._email_subscribers(matched, stats)

        # ---- 6) 过期剔除：从状态库硬删除（保留期见 prune_expired_days）----
        keep_days = int(behavior.get("prune_expired_days", 7))
        cutoff = date.fromordinal(today.toordinal() - keep_days)
        for hid in list(updated):
            st = updated[hid]
            if st.status not in (STATUS_EXPIRED, STATUS_CLOSED):
                continue
            end = parse_date(st.end_date) if st.end_date else None
            if end and end < cutoff:
                updated.pop(hid, None)
                stats.pruned += 1

        # ---- 7) 落库 ----
        if persist:
            self.store.save(updated)
            self.store.record_arrivals(arrivals)
            try:
                self.store.prune(max(keep_days, 1))
            except Exception:
                pass

        stats.elapsed = time.monotonic() - started
        stats.requests = self.client.request_count
        self.last_items = items
        self.last_states = updated
        self.log(
            f"[完成] 全国 {stats.total} / 抓到 {stats.fetched} / 新增 {stats.new} / "
            f"回到订单库 {stats.relisted} / 降价 {stats.price_drop} / 锁定 {stats.locked} / "
            f"关闭 {stats.closed} / 过期 {stats.expired} / 剔除 {stats.pruned} / "
            f"命中 {stats.matched} / 推送 {stats.notified} / 用时 {stats.elapsed:.1f}s"
        )
        return stats

    # ------------------------------------------------------------------ 文案

    def build_message(self, events: list[Event], stats: ScanStats) -> Message:
        behavior = self.cfg.get("behavior", {})
        max_items = int(behavior.get("max_items_per_message", 8))

        grouped: dict[str, list[Event]] = {}
        for ev in events:
            grouped.setdefault(ev.kind, []).append(ev)

        summary_bits = [
            f"{KIND_LABEL[k]} {len(grouped[k])} 条"
            for k in KIND_ORDER
            if k in grouped
        ]
        summary = " / ".join(summary_bits)

        by_profile: dict[str, int] = {}
        for ev in events:
            if ev.profile:
                by_profile[ev.profile] = by_profile.get(ev.profile, 0) + 1
        profile_line = " · ".join(
            f"{name} {count} 条"
            for name, count in sorted(by_profile.items(), key=lambda x: -x[1])
        )

        md: list[str] = [f"## 🚗 神州顺风车 · {summary}", ""]
        plain: list[str] = [f"神州顺风车 · {summary}", ""]
        if profile_line:
            md += [f"**命中订阅**：{profile_line}", ""]
            plain += [f"命中订阅：{profile_line}", ""]
        rows: list[str] = []
        shown_total = 0

        for kind in KIND_ORDER:
            bucket = grouped.get(kind)
            if not bucket:
                continue
            remaining = max(0, max_items - shown_total)
            if remaining == 0:
                continue
            shown = bucket[:remaining]
            shown_total += len(shown)

            md.append(f"### {KIND_ICON[kind]} {KIND_LABEL[kind]}（{len(bucket)} 条）")
            plain.append(f"【{KIND_LABEL[kind]}】{len(bucket)} 条")

            for idx, ev in enumerate(shown, 1):
                it = ev.item
                price_txt = f"**{it.price} 元**"
                if it.original_price and it.original_price > it.price:
                    price_txt += f"（原价 {it.original_price}）"
                if ev.old_price and ev.old_price > it.price:
                    price_txt += f" ⬇ 原 {ev.old_price} 元"

                route = f"{it.pickup_city} → {it.return_city}"
                duration = (
                    f"{it.days} 天" if it.days
                    else (f"限 {it.limit_days} 天" if it.limit_days else "租期待确认")
                )

                md.append(f"**{idx}. {route} · {it.model_name}**")
                if ev.profile:
                    md.append(f"> 🎯 订阅：{ev.profile}")
                md.append(f"> 💰 {price_txt}")
                md.append(f"> 📅 {it.date_range() or '日期待确认'} · {duration}")
                if it.pickup_dept or it.return_dept:
                    md.append(f"> 📍 {it.pickup_dept or it.pickup_city} → {it.return_dept or it.return_city}")
                if it.model_desc:
                    md.append(f"> 🚙 {it.model_desc}")
                md.append(f"> 🆔 {it.hitch_id}")
                md.append("")

                plain.append(
                    f"{idx}. {route} · {it.model_name} | {it.price}元"
                    + (f"(原价{it.original_price})" if it.original_price else "")
                    + f" | {it.date_range()} {duration} | {it.hitch_id}"
                    + (f" | 订阅:{ev.profile}" if ev.profile else "")
                )
                rows.append(self._email_row(it, ev))

            if len(bucket) > len(shown):
                md.append(f"…… 该类另有 {len(bucket) - len(shown)} 条，详见面板")
                plain.append(f"…… 该类另有 {len(bucket) - len(shown)} 条")

        rest = sum(len(v) for v in grouped.values()) - shown_total
        if rest > 0:
            md.append(f"\n<font color=\"comment\">共 {rest} 条未展开，打开面板查看全部</font>")
            plain.append(f"\n共 {rest} 条未展开，打开面板查看全部")

        footer = f"筛选：{self.profiles.describe()} · 全国在售 {stats.total} 条"
        md.append(f"\n<font color=\"comment\">{footer}</font>")
        plain.append(f"\n{footer}")
        plain.append("打开「神州租车」App → 首页「顺风车」按取还车城市搜索")

        title = f"神州顺风车 {summary}" if summary else "神州顺风车 更新"
        if grouped.get("new") or grouped.get("relisted"):
            cheapest = min(
                (e.item.price for e in events if e.kind in ("new", "relisted")),
                default=None,
            )
            if cheapest is not None:
                title += f"（最低 {cheapest} 元）"

        return Message(
            title=title,
            markdown="\n".join(md),
            plain="\n".join(plain),
            html=self._email_html(title, rows, summary, footer),
        )

    # -------------------------------------------------------------- 邮件 HTML

    @staticmethod
    def _email_row(item: HitchItem, ev: Event) -> str:
        esc = _html.escape
        badge_color = {
            "new": ("#fff3e0", "#e65100"),
            "relisted": ("#e8f5e9", "#2e7d32"),
            "price_drop": ("#e3f2fd", "#1565c0"),
            "locked": ("#fce4ec", "#ad1457"),
            "closed": ("#eceff1", "#546e7a"),
            "expired": ("#eceff1", "#546e7a"),
        }.get(ev.kind, ("#eee", "#666"))

        orig = ""
        if item.original_price and item.original_price > item.price:
            orig = (
                f'<span style="color:#9e9e9e;text-decoration:line-through;'
                f'margin-left:8px;font-size:13px">原价 {item.original_price}</span>'
            )
        drop = ""
        if ev.old_price and ev.old_price > item.price:
            drop = f'<span style="color:#1565c0;margin-left:8px;font-size:13px">↓ 原 {ev.old_price}</span>'

        meta = " · ".join(
            x for x in [
                item.date_range(),
                f"{item.days} 天" if item.days else "",
                item.model_desc,
                f"订阅 {ev.profile}" if ev.profile else "",
            ] if x
        )
        dept = ""
        if item.pickup_dept or item.return_dept:
            dept = (
                f'<div style="color:#78909c;font-size:12px;margin-top:4px">'
                f"📍 {esc(item.pickup_dept or item.pickup_city)} → "
                f"{esc(item.return_dept or item.return_city)}</div>"
            )

        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8eaed;border-radius:12px;margin:0 0 10px;background:#fff">
          <tr><td style="padding:14px 16px 12px">
            <div style="font-size:15px;font-weight:600;color:#1a1a1a">
              {esc(item.pickup_city)} → {esc(item.return_city)}
              <span style="color:#666;font-weight:400"> · {esc(item.model_name)}</span>
              <span style="background:{badge_color[0]};color:{badge_color[1]};font-size:11px;
                           padding:2px 7px;border-radius:999px;margin-left:8px;vertical-align:middle">
                {ev.kind_label}
              </span>
            </div>
            <div style="margin-top:8px">
              <span style="color:#e65100;font-weight:700;font-size:22px">{item.price}</span>
              <span style="color:#e65100;font-size:13px"> 元</span>{orig}{drop}
            </div>
            <div style="color:#546e7a;font-size:13px;margin-top:6px">{esc(meta)}</div>
            {dept}
          </td></tr>
        </table>"""

    def _email_html(self, title: str, rows: list[str], summary: str, footer: str) -> str:
        esc = _html.escape
        body = "\n".join(rows) if rows else ""
        return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f6f8">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px">
 <tr><td align="center">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
   <tr><td style="padding:0 4px 16px">
     <div style="font-size:20px;font-weight:700;color:#1a1a1a">🚗 神州顺风车</div>
     <div style="color:#78909c;font-size:13px;margin-top:4px">{esc(summary)}</div>
   </td></tr>
   <tr><td>{body}</td></tr>
   <tr><td style="padding:8px 4px 0;color:#90a4ae;font-size:12px;line-height:1.6">{esc(footer)}</td></tr>
   <tr><td style="padding:8px 4px 0;color:#90a4ae;font-size:12px">
     打开「神州租车」App → 首页「顺风车」按取还车城市搜索
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""
