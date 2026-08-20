# WorkBuddy Auth Token 获取方法



## ⚠️ 合法性边界


本文所有方法都建立在「**你已用自己账号登录 WorkBuddy 桌面端**」这一前提之上。token 是你个人会话令牌，与账号绑定，不共享。

---

## 📋 前置条件

1. 你已在 WorkBuddy 桌面端**登录**（QQ / 微信 / 腾讯账号任一）。
2. 桌面端至少成功启动过一次（这样它才会在本地写入 auth 文件）。

---

## 方法一：读取本地明文 auth 文件（推荐，最简单）

WorkBuddy 桌面端登录后，会把认证信息以**明文 JSON** 写入本地用户数据目录。这是最直接、最可靠的方式——不需要任何逆向，只是读你自己账号产生的文件。

### auth 文件路径

| 平台 | 路径 |
|------|------|
| **macOS** | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |
| **Windows** | `%APPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |

> **关于 Windows 路径**：`%APPDATA%` 通常展开为 `C:\Users\<你的用户名>\AppData\Roaming`。
> macOS 路径已在本项目实测确认存在；Windows 路径基于 Electron 标准约定（`app.getPath('userData')` 在 Windows 上默认指向 `%APPDATA%\<应用名>`）推断，**请在 Windows 上实测确认**，若路径不同欢迎反馈订正。

### macOS —— 一行命令提取 access_token

打开 **Terminal**（终端），粘贴：

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info'))); print(d['auth']['accessToken'])"
```

> macOS 自带 `python3`（Apple 提供的 Python 3），无需额外安装。

### Windows —— 一行命令提取 access_token

打开 **PowerShell**，粘贴：

```powershell
((Get-Content "$env:APPDATA\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info" -Raw | ConvertFrom-Json).auth.accessToken)
```

> PowerShell 是 Windows 自带的，`ConvertFrom-Json` 是其内置 cmdlet，无需安装。

### 想看完整文件内容？

**macOS：**
```bash
cat ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

**Windows：**
```powershell
Get-Content "$env:APPDATA\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info" -Raw | ConvertFrom-Json
```

---

## 方法二：用 refresh_token 换取新的 access_token（跨平台通用）

当你已有的 `access_token` 过期，但 `refresh_token` 还没过期时，可以用它换一个新的 `access_token`。这个方法用 `curl`，**Windows 10 (1803+) 和 macOS 都自带 curl**，完全跨平台。

### 刷新请求

```bash
curl -X POST https://www.codebuddy.cn/v2/plugin/auth/token/refresh \
  -H "Authorization: Bearer <你的_access_token>" \
  -H "X-Refresh-Token: <你的_refresh_token>" \
  -H "X-Auth-Refresh-Source: plugin" \
  -H "X-Product: SaaS" \
  -H "Content-Type: application/json" \
  -d "{}"
```

**响应里**（HTTP 200）的关键字段：

```json
{
  "data": {
    "accessToken": "eyJhbGciOi...<新的 access_token>",
    "refreshToken": "eyJhbGciOi...<可能更新的 refresh_token>",
    "expiresIn": 5184000,
    "refreshExpiresIn": 7776000
  }
}
```

> 提示：先按**方法一**从 auth 文件里拿到 `accessToken` 和 `refreshToken`，再填到上面的命令里。

### 从 auth 文件直接读出 refresh_token

**macOS：**
```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info'))); print(d['auth']['refreshToken'])"
```

**Windows：**
```powershell
((Get-Content "$env:APPDATA\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info" -Raw | ConvertFrom-Json).auth.refreshToken)
```

---

## 方法三：设置环境变量给本项目使用

拿到 token 后，设置环境变量即可让本项目的代理服务使用它：

### macOS / Linux

```bash
export CODEBUDDY_AUTH_TOKEN="你拿到的_access_token"
export API_KEY="你自定义的代理密钥"
python server.py
```

### Windows（PowerShell）

```powershell
$env:CODEBUDDY_AUTH_TOKEN = "你拿到的_access_token"
$env:API_KEY = "你自定义的代理密钥"
python server.py
```

---

## 方法四：MITM 代理截获（高级 / 可选）

> ⚠️ 此方法用于**分析你自己账号的请求协议**、验证字段含义等研究目的。需要你完全控制本机环境。

### 原理

WorkBuddy 桌面端基于 Electron，其网络层（`net-log.js`）会读取 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量构造 `ProxyAgent`；同时**未实现 TLS 证书锁定（no certificate pinning）**，信任系统 CA。因此：

```
┌─────────────┐    HTTPS_PROXY     ┌──────────────┐    解密后可见     ┌──────────┐
│ WorkBuddy   │ ─────────────────▶ │  本地代理     │ ───────────────▶ │ 后端服务器│
│ 桌面端       │   信任你的 CA      │ mitmproxy/   │   Authorization  │           │
│             │ ◀───────────────── │  Charles     │ ◀──────────────── │           │
└─────────────┘    回包            └──────────────┘                   └──────────┘
                                          │
                                          ▼
                                   你能看到所有请求头里的
                                  Authorization: Bearer <token>
```

### 步骤

1. **启动一个本地 HTTPS 代理**，监听 `127.0.0.1:8080`：
   - `mitmproxy`（命令行，免费）
   - `Charles`（GUI，付费试用）
   - `Fiddler`（Windows，免费）

2. **把代理的 CA 证书安装进系统钥匙串并设为「始终信任」**：
   - macOS：钥匙串访问 → 导入 mitmproxy 的 CA (`~/.mitmproxy/mitmproxy-ca-cert.pem`) → 设为始终信任
   - Windows：证书管理器 (`certmgr.msc`) → 受信任的根证书颁发机构 → 导入

3. **设置环境变量后启动 WorkBuddy**：

   **macOS / Linux：**
   ```bash
   export HTTPS_PROXY=http://127.0.0.1:8080
   export HTTP_PROXY=http://127.0.0.1:8080
   open /Applications/WorkBuddy.app
   ```

   **Windows（PowerShell，随后手动启动 WorkBuddy）：**
   ```powershell
   $env:HTTPS_PROXY = "http://127.0.0.1:8080"
   $env:HTTP_PROXY = "http://127.0.0.1:8080"
   ```

4. **在代理界面里观察请求**，筛选目标域名（`codebuddy.cn` / `copilot.tencent.com` 等），任意请求的 `Authorization` 头就是你的 Bearer token。

> 本项目仓库同级的 `cnvd_poc/poc_proxy.py` 是一个现成的 mitmproxy 插件示例（它会自动脱敏，只记录长度，不导出完整 token）。

---

## 📦 auth 文件结构（参考）

auth 文件是明文 JSON，关键字段在 `auth` 和 `account` 两个对象里：

```
workbuddy-desktop.info
├── account
│   ├── uid            账号唯一 ID（UUID）
│   ├── nickname       昵称
│   ├── uin            内部账号编号
│   ├── type           账号类型（personal）
│   └── phoneNumber    绑定手机号
└── auth
    ├── accessToken    ← 你要的 access_token（JWT, RS256 签名）
    ├── refreshToken   ← 刷新令牌（JWT, HS512 签名）
    ├── tokenType      固定 "Bearer"
    ├── domain         "www.codebuddy.cn"
    ├── expiresAt      access_token 绝对过期时间（毫秒时间戳）
    ├── refreshExpiresAt  refresh_token 绝对过期时间（毫秒时间戳）
    ├── expiresIn      access_token 相对有效期（秒，如 5184000 = 60 天）
    └── refreshExpiresIn  refresh_token 相对有效期（秒，如 7776000 = 90 天）
```

### 快速查看过期时间

**macOS：**
```bash
python3 -c "
import json,os,time
d=json.load(open(os.path.expanduser('~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info')))
a=d['auth']
for k in ('expiresAt','refreshExpiresAt'):
    ts=a[k]/1000
    print(f'{k}: {time.strftime(\"%Y-%m-%d %H:%M:%S\", time.localtime(ts))} ({int((ts-time.time())/86400)} 天后)')
"
```

**Windows：**
```powershell
$j = Get-Content "$env:APPDATA\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info" -Raw | ConvertFrom-Json
[datetimeoffset]::FromUnixTimeMilliseconds($j.auth.expiresAt).LocalDateTime
[datetimeoffset]::FromUnixTimeMilliseconds($j.auth.refreshExpiresAt).LocalDateTime
```

---

## 🛡️ 安全注意事项

1. **token 即账号**：`access_token` 等同于你的登录凭证，泄露后他人可冒用你的身份和配额。请像对待密码一样保管。
2. **不要提交进仓库**：本项目 `.gitignore` 已排除 `tokens.json`；也**不要**把含真实 token 的命令历史或截图公开分享。
3. **定期更换**：如怀疑泄露，在 WorkBuddy 桌面端重新登录即可生成新 token，旧 token 随之失效。
4. **refresh_token 慎用**：它的有效期更长（约 90 天），泄露后果更严重；仅在确实需要刷新时才取出使用。

---

## ❓ 常见问题

**Q: auth 文件不存在？**
A: 说明 WorkBuddy 桌面端还没登录或从未在本机启动过。请先打开 WorkBuddy 完成一次登录。

**Q: token 拿到了但调用报 401？**
A: `access_token` 可能已过期（约 60 天）。用**方法二**拿 `refresh_token` 换新的，或直接在桌面端重新登录刷新 auth 文件。

**Q: Windows 上 `%APPDATA%` 路径找不到文件？**
A: 尝试 `%LOCALAPPDATA%\CodeBuddyExtension\...`（部分应用用 Local 而非 Roaming）；或在资源管理器地址栏输入 `%APPDATA%` 回车，手动确认 `CodeBuddyExtension` 目录是否存在及其确切路径。欢迎把实测路径反馈给我们以订正本文档。
