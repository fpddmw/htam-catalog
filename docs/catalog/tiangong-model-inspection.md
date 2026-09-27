# 天工原生模型读取：首个适配入口

更新：2026-09-27。对应[建设计划](implementation-plan.md)批次 1c 的只读部分。当前支持从本地 TianGong TIDAS 生命周期模型 `json_ordered` 文件提取用于目录映射的结构身份；可直接读取含 `lifeCycleModelDataSet` 的对象，也可读取外层为 `json_ordered` 的对象。此操作只读文件，不需要天工账号或网络连接。

```sh
python3 scripts/catalog.py inspect-tiangong-model --file examples/tiangong/synthetic-lifecyclemodel.json
```

返回模型 ID、请求版本、内容散列、参考过程实例、每个实例的内部 ID、过程 ID/请求版本、倍率、参数字段是否出现，以及实例之间声明的流连接。`structurally_consistent` 只表示所检查的字段没有发现结构问题。诊断包括未固定过程版本、重复实例 ID、缺少倍率、下游实例缺失、连接流 ID 不一致等。

示例文件来自此前[天工 CLI 本地构造试验](../tiangong-capability-verification-20260927.md)的标准化输出；所有 ID 和数量都是构造值。文件保存原生 `json_ordered` 结构，可用于无账号环境下检验读取契约。当前代码不解析或修改过程清单，不求值参数，也不把模型实例自动等同于本项目工艺卡片。模型中引用的过程数据仍需按实际 ID、版本取得，核实返回版本与内容后才能建立映射。

批次 1c 接下来的操作仍按原计划：用可访问的真实过程及原生模型检查读取、保存重开、实例改参和内层变化传播。当前本机 CLI `auth status --json` 返回 `login-required`；所能验收的范围是本地文件读取与此前构造模型的离线汇总。实际组内部署入口及认证后的模型操作需要在对应环境继续核验。
