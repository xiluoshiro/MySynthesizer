# MySynthesizer 合成器引擎实现设计

## 1. 目标

最终目标是写一个本地可运行的合成器引擎：

```text
输入：两个待合成对象 + operation(add/subtract)
输出：合成后的对象 + 命中/新建判断 + 可解释过程
```

后端使用 Python 实现。这个引擎不是简单调用前端 `POST /api/craft` 的封装，而是把当前设计文档中反推出来的合成流程工程化，形成一套可测试、可调参、可替换模型的完整 pipeline。

第一版目标是行为相似，而不是完全复刻线上后端。真实 prompt、检索算法和阈值不可见，因此实现应保留可观察证据、评分明细和调参入口。

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

本地 Python 后端建议把接口拆成两层：

- `POST /api/craft`：核心合成接口，接收两个完整对象。
- `POST /api/craft/by-ids`：兼容接口，接收 id，由后端 adapter 取对象详情后再调用核心引擎。

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
- 后续可替换为 SQLite、Postgres、向量库或线上 API。

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

## 6. Python 后端接口草案

### 6.1 纯引擎接口

```python
from typing import Protocol


class SynthesizerEngine(Protocol):
    def craft(self, request: CraftRequest) -> CraftResult:
        ...
```

### 6.2 核心 HTTP 接口

核心接口直接接收两个对象，适合本地后端、测试、批量评估和后续服务化。

```http
POST /api/craft
Content-Type: application/json
```

请求体：

```json
{
  "operation": "add",
  "ingredient_a": {
    "id": 1,
    "name": "水",
    "emoji": "💧",
    "type": "element",
    "description": "流动、滋养与变化的基础元素。",
    "source": "system",
    "category_ids": []
  },
  "ingredient_b": {
    "id": 35,
    "name": "风",
    "emoji": "💨",
    "type": "element",
    "description": "流动的空气与运动的力量。",
    "source": "system",
    "category_ids": []
  },
  "options": {
    "use_llm": false,
    "max_candidates": 10,
    "match_threshold": 0.78,
    "review_threshold": 0.62,
    "explain": true
  }
}
```

响应体：

```json
{
  "success": true,
  "result": {
    "id": 252,
    "name": "水龙卷",
    "emoji": "🌪️",
    "type": "element",
    "description": null,
    "source": "llm",
    "is_banned": false,
    "first_discoverer_id": 17,
    "first_discoverer_nickname": "Gloaming",
    "created_at": "2026-06-30T11:04:56.139361Z",
    "discovered_at": "2026-07-06T16:04:26.843809Z",
    "discovery_method": "recipe_cache",
    "is_first_discoverer": false,
    "category_ids": []
  },
  "failure_reason": null,
  "decision": "matched_existing",
  "cached": true,
  "candidate": {
    "name": "水龙卷",
    "type": "element",
    "description": "水与风结合形成的旋转水柱或强烈涡旋现象。",
    "emoji": "🌪️",
    "core_tags": ["水", "风", "旋转", "涡旋"],
    "anchors": [],
    "source_reason": "水提供水体，风提供旋转和运动力，最自然结果是水龙卷。"
  },
  "matched_object_id": 252,
  "score_breakdown": {
    "semantic_similarity": 0.4,
    "name_similarity": 0.2,
    "anchor_bonus": 0.0,
    "type_compatibility_bonus": 0.15,
    "route_prior_bonus": 0.08,
    "contradiction_penalty": 0.0,
    "over_generic_penalty": 0.0,
    "total": 0.83
  },
  "explanation": "水与风的融合优先解释为自然现象，候选水龙卷与已有对象在名称和类型上强匹配，因此返回已有对象。"
}
```

### 6.3 兼容 id 接口

兼容接口只负责把 id 转成完整对象，再调用核心接口。

```http
POST /api/craft/by-ids
Content-Type: application/json
```

请求体：

```json
{
  "ingredient_ids": [1, 35],
  "operation": "add",
  "options": {
    "use_llm": false
  }
}
```

Pydantic 模型：

```python
class CraftByIdsRequest(BaseModel):
    ingredient_ids: tuple[int, int]
    operation: Operation
    options: CraftOptions = Field(default_factory=CraftOptions)
```

处理流程：

```text
CraftByIdRequest
  -> ObjectAdapter.getObject(id)
  -> SynthesizerEngine.craft(CraftRequest)
  -> ApiResponseFormatter.toCraftResponse(CraftResult)
```

### 6.4 FastAPI 草案

```python
from fastapi import APIRouter


router = APIRouter(prefix="/api")


@router.post("/craft", response_model=CraftResult)
def craft(request: CraftRequest) -> CraftResult:
    return engine.craft(request)


@router.post("/craft/by-ids", response_model=CraftResult)
def craft_by_ids(request: CraftByIdsRequest) -> CraftResult:
    object_a = object_adapter.get_object(request.ingredient_ids[0])
    object_b = object_adapter.get_object(request.ingredient_ids[1])
    return engine.craft(
        CraftRequest(
            operation=request.operation,
            ingredient_a=object_a,
            ingredient_b=object_b,
            options=request.options,
        )
    )
```

### 6.5 CLI 草案

```bash
mysynth craft --a 1 --b 35 --operation add
mysynth craft --a-name 水 --b-name 风 --operation add
mysynth eval --routes outputs/data/current/mysynthesizer_mine_full_routes_latest.json
```

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

## 8. 第一阶段实现建议

Phase 1：离线规则引擎

- 读取 `mysynthesizer_mine_full_routes_latest.json`。
- 建立 object index 和 route index。
- 实现数据模型、标准化、简单特征抽取。
- 实现名称/description 关键词召回。
- 实现可解释评分。
- 支持 CLI 单次合成和 route 回放评估。

Phase 2：LLM 候选生成

- 增加 `CandidateGenerator` 接口。
- 用结构化 prompt 生成候选。
- 保留规则候选作为 fallback。
- 对候选输出做 schema 校验。

Phase 3：语义检索与调参

- 增加 embedding 召回。
- 用 route_edges 做阈值校准。
- 输出错误样本报告，区分泛化吸附、专名缺失、type 错误和减法错误。

Phase 4：API 兼容层

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
