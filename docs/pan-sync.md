# 百度网盘同步配置

多台机器共用一份 tea_a2 配置。

配置住在**机器本地**（`<tea_home>/provider.yaml`、`tea_agent.yaml`、`mcp.yaml`…）。
换一台机器、装一个系统、把配置「整理」一下 —— 这些事都会逼你重新抄一遍 provider.yaml，
而 provider.yaml 里有 API key、有 peers 的令牌、有你自己调过的模型顺序。这个脚本拿
**百度网盘**当那根线：本地打包 → 加密 → 传到网盘的一个固定目录；另一台机器再拉下来。

工具是 `sched/pan_sync.py`（Python 侧，标准库）。它不在 C++ 二进制里，原因和任务队列、
邮件看门狗一样：**策略不是功能** —— 同步哪些文件、远端放哪、什么时候同步、冲突了怎么办，
都是改一行配置的事，不该重新编译；而且坏了不影响本体（同步挂了，agent 照常能聊）。

---

## 三分钟跑起来

### 1. 建一个百度网盘应用

打开 <https://pan.baidu.com/union/console/createapp>，建一个「软件应用」，拿到
**AppKey** 和 **SecretKey**。个人认证就够（个人认证最多 2 个应用）。

### 2. 写配置

```bash
mkdir -p ~/.tea_agent/pan-sync
cp sched/pan-sync.template ~/.tea_agent/pan-sync/config
$EDITOR ~/.tea_agent/pan-sync/config
```

最少填三样：

```yaml
app_key: 你的 AppKey
app_secret: 你的 SecretKey
passphrase: 你自己想的加密口令
```

`passphrase` 是**两台机器要填一样的**那一个：provider.yaml 里有 API key，明文躺在网盘里
等于把钥匙挂在门上。脚本用 PBKDF2 派生密钥，整包加密 + HMAC 校验后才传。

### 3. 授权

```bash
python3 sched/pan_sync.py auth
```

会给你一个网址和一个用户码（也可以扫二维码）。浏览器打开网址、输用户码、同意授权，
令牌就落在 `~/.tea_agent/pan-sync/token.json`（0600）。令牌 30 天过期，脚本自己会用
refresh_token 续 —— **刷新令牌只能用一次**，所以每次刷新都立刻落盘。

### 4. 同步

```bash
python3 sched/pan_sync.py push      # 这台机器当「源头」：把本地配置推上去
python3 sched/pan_sync.py sync      # 日常用这个：先拉后推
```

另一台机器上：填同样的 `app_key`/`app_secret`/`passphrase`，跑一次 `auth`，然后
`python3 sched/pan_sync.py pull` —— 它的 provider.yaml 就跟第一台一模一样了。

---

## 命令

| 命令 | 干什么 |
| --- | --- |
| `auth` | 授权一次（设备码：给个网址 + 用户码） |
| `status` | 本地和远端现在各是什么、有没有冲突 |
| `push` | 把本地配置推上去 |
| `pull` | 把远端配置拉下来 |
| `sync` | 先拉后推（日常用这个） |
| `watch` | 常驻，每隔 `interval` 秒同步一次 |
| `check` | 配置、令牌、网盘通不通 |

`push` / `pull` / `sync` 都支持 `--dry-run`（只看会做什么）和 `--force`（两边都改过时
也覆盖）。

```bash
python3 sched/pan_sync.py status
# 远端目录：/tea_a2/default
# 本地：
#   provider.yaml         35dbf9ea47f2
#   tea_agent.yaml        967ef7a5ef4b
# 上次同步：2026-10-30T07:42:55Z
# 远端：是 sunkw-legion 在 2026-10-30T07:42:55Z 推的
# 本地动过的文件：provider.yaml
---

## 同步什么，不同步什么

| 同步 | 不同步 |
| --- | --- |
| `provider.yaml`（provider、key、模型顺序、search/asr 段） | **工作区清单**（`workspaces.json`，见下） |
| `tea_agent.yaml`（机器级设置：peers、输出上限、压缩…） | 会话记录（`chat_history.db` 之类） |
| `mcp.yaml`（MCP 服务器清单，没有就跳过） | 记忆、产出文件、工作区里的 `.tea_agent_run/` |
| | 日志、统计、缓存、token 与状态文件（`~/.tea_agent/pan-sync/`） |

理由：**「跟谁说话、怎么干活」是共享的，「发生过什么」「我这儿有什么」是每台机器自己的**。

**`workspaces.json` 特意不同步**：它是**工作区清单**，里面是本机的绝对路径
（`/home/sunkw/work/git/tea_a2`）和每个工作区的会话计数。搬到另一台上，那边的工作区清单
就被覆盖成一堆不存在的路径 —— 那边的工作区还在，只是清单没了。

要改同步范围，改配置里的 `files:`（逗号分隔的文件名，不要写目录）。**拉取时只写名单里的
文件**：远端包里有的、名单里没有的（比如上一版留下的 `workspaces.json`），会被跳过并报出来，
不会写进你的 tea home。范围改了（少了一个文件）也会重推一份，远端包里就不会留着旧文件。

---

## 三条规矩

### 一、两边都改了，就停手

同步工具最忌讳的是**默默覆盖**：它会把一台机器的 provider.yaml 换成另一台机器的，
而没人知道。

判定靠三样东西：本地每个文件的 sha256、远端清单（谁在什么时候推的）、本地状态
（上次同步完两边是什么样，`state.json`）。**两边都比上次动过 = 冲突**，脚本不动任何
一边，把两边都说清楚，让人来定：

```
$ python3 sched/pan_sync.py push
⚠ 冲突：两边都改过了 —— 我没有覆盖任何一边。
· 远端在上次同步之后也动过（laptop），本地也动过（provider.yaml）。
  两边都改了就不知道谁对，先看看 status，确定要覆盖远端再加 --force。
```

`--force` 才覆盖。**顺序永远是先拉后推**：先推会把远端还没收下来的更新盖掉。

### 二、拉下来的东西先备份

每次写本地文件之前，旧版本先存进 `~/.tea_agent/pan-sync/backup/<时间戳>/`。覆盖了还能
找回来。备份不会自动清理（文件很小，一年也就几十个）。

### 三、删除不传播

本地删掉一个文件，远端的包里就没有它了；但**拉取不会删掉别的机器上已有的那份**。
少同步一个文件比删掉一份配置安全得多 —— 同步工具不该有「删除」这个动作。

---

## 第一次同步

新机器（还没有 `state.json`）第一次拉的时候**不会被「冲突」挡住** —— 它还没有「上次同步」
可言，无从判断谁对。远端那份会盖掉本地的同名文件，**本地那份先备份**：

```
$ python3 sched/pan_sync.py pull
第一次同步：远端那份会盖掉本地的 provider.yaml（旧的备份在
/home/me/.tea_agent/pan-sync/backup/20261030-154210）。
写下来：provider.yaml、tea_agent.yaml
```

反过来，新机器第一次**推**的时候，如果远端已经有一份配置了，脚本会停下来问一句
（要覆盖就 `--force`）：远端那份可能是别人辛辛苦苦调好的。

---

## 本地有、远端没有的文件

比如这台机器有 `mcp.yaml`，另一台没有。拉回来的时候它不在包里，但**下一次 `push` 会把它
带上去** —— 「本地有、远端没有」也算「动过」。少了这一条，这个文件就永远传不出去
（拉回来时它不在包里，却被记成「已经同步过了」）。

---

## 底层是什么

百度网盘**开放平台**的 REST 接口：

| 干什么 | 接口 |
| --- | --- |
| 设备码 | `GET openapi.baidu.com/oauth/2.0/device/code?response_type=device_code&scope=basic,netdisk` |
| 换令牌 | `GET openapi.baidu.com/oauth/2.0/token?grant_type=device_token` |
| 刷新令牌 | `GET openapi.baidu.com/oauth/2.0/token?grant_type=refresh_token` |
| 列目录 | `GET pan.baidu.com/rest/2.0/xpan/file?method=list` |
| 建目录 | `POST pan.baidu.com/rest/2.0/xpan/file?method=create`（isdir=1） |
| 预上传 | `POST pan.baidu.com/rest/2.0/xpan/file?method=precreate` |
| 问上传域名 | `GET d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload`（域名是动态的） |
| 传分片 | `POST <上传域名>/rest/2.0/pcs/superfile2?method=upload`（4MB 一片） |
| 合并 | `POST pan.baidu.com/rest/2.0/xpan/file?method=create`（isdir=0） |
| 下载地址 | `GET pan.baidu.com/rest/2.0/xpan/multimedia?method=filemetas&dlink=1` |

官方文档：<https://pan.baidu.com/union/doc/>。官方还发了一个 MCP Server
（<https://github.com/baidu-netdisk/mcp>，18 个工具，SSE 在 `https://mcp-pan.baidu.com/sse`）——
想要更细的网盘能力（分享、转存、搜索），接它比接 REST 划算。

**配额**：普通接口 1000 次/天、上传 100 次/天、下载 1000 次/天（以应用审核后的配额为准）。
同步是「几分钟一次、每次两个文件」，用不掉多少。

---

## 现状与坑

**已经对着真账号跑通了**（2026-10-30）：授权 → 推送 → 另一台机器拉下来 → 两边的
provider.yaml sha256 一样；冲突检测在真网盘上也验过（两边都改 → 停手，两边都不动）。

两个**照着文档写也会踩**的坑（都是真机才发现的）：

1. **上传域名是动态的**。官方文档写「上传域名必须通过 获取上传域名 接口获取，不要在生产
   代码中固定使用示例域名」。把 `superfile2` 打在 `pan.baidu.com` 上会得到
   `HTTP 403 页面不存在`（那是网页前端的 404 页）；打在 `d.pcs.baidu.com` 上才行，
   而且最好先问一次 `locateupload`
   （`GET https://d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload&appid=250528
   &access_token=…&path=…&uploadid=…&upload_version=2.0`，返回 `host` 和
   `servers[].server`）。
2. **分片上传的参数位置**：`path` / `uploadid` / `partseq` / `type` 都是**查询参数**，
   请求体（multipart）里只有 `file`。官方 SDK 的 `location_map` 也是这么写的。

另外：

- **文档说预上传「仅支持访问 /apps/{appname} 目录下的文件」**，但实测在普通目录
  （`/tea_a2/default`）下建目录、上传、下载都正常 —— 不用绕到 `/apps/` 去。
- **令牌 30 天过期**；refresh_token 只能用一次，刷新失败就是「用户解除了授权」，只能重新
  `auth`。
- **风控**：接口返回 31034 就是「需要验证」，多半是同一台机器短时间请求太多。
- 这个工具在 Python 侧，**每台要同步的机器上都要有**（仓库里的 `sched/`，或者从 release
  的 `tasks/` 里拿）。

---

## 相关

- [与其他应用对接](integrations.md) —— 三个方向、watcher 的形状、通知出口
- [配置](config.md) —— provider.yaml / tea_agent.yaml 的字段
- [级联](peer.md) —— 连得上彼此的机器用级联；连不上的用网盘同步
- [多级协同与机群运维](fleet.md) —— 一台服务器管 40 台设备
- [发版：包传到网盘](build.md#发版包传到百度网盘不进-git) —— 发布产物不再提交到 git（`sched/pan_publish.py`，4MB 分片 + 回读校验）
