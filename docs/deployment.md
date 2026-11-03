> 历史部署记录：当前统一操作入口见 [当前部署与验收](current_deployment.md)。

# 从当前工程到可提交容器

状态：本机是 macOS ARM64，缺少 Vivado、AMD ROCm GPU 与 Docker。本文是待执行操作单，不是部署成功记录。

1. **取得赛事事实。** 保存官方基础镜像精确引用及 digest、评分细则、输入输出规范、题目/工具反馈可见性、单题/总时限、pass@1/5 定义、模型资产挂载规则和许可方式。当前指南没有这些完整值，不自行编造。
2. **准备目标机。** 按实际 GPU 型号核对 AMD 支持的 Linux、驱动、ROCm、PyTorch 和推理后端组合。W7900 可验证迁移流程，但其 48 GB 不能证明方案装入 R9700 的 32 GB。
3. **先验证 EDA。** 加载 Vivado 2025.2 环境；`python3 scripts/doctor.py` 检查 PATH；用已知正确与已知错误设计分别验证三级判定、超时与错误日志；检查 part 安装和许可可用。不要用其他版本结果替代。
4. **固定本地模型。** 初始候选为 Qwen2.5-Coder-7B-Instruct FP16，用于尽快打通链路，不声称它是最优。下载阶段固定 revision，保存权重/tokenizer/chat template/config 文件及 SHA-256/许可证；修改 `serve/config.json`、`model/MODEL.md`。下载与安装在准备阶段完成，运行阶段禁止联网取资产。
5. **固定后端。** 按所选 ROCm 组合安装并验证 vLLM；将实际版本、wheel 校验和、驱动及依赖记录归档。`serve/start.sh` 仅启动已经安装的指定版本，不负责安装。当前 Dockerfile 没有这些资产，需要在官方镜像上补齐锁定 wheelhouse 与离线安装步骤。
6. **单卡服务验证。** 使用单 GPU、tensor parallel=1、初始 context=8192、输出上限=2048、并发=1。启动脚本从同一配置读取模型别名及上下文。服务 readiness、长题推理、异常退出、OOM 与重启恢复均须验证。监听地址与客户端同为 127.0.0.1:8000；如改端口，两个位置同时改。
7. **测真实显存。** 使用匹配版本 AMD SMI 记录服务启动/加载模型、最长输入、最大输出及多题连续运行的峰值。包含权重、KV cache、运行工作区，不以参数量估算代替。记录物理卡总显存/可用显存、监控间隔、峰值、是否 OOM；在 32 GB 卡上确认完整容纳。
8. **验证配对流程。** 先用 `examples/counter.json` 跑 `run_pair.sh`，审查模型请求：baseline 只有题目且一次，agent 有规则及允许的工具反馈。确认同服务同上下文，日志可追溯，错误设计不能伪造通过。
9. **核验已导入公开数据。** 已固定 VerilogEval `dataset_spec-to-rtl` revision，完成 156 题导入、开发/保留划分和许可/文件哈希；参考测试台与模型输入分离，见 `docs/dataset.md`。在真实 EDA 上先核对 `verilogeval_v2` 结果解析和全部测试台语法，再扩大运行；当前只有文件和软件协议验证。
10. **做批量评测与消融。** 使用 `scripts/batch.py` 依固定开发/保留划分运行；完整记录失败、超时、不可判定和每阶段结果；模型与策略只在开发集调参；冻结后保留集测试。批处理程序已通过本机回放，公开集真实跑分尚未执行。
11. **补齐容器。** `Dockerfile` 必须以官方镜像为基底；补齐推理依赖、模型资产与服务生命周期管理。保留 EDA 安装和许可约定。根据官方允许方式将模型打入镜像或离线挂载，外部权重也必须冻结并核验。当前模板不自动启动推理服务，首次集成须增加受控启动/等待/清理逻辑。
12. **干净环境断网验收。** 从固定镜像、全部本地资产和锁文件开始，关闭外网，暴露官方允许的唯一 GPU 与资源；执行批评测；证明无下载/远程API依赖、基线同批生成、时间/空间限制生效、日志完整。退出服务后再冷启动一次检查可复现性。
13. **冻结。** 用 `scripts/freeze.py --help` 查看 create/verify 参数；保存容器 digest、方案包清单和外部模型清单。评测前验证无改动，窗口内禁止改源码/规则/权重，不自动吸收评测题成为新技能。

官方参考（2026-10-07 查阅；以部署时对应版本文档为准）：

- [VerilogEval 官方仓库](https://github.com/NVlabs/verilog-eval)：公开 harness 与题目目录；其原始第三方仿真设置不能替代赛方 Vivado 判定。
- [Qwen 模型卡](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct)：来源、架构与许可；没有本项目通过率保证。
- [vLLM GPU 安装文档](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)：核对 ROCm 版本及 GPU 支持，不使用未固定的 latest 构建作最终证据。
- [AMD ROCm 系统要求](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/reference/system-requirements.html)：核对 GPU/系统组合。
- [Vivado 2025.2 仿真命令](https://docs.amd.com/r/2025.2-English/ug900-vivado-logic-simulation/xelab-xvhdl-and-xvlog-xsim-Command-Options)：核对 xvlog、xelab 与 xsim 参数。

赛事官网本次访问超时，未确认最新报名/提交日期。10 月 9 日 AI PC 中期申请是指南 3.2 的节点，不能移用为 3.1 的截止日期。
