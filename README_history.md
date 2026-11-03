# 历史 README（封存于 2026-11-01）

当前操作请阅读 [当前部署说明](docs/current_deployment.md)。以下内容仅作历史追溯。

# 3.1 RTL 本地智能体：计划与首版工程

2026-11-01：本目录已作为统一开发和交付入口。当前配置、整合决策和复现步骤以 [统一交付说明](docs/unified_release_20261101.md) 为准；下文较早日期内容保留为历史记录。

2026-10-14 审计修复完成：全套 170 项，零失败、零错误，Windows 上 21 项环境能力用例跳过。当前开发包为 `../output/rtl_agent_local_v0.3.zip`。问题处置、统计口径与仍待真机校准的预算见 [审计修复记录](docs/audit_fixes_20261014.md)。测试规模以 `unittest discover` 实测为准。

状态：**详细计划已制定，本机软件流程可验证；尚未完成真实模型、Vivado 或官方容器验收。** 用户已确认选择 RTL 单 track，先完成本机首版。

先读 [详细步骤计划](docs/plan.md)，再看 [要求与验收矩阵](docs/requirements.md)。计划依据原指南 3.1（PDF 第 9–12 页，页脚 6–9），官方后续细则优先。本目录的任务 JSON、时间预算和统计方法是开发约定，不是已公布的赛事接口。

完整交付范围与后续外部输入见 [本机完成状态](docs/local_status.md)。

移交包解压后进入 `rtl_agent/`，先执行 `python3 scripts/freeze.py verify . local_manifest.json`，再执行下方自检。压缩包保留数据、许可和测试证据，排除 Git 元数据及运行缓存；重新导入公开题集时应另备该固定版本的干净上游仓库。

## 已实现

- 仅连接本机回环地址的模型客户端；不向云端发送题目，无自动联网下载。
- `run.sh`：读题、加载 RTL 规则、生成、逐级 EDA 验证、有限修复、保留已验证层级最高的候选。
- `run_baseline.sh`：同配置，提示词仅题目，一次生成，不读取技能、不调用 EDA、不重试。
- `run_pair.sh`：同进程、同服务、同配置、同题目/种子产出两者；独立于基线生成步骤评分。保存总墙钟和配对 ID。
- Vivado 2025.2 适配代码：编译/展开、仿真、综合，器件 `xczu3eg-sbva484-1-e`、约束 5 ns。**该路径仅做了替身测试，未运行真实 EDA。**
- 日志、配置快照、失败状态、结果汇总和 SHA-256 冻结工具。
- 已导入固定版本的 VerilogEval v2：156 题，开发集 124、保留集 32；保留来源哈希和许可。
- 串行批量配对、多种子、失败继续与超时清理；公开测试台协议适配。
- 一键自检与明确标记 mock 的本机 HTTP 服务，可验证真实 HTTP 传输而不冒充模型推理。
- 候选模型声明、推理启动草案、容器模板、设计报告初稿。

## 本机复现（macOS/Linux、Python 3.10+、Git、Bash）

运行框架使用 Python 标准库；完整自检中的数据导入测试需要 Git，`.sh` 入口需要 Bash。先进入解压或下载后的 `rtl_agent` 目录，再执行以下命令。

```bash
python3 -m unittest discover -s tests -v
python3 scripts/doctor.py
./run_pair.sh --task examples/counter.json --output outputs/my_smoke --replay examples/replay.json
python3 scripts/metrics.py outputs/my_smoke
python3 scripts/import_verilogeval.py --verify data/verilogeval
python3 scripts/batch.py --tasks examples/counter.json --seeds 0 1 --replay examples/replay.json --output outputs/my_batch
python3 scripts/selfcheck.py --output outputs/my_selfcheck --http
```

输出目录必须不存在，防止覆盖已有证据。`--replay` 回放人工编写的固定回答，用于软件冒烟测试；结果标记 `mock`，EDA 不执行，汇总器不会把它计入真实成绩。示例计数器测试台覆盖同步复位优先级、使能保持、递增和回绕，尚待真实仿真验证。

统计独立完整运行时显式传入不同 `--seed`；重复默认种子不会被计为五个独立样本。不能把一次 agent 的内部修复次数当成 pass@5。题目内容摘要不同的结果分开统计。运行种子与尝试序号已解耦：每次生成实际使用的采样种子为 `seed * 1000 + (尝试序号 - 1)`，逐条记录在 `result.json` 的 `attempts[].seed` 和 `sampling_seeds`，因此 `--seeds 0 1 2 3 4` 的修复轮次不会再互相复用采样种子。

完整自检需允许绑定本机回环端口；受限沙箱中可省略 `--http`，其余检查仍可执行。当前 149 项软件测试，含真实子进程取消清理测试；它们不代表模型能力或 HDL 通过率。公开数据使用与批量运行详见 [题集说明](docs/dataset.md)。

## 接入真实环境

先完成 [部署验收](docs/deployment.md)：获得官方镜像/协议，准备 AMD 单卡 Linux、Vivado 2025.2、兼容该 GPU 的推理后端、本地模型权重和许可环境。不要用本机测试结果填入比赛通过率。

```bash
# 在已安装并验证 ROCm / vLLM 的目标 Linux 中执行；不是本机安装命令。
export MODEL_PATH=/opt/models/selected-model
export EXPECTED_VLLM_VERSION=已实测并锁定的版本
# 先更新 serve/config.json 的 model_revision，另一个终端启动：
bash serve/start.sh
# 在同一容器/网络命名空间中；先加载 Vivado 2025.2 环境：
./run_pair.sh --task examples/counter.json --output outputs/real_counter
```

无测试台时仍可做编译反馈，但仿真状态是 `unverified`，不能记为功能通过，也不继续综合。首版仅接受有明确通过标记的可信开发测试台。官方隐藏测试台、参考实现和评分器是否向智能体开放尚未确定；不可预设访问权限。

## 目录

| 位置 | 内容 |
|---|---|
| `docs/plan.md` | 工作包、阶段排期、验收条件 |
| `docs/requirements.md` | 规则映射与待确认事项 |
| `docs/interface.md` | 开发输入、输出与状态语义 |
| `docs/dataset.md`、`data/verilogeval/` | 题集、哈希校验、开发/保留划分 |
| `agent/` | 客户端、控制流、EDA 适配与命令行 |
| `skill/` | 首版 RTL 规则及验证边界 |
| `serve/`、`model/MODEL.md` | 服务配置、启动草案与模型声明 |
| `scripts/` | 环境检查、统计、冻结工具 |
| `tests/`、`examples/` | 软件测试与开发示例 |
| `metrics/` | 本机检查证据，不包含比赛成绩 |
| `REPORT.md` | 设计报告初稿、实验计划和未验证项 |

## 尚需完成

真实模型权重及固定版本、公开数据集真实批量评测、技能消融、显存峰值采集、W7900/R9700 实测、官方 I/O 适配、推理依赖锁定及完整离线容器构建、最终报告和冻结验收。当前 Dockerfile 是明确标注的模板，没有包含模型权重或推理依赖，不能直接作为完整参赛交付物。公开题集与参考答案是开发资产，通过 `.dockerignore` 排除在该运行镜像模板外。
