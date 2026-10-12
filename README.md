# InsightFlow · 后端 V0.1

> **最新独立验收（2026-10-12）：FAIL。** 已完成pgvector/核心修复，SQLite161通过/5跳过、PostgreSQL166通过、Ruff通过；真实分类/复核有完成样本但仍有结构失败，真实语义Embedding尚未配置。Git目录未建立。以[Phase 2独立验收报告](docs/17-Phase2独立验收与修复报告.md)为当前结论，下面开发交付数字保留为历史；STOP，禁止Phase3。

> **2026-10-12 Phase 2 — Embedding + Semantic Clustering 技术交付完成。** 26 项 Phase 2 测试通过；全量回归 118 passed / 3 skipped（PG-only；PG 上 121 passed）；Ruff 通过；真实 PG 16.2 迁移 upgrade→downgrade→reupgrade 通过；真实 MiniMax-M3 端到端 12/12 分类 + 10 簇（9 LLM 命名 + 1 OUTLIER）。完整报告见 [Phase 2 交付报告](docs/16-Phase2交付报告.md)。等待三文鱼亲自复测并明确批准；在此之前：禁止开始 Phase 3；禁止修改 Raw feedback / Phase 1 已有测试 / Phase 1 迁移 / Phase 1 prompt。

AI 产品需求洞察 Agent：让原始反馈最终成为可追溯的产品洞察与需求建议。

本次已完成完整讨论归档、FastAPI 项目骨架、PostgreSQL 项目/原始反馈模型、Alembic迁移、CSV导入与查询、Phase 1 分类（26 项 Phase 1+修复测试）、Phase 2 嵌入与聚类（26 项 Phase 2 测试）。**尚未接入 Insight / Evidence / Opportunity / Frontend / PRD / MCP / Multi-Agent；完整 V1 目标不等于本次已实现能力。**

项目位置：`/Users/salmon/Desktop/InsightFlow`。

## 已实现能力

- 创建项目、项目详情与列表。
- CSV UTF-8/BOM、中文/引号/多行内容解析与分页查询。
- 原始反馈按项目隔离；同ID同内容重复导入跳过；不同内容拒绝并回滚整批。
- 缺列、空内容、错误日期、重复表头、畸形CSV、文件/行数上限处理。
- 进程及数据库就绪检查、Swagger交互接口文档。
- **Phase 1 — AI Feedback Classification**：环境变量配置密钥与模型、Structured Output 批量分类服务、五状态 Run、严格 schema 校验、按批独立 commit、原子 Retry 抢占、Token/Latency 真实累加、Raw 原文不变。
- **Phase 2 — Embedding + Semantic Clustering**：local/remote 嵌入、层次聚类（cosine + average/complete/single linkage）、LLM 语义复核（KEEP/SPLIT/MERGE/OUTLIER）、7 个聚类 HTTP 端点、阈值可配置。
- 26+26 = 52 项新测试；真实 PostgreSQL 16.2 迁移、回退再升级与接口验证。

## 项目目录

```text
InsightFlow/
├── README.md
├── compose.yaml                 # 可选 PostgreSQL 17 容器
├── docs/
│   ├── 01-产品定位与项目说明.md
│   ├── 02-V1-PRD.md              # 完整基准PRD
│   ├── 03-Sprint1方案.md         # 完整Sprint1原方案
│   ├── 04-Agent与Prompt设计.md
│   ├── 05-数据库与API设计.md
│   ├── 06-开发路线图.md
│   ├── 07-Demo数据集设计.md
│   ├── 08-决策记录与修正.md
│   ├── 09-验证与交付记录.md
│   ├── archive/                 # 全部5轮讨论 Markdown + JSON
│   └── superpowers/plans/       # 本次实施计划
├── backend/
│   ├── pyproject.toml / uv.lock
│   ├── .python-version / .env.example
│   ├── app/
│   │   ├── main.py
│   │   ├── api/                 # projects、feedback；analysis/clusters仅预留
│   │   ├── models/              # project、feedback；后续模型位置
│   │   ├── schemas/ / db/
│   │   ├── services/            # CSV解析和事务导入；AI服务仅预留
│   │   ├── agents/prompts/      # 分类原文及复核/洞察草案
│   │   └── evals/               # 后续评测位置
│   ├── alembic/                 # 固定版本迁移0001
│   ├── scripts/                 # 本机启动、临时PostgreSQL验证
│   └── tests/test_api.py
├── frontend/                    # app/components/lib目录，尚无前端应用
├── data/                        # 12条演示反馈与初始参考标签
└── work/                        # 本机运行生成的独立数据库，不纳入版本控制
```

## 本机最快运行（已验证，无需Docker）

这台Mac已有uv，项目固定使用Python 3.12。首次运行会下载项目依赖和本机开发用PostgreSQL二进制包。

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv sync --locked
# 首次使用或pgserver缓存重建后，为该运行时安装pgvector（需编译工具与网络）
uv run --with pgserver==0.1.4 python scripts/install_pgvector_local.py
uv run --with pgserver==0.1.4 python scripts/dev.py
```

打开 [交互API文档](http://127.0.0.1:8000/docs)。终端按Ctrl+C停止。数据库保存在项目`work/local-postgres`，重启会保留数据；不会复用系统PostgreSQL。

此入口自动创建独立数据库、执行迁移并启动FastAPI；它使用独立连接，**不会读取.env中的数据库地址作为启动目标**。后端业务本身仍使用标准PostgreSQL和SQLAlchemy，pgserver仅是本机开发启动辅助。该二进制包在本机提供PostgreSQL16.2；正式部署建议使用维护中的PostgreSQL容器或托管服务。

### 三步验证

1. 在`POST /projects`点Try it out，提交：
   `{"name":"AI Chat反馈分析","product_name":"AI Chat Assistant","description":"演示项目"}`。
2. 复制返回的id，在`POST /projects/{project_id}/feedback/import`填入并上传项目`data/demo_feedback.csv`。
3. 调用`GET /projects/{project_id}/feedback`，应返回12条总数；再次导入同文件，imported=0、skipped=12。

## Docker / 外部PostgreSQL运行（可选）

需要Docker Desktop或已运行的PostgreSQL；本机当前未安装Docker，因此Compose启动未实测。无需同时启动上面的本机入口。

```bash
cd /Users/salmon/Desktop/InsightFlow
# 启动本项目数据库，端口5433，仅监听本机
# 如已有独立PostgreSQL，可跳过此命令并自行配置DATABASE_URL
docker compose up -d db
cd backend
uv sync --locked
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

默认连接：`postgresql+psycopg://insightflow:insightflow_local@localhost:5433/insightflow`。
示例凭证仅用于本地开发；.env已加入忽略规则。使用已有数据库时先把.env中的DATABASE_URL改为自己的开发库。迁移会创建两张业务表，不应指向不相关项目的库。

`docker compose stop db`停止容器并保留数据；不要使用带`-v`的删除命令，除非确实要清空数据。

## 自动验证

在backend目录：

```bash
uv run pytest -q
uv run ruff check app tests alembic scripts
uv run --with pgserver==0.1.4 python scripts/verify_postgres.py
```

普通测试用临时SQLite作快速API回归；第三个命令创建独立临时PostgreSQL，验证完整15项测试、模型/迁移一致性，以及迁移降级再升级，完成后停止并清理临时库。具体实测见docs/09。

## 数据与范围说明

CSV标准列：feedback_id、content必填；source、user_id、user_type、created_at可选。仅接收标准列、UTF-8、10MiB以内和10000条以内的数据。created_at无时区按UTC，输出时间统一UTC；content原文保留。

本次只有12条模拟样本，初始参考标签未经人工验收。原讨论500条主题配额实际为492，修正建议已写入docs/07；**没有虚构完整500条数据集或模型评测成绩**。

下一步是完成正式评测数据、Insight / Evidence / Opportunity 生成、前端 / PRD 等。详见docs/06、docs/16-Phase2交付报告.md。Phase 2 之后等待三文鱼复测与明确批准才能开始 Phase 3。

## 开发规范入口

开发前阅读[技术开发规范与 Git 规则](技术开发规范与Git规则.md)，并结合[工作记录](工作记录.md)和[Agent 交接入口](AGENT_HANDOFF.md)确认当前阶段。本文规范不代表Git或CI已配置。


## 当前Phase 2复测入口（2026-10-12）

迁移head=0007，现有数据库前向升级会将JSON向量转为pgvector；请先备份本项目开发数据。Docker选项改为pgvector/pgvector:pg17，尚未实测。

使用安全配置的真实Embedding后运行：

```bash
cd /Users/salmon/Desktop/InsightFlow/backend
uv run --with pgserver==0.1.4 python scripts/verify_phase2_http.py
uv run --with pgserver==0.1.4 python scripts/verify_phase2_migration.py
```

脚本创建并清理自己的临时数据库，不降级已有库。8000已占用时HTTP脚本改用18085。真实Embedding要求报告embedding_mode=remote；默认local-hash-v1仅作测试。

成功计数是反馈数；最终簇数需排除空审计父簇。completed Retry为无副作用读取；重新评估创建新Run。旧无配置快照Run不可Retry。完整复测和限制见docs/17。
