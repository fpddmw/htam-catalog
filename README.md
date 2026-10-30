# HTAM 源码备份（2026-10-30）

本分支保存转用 data-workspace 前的 HTAM 通用源码。它从 GitHub 原 main 的 `399412ceae84c410bf9aa89af5066f7583948233` 建立，纳入当时本地尚未提交的实现；原 main 和正在使用的本地工作目录保持原状。

## 保存范围

- `harness/`、`scripts/`：管理核心、原生会话适配、知识与资产、反馈维护、发布打包等程序。
- `tests/harness/`：管理核心现有测试。
- `workspaces/industrial-reorganization/`：已迁址的目录工具源码、通用脚本、测试、打包配置及原已公开示例。
- `docs/catalog-baseline/`：已公开的原版本使用说明，只保留其历史字节，不作为当前研究状态。
- `source-backup-manifest.json`：115 个原样复制文件的逐项 SHA-256、大小及来源版本，标明这是本地工作树源码快照。

个人指引、Wiki、科研配置、专属研究代码和材料、管理数据库、原生任务状态、凭据、安装目录与运行记录继续保存在各自原位置。这是通用源码备份，不是完整研究环境或运行数据备份。原 `harness/scopes.json` 及操作手册包含私有研究上下文，未公开；可参考无业务内容的 `harness/scopes.example.json`。

## 检查入口

在本分支根目录运行管理核心测试：

```sh
python3 -m unittest discover -s tests/harness -p 'test_*.py'
python3 scripts/harness.py --help
```

目录工具从其现有子目录运行：

```sh
cd workspaces/industrial-reorganization
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/catalog.py --help
```

Python 与原生宿主适配分别验证；部分测试需要 Node.js。测试使用临时目录和测试夹具，不证明真实宿主、研究任务或线上工具已接通。

本快照验证：管理核心 305 项、目录工具 87 项测试全部通过；115 个复制文件与本地源文件逐项哈希一致。提交共 129 个文件，包含必要的备份说明和原已公开历史指引，通过范围、语法、敏感内容及 Git 差异检查。

## 后续接续

用户已确定后续工作转用独立的 data-workspace。该实现确认版本为 `036eb94a4d854bc598c8cdce765daec42adf77d5`，包含老师最新代码及完整任务改造、审阅修复；它与本分支保有各自的 Git 历史。

HTAM 原科研资产、管理状态和在途工作仍由原工作区保管。后续按具体用途引用原件、核对责任及运行环境，再接入 data-workspace。本分支没有迁移管理库、恢复旧任务、启用自动化或变更生产部署。
