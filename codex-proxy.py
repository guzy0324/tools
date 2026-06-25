# codex_proxy.py
# 用代理调用 codex CLI 的小壳子

import os
import sys
import subprocess

# 1. 设置代理（按你实际代理改）
PROXY = "http://127.0.0.1:7890"  # TODO: 改成你的地址

os.environ["HTTP_PROXY"] = PROXY
os.environ["HTTPS_PROXY"] = PROXY
os.environ["ALL_PROXY"] = PROXY

# 2. 调用 codex
# 如果 codex 在 PATH 里，直接用 cmd.exe /c codex ...
cmd = ["cmd.exe", "/c", "codex", *sys.argv[1:]]

# 如果 codex 不在 PATH，可以写绝对路径，比如：
# REAL_CODEX = r"C:\Users\你的用户名\AppData\Roaming\npm\codex.cmd"
# cmd = ["cmd.exe", "/c", REAL_CODEX, *sys.argv[1:]]

result = subprocess.run(cmd)
sys.exit(result.returncode)
