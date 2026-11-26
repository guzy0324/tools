# tools

## Clash Dashboard 自启动

使用平时运行看板的 Python 环境安装。脚本会尝试自动定位 Clash Verge 的运行配置，找到多份配置时可用 `--config` 指定。

```sh
python clash_dashboard.py --install
```

安装默认启用看板和自动切换，也可以用 `--install --dry-run` 仅测速。重复安装会更新启动参数。

- Windows 在任务计划程序中注册 `Clash Dashboard`，当前用户登录 30 秒后后台启动，保留托盘。
- Ubuntu 使用普通用户安装。脚本会写入 `~/.config/autostart/clash-dashboard.desktop`（如果设置了 `XDG_CONFIG_HOME`，则使用该目录）。注销再登录桌面后，托盘、看板和自动选节点在同一个进程中启动。托盘里的「退出看板」会同时关闭三者。
- Ubuntu 托盘要求当前 Python 能导入 GTK 3 和 Ayatana AppIndicator。系统依赖可用 `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1` 安装。GNOME 桌面需启用 AppIndicator 扩展；只需要网页看板和自动选节点时可用 `--install --no-tray`。

看板默认在 `http://127.0.0.1:5050/`。直接运行 `python clash_dashboard.py` 也会同时启动看板和自动选节点。在「转发订阅」勾选「启用转发」后会显示远程订阅和复制链接。远程订阅默认全不选，勾选后才会显示对应链接。每次勾选或取消都会自动保存，重启后自动恢复。

可以在每条转发链接下填写并复制完整的别名 URL。自定义 URL 使用看板服务器的 IP 和端口，路径以 `/subscription/` 开头。能访问该服务器且知道完整地址的客户端可以下载订阅。

订阅转发使用 `psutil` 枚举本机网卡，可用 `python -m pip install psutil` 安装。复制的链接优先使用服务器网卡的公网 IPv4，其次使用局域网 IPv4。客户端需要能访问该地址和看板端口。

如果看板运行在 SSH lab 上，在 Windows 运行 `ssh -N -L 127.0.0.1:15050:127.0.0.1:5050 lab`，再打开 `http://127.0.0.1:15050/` 管理转发。复制的订阅链接会使用 lab 的 IP。

## 后台运行 DevSpace（Windows PowerShell）

安装命令入口（首次或移动脚本位置后执行）：

```powershell
& C:\Users\guzy0\Desktop\codes\tools\install-devspace.ps1
```

新开 PowerShell 终端，在希望开放的项目目录中运行：

```powershell
devspace_serve     # 后台启动 server 和 Cloudflare 隧道，打印 MCP 地址和 Owner password
devspace_status    # 查看运行状态、MCP 地址、进程、开放目录及日志位置
devspace_stop      # 停止此管理器启动的 server 和隧道
```

运行 `devspace_serve` 后可以关闭终端。进入其他项目目录再次执行 `devspace_serve`，会将该目录追加到同一份 `workspaces.allowedRoots`，只重启 server，保留隧道、端口和 MCP 地址。同目录重复调用不重启。配置中的其他 allowed roots 会保留。

每次 `devspace_serve` 会先检查 npm 上的 DevSpace 最新版本；有更新时在当前终端询问是否安装，默认不更新。网络检查失败会继续启动。后台运行中更新时，仅暂停并重启 server，保留隧道。非交互调用默认跳过安装，可设置 `$env:DEVSPACE_UPDATE='y'` 自动安装更新，或 `$env:DEVSPACE_UPDATE='n'` 只检查不安装；`$env:DEVSPACE_UPDATE_CHECK='0'` 完全跳过检查。

`devspace_stop` 后再次 `devspace_serve` 会创建新的 Quick Tunnel，公网地址可能改变。Server 重启期间连接会短暂中断；客户端可能需要重连。后台服务不会随系统重启自动启动。

使用官方 `~/.devspace/config.jsonc` 和 `auth.json`，也支持通过 `DEVSPACE_CONFIG_DIR` 选择另一套配置。首次使用前运行 `devspace init`。首次修改配置前会备份为 `config.jsonc.before-launcher.bak`；配置经官方 CLI 读取和校验，写回时转为无注释的 JSON。

`devspace_status` 只检查本机进程及 server，不代表公网隧道端到端可用。日志位于配置目录下的 `launcher/`。Server 退出后可用 `devspace_serve` 重启；隧道退出后使用 `devspace_stop`、`devspace_serve` 重新启动。

安装器在当前用户的 PowerShell AllHosts profile 中添加三个独立函数：`devspace_serve`、`devspace_stop`、`devspace_status`，并移除之前安装的 `devspace` 覆盖。官方 `devspace` 命令保持原样，可继续使用 `devspace init`、`devspace doctor` 等子命令。既有前台进程不由此管理器接管，切换前请在原窗口停止它们。

测试：

```powershell
node --test devspace-manager.test.mjs devspace-update.test.mjs
```

测试使用独立配置、模拟 server 和模拟隧道，验证后台生命周期、并发去重、新目录重启 server 而保留隧道、配置保留、停止和启动失败处理，不会连接真实公网隧道。
