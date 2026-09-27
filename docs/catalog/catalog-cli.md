# 本地目录命令与卡片格式 v1

2026-09-27 已实现。入口为 [catalog.py](../../scripts/catalog.py)，Python 3.10+，仅使用标准库。实际验证环境为 Ubuntu 24.04 / Python 3.12.3；代码按跨平台路径和 UTF-8 输出实现，Windows 原生环境尚未运行验收。命令在指定工作区操作，不调用 AI、网络或天工服务。

## 快速体验

在本项目根目录执行，工作区选择一个新的目录：

```bash
python3 scripts/demo_catalog.py --workspace outputs/process-catalog-demo
python3 scripts/catalog.py --workspace outputs/process-catalog-demo search --function 接收 --input 矿物固体
python3 scripts/catalog.py --workspace outputs/process-catalog-demo candidates demo-dryer --port product
python3 scripts/catalog.py --workspace outputs/process-catalog-demo expand demo-line
```

示例脚本导入8张构造卡片及1张公开过程身份/名称摘要，写出 `runs/demo-summary.json`。数值和构造流程用于验证程序行为；公开摘要未包含完整清单。已有卡片的工作区会被示例脚本拒绝，便于把演示与研究数据分开。

从任意其他目录使用时，将脚本和工作区改为对应绝对路径即可。搬迁项目时保留 src、scripts 和 examples 的相对结构，同样可以运行。

## 命令

通用形式：`python3 /path/to/catalog.py --workspace /path/to/workspace COMMAND`。`--workspace` 放在子命令前。成功返回 stdout JSON `{ok: true, result: ...}`，失败返回 stderr JSON 与退出码2；参数用法错误由命令解析器显示帮助。成功退出码0。

| 命令 | 参数 | 行为 |
| --- | --- | --- |
| `init` | 无 | 创建目录标识；重复调用保留已有工作区 |
| `validate` | `--file card.json`；无需工作区 | 检查卡片结构、来源引用、局部端口及关系；跨模块引用在保存时检查 |
| `save` | `--file card.json --expect-revision N` | 新模块 N=0；修改时必须匹配当前修订，卡片修订必须为 N+1 |
| `get` | `MODULE [--revision N]` | 读取卡片、内容散列、合并后字段和字段来源；指定修订精确读取 |
| `history` | `MODULE` | 返回全部已保存修订 |
| `search` | `--query TEXT --function TEXT --category TEXT --input TEXT --output TEXT`，均可选 | 搜索最新修订，返回匹配字段、卡片摘要与来源；多个条件取交集 |
| `reindex` | 无 | 从最新卡片重建索引 |
| `expand` | `MODULE [--revision N] [--representation ID] [--depth N] [--max-nodes N]` | 默认选第一种表达，按固定修订展开，返回实例路径、端口、连接、边界映射和未知项 |
| `candidates` | `MODULE --port OUTPUT [--revision N]` | 寻找最新模块中的接收接口，返回匹配线索和逐项条件判断 |
| `refresh` | `MODULE --file refresh.json --expect-revision N` | 更新来源提取层，保留本地补充，形成新修订并报告受影响及被本地覆盖的字段 |
| `inspect-tiangong-model` | `--file model.json`；无需工作区 | 只读提取天工原生模型的实例、版本、倍率和连接，并返回结构诊断 |

检索采用 Unicode 规范化后的子串匹配；查询文本按空白分词后取交集，功能、类别、输入和输出按指定字段过滤。当前覆盖名称、摘要、同义词、功能、类别、行业和接口术语。首次查询、索引缺失/损坏或卡片更新后会重建索引，所以 `search` 和 `candidates` 可能写入派生索引。

天工模型读取的输入与范围见[原生模型检查](tiangong-model-inspection.md)。

## 卡片格式

可直接查看 [干燥构造卡片](../../examples/catalog/demo-dryer.json)、[复合模块](../../examples/catalog/demo-conditioning.json)、[公开名称摘要](../../examples/catalog/tiangong-clinker-public-summary.json)。结构由 `validate_card` 校验，未知字段会报错，避免拼写错误静默丢失。此版本使用运行时校验器，尚未提供独立 JSON Schema。

顶层恰有六个字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 当前固定为整数1 |
| `module_id` | 本地稳定身份，小写字母/数字/下划线/连字符，最长80字符；显示名称可用中文 |
| `revision` | 本地正整数修订，与上游数据版本分别记录 |
| `sources` | 来源数组，每项包含 `id`、`kind`、`citation`；可加 `uri`、`locator`、`retrieved_at`、`sha256` |
| `extracted` | 来源提取层，包含 `origin`、`source_ids`、`data` |
| `local` | 本地补充层，同样包含 `origin`、`source_ids`、`data` |

`origin` 支持 `source_extraction`、`sourced_supplement`、`hypothesis`、`synthetic`。有来源的非空层必须引用已记录来源；假设和构造数据保留各自地位。`source_ids` 默认作用于该层各字段。首版追溯粒度为顶层字段；更细的单条性质证据需要在来源定位中说明，尚无逐断言混合来源模型。

`data` 支持 `name`、`summary`、`aliases`、`categories`、`functions`、`industries`、`interfaces`、`representations`、`external_refs`、`unknowns`、`parameters`。合并后至少包含名称、摘要、接口数组、表达数组及未知项数组；接口可以为空，表达至少一项。标签和未知项是字符串数组；参数暂按对象保留，不执行公式。

合并规则是**本地层按顶层字段整体覆盖提取层**。本地未设置的字段沿用提取层；数组和嵌套对象整体覆盖，空数组表示明确采用空值。返回的 `field_origins` 指出每个字段的来源层。维护接口时如需局部补充，先形成完整的本地接口数组；刷新会报告被覆盖字段，便于人工合并新来源内容。

### 接口与条件

每个接口有 `id`、`direction: input|output`、`kind: material|energy|service|environment` 和术语数组 `terms`；可加 `properties`、`requirements`、`flow_refs` 和 `unknowns`。

- 性质项：`key`、`value`、`unit`、`basis`。值支持有限数值、字符串或 `null`；同一接口的性质键唯一。
- 要求项：上述字段加 `op: eq|le|ge`；数值不等式使用数值或未知值。
- 流引用：`provider`、`id`，可加 `version`。流身份相同只是候选线索。

候选搜索要求输出/输入方向和接口种类相容，并有完全匹配的规范化术语或相同提供方/流 ID。之后逐项检查声明的标量条件。单位及基准须明确且一致；首版不做换算。缺性质、未知值、基准不一致和未声明条件返回未知。零值参与正常计算。

返回的 `condition_status` 仅对应声明条件：已知不满足优先；有未知条件或模块/接口未知项时保留未知。结果附带两端修订、证据来源层和检查范围，工程可行性仍未评估。`candidates` 当前返回结果，不持久化独立路线或执行多跳自动搜索。

### 层级与外部映射

每种表达有 `id` 和 `kind: whole|composite`。复合表达包含 `children`：每个子实例有独立 `id`，指定 `module_id`、整数 `revision` 和 `representation`。连接通过 `from/to` 中的 `{instance, port}` 引用子实例端口；`boundary_bindings` 将父接口映射到一个子实例端口，首版保持一对一映射。

保存时要求子模块和所选表达已经存在，检查端口、连接方向及边界类型；资料缺少内部结构时可先保存整体表达和未知项。部分边界映射允许保存，需要在 `unknowns` 中说明未解释部分。层级展开默认深度5、最多500节点；达到限制或发现包含环时返回 `partial` 和定位信息。物料回流作为连接保留，展开不进行物理求解。

`external_refs` 每项包含 `provider`、`id`、`relation`、`scope`，可加请求/返回版本、URI、获取时间及散列。关系可为 `exact`、`partial`、`aggregate_contains`、`candidate`，同一模块可以有多个映射，同一外部身份也可以被多个模块引用。当前按字段保存与返回这些映射，完整上游记录仍须由现有天工工具获取。

## 刷新与保存

刷新文件包含新的整个 `extracted` 对象和新增 `sources` 数组。例如从旧卡片复制提取层、修改相应摘要，并引用新的观测来源 ID。旧来源保留；同一来源 ID 的内容发生变化会报告 `SOURCE_CONFLICT`，应为新观测分配新 ID。

保存采用独占写锁、临时文件及原子替换；修订只新增，带 `content_hash` 和保存时间。过期的 `--expect-revision` 被拒绝，已有修订保持原样。进程异常退出可能遗留 `.catalog.lock`，核实写进程已结束后人工处理；程序不自动抢占锁。面向单机本地目录，尚未验收网络共享目录上的多人协作与断电恢复保证。

实际布局：

```text
workspace/
├─ catalog.json
├─ modules/<module_id>/00000001.json  # card、content_hash、saved_at、change
├─ index/catalog.json                # 可重建的最新卡片索引
└─ runs/demo-summary.json             # 仅示例脚本生成
```

需要编辑时从 `get` 返回值中提取 `result.card`，保存为工作文件，递增修订后调用 `save`。存储文件被直接修改后内容散列不符，读取会报 `INTEGRITY_ERROR`。修订明确指定时不回退其他版本。首次实现为保证一致性，每次检索检查最新卡片内容；大库性能、语义检索及更细的局部刷新留待真实规模验证。

## 开发验证

在仓库根目录运行 `python3 -m unittest discover -s tests -v`。目前测试覆盖持久化/修订冲突、来源刷新、索引恢复、接口条件、固定修订层级、包含环及从其他安装位置调用。参数求值、实例参数编辑、封装/替换、完整 LCA 导入导出和在线求解仍是后续能力。
