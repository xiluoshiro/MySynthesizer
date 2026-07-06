# MySynthesizer 合成器引擎实现设计

## 1. 目标

最终目标是写一个本地可运行的合成器引擎：

```text
输入：两个待合成对象 + operation(add/subtract)
输出：合成后的对象 + 命中/新建判断 + 可解释过程
```

后端使用 Python 实现。这个引擎不是简单调用前端 `POST /api/craft` 的封装，而是把当前设计文档中反推出来的合成流程工程化，形成一套可测试、可调参、可替换模型的完整 pipeline。

第一版目标是行为相似，而不是完全复刻线上后端。真实 prompt、检索算法和阈值不可见，因此实现应保留可观察证据、评分明细和调参入口。

## 1.1 当前实现范围

当前阶段只实现核心合成器引擎，不做线上 API 适配和 FastAPI 服务层。

本阶段交付物：

- Python 包内的数据模型和引擎模块。
- 可从本地图谱加载对象、路线和 recipe cache 的轻量 SQLite 存储层。
- `craft()` 核心流程：校验、标准化、特征抽取、候选生成、召回、评分、决策、保存结果。
- CLI 或脚本级入口，用于单次合成和 route 回放评估。
- 可解释日志和评估报告。

明确后置：

- 前端 API 兼容层。
- 线上 `GET /api/objects/{id}` adapter。
- FastAPI/HTTP 服务。
- 真实数据库、用户系统、权限和 cookie 处理。

## 2. 参考接口

线上前端合成接口：

```http
POST /api/craft
Content-Type: application/json
Cookie: hc_session=...
```

请求体：

```json
{
  "ingredient_ids": [1, 35],
  "operation": "add"
}
```

响应体核心结构：

```json
{
  "success": true,
  "result": {
    "id": 252,
    "name": "水龙卷",
    "emoji": "🌪️",
    "type": "element",
    "description": null,
    "character_summary": null,
    "background": null,
    "specialty": null,
    "weakness": null,
    "source": "llm",
    "is_banned": false,
    "ban_reason": null,
    "banned_at": null,
    "banned_by_user_id": null,
    "first_discoverer_id": 17,
    "first_discoverer_nickname": "Gloaming",
    "created_at": "2026-06-30T11:04:56.139361Z",
    "discovered_at": "2026-07-06T16:04:26.843809Z",
    "discovery_method": "recipe_cache",
    "is_first_discoverer": false,
    "category_ids": []
  },
  "failure_reason": null,
  "cached": true,
  "new_discovery": true,
  "first_discovery": false,
  "plaza_craft_remaining_today": null
}
```

对象详情接口：

```http
GET /api/objects/{id}
```

本地图谱中的对象字段与该响应基本一致，并可能额外内联 `craft_sources`。因此引擎内部应以完整对象为输入，而不是以 id 为输入。id 查询只属于外部 adapter。

后续做 API 适配时，可以把接口拆成两层：

- `POST /api/craft`：核心合成接口，接收两个完整对象。
- `POST /api/craft/by-ids`：兼容接口，接收 id，由后端 adapter 取对象详情后再调用核心引擎。

当前阶段不实现这两层 HTTP 接口，只保留它们作为未来适配方向。

## 3. 系统边界

### 3.1 外部 API Adapter

职责：

- 根据 `ingredient_ids` 拉取对象详情。
- 把线上对象转换为引擎内部 `SynthObject`。
- 可选：把本地引擎结果转换回前端类似响应格式。
- 不在 adapter 内做合成推理。

### 3.2 Core Engine

职责：

- 接收两个 `SynthObject` 和一个 `operation`。
- 执行标准化、特征抽取、候选生成、检索、评分、决策和结果构造。
- 返回结构化 `CraftResult`。

### 3.3 Object Store

职责：

- 提供已有对象库查询。
- 提供 route/craft_sources 查询。
- 支持从 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json` 加载离线图谱。
- 支持把本地新合成结果保存到嵌入式 SQLite 数据库。
- 通过 `ObjectStore` 接口隔离持久化实现，后续可替换为 Postgres、MongoDB、向量库或线上 API。

### 3.4 当前轻量存储方案

当前工程不依赖外部数据库服务。默认采用 SQLite：它是单文件嵌入式数据库，不需要单独部署，但提供事务、唯一约束、索引和结构化查询，适合当前轻量工程。

JSON/JSONL 仍保留为导入导出和审计格式：

- 从 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json` 导入基础图谱。
- 可导出 `objects.json` 方便人工检查和版本归档。
- 可选写入 `craft_events.jsonl` 作为追加审计日志，记录每次本地 craft 的输入、输出和评分。

推荐目录：

```text
data/
  engine/
    mysynth.db
    exports/
      objects.json
      route_edges.jsonl
    logs/
      craft_events.jsonl
      failures.jsonl
    meta.json
```

SQLite 表职责：

- `objects`：对象主表，保存完整 `SynthObject` 的稳定字段。
- `object_payloads`：对象原始 JSON 载荷，用于保留未知字段和兼容线上结构。
- `route_edges`：合成边，记录 `a_id/b_id/operation/result_id`。
- `recipe_cache`：规范化 recipe key 到 result id 的缓存。
- `craft_events`：本地每次 craft 的输入、候选、评分、决策和输出。
- `failures`：失败合成和异常输入，用于调试。
- `meta.json`：当前数据版本、下一个本地 id、生成时间、来源图谱路径。

写入策略：

1. 首次启动时从 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json` 初始化 SQLite。
2. 后续启动优先读取 `data/engine/mysynth.db`，除非显式执行重新导入。
3. 新建对象时分配本地负 id，避免和线上正整数 id 冲突。
4. 每次本地 craft 成功后，在一个事务内写入 `objects`、`route_edges`、`recipe_cache` 和 `craft_events`。
5. craft 失败时写入 `failures`，并保留足够上下文用于复现。
6. 导出 JSON/JSONL 时使用快照，不作为主写入路径。

内存索引：

- `objects_by_id: dict[int | str, SynthObject]`
- `name_index: dict[str, set[id]]`
- `token_index: dict[str, set[id]]`
- `type_index: dict[SynthType, set[id]]`
- `recipe_index: dict[RecipeKey, id]`
- `neighbors_by_object: dict[id, set[id]]`

SQLite schema 应保持窄表 + JSON payload 的折中：

```text
objects(id, name, normalized_name, type, description, source, is_banned, created_at, discovered_at)
object_payloads(object_id, payload_json)
route_edges(id, a_id, b_id, operation, result_id, source)
recipe_cache(recipe_key, a_id, b_id, operation, result_id, created_at)
craft_events(id, request_json, result_json, score_json, created_at)
failures(id, request_json, reason, context_json, created_at)
```

迁移原则：

- `ObjectStore` 接口不能暴露 SQLite 细节。
- 业务逻辑只依赖 `ObjectStore`，不直接写 SQL。
- SQLite 实现命名为 `SQLiteObjectStore`。
- 如果后续换 Postgres/MongoDB/向量库，只新增 store 实现，不改 engine pipeline。
- embedding 或全文检索可以作为 side index 接入，不替代主对象库。

## 4. Python 数据模型

### 4.1 SynthObject

```python
from typing import Literal

from pydantic import BaseModel, Field


SynthType = Literal["element", "item", "equipment", "creature", "concept"]
Operation = Literal["add", "subtract"]
Decision = Literal["matched_existing", "created_new", "failed"]


class SynthObject(BaseModel):
    id: int | None = None
    name: str
    emoji: str | None = None
    type: SynthType
    description: str | None = None
    character_summary: str | None = None
    background: str | None = None
    specialty: str | None = None
    weakness: str | None = None
    source: str | None = None
    is_banned: bool = False
    ban_reason: str | None = None
    banned_at: str | None = None
    banned_by_user_id: int | None = None
    first_discoverer_id: int | None = None
    first_discoverer_nickname: str | None = None
    created_at: str | None = None
    discovered_at: str | None = None
    discovery_method: str | None = None
    is_first_discoverer: bool = False
    category_ids: list[int] = Field(default_factory=list)
```

### 4.2 CraftRequest

```python
class CraftOptions(BaseModel):
    allow_banned: bool = False
    match_threshold: float = 0.78
    review_threshold: float = 0.62
    max_candidates: int = 10
    use_llm: bool = False
    explain: bool = True


class CraftRequest(BaseModel):
    operation: Operation
    ingredient_a: SynthObject
    ingredient_b: SynthObject
    options: CraftOptions = Field(default_factory=CraftOptions)
```

### 4.3 CandidateObject

候选对象是生成阶段的中间产物，不一定会成为最终对象。

```python
class CandidateObject(BaseModel):
    name: str
    type: SynthType
    description: str
    emoji: str | None = None
    core_tags: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    source_reason: str
```

### 4.4 CraftResult

```python
class ScoreBreakdown(BaseModel):
    semantic_similarity: float = 0.0
    name_similarity: float = 0.0
    anchor_bonus: float = 0.0
    type_compatibility_bonus: float = 0.0
    route_prior_bonus: float = 0.0
    contradiction_penalty: float = 0.0
    over_generic_penalty: float = 0.0
    total: float = 0.0


class CraftResult(BaseModel):
    success: bool
    result: SynthObject | None = None
    failure_reason: str | None = None
    decision: Decision
    cached: bool = False
    candidate: CandidateObject | None = None
    matched_object_id: int | None = None
    score_breakdown: ScoreBreakdown | None = None
    explanation: str
```

## 5. Pipeline

### 5.1 Validate

检查内容：

- `operation` 必须是 `add` 或 `subtract`。
- 两个输入对象必须有 `name` 和合法 `type`。
- banned 对象默认不可参与合成，除非 options 显式允许。
- description 为空时仍可合成，但应降低语义置信度。

### 5.2 Normalize

标准化内容：

- 去除首尾空白。
- 统一中英文符号。
- 保留专名大小写和特殊标点，例如 `BanG Dream!`、`MyGO!!!!!`。
- 将 null description 转为空字符串用于检索，但在对象输出中保留 null 语义。

### 5.3 Feature Extraction

从对象中抽取：

- `name_tokens`：名称词、专名、数字、符号专名。
- `semantic_tags`：自然物、职业、武器、舞台、音乐、角色、组织等。
- `anchors`：IP、角色、代表曲、国别、世界观词、品牌、系列名。
- `functions`：用途、能力、行为、装备功能。
- `materials`：物质、能量、组成。
- `constraints`：矛盾项、排除项、已剥离属性。

第一版可以用规则和词典实现；后续再接 LLM 或 embedding 模型。

### 5.4 Intent Planning

根据 type 组合和 operation 生成意图：

```text
add:
  融合 A/B 的物质、功能、身份、题材、世界观或文化锚点。

subtract:
  从 A 中移除 B 代表的属性、组成、用途、身份、武装、能源或题材。
```

Intent 至少包含：

- 预期输出 type。
- 主要保留项。
- 主要移除项。
- 应优先考虑的锚点。
- 是否偏向常识对象、专名对象或新概念。

### 5.5 Candidate Generation

候选生成器分两层：

1. 规则候选：根据 type 组合、关键词、已有 route 模式生成 3 到 10 个候选。
2. LLM 候选：用结构化 prompt 生成更自然的候选名、type、description 和解释。

第一版可以只实现规则候选 + 可插拔 LLM 接口。候选输出必须结构化，不能只返回一段自然语言。

### 5.6 Retrieval

用候选对象检索已有对象库：

- 名称精确匹配。
- 名称归一化匹配。
- description/tag 关键词召回。
- craft_sources 邻接召回：输入 A/B 附近的结果和来源对象。
- 可选 embedding 召回。

召回阶段宁可多取一些，最终由 ranker 决定。

### 5.7 Ranking

评分公式：

```text
score =
  semantic_similarity
  + name_similarity
  + anchor_bonus
  + type_compatibility_bonus
  + route_prior_bonus
  - contradiction_penalty
  - over_generic_penalty
```

评分项：

- `semantic_similarity`：候选定义与已有对象定义是否等价。
- `name_similarity`：名称是否同义、缩写、翻译或上下位关系。
- `anchor_bonus`：是否共享强排他锚点。
- `type_compatibility_bonus`：type 是否符合 intent。
- `route_prior_bonus`：已有 craft_sources 是否支持相邻路径。
- `contradiction_penalty`：描述中是否存在明确冲突。
- `over_generic_penalty`：候选是否被过泛对象吸附。

### 5.8 Decision

决策输出三类：

```text
强命中：返回已有对象，并记录 matched_existing。
弱命中：返回候选与 top matches，按配置决定是否新建。
无命中：创建新对象。
```

第一版建议使用可配置阈值：

```text
match_threshold = 0.78
review_threshold = 0.62
```

这些阈值不是线上真实阈值，只是本地实现的初始参数，必须通过回放数据校准。

### 5.9 New Object Construction

新对象字段生成规则：

- `name`：选择最自然、稳定、可命名的候选名。
- `type`：来自 intent/candidate/ranker 投票。
- `description`：必须解释 A/B 与 operation 如何得到结果。
- `emoji`：可由 type + name 规则或 LLM 生成。
- `source`：本地引擎可标记为 `local_engine`。
- `craft_sources`：记录本次 A/B/operation。

不要强行拼接名称，除非拼接本身就是最自然命名。

## 6. 当前引擎模块设计

### 6.1 模块结构

当前实现以 Python 包为核心，不依赖 HTTP 服务。建议结构：

```text
mysynth/
  __init__.py
  models.py
  engine.py
  store.py
  normalize.py
  features.py
  intent.py
  candidates.py
  retrieval.py
  ranking.py
  decision.py
  persistence.py
  evaluation.py
  cli.py
```

职责：

- `models.py`：Pydantic 数据模型。
- `engine.py`：编排完整 craft pipeline。
- `store.py`：对象库、路线库、recipe cache 的查询接口。
- `normalize.py`：名称、描述、符号、空值标准化。
- `features.py`：规则特征抽取。
- `intent.py`：根据输入 type 和 operation 生成合成意图。
- `candidates.py`：规则候选生成，后续接 LLM 候选生成器。
- `retrieval.py`：已有对象召回。
- `ranking.py`：候选与已有对象评分。
- `decision.py`：命中已有对象或创建新对象。
- `persistence.py`：SQLite 初始化、迁移、导入导出和审计日志。
- `evaluation.py`：route_edges 回放评估。
- `cli.py`：单次合成和评估命令。

### 6.2 纯引擎接口

```python
from typing import Protocol


class SynthesizerEngine(Protocol):
    def craft(self, request: CraftRequest) -> CraftResult:
        ...
```

### 6.3 Store 接口

```python
from typing import Iterable, Protocol


ObjectId = int | str
RecipeKey = tuple[ObjectId, ObjectId, Operation]


class ObjectStore(Protocol):
    def get_object(self, object_id: ObjectId) -> SynthObject | None:
        ...

    def list_objects(self) -> Iterable[SynthObject]:
        ...

    def find_recipe(self, key: RecipeKey) -> SynthObject | None:
        ...

    def search_candidates(self, candidate: CandidateObject, limit: int) -> list[SynthObject]:
        ...

    def save_craft_result(
        self,
        request: CraftRequest,
        result: CraftResult,
    ) -> None:
        ...
```

### 6.4 Engine 编排逻辑

```text
craft(request)
  -> validate(request)
  -> normalize(ingredient_a, ingredient_b)
  -> recipe_cache lookup
  -> extract_features(a, b)
  -> plan_intent(features, operation)
  -> generate_candidates(intent, features)
  -> retrieve_existing_objects(candidates, store)
  -> rank_matches(candidates, retrieved_objects)
  -> decide(best_match, best_candidate, thresholds)
  -> construct result
  -> store.save_craft_result(request, result)
  -> return CraftResult
```

关键规则：

- `add` 默认对称，recipe key 应规范化输入顺序；`subtract` 不对称，必须保留 A/B 顺序。
- 若 recipe cache 命中，直接返回缓存对象，同时 `cached=true`。
- 若已有对象强命中，返回已有对象，不改写已有 description。
- 若创建新对象，description 必须解释本次 A/B/operation。
- 本地新对象默认 `source="local_engine"`，`discovery_method="local_engine"`。
- 持久化失败时不应吞掉错误；引擎应返回失败或抛出受控异常，避免内存结果和磁盘结果不一致。

### 6.5 第一版候选生成逻辑

第一版不直接依赖 LLM。候选生成按规则产生可解释结果：

- 名称组合候选：`A+B`、`B+A`、核心词组合、上位词组合。
- 常识模板候选：自然物、装备、职业、组织、舞台、武器、能源等模板。
- 减法候选：保留 A 的物理壳、功能壳、抽象本质，移除 B 的属性。
- route 类比候选：从相同 type 组合和相邻对象路线中提取常见结果模式。
- 专名锚点候选：当输入含强锚点时，保留专名而不是泛化成题材词。

候选必须带 `source_reason`，否则不进入 ranking。

### 6.6 第一版召回和评分逻辑

召回顺序：

1. recipe cache 精确召回。
2. 名称精确或归一化召回。
3. 名称 token 召回。
4. description/tag 召回。
5. route 邻接召回。
6. type 相同对象补充召回。

评分初始权重：

```text
semantic_similarity:      0.30
name_similarity:          0.25
anchor_bonus:             0.15
type_compatibility_bonus: 0.12
route_prior_bonus:        0.10
contradiction_penalty:   -0.20
over_generic_penalty:    -0.10
```

第一版评分不追求复杂模型，优先做到：

- 每一项可解释。
- 每个 top match 能输出命中理由。
- 错误样本能定位是召回失败、候选失败、评分失败还是阈值失败。

### 6.7 CLI 草案

```bash
mysynth craft --a 1 --b 35 --operation add
mysynth craft --a-name 水 --b-name 风 --operation add
mysynth eval --routes outputs/data/current/mysynthesizer_mine_full_routes_latest.json
```

CLI 是当前阶段的主要交互面。HTTP/API 适配等核心引擎稳定后再做。

## 7. 评估方法

使用本地图谱的 `route_edges` 做回放：

```text
输入：a_id, b_id, operation
真实输出：result_id
引擎输出：predicted result
```

指标：

- exact id match：是否命中同一个对象。
- name match：名称是否一致或归一化一致。
- type match：type 是否一致。
- top-k match：真实结果是否进入候选 top k。
- decision accuracy：应命中已有对象时是否命中，应新建时是否新建。
- explanation quality：解释是否覆盖 A/B 和 operation。

评估集应按类型拆分：

- `concept + concept`
- `element + element`
- `concept + creature`
- `concept + equipment`
- `equipment + equipment`
- subtract 主要组合
- IP/专名锚点样本
- 常识物理样本

## 8. 实施阶段

Phase 1：核心引擎骨架

- 建立 `mysynth/` Python 包。
- 实现 `models.py`、`engine.py`、`store.py` 的最小闭环。
- 读取 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
- 建立 `objects_by_id`、`recipe_index`、`name_index`、`token_index`、`neighbors_by_object`。
- 支持 `craft(CraftRequest) -> CraftResult`。

Phase 2：SQLite 持久化

- 创建 `data/engine/` 数据目录。
- 实现 `SQLiteObjectStore`。
- 初始化 `mysynth.db` schema。
- 从 `mysynthesizer_mine_full_routes_latest.json` 导入对象和路线。
- 实现 `objects`、`object_payloads`、`route_edges`、`recipe_cache`、`craft_events`、`failures` 表写入。
- 本地新对象使用负 id。
- 写入使用事务，保证对象、路线和 recipe cache 一致。

Phase 3：规则 pipeline

- 实现 validate、normalize、feature extraction、intent planning。
- 实现规则候选生成。
- 实现 recipe cache、名称、token、description、route 邻接召回。
- 实现可解释评分和阈值决策。
- 支持新对象构造和保存。

Phase 4：CLI 与回放评估

- 支持 CLI 单次合成。
- 支持按 `route_edges` 回放评估。
- 输出 exact id/name/type/top-k/decision 指标。
- 输出错误样本报告，区分候选失败、召回失败、评分失败和阈值失败。

Phase 5：LLM 候选生成

- 增加 `CandidateGenerator` 接口。
- 用结构化 prompt 生成候选。
- 保留规则候选作为 fallback。
- 对候选输出做 schema 校验。

Phase 6：语义检索与调参

- 增加 embedding 召回。
- 用 route_edges 做阈值校准。
- 输出错误样本报告，区分泛化吸附、专名缺失、type 错误和减法错误。

Phase 7：API 兼容层

- 提供 `POST /api/craft` 核心接口，接收完整 objects。
- 提供 `POST /api/craft/by-ids` 兼容接口，接收 `ingredient_ids`。
- 响应格式以 `CraftResult` 为准，保留线上前端响应中的关键对象字段。

## 9. 与现有设计文档的关系

`mysynthesizer-synthesizer-design.md` 继续作为机制分析和行为模型入口。

本文负责实现边界、数据模型、pipeline、接口和评估方法。后续修改时应避免把产品/实现细节混回机制分析文档。

## 10. 注意事项

- 不把真实 session cookie 写入仓库。
- 不把前端 API 当作本地引擎的唯一输入形态。
- 不假设线上真实阈值已知。
- 不用大范围模糊匹配替代强锚点判断。
- 所有“像线上”的判断都必须能通过 route_edges 回放评估。
