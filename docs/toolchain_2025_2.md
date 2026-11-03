# Vivado / Vitis 2025.2 版本政策

确认日期：2026-11-04。按用户确认的适用赛事要求，两条赛道正式提交、本地开发、诊断与复现 **全部统一为 2025.2**，不提供其他版本的兼容路线。

## 唯一有效配置

| 项目 | 固定值 |
|---|---|
| RTL Vivado | 2025.2 |
| HLS Vitis / Vitis HLS | 2025.2 |
| RTL_AGENT_VIVADO_VERSION | 2025.2；未设时亦为此值 |
| HLS_AGENT_VITIS_VERSION | 2025.2；未设时亦为此值 |
| manifest.json 的 toolchain | vivado=2025.2、vitis=2025.2 |
| HTTP / 本地 JSON / Docker / start.sh | 同一版本政策 |

环境变量不是开放版本选择开关：设为其他版本会报错，不会自动降级、切换安装或接受不匹配工具。仍需读取实际工具 banner，不能只凭目录名判定版本。

## Linux / 目标机准备

由已验证的 2025.2 安装环境提供 PATH、XILINX_VIVADO、XILINX_VITIS、XILINX_HLS，不假定厂商安装目录布局。必要时先 source 对应 2025.2 settings64.sh，再设置：

```bash
export RTL_AGENT_VIVADO_VERSION=2025.2
export HLS_AGENT_VITIS_VERSION=2025.2
vivado -version
vitis-run --version
```

RTL 要求 vivado、xvlog、xelab、xsim 来自同一安装；HLS 需要 vitis-run，正式无测试台综合路径还需要 v++。检查目标器件 xczu3eg-sbva484-1-e 的安装支持和许可。工具返回码为零不自动代表完成了综合或功能仿真。

## Windows 本机示例

本轮实际可定位的安装为 D:/AMDDesignTools/2025.2。其他机器按实际路径修改，不复制不存在的盘符。

```powershell
$edaRoot = 'D:/AMDDesignTools/2025.2'
$env:RTL_AGENT_VIVADO_VERSION = '2025.2'
$env:HLS_AGENT_VITIS_VERSION = '2025.2'
$env:XILINX_VIVADO = "$edaRoot/Vivado"
$env:XILINX_VITIS = "$edaRoot/Vitis"
$env:XILINX_HLS = "$edaRoot/Vitis"
$env:PATH = "$edaRoot/Vivado/bin;$edaRoot/Vitis/bin;" + $env:PATH
python scripts/doctor.py --work-dir .
```

doctor 是预检；真实综合、可信测试台验证、真实模型和显存验收分别保留证据。之前本机九项 RTL 时间单位/复位测试已经使用 2025.2，版本政策更新不把旧测试追认为新执行结果。

## Docker 基础镜像

不能通过修改环境变量把镜像内的工具改成目标版本。当前未取得可核验的 2025.2 目标镜像引用，因此 Dockerfile 的 BASE_IMAGE 改为必须显式传入，避免继续隐式采用不匹配镜像。

```bash
docker build --build-arg BASE_IMAGE='<已验证含 ROCm 与 Vivado/Vitis 2025.2 的镜像引用>' -t track-agent:2025.2 .
```

这是构建示例，镜像引用必须替换为真实值；本轮未完成容器构建验收。不得虚填 tag 或 digest 作为“已固定镜像”。

## 文档与历史记录

当前 README、提交说明、REPORT、部署说明及配置均以本页为准。docs/official/API_CONTRACT.md 为项目适用文本：HTTP、鉴权、队列、输入输出和官方基线条款保留，环境段已按本次确认修订，明确区别于官方原件。

历史原件完整保存在工作区 output/toolchain_2025_2_20261104/ 的四份 *_before.zip；旧版本运行记录不改写为 2025.2 成绩。官方 baseline.py、run_baseline.sh 未修改。原始参考哈希与当前适用文本哈希分别记录在 official_reference_sha256.json。
