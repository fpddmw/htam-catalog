# HTAM Catalog · 递归工艺模块目录

用于工业过程与潜在重组路径探索的本地基础设施。模块保存工艺功能、输入输出接口、条件与来源，也可以包含其他模块；详细 LCA 清单通过外部引用按需关联。

当前版本实现本地目录操作，采用普通 Python 项目结构，运行时仅依赖标准库，无需 AI 或在线服务。

**当前建设重点是批量数据流水线**：从可访问的天工过程和流记录自动提取工艺信息，持续同步、建立索引，并提供可追溯的查询与潜在连接匹配。第一版已支持天工 CLI 只读分页、离线导入、增量更新、断点恢复和本地检索；真实账号的批量验证仍待完成。使用方法见[批量同步](docs/catalog/tiangong-batch-sync.md)，阶段安排见[建设计划](docs/catalog/implementation-plan.md)。

## 已有功能

- JSON 工艺卡片校验、保存、读取与不可变修订历史。
- 来源提取与本地补充分层；来源刷新保留本地补充并报告变化。
- 功能、类别、输入输出等字段检索与可重建索引。
- 固定子模块修订及表达的递归展开、实例路径、边界映射与包含环诊断。
- 接口候选检索及已声明标量条件检查，返回满足、不满足或未知。
- 天工原生生命周期模型 JSON 的只读结构检查，提取过程实例、版本、倍率和连接。
- 天工过程与流的批量同步、工艺字段投影、SQLite 索引、来源与失败记录、初步流身份连接查询。

当前参数可作为数据记录；参数公式求值、完整方案编辑、自动多步重组和在线 LCA 计算仍待开发。候选结果的适用范围取决于已记录的条件与来源。

## 快速使用

需要 Python 3.10 或以上。在项目根目录运行，演示使用一个新的空工作区：

```sh
python3 scripts/demo_catalog.py --workspace outputs/process-catalog-demo
python3 scripts/catalog.py --workspace outputs/process-catalog-demo search --function 接收 --input 矿物固体
python3 scripts/catalog.py --workspace outputs/process-catalog-demo candidates demo-dryer --port product
python3 scripts/catalog.py --workspace outputs/process-catalog-demo expand demo-line
python3 scripts/catalog.py inspect-tiangong-model --file examples/tiangong/synthetic-lifecyclemodel.json
```

演示导入 9 张卡片并生成 JSON 报告。其中 8 张为构造样例，1 张为天工公开过程身份与名称摘要；这些样例用于验证软件行为，尚未构成真实工艺可行性或环境收益验证。

`--workspace` 显式指定数据位置，可以使用项目内或外部路径。实际工作区与派生索引保存在本地，详细数据不会自动从天工下载。

## 项目结构

```text
src/htam_catalog/       核心实现与 Python API
scripts/catalog.py     从源码直接运行的命令入口
scripts/demo_catalog.py 样例导入与演示
scripts/tiangong_sync.py 天工批量同步与检索
examples/catalog/      带来源和证据状态的样例卡片
examples/tiangong/     原生模型结构的构造样例
tests/                 行为测试
docs/catalog/          格式、设计、天工复用与开发计划
pyproject.toml         Python 包元数据
```

源码入口无需安装包。Python 调用方可从 `htam_catalog` 导入 `Catalog`；源码开发时将 `src` 加入模块搜索路径。包元数据也声明了 `htam-catalog` 命令入口。

## 测试与文档

```sh
python3 -m unittest discover -s tests -v
```

当前 27 项行为测试覆盖卡片修订与完整性、索引恢复、递归引用、天工模型结构检查，以及批量同步的幂等、断点恢复、坏行隔离、检索和连接分类。实际验证环境为 Ubuntu 24.04 / Python 3.12.3；其他环境需要进一步验证。

- [命令与卡片格式](docs/catalog/catalog-cli.md)
- [模块目录语义](docs/catalog/module-catalog.md)
- [递归模型设计](docs/catalog/model-contract.md)
- [AI 与程序的执行分工](docs/catalog/execution-boundary.md)
- [建设计划](docs/catalog/implementation-plan.md)
- [天工复用边界](docs/catalog/tiangong-reuse.md)
- [组内接入约定](docs/catalog/group-integration.md)
- [天工原生模型只读检查](docs/catalog/tiangong-model-inspection.md)
- [天工过程批量同步](docs/catalog/tiangong-batch-sync.md)

## 数据与外部工具

本地目录保存工艺摘要、接口和引导字段；现有天工 CLI/Skills 可按任务复用，详细清单与模型通过其实际接口关联。依赖版本与已核验范围见开发文档，在线集成仍待验证。

本仓库仅发布代码、样例和开发文档。研究原始资料、完整数据库、运行工作区和历史归档在本地维护。公开过程摘要保留原始页面引用；第三方资料的权利与使用条件沿用来源。本项目目前尚未指定开源许可证。
