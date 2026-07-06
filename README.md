# MySynthesizer 合成器研究工作区

这个目录用于保存 MySynthesizer 合成路径数据、合成器机制分析，以及后续复刻合成器的设计资料。

当前已开始实现一个本地 Python 合成器引擎原型：读取已有图谱，导入 SQLite，执行 `craft()` 合成流程，并提供 CLI 做单次合成和 route 回放评估。

## 当前入口

- `docs/design/mysynthesizer-synthesizer-design.md`：合成器机制、正向模型、AI 操作规则的统一设计文档。
- `docs/design/synthesizer-engine-implementation.md`：Python 后端合成器引擎的数据模型、pipeline、SQLite 轻量存储和评估设计。
- `outputs/data/current/`：当前最新可用合成数据。

## 重要文件

- `docs/design/mysynthesizer-synthesizer-design.md`  
  合并后的权威设计入口，包含机制分析、正向合成流程、匹配/新建判断、type 统计、专名化策略和后续 AI 工作规范。

- `docs/design/synthesizer-engine-implementation.md`  
  面向最终 Python 后端实现的工程设计入口，定义合成器引擎边界、对象模型、SQLite 轻量存储、候选生成、检索评分、命中/新建决策和回放评估方法。

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
  candidates.py   第一版规则候选生成
  ranking.py      候选与已有对象评分
  features.py     规则特征抽取
  intent.py       合成意图判断
  normalize.py    名称、文本、recipe key 标准化
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

初始化本地 SQLite：

```bash
python -B -m mysynth init --force
```

单次合成：

```bash
python -B -m mysynth craft --a 2 --b 3396 --operation add --no-persist
```

示例结果：`火 + 氢气 -> 水`，如果 recipe cache 已有记录，会直接命中已有对象。

回放评估：

```bash
python -B -m mysynth eval --limit 20 --failures 10
```

评估输出包含总体命中率、按 `operation/type` 分桶的指标，以及未精确命中的失败样本。

运行全部测试：

```bash
python -B scripts/run_tests.py
```

说明：

- 当前 CLI 默认读取 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
- 当前主存储是 SQLite，不需要外部数据库服务。
- `--no-persist` 用于只看结果、不写入本地 craft 记录。
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
