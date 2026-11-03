# 项目适用接口契约

工具版本统一为 **2025.2**。本文保留参考契约的 HTTP、输入输出及基线要求，并按用户确认修订环境段；**本文是项目适用说明，不是官方原件**。原始参考文件已另存版本修订备份，原始 SHA-256 仍在 official_reference_sha256.json。

# 接口契约

评测当天，赛事方从外部向各队的服务投题。**这是唯一赛前固定、之后一个字都不能改的东西。**

本文所列格式最终以赛前公告为准，但结构不会变。按本文实现的服务在正式评测中可直接使用。

---

## 一、这件事在做什么

正式评测采用远程投题：**队伍在自己的机器上把服务跑起来，赛事方从外面投题。**
赛事方按 `Dockerfile` 重建容器、在自己的机器上逐题运行的方式保留为退路，仅在远程投题
无法进行时启用。两种方式下 `run.sh` 由谁调用、超时如何处理，见第八节。

```
  ① 队伍那边跑一个程序，它一直不退出，守着一个端口
  ② 赛事方发一个 HTTP 请求过去，内容是一段 JSON，把题面送过去
  ③ 队伍的程序收到后，调用自己的智能体去解题
  ④ 赛事方每隔几秒查询一次，解完后把代码以 JSON 取回
```

需要理解的只有三个词：

| 词         | 是什么                                        | 为什么需要                                     |
| ---------- | --------------------------------------------- | ---------------------------------------------- |
| **端点**   | 一个网址，如 `https://xxx.example.com/v1/solve` | 赛事方照着这个地址投题                       |
| **令牌**   | 一串密码字符串，随请求一起发过去              | 端点网址是公开的，没有令牌任何人都能投题       |
| **异步**   | 投题后立即拿到任务编号，之后查询，做完再取结果 | 平台入口会切断 60 秒内没有响应的请求，一道题却常要几分钟 |

**题目在第 ② 步才第一次离开赛事方的机器，而且一次只走一道。** 队伍从头到尾拿不到完整
题集，也不知道下一题是什么。

---

## 二、`GET /v1/health`

赛事方在投题前调用，确认服务与模型就绪。

**请求**

```
GET /v1/health
Authorization: Bearer <令牌>
```

**响应**

```json
{
  "ready": true,
  "track": "rtl",
  "model": "Qwen/Qwen3-8B",
  "vram_gb": 21.4
}
```

| 字段      | 说明                                                  |
| --------- | ----------------------------------------------------- |
| `ready`   | 模型已加载、智能体可用时为 `true`                     |
| `track`   | **必须是 `"rtl"`**，投题机会核对，不符即拒投    |
| `model`   | 与 `MODEL.md` 中声明的模型一致                        |
| `vram_gb` | 当前实测显存占用                                      |

**`track` 这一项是实测中一次事故加上的。** 曾出现 HLS 的投题服务因端口被占启动失败、
而 RTL 服务仍在应答的情形，投题机照常投题，24 个 HLS 样本全部收到 Verilog，产物
文件名是投题机按自己的 track 命名的，**表面上看不出任何异常**，直到判定时全部落在
最低一级才暴露。健康检查的意义就是挡住这类「服务活着但不对」的状态。

---

## 三、投题与取结果

### 投题：`POST /v1/solve`

**请求**

```
POST /v1/solve
Authorization: Bearer <令牌>
Content-Type: application/json
```

```json
{
  "task_id":    "eval-0007",
  "nonce":      "T042-eval-0007-3-1763251200123456789",
  "mode":       "agent",
  "prompt":     "实现一个模块 TopModule，在串行输入位流中检测序列 1101 ...",
  "interface":  "module TopModule (input clk, input reset, input in, output detected);",
  "deadline_s": 900
}
```

**响应**：立即返回，状态码 202。

```json
{
  "job_id":  "7c1e0f2a9b...",
  "task_id": "eval-0007",
  "status":  "queued"
}
```

### 取结果：`GET /v1/jobs/<job_id>`

```
GET /v1/jobs/7c1e0f2a9b...
Authorization: Bearer <令牌>
```

还没做完时：

```json
{ "job_id": "7c1e0f2a9b...", "task_id": "eval-0007", "status": "running" }
```

做完时：

```json
{
  "job_id":    "7c1e0f2a9b...",
  "task_id":   "eval-0007",
  "status":    "done",
  "solution":  "module TopModule (...);\n  ...\nendmodule\n",
  "trace":     "{\"tool\":\"lint\",\"ts\":...}\n{...}\n",
  "elapsed_s": 214.3
}
```

赛事方投题后每 2 秒查询一次，查到 `done` 即取走结果。


| 字段         | 说明                                                                   |
| ------------ | ---------------------------------------------------------------------- |
| `task_id`    | 题号。返回时必须原样带回，用于对账                                     |
| `job_id`     | 服务为这次投题分配的任务编号，查询结果时用                             |
| `status`     | `queued` 排队中，`running` 解题中，`done` 已完成；只有 `done` 时才带结果字段 |
| `nonce`      | 一次性随机串，每次投题都不同。同一个 `nonce` 重复投递应返回同一个任务，不得重复解题 |
| `mode`       | `agent` 走完整方案，`baseline` 绕过智能体与技能包                      |
| `prompt`     | 题面，纯文本                                                           |
| `interface`  | 模块声明。**评测题集下为空串**，接口写在题面里，见下 |
| `deadline_s` | 单题墙钟上限，正式评测为 900 秒（15 分钟），队伍应在此之前自行停止     |
| `solution`   | 产出的 Verilog / SystemVerilog 代码。**做不出来返回空字符串，不要报错**           |
| `trace`      | 工具调用记录，JSONL 格式，见第五节                                     |
| `elapsed_s`  | 队伍自报耗时。**不用于计分**，仅与赛事方计时交叉核对             |

---

## 四、几条硬约定

- **单题墙钟以赛事方发出投题到查询到 `done` 为准**，误差不超过一个查询间隔（2 秒），
  队伍自报的 `elapsed_s` 不作数
- 投题后 `deadline_s + 20` 秒内仍未查询到 `done`，该题按 L0。时限从投题时算起，
  排队的时间也计入
- **`POST /v1/solve` 与 `GET /v1/jobs/…` 都须在几秒内返回**。平台入口（Azure Front Door）
  对 60 秒内没有响应的请求一律返回 504，所以不能让请求一直挂着等解题完成
- **任何失败一律按 L0，不重试。** 超时、连接断开、返回格式错，同等对待
- **服务须能连续处理数百次请求，中途不得重启**
- 方案与基线走同一个端点，用 `mode` 区分，保证两者出自同一次运行、同一个服务、
  同一份配置
- **服务须绑 `127.0.0.1`，不是 `0.0.0.0`**；对外由平台的 `rc-tunnel` 转发，见第七节

关于「做不出来返回空串而不是报错」：这样能让「没做出来」和「服务坏了」在协议层可区分。
前者是正常的 L0，后者要排查。

### `interface` 在 RTL 评测题集下是空串

接口信息**已经写在题面里**了。评测题集的题面沿用 VerilogEval v2 `dataset_spec-to-rtl`
的格式，题面本身就是一份含接口的规格说明：

```
I would like you to implement a module named TopModule with the following
interface. All input and output ports are one bit unless otherwise specified.

 - input  clk
 - input  d   (8 bits)
 - output q   (8 bits)

The module should include 8 D flip-flops. ...
```

模块名、端口名、方向、位宽都在里面。再单出一份 `interface.txt` 是同一信息的第二份
拷贝，两份一旦不一致，队伍无从判断该信哪个 —— 所以评测题集不提供它，`interface`
字段恒为空串。

**这与 HLS track 不同，那边 `interface` 是必有的**：HLS 题的头文件里有 `typedef`、
数组长度宏和常量系数表，这些东西题面用文字描述不出来。两条赛道的数据集形态不同，
不是疏漏。

**由此有一条要注意的事：依赖 `interface` 做接口比对的逻辑，必须能从题面取端口表**，
否则它在评测当天会静默失效 —— 输入为空，检查照常返回「通过」。题面里那个
`- input  clk` 列表格式是规整的，可以直接解析；`example/agent/tools.py` 的
`ports_from_prompt()` 是一份可用的实现。

仓库自带的三道示例题与评测题集**同形**：也只有 `prompt.txt`，没有 `interface.txt`。
示例题的作用是照着它开发，形状不一致的话，队伍会写出依赖某个评测当天不存在的
文件的智能体 —— 而那种失效是静默的。`selftest/veval_import.py` 转换出来的题集
同理。

---

## 五、`trace.jsonl` 里应当有什么

每行一个 JSON 对象，记录智能体做过的每一次工具调用与模型调用。

```json
{"ts": 1763251200.12, "tool": "llm", "tokens_in": 2841, "tokens_out": 512}
{"ts": 1763251203.45, "tool": "lint", "rc": 1, "excerpt": "ERROR: [VRFC 10-3180] cannot find port 'q' on this module"}
{"ts": 1763251260.01, "tool": "llm", "tokens_in": 3402, "tokens_out": 488}
{"ts": 1763251302.77, "tool": "synth", "rc": 0, "excerpt": "Synthesis finished with 0 errors"}
```

建议字段：

| 字段         | 说明                                       |
| ------------ | ------------------------------------------ |
| `ts`         | Unix 时间戳，浮点秒                        |
| `tool`       | 工具名，如 `llm` / `check_interface` / `lint` / `synth`       |
| `rc`         | 返回码，0 为成功                           |
| `excerpt`    | 输出摘要，建议截断到 2 KB 以内             |
| `tokens_in`  | 模型调用的输入 token 数                    |
| `tokens_out` | 模型调用的输出 token 数                    |

这一项同时喂三个用途：**工程质量评定（20 分）、作弊复核（生成产物与工具调用能否对上）、
失败分析。** 属必需项，不是可选。

---

## 六、最小实现

`example/serve/serve_api.py` 是一份可直接运行的实现，把 `run.sh` 套一层 HTTP 壳。
智能体本身无需改动，仍为读取一个目录并写出一个文件。

```bash
cd example
pip install -r requirements.txt
export FPGACHINA_TOKEN=<赛事方发的令牌>
export MODEL_NAME=Qwen/Qwen3-8B
./serve/start.sh                 # 拉起推理服务等常驻进程，就绪后返回
./serve/run_api.sh start         # 投题服务，退出后 2 秒内自动重启；停止用 ./serve/run_api.sh stop
```

自测：

```bash
curl -H "Authorization: Bearer $FPGACHINA_TOKEN" http://localhost:7860/v1/health

curl -X POST http://localhost:7860/v1/solve \
  -H "Authorization: Bearer $FPGACHINA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task_id":"t1","nonce":"n1","mode":"agent","prompt":"实现一个 8 位计数器","interface":"module TopModule (input clk, input reset, output [7:0] q);","deadline_s":60}'
# 返回 {"job_id":"...","status":"queued"}，然后用 job_id 查询，直到 status 为 done
curl -H "Authorization: Bearer $FPGACHINA_TOKEN" http://localhost:7860/v1/jobs/<job_id>
```

---

## 七、端口与网络

```
  7860   serve_api.py   投题接口   绑 127.0.0.1，经 rc-tunnel 对外
  8000   vllm.sh        推理服务   绑 127.0.0.1，不暴露
```

### 只能绑 `127.0.0.1`

Radeon Cloud 的对外暴露机制（`rc-tunnel`）**只接管绑在 `127.0.0.1` 上的 HTTP /
WebSocket 服务**，绑 `0.0.0.0` 平台不接管。平台文档原文：

> Only HTTP and WebSocket services bound to `127.0.0.1` inside the notebook
> are supported.

暴露流程，在 notebook 终端里执行：

```bash
/var/run/secrets/frp-self-service/install
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$HOME/.local/bin/rc-tunnel" expose --port 7860
"$HOME/.local/bin/rc-tunnel" status        # 应显示 Status: active
```

`env -u …` 让隧道客户端不经实例的出网代理、直接连接平台的隧道服务器。请照这条命令
执行。`status` 显示 `Status: active` 才算建好；若公网网址返回 404，查看
`/root/.local/state/rc-tunnel/frpc.log`，正常时其中有 `start proxy success`。
**重新执行 `expose` 后网址会变**，变了要重新报送。

### 隧道会被收回

rc-tunnel 每 10 秒探测一次 `127.0.0.1:7860`，**连续约 1 分钟不通，平台就收回这条隧道**，网址永久
失效；服务恢复后也不会自动连回，只能重新 `expose`，并重新报送新网址。因此：

- 先启动投题服务，确认 `curl --noproxy '*' http://127.0.0.1:7860/v1/health` 返回 401，再 `expose`
- 用 `serve/run_api.sh start` 启动投题服务。它以 `setsid` 在后台运行，关掉终端不受影响，服务退出后
  2 秒内自动重启
- 实例重启后 `rc-tunnel` 也会被清除，需重新执行上面的 `install`
- 在实例里访问自己的公网网址，同样要带 `env -u …` 去掉代理变量，否则请求会被出网代理拦下

绑错地址的后果：服务本身是活的，但外面永远打不进来，**在赛事方那边看起来和挂掉
没有区别**，而失败不重投。

### 只有投题接口需要暴露

平台限制**一个 notebook 同时只能暴露一个端口**。7860 是唯一需要暴露的；vLLM 的
8000 由智能体经 `127.0.0.1` 内部调用，**不要去暴露它**——暴露了既占掉唯一的名额，
也把推理服务开在了公网上。

由此有一条推论：**同时参加 RTL 与 HLS 两个赛道的队伍需要两台 VM。** 一台机器起
两个服务，只能暴露其中一个。

### 网址是公开的，鉴权得自己做

平台分配 `rc-<random>.radeon.firstdg.ai` 形式的域名，不支持自定义，且**平台不为
它加任何业务层鉴权**。因此 `/v1/solve` 与 `/v1/health` 都必须校验
`Authorization: Bearer <令牌>`，`serve_api.py` 里那段令牌校验不可省略。

`/v1/health` 同样要校验——它会报出队伍所用的模型名与显存大小，公网可读等于把这些
情报直接送出去。

### 端口范围

1024–65535，且需避开平台保留端口，7860 与 8000 都在范围内。平台只支持 HTTP 与
WebSocket，不支持 TCP / UDP / SSH / 数据库端口。

### 工程文件落本地目录

工程与中间文件请落在本地目录，不要落在网络存储上。Vivado 综合会产生
大量小文件，写网络存储极慢。这不是优化建议。
Radeon Cloud 实例上的 `/tmp` 是 4 GB 内存盘，连续运行容易写满，请按 README 开头的 `env.sh`
把 `EDA_TMP`、`AGENT_TMP` 设到本地盘 `/var/tmp`。

---

## 八、单题入口 `run.sh` 与 `run_baseline.sh`

```
用法    run.sh <task_dir> <out_dir>

读      <task_dir>/prompt.txt        题面。★ 模块名与端口就在这里面
        <task_dir>/interface.txt     不提供。字段保留是为了与 HLS track 共用同一份契约

写      <out_dir>/solution.v         产出的 RTL
        <out_dir>/trace.jsonl        工具调用记录，见第五节

退出码  0    产出了文件
        非0  放弃本题
```

### 两种评测方式下由谁调用

|                | 远程投题（正式方式）                     | 容器重建（退路）                              |
| -------------- | ---------------------------------------- | --------------------------------------------- |
| 启动常驻进程   | 队伍运行 `serve/start.sh`，再起投题服务  | 赛事方运行一次 `serve/start.sh`               |
| 调用 `run.sh`  | 队伍的投题服务，每个请求调用一次         | 赛事方的测试脚本，每题调用一次                |
| 投题服务       | 运行，守 7860                            | 不启动                                        |
| 1 号进程       | JupyterLab，不回收僵尸进程，见第十节     | `docker run --init`，由 Docker 的 init 回收   |
| 超时           | 投题后 `deadline_s + 20` 秒内未查询到完成即按 L0，不向队伍机器发送任何信号 | 到点向 `run.sh` 的进程组发 SIGTERM，10 秒后仍未退出则 SIGKILL |
| 超时计分       | L0                                       | L0                                            |

一次评测只采用其中一种，不会混用。无论哪种方式，同一支队伍同一时刻只有一道题在运行。
计分以赛事方侧测得的墙钟为准，不要依赖自己计时。

### `serve/start.sh` 是常驻进程的唯一入口

`run.sh` 依赖的常驻进程，包括推理服务，以及队伍自己的常驻引擎（如果 `run.sh` 把题交给
它去解），都由 `serve/start.sh` 拉起。这个脚本须满足三条：

- 全部就绪后返回 0，否则返回非 0
- 可重复执行，服务已在运行时什么也不做
- 用 `setsid` 拉起常驻进程，使其脱离调用者的进程组

`run.sh` 可以自己解题，也可以只负责把题交给常驻引擎、取回结果，两种写法都允许。前提是
在按 `Dockerfile` 重建的干净容器里运行一次 `serve/start.sh` 之后，`run.sh` 不需要任何
人工介入就能产出 `solution.v`。容器重建方式下赛事方只做这两步，不会启动投题服务，也不会
执行 README 里的其他命令。

### 超时信号只作用于 `run.sh` 的进程组

容器重建方式下，超时信号发给 `run.sh` 所在的进程组（`kill -- -<pgid>`），即 `run.sh`
本身以及它启动的、没有另立进程组的全部子进程。组外的进程不受影响。`serve/start.sh` 用
`setsid` 拉起的常驻进程在组外，超时只结束这一题。

如果常驻进程是 `run.sh` 直接放到后台启动的、没有经过 `setsid`，它就留在这个进程组里，
会随这一题一起被结束，下一题到来时服务已经不在了。

远程投题方式下，同样的事由队伍的投题服务负责。`serve/serve_api.py` 为每次 `run.sh`
单开一个进程组，在 `deadline_s - 10` 秒时对整组发 SIGTERM，5 秒后发 SIGKILL，正常结束
后也会清扫一遍整组。只结束 `run.sh` 一个进程是不够的，它启动的智能体与 Vivado 会继续
运行，占着 GPU 与 CPU 和下一题争抢。

`run_baseline.sh` 参数完全相同，但**绕过智能体与技能包**：提示词只含题面本身，
单次生成，不重试，不调用任何工具，**不得挂载任何 MCP server**。

最后一条要单独强调，因为 MCP 容易被理解成「推理栈」的一部分而在基线里保留。
那样增益项（40 分）的基线被抬高，整项失真。**基线里模型只能拿到题面文本。**

推理服务本身在方案与基线之间必须完全一致——同一个模型、同一份量化、同一套上下文
配置，以及 `manifest.json` 中声明的同一套思考设置。赛事方会在同一次运行中先后调用两者，远程投题下以 `mode` 字段区分。

---

## 九、运行环境（项目适用版本）

Vivado/Vitis 仅使用 **2025.2**，以用户 2026-11-04 确认要求为准。读取目标机已设置的 XILINX_VIVADO、XILINX_VITIS、XILINX_HLS 与 PATH，不拼接或猜测安装目录。

工具必须实际报告 2025.2。版本不匹配、探测失败或许可不可用时不得宣称验证通过。HLS 综合使用 v++ -c --mode hls；开发验证可使用 vitis-run --mode hls --tcl。是否安装相应组件与器件支持须实机检查。

### 什么该写在哪

这是**性能与配额问题，不是风格建议**。写错地方会让单题墙钟翻几倍，或者把配额吃光。

```
/tools/Xilinx/     EDA 工具本体    只读，来自镜像层，同节点多容器共用一份
/workspace/        队伍工作区      ★ 网络存储（NFS），持久，有配额
/  (含 /root)      容器根目录      节点本地盘，实例重启后清空，见 FAQ「实例配置」
$EDA_TMP/<id>/     Vivado 工作目录 ★ 本地，快，不持久；实例上设为 /var/tmp/eda
```

`xsim.dir`、`.Xil` 都是几万个小文件。在 NFS 上创建几万个小文件是最慢的一种 IO
模式，而且会吃掉 25 GB 的持久化配额。镜像里提供了 `eda-workdir` 代劳：

```bash
WORK=$(eda-workdir prob001 s3)      # 建 $EDA_TMP/prob001-s3-XXXXXX
cd "$WORK"
# ... 跑 Vivado ...
cp solution.v *.log /workspace/runs/prob001/s3/    # 只把小文件搬回去
```

**每次运行建一个**，不是每题一个。正式评测每题跑 5 次采样，参数里那个 `s3` 就是
采样序号。不能共用是因为 Vivado 把 `.Xil/`、`vivado.log`、`vivado.jou` 都写进 cwd
（见第十节），两次运行共用一个目录会互相覆盖，并发时尤其明显。

`$EDA_TMP` 下**整棵子树**超过两小时没有任何改动的目录，由后台循环每十分钟清理一次。
这个循环由镜像入口启动；但 Radeon Cloud 会把容器的启动命令换成 JupyterLab，镜像入口
未必运行，所以 `serve/start.sh` 也会启动一份，同一时刻只保留一份。判定「有没有改动」看整棵子树而不是顶层目录，所以跑了两小时以上的任务
不会被误删——目录自身的 mtime 只在增删直接条目时更新，而 Vivado 开头建好
`xsim.dir/`、`.Xil/` 之后就只往里写，顶层看着会一直是「旧」的。

两小时清理只是兜底。智能体应在每次运行结束后删除自己的工作目录，示范方案即如此；否则连续
投题时容易写满，之后的题全部失败。

---

## 十、已知的环境细节

这几条都是实测撞出来的，每一条不知道都会白花几小时。

**`xelab` 需要 `-timescale`。** 参考测试台首行是 `` `timescale 1 ps/1 ps ``，而待测
模块从不声明 timescale。xsim 遇到这种混合会报 `ERROR [XSIM 43-4099]` 拒绝详细描述。
判定器已固定加 `-timescale 1ps/1ps`，自己调 `xelab` 时也要加。

**Vivado 会往当前目录写东西。** `.Xil/`、`vivado.log`、`vivado.jou` 都落在 cwd，
连查一次版本号都会写 `.pb` 文件。所以并发跑必须逐样本隔离工作目录，这也是
`eda-workdir` 存在的理由。

**自测脚本不要用 `set -u`。** AMD 的 `.settings64-Vitis.sh` 内部引用了未定义的
`MATLABPATH`，在 `set -u` 下 `source` 它会当场退出：

```
/tools/Xilinx/.../.settings64-Vitis.sh: line 18: MATLABPATH: unbound variable
```

`set -euo pipefail` 是很常见的写法，踩上了症状是脚本在 source 环境那一步就悄悄
断掉，看着像工具装坏了。用 `set -eo pipefail`，或者在 source 前后临时切换。

**授权失败单独记录。** 保存工具原始诊断、退出码和许可状态，不把环境故障当作设计功能错误。

**容器的 1 号进程不回收僵尸进程。** Radeon Cloud 实例的 1 号进程是 JupyterLab，不是
init。进程被结束后，它那些失去父进程的子进程交给 1 号进程接管，JupyterLab 不回收它们，
退出后就成为僵尸进程。僵尸不占 CPU 与内存，但一直占着进程号，只增不减；进程号耗尽后
任何新进程都起不来，之后的题目全部失败。

单题超时被结束时最容易出现这种情况：`run.sh` 先被结束，它启动的智能体、Vivado 随后
被结束，全都成了孤儿。只结束 `run.sh` 一个进程更糟，孤儿还活着，继续占用资源。

`serve/serve_api.py` 已经处理：启动时调用 `prctl(PR_SET_CHILD_SUBREAPER)` 把自己设为
回收者，子孙进程成为孤儿后交给它而不是 1 号进程；每题结束后结束并回收该题进程组里的
全部进程。自己写投题服务的，要做同样的事，或者用 `tini -s` 这类带回收能力的 init 包住
整个服务（镜像未自带 tini）。检查方法是连续投几十题之后运行
`ps -eo stat | grep -c '^Z'`，数字应保持不变。

开发期在 JupyterLab 终端里直接运行、中途结束的程序不在投题服务之下，同样会留下僵尸。
挂在 1 号进程下的僵尸只有 1 号进程能回收，积累多了只能重建实例。
