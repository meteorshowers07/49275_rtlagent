# 当前部署与验收入口（2026-11-04）

按用户确认，正式与本地开发工具链均统一为 **Vivado/Vitis 2025.2**。HTTP、输入输出和基线要求保持不变。见 [当前版本政策](toolchain_2025_2.md)、[修复记录](official_repair_20261104.md) 和 [项目适用契约](official/API_CONTRACT.md)。

## 部署前必须准备

在 manifest.json 固定真实模型 name/source/revision、量化、后端版本和实测显存，并同步 model/MODEL.md。当前 UNPINNED 与 UNVALIDATED 明确保留未知状态，启动器会拒绝伪就绪。默认启动命令支持本地 FP16、单 GPU vLLM；更换量化栈须同时修改并实测启动参数，不能只改声明。manifest 中 32768 上下文是待验配置，16384 输出上限来自原样官方基线，不是已经验证的容量。

在目标官方环境、权重尚可联网下载时完成安装和固定；断网后不下载权重或依赖。Dockerfile 只提供重建配方，目标镜像未在本机构建验收。

```bash
python3 -m pip install -r requirements.txt
chmod +x run.sh run_baseline.sh run_baseline_local.sh serve/start.sh serve/run_api.sh
export MODEL_PATH=/absolute/path/to/pinned/model
export MODEL_NAME=<与 manifest.json 和 model/MODEL.md 一致>
export RTL_AGENT_VIVADO_VERSION=2025.2
export HLS_AGENT_VITIS_VERSION=2025.2
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export AGENT_TMP=/var/tmp/rtl_agent
export EDA_TMP=/var/tmp/rtl_eda
export FPGACHINA_TOKEN=<赛事令牌>
./serve/start.sh
./serve/run_api.sh start
curl --noproxy '*' -H "Authorization: Bearer $FPGACHINA_TOKEN" http://127.0.0.1:7860/v1/health
```

health 的 ready 必须为 true 才可报送；无令牌检查应返回 401。7860 仅绑定 127.0.0.1；隧道流程参照项目适用契约。不自动报送网址或发布服务。两个赛道按本地官方契约各用独立 VM，不能同时抢占一个 notebook 的唯一暴露端口。

## 官方单题调用

```bash
./run.sh /absolute/path/to/task_dir /absolute/path/to/out_dir
./run_baseline.sh /absolute/path/to/task_dir /absolute/path/to/baseline_out
```

读取 prompt.txt。HLS 还必须提供 interface.txt 完整头文件，首行为 // header: filename.h；RTL 可以没有该文件，接口从题面提取。输出 solution.v 和 trace.jsonl；输出目录允许预先存在。agent 失败写空解，外层 HTTP 仍返回 done。官方 baseline 原样保留，勿自行添加技能、工具或重试。

原有本地开发命令仍可用：python -m agent.cli agent/baseline/pair --task <JSON> --output <新目录>；run_baseline_local.sh 对应旧本地基线。它们产生 design.cpp/design.sv 和本地验证阶段，不是官方基线成绩。

## 验证与清单

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -q
python3 scripts/freeze.py verify . local_manifest.json
```

HTTP 测试需要本机回环权限；Windows 自测请将 TEMP/TMP 设为可写 ASCII 工作目录。本机 Windows 已验证的内容和跳过项见 validation_20261104。上传后的实际依赖、权重、器件许可、端到端 GPU 推理及数百请求稳定性仍需目标机补验。


## 工具版本和容器

Vivado/Vitis 仅接受 2025.2，具体 PATH 设置、版本核验和 Docker 构建参数见 [版本政策](toolchain_2025_2.md)。BASE_IMAGE 必须是已验证包含 2025.2 的实际镜像引用，当前未提供默认镜像。
