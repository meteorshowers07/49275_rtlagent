# 截图审计问题修复记录（2026-10-14）

本轮针对截图中的 P1–P7，修改两个 track 的软件实现、统计和交付流程。P2、P3 在本轮开始时已经修复，本轮通过既有回归再次确认。真实 Vivado/Vitis、模型与 GPU 成绩仍未验证。

| 问题 | 处置与状态 |
|---|---|
| P1 Windows 打包丢执行位 | 两个打包器在 Windows 为 `.sh` 写入 ZIP Unix 模式 0755，其他文件为 0644；POSIX 继续保留源模式。模式校验仍在打包流程执行，内容冻结清单不承担模式认证。 |
| P2 RTL 测试台内容摘要 | 已有 `asset_sha256` 纳入 `task_digest`，声明的上游哈希和验证器复制快照检查生效；本轮回归通过。 |
| P3 跨运行采样种子复用 | 已有 `run_seed * 1000 + attempt - 1`，每轮实际种子留证且尝试数不超过 1000；本轮回归通过。 |
| P4 缺整体 pass@k、未知结果退出分母 | 新增 `track_summary`：按同配置、同模式分组，等题权平均逐题 pass@1/pass@5；任一题不足 5 个样本，整体 pass@5 为 null，附可估计题数。新增 `planned_performance`：按 batch 计划的全部 task/seed/mode 槽统计，缺失、未知、被排除槽算作无已验证成功，保留完整分母及覆盖数；这是保守开发估计，不是把环境错误诊断成设计失败。 |
| P5 mock 来源绑定 | 结果写入 HTTP 端点、端点 SHA-256、模型声明或 replay 原始字节 SHA-256，并哈希来源对象。批处理保存实际来源，与每个结果交叉核对；metrics 对照配置快照和响应 mock 标记。缺来源、来源变更、replay/custom 冒充 real 均不进入真实统计。 |
| P6 预算偏紧 | 已有工具超时/预算耗尽失败标签和真机校准工具；本轮阻止 agent 在 EDA 额度耗尽后继续把候选当设计错误修复。默认 180/60 保留为未校准开发值，真实耗时校准仍待 Vivado 2025.2 环境。 |
| P7 时钟只认 clk | 导入器和缺省任务加载共用公开接口识别器，支持 `clock`、`clk_i`、`sys_clk` 等唯一标量输入。显式 `clock_port` 优先，多候选不猜测。已有 `Prob145_circuit8` 元数据改为 `clock`，上游原文/RTL/测试台字节未改。 |

## 新统计的使用

```bash
python scripts/metrics.py outputs/real_batch --output metrics/real_batch_summary.json
```

传入包含 `batch.json` 的目录时，metrics 自动读取计划，核对结果所在槽和身份，生成完整计划分母。batch 自身写出的 `summary.json` 使用相同逻辑。分别查看：

- `groups`：已完成、结论性独立运行的条件估计。
- `track_summary`：已观察题组的等题权平均；不同配置和模式不混合。
- `planned_performance`：全计划的保守估计、预期/存在/缺失/有效结果数、完整性，以及全计划等题权平均。

仅输入单独结果且无 batch 计划时，`planned_performance.available=false` 明确说明计划分母未知。mock 计划不产生保守成绩。官方题目权重尚未提供，等题权平均明确标为开发约定，不声称实现官方加权得分。没有有效来源绑定的旧 real 记录会被排除；不要通过手工补字段把旧记录升级为新证据。

来源哈希用于绑定与检测不一致，不能认证服务器一定是真实模型，也无法阻止伪造整套元数据。正式证据仍需保存冻结包、模型版本与真实工具日志，并抽查复现。

## 时钟与预算的剩余验证

当前 156 道导入任务中仍有 82 条 `clock_port_undetermined`，见数据集 `provenance.json`。其中可能包含组合逻辑题，不能把数量解释成 82 个缺陷；真实综合前需区分组合逻辑与需要人工指定的时钟。未选物理端口时使用虚拟时钟，不能把该约束当作真实输入时钟的证据。

P6 数值定稿须在目标 Vivado 2025.2 上执行：

```bash
python scripts/eda_budget.py --repeats 5 --output metrics/eda_budget_profile.json
# 审查完整测量、warnings 和端到端墙钟后，使用 --apply 应用实测预算。
```

## 本轮验证与交付

Windows / Python 3.14，全套 RTL **170 项**、HLS **123 项**，均为零失败、零错误，各 **21 项 skip**。skip 分别来自 POSIX 硬超时/进程组能力与 Windows 缺符号链接权限；没有降低生产硬超时保证。符号链接用例只有遇到 WinError 1314 才跳过，其他异常仍失败。

测试运行使用既有 `tmp/audit/audit_run_tests.py` 的预建目录垫片解决沙箱临时目录限制；它不替换生产代码或验证结果。当前套件规模以 `unittest discover` 为准。原始日志保存在两个项目各自 `metrics/audit_fixes_20261014/tests.log`。

RTL 数据集再次哈希校验通过：156 题，开发 124、保留 32。重新生成两个 `local_manifest.json` 并校验，新开发包为 `output/rtl_agent_local_v0.3.zip` 与 `output/hls_agent_local_v0.3.zip`，保留此前包。新包仍不是最终参赛包；本轮通过不等于真实 EDA/模型验收通过。
