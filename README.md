# Qzone Shuoshuo 插件

QQ 空间说说插件（`qzone_shuoshuo`），提供发送、阅读、互动与自动监控能力。

- 当前版本：`1.1.0`
- 适配器依赖：`napcat_adapter`
- 推荐入口命令：`/send_feed`、`/read_feed`

---

## 功能总览

### 1) 说说操作
- 发送说说（支持文本与图片）
- 删除说说
- 获取列表与详情

### 2) 社交互动
- 点赞说说
- 评论说说
- 回复评论
- 概率控制（点赞/评论/回复）

### 3) 自动监控
- 定时轮询新动态
- 新动态通知（群/私聊）
- 自动点赞/自动评论（可选）
- 静默时间窗口（默认 `23:00-07:00`）
- 手动阅读后冷却重置，避免“刚手动又自动”

### 4) 启动首轮与就绪重试（v1.1.0）
- `auto_monitor:start` 后会立即执行首轮（`force`）
- 若首轮拿不到 QQ（适配器未就绪），自动进入短周期重试
- 重试成功后停止重试，恢复常规定时任务

### 5) 状态可观测性（v1.1.0）
`auto_monitor:status` 可看到：
- 最近执行时间、来源、是否强制执行
- 最近结果（ok/skipped/error）
- 最近跳过原因与错误信息
- 启动重试进度（当前次数/上限/间隔）

---

## 命令入口（推荐）

### `/send_feed`
发送一条说说。

示例：
- `/send_feed 今天风挺舒服`

### `/read_feed`
阅读最近说说，可附带互动控制。

示例：
- `/read_feed`
- `/read_feed 10`
- `/read_feed 10 --read-only`
- `/read_feed 10 --no-like --comment-prob=1.0`
- `/read_feed 10 --no-comment --like-prob=1.0`

---

## Action 接口

| Action | 说明 | 关键参数 |
|---|---|---|
| `send_shuoshuo` | 发送说说 | `content`(必填), `images`(可选), `visible`(可选) |
| `delete_shuoshuo` | 删除说说 | `shuoshuo_id` |
| `like_shuoshuo` | 点赞说说 | `shuoshuo_id`, `owner_qq`(可选) |
| `comment_shuoshuo` | 评论/回复 | `shuoshuo_id`, `content`, `owner_qq`(可选), `comment_id`(可选) |
| `read_shuoshuo` | 阅读说说 | `count`, `offset`, `qq_number`(可选) |
| `auto_monitor` | 自动监控控制 | `action_type=start/stop/status` |

### `auto_monitor` 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `action_type` | str | 必填 | `start` / `stop` / `status` |
| `interval` | int | `monitor.default_interval`（1800） | 监控间隔（秒） |
| `target_group` | str | - | 推送群号 |
| `target_user` | str | - | 推送用户QQ |
| `auto_comment` | bool | false | 自动评论 |
| `auto_like` | bool | false | 自动点赞 |
| `like_probability` | float | 1.0 | 点赞概率（0-1） |
| `comment_probability` | float | 0.3 | 评论概率（0-1） |

---

## 配置说明

配置文件：`config/plugins/qzone_shuoshuo/config.toml`

### `[plugin]`
- `enabled`: 是否启用插件

### `[debug]`
- `enable_debug`: 调试开关
- `log_level`: `info` / `debug`

### `[qzone]`
- `default_visible`: 默认可见范围
- `enable_image`: 是否允许图片
- `max_image_count`: 单条最大图片数

### `[monitor]`
- `enabled`: 自动监控总开关
- `default_interval`: 默认轮询间隔（60~86400）
- `like_probability` / `comment_probability`
- `enable_auto_reply_comments` / `auto_reply_probability`
- `quiet_hours_enabled`
- `quiet_hours_start` / `quiet_hours_end`

### `[storage]`
- `data_dir`: 插件数据目录（默认 `data/qzone_shuoshuo`）

---

## 监控行为规则（重要）

1. 首次启动自动监控会先建立基线（记录最新 `tid`），不会补推历史动态。
2. 后续仅处理“基线之后”的新动态。
3. `force` 首轮会跳过静默窗口与手动冷却检查，但仍遵守已读/去重逻辑，避免刷屏。
4. 自动回复评论跟随自动评论开关：`auto_comment=false` 时不做自动回复。

---

## 版本变更

### v1.1.0
- 新增：启动首轮连接就绪重试机制
- 新增：监控运行态字段（最近执行来源/结果/跳过原因/错误）
- 优化：`auto_monitor` 状态回执可解释性
- 文档：重整 README，统一行为说明与参数口径

### v1.0.0
- 初始版本：发送/读取/互动/基础监控能力

---

## 开发待办（精简）

- 存储：`read_tids/commented_tids` 从 JSON 迁移到 DB 索引
- 调度：监控间隔随机抖动（低优先级，防规律触发）
- 类型：接口返回结构逐步模型化，减少 `dict[str, Any]` 依赖

---

## 依赖

- Python `>=3.11`
- `httpx`
- `orjson`
- `aiofiles`
- Neo-MoFox `>=1.0.0`
- `napcat_adapter`
