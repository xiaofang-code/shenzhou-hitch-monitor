# 神州顺风车监控

盯「神州租车」App 里的**顺风车**（异地调车特价车）—— 车需要从一个城市调回另一个城市，你以 1 元 / 50 元 / 100 元的固定价把车开过去，租期 1～13 天。放出来基本靠抢，所以需要程序盯着。

程序做的事：**定时拉全量 → 比对生命周期（新上线 / 被锁定 / 回到订单库 / 过期剔除）→ 按条件过滤 → 发邮件等通知**，并把结果同步到一个可搜索的**可视面板**。

> 逆向过程、接口证据链见 [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)。
> 线上看板：<https://moodzzzz.github.io/shenzhou-hitch-monitor/>

---

## 它能告诉你什么

| 事件 | 含义 | 默认是否推送 |
|---|---|---|
| 🆕 **新上线** | 新放出来的顺风车 | 是 |
| ♻️ **回到订单库** | 之前被锁定/关闭的车，订单取消后又可预订了 | 是 |
| 🔒 **被锁定** | 车被别人下单锁定了，从可订池里消失 | 是 |
| ⬇️ **降价** | 同一条 `hitchId` 价格下调 | 是 |
| ⌛ **已过期剔除** | 还车日期已过，从库里清掉 | 否（只计数） |
| 🧹 **已关闭剔除** | 工单已不存在 | 否（只计数） |

> 「被锁定」不是猜的：列表接口只返回可预订的车，某条消失后程序会**按这条线路定向复查**，仍查不到才判定不可订；而且要**连续两轮**都查不到才推送，避免翻页抖动造成的误报。

---

## 特点

| | |
|---|---|
| 零第三方依赖 | 纯 Python 标准库，云端不用 `pip install` |
| 一键订阅 | 面板上填条件点一下 → 存成仓库 Issue，监控自动读取；**关闭 Issue 即退订** |
| 免登录 | 列表/详情/城市字典都不需要账号、Cookie 或签名 |
| 全国覆盖 | 5 个请求拿全 950+ 条（`pageSize=200`） |
| 城市全量 | 471 个城市的字典，含省份、全拼、首字母；中文 / `chengdu` / `cd` 都能搜到 |
| 车型归一 | 把 `modelName` 拆成「品牌 + 车系」，剥掉 `2023` 年份前缀和「尊享版」这类后缀 |
| 只推增量 | 按 `hitchId` 落库，新上线性推送；同一条降价也提示 |
| 抗漏抓 | 全量翻页遇到数据位移会自动补一遍；消失的车再按线路复核 |
| 首轮不刷屏 | 首次运行只建基线，不把 900+ 条历史当「新上线」 |
| 邮件优先 | HTML 邮件，卡片式排版，手机上也好看 |
| 可视面板 | 时刻表式看板：全量城市选择器、品牌筛选、预警监控 |

---

## 5 分钟上手

**不想碰命令行？** 打开看板 → 左侧选好城市 / 路线 / 品牌 → 填邮箱 → 点「⚡ 一键订阅」：
部署了邮箱订阅 Worker（见「部署 → 邮箱订阅」）时**不需要 GitHub 账号**，到邮箱点确认链接即生效，每封提醒底部可一键退订；
否则条件会存成仓库里的一个 Issue，下一轮监控自动按它推送，**关闭 Issue 即退订**。
邮件提醒：Issue 订阅命中时监控会在 Issue 下回执（GitHub 自动向订阅者发邮件，零配置）；
若在 Secrets 里配置了 SMTP（`SZHITCH_SMTP_HOST/_PORT/_USER/_PASS/_TO/_FROM`），还会**只把该订阅命中的车**单独发到订阅里填的邮箱（一人一封，互不可见）；`_TO` 收全量汇总。

```bash
python main.py init                      # 生成配置
python main.py once --dry-run            # 演练：看当前能命中什么（不推送、不写状态）
python main.py test-notify               # 测通知渠道

python main.py once                      # 建立基线（首轮只记录，不推送）
python main.py watch                     # 常驻，默认 4 小时一轮
```

看板：

```bash
python main.py serve --refresh           # 抓一轮 + 起本地看板 → http://127.0.0.1:8765/
```

查城市 ID、导出 CSV：

```bash
python main.py cities 成都          # 中文 / 全拼 / 首字母都能查
python main.py cities cd            # 首字母：常德 / 成都 / 承德 / 昌都
python main.py cities --letter L    # 按字母分组浏览
python main.py snapshot             # 导出全国 CSV，标注命中的订阅
```

城市字典补拼音（只在生成时用 pypinyin，运行期无依赖）：

```bash
pip install pypinyin && python tools/build_cities.py
```

---

## 可视面板

![看板](docs/panel.png)

城市选择器：471 城全量，中文 / 全拼 / 首字母都能搜，右侧是字母索引条。

![城市选择器](docs/panel-city.png)

面板是一个**静态页 + 一份 JSON**，所以两种用法都行：

- 本地：`python main.py serve --refresh`（或 `once --export dashboard/dashboard.json` 后起任意静态服务）
- 线上：GitHub Actions 每轮会把 `dashboard/dashboard.json` 提交回仓库并发布到 Pages

功能：

- **城市选择器（全量 471 城）**：不依赖「当前有没有车」，没车的城市也在列表里，方便提前盯
  - 中文 / 全拼 / 首字母三种搜法：`成都`、`chengdu`、`cd` 都命中
  - 右侧 A–Z 索引条直接跳到字母段，按省份也能搜（输入「云南」）
  - 每个城市后面标着当前取车 / 还车条数，一眼看出哪条线路有货
- **车型筛选**：品牌 chip（大众 / 理想 / 宝马…，带条数）多选 + 车系关键词，两者可叠加
- **路线筛选**：取车 + 还车两个选择器就是一条定向路线，或点「热门路线」直接跳进去
- **只看关注**：服务端订阅命中的车会带 ★ 与左侧琥珀竖线
- **关注条件 → 订阅**：选完条件后一键复制 `watch_profiles` / `watch` JSON，粘进 `config.json` 即生效
- **链接可分享**：筛选条件写进 URL hash（`#from=成都&brands=大众`），发给别人/收藏都带着条件
- **预警监控**：盯指定城市 / 指定路线 / 指定车型的核心面板
  - 当前命中：筛选条件命中的车，一键「只看命中」
  - 订阅状态：服务端生效中的 `watch_profiles` 及其条件
  - 预警记录：最近 30 天每次「新上线 / 回到订单库 / 降价 / 被锁定」的时间、路线、车型、价格、命中的订阅名
- **观测节奏**：每天新增多少条，跑得越久越准

> 界面是「夜间发车牌」风格：路线做成虚线轨道，价格用等宽数字右对齐，好扫。
> 移动端会把行折叠成两列，筛选栏取消吸顶。

> 「预警记录」来自监控每轮的命中流水（与是否配置通知渠道无关）；刚跑第一天记录较少属正常。

---

## 配置说明

`config.json`（`python main.py init` 生成）。

### `watch` —— 推什么

全部为「与」关系；列表内部是「或」；留空表示不限制。

| 字段 | 说明 |
|---|---|
| `pickup_cities` | 取车城市，如 `["成都","重庆"]`；「成都」「成都市」都能匹配 |
| `return_cities` | 还车城市 |
| `any_cities` | 取车**或**还车任一命中即可，适合「只想离开/到达某城」 |
| `routes` | 精确路线，方向敏感：`["成都→丽江"]` 不会命中 `丽江→成都`；支持 `成都→*`、`*→丽江` |
| `brands` / `exclude_brands` | 品牌白名单/黑名单，如 `["理想","宝马"]`（由车型名解析，见下） |
| `models` / `exclude_models` | 车型关键词白名单/黑名单，如 `["帕萨特","L7"]` |
| `only_special` / `special_price` | 只推特价，默认 ≤100 元 |
| `max_price` / `min_price` | 总价区间 |
| `max_price_per_day` | 日均价上限 |
| `min_days` / `max_days` | 租期天数 |
| `date_from` / `date_to` | 取车日期窗口 |

```json
"watch": {
  "pickup_cities": ["成都"],
  "brands": ["理想", "宝马"],
  "routes": ["成都→*"],
  "max_price": 100,
  "exclude_brands": ["大众"]
}
```

### `watch_profiles` —— 多组订阅

想同时盯几条互不相干的线路时用它。**非空时优先于 `watch`**，任一订阅命中就推送，
消息里会写「🎯 订阅：成都出发」，面板也会把这些车标成 ★。

```json
"watch_profiles": [
  { "name": "成都出发", "pickup_cities": ["成都"], "max_price": 100 },
  { "name": "回丽江/三亚", "routes": ["*→丽江", "*→三亚"],
    "brands": ["宝马", "奔驰", "奥迪", "沃尔沃", "理想", "问界"] },
  { "name": "先别推这个", "enabled": false, "brands": ["大众"] }
]
```

| 字段 | 说明 |
|---|---|
| `name` | 订阅名，出现在推送与面板里；缺省为「订阅1」 |
| `enabled` | `false` 时跳过这一组 |
| 其余字段 | 与 `watch` 完全一致 |

### 车型品牌是怎么认的

`modelName` 是「品牌+车系+配置」拼在一起的字符串，程序按别名表拆开：

| 原始车型名 | 品牌 | 车系 |
|---|---|---|
| `2023沃尔沃S90` | 沃尔沃 | S90 |
| `广汽传祺M8尊享版` | 传祺 | M8 |
| `问界M9六座版` | 问界 | M9 |
| `吉利银河星耀8` | 吉利银河 | 星耀8 |
| `长安深蓝S07` | 深蓝 | S07 |

- 长别名优先：`吉利银河` 不会被 `吉利` 抢走，`奇瑞捷途` 归到 `捷途`
- 年份前缀（`2023`）与配置后缀（尊享版/六座版/两驱/乾崑…）会被剥掉
- 品牌表在 `data/brands.json`，加新品牌直接往里补 `name` + `aliases` 即可
- 面板与推送里的品牌统计都基于这套解析，不是字符串包含

### `behavior` —— 怎么推

| 字段 | 默认 | 说明 |
|---|---|---|
| `first_run` | `silent` | 首轮只建基线；改 `notify` 会把存量全推一遍 |
| `notify_new` | `true` | 推新上线 |
| `notify_relisted` | `true` | 推「回到订单库」 |
| `notify_locked` | `true` | 推「被锁定」 |
| `notify_price_drop` | `true` | 推降价 |
| `notify_expired` / `notify_closed` | `false` | 过期/关闭一般只计数，避免噪音 |
| `verify_missing` | `true` | 消失后按线路定向复查 |
| `verify_missing_limit` | `40` | 每轮最多复查多少条（控制请求量） |
| `confirm_absence_passes` | `2` | 连续几轮查不到才判定锁定 |
| `prune_expired_days` | `7` | 过期/关闭记录保留几天后硬删除 |
| `enrich_detail` | `true` | 命中后补详情拿原价、门店 |
| `detail_limit` | `10` | 每轮最多补几条事件详情 |
| `dashboard_enrich_limit` | `40` | 每轮为面板缓存几条原价（渐进覆盖，4 小时一轮约 4 天补全） |
| `max_items_per_message` | `8` | 单条消息最多列几条 |

### `poll` —— 多久拉一次

| 字段 | 默认 | 说明 |
|---|---|---|
| `interval_seconds` | `14400` | 轮询间隔，默认 4 小时，最小 120 秒 |
| `page_size` | `200` | 单页条数，上限 200 |
| `min_request_interval` | `1.2` | 请求间隔，自我限速 |

---

## 通知渠道

只填你想用的，其余留空。**密钥走环境变量**，不要提交到仓库。

| 渠道 | 配置 | 环境变量 |
|---|---|---|
| **邮件** | `notify.smtp` | `SZHITCH_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_TO` / `_FROM`（可选，发件地址，缺省同 `_USER`） |
| 企业微信群机器人 | `notify.wecom_webhook` | `SZHITCH_WECOM` |
| 飞书 | `notify.feishu_webhook` + `feishu_secret` | `SZHITCH_FEISHU` |
| 钉钉 | `notify.dingtalk_webhook` + `dingtalk_secret` | `SZHITCH_DINGTALK` |
| Bark（iOS） | `notify.bark_key` | `SZHITCH_BARK` |
| Server酱 | `notify.serverchan_key` | `SZHITCH_SERVERCHAN` |
| 通用 Webhook | `notify.webhook_url` | `SZHITCH_WEBHOOK` |

### 邮件怎么配

以 QQ 邮箱为例（163、Gmail、Outlook 同理，用 **SMTP 授权码**，不是登录密码）：

```bash
export SZHITCH_SMTP_HOST=smtp.qq.com
export SZHITCH_SMTP_PORT=465
export SZHITCH_SMTP_USER=you@qq.com
export SZHITCH_SMTP_PASS=你的16位授权码
export SZHITCH_SMTP_TO=you@qq.com,other@example.com
python main.py test-notify
```

或直接写进 `config.json`：

```json
"notify": {
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "user": "you@qq.com",
    "password": "授权码",
    "to": ["you@qq.com"],
    "ssl": true
  }
}
```

邮件是 HTML 卡片版：每条车一张卡，含路线、车型、现价/原价、「省 99%」、日期、门店。同一封邮件按「新上线 / 回到订单库 / 被锁定」分节。

---

## 部署

### GitHub Actions（推荐，零成本）

1. 建一个**公开**仓库（Free 套餐的 Pages 只支持公开仓库），把本目录推上去
2. **Settings → Secrets and variables → Actions** 添加邮件等密钥（见上表）
3. **Settings → Pages → Source** 选 `GitHub Actions`
4. 启用 Actions，手动跑一次（首次是建基线）

之后每 4 小时自动跑：抓取 → 发通知 → 提交 `dashboard/dashboard.json` → 发布 Pages。
面板地址形如 `https://<用户名>.github.io/<仓库名>/`。

> 仓库是公开的，所以**密钥只能放 Secrets**。配置里留空即可，工作流里有一道检查，
> 一旦发现 `config.json` 出现明文 webhook/密码会直接失败并报错。

> GitHub 的 cron 用 UTC，且高峰期可能延迟数分钟到十几分钟。要更准就用云函数。

### 本机 / VPS 常驻

```bash
python main.py watch
```

```bash
sudo cp -r . /opt/shenzhou-hitch-monitor
sudo cp deploy/systemd/szhitch.service /etc/systemd/system/
sudo systemctl enable --now szhitch
journalctl -u szhitch -f
```

### Docker

```bash
docker build -t szhitch .
docker run -d --name szhitch --restart always \
  -e SZHITCH_SMTP_HOST=smtp.qq.com -e SZHITCH_SMTP_USER=you@qq.com \
  -e SZHITCH_SMTP_PASS=授权码 -e SZHITCH_SMTP_TO=you@qq.com \
  -p 8765:8765 \
  -v $(pwd)/config.json:/app/config.json -v $(pwd)/data:/app/data \
  szhitch
```

### 邮箱订阅（Cloudflare Worker，可选）

`cloud/cf_worker/` 是一个 Worker + KV：看板提交邮箱和条件 → 发确认信 → 点确认才生效；
监控每轮用密钥拉取已确认订阅，提醒邮件带退订链接。需要 SMTP（推荐 Resend）已配好。

1. 改 `cloud/cf_worker/wrangler.toml`：KV id（`npx wrangler kv namespace create szhitch-subs`）、自定义域名、`MAIL_FROM`、`ALLOWED_ORIGINS`（看板所在域名）
2. 在该目录 `npx wrangler deploy`，再 `wrangler secret put RESEND_API_KEY` 与 `wrangler secret put ADMIN_TOKEN`（随机长串）
3. 仓库 Secrets 加 `SZHITCH_SUB_API`（Worker 地址）与 `SZHITCH_SUB_TOKEN`（同 ADMIN_TOKEN）

> `*.workers.dev` 在国内基本打不开，务必挂自己的域名。
> 仓库 Secrets 再加 `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`，工作流会顺带把看板发布到 Cloudflare Pages（项目名 `szhitch`）。

### 阿里云函数计算 / 腾讯云 SCF

处理程序分别填 `cloud.aliyun_fc.handler.handler` 与 `cloud.tencent_scf.main_handler.main_handler`，定时触发器建议 4 小时一次。
函数实例无状态，**必须**外接存储放状态：`SZHITCH_STATE=oss://bucket/szhitch/state.json` + `OSS_*` 环境变量，否则每轮都会重复推送。

---

## 常见问题

**Q：面板说「被锁定」，但 App 里明明还能订？**这是全量翻页的漏抓导致的误判。现已修复：消失的车会按「取车城市 + 还车城市」定向复查，查得到就撤销判定并记 `[纠正]` 日志；而且要连续两轮查不到才推送。历史遗留的误判可手动修：

```bash
python main.py recheck --dry-run   # 先看
python main.py recheck             # 写回
```

**Q：城市为什么是 471 个？会不会漏掉？**
471 是官方 `cityList` 接口返回的全量（`allCities` 471 + `hotCities` 24，去重后 471）。
面板里的城市选择器不再只列「当前有车的城市」，而是整份字典 —— 现在在售覆盖 190 多个城市，
剩下 200 多个暂时没车，但你想盯的时候能选到。万一列表里冒出新城市而字典还没收录，
`insights.py` 会把它补进 `city_dict`（字母归 `#`），面板不会显示不出来。

**Q：路线写了 `成都→丽江`，为什么 `丽江→成都` 的车也推了？**
现在的实现是方向敏感的，不会。早期版本把两个城市名当子串在整串里找，反向线路会误命中，
这个问题已修，并在 `tests/test_catalog_rules.py` 里用 `test_direction_is_respected` 锁死。
反过来，如果你**想**要双向，写两条：`["成都→丽江","丽江→成都"]`，或者用 `any_cities`。

**Q：`watch` 和 `watch_profiles` 同时配了，以哪个为准？**
`watch_profiles` 非空时只用订阅分组，`watch` 被忽略；想回到单规则模式就把 `watch_profiles` 清空。

**Q：面板里「只看关注」为什么是空的？**
`★` 来自服务端订阅（`watch_profiles`）的命中结果，而面板数据是监控跑完写出来的。
先在面板里选好条件、复制 `watch_profiles` 进 `config.json`，跑一轮 `python main.py once --export dashboard/dashboard.json` 就会标出来。

**Q：为什么第一次正式运行没有推送？**
首轮只建基线，否则 950+ 条会刷屏，之后也分不清哪些是真新出现。想看效果用 `--dry-run`。

**Q：一键订阅按钮是灰的？**
面板数据里没有仓库信息。GitHub Actions 部署时会自动带上（`GITHUB_REPOSITORY`）；
本地 `python main.py serve` 预览时需在 `config.json` 里加 `"subscribe": {"repo": "你的用户名/仓库名"}`。

**Q：预警记录为什么是空的？**
记录来自监控每轮的命中流水。先在 `config.json` 配好 `watch_profiles`（或用面板生成），跑一轮
`python main.py once` 之后就有记录；GitHub Actions 部署用 cache 保存状态，换仓库存（OSS）不会丢。

**Q：推送里的「原价 3048」是什么？**
顺风车本质是异地调车，`estTotalPrice` 是按日租算的正常价，`realTotalPrice` 是调车补贴价。

**Q：会被封吗？**
列表接口不需要账号。默认每 1.2 秒一个请求、4 小时一轮（约 6 轮/天、合计 300 请求/天以内），实测无风控。接口无公开文档，随时可能变。

---

## 目录结构

```
shenzhou-hitch-monitor/
├── main.py                 CLI：once / watch / serve / recheck / snapshot / cities
├── szhitch/
│   ├── gateway.py          网关客户端 + 抗漏抓翻页
│   ├── models.py           HitchItem 数据模型
│   ├── catalog.py          城市索引（拼音/首字母/省份）+ 车型品牌解析
│   ├── rules.py            筛选规则、路线方向匹配、订阅分组
│   ├── store.py            状态机 + 到达/预警日志（SQLite / JSON / OSS）
│   ├── subscriptions.py    订阅中心：GitHub Issue 订阅的读取与回执
│   ├── monitor.py          生命周期判定、文案、邮件 HTML
│   ├── insights.py         面板数据 + 预警记录
│   ├── notify.py           邮件/企微/飞书/钉钉/Bark/Server酱
│   └── config.py           配置加载
├── dashboard/              静态看板（index.html + dashboard.json）
├── tools/build_cities.py   生成城市字典（拼音/首字母，需 pypinyin）
├── cloud/                  云函数入口
├── deploy/systemd/         systemd unit
├── data/cities.json        471 个城市 + 省份 + 拼音 + 首字母
├── data/brands.json        品牌别名表（约 100 个品牌）
├── docs/                   逆向报告 + 面板截图
└── tests/                  76 项离线测试（无需联网）
```

```bash
python -m unittest discover -s tests -v
```

---

## 合规提醒

- 只读取**公开可见**的在售列表，不涉及登录、下单、账号数据。
- 仅用于个人比价/盯盘，请勿转售数据或用于商业用途。
- 保持克制的请求频率；接口属于对方资产，滥用可能违反服务条款。
- 库存、价格、规则以 App 内实际展示为准。
