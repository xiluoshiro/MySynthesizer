# MySynthesizer 合成器研究工作区

这个目录用于保存 MySynthesizer 合成路径数据、合成器机制分析，以及后续复刻合成器的设计资料。

当前目标是一个本地单机合成器工作台：读取已有图谱，导入 SQLite，执行 `craft()` 合成流程，并提供 CLI、本地静态 UI 和后续 exe 打包路线。

## 当前进展

已完成：

- Python 包结构和核心数据模型。
- SQLite 本地存储，覆盖对象、原始 payload、路线边、recipe cache、合成事件、失败记录。
- 第一版规则合成 pipeline：校验、缓存、特征抽取、意图判断、候选生成、召回、评分、决策、持久化。
- CLI：`init`、`reset`、`craft`、`eval`、`workbench`、`embed objects/recipes`、`review promote/reject/merge`。
- 评估与测试看护：根目录 `scripts/run_tests.py` 可一键运行语法检查和单元测试。
- 向量化实验层：embedding 文本构造、fake provider、SQLite sidecar 表、去重、stale 标记、本地 vector top-k 召回；craft 默认 auto on，缺少 provider/index 或 active embedding 时自动跳过。
- LLM 候选生成第一版：OpenAI-compatible chat completions，可用环境变量配置，默认 auto on，只生成候选并进入现有 ranker/pending 流程。
- 质量治理基础闭环：active-only 在线召回、`created_pending`、`merged_existing`、`object_aliases`、disabled route、pending 的 promote/reject/merge 审核命令。
- 本地 workbench：标准库 loopback HTTP + `ui/` 静态页面，可搜索对象、查看对象详情、执行合成、结构化展示结果、审核 pending 和一键还原。
- PyInstaller 打包脚本：`scripts/build_desktop.py`，已验证真实构建输出 `dist/MySynthesizer/`。

进行中：

- 单机 UI 仍是第一版，尚未做批量操作、筛选排序和更完整的审核工作流。
- 质量治理仍是最小闭环，尚未实现质量评分、审核记录表和批量维护命令。
- 真实向量模型、ANN 向量库和 LLM rerank/质量审核仍是远期实验，不是近期主线。

建议下一步是继续完善 pending 审核记录和批量治理，并考虑给 exe 增加托盘/窗口壳。

## 当前入口

- `docs/design/mysynthesizer-synthesizer-design.md`：合成器机制、正向模型、AI 操作规则的统一设计文档。
- `docs/design/synthesizer-engine-implementation.md`：Python 后端合成器引擎的数据模型、pipeline、SQLite 轻量存储、向量化、质量治理和评估设计。
- `outputs/data/current/`：当前最新可用合成数据。

## 重要文件

- `docs/design/mysynthesizer-synthesizer-design.md`  
  合并后的权威设计入口，包含机制分析、正向合成流程、匹配/新建判断、type 统计、专名化策略和后续 AI 工作规范。

- `docs/design/synthesizer-engine-implementation.md`  
  面向最终 Python 后端实现的工程设计入口，定义合成器引擎边界、对象模型、SQLite 轻量存储、向量化、只增图治理、质量治理、候选生成、检索评分、命中/新建决策和回放评估方法。

- `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`  
  当前最新完整图谱。最近一次刷新为 `1355 objects / 3100 craft_sources`。

- `outputs/data/current/mysynthesizer_route_summary.md`  
  最新扫描摘要，包含新增对象与新增路线概览。

## 工程结构

```text
mysynth/
  models.py       Pydantic 数据模型：SynthObject、CraftRequest、CraftResult 等
  store.py        SQLiteObjectStore：导入图谱、对象查询、recipe cache、结果持久化
  engine.py       RuleSynthesizerEngine：核心 craft pipeline
  candidate_generators.py  规则/LLM/组合候选生成器
  candidates.py   第一版规则候选生成
  ranking.py      候选与已有对象评分
  features.py     规则特征抽取
  intent.py       合成意图判断
  normalize.py    名称、文本、recipe key 标准化
  embeddings.py   embedding 文本、provider 接口、SQLite sidecar 和实验 vector 召回
  workbench.py    本地 loopback HTTP + 静态 UI 托管入口
  evaluation.py   route_edges 回放评估
  cli.py          命令行入口
```

运行产物：

- `data/engine/mysynth.db`：本地 SQLite 数据库，由 `init` 命令生成，已被 git 忽略。
- `outputs/data/current/`：当前图谱数据源，已被 git 忽略但本地可用。

## 使用方法

环境要求：

- Python `>= 3.12`
- Pydantic `>= 2.0`

初始化本地 SQLite。默认只保留初始四元素 `1=水 / 2=火 / 3=土 / 4=风`：

```bash
python -B -m mysynth init --force
```

导入完整图谱用于开发和回放评估：

```bash
python -B -m mysynth init --force --full
```

单次合成：

```bash
python -B -m mysynth craft --a 2 --b 3396 --operation add --no-persist
```

示例结果：`火 + 氢气 -> 水`，如果 recipe cache 已有记录，会直接命中已有对象。

启动本地工作台：

```bash
python -B -m mysynth workbench --port 8765
```

打开 `http://127.0.0.1:8765/` 后可搜索对象、查看对象详情、选择 A/B、执行合成、查看结构化结果和 top matches，并审核 pending 对象。

还原到初始四元素：

```bash
python -B -m mysynth reset --yes
```

还原会删除除 `1/2/3/4` 外的所有对象和全部路线、recipe cache、事件、失败记录、alias、embedding 派生数据。UI 右侧维护区也提供同样的“还原到初始四元素”按钮。

回放评估：

```bash
python -B -m mysynth eval --limit 20 --failures 10
```

评估输出包含总体命中率、按 `operation/type` 分桶的指标，以及未精确命中的失败样本。

生成本地 fake embedding：

```bash
python -B -m mysynth embed objects --limit 100
python -B -m mysynth embed recipes --limit 100
```

当前 embedding 命令使用确定性的 `fake-hash-v1` provider，用 SQLite sidecar 做本地 brute-force top-k；真实向量模型和 ANN 向量库后续接入。

说明：fake embedding 只是测试替身，没有真实语义能力；craft 默认进入 vector auto 模式，只有存在可用 provider/index 和 active embedding 时才会把 vector top-k 作为候选证据。开发排障时可以关闭：

```bash
python -B -m mysynth craft --a 2 --b 3 --operation add --no-vectors
```

配置 LLM 候选生成：

```bash
$env:MYSYNTH_LLM_BASE_URL="https://api.openai.com/v1"
$env:MYSYNTH_LLM_API_KEY="your_api_key"
$env:MYSYNTH_LLM_MODEL="your_model"
python -B -m mysynth craft --a 2 --b 4 --operation subtract
```

LLM 默认启用，只生成结构化候选，不会绕过 `recipe_cache`、direct route、ranker 或 pending 审核。未配置 key/model 或接口失败时会自动回退到规则候选。Workbench 不提供 LLM 开关；开发排障时 CLI 可使用 `--no-llm`。

审核 pending 对象：

```bash
python -B -m mysynth review promote --id -1
python -B -m mysynth review reject --id -1 --reason bad_name
python -B -m mysynth review merge --id -1 --canonical-id 1
```

`promote` 会把 pending 对象、disabled route 和 recipe cache 激活；`reject` 保持对象和路线隔离；`merge` 会把 pending 对象归并到 canonical 对象，并写入 alias、route 和 recipe cache。

运行全部测试：

```bash
python -B scripts/run_tests.py
```

检查桌面打包计划：

```bash
python -B scripts/build_desktop.py --dry-run
```

构建桌面包：

```bash
python -B scripts/build_desktop.py
```

构建后输出：

```text
dist/MySynthesizer/
  MySynthesizer.exe
  data/engine/mysynth.db
  ui/
  _internal/
```

说明：

- 当前 CLI 默认读取 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
- 当前主存储是 SQLite，不需要外部数据库服务。
- `init` 默认生成四元素初始库；`init --full` 才导入完整图谱。
- `--no-persist` 用于只看结果、不写入本地 craft 记录。
- craft 默认使用 LLM/vector auto 路径；`--no-llm`、`--no-vectors` 只用于开发排障。
- `embed objects/recipes` 会写入 SQLite 的 embedding sidecar 表，不调用外部模型。
- vector 召回只作为候选证据，不会覆盖 `recipe_cache` 或 direct route 的确定结果。
- 未命中确定结果的新合成默认进入 `pending`，不会参与 active 召回；需要通过 `review` 命令审核。
- `scripts/run_tests.py` 是统一测试入口，当前包含语法检查和 `unittest` 发现，后续测试套件继续挂到这里。
- 在当前 Windows 环境里建议使用 `python -B`，避免写 `__pycache__` 时触发权限问题。

## 后续新会话建议

1. 先读本 README。
2. 如果要理解合成器机制或规划后续合成路线，读 `docs/design/mysynthesizer-synthesizer-design.md`。
3. 如果要实现本地合成器引擎，读 `docs/design/synthesizer-engine-implementation.md`。
4. 如果要做证据核查，读 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
5. 如果用户说“又合成了新东西”或要求刷新当前 mine，调用 `mysynthesizer-route-scanner` skill，并用最新 cookie 重新扫描。

## 数据整理说明

本次整理完成了分类移动，并删除了确认无用或已被最新数据覆盖的文件：

- 合成器设计文档移动到 `docs/design/`。
- 三份旧设计文档已合并为 `docs/design/mysynthesizer-synthesizer-design.md`，旧稿已删除。
- 最新完整图谱和扫描摘要移动到 `outputs/data/current/`。
- 临时脚本、截图和 skill 测试输出已删除。
- 与 `mysynthesizer_mine_full_routes_latest.json` 内容完全相同的旧命名副本已删除，只保留 `*_latest` 入口。
- `outputs/data/archive/` 中的历史对象 key 已确认全部被 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json` 覆盖，archive 已删除。
