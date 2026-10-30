# 天工复用与适配约定

2026-09-26 设计基线。区分 Tiangong AI 与 TianGong LCA；每次运行记录实际选用的包、Skill 提交和兼容性结果。本页给出候选复用边界，不自动安装、登录或发布。

## 上游归属

| 上游 | 拟复用内容 | 目前依据与限制 |
| --- | --- | --- |
| [Tiangong AI CLI](https://github.com/tiangong-ai/cli-toolkit) | 已有通用数据能力及任务实际需要的受支持命令 | 包 `@tiangong-ai/cli`、命令 `tiangong-ai`；本轮只读文档/元数据，未运行集成 |
| [Tiangong AI Skills](https://github.com/tiangong-ai/agent-skills) | 文献/专利检索、资料解析等已有工作流 | 按所选 Skill 的实际入口和依赖使用；并非所有 Skill 都由同一 CLI 承载 |
| [TianGong LCA CLI](https://github.com/tiangong-lca/cli) | TIDAS 上下文、构建、组装、校验和计算相关入口 | 包 `@tiangong-lca/cli`、命令 `tiangong-lca`；先前已做部分本地有界验证，在线认证仍需实际接入 |
| [TianGong LCA Skills](https://github.com/tiangong-lca/agent-skills) | 既有过程构建、模型组装、来源开发及导入工作流 | 所选包装器与其 CLI 要对应，不能仅按“都已安装”认定兼容 |

候选来源能力包括 `tiangong-kb-sci-search`、`tiangong-kb-patent-search`、`tiangong-kb-report-search` 和 `document-granular-decompose`；候选 LCA 能力包括 `process-automated-builder`、`lifecyclemodel-automated-builder`、`tidas-data-import` 及 `source-evidence-dataset-development`。使用时确认名称、能力和当前安装内容，按任务选取，不全部串成固定前置步骤。

## 固定来源与版本状态

- AI CLI：检查提交 `fee12538aca7bc35f8dfb0da3bbd7d5836a61124`，包版本 0.0.63，声明 Node `>=24 <25`。[package.json](https://github.com/tiangong-ai/cli-toolkit/blob/fee12538aca7bc35f8dfb0da3bbd7d5836a61124/package.json)
- AI Skills：检查提交 `5e692461708a4e02c1012086a59d2bd75da99673`。旧地址 `tiangong-ai/skills` 已重定向至 `agent-skills`；CLI 旧地址 `tiangong-ai/cli` 已重定向至 `cli-toolkit`。[README](https://github.com/tiangong-ai/agent-skills/blob/5e692461708a4e02c1012086a59d2bd75da99673/README.md)
- LCA CLI：先前检查提交 `ba286d42db5a48f8b70fd649162fb45586e7cfa2`，0.1.22 在原研究工作区做过部分本地运行。[package.json](https://github.com/tiangong-lca/cli/blob/ba286d42db5a48f8b70fd649162fb45586e7cfa2/package.json)
- LCA Skills：先前检查提交 `5cfd434bd1edcec52f819eeddd22a912932611ee`；其普通包装器声明 CLI 0.1.20，Foundry 入口有独立版本组合。这不构成与 CLI 0.1.22 的组合验证。[执行约定](https://github.com/tiangong-lca/agent-skills/blob/5cfd434bd1edcec52f819eeddd22a912932611ee/README.md#L163)

上述是观察基线。首次使用一个操作前确认其实际契约和版本；通过小型输入输出检查后再记录该组合可用。每个外部工具保留自己的版本归属，避免把一个工具的版本锁套到另一个工具上。

## 适配边界

2026-09-27能力核对：官方原生模型已有过程实例、版本引用、倍率及连接；CLI 0.1.22 已完成本地两过程汇总、倍率变化和汇总过程再次组合。实际组内部署入口未确认，当前 CLI 未登录，在线读取私有数据、保存及环境计算未验收。实例参数字段存在，但所查平台图序列化写入空参数，矩阵计算负载未传入实例参数；当前不能承诺独立改参自动传播。

适配时须检查两个具体差异：平台计算优先使用 `resultingAmount`，CLI结果过程命令优先使用 `meanAmount`；CLI指定版本读取可能回退最新版，而上游草稿可在同版本号下修改。保留实际返回版本、内容状态及必要的散列/快照。递归能力已实测到“汇总过程作为下一层节点”，任意多层展开、边界映射和变更传播仍待验证。

组内现有 LCA 数据作为详细清单的优先数据源，现成过程、流、单位、参考量、交换和参数通过其接口引用和读取，沿用既有定义。本地工艺目录按[目录约定](module-catalog.md)保存功能、接口及条件摘要，支持跨工艺探索；摘要可以来自上游已有字段，保留来源和刷新信息。详细内容按需缓存，不要求复制整个 LCA 库。

来源工具输出原文和候选证据；本项目 辅助形成模块卡片和递归过程草稿。文献/专利中的工艺可以先进入目录，逐步补充清单和外部映射；形成完整过程时优先采用现有 TIDAS 等格式。工艺模块与 LCA 记录按支持范围多对多关联，原生模型之外的操作缺口再扩展。

本项目待核实职责集中在引用复现、实例覆盖、边界映射、模型分支与重组操作。这些是需求清单，尚未全部确定为新增实现。已有上游命令能满足时直接调用；缺口先通过有界契约检查确认，再实现必要脚本。上游数据接口和认证继续由上游负责。

方案内容能够保存在现有生命周期模型、过程或其他组内接口中时，复用其存储和版本能力。本地卡片及派生索引服务于探索，工作区也保存原创判断和必要方案扩展；具体检索与存储后端由实际使用确定。

输出时将所选本地方案整理为目标支持的过程或模型文件，再调用已有校验与计算能力。层级与映射单独保留。TIDAS 接收成功、LCA 成功计算与新工艺模型的证据充分程度分别记录。

## 运行与组内复用

将本项目源码放到组内开发环境，保留 src 与 scripts 的相对结构，外部依赖由组内环境提供或按其机制安装。仅记录非敏感配置与实际依赖身份；凭证和账号会话由既有环境/CLI 管理。调用现有上游命令前读取其实际说明，避免推测参数。

研究工作区和数据目录显式传入，命令可以从其他当前目录调用。不镜像上游 Skill 全文或实现，不把个人路径或 HTAM 材料写成运行依赖。按上游许可引用和复用；本仓库自身发布范围另行确定。

本项目 默认采用普通建模工作流。依赖列表不包含自动启用 Auto Research 的步骤，也不把外部项目的 gate、预算或历史授权传入当前任务。
