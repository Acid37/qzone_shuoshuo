# Qzone Shuoshuo 插件

QQ 空间说说插件（`qzone_shuoshuo`），为 Neo-MoFox 提供完整的说说发布、阅读、互动与自动监控能力。

- **当前版本**：`1.5.0`
- **适配器依赖**：`napcat_adapter`
- **推荐入口命令**：`/send_feed`、`/read_feed`
- **生产状态**：✅ 已就绪，可直接用于生产环境

---

## 功能总览

### 1) 说说操作
- 发布说说（文字 / 多图）
- 删除说说
- 获取说说列表与详情
- 可见范围控制（公开 / 好友可见 / 仅自己可见）

### 2) AI 驱动的智能互动
- **AI 评论生成**：基于 LLM 生成自然、贴合语境的评论
- **智能回复**：自动回复自己说说下的他人评论，支持时间上下文感知
- **概率控制**：点赞 / 评论 / 回复均可配置概率（0.0~1.0）
- **内容去重**：发布前自动去重，避免短时间重复内容
- **人设注入**：评论/回复自动对齐 Bot 人设、安全底线与禁止行为

### 3) 自动监控
- 定时轮询好友动态流（`feeds3_html_more`）
- 新动态通知推送（群聊 / 私聊）
- 自动点赞 / 自动评论（可选，支持概率控制）
- 静默时间窗口（默认 `23:00-07:00`，可配置）
- 批量处理限速：单轮最多处理 5 条，每条间 3-8 秒随机延迟
- 手动触发冷却：手动执行后自动重置计时，避免"刚手动又自动"
- 已读追踪：防止历史动态重复处理

### 4) 安全与稳定性
- **评论并发锁**：防止同一评论被并发处理时重复回复
- **已点赞过滤**：自动跳过已点赞的说说，避免无效操作
- **Cookie 自动管理**：失效检测、自动刷新、容错重试
- **人类化随机延迟**：所有互动操作均加入随机间隔，降低风控风险
- **HTTP 退避重试**：遇到 429/5xx 自动指数退避
- **失败原因分类**：cookie / rate_limit / server / permission / parse

### 4) 状态可观测性
监控状态可通过 `service.get_monitor_status()` 查看：
- 监控运行状态、最近执行时间、来源
- 最近结果（ok / skipped / error）
- 最近跳过原因与错误信息
- 启动重试进度（如适用）

---

## 命令入口

### `/send_feed [内容]`
发布一条说说。

**行为说明**：
- 直接跟文本内容即发布
- 空内容或输入 `随机` `random` `rand` 时，由 AI 生成随机主题后发布

**示例**：
```
/send_feed 今天风挺舒服的
/send_feed 随机
/send_feed
```

### `/read_feed [数量]`
读取最近说说，并按配置概率自动点赞/评论。

**默认行为**：
- 读取当前登录 QQ 的好友动态
- 跳过本人动态的自动评论
- 点赞和评论概率从配置 `[monitor]` 节读取（默认点赞 0.8 / 评论 0.3）
- 返回处理摘要（读取条数、互动结果、失败原因）

**示例**：
```
/read_feed          # 读取 5 条
/read_feed 10       # 读取 10 条
```

## Action 接口

| Action | 说明 | 关键参数 |
|---|---|---|
| `send_shuoshuo` | 发布说说 | `content`(必填), `images`(可选), `visible`(可选) |
| `read_shuoshuo` | 阅读说说 | `count`(可选), `offset`(可选), `qq_number`(可选) |

---

## 配置说明

配置文件：`config/plugins/qzone_shuoshuo/config.toml`

### `[plugin]`
- `enabled`: 是否启用插件（默认 `true`）

### `[debug]`
- `enable_debug`: 调试开关
- `log_level`: `info` / `debug`

### `[qzone]`
- `default_visible`: 默认可见范围（`all` / `friends` / `self`）
- `enable_image`: 是否允许图片（默认 `false`）
- `max_image_count`: 单条最大图片数（默认 `9`）

### `[monitor]`
| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 自动监控总开关 |
| `auto_start` | bool | true | 插件加载后自动启动监控 |
| `feed_source` | str | `friend_flow` | 监控动态源（`friend_flow` / `self_list`） |
| `friend_feed_count` | int | 20 | `friend_flow` 源每轮抓取条数（5-50） |
| `default_interval` | int | 1800 | 默认轮询间隔（60~86400 秒） |
| `log_heartbeat` | bool | true | 是否输出每轮监控心跳日志 |
| `auto_like` | bool | true | 默认启用自动点赞 |
| `auto_comment` | bool | true | 默认启用自动评论 |
| `like_probability` | float | 0.8 | 点赞概率（0.0-1.0） |
| `comment_probability` | float | 0.3 | 评论概率（0.0-1.0） |
| `enable_auto_reply_comments` | bool | true | 是否回复自己说说的评论 |
| `auto_reply_probability` | float | 0.9 | 回复自己说说评论的概率 |
| `quiet_hours_enabled` | bool | true | 是否启用静默时间窗口 |
| `quiet_hours_start` | int | 23 | 静默时段开始小时（0-23） |
| `quiet_hours_end` | int | 7 | 静默时段结束小时（0-23） |

### `[storage]`
- `data_dir`: 插件数据目录（默认 `data/qzone_shuoshuo`）

---

## 监控行为规则

1. **首次启动建立基线**：记录最新 `tid`，不会补推历史动态
2. **增量处理**：后续仅处理"基线之后"的新动态
3. **批量处理限速**：单轮最多处理 5 条新说说，每条间 3-8 秒随机延迟
4. **已读防重复**：已读追踪 + 已评论追踪 + 已点赞过滤，确保不重复处理
5. **静默窗口**：默认 `23:00-07:00` 不执行自动监控（可配置）
6. **手动冷却**：手动触发后自动重置计时器，避免"刚手动又自动"
7. **强制首轮**：`start` 后立即执行一轮（跳过静默/冷却检查），但遵守已读/去重逻辑

---

## 技术架构

### 核心依赖
- **HTTP 客户端**：`httpx`（异步）
- **JSON 解析**：`orjson`（高性能） + `json_repair`（容错修复）
- **HTML 解析**：`beautifulsoup4`（精确提取）
- **异步运行时**：`asyncio`
- **调度器**：Neo-MoFox 统一调度器（`src.kernel.scheduler`）

### 接口对齐
- 好友动态：`feeds3_html_more`（ic2.qzone.qq.com）
- 说说列表：`msglist_v6`（taotao.qq.com）
- 发布说说：`emotion_cgi_publish_v6`
- 评论/回复：`emotion_cgi_re_feeds`
- 点赞接口：`internal_dolike_app`
- 图片上传：`cgi_upload_image`

### 安全设计
| 机制 | 说明 |
|------|------|
| 评论并发锁 | `processing_comments` set + `try/finally` 确保解锁 |
| 已点赞过滤 | 从 HTML 提取 `data-islike` 状态，避免重复点赞 |
| Cookie 容错 | -3000 错误码检测 + 自动刷新 + 重试 |
| HTTP 退避 | 429/5xx 指数退避，最多 2 次重试 |
| 人类化延迟 | 所有互动加入 1-8 秒随机间隔 |
| 内容去重 | 发布前 SHA256 哈希校验，5 分钟内不重复 |

---

## 依赖

- Python `>=3.11`
- `httpx`
- `orjson`
- `json-repair`
- `beautifulsoup4`
- `aiofiles`
- Neo-MoFox `>=1.0.0`
- `napcat_adapter`

---

## 许可证

本插件遵循项目根目录的 `LICENSE` 文件条款。
