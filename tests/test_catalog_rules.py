"""城市字典 / 车型品牌 / 路线方向 / 订阅分组 的离线测试。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.catalog import (  # noqa: E402
    BrandCatalog,
    CityIndex,
    load_brands,
    load_cities,
    normalize_city,
    province_of,
)
from szhitch.insights import build_alerts, build_payload  # noqa: E402
from szhitch.models import HitchItem  # noqa: E402
from szhitch.monitor import Event, Monitor, ScanStats  # noqa: E402
from szhitch.rules import ProfileSet, RuleSet, build_profiles, parse_route, route_match  # noqa: E402
from szhitch.store import JsonStore, SqliteStore  # noqa: E402
from szhitch.subscriptions import (  # noqa: E402
    fetch_worker_subscriptions,
    is_subscription_title,
    new_issue_url,
    parse_issue,
    parse_issue_body,
)


def item(frm="成都", to="丽江", model="大众朗逸", price=100, begin=None):
    return HitchItem(
        hitch_id="1", model_name=model, price=price,
        pickup_city=frm, return_city=to,
        begin_date=begin, end_date=None,
    )


class TestCityIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load_cities()

    def test_dictionary_is_complete(self):
        # 官方 cityList 全量 471 个城市，一个都不能少
        self.assertGreaterEqual(len(self.index), 471)

    def test_suffix_normalization(self):
        self.assertEqual(normalize_city("成都市"), "成都")
        self.assertEqual(normalize_city("阿坝藏族羌族自治州"), "阿坝藏族羌族")
        self.assertEqual(self.index.city_id("成都市"), self.index.city_id("成都"))

    def test_province_from_admin_code(self):
        self.assertEqual(province_of("110100"), "北京")
        self.assertEqual(province_of("510100"), "四川")
        self.assertEqual(self.index.get("成都")["province"], "四川")
        self.assertEqual(self.index.get("呼和浩特")["province"], "内蒙古")

    def test_pinyin_and_initials(self):
        self.assertEqual(self.index.get("呼和浩特")["initials"], "hhht")
        self.assertEqual(self.index.get("重庆")["initials"], "cq")
        self.assertEqual(self.index.get("长沙")["pinyin"], "changsha")

    def test_search_by_chinese_pinyin_and_initials(self):
        self.assertIn("成都", [c["cityName"] for c in self.index.search("成都")])
        self.assertIn("成都", [c["cityName"] for c in self.index.search("chengdu")])
        self.assertIn("成都", [c["cityName"] for c in self.index.search("cd")])
        self.assertEqual(self.index.search("hhht")[0]["cityName"], "呼和浩特")

    def test_search_by_province(self):
        names = [c["cityName"] for c in self.index.search("云南")]
        self.assertIn("丽江", names)

    def test_letter_groups(self):
        groups = self.index.by_letter()
        self.assertIn("A", groups)
        self.assertTrue(all(c["letter"] == "A" for c in groups["A"]))
        total = sum(len(v) for v in groups.values())
        self.assertEqual(total, len(self.index))

    def test_empty_dictionary_is_safe(self):
        empty = CityIndex([])
        self.assertIsNone(empty.city_id("成都"))
        self.assertEqual(empty.search("成都"), [])


class TestBrandCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = load_brands()

    def test_simple_brand(self):
        info = self.cat.parse("理想L7")
        self.assertEqual((info.brand, info.series), ("理想", "L7"))

    def test_year_prefix_stripped(self):
        info = self.cat.parse("2023沃尔沃S90")
        self.assertEqual((info.brand, info.series), ("沃尔沃", "S90"))

    def test_config_suffix_stripped(self):
        info = self.cat.parse("广汽传祺M8尊享版")
        self.assertEqual((info.brand, info.series), ("传祺", "M8"))
        info2 = self.cat.parse("问界M9六座版")
        self.assertEqual((info2.brand, info2.series), ("问界", "M9"))

    def test_longest_alias_wins(self):
        self.assertEqual(self.cat.parse("吉利银河星耀8").brand, "吉利银河")
        self.assertEqual(self.cat.parse("长安深蓝S07").brand, "深蓝")
        self.assertEqual(self.cat.parse("奇瑞捷途旅行者").brand, "捷途")

    def test_unknown_brand(self):
        info = self.cat.parse("某不存在的车X1")
        self.assertEqual(info.brand, "")
        self.assertEqual(info.series, "某不存在的车X1")

    def test_empty_catalog_does_not_crash(self):
        self.assertEqual(BrandCatalog([]).parse("理想L7").series, "理想L7")


class TestRouteRules(unittest.TestCase):
    def test_parse_route_variants(self):
        self.assertEqual(parse_route("成都→丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都->丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都-丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都"), ("成都", "*"))
        self.assertIsNone(parse_route(""))

    def test_direction_is_respected(self):
        # 旧实现把两个城市当子串在整串里找，反向路线会误命中；这里必须方向敏感
        self.assertTrue(route_match(item("成都", "丽江"), "成都→丽江"))
        self.assertFalse(route_match(item("丽江", "成都"), "成都→丽江"))

    def test_wildcards(self):
        self.assertTrue(route_match(item("成都", "丽江"), "成都→*"))
        self.assertFalse(route_match(item("重庆", "丽江"), "成都→*"))
        self.assertTrue(route_match(item("重庆", "丽江"), "*→丽江"))
        self.assertFalse(route_match(item("重庆", "成都"), "*→丽江"))

    def test_single_city_means_pickup(self):
        self.assertTrue(route_match(item("成都", "丽江"), "成都"))
        self.assertFalse(route_match(item("丽江", "成都"), "成都"))

    def test_routes_in_ruleset(self):
        rules = RuleSet(routes=["成都→丽江"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])
        self.assertFalse(rules.evaluate(item("丽江", "成都"))[0])

    def test_city_suffix_tolerated_in_route(self):
        rules = RuleSet(routes=["成都市→丽江市"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])


class TestBrandRules(unittest.TestCase):
    def test_brand_whitelist(self):
        rules = RuleSet(brands=["理想"])
        self.assertTrue(rules.evaluate(item(model="理想L7"))[0])
        self.assertFalse(rules.evaluate(item(model="大众朗逸"))[0])

    def test_brand_whitelist_beats_substring(self):
        # 「大众」不应被当成「大众朗逸」以外的任何东西；同时「北京」不该吃掉「北京BJ30」以外车型
        rules = RuleSet(brands=["北京"])
        self.assertTrue(rules.evaluate(item(model="北京BJ30"))[0])
        self.assertFalse(rules.evaluate(item(model="大众途观L"))[0])

    def test_exclude_brand(self):
        rules = RuleSet(exclude_brands=["大众"])
        self.assertFalse(rules.evaluate(item(model="大众朗逸"))[0])
        self.assertTrue(rules.evaluate(item(model="理想L7"))[0])

    def test_any_cities(self):
        rules = RuleSet(any_cities=["丽江"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])
        self.assertFalse(rules.evaluate(item("成都", "重庆"))[0])

    def test_describe_mentions_new_filters(self):
        text = RuleSet(name="去丽江", routes=["*→丽江"], brands=["理想"]).describe()
        self.assertIn("去丽江", text)
        self.assertIn("*→丽江", text)
        self.assertIn("理想", text)


class TestProfiles(unittest.TestCase):
    def test_no_profiles_falls_back_to_watch(self):
        profiles = build_profiles({"watch": {"pickup_cities": ["成都"]}})
        self.assertEqual(len(profiles), 0)
        ok, name, _ = profiles.evaluate(item("成都", "丽江"))
        self.assertTrue(ok)
        self.assertEqual(name, "")

    def test_named_profile_reported(self):
        profiles = build_profiles({"watch_profiles": [
            {"name": "成都出发", "pickup_cities": ["成都"]},
            {"name": "回丽江", "routes": ["*→丽江"]},
        ]})
        self.assertEqual(len(profiles), 2)
        ok, name, _ = profiles.evaluate(item("成都", "丽江"))
        self.assertTrue(ok)
        self.assertEqual(name, "成都出发")

        ok2, name2, _ = profiles.evaluate(item("重庆", "丽江"))
        self.assertTrue(ok2)
        self.assertEqual(name2, "回丽江")

        ok3, _, reason = profiles.evaluate(item("重庆", "成都"))
        self.assertFalse(ok3)
        self.assertTrue(reason)

    def test_disabled_profile_skipped(self):
        profiles = build_profiles({"watch_profiles": [
            {"name": "停用", "enabled": False, "pickup_cities": ["成都"]},
            {"name": "启用", "routes": ["*→丽江"]},
        ]})
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles.profiles[0].name, "启用")

    def test_profile_without_name_gets_index(self):
        profiles = build_profiles({"watch_profiles": [{"pickup_cities": ["成都"]}]})
        self.assertEqual(profiles.profiles[0].name, "订阅1")


class TestMessageProfile(unittest.TestCase):
    def test_message_marks_hit_profile(self):
        cfg = {"poll": {}, "watch": {}, "notify": {}, "behavior": {}}
        mon = Monitor(cfg, client=object(), store=object(), notifiers=[], log=lambda *_: None)
        ev = Event("new", item("成都", "丽江"))
        ev.profile = "成都出发"
        msg = mon.build_message([ev], ScanStats(total=10))
        self.assertIn("命中订阅", msg.markdown)
        self.assertIn("成都出发", msg.markdown)
        self.assertIn("订阅:成都出发", msg.plain)
        self.assertIn("成都出发", msg.html)


class TestPayload(unittest.TestCase):
    def test_payload_has_full_city_dictionary_and_facets(self):
        items = [item("成都", "丽江", "理想L7"), item("重庆", "三亚", "大众朗逸", 1)]
        payload = build_payload(items, {})
        self.assertEqual(len(payload["city_dict"]), len(load_cities()))
        self.assertEqual(payload["totals"]["city_dict"], len(load_cities()))
        names = {row[1] for row in payload["city_dict"]}
        self.assertIn("成都", names)
        self.assertIn("阿坝", names)  # 当前没车也必须出现在字典里
        self.assertIn("理想", [b["name"] for b in payload["brands"]])
        self.assertTrue(any(r["from"] == "成都" and r["to"] == "丽江" for r in payload["routes"]))

    def test_listing_carries_brand_and_series(self):
        payload = build_payload([item("成都", "丽江", "2023沃尔沃S90")], {})
        row = payload["listings"][0]
        self.assertEqual(row["brand"], "沃尔沃")
        self.assertEqual(row["series"], "S90")

    def test_hit_flag_follows_profiles(self):
        profiles = build_profiles({"watch_profiles": [{"name": "成都", "pickup_cities": ["成都"]}]})
        payload = build_payload([item("成都", "丽江"), item("重庆", "三亚")], {}, profiles=profiles)
        hits = {row["from"]: row.get("hit") for row in payload["listings"]}
        self.assertTrue(hits["成都"])
        self.assertFalse(bool(hits["重庆"]))
        self.assertEqual(payload["watch"]["profiles"][0]["name"], "成都")

    def test_empty_payload_is_stable(self):
        payload = build_payload([], {})
        self.assertEqual(payload["totals"]["live"], 0)
        self.assertEqual(len(payload["city_dict"]), len(load_cities()))

    def test_no_profiles_means_no_hit(self):
        # 没配订阅时不应把全部车都标成「命中」（回落规则为空 = 匹配一切）
        payload = build_payload([item("成都", "丽江")], {}, profiles=build_profiles({"watch": {}}))
        self.assertNotIn("hit", payload["listings"][0])
        self.assertEqual(payload["totals"]["hit"], 0)
        self.assertFalse(payload["watch"]["active"])


class TestAlerts(unittest.TestCase):
    """预警流水：状态库 → 面板。"""

    @staticmethod
    def _alert(kind="new", model="理想L7", frm="成都", to="丽江",
               price=100, profile="成都出发", ts="2026-10-21T08:00:00Z"):
        return {"ts": ts, "kind": kind, "hitch_id": "1", "price": price,
                "model": model, "pickup_city": frm, "return_city": to, "profile": profile}

    def test_payload_includes_alert_log(self):
        row = build_alerts([self._alert()])["recent"][0]
        self.assertEqual(row["label"], "新上线")
        self.assertEqual(row["icon"], "🆕")
        self.assertEqual(row["from"], "成都")
        self.assertEqual(row["to"], "丽江")
        self.assertEqual(row["profile"], "成都出发")
        self.assertEqual(row["price"], 100)

    def test_alert_kinds_counted(self):
        data = build_alerts([self._alert(), self._alert(kind="locked"), self._alert(kind="locked")])
        self.assertEqual(data["kinds"], {"new": 1, "locked": 2})
        self.assertEqual(data["total"], 3)

    def test_alert_limit(self):
        rows = [self._alert(ts=f"2026-10-{d:02d}T08:00:00Z") for d in range(1, 11)]
        self.assertEqual(build_alerts(rows, limit=5)["total"], 5)

    def test_empty_events(self):
        self.assertEqual(build_alerts(None)["recent"], [])
        self.assertEqual(build_alerts(None)["total"], 0)

    def test_payload_carries_alerts(self):
        payload = build_payload([item("成都", "丽江")], {}, events=[self._alert()])
        self.assertEqual(len(payload["alerts"]["recent"]), 1)
        self.assertEqual(payload["alerts"]["recent"][0]["model"], "理想L7")


class TestStoreEvents(unittest.TestCase):
    """事件流水在两种存储后端里都能写读。"""

    def test_sqlite_events_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.log_event("new", HitchItem(hitch_id="9", model_name="理想L7", price=100,
                                             pickup_city="成都", return_city="丽江"),
                            {"profile": "成都出发"})
            store.log_event("locked", HitchItem(hitch_id="8", model_name="大众朗逸", price=50,
                                                pickup_city="重庆", return_city="三亚"))
            events = store.load_events(days=30, limit=10)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["kind"], "locked")      # 倒序：最近在前
            self.assertEqual(events[1]["profile"], "成都出发")
            self.assertEqual(events[1]["model"], "理想L7")

    def test_json_events_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "state.json")
            for i in range(7):
                store.log_event("new", HitchItem(hitch_id=str(i), model_name="理想L7", price=100,
                                                 pickup_city="成都", return_city="丽江"),
                                {"profile": "p"})
            events = store.load_events(days=30, limit=5)
            self.assertEqual(len(events), 5)
            self.assertEqual(events[0]["hitch_id"], "6")       # 最近一条在前

    def test_events_pruned_after_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.log_event("new", HitchItem(hitch_id="1", model_name="理想L7", price=100,
                                             pickup_city="成都", return_city="丽江"))
            conn = sqlite3.connect(store.path)
            with conn:
                conn.execute("UPDATE events SET ts = datetime('now', '-100 days')")
            conn.close()
            self.assertEqual(store.load_events(days=30), [])


class TestSubscriptions(unittest.TestCase):
    """订阅中心：Issue 正文 → 订阅规则。"""

    BODY = (
        "<!-- szhitch-subscription-v1 -->\n"
        "本 Issue 是一条预警订阅。\n\n"
        "```json\n"
        '{"name": "成都出发", "email": "me@example.com", "pickup_cities": ["成都"],\n'
        ' "max_price": 100, "hack_key": "should-be-dropped"}\n'
        "```\n"
    )

    def test_title_prefix_recognised(self):
        self.assertTrue(is_subscription_title("[订阅] 成都出发"))
        self.assertFalse(is_subscription_title("随便聊聊"))

    def test_parse_body_json(self):
        profile = parse_issue_body(self.BODY)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["name"], "成都出发")
        self.assertEqual(profile["email"], "me@example.com")
        self.assertEqual(profile["pickup_cities"], ["成都"])
        self.assertNotIn("hack_key", profile)          # 白名单之外一律丢弃

    def test_parse_body_rejects_invalid(self):
        self.assertIsNone(parse_issue_body("```json\nnot json\n```"))
        self.assertIsNone(parse_issue_body("```json\n{\"foo\": 1}\n```"))   # 没有任何筛选条件
        self.assertIsNone(parse_issue_body("没有代码块"))

    def test_parse_issue_object(self):
        sub = parse_issue({"number": 7, "title": "[订阅] 回丽江",
                           "body": self.BODY.replace("成都出发", "回丽江")})
        self.assertIsNotNone(sub)
        self.assertEqual(sub["__issue"], 7)
        self.assertEqual(sub["__title"], "[订阅] 回丽江")
        self.assertEqual(sub["__email"], "me@example.com")

    def test_parse_issue_skips_normal_issue(self):
        # 没有标题前缀、也没有订阅标记的 Issue 一律不算订阅
        self.assertIsNone(parse_issue({"number": 8, "title": "报个错", "body": "就是聊聊"}))
        self.assertIsNone(parse_issue({"number": 9, "title": "[订阅] 空的", "body": "没有 json"}))

    def test_parse_issue_keeps_marker_even_if_title_renamed(self):
        # 用户改了标题但保留订阅标记 → 仍视为订阅
        sub = parse_issue({"number": 10, "title": "盯成都出发的车",
                           "body": self.BODY})
        self.assertIsNotNone(sub)
        self.assertEqual(sub["__issue"], 10)

    def test_new_issue_url_encodes(self):
        url = new_issue_url("Moodzzzz/shenzhou-hitch-monitor",
                            {"name": "成都出发", "pickup_cities": ["成都"], "max_price": 100})
        self.assertIn("https://github.com/Moodzzzz/shenzhou-hitch-monitor/issues/new", url)
        self.assertIn("title=%5B%E8%AE%A2%E9%98%85%5D", url)
        self.assertIn("szhitch-subscription-v1", url)
        self.assertIn("max_price", url)

    def test_profile_email_key_is_ignored_by_ruleset(self):
        rules = RuleSet.from_config({"name": "x", "email": "me@example.com",
                                     "pickup_cities": ["成都"]})
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])

    def test_fetch_worker_subscriptions(self):
        import json
        from unittest import mock
        payload = {"subscriptions": [
            {"id": "abc", "token": "tk", "email": "a@x.com",
             "profile": {"name": "成都", "pickup_cities": ["成都"], "evil": 1, "email": "b@x.com"}},
            {"id": "n", "token": "t", "email": "c@x.com", "profile": {"name": "无条件"}},
        ]}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            subs = fetch_worker_subscriptions("https://api.example/", "secret")
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.example/subscriptions")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret")
        self.assertEqual(len(subs), 1)  # 无筛选条件的被丢弃
        sub = subs[0]
        self.assertEqual(sub["__email"], "a@x.com")
        self.assertEqual(sub["__unsub"], "https://api.example/unsubscribe?id=abc&t=tk")
        self.assertNotIn("evil", sub)
        self.assertNotIn("email", sub)


if __name__ == "__main__":
    unittest.main(verbosity=2)
