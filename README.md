# JumpServer for Royal TSX

把 JumpServer 上有权限访问的资产和账号，变成 Royal TSX 左侧导航栏里一棵可以点开
的树。点一下就直接 SSH 到目标机器，不用先登录堡垒机、再在 TUI 菜单里一层层翻。

> **English**: Turns the assets and accounts you can reach on JumpServer into a
> browsable tree in Royal TSX's sidebar — one click opens SSH straight to the
> target, skipping the bastion's interactive menu. It is a **Dynamic Folder**
> (script → RoyalJSON), not a native `.plugin`, because Royal TSX's plugin SDK
> is not public. All documentation below is in Chinese; the code and comments
> are in English.

```
JumpServer                    ← 动态文件夹
├── k8s-master01 (10.0.0.10)
│   ├── root
│   └── dev
├── web-01 (10.0.0.11)
│   └── root
└── 生产环境/k8s                ← 按 JumpServer 资产树分组（可选）
    └── node-03 (10.0.0.13)
        └── root
```

## 为什么不是「真·插件」

Royal TSX 的插件是**闭源**的：安装目录下的 `.plugin` 包里只有一个混淆并签名的
`Plugin.rtsxbin`，Royal Apps 没有公开 SDK，也没有公开任何插件模板或签名工具。
自己写一个 `.plugin` 在当前版本上做不到。

但 Royal TSX 自带一个官方扩展点，能力刚好覆盖这个需求：**动态文件夹
（Dynamic Folder）**。它允许你配置一段脚本，脚本往 stdout 打印 RoyalJSON，
Royal TSX 就把这些对象渲染成导航栏里的连接，并且可以随时 Reload 刷新。这就是
本项目的做法——用官方支持的方式，拿到等价的效果。

## 原理：koko 的直连登录格式

真正让「点一下就连上」成立的是 JumpServer 服务端 koko 的一个特性。koko 的 SSH
服务除了交互式菜单，还认两种可以直接指定资产的登录名（见 koko 源码
`pkg/auth/ssh.go`）：

| 格式 | 说明 |
| --- | --- |
| `<平台用户名>@<账号用户名>@<资产IP或UUID>` | 用你的 JumpServer 密码认证，直接进资产 |
| `JMS-<token>` | 用户名是 `JMS-<token>`，密码就是 token 本身 |

分隔符 `@` 和 `#` 都行。资产那一段必须是**IP 或 UUID**，不能用资产名——koko 用
`GetUserPermAssetsByIP` / `GetUserPermAssetsByUserId` 去匹配，并且要求唯一命中。

本项目默认用第一种格式，于是每个连接就是一个普通的 SSH 连接：

- 主机 / 端口：koko 的地址和端口
- 用户名：`alice@root@10.0.0.10`
- 密码：你的 JumpServer 密码

也就是说，**不需要改 JumpServer，也不需要任何中间件**。

## 安装

```bash
./install.sh
```

它会把脚本装到 `~/.config/royaltsx-jumpserver/`，生成配置模板，并在 `dist/`
下生成 `JumpServer.rdfe`（Royal TSX 的一键导入文件）。

配置里的 `base_url`、`auth.username` 和 koko 端口安装时已经填好，通常不用动。

### 凭据怎么填（可视化，推荐）

密码**填在 Royal TSX 里**，不写任何配置文件：

1. 选中导入进来的 `JumpServer` 动态文件夹
2. 右边切到 **Credentials** 页
3. 选 **Specify username and password**，填上你的 JumpServer 用户名和密码

密码会加密保存在 `.rtsz` 文档里，和 Royal TSX 内置连接类型的凭据完全一样。
导入文件已经帮你把 Credential 模式设成 `Specify username and password`、用户名
预填好了，你只需要补密码。

**它是怎么送进脚本的**：Royal TSX 会把脚本正文里的替换 token 先替换成实际值，再把
展开后的脚本写成临时文件执行。所以脚本正文里写的是 `$EffectiveUsername$` 和
`$EffectivePassword$` 两行，运行时它们已经变成了你的用户名和密码。

直接把它们拼进 shell 变量是危险的（密码里有引号、`$`、反引号就会破坏脚本）。这里用
的是引号包住的 heredoc：

```sh
env python3 /path/to/jms_royaltsx.py <<'JMS_CREDS_EOF'
$EffectiveUsername$
$EffectivePassword$
JMS_CREDS_EOF
```

定界符带了引号，bash 会原样把内容交给 Python 的 stdin，不做任何展开和分词，所以密码
里有什么字符都没关系。有测试专门跑 `it's "fine" $HOME \`id\` ; echo pwned` 这种密码
来盯这一点。

### SSH 用什么认证

和上面分开：上面那份凭据是**调 API 列资产**用的；连机器时用什么是另一回事，因为
很多人连堡垒机走的是密钥而不是密码。`connection_auth.mode` 三选一：

| 值 | 说明 |
| --- | --- |
| `password`（默认） | 把 JumpServer 密码写进每个连接，和直连格式配套 |
| `key` | 用 SSH 私钥；再配 `connection_auth.key_file`（支持 `~`），有口令就填 `key_passphrase` |
| `none` | 不在连接里放凭据，由你在 Royal TSX 里另外指定 |

例如走公钥：

```json
"connection_auth": { "mode": "key", "key_file": "~/.ssh/id_rsa" }
```

### 退出后想保留页签翻历史

两处配合：

1. **每个连接上**（导入文件已默认打开，配置里可关）：

   | 属性 | 作用 |
   | --- | --- |
   | `KeepTerminalContentOnDisconnect` | 远程断开后保留终端内容 |
   | `ShowHistoryAfterDisconnect` | 断开后把历史显示出来，能往上翻 |

2. **Royal TSX 全局设置**（这个动态文件夹改不了，要手动开一次）：设置窗口里搜
   **「Keep remotely disconnected tabs open」**（`DontCloseTabOnRemoteDisconnect`），
   勾上。不勾的话，`exit` 或掉线时标签页还是会被关掉，前面那两条就看不到了。

### 树里的名字

每个连接显示的是**账号用户名**（比如 `deploy`），不是 JumpServer 给账号分配的
`alias`——从资产上自动发现的账号，那个 alias 往往就是一串 UUID，没法看。

万一同一个资产上真有两个同名账号，才会把 alias 附在后面区分，形如 `root (特权账号)`。

### 资产行上的信息

资产是真正的 Folder 对象（不是用 `Path` 简写拼出来的），所以它们自己能带属性：

- **Description** 列显示 `平台 · 组织`，例如 `Linux · Default`
- **Notes** 里有一张表：名称、地址、平台、类型、组织、备注、可用账号、资产 ID

> rJSON 的 `Path` 写法没法给文件夹本身设属性，只有把文件夹作为一等对象嵌套才行。
> 这也是为什么脚本生成的是嵌套结构而不是扁平列表。

如果你更想用别的方式，配置里的 `password_source` 决定**兜底**来源（GUI 凭据始终
优先）：

| 值 | 说明 |
| --- | --- |
| `royaltsx`（默认） | 只用 GUI 里填的凭据 |
| `auto` | GUI → 钥匙串 → 配置文件，依次尝试 |
| `keychain` | macOS 钥匙串 |
| `config` | 直接写在 `config.json` 的 `auth.password`（文件权限 600） |
| `env` | 读环境变量 `JMS_PASSWORD` |
| `none` | 不要密码（配合 `private_token` / `access_key` 认证时用） |

钥匙串的写法，如果你要用：

```bash
security add-generic-password -s royaltsx-jumpserver -a '<JumpServer用户名>' -w '<密码>' -U
```

### 认证方式

`auth.mode` 三选一：

- `password`（默认）：用用户名密码登录 API，拿一个临时 Bearer token
- `private_token`：永久 token，请求头 `Authorization: Token <private_token>`。
  需要在 JumpServer 服务器上 `python manage.py shell` 里执行
  `u.create_private_token()`，一般运维才有权限
- `access_key`：在 JumpServer 网页端「个人设置 → 访问密钥」自助创建，请求用
  HMAC-SHA256 签名。这个不需要服务器权限，也不用把密码交出来，是最干净的方案

## 使用

1. 把 `dist/JumpServer.rdfe` 拖进 Royal TSX（或菜单 `File → Import`）。
2. 选中 `JumpServer` 文件夹 → 右边 **Credentials** 页 → 填用户名密码。
3. 右键文件夹 → **Reload**。资产树就出来了，展开机器点账号即可连接。

> 动态文件夹的内容是只读的，每次 Reload 重新生成。想打开文档时自动刷新，在文件夹
> 属性 → Advanced 里勾上 *Reload contents automatically*。

### 可选的终端自检

想在终端里确认 API 和 SSH 端口通不通：

```bash
python3 ~/.config/royaltsx-jumpserver/jms_royaltsx.py --check
```

输出大致是：

```
配置文件   : /Users/you/.config/royaltsx-jumpserver/config.json
JumpServer : https://jumpserver.example.com
koko SSH   : jumpserver.example.com:2222
认证方式   : password
凭据来源   : royaltsx
             注意：Royal TSX 只在跑脚本时把凭据传进来，在终端里跑看不到。

[ok] 认证成功
[ok] 当前用户: alice (...)
[ok] 有权限的资产: 37 个
[ok] 样例资产 k8s-master01 的账号: ['root', 'dev']
[ok] koko SSH jumpserver.example.com:2222 可连接 -- SSH-2.0-JumpServer
```

注意：如果密码只填在 Royal TSX 的 Credentials 页，**在终端里跑 `--check` 是认证不
了的**——那些环境变量只在 Royal TSX 执行脚本时才存在。想在终端验证，把
`password_source` 临时改成 `auto` 并往钥匙串里放一份密码。以 Royal TSX 里 Reload
的结果为准。

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | — | JumpServer 地址，如 `https://jumpserver.example.com` |
| `koko.host` | 取 `base_url` 的域名 | koko 的 SSH 地址，可以和 Web 不是同一台 |
| `koko.port` | `2222` | koko 的 SSH 端口。JumpServer 默认 `2222`，不少部署会改到别的端口，填错时 `--check` 会帮你扫 |
| `auth.mode` | `password` | `password` / `private_token` / `access_key` |
| `password_source` | `royaltsx` | 兜底来源：`royaltsx` / `auto` / `keychain` / `config` / `env` / `none`。GUI 凭据始终优先 |
| `connection_auth.mode` | `password` | 连接用什么认证：`password` / `key` / `none` |
| `connection_auth.key_file` | 空 | `mode` 为 `key` 时的私钥路径，支持 `~` |
| `connection_auth.key_passphrase` | 空 | 私钥口令 |
| `terminal.keep_content_on_disconnect` | `true` | 连接断开后保留终端内容（对应 `KeepTerminalContentOnDisconnect`） |
| `terminal.show_history_after_disconnect` | `true` | 断开后显示历史，便于往上翻（对应 `ShowHistoryAfterDisconnect`） |
| `org_id` | 空 | 多组织部署时填组织 UUID，会作为 `X-JMS-ORG` 头发送 |
| `verify_tls` | `true` | 自签证书的部署可以改成 `false` |
| `options.group_by` | `asset` | `asset` 按资产分组；`node` 按 JumpServer 资产树分组 |
| `options.include_accounts` | `true` | 关掉则每个资产只生成一个用你自己账号的连接 |
| `options.skip_manual_accounts` | `true` | 跳过「手动输入」这类伪账号（`@INPUT`），它没法用直连格式表达 |
| `options.separator` | `auto` | `auto` 会优先用 `@`，账号名里含 `@` 时自动改用 `#` |
| `options.use_uuid_as_target` | `false` | 资产的 `address` 不是 IP 时建议打开，改用资产 UUID |
| `options.embed_password_in_connections` | `true` | 关掉则连接里不带密码，改由 Royal TSX 凭据在连接时提供 |
| `options.workers` | `8` | 拉取每个资产账号列表的并发数 |
| `options.path_prefix` | 空 | 给所有连接加一层目录前缀 |

## 排错

**Reload 报「unsupported script interpreter」**
导入文件里脚本解释器写的是 `bash`。Royal TSX 认的解释器 ID 就是这几个小写名字：
`bash` / `perl` / `php` / `powershell` / `python` / `ruby` / `json` / `javascript`
（对应应用设置里的 `DynFolder<名字>Path` 一组选项）。如果哪天它不认了，打开文件夹
属性 → Script 页，在下拉里换一个即可——脚本本身就是一行 POSIX shell。

注意别和**应用设置**里的 `DynamicFolderShellName` 搞混，那个是「用哪个 shell 去
跑脚本」（system default / bash / zsh），是全局设置，不是解释器 ID。

**Reload 报错但看不到原因**
动态文件夹属性 → Advanced 里打开 *Debug Logging*，然后看
`~/Library/Application Support/Royal TSX/Logs/`。也可以在终端里直接跑
`python3 ~/.config/royaltsx-jumpserver/jms_royaltsx.py`，错误信息会打到 stderr。

**Reload 说「没有取到 JumpServer 密码」，但我在 Credentials 页填了**
报错信息里会给出排查线索（**只列变量名和状态，不会打印密码**）：

- 「stdin（heredoc）里什么都没收到」→ 脚本正文里的 `$EffectiveUsername$` /
  `$EffectivePassword$` 没被替换。多半是导入时脚本没带这两行，或者文件夹的
  Advanced 里替换 token 的模式被改成了 Environment Variables 但 token 列表是空的。
  对照 `dist/JumpServer.rdfe` 里的 Script 内容，确认那两行在。
- 「stdin 里只收到用户名，没有密码」→ token 替换正常，但 Credentials 页的密码是空的。
- 「环境变量里有：…」→ 你用的是 Environment Variables 模式，脚本也支持，照常工作。

**连上以后停在 `Opt>` 菜单**
说明 koko 没识别出直连格式，退回了交互菜单。检查 `--check` 里样例资产的用户名
拼出来是不是 `用户名@账号@IP` 三段。常见原因：账号用户名本身含 `@`（这时应该用
`#` 分隔，`separator` 设 `auto` 会自动处理），或者资产那段填的不是 IP。

**`Must be unique asset for ...`**
资产段匹配到了多个资产。打开 `options.use_uuid_as_target`，改用资产 UUID。

**SSH 端口连不上**
`--check` 会自动探测并给出建议——它扫一批常见端口，把应答了 SSH banner 的列出来。
koko 默认在 **2222**，但很多部署会改掉。`--check` 会扫一批常见端口，把应答了 SSH
banner 的列出来。

**想确认 API 返回了什么**
```bash
python3 ~/.config/royaltsx-jumpserver/jms_royaltsx.py --raw /api/v1/perms/users/self/assets/
```

## 用到的 JumpServer 接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/authentication/auth/` | 用户名密码换临时 token |
| `GET /api/v1/users/profile/` | 当前用户 |
| `GET /api/v1/perms/users/self/assets/` | 我有权限的资产（分页） |
| `GET /api/v1/perms/users/self/assets/<uuid>/` | 资产详情，含 `permed_accounts` 账号列表 |
| `GET /api/v1/perms/users/self/nodes/` | 我有权限的资产树节点（按节点分组时用） |

账号列表藏在资产**详情**里（`AssetPermedDetailSerializer.permed_accounts`），所以
每个资产要额外一次请求——`options.workers` 控制并发。

## 开发

```bash
python3 tests/test_royaljson.py     # 用真实响应结构做断言，不需要连堡垒机
```

`src/jms_royaltsx.py` 是纯标准库，没有第三方依赖，可以直接在终端里跑和调试。

## 已知限制

- 动态文件夹的内容是只读的，不能在 Royal TSX 里改完保存回 JumpServer。
- 「手动输入」（`@INPUT`）账号没法用直连格式表达，默认跳过。
- 用 RDP / 数据库 / K8s 之类的资产不在本项目的范围，只处理 SSH。不过
  `--raw` 拿到的数据里 `permed_protocols` 是有的，扩展起来不难。
- 如果 JumpServer 对 SSH 登录强制 MFA，连接时 koko 会弹键盘交互提示，需要在
  Royal TSX 的终端里手动输一次验证码。
