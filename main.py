"""神州顺风车监控 —— 命令行入口。"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from szhitch.catalog import (
    CITY_FILE,
    collect_city_payload,
    enrich_cities,
    load_cities,
    save_cities,
)
from szhitch.config import dump_default, load_config
from szhitch.gateway import GatewayClient
from szhitch.insights import build_payload
from szhitch.models import HitchItem
from szhitch.monitor import Monitor
from szhitch.notify import Message, build_notifiers
from szhitch.rules import build_profiles
from szhitch.store import create_store

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DASHBOARD_DIR = ROOT / "dashboard"
DASHBOARD_DATA = DASHBOARD_DIR / "dashboard.json"


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def _cfg(args) -> dict:
    return load_config(args.config)


def cmd_init(args) -> int:
    path = Path(args.config or DEFAULT_CONFIG)
    if path.exists() and not args.force:
        print(f"配置已存在：{path}（加 --force 覆盖）")
        return 1
    dump_default(path)
    print(f"已生成配置模板：{path}")
    print("下一步：填入 notify.wecom_webhook 等渠道，再执行 python main.py once --dry-run 验证")
    return 0


def cmd_cities(args) -> int:
    index = load_cities(CITY_FILE)
    if not len(index):
        print(f"缺少城市字典 {CITY_FILE}，请先执行 python tools/build_cities.py")
        return 1
    query = (args.query or "").strip()
    if args.letter:
        groups = index.by_letter()
        cities = groups.get(args.letter.upper(), [])
        print(f"字母 {args.letter.upper()} 下共 {len(cities)} 个城市")
    elif query:
        cities = index.search(query, limit=args.limit)
        print(f"「{query}」命中 {len(cities)} 个城市（支持中文/全拼/首字母/省份）")
    else:
        cities = index.cities
        print(f"共 {len(index)} 个城市")

    for c in cities[: args.limit]:
        print(
            f"  {str(c['cityId']):>5}  {c['cityName']:<8} {c.get('initials', ''):<6}"
            f"{c.get('pinyin', ''):<16} {c.get('province', ''):<6} {c.get('letter', '')}"
        )
    if len(cities) > args.limit:
        print(f"  …… 仅显示前 {args.limit} 条（--limit 调整）")
    return 0


def cmd_refresh_cities(args) -> int:
    client = GatewayClient()
    raw = client.cities()
    previous = {str(c.get("cityId")): c for c in load_cities(CITY_FILE).cities}
    cities = enrich_cities(collect_city_payload(raw), previous)
    path = save_cities(cities)
    missing_pinyin = sum(1 for c in cities if not c.get("pinyin"))
    print(f"已更新城市字典：{len(cities)} 个城市 → {path}")
    if missing_pinyin:
        print(f"  提示：{missing_pinyin} 个城市没有拼音，执行 python tools/build_cities.py 补齐")
    return 0


def _dashboard_repo(cfg: dict) -> str:
    """面板「一键订阅」要用的仓库：配置优先，Actions 里自动取 GITHUB_REPOSITORY。"""
    return str(cfg.get("subscribe", {}).get("repo") or "") or os.environ.get("GITHUB_REPOSITORY", "")


def _write_dashboard(monitor: Monitor, path: Path) -> dict:
    """把最近一轮结果写成面板数据（面板是静态页，只读这个 JSON）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(
        monitor.last_items,
        monitor.last_states,
        arrivals=monitor.store.load_arrivals(180),
        profiles=monitor.profiles,
        events=getattr(monitor.store, "load_events", lambda **_: [])(
            days=30, limit=40
        ),
        repo=_dashboard_repo(monitor.cfg),
    )
    # 邮箱订阅 Worker 地址（cloud/cf_worker），面板据此显示「填邮箱订阅」
    payload["sub_api"] = os.environ.get("SZHITCH_SUB_API", "").rstrip("/")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )
    return payload


def cmd_once(args) -> int:
    cfg = _cfg(args)
    if args.dry_run:
        cfg["notify"] = {"console": True}
        # 演练时按「正常轮次」推演，方便看清当前能命中什么
        cfg["behavior"]["first_run"] = "notify"
    monitor = Monitor(cfg)
    print(f"筛选条件：{monitor.profiles.describe()}")
    if args.dry_run:
        print("演练模式：不写状态、不消耗「新增」事件\n")
    stats = monitor.run_once(persist=not args.dry_run, ignore_state=args.dry_run)
    if args.export:
        payload = _write_dashboard(monitor, Path(args.export))
        print(f"面板数据已导出：{args.export}（在售 {payload['totals']['live']} / "
              f"锁定 {payload['totals']['locked']}）")
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    if stats.error:
        return 2
    return 0


def cmd_recheck(args) -> int:
    """复核所有「被锁定」记录：仍能定向查到就撤销，避免误报留在库里。"""
    cfg = _cfg(args)
    monitor = Monitor(cfg)
    states = monitor.store.load()
    locked = [s for s in states.values() if s.status == "locked"]
    print(f"待复核：{len(locked)} 条锁定记录")
    if not locked:
        return 0

    restored = kept = unknown = 0
    for s in locked:
        listed = monitor._route_still_listed(s)
        if listed is True:
            s.status = "available"
            s.active = True
            s.absent_passes = 0
            s.gone_since = ""
            restored += 1
            print(f"  ✓ 撤销锁定 {s.pickup_city}→{s.return_city} {s.model_name}（{s.hitch_id}）")
        elif listed is False:
            kept += 1
            print(f"  · 确认下架 {s.pickup_city}→{s.return_city} {s.model_name}（{s.hitch_id}）")
        else:
            unknown += 1
            print(f"  ? 无法确认 {s.hitch_id}（{s.pickup_city}→{s.return_city}）")

    if not args.dry_run:
        monitor.store.save(states)
        print("已写回状态库")
    print(f"\n撤销 {restored} / 确认锁定 {kept} / 无法确认 {unknown}")
    return 0


class _PanelHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, *args) -> None:  # 安静一点
        pass


def cmd_serve(args) -> int:
    root = DASHBOARD_DIR
    if args.refresh or not DASHBOARD_DATA.exists():
        cfg = _cfg(args)
        monitor = Monitor(cfg)
        print("刷新数据中…")
        stats = monitor.run_once()
        if stats.error:
            print(f"抓取失败：{stats.error}")
        payload = _write_dashboard(monitor, DASHBOARD_DATA)
        print(f"数据已更新：在售 {payload['totals']['live']} / 锁定 {payload['totals']['locked']}")
    elif not (root / "index.html").exists():
        print(f"缺少面板页面：{root / 'index.html'}")
        return 1

    handler = partial(_PanelHandler, directory=str(root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"面板已启动：http://{args.host}:{args.port}/   （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


def cmd_watch(args) -> int:
    cfg = _cfg(args)
    interval = args.interval or int(cfg["poll"]["interval_seconds"])
    monitor = Monitor(cfg)
    print(f"监控启动 · 每 {interval}s 轮询一次 · 筛选：{monitor.profiles.describe()}")
    print("Ctrl+C 退出")

    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True
        print("\n收到退出信号，本轮结束后停止…")

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    while not stopping["flag"]:
        started = time.monotonic()
        stats = monitor.run_once()
        if stats.notified:
            print(f"  → 已推送 {stats.notified} 个渠道")
        cost = time.monotonic() - started
        sleep_for = max(30.0, interval - cost)
        deadline = time.monotonic() + sleep_for
        while time.monotonic() < deadline and not stopping["flag"]:
            time.sleep(min(5.0, deadline - time.monotonic()))
    print("已停止")
    return 0


def cmd_snapshot(args) -> int:
    cfg = _cfg(args)
    client = GatewayClient(
        timeout=float(cfg["poll"]["timeout"]),
        retries=int(cfg["poll"]["retries"]),
        min_interval=float(cfg["poll"]["min_request_interval"]),
    )
    raw, total = client.hitch_all(
        page_size=int(cfg["poll"]["page_size"]),
        max_pages=int(cfg["poll"]["max_pages"]),
        pickup_city_id=cfg["source"].get("pickup_city_id"),
        return_city_id=cfg["source"].get("return_city_id"),
    )
    items = [HitchItem.from_api(p) for p in raw]
    profiles = build_profiles(cfg)
    out_dir = ROOT / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = out_dir / f"snapshot-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["hitchId", "取车城市", "还车城市", "车型", "价格", "取车日", "还车日",
                         "天数", "命中订阅"])
        for it in sorted(items, key=lambda x: (x.pickup_city, x.price)):
            ok, profile, _ = profiles.evaluate(it)
            writer.writerow([it.hitch_id, it.pickup_city, it.return_city, it.model_name, it.price,
                             it.begin_date or "", it.end_date or "", it.days or "",
                             profile if ok else ""])
    print(f"全国 {total} 条，已导出 {len(items)} 条 → {csv_path}")
    return 0


def cmd_test_notify(args) -> int:
    cfg = _cfg(args)
    notifiers = build_notifiers(cfg.get("notify") or {})
    if not notifiers:
        print("没有配置任何通知渠道")
        return 1
    msg = Message(
        title="神州顺风车监控 · 连通性测试",
        markdown="## ✅ 通知渠道已连通\n\n收到这条消息说明配置正确，监控开始后会推送真实顺风车。",
        plain="通知渠道已连通。收到这条消息说明配置正确。",
    )
    ok = 0
    for n in notifiers:
        try:
            n.send(msg)
            ok += 1
            print(f"  ✓ {n}")
        except Exception as exc:
            print(f"  ✗ {n}: {exc}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = argparse.ArgumentParser(
        prog="szhitch", description="神州顺风车监控（租车异地调车特价车）"
    )
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG), help="配置文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("cities", help="查询城市 ID（支持中文/全拼/首字母/省份）")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--letter", default="", help="按首字母分组查看，如 --letter C")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_cities)

    p = sub.add_parser("refresh-cities", help="从线上刷新城市字典")
    p.set_defaults(func=cmd_refresh_cities)

    p = sub.add_parser("once", help="执行一次检查")
    p.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    p.add_argument("--export", metavar="PATH", default="", help="同时导出面板数据，如 dashboard/dashboard.json")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("recheck", help="复核「被锁定」记录，撤销误判")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写回")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("serve", help="启动可视面板（本地 HTTP 服务）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--refresh", action="store_true", help="启动前先抓一轮最新数据")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("watch", help="常驻轮询")
    p.add_argument("--interval", type=int, default=0, help="轮询间隔秒数（覆盖配置）")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("snapshot", help="导出当前全国顺风车为 CSV")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("test-notify", help="测试通知渠道连通性")
    p.set_defaults(func=cmd_test_notify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
