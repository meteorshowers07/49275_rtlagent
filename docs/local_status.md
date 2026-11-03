# 本机交付状态

2026-10-14 当前状态：全套 170 项，零失败、零错误，各 21 项因本机缺所需能力而跳过；P1/P4/P5/P7 已补齐，P2/P3 回归确认，P6 数值仍待真机校准。详见 [审计修复记录](audit_fixes_20261014.md)。以下日期条目为历史记录。

2026-10-09 更新：第二轮软件优化已验收，**131 项单测、10 个自检步骤通过**；新证据位于 `metrics/optimization_round2/`，详见 [本轮优化记录](optimization_round2.md)。当前交付为 v0.2，以下旧日期部分保留为历史验收记录。

2026-10-13 增补（P6 处置）：新增 `scripts/eda_budget.py`（真机实测并推导 EDA 预算，默认只写证据、不写配置）、`tests/test_eda_budget.py`（16 项）与 [EDA 时间预算](eda_budget.md)；`validate_rtl` 现在把超时明确记为 `failed` 并附 `failure_cause`/`budget_exhausted`，使"预算耗尽"既能进入通过率分母、又不与设计错误混淆。新增测试后全套为 **157 项**（原 131 + `test_eda` 8 项 + `test_eda_budget` 16 项 + `test_metrics` 2 项），本机复跑为 `failures=1, errors=21, skipped=11`——**与增补前的基线错误数完全一致**，即 26 项新增测试零失败；残余错误全部为既有环境限制（POSIX `os.killpg`/硬超时、Windows 符号链接权限、Windows `chmod` 只读位）。**`serve/config.json` 的 180/60 未改动**，等待真机 Vivado 2025.2 实测。新增文件后 `local_manifest.json` 已过期，下次交付前须重新生成。

日期：2026-10-07。用户已确认 RTL 方向，当前没有 AMD Linux/EDA 环境，先完成本机可独立验证的软件部分。

## 已完成并有本机证据

| 工作 | 证据 |
|---|---|
| 七阶段详细计划、评分映射、验收矩阵 | `docs/plan.md`、`docs/requirements.md` |
| 本地 HTTP 客户端、纯基线、有限反馈修复、候选保留 | `agent/`、`run*.sh` 与软件测试 |
| 单题配对及串行多题多种子批处理 | `scripts/batch.py`、`metrics/batch_smoke.json` |
| Vivado 2025.2 三级适配与公开测试台输出协议 | 适配代码和替身测试；不含真实 EDA 证据 |
| VerilogEval 固定版本导入 | 156 题，开发 124/保留 32；`metrics/dataset_verification.json` |
| 原题、参考实现与测试台分离，来源/许可/哈希保留 | `data/verilogeval/provenance.json`、`LICENSE` |
| mock 与真实结论隔离、独立种子去重、同题摘要配对 | `scripts/metrics.py` 与对应测试 |
| 本机 HTTP 模拟服务联调 | `metrics/http_requests.jsonl`、`metrics/http_summary.json` |
| EDA/批处理取消和子进程回收 | `tests/test_process_cleanup.py` 实际进程测试 |
| 一键自检、版本清单与校验 | `scripts/selfcheck.py`、`scripts/freeze.py` |
| 模型声明、部署操作单和设计报告初稿 | `model/MODEL.md`、`docs/deployment.md`、`REPORT.md` |

`metrics/local_selfcheck.json` 中的检查成功表示软件流程通过。mock 生成内容来自固定人工样例，所有 mock 结果均从真实性能统计排除。环境探测明确返回 Vivado 工具缺失，未伪造编译、仿真或综合成功。

## 当前继续工作的外部输入

1. 可用的模型权重及选定 revision；完整依赖、tokenizer 与 chat template。
2. 可访问的 Linux + AMD GPU 环境，最终证明方案在单张 32 GB 卡完整运行。
3. Vivado 2025.2、目标器件支持和合法可用许可环境。
4. 官方基础镜像、输入输出接口、预算与最终评分细则。

上述条件具备后按计划执行真实样例、开发集、技能消融、保留集、显存/墙钟测量以及官方离线容器验收。真实成绩、模型最优性、显存合规、时序表现和参赛提交都仍未验证。

## 本机版本校验

`local_manifest.json` 是当前开发交付物的快照，**不是最终赛事冻结版本**。它包含源码、文档、数据与许可、模型声明和已保存的软件测试证据；当前没有实际模型权重或官方镜像。每次继续开发都会改变快照，需在新版交付时重新生成。

```bash
python3 scripts/freeze.py verify . local_manifest.json
```

`outputs/`、`.git/`、`__pycache__/`、`tmp/` 为可变运行目录，清单会跳过它们；关键检查证据已经复制到 `metrics/`。完整输出仍留在本地 `outputs/` 供审查。

## 可移交压缩包

`scripts/package.py` 根据已经验证的 `local_manifest.json` 生成固定元数据的 ZIP 和 SHA-256 文件；不覆盖已有包。每个归档文件重新核对内容哈希，保存脚本执行权限，并在发布前解压到干净目录复核。压缩包包含已导入的数据和上游许可，但省略上游 Git 元数据；因此压缩包中的 `third_party/verilog-eval` 是源码快照，不能直接作为要求干净 Git 仓库的重新导入源。已有 `data/verilogeval` 可离线直接验证和使用；如需重导入，应在新的目录获取并 checkout 同一上游 revision。

```bash
python3 scripts/package.py --root . --manifest local_manifest.json --output ../output/rtl_agent_local_v0.1.zip
```

压缩包与 `local_manifest.json` 一样属于本机开发版本；模型权重、官方镜像和真实性能验收没有包含在内。
