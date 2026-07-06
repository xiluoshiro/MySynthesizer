# MySynthesizer 合成器研究工作区

这个目录用于保存 MySynthesizer 合成路径数据、合成器机制分析，以及后续复刻合成器的设计资料。

## 当前入口

- `docs/design/mysynthesizer-synthesizer-design.md`：合成器机制、正向模型、AI 操作规则的统一设计文档。
- `outputs/data/current/`：当前最新可用合成数据。

## 重要文件

- `docs/design/mysynthesizer-synthesizer-design.md`  
  合并后的权威设计入口，包含机制分析、正向合成流程、匹配/新建判断、type 统计、专名化策略和后续 AI 工作规范。

- `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`  
  当前最新完整图谱。最近一次刷新为 `1355 objects / 3100 craft_sources`。

- `outputs/data/current/mysynthesizer_route_summary.md`  
  最新扫描摘要，包含新增对象与新增路线概览。

## 后续新会话建议

1. 先读本 README。
2. 如果要理解合成器机制或规划后续合成路线，读 `docs/design/mysynthesizer-synthesizer-design.md`。
3. 如果要做证据核查，读 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json`。
4. 如果用户说“又合成了新东西”或要求刷新当前 mine，调用 `mysynthesizer-route-scanner` skill，并用最新 cookie 重新扫描。

## 数据整理说明

本次整理完成了分类移动，并删除了确认无用或已被最新数据覆盖的文件：

- 合成器设计文档移动到 `docs/design/`。
- 三份旧设计文档已合并为 `docs/design/mysynthesizer-synthesizer-design.md`，旧稿已删除。
- 最新完整图谱和扫描摘要移动到 `outputs/data/current/`。
- 临时脚本、截图和 skill 测试输出已删除。
- 与 `mysynthesizer_mine_full_routes_latest.json` 内容完全相同的旧命名副本已删除，只保留 `*_latest` 入口。
- `outputs/data/archive/` 中的历史对象 key 已确认全部被 `outputs/data/current/mysynthesizer_mine_full_routes_latest.json` 覆盖，archive 已删除。
