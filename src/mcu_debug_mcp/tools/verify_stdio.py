# -*- coding: utf-8 -*-
"""一次性验证脚本：真 MCP stdio 客户端连 server.py，核对 59 工具 / 16 资源 / 6 prompts。

用法（managed venv，已装 mcp<2）：
    python tools/verify_stdio.py

为什么单独写这个而不是只靠 pytest：审查报告（2026-10-13）指出 README 里那次
stdio 实测是 v4.3 时代的 35 工具，此后工具扩到 55 个从未重测过端到端。
单测全是 FakeBridge/mock，抓不到「注册层挂了 / schema 不让客户端列出」这类问题。
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # .../src/mcu_debug_mcp/tools
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # 仓库根（往上三级）
SRC = os.path.join(REPO, "src")
sys.path.insert(0, SRC)

from mcp import ClientSession, StdioServerParameters          # noqa: E402
from mcp.client.stdio import stdio_client                     # noqa: E402

WANT_TOOLS = 59
WANT_RESOURCES = 16
WANT_PROMPTS = 6


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(SRC, "mcu_debug_mcp", "server.py")],
        env={**os.environ,
             "MCU_DEBUG_MCP_MODE": "readonly",
             "MCU_DEBUG_MCP_PORT_DENY": "*",
             # server.py 以脚本方式拉起，包内绝对导入依赖 src 在子进程的 sys.path 上
             "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            n_tools = len(tools.tools)
            names = [t.name for t in tools.tools]
            # 每个工具必须有描述（AI 选工具全靠它）与合法 input schema
            no_desc = [t.name for t in tools.tools if not (t.description or "").strip()]
            bad_schema = [t.name for t in tools.tools
                          if not isinstance(t.inputSchema, dict)
                          or t.inputSchema.get("type") != "object"]

            resources = await session.list_resources()
            n_res = len(resources.resources)

            templates = await session.list_resource_templates()
            n_tpl = len(templates.resourceTemplates)

            prompts = await session.list_prompts()
            n_prompt = len(prompts.prompts)

            # 逐个读资源（静态 + 模板各读一个代表），确认不抛、内容非空
            read_ok, read_bad = 0, []
            static_uris = [str(r.uri) for r in resources.resources]
            for uri in static_uris:
                try:
                    r = await session.read_resource(uri)
                    if r.contents and any(
                            getattr(c, "text", "") or getattr(c, "blob", None)
                            for c in r.contents):
                        read_ok += 1
                    else:
                        read_bad.append("%s（空内容）" % uri)
                except Exception as e:                      # noqa: BLE001
                    read_bad.append("%s（%s）" % (uri, e))
            # 模板资源各读一个代表实例
            samples = {
                "mcu://driver/{name}": "mcu://driver/dht11",
                "mcu://wiring/{name}": "mcu://wiring/dht11",
                "mcu://pinmap/{mcu}": "mcu://pinmap/stm32f103c8t6",
                "mcu://board/{name}": "mcu://board/bluepill",
                "mcu://ecosystem/{name}": "mcu://ecosystem/zephyr",
                "mcu://errcode/{table}": "mcu://errcode/cme",
                "mcu://faultbits/{arch}": "mcu://faultbits/cortex_m",
            }
            tpl_read = 0
            for tpl in templates.resourceTemplates:
                uri = samples.get(tpl.uriTemplate)
                if not uri:
                    continue
                try:
                    r = await session.read_resource(uri)
                    if r.contents:
                        tpl_read += 1
                except Exception as e:                      # noqa: BLE001
                    read_bad.append("%s（%s）" % (uri, e))

            # 工具端到端调一个纯离线工具（不碰硬件）
            call_out = None
            try:
                r = await session.call_tool("env_status", {})
                call_out = (r.content[0].text[:160]
                            if r and r.content else "(空)")
            except Exception as e:                          # noqa: BLE001
                call_out = "调用失败：%s" % e

            print("=" * 60)
            print("MCP stdio 端到端（真客户端 initialize → list/read/call）")
            print("=" * 60)
            print("工具        : %d（期望 %d）%s" % (
                n_tools, WANT_TOOLS, "OK" if n_tools == WANT_TOOLS else "✗ 不符"))
            print("  无描述    : %s" % (no_desc or "无"))
            print("  schema 坏 : %s" % (bad_schema or "无"))
            print("静态资源    : %d（期望 %d）%s" % (
                n_res, 6, "OK" if n_res == 6 else "✗ 不符"))
            print("模板资源    : %d（期望 %d）%s" % (
                n_tpl, 7, "OK" if n_tpl == 7 else "✗ 不符"))
            print("  资源合计  : %d（期望 %d）%s" % (
                n_res + n_tpl, WANT_RESOURCES,
                "OK" if n_res + n_tpl == WANT_RESOURCES else "✗ 不符"))
            print("  静态可读  : %d/%d" % (read_ok, n_res))
            print("  模板可读  : %d/%d" % (tpl_read, len(samples)))
            if read_bad:
                print("  读失败    : %s" % read_bad)
            print("prompts     : %d（期望 %d）%s" % (
                n_prompt, WANT_PROMPTS,
                "OK" if n_prompt == WANT_PROMPTS else "✗ 不符"))
            print("env_status  : %s" % call_out)
            print("=" * 60)
            ok = (n_tools == WANT_TOOLS and not no_desc and not bad_schema
                  and n_res + n_tpl == WANT_RESOURCES
                  and read_ok == n_res and tpl_read == len(samples)
                  and not read_bad and n_prompt == WANT_PROMPTS
                  and call_out and "调用失败" not in call_out)
            print("结论：%s" % ("全部通过" if ok else "存在不符项（见上）"))
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
