# 天工过程批量同步（第一版）

更新：2026-09-27。核心实现位于 `src/htam_catalog/tiangong_sync.py`，命令入口为 `scripts/tiangong_sync.py`。它通过天工 CLI 的只读 `process list`、`flow list` 分页读取数据，在本项目的 SQLite 文件中保存**可检索的工艺投影**。运行不调用 AI，不修改天工数据库。

## 来源与保存范围

本适配器按本机已核验的 `@tiangong-lca/cli` 0.1.22 契约读取 `rows[].process` / `rows[].flow`，并接受保存的 `--json` 列表响应或单个 TIDAS JSON 文件。CLI 返回的行包含 `id`、`version`、`state_code`、`modified_at` 和完整数据集。适配器以数据集内部 UUID、版本核对外层身份，以规范化 JSON 散列检测同版本内容变化。

SQLite 保存过程名称、过程类型、参考交换、地区、年份、行业分类、工艺描述；每条交换的方向、流 ID/版本、`meanAmount`、`resultingAmount` 与参考标记；流名称与类型；来源定位、内容散列、同版本内容变化记录、同步时间、分页进度及失败原因。完整 LCA JSON 不复制进本地库，仍以天工或导出文件为权威来源。量纲、流属性及模型边界尚未从权威引用补全；这些字段不得从交换名称推断。

## 登录与运行

在**同一个 WSL 用户**的终端里运行天工 CLI `auth login`，浏览器完成 OAuth 授权。CLI 会话保存在该用户本地。不要把登录链接、验证码或令牌发到聊天里。登录后可运行 `auth status --json`，只需检查是否已登录。

若 WSL 的 `xdg-open` 无法唤起 Windows 浏览器，本机可把一个调用 Windows `explorer.exe` 的 `xdg-open` 桥接目录放在 `PATH` 最前。这个桥接仅负责打开系统浏览器；回调仍由 CLI 在 `127.0.0.1` 接收。当前会话的临时桥接目录为 `/tmp/htam-cli-browser`。

以下命令在项目根目录运行。`CLI_JS` 指向本机已安装的 CLI；换机时更新该路径。

```sh
CLI_JS=/home/fpddmw/.npm/_npx/6f554b2d4ffb9ceb/node_modules/@tiangong-lca/cli/bin/tiangong-lca.js
DB=outputs/tiangong-sync/catalog.sqlite

python3 scripts/tiangong_sync.py --db "$DB" sync --kind flow --cli node "$CLI_JS" --page-size 100
python3 scripts/tiangong_sync.py --db "$DB" sync --kind process --cli node "$CLI_JS" --page-size 100
python3 scripts/tiangong_sync.py --db "$DB" sync-flow-refs --cli node "$CLI_JS" --batch-size 40
python3 scripts/tiangong_sync.py --db "$DB" status
python3 scripts/tiangong_sync.py --db "$DB" failures
python3 scripts/tiangong_sync.py --db "$DB" issues
python3 scripts/tiangong_sync.py --db "$DB" search '污泥'
python3 scripts/tiangong_sync.py --db "$DB" query --industry-code 24 --location CN
python3 scripts/tiangong_sync.py --db "$DB" matches --process-id PROCESS_UUID --version 00.00.001
```

默认读取 `state_code=100`。可以重复传 `--state-code` 设置其它可访问状态。`--max-pages 1` 可先试一页；继续时添加 `--resume`，保持原页大小和状态筛选。正常重跑从第一页开始，通过 ID、版本和散列更新变化记录。整条记录无法解析时进入失败表，可用 `failures` 查看原因，再用 `retry-failures --kind process --cli node "$CLI_JS"` 按 ID 重取。个别交换字段缺失时保留其余过程投影，并用 `issues` 查看跳过项。整个分页命令失败时保留下一页的偏移供恢复。

如果主要目标是补齐已导入过程所引用的流，可直接运行 `sync-flow-refs`，它从交换索引提取缺失的流 ID，按 ID 批量调用天工 `flow list`，记录已找到与未找到的引用；无需先同步天工的全部流。`--max-batches 1` 可先试一批，重跑继续未处理的 ID；`--retry-unresolved` 重新检查已有查询结果。完整流目录仍可用 `sync --kind flow` 单独同步。

离线样本可以直接导入：

```sh
python3 scripts/tiangong_sync.py --db "$DB" import-file --kind process --file /path/to/process.json
python3 scripts/tiangong_sync.py --db "$DB" import-file --kind flow --file /path/to/flow.json
```

`search` 查过程名称、行业分类、工艺描述、已知流名称或流 ID。`query` 可组合过程名称、行业代码、地区、过程类型、年份及精确的输入/输出流 ID，并用 `--limit`、`--offset` 分页。`status` 报告跳过的交换、缺少参考交换、行业分类、工艺描述、确切流记录和流类型的数量。`matches` 找同一流 ID 的输出与输入；只有双方流版本一致且流类型为产品流或废物流时标为 `shared_technosphere_flow`。基本流标为 `environmental_exchange`；缺少流类型或版本时分别标为 `flow_type_unresolved`、`version_unresolved`。这些只是**索引层的连接线索**，后续仍要核对单位、物理状态、浓度、地区、时间、容量和过程边界。

## 已验证与待验证

本地测试覆盖分页恢复、重复导入、同版本变更、坏行隔离、字段提取、格式迁移、检索和连接分类；另用已有 TIDAS 过程样本完成离线导入。真实账号已验证已发布过程的完整分页及按引用批量查询流，发现整数形式参考年份、个别缺少流 ID 的交换以及过程引用的流版本不可访问等实际差异，并已据此调整提取与诊断。实际运行数据库保存在项目的忽略目录 `outputs/`，不会随公开代码仓库发布。CLI 目前采用按 `id,version` 排序的 offset 分页；远端集合在长时间同步中变动时可能使页边界移动，首次大批量运行后应再从头同步并比对数量，后续考虑按身份游标或服务端快照。正式部署前还需持续检验远端变更频率、数据库迁移和大规模查询性能。
