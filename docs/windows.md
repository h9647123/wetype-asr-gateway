# Windows 桥接器

自行安装有合法使用权的 Windows 环境。本仓库不包含 Windows ISO、WeType 安装包或上游 bridge 二进制，也不修改激活状态。

实测基线：Windows 10 Enterprise LTSC 2021、WeType 3.0.0.38、WeChatASRBridge portable v0.3.3。3.0.0.38 存在上游“版本未验证”提示；原型真实识别通过不代表以后版本兼容。

## 材料

- [微信输入法官网](https://z.weixin.qq.com/)；原型安装包为 `https://download.z.weixin.qq.com/app/win/WeTypeSetup_3.0.0.38_26.exe`。保存为 `WeTypeSetup.exe`，安装脚本验证有效的 Tencent Authenticode 签名。
- [WeChatASRBridge v0.3.3](https://github.com/kuxiaowo/wechat-asr-bridge/releases/tag/v0.3.3) 的 portable 包，保存为 `bridge.zip`。
- portable ZIP SHA-256：`1db6c3664dde9883a1c08fe39c3834e7d44c767cc734a248865aeded6a4390f8`，已与上游 SHA256SUMS 核对。
- Linux 初始化生成的 `bridge-api-key.txt`，通过私密渠道复制到 Windows。不要把 key 写进仓库、命令行或普通文件共享。

## 安装

将两份下载材料放入本地目录。管理员 PowerShell 执行：

```powershell
.\windows\Install-ASR.ps1 -PayloadDirectory C:\Private\payload `
  -KeyFile C:\Private\bridge-api-key.txt -GatewayAddress 192.168.122.1
```

`GatewayAddress` 应替换为 Linux 网关访问 VM 时的实际源 IP。默认目录 `C:\WeTypeASR`，ACL 仅 SYSTEM / Administrators；Windows Firewall 的 8765 仅放行该网关。现有安装会被拒绝覆盖，升级需先备份、停止识别并人工检查。

任务 `WeType-ASR` 在开机后延迟 30 秒以 SYSTEM 启动，不依赖桌面自动登录。启动脚本监控实际 bridge 进程，并在需要时启动 `wetype_update.exe`。这个名字包含 update 的进程也承载本方案依赖的语音接口，不能通过删除或禁止它启动来“关闭更新”。

## 维护

```powershell
Get-ScheduledTask -TaskName WeType-ASR
Get-Process WeChatASRBridge,wetype_update
```

以私有网络为边界限制管理入口，避免将 WinRM / RDP / VNC 暴露到公网。重新启动后做真实音频识别验收。若需固定版本，先确认更新控制方式不会阻断语音网络；不要将“进程存在”当成版本锁定已验证。

## 可选：锁定 WeType 版本

专用 ASR 虚拟机如需固定当前版本，可在 **Windows Enterprise LTSC** 管理员 PowerShell 中执行：

```powershell
.\windows\Lock-WeTypeVersion.ps1 -BackupDirectory C:\WeTypeASR\version-lock-backup
```

这不是修改某个未经验证的注册表开关，而是阻止自动升级落地：

1. AppLocker 拒绝 `WeTypeSetup*.exe` / `WeTypeInstaller*.exe`，并显式启用 SYSTEM / 服务进程的规则执行；不阻止 `wetype_update.exe` 或 bridge。
2. 对现有 WeType 程序目录设置继承的写入、删除拒绝权限，阻止直接覆盖和补丁替换。
3. 在 hosts 中阻止已知官方安装包下载域名 `download.z.weixin.qq.com`，不屏蔽其他腾讯或语音服务域名。

目录原始 ACL、AppLocker 策略和 hosts 文件保存到私有备份目录。脚本不会重复覆盖已有备份；已有不同 AppLocker 模式时会停止，避免改变其他策略。Windows 系统自动更新和 WeType 自动升级是两件事，本脚本不修改 Windows Update 策略。

这是一套当前部署版本的防升级措施，不是抵御拥有管理员权限的恶意绕过者的安全边界，也不承诺所有未来版本永远使用同一安装器命名或下载域名。程序仍可能检查更新，但现有安装器执行、下载和目录覆盖受到限制。

原型已用 SYSTEM 任务验证：命名为升级安装器的无害探测程序被阻止，安装目录写入被拒绝；随后重启 VM 并重新验收真实 ASR。不要用真正的安装器做“尝试执行”测试。

需要手动升级时先停止识别、备份 VM，再运行 `Unlock-WeTypeVersion.ps1`。它恢复原 ACL、AppLocker 和 hosts 快照；**若这期间另有合法策略或 hosts 变更，先人工合并，勿直接覆盖**。完成升级后必须重新验收，不要自动追新版本。
