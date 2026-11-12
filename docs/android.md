# a2 在手机上（Android APK）

手机上的 a2 就是仓库里这个 C++ agent——不是一个「手机版」，是同一份代码：编成
`liba2.so`，由 APK 加载，跑在 app 的进程里。

```
Android app (Kotlin)
├── AgentService        前台服务：agent 一直在，手机息屏也不死
├── liba2.so            agent 本体：HTTP 服务 + 工具 + 内嵌 Web UI
├── PhoneBridge         手机桥：通知 / 位置 / 剪贴板 / 截图 / Shizuku shell
└── WebView             顶栏 + agent 自己的网页 UI
```

## 为什么不是「把二进制丢进手机」

Android 10 以后（W^X），app 不能执行自己数据目录里的可执行文件。所以手机上的
a2 **不是一个独立进程**，而是链接进 app 进程的库：`A2Native.start(argv)` 走
JNI 进到 `tea_agent_main()`（`src/main.cpp`）。这也意味着：

- 没有 Termux、没有 root、不用装任何东西；
- agent 的家目录就是 app 的私有目录（`files/.tea_agent`），装完卸载就干净；
- 一个 app 进程里既有 agent 又有桥，agent 调桥就是一次本机 HTTP。

## 编译

需要三样东西（都在本机装好了的话直接跳过）：

| 东西 | 版本 | 装法 |
| --- | --- | --- |
| Android SDK | platform 34 + build-tools 34 | `sdkmanager "platforms;android-34" "build-tools;34.0.0"` |
| Android NDK | r27（27.3.13750724） | `sdkmanager "ndk;27.3.13750724"`（**别用 sdkmanager 装**：它的解压会挂在 symlink 上，手动 `unzip` 就好） |
| JDK | 17 | 任意 |

```bash
export ANDROID_HOME=$HOME/work/android-sdk
export TEA_ANDROID_NDK=$ANDROID_HOME/ndk/27.3.13750724

# 整个 APK（arm64-v8a 给手机，x86_64 给模拟器）
bash scripts/build_android.sh

# 只给手机编（快一半）
bash scripts/build_android.sh --arm64

# 正式包（用 debug key 签，装机即用；上架要换自己的 key）
bash scripts/build_android.sh --release
```

产物：`android/app/build/outputs/apk/debug/app-debug.apk`（约 17 MB，两个 ABI）。

脚本干三件事：CMake 编 `liba2.so`（`cmake/android-ndk.cmake`）→ strip → 放进
`android/app/src/main/jniLibs/<abi>/` → Gradle 打包。

## 装到手机上

```bash
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

没有 adb 就把 APK 拷到手机上点开（要允许「安装未知来源的应用」）。

## 第一次打开

1. **给模型**：app 第一屏是设置页。选 provider（deepseek / minimax / mimo）粘
   key，点「Save and start」——agent 自己的 `/api/config/quickstart` 会把配置
   写好。也可以整份贴 `provider.yaml`。
2. **给权限**（都在 app 的设置页里点，或者系统弹窗）：
   - 位置：agent 问「我在哪」要用；
   - 通知读取：系统设置 → 应用 → 通知访问（系统不让 app 自己要）；
   - 截屏：顶栏的「Capture」按钮（MediaProjection 每次都要用户点一次）。
3. **Shizuku**（可选，但很值）：顶栏的「Shizuku」按钮 → Shizuku app 里允许本
   app 一下。

## 手机上 agent 能干什么

工具叫 `toolkit_phone`（`src/phone_tool.cpp` + `src/phone_tool.h`）：

| action | 干什么 | 需要什么 |
| --- | --- | --- |
| `device` | 型号 / 电量 / 存储 / 网络 / 运行时长 | 无 |
| `notifications` | 刚进来的通知（标题 + 正文 + 包名） | 通知访问 |
| `location` | 当前位置（先要一个新鲜定位，8 秒内） | 位置权限 |
| `clipboard_get` / `clipboard_set` | 读 / 写剪贴板 | 无 |
| `open` | 打开 URL / 应用 / 搜索 | 无 |
| `apps` | 装了哪些能打开的 app | 无 |
| `screenshot` | 屏幕截图（PNG） | Shizuku 优先，否则 MediaProjection |
| `screen` | **读屏**：当前界面上的文字与可点的控件 | 无障碍服务（或 Shizuku 的 uiautomator） |
| `click` | **按文字点击**（“点那个叫『确认』的按钮”） | 无障碍服务（或 Shizuku） |
| `global` | 全局动作：返回 / 主页 / 最近任务 / 通知栏 | 无障碍服务（或 Shizuku 的 keyevent） |
| `sms` | 发短信 | 短信权限 + 设置里打开 |
| `call` | 拨号 | 无 |
| `shell` | **以 shell 身份跑一条命令** | Shizuku |
| `tap` / `swipe` / `type` / `paste` | 点屏幕 / 拖 / 打字 / 粘贴 | Shizuku |

**Shizuku 是这套东西里最关键的一块**：它给 app 一个 `shell` 用户（uid 2000）的
身份，于是 `screencap`、`input tap`、`input text`、`content query`（短信、通讯录、
通话记录这些内容提供器）、`pm list packages` 都能用——不用 root，手机上的
「封闭」就打开了几个门。

`type` 只能打 ASCII（`input text` 的老毛病）；中文走 `paste`：先把文字放进
剪贴板，再 `input keyevent 279`（粘贴）。

## 无障碍服务：轻手的一条路

Shizuku 给的是 **shell 身份**（截图、短信、命令），但不是每个手机都有；**无障碍服务**
是标准 Android 权限，用户在系统设置里点一下就有。app 的顶栏有「无障碍」按钮，
点它直接跳到系统的无障碍设置页。

它多出来的能力：

- **读屏**：`toolkit_phone action=screen` → 当前界面的节点树（文字、哪些可点、坐标），
  不是坐标而是**意思**（“按钮『确认』在 (x,y)”）；
- **按文字点击**：`action=click {"text":"确认"}` —— 不用算坐标，中文按钮也能点；
- **输入**：`action=type` 走无障碍时能打中文（`input text` 只能 ASCII）；
- **全局动作**：`action=global {"action":"back|home|recents|notifications"}`。

两者的分工：**无障碍优先、Shizuku 兜底**（桥里的每个动作都是先试无障碍，不行再走 shell）。

⚠️ **force-stop 会关掉无障碍服务**：Android 在 app 被强制停止时会把它的无障碍服务
也停掉（`settings get secure enabled_accessibility_services` 会变成 null）。所以别
`am force-stop` 这个 app；实在要重启，开一次「无障碍」按钮再打开。

⚠️ **银行/支付类 app 会检测无障碍**（认为你在自动化），可能拒绝登录。

## 从电脑上访问 / 驱动这台手机

agent **默认就听 `0.0.0.0:8383`**（手机桥那个 8384 仍然只听 127.0.0.1——那是内部
接口，不该给外面）。app 的按钮栏下面有一行写着**手机当前的局域网地址**，旁边一个
「复制」按钮，复制出来的是现成的配置片段：

```
# 桌面上的 ~/.tea_agent/tea_agent.yaml 里加这段：
peers:
  phone:
    url: http://172.16.1.220:8383
    token: ca47e773bf9adcc5a4d00ded11ce62ee007c
```

- **网页**：电脑上直接开 `http://<手机的局域网IP>:8383/` 就是手机上的 agent（同一个
  程序、同一份上下文）。
- **当 peer 用**（桌面的 a2 驱动手机）：把上面那段加到桌面
  `~/.tea_agent/tea_agent.yaml`，然后
  `toolkit_peer chat peer=phone prompt="看一下今天的通知"` —— 手机用自己的
  provider、自己的上下文干活。

实测（小米 2311DRK48C，Wi-Fi 172.16.1.220）：
`/api/health`、`/`（网页 UI）、`/api/peer/health`、`/api/peer/tools` 从电脑都通 ✓。

### 换了网络会怎样

**绑定不受影响**：agent 听的是 `0.0.0.0`（所有网卡），socket 一直在 → 换 Wi-Fi、
关 Wi-Fi 走蜂窝、连别的 AP，**服务都不中断，轮子照跑**（真机实测：关掉 Wi-Fi 后
`127.0.0.1:8383` 照常应答）。

**变的是地址**：手机换了网，IP 就换了。app 的地址行**每 10 秒跟着刷新**
（看门狗顺手做），变了会弹一句「地址变了，重新复制一次给电脑」；没有 Wi-Fi 地址时
写「没有 Wi-Fi 地址（电脑现在连不上）」。桥的 `/phone/device` 每次请求现算 ✓。

⚠️ **电脑那边会断**：桌面 `tea_agent.yaml` 里的 `url: http://172.16.1.220:8383` 是写死的
→ 手机换 IP 后 peer 连不上。三条路：
1. 换 IP 后在手机上再点一次「复制」，改一下桌面那份（最省事）；
2. 桌面用**主机名**而不是 IP（路由器给的 DHCP 名字/mDNS，PC 能解析就行）；
3. 走**隧道**（broker 的地址是固定的，手机的 IP 变了也不影响）。

### 地址是怎么挑的

手机上常常有好几个地址：Wi-Fi（wlan0）、VPN（tun0）、蜂窝（ccmi*）。**电脑能连的
只有 Wi-Fi 那个**，所以 app 优先挑 wlan0（接口名 + ConnectivityManager 的 Wi-Fi 网络
两个来源），**没有 Wi-Fi 地址时宁可不报**（VPN/蜂窝的地址对电脑是没用的）。

⚠️ **安全**：Web UI 和聊天接口**没有密码**——`--api-token` 只挡 `/api/exec` 和
peer 通道。所以同一个 Wi-Fi 上的人能直接跟手机上的 agent 说话，**而 agent 能跑命令**
（`toolkit_exec` 不受令牌保护）。token 是自动生成并一直保留的（存在 app 的
SharedPreferences 里），改不了除非重装。

## 验证过的（模拟器上真跑过）

| 事情 | 结果 |
| --- | --- |
| agent 在 APK 里起来 | `A2Service: starting the agent: a2 --port 8383 --tea-home …` ✅ |
| `/api/health` | `{"version":"1.2.12","tea_home_writable":true,…}` ✅ |
| 手机桥 `/ping`、`/phone/device` | 真的电量 / 存储 / 网络 ✅ |
| `toolkit_phone` 出现在工具表里 | ✅ |
| agent 自己调 `toolkit_phone` | 「型号 + Android 版本 + 电量」一句话答出来 ✅ |
| 截图（MediaProjection） | 82 KB 的真 PNG，连截三张 1～3 秒 ✅ |
| 手机上的 Web UI | 侧栏收成抽屉、聊天区占满 ✅ |

## 真机上遇到的两个坑（小米 HyperOS / Android 16）

### 1. Android 16：一启动就退（`SecurityException`）

Android 16 上，带 `mediaProjection` 类型的前台服务还需要系统权限
`CAPTURE_VIDEO_OUTPUT`——普通 app 拿不到。而 `startForeground(id, notification)`
这个两参数的版本会把 **manifest 里声明的所有类型**都算进去，于是 app 一启动就崩：

```
java.lang.SecurityException: Starting FGS with type mediaProjection
  requires permissions: ... [android.permission.CAPTURE_VIDEO_OUTPUT, android.permission.project_media]
```

**做法**：前台服务只用 `specialUse` 类型启动（三参数的 `startForeground` 明确写类型），
截屏时再单独加 `mediaProjection` 类型；加不上就退到 Shizuku 的 `screencap`。

### 2. 息屏之后 agent 变聋（HyperOS 冻结 uid）

屏幕一关，HyperOS 会**冻结整个 uid**（不是普通的 Doze，是厂商自己的那层）：

```
PowerManagerServiceImpl: Partial wakeLock:['a2:agent' ...], disabled: true, reason: greeze
PowerManagerServiceImpl: ... reason: UidFrozen
```

被冻结的 uid 里所有线程都不跑——端口还在监听，但没人应答；app 的 wakelock 也被系统
禁用。唤醒屏幕立刻恢复。

**做法**（一次设置，app 里的「后台」按钮会带你过去）：
设置 → 应用设置 → 应用管理 → A2 →
- **省电策略 → 无限制**（这一条最关键：冻结列表就是按它来的）
- 允许通知（前台服务的通知要看得见）
- 允许后台活动 / 锁住最近任务（可选）

app 这边也做了能做的：常驻一把 partial wake lock（`a2:agent`），让系统在允许的
情况下别给 CPU 降频；顶栏的「后台」按钮会依次尝试厂商的省电策略页
（`miui.intent.action.POWER_HIDE_MODE_APP_LIST`）、系统的电量优化豁免页、通知设置页。

> 注：`dumpsys deviceidle whitelist +<包名>`（Doze 白名单）**不够**——HyperOS 冻结
> 是厂商层的行为，跟 Doze 白名单是两套东西。

## 真机验收（小米 2311DRK48C，Android 16 / HyperOS）

息屏状态下跑通的（屏幕关掉、手机放桌上）：

| 事情 | 结果 |
| --- | --- |
| `/api/health`、`/api/tools`、`/api/config/reload` | 都应答 ✓ |
| 一次裸模型调用（`/api/llm/chat`） | 2.3 s 返回 ✓ |
| 整轮对话（`POST /api/chat`） | 3.5 s 跑完 ✓ |
| **agent 自己调 `toolkit_phone`** | 「小米 2311DRK48C / Android 16 / 电量 97%」✓ |
| Shizuku shell（`id`） | `uid=2000(shell)`，带 input/log/adb/sdcard 组 ✓ |
| Shizuku 截图（`screencap -p`） | 真 PNG ✓（不用 MediaProjection 授权） |
| Shizuku 模拟点击（`input tap`） | ✓ |
| Shizuku 读短信收件箱（`content query`） | 读到真短信 ✓ |

### 无障碍的坑

- **`rootInActiveWindow` 会是 null**：锁屏/息屏时没有活动窗口 → 桥会自动退回 Shizuku
  的 `uiautomator dump`（桥的回答里有 `source` 字段说明是哪儿来的，还有 `a11y` 状态）。
- **force-stop 会把服务停掉**（见上）。

### Shizuku 的两个坑

1. **`Shizuku.newProcess` 是 private**（v13 的 API），得反射调用；它返回的
   `ShizukuRemoteProcess` 就是一个 `java.lang.Process` ✓。
2. **`Process.waitFor(timeout)` 在 Shizuku 上不能用**：远端进程没结束时它抛异常而不是
   说「还在跑」，基类实现当场就放弃了。要用 Shizuku 自己的
   `waitForTimeout(timeout, unit)`。

### 「界面每秒刷一次」= WebView 的重载循环（自己的 bug）

`WebViewClient.onReceivedError` **对任何子资源失败都会回调**（agent 的事件流断一下、
一个字体 404……），而我当时直接整页重载 → 变成 1～2 Hz 的重载循环：屏幕在闪，
CPU 跑到 **29%**。

修法（`MainActivity.kt`）：
- **只看主框架**（`request.isForMainFrame`）→ 子资源失败不重载；
- 页面已经加载成功过 → 只记日志，不重载；
- agent 没了（重启/崩了）由看门狗发现，**等它回来再载一次**（不是循环）；
- 等待重试有上限，超了就放弃（免得对着死掉的 agent 一直刷）。

实测：12 秒 7 次重载 → **1 次**；CPU 29% → **0.8%**。

### 还有一条要注意的

一个卡死的轮子会把会话占住，之后的对话都接在它上面（看起来像「请求没反应」）。
`GET /api/turns` 能看到卡住的那一轮。

## 已知的边界

- **没有 root 就没有别的 app 的私有目录**。Shizuku 的 shell 身份也读不到
  `/data/data/<别的app>`（SELinux 挡着）。能拿的是系统级的东西：内容提供器、
  界面、截图、命令。
- **手机上的 ASR 没打包**。语音输入要另说（模型 240MB，塞进 APK 不合适）。
  先用桌面上的 `sched/asr_server.py`。
- **只有一个进程**。agent 和 app 生死绑定：系统杀 app，agent 就没了。前台服务
  + 通知是为此存在的（用户可以随时划掉）。
- **隧道（a2_tunnel）没编进 APK**：手机上直接走局域网更简单。
  想要跨网，走 broker + 侧载（见 `docs/tunnel.md`）。
- **Android 14 的截屏规矩**：MediaProjection 必须先有 `mediaProjection` 类型的前台服务
  （app 已经这么做了），而且一个 projection 只能建一次虚拟显示。屏幕不变时不会产生新帧，
  所以截屏走「监听器 + 缓存最后一帧」，不去阻塞式地等一帧（等下去会把手机桥挂住）。

## 代码在哪

| 文件 | 干什么 |
| --- | --- |
| `cmake/android-ndk.cmake` | NDK toolchain |
| `src/android_jni.cpp` | JNI 入口（start / stop / setEnv） |
| `src/phone_tool.cpp` | `toolkit_phone` 工具（agent 这边） |
| `android/app/src/main/java/com/tea/a2/AgentService.kt` | 前台服务，跑 agent |
| `android/app/src/main/java/com/tea/a2/PhoneBridge.kt` | 手机桥（HTTP API） |
| `android/app/src/main/java/com/tea/a2/ShizukuHelper.kt` | Shizuku（shell 身份） |
| `android/app/src/main/java/com/tea/a2/NotificationCollector.kt` | 通知监听 |
| `android/app/src/main/java/com/tea/a2/ScreenCapture.kt` | 截屏（MediaProjection） |
| `scripts/build_android.sh` | 一条命令出 APK |
