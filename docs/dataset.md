# 公开题集与本机批处理

## 当前固定数据

- 来源：[NVlabs/verilog-eval](https://github.com/NVlabs/verilog-eval)
- revision：`c498220d0a52248f8e3fdffe279075215bde2da6`
- 任务目录：`dataset_spec-to-rtl`
- 本机划分：开发集 124 题，保留集 32 题；固定种子 20261007。
- 本机核对：156 个题目，每题有 prompt、参考实现和测试台；156 个测试台均有同一类 mismatch 总结输出。
- 原始仓库：`third_party/verilog-eval`；原始 MIT 许可保留在仓库和导入资产中。

这些是公开开发数据，不能等同于官方隐藏题集。已完成下载和文件核对不代表已运行模型、仿真或综合，也不产生 pass@k 成绩。

## 导入和划分

工作区已完成导入；日常复核先执行 `python3 scripts/import_verilogeval.py --verify data/verilogeval`。以下导入命令仅适用于具有 `.git` 元数据的干净源仓库。移交 ZIP 内的 `third_party/verilog-eval` 是去除 Git 元数据的源码快照；如需重新导入，应在新目录 clone 上游仓库并 checkout 上述固定 revision，再将 `--source` 指向该仓库。

```bash
python3 scripts/import_verilogeval.py --source third_party/verilog-eval --output data/verilogeval --split-seed 20261007 --dev-fraction 0.8
```

本工作区已经完成上述导入，无需重复导入；复核已有文件可执行 `python3 scripts/import_verilogeval.py --verify data/verilogeval`。重新导入时输出目录必须不存在。导入器固定 Git revision，保留逐文件 SHA-256 和许可；原始 prompt 不加参考答案。参考实现与测试台放在独立评价目录，只交给本地开发评分工具。开发集和保留集按固定种子确定性划分，不重叠且覆盖全部题目；它们是我们的实验划分，不能声称与官方题目划分相同。

每题 JSON 的 `top=TopModule`、`testbench_top=tb`、`simulation_protocol=verilogeval_v2`。明确出现 input clk 时记录 `clock_port=clk`，没有明确可识别时记录空值及必要警告；多时钟与特殊接口仍须人工核对。综合约束针对实际 DUT 端口，不把测试台时钟误当成 DUT 输入。

## 原测试台判定

不向原测试台插入人为通过语句，不修改黄金参考。适配器检查测试台的完整总结行：mismatch 为零、采样数大于零、计数一致、正常结束标志存在，而且没有 timeout/fatal/error。缺少或含歧义的总结不能通过。该解释仅服务于公开集开发，**仍需在 Vivado 2025.2 上验证兼容性**，不能预设是官方评分协议。

智能体默认只接收题面、通用 RTL 规则及允许的工具诊断。参考实现、测试台内容不作为模型 prompt。正式赛题是否允许读取某类工具反馈，等待官方 I/O 规范确认。

v2 首版还保守限制了生成代码访问测试台/参考模块、跨层次信号、bind/force 等行为；当前点成员语法检查会同时排除部分合法 struct/interface 写法。普通命名端口连接仍允许。它是待真实数据验证与改进的支持范围，不能宣称覆盖全部 SystemVerilog 语法，也不能代替官方操作系统沙箱。

## 串行批量配对

先用人工响应验证批处理，不需要模型服务：

```bash
python3 scripts/batch.py --tasks examples/counter.json --seeds 0 1 --replay examples/replay.json --output outputs/batch_smoke
```

具备真实模型与 EDA 后执行开发集：

```bash
python3 scripts/batch.py --task-manifest data/verilogeval/dev.json --seeds 0 1 2 3 4 --config serve/config.json --output outputs/dev_real
```

每个题目和独立种子启动一次完整配对运行；默认串行，不叠加推理并发。每对产出 baseline 和 agent，共用同一服务与配置。批处理保存整体墙钟、逐次退出状态、标准输出/错误和汇总 JSON；一个运行出错后仍继续其他题，最终用非零退出码说明存在运行错误。

首次真实测试先用一到数题做检查，再扩至全量；不能直接将全量队列完成视为所有题通过。统计会单列 mock、不可判定、重复身份与重复种子，未形成足够独立样本时 pass@5 为空。

## 保留集使用规则

只在开发集修改提示、规则、重试和模型配置。模型与策略冻结后，才运行保留集并汇总；不根据保留集逐题结果补答案模板。如确需改策略，则明确开启新实验并使用新的未调参评价条件，不把反复调试后的保留集仍称为未见数据。官方隐藏题严格遵守赛事冻结要求。

## 2026-10-14 时钟元数据修复

导入与加载共用公开接口识别器，支持唯一标量 `clk`、`clock`、`clk_i`、`sys_clk` 等输入。已有 `Prob145_circuit8` 已改为 `clock` 并更新导出哈希，上游字节未修改；数据集校验通过。仍有 82 条 `clock_port_undetermined`，真实综合前须人工区分组合逻辑与未识别时钟，不将虚拟时钟当作物理时钟约束证据。
