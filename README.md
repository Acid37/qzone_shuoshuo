# Qzone Shuoshuo 说说插件

> 让你的 Bot 能够发送和管理 QQ 空间说说喵

---

## 功能特性

### 🛠️ 插件演进与技术债 (Technical Backlog)

#### 1. 存储架构升级 (Storage & DB Migration)
- [ ] **由 JSON 转向 DB 索引**：
    - **现状**：`read_tids` 和 `commented_tids` 存放在 `monitor_state.json` 中，随着数据增加性能线性下降。
    - **建议**：定义 `QzoneTidModel(Base)`，接入 `kernel.db` 使用 `CRUDBase` 进行持久化。利用 SQLite 的索引能力实现毫秒级去重，并支持按时间自动清理超期数据。
- [ ] **标准化存储路径**：移除硬编码的 `data/qzone_shuoshuo` 路径，改用框架 `storage_api` 获取标准数据目录，增强容器化环境兼容性。

#### 2. 调度与生命周期标准化 (Standardized Task Scheduling)
- [ ] **接入 `kernel.scheduler`**：
    - **现状**：监控逻辑通过内部私有循环或 Action 触发。
    - **建议**：将 `check_new_shuoshuo` 注册为框架层级的 `Job`。这样可以利用框架自带的 **任务可视化管理** 和 **失败回退机制**，且不再阻塞插件的卸载流程。
- [ ] **全面接入 `TaskManager`**：确保 VLM 图片识别等耗时异步任务都通过 `get_task_manager().create_task` 运行，规避“僵尸协程”风险。

#### 3. 健壮性与类型契约 (Robustness & Type Contract)
- [ ] **Pydantic 数据模型化**：
    - **问题**：大量使用 `dict[str, Any]` 取值（如 `data.get("code")`），缺乏 IDE 自动补全且易因上游协议变动导致隐蔽崩溃。
    - **建议**：为 `get_shuoshuo_list` 和图片上传结果定义强类型的 `BaseModel`。
- [ ] **精细化错误处理**：将目前的 `except Exception` 细化为 `NetworkError` (httpx), `AuthError` (Cookie过期), `ParseError` (JSON解析失败)。支持针对不同错误类型触发不同的重试策略。

#### 4. 对话感知优化 (Dialogue Context Awareness)
- [ ] **回执上下文注入**：
    - **问题**：`/send_feed`、`/read_feed` 的执行成功与否目前仅作反馈显示，AI 并不一定能感知到“已发送”这一状态事实，可能导致重复操作。
    - **方案**：在命令执行完成后，将关键结果（如 `tid` 或发送成功摘要）稳定注入当前对话流的 `LLMPayload` 历史中。

### 发送说说
- 支持发送纯文本说说
- 支持发送带图片说说（最多9张）
- 可设置可见范围（所有人/好友/仅自己）

### Cookie 管理
- Cookie 有效期检查：每次使用前自动验证有效性
- 自动刷新：检测到 Cookie 失效后自动从 NapCat 适配器刷新

### 查询管理
- 查询说说列表
- 查看说说详情
- 删除说说

### 互动功能
- 点赞说说
- 评论说说
- 回复他人评论

### 监控功能
- **阅读说说**：让 AI 主动前往 QQ 空间阅读说说
- **自动监控**：启动后自动检测新说说，支持：
  - 推送通知到群/好友
  - 概率点赞（可配置概率）
  - 概率评论（可配置概率，支持 AI 生成评论）
  - 自动回复自己说说下的评论（默认开启，高概率，归入互动开关）
  - 静默时间窗口控制（默认 23:00-07:00，半夜不执行）
  - 主动执行 `/read_feed` 后自动重置监控计时，避免与自动监控抢节奏

### 调试功能
- Debug 模式开关
- 简洁/详细日志级别

### 命令模式（已对齐）
- 仅保留两个命令入口：`/send_feed` 与 `/read_feed`
- `/read_feed` 默认会在“阅读”流程内按概率执行点赞/评论（可配）
- `/read_feed` 优先按未读列表识别，仅处理未读内容；处理完成后自动标记为已读
- 支持 `--read-only`，仅阅读不互动
- 支持细粒度开关：`--no-like`、`--no-comment`
- 支持概率覆盖：`--like-prob=0.8`、`--comment-prob=0.3`
- 概率参数说明：若传入值超出 `[0,1]`，将自动回退到配置默认值（并在结果中提示）
- 执行后会返回自然语言总结，并引导继续对话
- 互动失败会返回用户可见的分类摘要（如频率限制、服务不稳定）

> 说明：`action=read_shuoshuo` 采用“规则优先 + 概率兜底”策略：
> - 用户明确要求（只读/只点赞/只评论）优先；
> - 未明确要求时，按兴趣与概率决定是否继续点赞/评论；
> - 无论是否执行互动，都会返回任务总结（read/like/comment 与原因）。

---

## 原生 Action 支持

LLM 可以通过以下动作与插件交互：

| 动作 | 用途 | 参数 |
|------|------|------|
| `send_shuoshuo` | 发送说说 | `content`(必填), `images`(可选), `visible`(可选) |
| `delete_shuoshuo` | 删除说说 | `shuoshuo_id`(必填) |
| `like_shuoshuo` | 点赞说说 | `shuoshuo_id`(必填), `owner_qq`(可选) |
| `comment_shuoshuo` | 评论说说 | `shuoshuo_id`(必填), `content`(必填), `owner_qq`(可选), `comment_id`(可选) |
| `read_shuoshuo` | 阅读说说 | `count`(可选), `offset`(可选), `qq_number`(可选) |
| `auto_monitor` | 自动监控 | 见下方详细参数 |

### auto_monitor 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action_type` | str | 必填 | start/stop/status |
| `interval` | int | 300 | 监控间隔（秒） |
| `target_group` | str | - | 推送群号 |
| `target_user` | str | - | 推送QQ号 |
| `auto_comment` | bool | false | 是否自动评论 |
| `auto_like` | bool | false | 是否自动点赞 |
| `like_probability` | float | 1.0 | 点赞概率 (0.0-1.0) |
| `comment_probability` | float | 0.3 | 评论概率 (0.0-1.0) |
| `quiet_hours_enabled` | bool | true | 是否启用监控静默时间窗口 |
| `quiet_hours_start` | int | 23 | 静默时段开始小时（0-23，含） |
| `quiet_hours_end` | int | 7 | 静默时段结束小时（0-23，不含） |

---

## 使用示例

### 命令模式（推荐）
```
# 发送说说
/send_feed 今天风有点舒服

# 阅读最近 5 条（默认会按概率互动）
/read_feed

# 阅读最近 10 条
/read_feed 10

# 只阅读，不点赞不评论
/read_feed 10 --read-only

# 只点赞不评论
/read_feed 10 --no-comment --like-prob=1.0

# 只评论不点赞
/read_feed 10 --no-like --comment-prob=1.0

# 自定义互动概率
/read_feed 10 --like-prob=0.4 --comment-prob=0.2
```

### 阅读说说
```
action=read_shuoshuo
action=read_shuoshuo, qq_number='123456789', count=20
```

### 评论说说
```
action=comment_shuoshuo, shuoshuo_id='abc123', content='说得太对了！'
```

### 启动自动监控
```
# 基础启动
action=auto_monitor, action_type='start'

# 设置间隔和推送
action=auto_monitor, action_type='start', interval=300, target_group='群号'

# 开启自动评论（30%概率）
action=auto_monitor, action_type='start', auto_comment=true

# 开启自动点赞（80%概率）并评论（50%概率）
action=auto_monitor, action_type='start', auto_like=true, like_probability=0.8, auto_comment=true, comment_probability=0.5

# 查看状态
action=auto_monitor, action_type='status'

# 停止监控
action=auto_monitor, action_type='stop'
```

---

## 文件结构

```
qzone_shuoshuo/
├── manifest.json            # 插件元数据
├── plugin.py                # 插件入口
├── config.py                # 配置定义
├── LICENSE                  # MIT 许可证
├── README.md                # 插件文档
├── __init__.py
├── actions/                 # 动作模块
│   ├── __init__.py
│   ├── send_shuoshuo.py     # 发送说说动作
│   ├── delete_shuoshuo.py   # 删除说说动作
│   ├── like_shuoshuo.py     # 点赞说说动作
│   ├── comment_shuoshuo.py  # 评论说说动作
│   ├── read_shuoshuo.py     # 阅读说说动作
│   └── auto_monitor.py      # 自动监控动作
├── commands/                # 命令模块
│   ├── __init__.py
│   └── shuoshuo_commands.py # 命令处理器
├── core/                    # 核心模块
│   ├── __init__.py
│   ├── service.py           # Qzone服务
│   ├── cookie_manager.py    # Cookie管理
│   └── dependency_manager.py# 依赖检测与自动安装
└── event_handlers/          # 事件处理模块
    ├── __init__.py
    └── monitor_handler.py   # 监控处理器
```

---

## 安装

1. 将 `qzone_shuoshuo/` 目录放入 Neo-MoFox 的 `plugins/` 文件夹
2. 首次启动自动生成配置

---

## 配置

配置文件路径：`config/plugins/qzone_shuoshuo/config.toml`（系统自动生成）

### 配置节

#### `[plugin]`
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 是否启用插件 |

#### `[debug]`
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_debug` | `false` | 是否启用调试模式 |
| `log_level` | `info` | 日志级别: info(简洁), debug(详细) |

#### `[qzone]`
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `default_visible` | `all` | 默认可见范围 |
| `enable_image` | `false` | 是否允许发送图片 |
| `max_image_count` | `9` | 单条说说最大图片数 |

> 说明：适配器签名已改为插件内部固定使用 `napcat_adapter:adapter:napcat_adapter`，不再对外暴露配置。

#### `[monitor]`
> 监控默认值，实际配置由 `action=auto_monitor` 参数指定

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 监控总开关，关闭后不允许启动自动监控 |
| `default_interval` | `5400` | 默认监控间隔（秒），当未传 `interval` 时生效，范围 60-86400 |
| `like_probability` | `0.8` | 点赞概率 (0.0-1.0)，0.8=80% |
| `comment_probability` | `0.3` | 评论概率 (0.0-1.0)，0.3=30% |
| `enable_auto_reply_comments` | `true` | 是否自动回复自己说说下的评论 |
| `auto_reply_probability` | `0.9` | 自动回复自己说说评论的概率 |
| `quiet_hours_enabled` | `true` | 是否启用监控静默时间窗口（静默时段跳过自动监控） |
| `quiet_hours_start` | `23` | 静默时段开始小时（0-23，含） |
| `quiet_hours_end` | `7` | 静默时段结束小时（0-23，不含；小于开始小时视为跨天） |

> 行为说明：
> - 自动回复评论会跟随“自动评论”开关：关闭自动评论时，不会执行自动回复评论。
> - 手动执行 `/read_feed` 会重置自动监控计时，避免刚手动读完又立刻触发一轮自动执行。

#### AI 上下文策略（无 `[prompt]` 配置节）
> 评论/回复/发布改写提示词已全部内置，统一由插件注入到 LLM 上下文，不再开放 TOML 配置项。
>
> **重要**：
> - 人设与风格自动从 `core.toml` 注入（内置必选）。
> - 平台说明与行为约束为插件内置固定文案。
> - 发布说说改写固定执行（无开关、无模板配置）。
> - 当说说包含图片时，会先进行图像语义识别，再注入评论/回复上下文。
>
> 评论策略：模型未生成有效评论时，将**直接跳过/失败**，不使用模板兜底。

#### 完整提示词结构（已与当前实现对齐）

为保证复刻效果，评论与回复都使用同一骨架顺序：

1. 平台说明
2. 人设定义（自动注入 `core.toml`）
3. 语言风格（自动注入 `core.toml`）
4. 当前情景（包含说说内容、评论内容、图片语义上下文等）
5. 接下来你说（直接要求生成正文）
6. 输出要求（仅允许输出单行正文）

评论链路会使用「请直接说一句自然、得体、有互动感的评论正文」进行收束；
回复链路会使用「请直接生成一条自然、礼貌、有人味的回复正文，贴合当前说说和评论语义」进行收束。

> 说明：图片不会直接注入 URL，而是先做图像语义识别得到摘要，再注入“当前情景”。

#### `[storage]`
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `data_dir` | `data/qzone_shuoshuo` | 插件数据存储目录 |

#### 依赖检查（内置）

插件在加载时会自动执行依赖自检，缺失时自动安装（开箱即用，无需额外配置）：

- 优先使用 `uv pip install`
- 若未检测到 `uv`，自动回退到 `python -m pip install`

---

## 依赖

- Python >= 3.11
- httpx
- orjson
- aiofiles
- Neo-MoFox >= 1.0.0
- napcat_adapter（用于获取Cookie）

---
