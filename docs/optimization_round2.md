# 2026-10-09 第二轮功能完善与稳定性强化

本轮在现有 RTL / HLS 独立流程上改进软件实现。未执行真实模型推理、Vivado/Vitis 或官方容器；软件测试、HTTP 假服务与宿主 C++ 冒烟均不能证明比赛通过率或模型增益。

## 新增功能

批处理支持 `--plan-only`：加载并校验任务、配置、唯一种子和回放输入，保存题目与配置快照，输出计划中的配对数、模型调用次数上界及串行配对预算；不调用模型或 EDA。`batch.json.status=planned`、`evaluation_kind=unobserved`。预算是配置上界，不是运行时间预测，进程清理可能额外耗时。预检目录保留原样，正式执行时去掉 `--plan-only` 并选择新输出目录；当前不提供原目录断点续跑。

```bash
python3 scripts/batch.py --tasks examples/counter.json --seeds 0 1 2 3 4 --output outputs/round2_plan --plan-only
python3 scripts/batch.py --tasks examples/counter.json --seeds 0 1 --output outputs/round2_replay --replay examples/replay.json
```

批报告新增 `summary.json.coverage`，逐一核对计划中的 baseline/agent 结果：

| 字段 | 含义 |
| --- | --- |
| `expected_pairs / expected_results` | 计划配对数 / 两种模式的结果总数 |
| `present_results / missing_results` | 已落盘 / 缺失结果，缺失项附任务、seed、模式与执行状态 |
| `rejected_slot_results` | 无法读取、解析或与任务/配置/seed/模式/赛道不匹配的结果 |
| `eligible_results / eligible_pairs` | 通过统计校验的真实确定结果 / 完整配对 |
| `eligible_result_fraction` | 可用结果占计划结果的比例，不是通过率 |
| `full_real_evaluation` | 所有计划结果都可评估且全部配对成立，才为 true |

单个子进程退出 0 只说明执行完成；mock、缺 EDA、缺结果或配对不完整时不能声称完整真实评测。批处理在启动每一对子进程前持久化 running 状态。没有身份匹配的来源记录时用 `unobserved`，请求使用普通 HTTP 本身不证明真实评测。

统计新增独立的 `diagnostics`，按模式和来源列出停止原因、验证状态、顶层错误记录数、已记载尝试次数及选中尝试分布。它包含 mock、未知状态、重复和无效输入记录，用于排障，不进入 pass@k。批处理先拒绝不匹配的槽位，拒绝数单列为 `batch_slot_rejections_not_in_diagnostics`；单独运行 metrics 则诊断所有传入文件。attempt 次数不是独立样本数，HTTP 未完成响应的调用也不能从该字段推定。

## 自动修复与判定

两套智能体均将诊断裁剪为最多 2000 字符，保留开头的阶段与原因、末尾日志，并明确标记中间省略；完整日志仍保留。上下文仍使用保守字节预检，超限会报错，不自动改写原题。长候选的上下文适配仍待真实模型验证。

重复代码不会再次调用 EDA，但该次模型响应仍写入 attempts，保存 usage、finish_reason、SHA-256 和 `duplicate_of_attempt`。最高已验证阶段的候选继续保留。直接调用生成函数仅允许 agent/baseline 模式，错误模式在生成文件前拒绝。

RTL 新输入和结果明确标记 track=rtl，拒绝显式 HLS 任务、配置和批清单。统计兼容历史缺 track 的 RTL 记录，但拒绝显式其他 track；passed 必须与最高阶段 3 一致，避免把 HLS 的阶段数字解释为 RTL 成绩。

RTL HTTP 增加请求全过程的绝对时限，覆盖连接、响应头、慢响应体和解析；超时关闭响应并恢复信号处理器。实现与 HLS 一致，仅支持 Linux/macOS POSIX 主线程；已有实时定时器或非主线程调用会明确拒绝。容器资源限制仍需在真实部署时验收。

RTL 默认 marker 仿真即使出现 TB_PASS、退出 0，也会拒绝 FATAL_ERROR / ERROR 工具诊断，失败后不会综合。已完成的空白、非文本或空代码块响应按 output_rejected / failed / stage 0 计入失败样本，保留 usage 和来源；智能体可继续修复，纯基线保持一次生成。服务请求未完成仍归未知环境结果。

## 本轮验收

本机软件单测 **163 项**（含采样种子解耦与测试台内容摘要回归；Windows 上 POSIX 专属用例按环境失败，Linux 验收仍待执行），一键自检 **10/10 步骤成功**，包括环境清单、单题回放、批预检、批回放、题集完整性验证及本机回环 HTTP 配对/批处理。

原始日志与汇总保存在 `metrics/optimization_round2/`，完整运行目录为 `outputs/optimization_round2_verified_20261009/`。历史 `metrics/` 根目录记录保留为旧版证据，不覆盖成新成绩。

新增回归覆盖：预检不发请求、预算快照、缺失/错配结果、seed 类型、配对 ID 不符、跨赛道混入、坏统计输入、长诊断根因、重复候选记账及轨道专项反例。一键自检现在同时验证预检和批结果覆盖率。所有 replay/HTTP mock 的 eligible_runs 均为 0。

测试台摘要回归另加 6 项：原地改写测试台改变 `task_digest`、声明哈希不符即拒绝、恢复原字节即恢复快照、缺失测试台不在加载阶段伪造身份、复制后哈希不符返回 `unavailable` 且不启动工具、`asset_sha256` 结构非法在启动工具前拒绝。这 6 项在修复前全部失败（`KeyError: 'asset_sha256'` 与 `'passed' != 'unavailable'`），修复后通过。

审计 P3 的采样种子复用缺陷已修复：运行种子与尝试序号解耦，见 [采样种子解耦](seed_decoupling.md)。

## 审计 P2：RTL 任务摘要不含测试台内容

RTL 此前只用路径计算 `task_digest`，原地改写测试台后摘要不变（实测 `024cdb38…` → `024cdb38…`），而 HLS 因 `asset_sha256` 会变。后果是内容不同的测试台共用同一身份，混进同一个 pass@k 池并互相配对，且没有任何信号。

已把 HLS 的机制移植到 RTL：

| 位置 | 行为 |
| --- | --- |
| `agent/core.py` `load_task` | 对每个 `testbench_files` 计算 SHA-256 并写入 `task['asset_sha256']`，摘要随之变化 |
| `agent/core.py` `load_task` | 声明了 `source_sha256` 时按文件名核对，不一致即拒绝；声明后不可读按错误处理 |
| `agent/eda.py` `_copy_assets` | 复制到工作目录后重新计算目的文件哈希，与 `asset_sha256` 不符返回 `unavailable`（阶段 0），工具不启动 |
| `agent/eda.py` | `asset_sha256` 结构非法（非对象、非 64 位小写十六进制）同样在启动工具前拒绝 |

缺失测试台仍交给验证器报告：未声明 `source_sha256` 时不在加载阶段制造内容身份，也不改变既有"测量真实任务可无测试台"的行为。全部 156 个 VerilogEval 导入任务通过核对，导入器声明的 `source_sha256` 从此真正生效。跨版本实验要分别保存：摘要已变化，不能自动并入旧记录。

## 交付与后续

`local_manifest.json` 已更新为本轮开发快照；新包为 `../output/rtl_agent_local_v0.2.zip`，原 v0.1 包继续保留。交付包不含真实模型或完整官方环境。新包解压后先校验清单，再按 README 运行自检。跨版本实验要分别保存；RTL 新加载任务补入 track 字段，task_digest 会变化，不能自动并入旧记录。

后续仍需 Linux + AMD GPU、固定模型权重、Vivado/Vitis 2025.2、单卡 32 GB 实测、官方 I/O/镜像以及同模型真实配对实验。本轮只证明软件行为与证据边界得到加强，不宣称 pass@1/pass@5 已提高。
