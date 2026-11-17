# 任务完成看板 · TaskBoard

中文 | [English](README.en.md)

给「一人老板 + 多个 AI Bot 干活」用的任务量化看板：Bot 每干完一项就写进接口，老板打开手机 3 秒看清今天干了多少、卡在哪、哪些等自己拍板。

- 总览大百分比圆环 + 总数拆分（分子分母永远可见）
- 助理侧完成率（剔除待用户拍板 / 外部卡住 / 用户下令搁置，剔除明细可展开逐条看）
- 分项目柱状图（绝对数 + 百分比）
- 甘特图（时间轴恒覆盖所选日期全天 00:00–24:00，所选日为今天时必显今日线且位置对应当前时刻；无起止时间的任务进未排期区，不伪造时间）
- 8 状态机 + 用户验收/打回（只有用户验收通过才算已完成）
- 开放 REST 接口：Bot 按独立令牌直写，汇总全部由任务明细实时聚合，禁止手填

## 快速开始（零依赖）

只需要 Python 3.10+ 标准库，不用 pip install 任何东西：

```bash
python3 server.py            # 默认 http://127.0.0.1:8780
python3 server.py --port 9000
```

打开浏览器访问即可。首次启动会从 `data/seed_tasks.json` 生成运行时数据 `data/tasks.json`（种子为脱敏演示数据：结构与统计分布与真实使用一致，内容为虚构示例）；删掉 `data/tasks.json` 即可重置回种子。

## 接口

令牌在 `tokens.json`（仓库内为本地开发演示值，上公网前必须更换）。请求头：`Authorization: Bearer <token>`。`role=bot` 可写任务；`role=user`（老板本人）才能验收/打回。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/tasks?date=&period=&anchor=&project=&status=&q=` | 任务列表（可筛选） |
| GET | `/api/tasks/{id}` | 单条详情（含证据、卡点、历史） |
| GET | `/api/summary?date=&period=&anchor=&project=&status=&q=` | 汇总：总数/完成数/总完成率/助理侧完成率/分项目/各状态计数/剔除明细/自检 |

统计周期（CHANGE-03）：`period` 取 day/week/month/year（周 = ISO 周一~周日、月 = 自然月、年 = 自然年），`anchor` 为锚点日期（YYYY-MM-DD，不传默认今天），两者传了则按推导出的 [range_start, range_end] 过滤并忽略 `date`；不传 `period` 时保持 `date` 精确匹配的旧行为。summary 响应回显 `period`/`anchor`/`range_start`/`range_end`；非法 period/anchor 返回 400。汇总里的「待我处理」是当前时点数（待验收+待拍板全量实时计数），不受周期影响。
| POST | `/api/tasks` | 新建任务（bot） |
| PATCH | `/api/tasks/{id}` | 更新进度（进行中须 10 的倍数 10–80 且带 note）/字段/证据（bot） |
| POST | `/api/tasks/{id}/transition` | 状态流转（bot，按状态机校验） |
| POST | `/api/tasks/{id}/submit` | 提交待验收（bot，无证据拒绝，progress 固定 95） |
| POST | `/api/tasks/{id}/accept` | 验收通过 → 已完成（仅 user，progress 100） |
| POST | `/api/tasks/{id}/reject` | 打回 → 进行中（仅 user，必填 reason，progress 回落 ≤80） |
| GET | `/api/license/status` | 企业版权益状态（统一开关入口，只读本地缓存令牌的验签结果） |
| POST | `/api/license/activate` | 提交注册码 → 远程授权服务器验证 → 换短期权益令牌（仅 user） |
| POST | `/api/license/verify` | 向远程重新验证并刷新权益令牌（仅 user） |
| POST | `/api/license/deactivate` | 停用：通知远程吊销 + 清除本地令牌缓存（仅 user） |

关键校验（PRD 口径）：无证据不得进待验收；无 `blocker_type`+`blocker_reason` 不得进被卡住；任何 Bot 都不能直接把任务置为已完成；终态（已完成/已取消）不可再流转。

## 量化口径（可复算）

- 总完成率 = 已完成数 / 有效总数（总数 − 已取消）
- 分项目完成率 = 该项目已完成 / 该项目有效总数
- 助理侧完成率 = 已完成 /（有效总数 − 待用户拍板 − 被卡住且 external/awaiting_user − 已搁置）
- progress：待办 0；进行中 10–80（10 的倍数、须带备注）；被卡住/待拍板/已搁置冻结；待验收 95；已完成 100

## 技术选型

- 后端：Python 标准库 `http.server`，零第三方依赖，一条命令启动。
- 前端：原生 HTML/CSS/JS，圆环/柱状/甘特全部手写 SVG（未引入图表库）。
- 甘特选型说明：调研过 Frappe Gantt（MIT），MVP 为保持零依赖与可控口径采用手写 SVG 实现同等功能（起止条 + progress 填充 + 分组 + 全天时间轴 + 今日线）；将来需要拖拽改期/依赖箭头时可替换为 Frappe Gantt 或企业版高级甘特（见 Editions）。
- 只学开源机制（Plane 的视图组织、OpenProject 的甘特口径、Vikunja 的 API 优先、Kanboard 的统计口径），未复制任何 AGPL/GPL 项目代码。

## Editions（Open Core）

本仓库是 **Community 版**，按 Open Core 模式运营（划分全文为运营方内部文档）：

- **Community（MIT，开源免费）**：本仓库全部功能，即简单任务量化全套——总览圆环、助理侧完成率、分项目柱状图、基础甘特、任务明细与 8 状态流转、筛选、开放接口读写与汇总、Bot 令牌直写、用户验收/打回、待我处理收件箱、数据自检。单机/自托管、单工作区。
- **Enterprise（付费，保留，不在本仓库）**：高级功能保留收费，将来放独立私仓以插件/扩展包接入，**本仓库不包含任何企业版功能代码**；仅按变更单 CHANGE-02 预留注册码远程验证接口层（见下节）。候选清单（终版待另行拍板，本节不构成价格/功能承诺）：
  - 多人/多工作区、组织架构与细粒度权限、SSO
  - 历史趋势高级分析、燃尽/累计流、周期视图、基线对比（计划 vs 实际偏差考核）
  - 任务依赖/里程碑/关键路径等高级甘特
  - 批量操作、定时通知/提醒、审计报表导出与分享
  - 大规模/高并发、企业级支持与 SLA

### 企业版扩展点（仅预留，不实现）

为将来接入企业版，架构上只留以下占位（MVP 均未实现任何企业版功能）：

1. **功能开关统一入口**：企业版是否可用只由权益状态决定——服务端 `enterprise_entitled()`、客户端 `GET /api/license/status` 与前端 `window.TB_ENTERPRISE`，任何企业版功能都必须走这个入口，不许自设本地布尔开关。
2. **插件接口占位**：前端视图区按「大盘 / 柱状 / 甘特 / 明细」四块独立渲染，将来高级视图（趋势/燃尽/依赖甘特）可作为新视图块挂载，不侵入现有四块。
3. **数据边界**：全部量化口径只依赖任务明细字段（见接口表），企业版分析直接消费同一明细，不需要社区版预埋付费逻辑。

### 企业版注册码接口（预留层，CHANGE-02）

免费版**不依赖**注册码：不填任何注册码，上面全部功能照常使用。注册码只为将来的企业版功能预留，验证**必须远程**：

- **配置**：环境变量 `TASKBOARD_LICENSE_SERVER_URL`（远程授权服务器地址）、`TASKBOARD_LICENSE_PUBLIC_KEY_FILE`（授权服务器 RSA 公钥 PEM 路径，默认仓库根目录 `license_public_key.pem`）。
- **流程**：`activate` 把 注册码 + 本机实例标识 提交远程 → 远程校验（存在/激活/到期/绑定）后签发**带签名、带到期的短期权益令牌** → 本地验签（公钥）+ 查到期 + 查实例绑定后缓存令牌；`verify` 向远程刷新令牌（远程可吊销，吊销/过期即失效）；`deactivate` 通知远程吊销并清除本地缓存。令牌过期后企业版自动关闭，须重新远程验证。
- **本地没有任何签发能力**：签发私钥只存在于远程授权服务器，主服务代码里只有公钥验签；不存在离线放行逻辑，未配置/连不上远程时企业版就是未激活状态。
- **远程不可达时**：免费版全部接口与页面照常运行，仅企业版返回「无法验证」提示；吊销/过期只停企业版，不锁用户数据。
- **强度边界（如实说明）**：任何纯本地软件都无法保证「绝对无法破解」，本设计的目标是让破解成本高于购买成本——远程签发、短期令牌、服务端可吊销/封禁异常注册码、企业版值钱的能力将来尽量由远程服务参与下发，而不是本地改一个开关全开。
- **本地联调**：`tools/mock_license_server.py` 是仅供测试的 mock 远程授权服务（私钥只在它自己的状态目录里，演示注册码 `TB-ENT-DEMO-0001`）；`python3 tools/test_license_flow.py` 一键自测激活/无效码/过期/远程关停全流程。真正的授权服务器与签发后台等企业版立项时另行开发。

## 目录

```
server.py            后端（API + 静态文件服务 + 企业版注册码预留层）
public/              前端（index.html / style.css / app.js）
data/seed_tasks.json 种子数据（脱敏演示数据）
data/tasks.json      运行时数据（首次启动生成，已 gitignore）
data/license.json    权益令牌缓存（运行时生成，已 gitignore）
data/instance_id     本机实例标识（运行时生成，已 gitignore）
tools/               mock 远程授权服务 + 注册码全流程自测脚本（仅测试用）
tokens.json          令牌表（开发演示值，上线前更换）
LICENSE              MIT
```

## License

MIT — 见 [LICENSE](LICENSE)。
