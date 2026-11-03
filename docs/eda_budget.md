# EDA 时间预算：为什么必须先实测

本文件记录 P6（RTL 时间预算偏紧，可能压制通过率）的完整分析、已验证的代码改动，以及把预算定稿所需的实测流程。**在真机 Vivado 2025.2 实测完成之前，`serve/config.json` 的数值保持不变**，本文件不提供替代数值。

## 1. 现状

| 项目 | rtl_agent | hls_agent |
|---|---|---|
| `run_timeout_s` | 180 | 600 |
| `tool_timeout_s` | 60 | 300 |
| 一次 EDA 评估内共享该额度的工具调用 | **5 次**：`vivado -version`、`xvlog`、`xelab`、`xsim`、`vivado -mode batch` | 1 次 Vitis/Vivado HLS 调用 |
| 最后一级 | `synth_design`（最慢，且**排在最后**） | 综合包含在同一次调用内 |

两个 track 相差 3.3 倍本身不是问题——问题是没有依据说明，而且结构上不可比。用"把 HLS 的 600/300 抄给 RTL"来消除差异是错的方向：HLS 的 300 秒覆盖**一次**调用，RTL 的 60 秒要覆盖**五次**调用中的全部阶段。RTL 需要的额度在结构上必然高于 HLS 的单次调用额度。

## 2. 结构性缺陷（与数值无关，已确认）

`agent/eda.py:validate_rtl` 把同一个 `deadline` 传给全部五个工具调用，并按顺序执行。由此产生的后果与额度大小无关：

1. **综合永远最后一个拿到时间**。`version → xvlog → xelab → xsim → synth_design` 顺序执行，前四步花掉多少，综合就少多少。
2. **版本探测先扣一笔固定开销**。`vivado -version` 要装载 Vivado 的 Tcl 运行时，通常十几到二十几秒，纯属环境校验，不产生任何设计信息，却和综合抢同一个 60 秒。
3. **超时后第三级不是"未通过"，而是"根本没跑"**。`_run_command` 在 `remaining <= 0` 时直接返回，综合进程连启动都没有。

第 3 点的后果分两层，**两层方向相反，必须分清**：

- 对 agent 的修复循环而言，这是**虚假的反馈**：模型看到"综合未通过"，但真实原因是环境额度不足，它会朝错误方向修。
- 对指标口径而言，**改动前后都必须算作不通过**（详见第 4 节）。

### 与 P7 的叠加

`scripts/import_verilogeval.py:147` 只认字面 `clk` 作为时钟端口，未识别时综合脚本退化为虚拟时钟。这不改变本文件的结论，但意味着"综合通过"这一级的证据强度依赖端口探测的覆盖率。

## 3. 实测流程（预算定稿的唯一依据）

`scripts/eda_budget.py` 用**与生产完全相同的** Tcl 与命令行测量每一级的墙钟：

```bash
# 1. 先看内置探针题（一个真实的乘法/累加数据通路），确认流程跑通
python3 scripts/eda_budget.py --output metrics/eda_budget_profile.json

# 2. 再按开发集抽样，重复多次，拿到可用的上分位
python3 scripts/eda_budget.py --repeats 5 \
    --task data/verilogeval/tasks/Prob001_zero.json \
    --task data/verilogeval/tasks/Prob035_count1to10.json \
    --task data/verilogeval/tasks/Prob091_2012_q2b.json \
    --output metrics/eda_budget_profile.json
```

脚本行为：

- 逐级计时 `compile` / `simulate` / `synthesize`，每轮使用**全新目录**，杜绝旧网表被当成"很快的综合"；
- 阶段预算 = 观测值的 **p95 × 1.25**，向上取整到 10 秒；`tool_timeout_s` = **各级阶段预算之和**（因为共用同一 deadline），不是取最大值；
- `run_timeout_s` = `tool_timeout_s × max_attempts`，下限 30 秒；
- 与当前配置对比并**明确判定"当前配置在实测管线下不可行"**；
- 默认**只写证据文件，不写配置**；只有 `--apply` 才修改 `serve/config.json`，且只改那两个键、保持其余键与顺序不变；
- 工具缺失或版本不符时返回 `status: unavailable`、退出码 3，**不产出任何数字**。

实测前必须先读 `metrics/eda_budget_profile.json` 里的 `warnings`：样本数不足、复次数不足、阶段未跑完都会被点名。

## 4. 判定口径（本文件之外唯一需要的约定）

超时**必须**同时满足两个条件，二者不可偏废：

1. **计入不通过**。第三级拿不到就是拿不到，官方"题集加权通过率"不会因为原因是额度不足而放过它。若记成 `unverified` 而被 `scripts/metrics.py` 排除出分母，通过率会被**系统性抬高**——这是比预算偏紧更严重的问题，因为它让缺陷不可见。
2. **不得与设计错误混为一谈**。额度不足说明的是环境配置，不是候选代码有问题；两者混在一起会污染 30 分项的失败诊断。

`validate_rtl` 现在对两者同时满足：超时结果是 `status: "failed"`（因此进入分母、且永远不可能成为 `highest_stage: 3`），同时附带

```json
{ "failure_cause": "budget_exhausted" | "tool_timeout", "budget_exhausted": true }
```

- `failure_cause = "tool_timeout"`：进程已启动但吃光了 deadline；`budget_exhausted` 仅在**综合**这一级为 `true`。
- `failure_cause = "budget_exhausted"`：进程在启动前就已无时间可用。
- 设计错误（编译/仿真/综合报错）**不写**这两个字段。

三个 stage 记录本身的键保持不变（`name`/`status`/`reason`/`wall_s`/`log`），因此既有冻结证据与下游解析不受影响。

## 5. 定稿时必须一起考虑的代价

预算不是越大越好，`run_timeout_s` 直接进入 10 分墙钟项：

- `scripts/batch.py:pair_timeout_s` = `2 × run_timeout_s + tool_timeout_s + 30`。当前 180/60 得到 450 秒；若提到 720/240，单题配对变成 1710 秒，124 题 ×5 seed 的队列从约 8 小时涨到约 29 小时。
- 评测窗口内跑不完的题在官方口径下同样是不通过。**单题额度过大反而会压低覆盖率**，所以最终数值应由"开发集端到端跑一轮"的结果决定，而不是由本文件的推荐值直接决定。

## 6. 尚未验证

- 真实 Vivado 各级耗时：**零测量**（本机无 Vivado；`metrics/eda_environment_probe.json` 为 `unavailable`）。
- 上表"版本探测十几到二十几秒"是**估计，不是测量**，不得当作依据引用。
- 综合在目标器件 `xczu3eg-sbva484-1-e` 上对公开题集的耗时分布：零测量。
- `--apply` 之后的端到端配对墙钟：零测量。
