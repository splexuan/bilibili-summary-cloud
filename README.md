# bilibili-summary-cloud

B 站 / YouTube 视频 **AI 总结 + 知识库问答** 的云端版。

从本地版 `bilibili-summary` 重构而来，解决两个原始痛点：
**存储不同步**（改用 COS 对象存储）与 **只能本机跑**（改成可部署的服务）。

- **用户前台** — 提交视频/文章 → 看进度 → 拿总结 → 就内容提问 / 跨内容问答
- **后台管理** — 配置密钥（DeepSeek / 腾讯云 COS / ASR）、用户与邀请码、数据与用量管理

---

## 架构

```
                      ┌─────────────┐
   浏览器 ──HTTP/SSE──▶│  FastAPI    │  Web 层：只做鉴权 + 建任务 + 入队
                      │  (web)      │  立即返回 job_id，不阻塞
                      └──────┬──────┘
                             │ enqueue
                      ┌──────▼──────┐
                      │ Redis + RQ  │◀── 进度也写这里（高频，SSE 轮询读取）
                      └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │   Worker    │  重活：下载 / 转码 / 上传 / ASR / 总结
                      └──────┬──────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ Postgres │   │   COS    │   │ 腾讯云ASR │
        │ 业务数据  │   │ 封面/音频 │   │ 语音识别  │
        └──────────┘   └──────────┘   └──────────┘
```

**核心设计**：Web 与 Worker 共用同一镜像，只是启动命令不同，可独立扩容
（`docker compose up -d --scale worker=3`）。

### 转写策略：字幕优先，ASR 兜底

```
解析视频信息
   │
   ├─ 有字幕？ ──是──▶ 直接用字幕（0 成本，最快）
   │
   └─否──▶ 下载音频 → 转码为 16k 单声道 m4a
                        │
                        ▼
              上传 COS → 生成预签名 URL
                        │
                        ▼
              提交腾讯云「录音文件识别」（异步）
                        │
                        ▼
              轮询结果 → 立即落库 → 删除 COS 音频
```

> ⚠️ **两个必须知道的坑**
>
> 1. **COS 地域必须与服务器地域一致。** 同地域走内网，不产生外网下行流量费；
>    跨地域会真金白银扣钱。腾讯云官方文档明确说明。
> 2. **ASR 识别结果只保留 24 小时。** 必须拿到结果后立刻写入 `videos.transcript`，
>    否则过期后数据永久丢失、只能重新付费识别。

---

## 快速开始

### 方式一：Docker（推荐）

```bash
cd C:\Game\bilibili-summary-cloud
cp .env.example .env
# 编辑 .env，至少填 SECRET_KEY / JWT_SECRET / POSTGRES_PASSWORD / ADMIN_PASSWORD
docker compose up -d --build
```

打开 http://localhost:8000 ，用 `.env` 里的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。

> 首个正式部署请务必改掉默认管理员密码。密钥生成：
> ```bash
> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```

### 方式二：裸机部署（2 核 4G 够用）

不用 Docker，直接跑在服务器上：

```bash
scp -r C:\Game\bilibili-summary-cloud root@你的服务器IP:/tmp/bsum
ssh root@你的服务器IP
cd /tmp/bsum && bash deploy/install.sh
```

脚本会自动装依赖（Postgres / Redis / ffmpeg / Nginx）、建库、生成密钥、
装 systemd 服务、配 Nginx，并打印随机管理员密码。

详见 **[deploy/README.md](deploy/README.md)** —— 含部署后必做项、日常运维、排障。

> 两种方式跑的是**同一套代码**，随时可以切换，不影响数据。

### 方式三：宝塔面板部署

已在服务器装了宝塔面板的话走这条。思路是「中间件用命令行装，项目和 Nginx 用面板管」。

**第一次装的话，看这份一步步的教程 —— [deploy/安装教程.md](deploy/安装教程.md)**，
从一台干净服务器 + 一个宝塔面板开始，全程照抄，每步都有验证方法。

`deploy/宝塔部署.md` 是同一套流程的**要点速查版**（含 MySQL 建库、Python 项目配置、
以及**必须手动加的 SSE 关缓冲配置**），装过一次之后回头查更方便。

> ⚠️ 宝塔软件商店**没有 Postgres**，所以用 MySQL（代码已兼容）。
> 另外 `ilike` 搜索在 MySQL 下会全表扫描，小规模无所谓。

### 方式四：本地开发（不用 Docker）

只需要 SQLite + 内存 Redis，方便改代码：

```bash
cd C:\Game\bilibili-summary-cloud
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 用 SQLite 跑，不依赖 Postgres
set APP_ENV=development
set DATABASE_URL=sqlite+aiosqlite:///./data/dev.db
set SECRET_KEY=<一个 Fernet key>
set JWT_SECRET=<随便一个 32 字节以上的串>

python -m app.bootstrap          # 建表 + 管理员 + 默认配置
uvicorn app.main:app --reload --port 8000
```

Worker 另开一个终端（本机跑需要真实 Redis）：

```bash
python -m app.workers.worker
```

---

## 数据库后端

三种后端全都支持，**改 `DATABASE_URL` 即可切换，代码不用动**：

| 后端 | URL 格式 | 适用场景 |
|---|---|---|
| **MySQL** | `mysql+aiomysql://user:pass@127.0.0.1:3306/bsum?charset=utf8mb4` | 宝塔用户、已有 MySQL |
| **Postgres** | `postgresql+asyncpg://user:pass@127.0.0.1:5432/bsum` | 官方推荐，`install.sh` 默认 |
| SQLite | `sqlite+aiosqlite:///./data/dev.db` | **仅本地开发**，多进程会锁冲突 |

同步连接 `DATABASE_URL_SYNC`（Alembic 迁移用）要配对应的同步驱动：
MySQL 用 `+pymysql`，Postgres 用 `+psycopg`，SQLite 去掉 `+aiosqlite`。

MySQL 注意事项：

- 字符集**必须 `utf8mb4`**（MySQL 的 `utf8` 只有 3 字节，emoji 会报错）
- 连接池已配 `pool_recycle=3600`，防 MySQL 默认 8 小时断连
- 列表搜索用 `ilike` 会生成 `lower() LIKE`，**用不上索引**。几千条以内无感

---

## 首次配置（后台管理端）

登录后台 `/admin`，在「密钥配置」里填：

| 配置项 | 说明 |
|---|---|
| **DeepSeek API Key** | 总结用的模型密钥。也可在「用户设置」里让每个用户自带 |
| **COS SecretId / SecretKey / 地域 / 存储桶** | 存封面与音频中转。**地域要与服务器一致** |
| **ASR SecretId / SecretKey / 地域** | 腾讯云语音识别 |
| **B站 Cookie** | 可选。用于解析需要登录的视频（如高码率、会员内容） |
| **HTTP 代理** | 可选。只有需要访问 YouTube 时才填 |

密钥在列表里一律**脱敏显示**（只显示前几位 + `****`），前端靠 `is_secret` / `is_set`
两个标记决定渲染成密码框还是普通输入框。

用户凭证（各自带得 DeepSeek Key / B站 Cookie）用 **Fernet 对称加密**后入库，
解密密钥来自 `SECRET_KEY`。换 `SECRET_KEY` 会导致已有密文全部解不开。

---

## 成本控制

腾讯云每月的免费额度基本能覆盖小规模自用：

| 项目 | 每月免费额度 |
|---|---|
| 录音文件识别 | 10 小时 |
| 录音文件识别极速版 | 5 小时 |
| 语音流异步识别 | 5 小时 |
| 实时语音识别 | 5 小时 |
| 一句话识别 | 5000 次 |

因此设计上做了这些取舍：

- **字幕优先** —— 大部分 B 站视频有 CC 字幕，直接拿，不消耗 ASR 额度
- **`ResTextFormat=3`** —— 按标点分段，适合字幕场景，**不额外收费**
  （`4` 语义分段、`5` 口语转书面语是增值付费项，已在配置里排除）
- **额度前置检查** —— 提交 ASR 前先比对 `asr_monthly_quota_sec`，
  超限直接拒绝，不会「跑完了才发现超额」
- **音频即用即删** —— 识别完立刻删 COS 音频，不留存储垃圾
- **Redis 存高频进度、Postgres 存终态** —— 避免每一条进度都写库

---

## 关键参数（沿用本地版调优结果，勿随意改动）

长文本总结采用分级 Map-Reduce：

| 文本长度 | 策略 |
|---|---|
| `< 12000` 字 | 直接一次总结 |
| `12000 ~ 20000` 字 | 两级：分段总结 → 汇总 |
| `> 20000` 字 | 三级：分段 → 分组汇总 → 最终汇总 |

- `chunk_size=6500`，`overlap=600`
- 温度：分段 `0.3` / 汇总 `0.4`
- 输出比例 `35%`，下限 1000 字，上限 8000 字

知识库检索是两层结构：

```
用户提问
   │
   ├─ 粗筛：BM25 + TF-IDF 混合（各 0.5 权重）→ Top 5 条内容
   │
   └─ 精查：对候选内容做字符级 ngram TF-IDF 余弦相似
            （char_wb ngram (2,4)，max_features=5000）
            → 得分低于最高分 10% 的剔除
```

---

## 目录结构

```
app/
├── main.py                 FastAPI 入口 + lifespan + 页面路由
├── bootstrap.py            容器启动引导（等库 → 建表 → 初始化）
├── core/
│   ├── config.py           pydantic-settings 配置
│   ├── constants.py        枚举：任务状态 / 阶段 / 类型
│   ├── security.py         口令哈希（bcrypt + SHA256 预哈希）、JWT、Fernet
│   ├── progress.py         Redis 进度读写
│   ├── queue.py            RQ 入队封装
│   └── exceptions.py       业务异常 → HTTP 状态码映射
├── db/
│   ├── models.py           ORM 模型
│   ├── session.py          引擎与会话（Postgres / SQLite 自适应）
│   └── settings_repo.py    系统配置读写（含密钥解密）
├── services/
│   ├── downloader.py       yt-dlp 封装：解析 / 字幕 / 音频 / 转码
│   ├── cos_service.py      腾讯云 COS 封装（惰性配置）
│   ├── asr_service.py      腾讯云 ASR：提交 + 轮询
│   ├── summarizer.py       DeepSeek 客户端 + Map-Reduce
│   └── retrieval.py        BM25 + TF-IDF 两层检索
├── workers/
│   ├── tasks.py            任务流水线（核心业务逻辑）
│   └── worker.py           RQ Worker 启动入口
├── api/
│   ├── deps.py             依赖注入：当前用户 / DB 会话
│   ├── schemas.py          请求响应模型
│   ├── user/               前台接口
│   │   ├── auth.py         登录 / 注册 / 邀请码
│   │   ├── content.py      提交任务 / SSE 进度 / 列表 / 删除
│   │   ├── chat.py         单内容问答 + 知识库问答
│   │   └── settings.py     个人设置 / 改密
│   └── admin/              后台接口
│       ├── settings.py     密钥配置
│       ├── users.py        用户 / 邀请码
│       └── data.py         数据管理 / 用量统计
├── static/
│   ├── common.css          共享样式（琥珀暖色系）
│   ├── common.js           SSE 订阅等公共逻辑
│   └── favicon.svg
└── templates/
    ├── login.html          登录 / 注册
    ├── index.html          前台工作台
    ├── knowledge.html      跨内容知识库问答
    └── admin.html          后台管理

tests/
├── verify.py               静态一致性校验
├── units.py                systemd unit 语法校验
├── mysql.py                MySQL 兼容性校验
├── css_admin.py            后台页面 CSS 回归（防中文标签竖排）
├── sse_fields.py           SSE 前后端字段名一致性校验
├── boot.py                 bootstrap 幂等性
├── smoke.py                HTTP 冒烟测试
└── e2e.py                  端到端流水线测试

deploy/
├── install.sh              裸机一键部署脚本（Postgres 版）
├── bsum-web.service        Web 服务 unit
├── bsum-worker.service     Worker 服务 unit
├── nginx.conf              反向代理配置（含 SSE 关缓冲）
├── run.sh                  前台调试启动（web / worker / bootstrap）
├── bt_start.sh             宝塔单项目启动（Web exec 接管 + Worker 后台）
├── bt_stop.sh              宝塔停止（含孤儿进程清理）
├── doctor.py               一键排障体检（配置/进程/任务/Redis/日志）
├── README.md               裸机部署指南
├── 宝塔部署.md              宝塔面板部署要点速查（MySQL 版）
├── 安装教程.md              从零开始的完整安装教程（宝塔 + MySQL，逐步验证）
└── 排障指南.md              报错去哪看：三处错误来源 + 按症状速查
```

---

## 已知的坑与对应处理

| 问题 | 处理方式 |
|---|---|
| **bcrypt 72 字节上限** | 先 SHA-256 + base64 压到固定 44 字节再哈希。不用 passlib（1.7.4 与 bcrypt 5.x 不兼容） |
| **RQ 的 `job_id` 参数名冲突** | 业务入队参数改叫 `task_id`。RQ 会把 `job_id` 当成自己的任务 ID（要求 `str`），传 `int` 直接抛 `TypeError` |
| **计数器 `+= 1` 撞 `None`** | 计数器列一律 `server_default=text("0")` + `nullable=False`，代码里再写 `(row.x or 0) + 1` 兜底。只写 `default=0` 时新对象在 flush 前属性是 `None` |
| **`_bootstrap()` 重复执行报唯一键冲突** | 一次性读出已有 key 集合，只插入缺失项，保证幂等（容器每次启动都会调用） |
| **COS 未配置导致列表接口 502** | COS 客户端惰性配置：构造不校验，只在真正上传/下载时才报错；`public_url` 未配置时返回空串，前端显示占位图 |
| **SSE 被反向代理缓冲** | 响应头带 `X-Accel-Buffering: no`；Nginx 侧也要 `proxy_buffering off` |
| **EventSource 无法自定义请求头** | token 通过 query param 传递（`/api/job/{id}/stream?token=...`） |
| **前端进度字段名与后端不一致** | 后端 `set_progress()` 写的是 `percent`，前端曾误读 `p.progress`，导致进度条静默失效。由 `tests/sse_fields.py` 守 |
| **`Container` 内 `e.data` 为 undefined 时吞掉真实错误** | 前端 SSE `error` 处理曾无条件把消息覆盖成「连接中断」。现在优先用服务端 `message`，没有才给出可操作提示 |
| **`.gitignore` 的 `_*.py` 会吃掉 `__init__.py`** | 加 `!__init__.py` / `!**/__init__.py` 反向规则。验证要查 `git ls-files`，不能只看本地文件 |
| **后台设置项中文标签竖排** | 局部 `width:250px` 打不过全局 `input[type=text]{width:100%}`，须显式 `width:auto` + 给标签区 `min-width` 兜底 |
| **Alembic 用异步 URL 跑 offline 模式** | `env.py` 里把 `+asyncpg` 转成 `+psycopg` 生成同步 URL |
| **systemd `ProtectHome` 让 yt-dlp 挂掉** | yt-dlp 默认写 `$HOME/.cache/yt-dlp`。开 `ProtectHome=true` 后 `$HOME` 不可访问，下载直接失败。必须同时 `Environment=HOME=/opt/bsum/data` + `XDG_CACHE_HOME` |
| **`rsync` 不是 Ubuntu 预装** | 最小化安装的 server 上没有 rsync。部署脚本里显式 `apt install rsync` |
| **Ubuntu 22.04 没有 python3.12** | 22.04 默认 3.10，3.12 不在默认源。部署脚本自动探测系统 `python3` 版本，不硬编码 |
| **`StartLimitBurst` 放在 `[Unit]` 会被静默忽略** | 这两个指令属于 `[Service]` 段。放错段 systemd 不报错也不生效 |
| **MySQL 的 `utf8` 只有 3 字节** | 必须用 `utf8mb4`，否则中文 emoji 报 `Incorrect string value`。URL 里要带 `?charset=utf8mb4` |
| **MySQL 默认 8 小时断连** | `wait_timeout` 到点会掐断空闲连接，aiomysql 报 `Lost connection`。连接池必须配 `pool_recycle=3600` |
| **MySQL 的 TEXT 不能做索引键** | 本项目的索引/唯一约束全在 `String(n)` 列上，没踩到。加新列时注意 |
| **SQLite 多进程写锁** | 只能开发用。Worker 与 Web 是两个进程，生产用会 `database is locked`。已开 WAL + `busy_timeout` 缓解，但不解决根本问题 |

---

## 测试

在项目根目录运行（只依赖 SQLite + fakeredis，**不需要** Postgres / Redis）：

```bash
python tests/verify.py    # 静态一致性：路由 / 模板引用 / 响应字段 / CSS 污染
python tests/units.py     # systemd unit 语法与配置配套检查
python tests/mysql.py     # MySQL 兼容性：DDL / 索引键长 / 查询方言
python tests/boot.py      # bootstrap 能否独立跑通 + 幂等性
python tests/smoke.py     # HTTP 冒烟：页面 / 鉴权 / 管理端 / 列表 / 入队（51 项）
python tests/e2e.py       # 端到端：入队 → 真 Worker 执行 → 校验落库（20 项）
```

`e2e.py` 是覆盖度最高的一个：它真的起一个 RQ `SimpleWorker` 把任务跑完，
校验任务状态、进度、总结落库、用量记账，以及**失败路径**是否正确写成 `error`。

生产环境不需要 `aiosqlite` / `fakeredis`，属于开发依赖。

---

## 与本地版的关系

本地版 `C:\Game\bilibili-summary` **保持不变**，仍可独立运行。
本项目是完全独立的新项目，不改动原仓库。

功能上做了这些差异：

| 维度 | 本地版 | 云端版 |
|---|---|---|
| 转写 | sherpa-onnx SenseVoice 本地推理 | 字幕优先 + 腾讯云 ASR 兜底 |
| 存储 | 本地 `data.db` + 本地目录 | Postgres + COS |
| 鉴权 | 无 | JWT + 邀请制 |
| 密钥 | `~/.bilibili-summary-key` 单份 | 加密入库，多用户各自一份 |
| 长任务 | 直接跑在请求线程 | Redis + RQ 队列，独立 Worker |
| 并发限制 | 进程内计数 | 用户维度槽位 + Worker 池 |
| ffmpeg | 硬编码 Windows 路径 | 容器内 `ffmpeg`，或 `FFMPEG_PATH` 指定 |

### 已从本地版对齐的能力

这几项本地版有、云端版早期漏掉，现已补齐（实现与提示词均照搬本地版）：

| 能力 | 说明 |
|---|---|
| 流式显字 | Worker 走 `summarize_stream_collect`，正文增量写 Redis，SSE 通过 `summary` 事件下发，前端边生成边渲染 |
| 重新总结 | `POST /api/videos/{id}/resummarize`、`POST /api/articles/{id}/resummarize`：复用已有转写/正文，只重跑 AI |
| 下载报错翻译 | `friendly_download_error()` 把 yt-dlp 原始输出翻成「需要大会员 / 已失效 / 地区限制 / 需要登录 / 触发风控」 |
| 文章标题 | `app/core/titling.py`：扫全行、去 Markdown 标记、超 30 字在标点处截断 |
| Cookie 反馈 | 保存时跳过空值项并回报有效条数，设置页显示「N 字符 · M 条」 |
| 朗读 | `POST /api/tts`（Edge TTS）生成 MP3，音色/语速可在个人设置里选 |
| 限流 | 按 IP 的写操作配额（默认 60 次/分，登录与 TTS 10 次/分） |

### 有意偏离本地版的地方

| 位置 | 本地版 | 云端版 | 原因 |
|---|---|---|---|
| `_PREAMBLE_PATTERNS` 第 4、5 条 | `[总结\|结构化总结]` | `(?:总结\|结构化总结)` | 本地版把分组误写成字符集，导致最常见的开场白「以下是对该视频的总结：」永远剥不掉；其余 6 条逐字一致 |
| 知识库原文精查未命中 | 跳过该来源 | 用该内容的总结兜底 | 本地版会返回「没有找到相关内容」，但云端是多人共用知识库，粗筛已命中却答不出来体验更差；10% 相关度阈值与 `=====` 分隔符保持本地版行为 |
| 三份总结提示词 | 无「内容取舍」段 | 增加「广告/赞助/带货推广不纳入总结」 | 本地版会把口播广告原样总结（实测同一份转写，旧提示词产出的总结里出现 `爱回收 / 严选 / 补贴 / 验机 / 低价专区` 等词，甚至单开「中插广告」章节）。原有内容要求逐字保留 |
| `SUMMARY_PROMPT` / `FINAL_SUMMARY_PROMPT` | 无排版要求 | 增加「Markdown 排版要求」段落 | 原有内容要求逐字保留（把该段整块删掉即与本地版完全一致）。本地版靠模型自觉输出标记，云端用的 `deepseek-v4-flash` 不会主动加粗、不加小标题，观感差别明显 |
| `summary_max_tokens()` | `word_limit * 4` | 额外 +4096 推理余量 | 本地版按非推理模型定预算；`deepseek-v4-flash` 会先花 4000+ token 思考，上限 4000 时正文只剩 0~500 字且排版要求全部失效。非推理模型不受影响（会提前 `finish=stop`） |

另外修掉两个本地版同样存在的 bug：

| bug | 表现 |
|---|---|
| `_strip_preamble_stream` 中途刷出缓冲区时用 `strip_preamble()`（含 rstrip），吃掉了缓冲区末尾的换行 | 第一个小标题与正文挤成一行（`## 事件缘起浙江绍兴一位…`），只有第一次刷新会踩到，后续片段原样透传 |
| SSE 结束帧只看 Redis 的 `mark_done`，不带 `video_id` | 生成完成后前端定位不到结果：标题卡在「AI 正在生成…」、闪烁光标不消失。现在结束帧统一回数据库取终态并带 `video_id/article_id`，失败改发 `error` 事件（原先发 `end{stage:'error'}`，前端一律当成功） |

---

## 本机部署记录（宝塔 AI 项目）

- 域名：`bsum.lexuan.love`（HTTP；HTTPS 证书待申请）
- 面板项目：AI 项目 `bsum`（id=17），运行用户 `www`，主端口 `8000`
- 启动/停止脚本：`deploy/bt_start.sh` / `deploy/bt_stop.sh`
  > 宝塔以「启动命令自身的 PID」判定项目是否运行，故 Web 用 `exec` 前台接管
  > （PID 即 uvicorn，端口检测对得上），Worker 作为独立后台进程。
- Nginx：`/www/server/panel/vhost/nginx/ai_bsum.conf`（反代 `127.0.0.1:8000`，含 SSE 关缓冲）
- 数据库：MySQL `bsum`（utf8mb4，用户 `bsum`，仅 127.0.0.1）；Redis：`127.0.0.1:6379`
- 运行配置：`.env`（权限 600，已含随机 `SECRET_KEY` / `JWT_SECRET` / `ADMIN_PASSWORD`）
- 日志：`logs/web.log`、`logs/worker.log`、`logs/app.log`、`logs/bootstrap.log`（面板「日志」按钮已指向前三个）
- 运行时 PID：`.run/`（Web/Worker 的 pid 文件，供 `bt_stop.sh` 收尾；`.aiproject/bsum.pid` 由宝塔自己维护）
- 依赖：`.venv`（基于 Python 3.12.13，面板运行时 `/www/server/pyporject_evn/versions/3.12.13`；
  venv 内 `pip.conf` 已指向腾讯云镜像 `mirrors.cloud.tencent.com`，官方源在本机仅约 60 KB/s）
  > 3.10 → 3.12 升级后旧环境备份 `.venv310.bak` 已在验证通过（服务、依赖、yt-dlp 均正常）后清理，
  > 回滚只能靠重建：`bt_stop.sh` → `rm -rf .venv && python3 -m venv .venv` → 装依赖 → `bt_start.sh`

> 维护：改代码后在面板重启 `bsum` 项目；依赖变更跑 `.venv/bin/pip install -r requirements.txt`；
> 表结构变更跑 `.venv/bin/python -m app.bootstrap`（幂等）。
> 注：8 个 `__init__.py` 已按上游 `main` 分支补齐（根因：`.gitignore` 的 `_*.py` glob 误把 `__init__.py` 排除了，上游已修复）。
> 更新代码：`cd /www/wwwroot/bilibili-summary-cloud && git pull`。本仓库已设 `pull.rebase=true` + `rebase.autostash=true`，
> 拉取时会自动暂存并恢复本段本地备注；另设了 `core.fileMode=false`，忽略面板部署造成的工作区权限位漂移（否则 git 会把 55 个文件误判为已修改而拒绝合并）。
> 本机 `deploy/` 脚本与上游 `main` 已完全一致（此前提到的 `bt_start.sh` stdout 重定向差异已不存在）。
