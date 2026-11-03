# RTL 智能体

本包已接入本地官方目录 I/O 和异步投题协议。正式与本地开发工具均固定为 **Vivado/Vitis 2025.2**；本轮是独立开发修复，尚未确认线上封存版本。

- [部署与调用](docs/current_deployment.md)
- [修复范围、验证和待办](docs/official_repair_20261104.md)
- [项目适用契约](docs/official/API_CONTRACT.md)
- [证据索引](docs/evidence_index.json)
- [模型声明](model/MODEL.md)、[方案声明](manifest.json)
- [旧开发说明](docs/README_before_official_repair.md)

软件回归 250 项通过（跳过 31 项），不代表真实模型、GPU 或目标容器验收。模型 revision、后端版本与实测显存尚未补齐，不能直接宣称已具备正式参赛运行条件。

版本配置、环境准备与基础镜像要求见 [2025.2 版本政策](docs/toolchain_2025_2.md)。其他版本不会被版本校验接受。
