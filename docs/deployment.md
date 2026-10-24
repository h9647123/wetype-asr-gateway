# Ubuntu 部署与局域网管理

## 私有配置

默认 `WETYPE_DATA_DIR=./data`，配置为该目录的 `config.json`。systemd 示例使用 `/var/lib/wetype-asr`。可通过 `WETYPE_CONFIG` 指定单独路径。不要把生成的配置、console token、key 文件加入 Git。

`allowed_console_origins` 必须精确列出你用于管理的 origin（含非默认端口，不带路径）。例如本机 `http://127.0.0.1:18080`，或你自己的局域网管理入口。管理变更需要登录 cookie/令牌、匹配的 Host 与 Origin，以及动态 `X-Console-Action`，不能把公网页面设为控制台入口。

## systemd

以下命令在 Ubuntu 上作为管理员执行。先审查配置中的 VM 地址、目录和 origin。仓库代码目录保持 root 所有，应用只写入私有数据目录。

```bash
sudo useradd --system --home /var/lib/wetype-asr --shell /usr/sbin/nologin wetype-asr
sudo install -d -m 700 -o wetype-asr -g wetype-asr /var/lib/wetype-asr
# 将仓库放到 /opt/wetype-asr-gateway，并在其中创建 .venv / 安装 requirements.txt
sudo -u wetype-asr /opt/wetype-asr-gateway/.venv/bin/python \
  /opt/wetype-asr-gateway/scripts/init_config.py \
  --data-dir /var/lib/wetype-asr --upstream http://VM_IP:8765 \
  --public-base https://asr.example.com --console-origin http://YOUR_LAN_IP:8080
sudo install -m 644 deploy/systemd/wetype-asr-console.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/wetype-iat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wetype-asr-console wetype-iat
```

数据已存在时不要重新初始化；备份后手动编辑相应字段。已有 key 的摘要与 scope 不会自动迁移，需显式导入或通过控制台创建。

## VM 与可选重启按钮

KVM 中自行创建名为 `wetype-asr` 的专用 Windows VM。原型使用 4 vCPU、6 GiB RAM、80 GiB qcow2，但资源应按实际需要调整。安装授权由使用者负责。

`scripts/vm_control.py` 只操作配置的 VM，正常关机最多等待 120 秒，超时不会强制断电。`WETYPE_VM_NAME` 可改变目标名。若 VM 镜像位于另一挂载点，给 `wetype-asr-vm.service` 加上正确的 `RequiresMountsFor=`。

有些 Windows VM 会忽略 ACPI 电源按钮。此时“Linux 服务重新进入 active”不代表 Windows 真正重启，必须核对 QEMU 进程和 Windows `LastBootUpTime`。可以安装 `requirements-winrm.txt`，配置私有 WinRM HTTPS 凭据，并在 VM unit 的 root-only drop-in 中设置：

```ini
[Service]
Environment=WETYPE_SHUTDOWN_HELPER=/opt/wetype-asr-gateway/scripts/winrm_shutdown.py
Environment=WETYPE_VM_CREDENTIALS=/etc/wetype-asr/vm-credentials.json
```

helper 须 root 所有并有执行权限，凭据文件 root:root / 600，不给 API 服务读权限。凭据字段为 `ip`、`username`、`password`；默认验证证书，可配置受信任证书。只有你明确接受私有 NAT 自签证书风险时才设置 `certificate_validation="ignore"`。WinRM 5986 仅放行宿主机，不公开到互联网。该 helper 请求 Windows 自身关机，控制器等待 VM 完全停止后再启动；不调用 libvirt destroy。升级后请先单独测试此关机路径，再使用控制台重启按钮。

```bash
sudo install -m 644 deploy/systemd/wetype-asr-vm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wetype-asr-vm
```

重启按钮默认禁用。若需要启用，先安装仅允许两个固定操作的 root-owned helper：

```bash
sudo install -d -o root -g root -m 755 /usr/local/libexec
sudo install -o root -g root -m 755 scripts/restart_helper.py /usr/local/libexec/wetype-asr-restart
sudo visudo -cf deploy/sudoers/wetype-asr
sudo install -o root -g root -m 440 deploy/sudoers/wetype-asr /etc/sudoers.d/wetype-asr
```

在私有 `config.json` 中设置 `enable_restart=true`。不要授予应用 `NOPASSWD: ALL` 或任意 `systemd-run` 权限。helper 自身及父目录不能由应用账户写入，目标服务固定为：

- ASR：`wetype-asr-vm.service` 和 `wetype-iat.service`。
- Search：`wetype-asr-console.service`，同时短暂中断原 `/api/asr`。

Search 是同一 Python 服务里的 RSS wrapper，没有可独立重启的搜索模型；此按钮重启共用 API 进程。控制台页可见时每 15 秒检查健康，Search 实际网络探测缓存 60 秒，失败显示错误；不会因为健康失败而自动重启 VM。

## Nginx 与现有博客

1. 参照 `deploy/nginx/upstreams.conf.example` 在 `http {}` 定义 `wetype_backend`，修改成实际私有 VM 地址。
2. 在现有博客的 `server {}` 中 include `public-routes.conf`。保留原 root、TLS、首页和其他业务路由。已有相同 location 时先合并，不要重复添加。
3. Cloudflare Tunnel 若转发 HTTP origin，相关路由也需位于其实际命中的 HTTP vhost；不必公开 VM 或 8765 端口。确认公网 WebSocket 可用。
4. 使用独立的 `lan-console.conf.example` 管理入口，按实际局域网 IP 修改监听、allow 和 `allowed_console_origins`。示例默认只允许回环，修改防火墙后才开放 LAN。
5. 用 `scripts/console_url.py --data-dir /var/lib/wetype-asr --origin http://YOUR_LAN_IP:8080` 生成私有登录链接；不要公开链接。
6. 执行 `sudo nginx -t`，通过后 `sudo systemctl reload nginx`。从公网确认博客正常、无 key 请求拒绝、`/console-api/` 返回 404。

上游直接指向私有 VM，避免套娃代理中的 real-IP / allow 规则误把公网客户端拒绝。WSS 必须保留原 Host；签名查询 URL 关闭 access log 和非严重错误日志。

## 检查与恢复

```bash
systemctl is-enabled wetype-asr-vm wetype-iat wetype-asr-console
systemctl is-active wetype-asr-vm wetype-iat wetype-asr-console
curl --fail http://127.0.0.1:8768/health
sudo virsh domstate wetype-asr
journalctl -u wetype-iat -u wetype-asr-console --since '10 minutes ago'
```

健康接口是进程探测，不保证腾讯云真实识别成功；更改后必须使用实际音频确认。先备份配置和 VM 镜像再升级，上游 key 轮换需同步 Windows `api-key.txt`、网关配置和调用方。不要在 Windows 更新中强制重启或关机。
