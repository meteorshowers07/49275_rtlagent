#!/usr/bin/env python3
"""
基线运行器。赛事方提供，队伍原样带上，不得修改。

用法：  baseline.py <task_dir> <out_dir> [rtl|hls]

    产出   <out_dir>/solution.v   或  solution.cpp
           <out_dir>/trace.jsonl

在无智能体、无技能包、单次直接生成的条件下解一道题，作为增益项的基线。
prompt 与采样参数由赛事方固定，队伍决定模型、量化、推理后端与服务配置。

对队伍的要求：推理服务须暴露 OpenAI 兼容的 `/v1/chat/completions` 端点，
默认 `http://127.0.0.1:8000/v1`，可用 `LLM_BASE_URL` 覆盖地址。使用
chat/completions 而非 completions，以便模型自带的 chat template 在服务端生效。

trace.jsonl 首行记录本脚本的 SHA-256 与服务端报出的模型名，供赛事方核验。
基线的 trace 须只有一次 llm 调用、零次工具调用。

思考设置：读取同目录 manifest.json 的 "generation" 段，随请求一并发出，使基线与
方案使用同一套思考设置（是否开启思考、思考强度）。只认 GENERATION_KEYS 中的两项，
其余字段一律忽略；没有这一段时不附加任何参数。
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ── 以下常量构成基线口径，不要改 ────────────────────────────────────────────
TEMPERATURE = 0.0
TOP_P = 1.0
MAX_TOKENS = 16384
TIMEOUT_S = 840
# MAX_TOKENS 取 16384：混合推理模型会先产出思考 token，预算过小会截断输出。
# TIMEOUT_S 取 840 秒：32B 级模型在单卡上约 28 token/s，写满 16384 需近 10 分钟，
# 仍在 900 秒的单题时限之内。
# 请求除 OpenAI 标准字段外，只附加 manifest.json 中声明的思考设置。
GENERATION_KEYS = ("chat_template_kwargs", "reasoning_effort")

SYS = {
    "rtl": ("You are an expert Verilog designer. Reply with a single synthesizable "
            "SystemVerilog module named TopModule. Output only code — no prose, no "
            "markdown fences."),
    "hls": ("You are an expert Vitis HLS engineer. Reply with a single C++ source "
            "file implementing the requested top-level function. Include the header "
            "shown in the interface section. Use only synthesizable C++ — no "
            "dynamic allocation, no recursion, no system calls. Output only code — "
            "no prose, no markdown fences."),
}
# ── 常量到此为止 ──────────────────────────────────────────────────────────

OUT_NAME = {"rtl": "solution.v", "hls": "solution.cpp"}
BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")


def load_generation():
    """读取 manifest.json 中声明的思考设置，只保留 GENERATION_KEYS 中的两项。

    chat_template_kwargs 的值只接受布尔、数字与字符串（如 enable_thinking），
    reasoning_effort 只接受字符串。采样参数、输出上限等不在此列，队伍无法借此
    改变基线口径。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            gen = json.load(f).get("generation") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    kw = gen.get("chat_template_kwargs")
    if isinstance(kw, dict):
        kw = {k: v for k, v in kw.items()
              if isinstance(k, str) and isinstance(v, (bool, int, float, str))}
        if kw:
            out["chat_template_kwargs"] = kw
    effort = gen.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        out["reasoning_effort"] = effort
    return out


def self_sha256():
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def served_model():
    """问服务端它实际在服务什么模型。用于与 MODEL.md 交叉核对。"""
    try:
        req = urllib.request.Request(BASE_URL + "/models")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read()).get("data", [])
        return data[0]["id"] if data else None
    except Exception:
        return None


def strip_think(text):
    """去掉推理模型的思考段，只留正式回答。

    推理服务未配置 reasoning parser 时，Qwen3 等模型会把 <think>…</think> 原样
    放在正文里。不先去掉，思考过程会连同代码一起写进产物，编译必然失败；思考中
    打过的代码草稿也可能被当成答案。三种形态：
      <think>…</think>答案     完整的思考段
      …</think>答案            对话模板已预填 <think>，回复里只有结束标记
      <think>…（截断）         输出上限耗尽在思考中，没有答案
    """
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        return text.split("<think>", 1)[0]
    return text


def extract(text, track):
    """从回复中提取代码。模型常附加 markdown 围栏或说明文字。

    与 agent.py 使用同一套抽取逻辑，以保证方案与基线口径一致。
    """
    text = strip_think(text)
    m = re.search(r"```(?:systemverilog|verilog|sv|cpp|c\+\+|c)?\s*(.*?)```",
                  text, re.S)
    if m:
        text = m.group(1)
    if track == "rtl":
        m = re.search(r"(module\s+TopModule\b.*?endmodule)", text, re.S)
        return (m.group(1) if m else text).strip() + "\n"
    return text.strip() + "\n"


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__.strip().splitlines()[2])
    task_dir, out_dir = sys.argv[1], sys.argv[2]
    track = (sys.argv[3] if len(sys.argv) > 3
             else os.environ.get("TRACK", "rtl")).lower()
    if track not in SYS:
        sys.exit(f"未知 track：{track}")
    os.makedirs(out_dir, exist_ok=True)

    prompt = open(os.path.join(task_dir, "prompt.txt")).read()
    iface_p = os.path.join(task_dir, "interface.txt")
    interface = open(iface_p).read() if os.path.isfile(iface_p) else ""
    if interface:
        prompt += "\n\nInterface:\n" + interface

    generation = load_generation()
    trace = [{"tool": "baseline_meta",
              "script_sha256": self_sha256(),
              "served_model": served_model(),
              "temperature": TEMPERATURE, "top_p": TOP_P,
              "max_tokens": MAX_TOKENS, "generation": generation, "track": track}]

    code, err = "", None
    t0 = time.time()
    try:
        d = post_json(BASE_URL + "/chat/completions", {
            # model 留空，由服务端选择默认模型
            "model": os.environ.get("MODEL_NAME") or (served_model() or "default"),
            "messages": [{"role": "system", "content": SYS[track]},
                         {"role": "user", "content": prompt}],
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "max_tokens": MAX_TOKENS,
            **generation,
        })
        choice = d["choices"][0]
        usage = d.get("usage", {})
        # 推理模型的思考 token 同样计入 max_tokens。预算耗尽时 content 为 null
        # 而非空串，此时按空产物处理。
        reply = choice["message"].get("content") or ""
        # 先记录 trace 再抽取代码，使 finish_reason 在任何情况下都有记录。
        trace.append({"ts": round(t0, 3), "tool": "llm", "round": 0,
                      "tokens_in": usage.get("prompt_tokens"),
                      "tokens_out": usage.get("completion_tokens"),
                      "finish": choice.get("finish_reason"),
                      "empty_content": not reply,
                      "sec": round(time.time() - t0, 2)})
        code = extract(reply, track)
    except Exception as e:                       # 超时、连不上、返回格式不对
        err = f"{type(e).__name__}: {e}"
        trace.append({"ts": round(t0, 3), "tool": "llm", "round": 0,
                      "error": err[:300], "sec": round(time.time() - t0, 2)})

    # 失败时写空产物，判定器按 L0 处理。
    with open(os.path.join(out_dir, OUT_NAME[track]), "w") as f:
        f.write(code)
    with open(os.path.join(out_dir, "trace.jsonl"), "w") as f:
        for e in trace:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if err:
        print(f"baseline: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
