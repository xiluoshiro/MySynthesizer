# MySynthesizer 合成器设计说明

数据快照：`outputs/data/current/mysynthesizer_mine_full_routes_latest.json`  
刷新时间：`2026-07-06T15:06:01.457Z`  
当前规模：`1355 objects / 3100 craft_sources`，其中 `add=2382`、`subtract=718`

## 1. 文档定位

本文是 MySynthesizer 合成器研究的唯一设计入口，合并了三类内容：

- 机制分析：从已拥有对象和 `craft_sources` 反推合成器行为。
- 正向模型：描述一次合成请求从输入到返回结果的推理流程。
- AI 操作规则：给后续 AI 或脚本用于规划、验证、刷新路线。

本文不是后端源码，也不是已读取到的真实 prompt。它是基于当前图谱行为反推出的高置信度工作模型。

## 2. 核心结论

当前数据支持这个模型：

1. 合成器读取两个输入对象的 `name / type / description` 和操作符。
2. 先生成一个自然、稳定、可命名的候选结果。
3. 再在已有对象库中寻找语义匹配项。
4. 如果候选足够接近已有对象，则返回已有对象，并给该对象追加一条 `craft_sources`。
5. 如果没有足够接近的对象，则创建新对象，通常写入能直接解释 A/B 来源的描述。

命中已有对象不是简单的名称匹配。更接近的公式是：

```text
命中概率 ≈ 语义相似度 + 已有对象稳定性 + 排他锚点强度 + type 相容性 - 输入冲突度
```

真实 prompt、检索算法和数值阈值不可见，不能可靠给出百分比阈值。

## 3. 输入与输出

合成器的核心输入：

- `ingredient_a`: name, type, description, optional fields
- `ingredient_b`: name, type, description, optional fields
- `operation`: `add` 或 `subtract`
- existing object library: 已存在对象及其 name/type/description/source/first_discoverer

核心输出：

- 命中已有对象：返回已有 object，并记录新的 `craft_sources`。
- 创建新对象：写入 name/type/description/emoji/source/first_discoverer，并记录 `craft_sources`。

对象类型固定在：

```text
element / item / equipment / creature / concept
```

## 4. 正向流水线

### 4.1 输入标准化

合成器会先整理输入：

- 去除空白，统一中英文符号，保留专名大小写。
- 读取 `type` 和 `description`。
- 从描述中抽取实体、功能、身份、场景、来源 IP、代表意象。
- 判断操作符：`add` 是融合，`subtract` 是剥离。

### 4.2 类型意图判断

不同 type 组合决定主要推理方向：

| 输入组合 | 主要问题 | 常见输出 |
|---|---|---|
| `concept + concept` | 两个抽象概念融合后最稳定的概念是什么？ | `concept` |
| `element + element` | 两种自然物质、能量、天象结合成什么？ | `element` 或自然现象 |
| `concept + creature` | 生物获得身份、职业、题材或关系后是什么？ | `creature` 或 `concept` |
| `concept + equipment` | 装备被技术、用途、体系改造后是什么？ | `equipment` 或 `concept` |
| `creature + creature` | 两个角色或生物结合后是新角色、关系还是群体？ | `creature` 或 `concept` |
| `equipment + equipment` | 两个装备组合后是复合装备还是作战体系？ | `equipment` 或 `concept` |

减法的核心问题是：

```text
从 A 中移除 B 代表的属性、组成、身份、用途、武装、能源或题材后，剩下最自然、稳定、可命名的对象是什么？
```

### 4.3 候选结果生成

加法近似 prompt：

```text
给定对象 A 与 B：
- A: name, type, description
- B: name, type, description

请判断 A 与 B 结合后，在常识、文化、题材、功能或世界观上最自然、稳定、可命名的结果。
输出候选对象名、type 和简短定义。
不要强行拼接名称，除非拼接本身就是最自然的命名。
如果输入包含 IP、角色、组织、代表曲、世界观、品牌、国别等排他锚点，应优先考虑这些锚点。
```

减法近似 prompt：

```text
给定对象 A 和待移除对象 B：
- A: name, type, description
- B: name, type, description

请从 A 中移除 B 所代表的属性、组成、身份、用途、武装、能源或题材。
推断剩余部分最自然、稳定、可命名的对象。
如果 A 是具体装备或物品，优先考虑是否保留物理外壳。
如果 A 是概念，优先考虑剥离后的抽象本质。
```

### 4.4 结构化与匹配

候选结果可理解为被结构化成：

```json
{
  "candidate_name": "音乐剧声优",
  "candidate_type": "concept",
  "candidate_description": "指在音乐剧中担任表演和歌唱的声优，或参与音乐剧演出的配音演员。",
  "core_tags": ["声优", "音乐剧", "表演", "歌唱", "舞台"],
  "source_reason": "声优与音乐剧结合，形成兼具配音、演唱和舞台表演能力的角色/职业概念。"
}
```

这些字段未必真实存在于 API，但能解释观察到的行为。

匹配逻辑可按伪代码理解：

```text
candidate = generate_candidate(A, B, operation)
matches = search_existing_objects(candidate.name, candidate.tags, candidate.description)

for match in matches:
    score = semantic_similarity(candidate, match)
    score += anchor_bonus(candidate, match)
    score += type_compatibility_bonus(candidate, match)
    score -= contradiction_penalty(candidate, match)

best = highest_score(matches)

if best is strong enough:
    return best.object and add craft_source
else:
    create candidate as new object
```

## 5. 命中与新建

### 5.1 命中已有对象

命中已有对象时，返回对象的 description 可能不是为当前输入写的。这解释了有些合成结果看起来“描述没有完全解释这次合成”的现象。

典型模式：

| 模式 | 行为 | 例子 |
|---|---|---|
| 高相似命中 | 候选名或定义几乎等价于已有对象 | `少女 + 歌剧 = 歌剧少女` |
| 上位概念吸附 | 多条路径自然归入同一稳定概念 | `人 + 文明 = 社会`，`文明 + 群体 = 社会` |
| 物理常识吸附 | 不同物理路径指向同一自然物 | `水 + 地热 = 温泉`，`水 + 火山 = 温泉` |
| 排他锚点命中 | 输入触发 IP、角色、代表意象或企划结构 | `声优 + 声优乐队 = BanG Dream!` |

### 5.2 创建新对象

创建新对象时，description 往往直接解释 A/B 如何导致结果。当前图谱中新对象强烈偏向 `concept`，其次是 `equipment` 或 `creature`。

典型样例：

| route | result | 判断 |
|---|---|---|
| `声优 + 音乐剧` | `音乐剧声优` | 保留两个输入的核心身份，生成职业/角色概念。 |
| `少女 + 音乐剧声优` | `音乐剧少女` | 把少女身份和音乐剧声优身份融合成角色类型。 |
| `星光 + 音乐剧少女` | `舞台明星` | 把星光解释为舞台闪耀感。 |
| `音乐剧声优 + 舞台明星` | `音乐剧之星` | 进一步抽象为舞台明星/声优表演者。 |
| `火炮巡洋舰 - 战争` | `海洋调查船` | 去掉军事用途，保留船体平台并重解释为科研用途。 |
| `DJ - 人类` | `打碟机` | 从人类职业中剥离人，留下职业工具。 |

## 6. 当前类型统计

### 6.1 对象类型分布

| type | count |
|---|---:|
| concept | 853 |
| equipment | 206 |
| creature | 140 |
| element | 97 |
| item | 59 |

### 6.2 加法倾向

加法样本：`2382` 条。

| result type | count |
|---|---:|
| concept | 1400 |
| element | 408 |
| equipment | 216 |
| creature | 207 |
| item | 151 |

主要输入组合：

| add input types | total | dominant output |
|---|---:|---|
| `concept + concept` | 765 | `concept` 699 |
| `element + element` | 366 | `element` 234 |
| `concept + element` | 340 | `concept` 181 / `element` 115 |
| `concept + creature` | 250 | `concept` 165 / `creature` 75 |
| `concept + equipment` | 169 | `concept` 90 / `equipment` 65 |
| `equipment + equipment` | 96 | `equipment` 70 |
| `element + item` | 85 | `item` 34 / `element` 26 |
| `concept + item` | 74 | `concept` 41 |
| `creature + creature` | 72 | `creature` 43 / `concept` 28 |

结论：

- `concept + concept` 极强地输出 `concept`，适合文化、IP、风格、活动、关系、体系。
- `element + element` 多数仍是 `element`，适合自然物、物质、能量、天象。
- `concept + creature` 若概念是身份/职业/阵营，可能输出 `creature`；若是题材/文化/关系，多数输出 `concept`。
- `concept + equipment` 若概念增强功能，输出 `equipment`；若描述战术、体系、用途，输出 `concept`。
- `equipment + equipment` 多数仍为 `equipment`，但编队、战术或体系会变 `concept`。

### 6.3 减法倾向

减法样本：`718` 条。

| result type | count |
|---|---:|
| concept | 397 |
| element | 249 |
| equipment | 34 |
| creature | 21 |
| item | 17 |

主要输入组合：

| subtract input types | total | dominant output |
|---|---:|---|
| `concept - concept` | 226 | `concept` 213 |
| `element - element` | 163 | `element` 143 |
| `concept - element` | 85 | `concept` 48 / `element` 34 |
| `element - concept` | 53 | `element` 35 / `concept` 18 |
| `creature - concept` | 34 | `concept` 26 |
| `concept - creature` | 21 | `concept` 14 / `creature` 6 |
| `equipment - concept` | 18 | `equipment` 10 |
| `item - element` | 17 | `element` 14 |
| `creature - creature` | 15 | `concept` 11 |

结论：

- 减法最常是“剥离属性、身份、功能后留下本质”。
- `concept - concept` 几乎总是 `concept`。
- `element - element` 几乎总是 `element`。
- `creature - concept` 常输出抽象身份或残留属性，而不是新生物。
- `equipment - concept` 容易保留装备壳。
- 若想保留具体物体或装备，A 应是具体 `item/equipment`，B 应是用途、武装、能源、身份等可剥离属性。

## 7. 专名化与排他锚点

专名化不是靠堆很多泛标签，而是靠排他锚点。

弱输入通常只会泛化：

```text
二次元 + 摇滚 + 乐队 + 少女
```

容易生成：

```text
二次元摇滚
动漫摇滚女团
次元摇滚乐队
```

强输入需要至少一个排他锚点：

- 已有 IP 名
- 代表角色
- 代表歌曲、口号、意象
- 独特组织结构
- 国别、品牌、世界观
- 足够排他的舞台或叙事词

`BanG Dream!` 的成功模式不是单纯“女子乐队”，而是：

```text
声优乐队 + 星之梦想/星之鼓动/星光舞台
户山香澄 + Roselia
MyGO!!!!! + 户山香澄
```

一旦专名对象被创建，它会成为强吸附点，后续相邻路线更容易命中它。

## 8. 当前语义簇与路线判断

### 8.1 泛乐队题材簇

代表对象：

- `迷子乐团`
- `迷途次元乐队`
- `动漫摇滚女团`
- `次元摇滚乐队`
- `二次元摇滚`
- `迷子乐队`

特征：

- 共享 `二次元 / 摇滚 / 乐队 / 迷途 / 女团`。
- 内部互相组合时，容易归并为更泛对象。

结论：

- 不要继续在这个簇内部做乐队概念互叠。

### 8.2 角色乐队企划簇

代表对象：

- `中之人`
- `演员`
- `偶像`
- `虚拟歌姬`
- `动漫摇滚歌手`
- `迷子乐团主唱`
- `动漫摇滚偶像`
- `虚拟主唱`
- `跨次元女子摇滚乐队`

特征：

- 共享身份、角色、主唱、偶像、配演、2.5 次元等结构词。
- 比单纯题材词更接近 IP 生成逻辑。

结论：

- 若目标是 `MyGO!!!!! / BanG Dream!` 一类角色乐队企划，应围绕这个簇继续试验。

### 8.3 少女歌剧 / Revue Starlight 路线

已有素材：

- `少女`
- `歌剧`
- `音乐剧`
- `舞台`
- `星光`
- `声优`
- `歌剧少女`
- `音乐剧声优`
- `音乐剧少女`
- `舞台明星`
- `音乐剧之星`
- `星光舞台`

不建议只用：

```text
少女 + 歌剧
```

因为这已经稳定命中普通 `歌剧少女`。

更合理的方向是先构造“舞台少女/音乐剧声优/星光舞台”簇，再加入排他星光锚点：

```text
音乐剧少女 + 星光舞台
音乐剧之星 + 歌剧少女
音乐剧声优 + 星光舞台
舞台明星 + 歌剧少女
```

预期：

- 若库中已有 `少女歌剧/Revue Starlight`，这些组合可能命中。
- 若未命中，可能新建 `星光音乐剧少女`、`少女歌剧之星`、`音乐剧少女舞台` 等中间对象。
- `Starlight九九组` 需要额外的 `九人组/组合/声优组合` 锚点。

## 9. 给 AI 的操作规范

### 9.1 先刷新，再推断

用户说“又合成了新东西”或要求刷新当前 mine 时：

1. 调用 `mysynthesizer-route-scanner` skill。
2. 用最新 cookie 重新扫描当前账号 mine。
3. 更新 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
4. 再基于最新图谱做判断。

### 9.2 所有结论分层

输出建议分三层：

- `已证明`：当前图谱里存在真实 `craft_sources`。
- `高置信度推断`：由对象名、description、plaza 样例、相邻路线推出。
- `低置信度脑补`：证据不足，只能作为下一步实验猜想。

### 9.3 描述优先于名字

对象名只提供表层语义。`description` 往往决定系统把对象收束到：

- 活动
- 角色
- 风格
- 企划
- 世界观
- 装备
- 自然物

### 9.4 避免同簇空转

若两个对象都已经带有：

- `二次元`
- `摇滚`
- `乐队`
- `迷途`
- `女团`

继续叠加通常只会泛化、回退或生成旁支。

### 9.5 若需要专名化，找簇外强锚点

优先级通常是：

1. 地域/国别
2. 作品/系列/企划
3. 代表角色/分工
4. 世界观专属词
5. 代表曲/口号/舞台意象
6. 题材词

题材词优先级最低，不能单独承担专名化。

## 10. 实战策略

若目标是新建对象：

- 使用两个具体但库中没有强现成对象的输入。
- 让 A/B 的描述能自然写出“融合/结合/去除”的新定义。
- 避免太常识化的组合，因为会命中已有对象。

若目标是命中已有对象：

- 使用已有对象 description 中的核心定义词。
- 对常识对象，用多条等价路径尝试。
- 对 IP 对象，使用排他锚点：代表角色、代表曲、世界观口号、企划结构。

若目标是专名化：

- 先构建结构簇，再加入排他锚点。
- 不要只叠同类题材词。
- 每轮控制实验数量，避免大范围盲合。

推荐控制实验格式：

1. 高相似输入：几乎等价于已有对象定义。
2. 边界输入：共享一个强锚点但缺少一个核心限定。
3. 低相似输入：只共享题材簇但没有排他锚点。

## 11. 已知限制

当前分析仍缺三类真实后端信息：

1. 真实 AI prompt。
2. 真实匹配算法：embedding、LLM rerank、名称检索或混合检索的比例未知。
3. 真实阈值：无法证明存在固定百分比。

观测标签也有局限：

- `first_discoverer_nickname = xiluo` 只能近似代表本账号创建的新对象。
- 非 `xiluo` 首发只能近似代表命中已有库对象，也可能是别人先创建后当前账号复现。
- 未被当前账号发现的对象，详情接口通常返回 `403`，不能确认精确配方。

因此后续结论应继续以最新 `mine` 图谱和真实 `craft_sources` 为准。
