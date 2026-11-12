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

## 从电脑上驱动这台手机

手机的 agent 在 `127.0.0.1:8383`。想让桌面上的 a2 把它当 peer：

1. app 的设置里打开「LAN 访问」→ agent 改成听 `0.0.0.0`；
2. 生成一个 API token（peer 通道要它）；
3. 桌面上的 `~/.tea_agent/tea_agent.yaml`：

```yaml
peers:
  phone:
    url: http://<手机的局域网IP>:8383
    token: <app 里那个 token>
```

然后 `toolkit_peer chat peer=phone prompt="看一下今天的通知"` 就让手机自己的
agent 干活了（它用自己的 provider、自己的上下文）。

⚠️ **安全**：Web UI 和聊天接口**没有密码**，`--api-token` 只挡 `/api/exec` 和
peer 通道。所以「LAN 访问」默认是关的——同一个 Wi-Fi 上的人能直接跟手机上的
agent 说话，而 agent 能跑命令。开之前想清楚。

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
