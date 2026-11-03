# 采样种子与尝试序号解耦（审计 P3 修复）

本文件记录一次针对统计口径缺陷的修复：**模型采样种子原先等于「运行种子 + 尝试序号 − 1」**，导致 `--seeds 0 1 2 3 4` 的五次「独立」运行在修复轮次上共用采样种子。修复前的实测与影响分析见工作区审计报告 `../../tmp/audit/AMD3.1_审计报告.md` 的 P3 节。

## 问题

`agent/core.py` 原先对每次生成发送 `seed + index - 1`。当 `max_attempts=3` 时：

| 运行 | 运行种子 | 各尝试实际使用的采样种子 |
| --- | --- | --- |
| run 0 | 0 | 0, 1, 2 |
| run 1 | 1 | 1, 2, 3 |
| run 2 | 2 | 2, 3, 4 |
| run 3 | 3 | 3, 4, 5 |
| run 4 | 4 | 4, 5, 6 |

15 次尝试只覆盖 7 个采样种子，seed 2/3/4 各被使用 3 次。去重逻辑保留这些记录是正确的（运行身份 `paired_run_id`/`seed` 确实不同），但估计量声明的独立性在**采样层面**已经不成立：名义样本数量高于真实独立信息量。该缺陷只在 agent 真正走到修复轮次时显现。

## 修复

- 采样种子改为 `seed * 1000 + (attempt - 1)`，常量 `SAMPLING_SEED_STRIDE = 1000` 与函数 `sampling_seed()` 定义在 `agent/core.py`。
- `seed` 仍是运行种子，作为运行身份参与 `task_id/task_digest/config_digest/mode/seed` 去重与配对，语义不变。
- 每次生成实际发送的采样种子逐条写入 `result.json` 的 `attempts[].seed`，并在 `sampling_seeds` 汇总；`attempt_XX/request.json` 的 `seed` 字段同步。**因此种子复用与否可以直接从证据核对**，不再依赖对公式的信任。
- `load_config()` 拒绝 `max_attempts > SAMPLING_SEED_STRIDE`，避免尝试序号越过进位基数后重新落到别的运行的编号段。
- `scripts/metrics.py` 的 `estimator.independence` 改为点名该派生规则，不再只写「assumed」。

修复后同一实验的 15 次尝试覆盖 15 个互不相同的采样种子：

```
run_seed | model sampling seeds actually sent
  0      | [0, 1, 2]
  1      | [1000, 1001, 1002]
  2      | [2000, 2001, 2002]
  3      | [3000, 3001, 3002]
  4      | [4000, 4001, 4002]
```

## 回归覆盖

- `tests/test_core.py::test_sampling_seeds_do_not_collide_across_independent_runs`：跑 5 个运行种子的**真实修复循环**（验证器恒判失败以跑满预算），断言 15 次尝试的采样种子互不相同，且与 `result.json` 记录一致。
- `tests/test_core.py::test_sampling_seed_keying_is_injective_over_supported_seeds`：枚举 100 个运行种子 × 1000 个尝试序号，确认映射无碰撞。
- `tests/test_core.py::test_attempt_budget_cannot_exceed_the_seed_stride`：越界配置被拒绝。

## 证据影响

修复改变的是采样种子，因此**修复前记录的运行不能与修复后的运行混池**：旧记录的尝试种子仍按旧公式派生。当前 `outputs/`、`metrics/` 下的全部记录均为 `mock`（`eligible_runs` 为 0），未进入任何性能统计，因此没有已发布的通过率需要撤回。真实评测开始前应重新生成全部记录。

公式是确定性函数（种子 = `seed * 1000 + 尝试序号 - 1`），因此旧记录是否使用旧公式可由其 `attempts[]` 是否带 `seed` 字段识别，无需另行标注版本。
