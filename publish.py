# -*- coding: utf-8 -*-
"""mcu-debug-mcp PyPI 一键发布脚本（2026-10-15）

两阶段：
  python publish.py test    -> TestPyPI 演练（验证账号/包名/元数据全链路）
  python publish.py prod    -> 正式 PyPI（演练通过后执行）

前置（一次性，不写进任何文件的真实凭据）：
  1. pypi.org 注册账号 + 开 2FA
  2. 生成 API token（Account settings -> API tokens，scope: Entire account 或限定项目）
  3. TestPyPI 同理（test.pypi.org 单独注册）
  4. 把 token 写入 %USERPROFILE%\\.pypirc（模板见 .pypirc.template，填 token 即可）

发布纪律：
  - 发布前 git 工作树必须干净、测试必须全绿（本脚本会检查）
  - 正式发后不可覆盖同版本号——发错了只能 bump patch 重发
"""
import subprocess
import sys
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(cmd, **kw):
    print("$", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    return r.returncode == 0, r


def git_clean():
    ok, r = run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return ok and r.stdout.strip() == "", r.stdout


def version():
    with open(os.path.join(ROOT, "src", "mcu_debug_mcp", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
    return m.group(1) if m else None


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    if stage not in ("test", "prod"):
        print(__doc__)
        sys.exit(2)

    # 1) 工作树干净
    clean, out = git_clean()
    if not clean:
        print("[X] git 工作树不干净，先提交：\n" + out)
        sys.exit(1)

    ver = version()
    print("[i] 版本 %s | 阶段 %s" % (ver, stage))

    # 2) 全量测试
    ok, _ = run([PY, "-m", "pytest", "tests/", "-q", "--tb=line"])
    if not ok:
        print("[X] 测试未全绿，禁止发布")
        sys.exit(1)

    # 3) 干净重建
    import shutil
    if os.path.isdir(os.path.join(ROOT, "dist")):
        shutil.rmtree(os.path.join(ROOT, "dist"))
    ok, _ = run([PY, "-m", "build", "--no-isolation"])
    if not ok:
        print("[X] 构建失败")
        sys.exit(1)

    # 4) 元数据校验
    ok, _ = run([PY, "-m", "twine", "check", "dist/*"])
    if not ok:
        print("[X] twine check 未过")
        sys.exit(1)

    # 5) 干净 venv 装包握手（用户视角）
    venv = os.path.join(ROOT, ".publish_venv")
    if os.path.isdir(venv):
        shutil.rmtree(venv)
    ok, _ = run([PY, "-m", "venv", venv])
    pip = os.path.join(venv, "Scripts", "python.exe")
    ok, _ = run([pip, "-m", "pip", "install", "--quiet",
                 "--index-url", "https://pypi.org/simple",
                 os.path.join(ROOT, "dist",
                              "mcu_debug_mcp-%s-py3-none-any.whl" % ver)])
    if not ok:
        print("[X] 干净 venv 安装失败")
        sys.exit(1)
    exe = os.path.join(venv, "Scripts", "mcu-debug-mcp.exe")
    ok, _ = run([PY, os.path.join("tools_ci", "mcp_handshake_check.py"), exe])
    if not ok:
        print("[X] 握手验收未过")
        sys.exit(1)

    # 6) 上传
    if stage == "test":
        ok, _ = run([PY, "-m", "twine", "upload",
                     "--repository-url", "https://test.pypi.org/legacy/",
                     "dist/*"])
    else:
        print("[!] 正式发布确认：版本 %s 不可覆盖，确认继续？(yes/no)" % ver)
        if input().strip().lower() != "yes":
            print("已取消")
            sys.exit(1)
        ok, _ = run([PY, "-m", "twine", "upload", "dist/*"])
    if not ok:
        print("[X] 上传失败")
        sys.exit(1)

    # 7) 上传后验证（pypi.org 数据有延迟，几分钟后可再查）
    url = ("https://test.pypi.org/project/mcu-debug-mcp/%s/" % ver) if stage == "test" \
        else ("https://pypi.org/project/mcu-debug-mcp/%s/" % ver)
    print("[OK] 发布完成，页面：%s" % url)
    print("[i] 用户验证：pip install -i https://test.pypi.org/simple/ mcu-debug-mcp（test 阶段）")


if __name__ == "__main__":
    main()
