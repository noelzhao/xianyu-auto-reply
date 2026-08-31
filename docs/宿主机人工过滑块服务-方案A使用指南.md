# 方案A：宿主机人工过滑块服务（使用指南）

> 适用版本：Docker Compose 部署（backend-web + websocket 容器）
> 目标：闲鱼触发滑块验证时，不再由容器内无头浏览器自动破解，而是弹出**宿主机真实浏览器窗口**，由人工拖动滑块完成验证，验证结果（新 x5sec Cookie）自动回传容器继续后续 Token 刷新流程。

---

## 1 原理

```
闲鱼要求滑块验证
   │
   ▼
websocket 容器（cookie_token_manager）
   │  读取系统设置「远程过滑块配置」3 项
   │  走远程接口（orchestrator._call_remote_solve）
   ▼
http://host.docker.internal:8787/solve   ←── 容器通过 extra_hosts 访问宿主机
   │
   ▼
宿主机 Python 服务（scripts/host_slider_solver.py）
   │  弹出真实 Edge 窗口（有头模式，人工可见）
   │  注入账号 Cookie 保持登录态
   ▼
人工拖动滑块 → 页面生成新 x5sec Cookie
   │  服务每 0.5s 轮询检测（旧 x5sec 不算数，只认新值）
   ▼
返回 {success, data:{cookies}} → websocket 更新 Token → 继续自动回复
```

- 容器侧**零代码改动**，仅依赖 compose 新增的 `extra_hosts: host.docker.internal:host-gateway`。
- 远程服务仅在「管理员配置了远程过滑块 URL + 秘钥」时才被调用；未配置时行为与之前完全一致。
- 远程调用失败/超时会**自动回退本机逻辑**，不会中断原有流程（视「本机滑块不处理」开关而定，见 4.4）。

---

## 2 第一次部署（一次性）

### 2.1 宿主机安装依赖（仅 Python 3.9+）

```bash
python -m pip install playwright
```

> 服务默认使用 **系统自带 Microsoft Edge**（`channel="msedge"`），**无需**执行 `playwright install` 下载 Chromium；Edge 未安装时可改用 `--browser chrome`。

### 2.2 启动服务

```bash
cd E:\Noel\starinfi\xianyu-auto-reply
python scripts/host_slider_solver.py --port 8787 --secret 你的秘钥
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | 8787 | 监听端口，需与 2.3 配置的 URL 一致 |
| `--secret` | 环境变量 `SLIDER_SOLVER_SECRET` | 秘钥，**必须**与 2.3 填写的一致 |
| `--browser` | `msedge` | 有头浏览器通道：`msedge` / `chrome` / `chromium` |
| `--timeout` | 自动(90~240s) | 人工滑动超时秒数，一般不用改 |

启动日志出现 `宿主机人工过滑块服务已启动` 即就绪；健康检查：`GET http://127.0.0.1:8787/health`。

> 建议：把启动命令做成 Windows 开机自启任务（任务计划程序），或保存到 `.bat` 双击启动。
> 安全：`--secret` 请用强口令；如端口仅本机使用，可在 Windows 防火墙限制 8787 仅允许本机/Docker 网段访问。

### 2.3 配置远程过滑块（网页操作）

1. 登录系统管理后台（`http://localhost:9000`，管理员账号）。
2. 进入「风控日志」页（管理员菜单）。
3. 找到 **「远程过滑块配置」** 卡片，填写：
   - **远程服务 URL**：`http://host.docker.internal:8787/solve`
   - **秘钥**：与 `--secret` 一致
   - **传递 Cookie**：建议开启（把账号 Cookie 注入弹出窗口，保持登录态，减少人工重新登录）
4. 点「测试连接」：应提示 `连接成功（远程返回：ok）`。
5. 保存。

> 注意：这里的「远程过滑块配置」在**风控日志页**，与「系统设置-基础设置」里的 Token 获取方式（`token.remote_url`）是**两个不同配置**，不要混淆。

> 生效时机：websocket 每次触发滑块时实时读取该配置，**保存后立即生效，无需重启容器**。

### 2.4 重启容器（compose 已加 extra_hosts）

```bash
docker compose up -d backend-web websocket
```

确认两个容器 healthy，并验证容器内可达宿主机服务：

```bash
docker exec xianyu-backend-web curl -s http://host.docker.internal:8787/health
docker exec xianyu-websocket   curl -s http://host.docker.internal:8787/health
# 均返回 {"success": true, "message": "alive", ...} 即成功
```

> 已完成：本项目 `docker-compose.yml` 已为 backend-web、websocket 两个服务添加 `extra_hosts: ["host.docker.internal:host-gateway"]`，无需再改。

---

## 3 日常使用

1. 保持宿主机服务运行（窗口最小化即可）。
2. 闲鱼触发滑块时：
   - 宿主机**自动弹出 Edge 窗口**，标题/地址为闲鱼验证页；
   - 控制台日志显示 `请在弹出的浏览器窗口中拖动滑块`；
   - 人工完成拖动；
3. 验证通过后窗口自动关闭（或由服务关闭），容器继续刷新 Token，自动回复恢复正常。
4. 宿主机日志会打印 `人工滑块验证成功（耗时 Xs），共提取 N 个 Cookie`。

一个账号同时只弹一个窗口；若多个账号同时触发，其余请求返回 503，调用方会自动回退或排队等待。

---

## 4 常见问题

### 4.1 测试连接失败

| 现象 | 原因/处理 |
| --- | --- |
| `无法连接到远程服务` | 宿主机服务未启动；端口不对；防火墙拦截；确认 `--port` 与 URL 一致 |
| `连接成功，但秘钥无效` | URL 通了但秘钥不符；与 `--secret` 核对一致后重试 |
| `远程服务返回异常（HTTP 404）` | URL 拼错；应带 `/solve` 后缀（`http://host.docker.internal:8787/solve`） |

### 4.2 能连通但不弹浏览器窗口

- 确认实际触发的是**滑块验证**（Token 获取）而非其它风控；
- 查看 websocket 日志，确认是否配置已加载（触发时有 `远程过滑块` 相关日志）；
- 检查弹出的窗口是否被 Windows 最小化/遮挡到副屏。

### 4.3 弹窗出来但一直没人工操作、超时了

按设计会返回超时失败并回退本机逻辑（见 4.4），属正常兜底；人工重新触发后再次弹窗。

### 4.4 希望「远程失败时不要自动破解滑块」

网页「风控日志」页开启 **「本机滑块不处理」** 开关（`updateLocalSliderConfig`）：
- 开启后，远程失败时不会再走到容器内无头自动滑动（避免自动破解频繁触发风控）；
- 代价是远程失败的那一轮 Token 刷新会等待下一周期重试。

### 4.5 验证链接过期（url_expired）

闲鱼验证链接有时效；服务检测到页面提示「页面访问出现了问题」时返回 `url_expired`，
调用方会自动使用新链接重试（最多 2 次），无需人工干预。

### 4.6 Docker 无法访问 host.docker.internal

- 本机 Docker Desktop + WSL2：compose 已加 `host.docker.internal:host-gateway`，通常无需处理；
- 若仍不通：确认 Docker Desktop 版本较新；或在宿主机防火墙放行 8787 入站（仅限 Docker 网段）。

---

## 5 测试方法（可选，用于自检）

`scripts/test_pages/` 下有两个**模拟页面**，可离线验证服务行为：

```bash
# 1) 起模拟验证页
python -m http.server 9001 --directory scripts/test_pages
# 2) 新终端启动服务
python scripts/host_slider_solver.py --port 8787 --secret test123 --timeout 10
# 3) 模拟"人工拖滑块"（页面 3 秒后自动产生新 x5sec）
curl -X POST http://127.0.0.1:8787/solve -H "Content-Type: application/json" \
  -d '{"secret_key":"test123","account_id":"demo","url":"http://127.0.0.1:9001/sim_slider_page.html","browser_timeout":20,"cookies":"cna=abc; x5sec=OLD"}'
# 预期: {"success": true, ... "x5sec": "NEW-VALUE-FROM-SIMULATED-SLIDE"}（旧值不算，只认新值）
```

| 页面 | 用途 |
| --- | --- |
| `sim_slider_page.html` | 3 秒后生成新 x5sec + 4 秒后自动关窗，模拟人工验证成功 |
| `expired_page.html` | 展示「页面访问出现了问题」，模拟链接过期（返回 url_expired） |

---

## 6 飞书 Webhook 告警通知

当滑块验证失败（无论使用方案 A 远程过滑块还是容器内 DrissionPage 兜底）导致 Token 刷新失败时，系统自动通过飞书自定义机器人 Webhook 发送告警通知，提醒人工介入更新 Cookie；Token 恢复成功后自动发送恢复通知。

### 6.1 工作原理

```
滑块验证失败 / 重试上限
    │
    ▼
cookie_token_manager 调用 notify_cookie_update_needed()
    │  带 10 分钟防刷（同一账号冷却期内不重复通知）
    │  标记账号为「异常」状态
    ▼
feishu_notify.send_feishu_message() → 飞书群收到告警消息
    │  含账号名、失败原因、操作步骤
    ▼
人工过滑块并更新 Cookie → Token 刷新成功
    │
    ▼
cookie_token_manager 调用 notify_account_recovered()
    │  仅对曾告警的账号发送（正常刷新不发恢复通知）
    ▼
feishu_notify.send_feishu_message() → 飞书群收到恢复消息
```

### 6.2 触发时机

| 触发点 | 代码位置 | 通知类型 | 说明 |
|--------|----------|----------|------|
| 滑块验证失败 | `cookie_token_manager.py` ~1330 行 | 告警 | 未获取到 x5sec cookie，需人工过滑块 |
| 滑块重试上限 | `cookie_token_manager.py` ~1159 行 | 告警 | 重试次数耗尽（默认上限），需人工处理 |
| Token 刷新成功 | `cookie_token_manager.py` ~1243 行 | 恢复 | 仅对曾告警的账号发送，正常刷新不通知 |

### 6.3 配置

飞书 Webhook 地址以硬编码默认值的方式内置在 `common/services/feishu_notify.py` 中：

```python
_DEFAULT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/10250c18-6c5a-4677-b5d5-d6530e80064b"
```

如需更换 Webhook 地址，支持通过数据库覆盖（无需改代码）：

| DB key (`xy_system_settings` 表) | 默认值 | 说明 |
|----------------------------------|--------|------|
| `notification.feishu_webhook_url` | 硬编码值 | 飞书机器人 Webhook 地址，设置后覆盖默认值 |
| `notification.feishu_enabled` | `true` | 是否启用飞书通知，设为 `false` 可关闭 |

> 配置后即时生效，下次触发通知时读取最新值，无需重启容器。

### 6.4 防刷机制

- **告警防刷**：同一账号 10 分钟（`_NOTIFY_COOLDOWN_SECONDS = 600`）内只发送一次告警通知，避免滑块反复失败时刷屏。
- **恢复通知过滤**：只有之前发过告警的账号才会收到恢复通知；从未异常的账号正常刷新 Token 不会发通知。
- **状态追踪**：模块内用 `_account_alerting` 集合追踪异常账号，发送恢复通知后自动移除标记。

### 6.5 消息格式

告警消息示例：

> **闲鱼账号需要手动更新Cookie**
>
> 账号 [xiaoyu] Token 刷新失败，滑块验证未通过
> 原因: 滑块验证失败，未获取到 x5sec cookie
>
> 操作步骤:
> 1. 浏览器打开 goofish.com（闲鱼网页版）
> 2. 通过滑块验证
> 3. 确认 Cookie 中包含 x5sec
> 4. 复制完整 Cookie 更新到系统
>
> 时间: 2026-08-14 17:28:24

恢复消息示例：

> **闲鱼账号已恢复**
>
> 账号 [xiaoyu] Token 刷新成功，已恢复正常运行
>
> 时间: 2026-08-14 17:35:00

### 6.6 验证通知

部署后可手动发送测试消息确认飞书通知链路正常：

```bash
docker exec xianyu-websocket python -c "
import asyncio
from common.services.feishu_notify import send_feishu_message

async def test():
    ok = await send_feishu_message(
        '闲鱼自动回复系统通知',
        '这是一条测试消息：飞书 Webhook 通知功能已配置成功。'
    )
    print('Send result:', ok)

asyncio.run(test())
"
```

返回 `Send result: True` 且飞书群收到消息即配置成功。

### 6.7 相关文件

| 文件 | 说明 |
|------|------|
| `common/services/feishu_notify.py` | 飞书通知模块（发送、防刷、状态追踪） |
| `websocket/app/services/xianyu/cookie_token_manager.py` | 3 处插入点：滑块失败、重试上限、Token 成功 |

---

## 7 相关文件

| 文件 | 说明 |
| --- | --- |
| `scripts/host_slider_solver.py` | 宿主机人工过滑块服务（本方案核心） |
| `scripts/test_pages/` | 自检用模拟页面（可删除） |
| `docker-compose.yml` | 已为 backend-web / websocket 添加 `host.docker.internal` 映射 |
| `common/services/feishu_notify.py` | 飞书 Webhook 告警通知模块（见第 6 节） |