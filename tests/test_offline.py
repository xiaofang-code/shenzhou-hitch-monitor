"""离线测试：不联网，用录制的真实响应验证解析、规则、去重与文案。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.models import HitchItem, parse_date, parse_int  # noqa: E402
from szhitch.monitor import Event, Monitor, ScanStats  # noqa: E402
from szhitch.rules import RuleSet  # noqa: E402
from szhitch.store import JsonStore, ListingState, SqliteStore  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "hitch_list.json"


def load_fixture_items() -> list[dict]:
    data = json.loads(FIXTURE.read_text("utf-8"))
    return data["content"]["hitchList"]


class FakeClient:
    """替代 GatewayClient，便于构造价格变化、漏抓、线路复核等场景。"""

    def __init__(self, items: list[dict], detail: dict | None = None,
                 route_items: list[dict] | None = None) -> None:
        self.items = items
        self.detail = detail or {}
        self.route_items = route_items
        self.request_count = 0

    def hitch_all(self, **_kwargs):
        self.request_count += 1
        return list(self.items), len(self.items)

    def hitch_page(self, page_no=1, page_size=200, pickup_city_id=None, return_city_id=None):
        """定向复核用。route_items=None 表示「线路查不到」，即无法确认。"""
        self.request_count += 1
        if self.route_items is None:
            return [], 0
        return list(self.route_items), len(self.route_items)

    def hitch_detail(self, hitch_id):
        self.request_count += 1
        return self.detail


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, ListingState] = {}
        self.arrivals: list[tuple[str, str]] = []
        self.saved = 0

    def load(self):
        return dict(self.data)

    def save(self, records):
        self.saved += 1
        self.data = dict(records)

    def record_arrivals(self, pairs):
        self.arrivals.extend(pairs)

    def load_arrivals(self, days=90):
        return list(self.arrivals)

    def prune(self, days=30):
        return None


BASE_CFG = {
    "poll": {"page_size": 200, "max_pages": 5, "min_request_interval": 0, "timeout": 5, "retries": 0},
    "source": {},
    "watch": {},
    "behavior": {
        "first_run": "notify",
        "notify_new": True,
        "notify_price_drop": True,
        "enrich_detail": False,
        "max_items_per_message": 8,
    },
    "notify": {"console": False},
    "storage": {"type": "sqlite", "path": "data/test.db"},
}


class TestParsing(unittest.TestCase):
    def test_list_item_fields(self):
        item = HitchItem.from_api(load_fixture_items()[0])
        self.assertEqual(item.hitch_id, "9515098")
        self.assertEqual(item.model_name, "理想L7")
        self.assertEqual(item.price, 100)
        self.assertEqual(item.pickup_city, "张掖")
        self.assertEqual(item.return_city, "揭阳")
        self.assertEqual(item.begin_date, date(2026, 10, 20))
        self.assertEqual(item.end_date, date(2026, 10, 22))
        self.assertEqual(item.days, 2)
        self.assertEqual(item.price_per_day, 50.0)

    def test_date_variants(self):
        self.assertEqual(parse_date("2026.10.20"), date(2026, 10, 20))
        self.assertEqual(parse_date("2026-10-22 00:00"), date(2026, 10, 22))
        self.assertEqual(parse_date("10月2日", fallback_year=2026), date(2026, 10, 2))
        self.assertIsNone(parse_date("", fallback_year=2026))

    def test_price_coercion(self):
        self.assertEqual(parse_int("100"), 100)
        self.assertEqual(parse_int("1.0"), 1)
        self.assertEqual(parse_int(None), None)
        self.assertEqual(parse_int("abc"), None)

    def test_merge_detail_fills_original_price(self):
        item = HitchItem.from_api(load_fixture_items()[0])
        merged = item.merge_detail(
            {
                "estTotalPrice": "3048",
                "realTotalPrice": "100",
                "modelDesc": "42kWh增程式 | SUV 5座",
                "pickupDept": {"deptName": "张掖机场自助点"},
                "returnDept": {"deptName": "揭阳国际机场服务点"},
            }
        )
        self.assertEqual(merged.original_price, 3048)
        self.assertEqual(merged.pickup_dept, "张掖机场自助点")
        self.assertIn("原价 3048", merged.discount)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.items = [HitchItem.from_api(p) for p in load_fixture_items()]

    def test_empty_rules_pass_everything(self):
        rules = RuleSet()
        for item in self.items:
            ok, _ = rules.evaluate(item)
            self.assertTrue(ok)

    def test_city_filter(self):
        rules = RuleSet(pickup_cities=["张掖"])
        hits = [i for i in self.items if rules.evaluate(i)[0]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].pickup_city, "张掖")

    def test_city_filter_normalizes_suffix(self):
        rules = RuleSet(pickup_cities=["张掖市"])
        ok, _ = rules.evaluate(self.items[0])
        self.assertTrue(ok)

    def test_price_and_special(self):
        rules = RuleSet(max_price=50)
        self.assertFalse(rules.evaluate(self.items[0])[0])  # 100 元被挡
        special = RuleSet(only_special=True, special_price=1)
        self.assertFalse(special.evaluate(self.items[0])[0])

    def test_route_filter(self):
        rules = RuleSet(routes=["张掖→揭阳"])
        ok = [i for i in self.items if rules.evaluate(i)[0]]
        self.assertEqual([i.hitch_id for i in ok], ["9515098"])
        rules_miss = RuleSet(routes=["成都→丽江"])
        self.assertFalse(any(rules_miss.evaluate(i)[0] for i in self.items))

    def test_model_whitelist_and_exclude(self):
        rules = RuleSet(models=["理想"])
        self.assertTrue(rules.evaluate(self.items[0])[0])
        self.assertFalse(rules.evaluate(self.items[1])[0])
        rules_ex = RuleSet(exclude_models=["理想"])
        self.assertFalse(rules_ex.evaluate(self.items[0])[0])


class TestMonitorFlow(unittest.TestCase):
    def _monitor(self, items, store, cfg=None, client=None):
        conf = json.loads(json.dumps(BASE_CFG))
        conf.update(cfg or {})
        mon = Monitor(conf, client=client or FakeClient(items), store=store,
                      notifiers=[], log=lambda *_: None)
        # 固定城市字典，避免依赖 data/cities.json
        mon._city_ids = {"张掖": "335", "揭阳": "236", "合肥": "300",
                         "芜湖": "301", "呼和浩特": "302", "信阳": "303"}
        return mon

    def test_first_run_emits_new(self):
        store = FakeStore()
        mon = self._monitor(load_fixture_items(), store)
        stats = mon.run_once()
        self.assertEqual(stats.new, 3)
        self.assertEqual(stats.matched, 3)
        self.assertEqual(store.saved, 1)

    def test_second_run_no_duplicate(self):
        store = FakeStore()
        items = load_fixture_items()
        mon = self._monitor(items, store)
        mon.run_once()
        stats = mon.run_once()
        self.assertEqual(stats.new, 0)
        self.assertEqual(stats.matched, 0)

    def test_price_drop_detected(self):
        store = FakeStore()
        items = json.loads(json.dumps(load_fixture_items()))
        mon = self._monitor(items, store)
        mon.run_once()

        items[0]["realTotalPrice"] = "1"
        mon2 = self._monitor(items, store)
        stats = mon2.run_once()
        self.assertEqual(stats.price_drop, 1)
        self.assertEqual(stats.events[0].old_price, 100)
        self.assertEqual(stats.events[0].item.price, 1)

    def test_baseline_silent_on_first_run(self):
        store = FakeStore()
        cfg = {"behavior": {**BASE_CFG["behavior"], "first_run": "silent"}}
        mon = self._monitor(load_fixture_items(), store, cfg)
        stats = mon.run_once()
        self.assertTrue(stats.baseline)
        self.assertEqual(stats.matched, 0)
        self.assertEqual(store.saved, 1)  # 仍然落基线

    def test_dry_run_does_not_persist(self):
        store = FakeStore()
        mon = self._monitor(load_fixture_items(), store)
        mon.run_once(persist=False)
        self.assertEqual(store.saved, 0)

    def test_missing_but_still_listed_is_not_locked(self):
        """全量翻页漏抓时，定向复核能查到就撤销判定（线上真实误报场景）。"""
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]  # 第三轮“消失”
        client = FakeClient(partial, route_items=items)  # 但线路里还在
        stats = self._monitor(partial, store, client=client).run_once()

        self.assertEqual(stats.locked, 0)
        self.assertEqual(stats.corrected, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_unknown_route_does_not_lock(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=None)  # 线路查不到，无法确认
        stats = self._monitor(partial, store, client=client).run_once()

        self.assertEqual(stats.locked, 0)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_confirmed_absence_needs_two_passes(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=partial)  # 线路里确实没了

        stats1 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats1.locked, 0)  # 第一次只记疑似
        self.assertEqual(store.data[items[2]["hitchId"]].absent_passes, 1)

        stats2 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats2.locked, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "locked")

        # 第三轮：已经锁定的不应重复推送
        stats3 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats3.locked, 0)

    def test_relisted_after_lock(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=partial)
        self._monitor(partial, store, client=client).run_once()
        self._monitor(partial, store, client=client).run_once()
        self.assertEqual(store.data[items[2]["hitchId"]].status, "locked")

        # 订单取消，重新出现在列表
        client2 = FakeClient(items, route_items=items)
        stats = self._monitor(items, store, client=client2).run_once()
        self.assertEqual(stats.relisted, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_expired_is_excluded(self):
        store = FakeStore()
        items = json.loads(json.dumps(load_fixture_items()))
        items[0]["beginTime"] = "2020-01-01"
        items[0]["endTime"] = "2020-01-02"
        self._monitor(items, store).run_once()

        rest = items[1:]
        client = FakeClient(rest, route_items=rest)
        stats = self._monitor(rest, store, client=client).run_once()
        self.assertEqual(stats.expired, 1)
        # 超过保留期（默认 7 天）直接剔除
        self.assertNotIn(items[0]["hitchId"], store.data)


class TestMessage(unittest.TestCase):
    def test_message_contains_original_price_and_route(self):
        item = HitchItem.from_api(load_fixture_items()[0]).merge_detail(
            {"estTotalPrice": "3048", "pickupDept": {"deptName": "张掖机场自助点"}}
        )
        mon = Monitor(dict(BASE_CFG), client=FakeClient([]), store=FakeStore(), notifiers=[])
        msg = mon.build_message([Event("new", item)], ScanStats(total=959))
        self.assertIn("张掖 → 揭阳", msg.markdown)
        self.assertIn("3048", msg.markdown)
        self.assertIn("原价3048", msg.plain)
        self.assertIn("9515098", msg.plain)

    def test_message_truncates_long_list(self):
        items = [HitchItem.from_api(p) for p in load_fixture_items()]
        events = [Event("new", i) for i in items] * 4
        mon = Monitor(dict(BASE_CFG), client=FakeClient([]), store=FakeStore(), notifiers=[])
        msg = mon.build_message(events, ScanStats(total=12))
        self.assertIn("另有", msg.plain)
        self.assertIn("另有", msg.markdown)


class FakeSmtp:
    name = "smtp"

    def __init__(self, to):
        self.to = list(to)
        self.sent: list[tuple[list[str], object]] = []  # copy.copy 后共享同一列表

    def send(self, msg):
        self.sent.append((list(self.to), msg))


class TestSubscriberEmail(unittest.TestCase):
    def setUp(self):
        self.items = [HitchItem.from_api(p) for p in load_fixture_items()]
        self.smtp = FakeSmtp(["admin@x.com"])
        self.mon = Monitor(dict(BASE_CFG), client=FakeClient([]), store=FakeStore(),
                           notifiers=[self.smtp], log=lambda *_: None)
        self.mon.subscriptions = [
            {"pickup_cities": [self.items[0].pickup_city], "__issue": 1, "__email": "a@x.com"},
            {"pickup_cities": [self.items[1].pickup_city], "__issue": 2, "__email": "B@x.com "},
            {"pickup_cities": [self.items[2].pickup_city], "__issue": 3},
        ]

    def test_each_subscriber_gets_only_own_hits(self):
        events = [Event("new", i) for i in self.items]
        self.mon._email_subscribers(events, ScanStats(total=3))
        sent = {tuple(to): msg for to, msg in self.smtp.sent}
        self.assertEqual(set(sent), {("a@x.com",), ("b@x.com",)})
        self.assertIn(self.items[0].hitch_id, sent[("a@x.com",)].plain)
        self.assertNotIn(self.items[1].hitch_id, sent[("a@x.com",)].plain)
        self.assertIn(self.items[1].hitch_id, sent[("b@x.com",)].plain)
        self.assertNotIn(self.items[0].hitch_id, sent[("b@x.com",)].plain)
        self.assertEqual(self.smtp.to, ["admin@x.com"])

    def test_no_mail_without_hits(self):
        self.mon._email_subscribers([Event("new", self.items[2])], ScanStats(total=3))
        self.assertEqual(self.smtp.sent, [])

    def test_summary_skipped_without_admin_recipients(self):
        self.smtp.to = []
        events = [Event("new", i) for i in self.items]
        self.assertEqual(self.mon.notify(self.mon.build_message(events, ScanStats(total=3))), 0)
        self.mon._email_subscribers(events, ScanStats(total=3))
        self.assertEqual(sorted(to for to, _ in self.smtp.sent), [["a@x.com"], ["b@x.com"]])

    def test_refresh_does_not_touch_admin_recipients(self):
        from unittest import mock
        subs = list(self.mon.subscriptions)
        with mock.patch("szhitch.subscriptions.fetch_subscriptions", return_value=subs), \
                mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "me/repo"}):
            self.mon._refresh_subscriptions()
        self.assertEqual(self.smtp.to, ["admin@x.com"])


class TestStores(unittest.TestCase):
    def test_json_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStore(path)
            rec = ListingState(hitch_id="1", price=100, pickup_city="张掖", first_seen="t0", last_seen="t0")
            store.save({"1": rec})
            loaded = store.load()
            self.assertEqual(loaded["1"].price, 100)
            self.assertEqual(loaded["1"].pickup_city, "张掖")

    def test_sqlite_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.save({"1": ListingState(hitch_id="1", price=50)})
            store.save({"1": ListingState(hitch_id="1", price=1), "2": ListingState(hitch_id="2", price=100)})
            loaded = store.load()
            self.assertEqual(loaded["1"].price, 1)
            self.assertEqual(len(loaded), 2)

    def test_sqlite_marks_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.save({"1": ListingState(hitch_id="1", price=50, active=False)})
            self.assertFalse(store.load()["1"].active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
