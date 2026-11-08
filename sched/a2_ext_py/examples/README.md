# a2_ext_py/examples —— 桌面三件套（截图 / 鼠标 / 键盘）

模块是**一个文件**放在模块目录里（默认 `~/.tea_agent/ext/`，可用 `TEA_EXT_DIR` 或
`mcp.yaml` 里的 `--modules` 改）。这三个是 GUI 测试常用的，**Windows 和 Linux 都能用**：

| 文件 | 工具 | 干什么 |
| --- | --- | --- |
| `toolkit_screenshot.py` | `toolkit_screenshot` | 抓屏，写成 PNG，**并把图回传给模型**（模型直接看见） |
| `toolkit_mouse.py` | `toolkit_mouse` | 鼠标：移动 / 单击 / 双击 / 右键 / 拖拽 / 滚轮 / 当前位置 |
| `toolkit_keyboard.py` | `toolkit_keyboard` | 键盘：打字（中文也行）、按一个键（enter/tab/esc/f5…）、按组合键（ctrl+v）、剪贴板 |

**只用标准库**（Win32 走 ctypes，Linux 走 ctypes + /dev/uinput + libX11，PNG 容器
自己写，zlib 是标准库），不需要 pip 装任何东西。

## 装上

**Linux**：

```sh
cp sched/a2_ext_py/examples/toolkit_*.py ~/.tea_agent/ext/
```

然后在 `~/.tea_agent/mcp.yaml` 里注册扩展层（Windows 一样的写法）：

```yaml
servers:
  a2ext:
    command: "python3"
    args: ["/path/to/tea_a2/sched/a2_ext_py/toolkit/mcp_server.py",
           "--modules", "/home/you/.tea_agent/ext",
           "--agent-url", "http://127.0.0.1:8282"]
```

```sh
curl -s -X POST http://127.0.0.1:8282/api/mcp/refresh
```

**Windows**：

```bat
copy sched\a2_ext_py\examples\toolkit_*.py  %USERPROFILE%\.tea_agent\ext\
curl -s -X POST http://127.0.0.1:8282/api/mcp/refresh
```

## Linux 的三个前提

1. **`/dev/uinput` 要能写**（虚拟输入设备，键鼠走它）：

   ```sh
   ls -l /dev/uinput        # 没有权限就 sudo usermod -aG input $USER（重登录生效）
   ```

2. **桌面要有一个截图程序**：`spectacle`（KDE）、`grim`、`gnome-screenshot`、
   `maim`——有一个就行（截图问合成器要，XWayland 自己看不到屏幕）。

3. **剪贴板**（中文这类不在键盘上的字符靠它）：KDE 的 Klipper（DBus，
   `gdbus` 在就行）、`wl-copy`、`xclip` 或 `xsel`，有一个就行。

## 抓屏

```python
toolkit_screenshot()                       # 整屏 → .tea_agent_run/artifacts/screenshot-<时间>.png
toolkit_screenshot(region="0,0,800,600")   # 一块区域
toolkit_screenshot(max_width=1024)         # 缩小（省 token）
```

工具回传的是**文本 + 图片**：图片进对话，模型直接看见（不用再去
`toolkit_load_image`）。文件也留着。

## 鼠标

```python
toolkit_mouse(action="position")                  # 鼠标在哪
toolkit_mouse(action="move", x=640, y=360)
toolkit_mouse(action="click", x=640, y=360)       # 左键
toolkit_mouse(action="double_click", x=640, y=360)
toolkit_mouse(action="right_click", x=640, y=360)
toolkit_mouse(action="drag", x=100, y=100, to_x=400, to_y=400, steps=20)
toolkit_mouse(action="scroll", dy=-3)             # 往下滚三格
```

坐标就是**截图的像素**（屏幕的物理像素）。截图缩小过的时候，把图片的宽度告诉它
（`image_width=1024`），它会把坐标换算回屏幕。

## 键盘

```python
toolkit_keyboard(action="type", text="测试 tea_a2 自动发消息")   # 中文/emoji 都行
toolkit_keyboard(action="type", text="慢一点", delay_ms=40)      # 输入法跟不上时
toolkit_keyboard(action="key", key="enter")
toolkit_keyboard(action="key", key="backspace", times=3)
toolkit_keyboard(action="hotkey", keys=["ctrl", "v"])
toolkit_keyboard(action="clipboard", text="放剪贴板")
toolkit_keyboard(action="clipboard_read")
```

**中文怎么进去的**：中文不在键盘上，所以走剪贴板——文本放进剪贴板，再按
`shift+insert` 粘贴（`paste_key="ctrl+v"` 可以换）。没有剪贴板程序时会明说，
不会假装打上了。

**输入法会翻译**：fcitx5 开着的时候，打 "hello" 可能出来的是中文。默认
（`ime="auto"`）在打字的那一瞬间把它关掉、打完再打开；`ime="keep"` 是不管它。

## 两个平台的后端（为什么不一样）

| | Windows | Linux |
| --- | --- | --- |
| 键鼠 | `SendInput` | **uinput**（内核虚拟设备） |
| 截图 | GDI | 合成器的截图程序，没有就用 X11 |

**Linux 上为什么是 uinput**：Wayland 桌面上 X11 的 XTest **到不了原生 Wayland
程序**（只走 XWayland 一层）；uinput 是内核里的虚拟设备，合成器把它当真的键盘
鼠标，**每个程序都收得到**。

**打字之前先确认焦点**：uinput 和 SendInput 的按键进的都是**当前焦点窗口**。
先把目标窗口点到前台（`toolkit_mouse`），再打字——否则那条消息会打进你当时正在
看的浏览器或终端里。

## 三个坑（写模块/配 MCP 时会撞上）

1. **模块可以自己 import**——加载器（`sched/a2_ext_py/toolkit/loader.py`）用同一个
   字典当 globals 和 locals，所以模块级的 `import os` 在函数和类体里都看得见。
2. **`mcp.yaml` 里的 Windows 路径要用单引号**。YAML 的双引号标量会处理反斜杠转义，
   `command: "C:\Users\..."` 里的 `\U` 不是合法转义 → yaml-cpp 拒绝整个文件 →
   MCP 配置被**静默丢掉**（表现是「文件不存在、服务器一个都没起来」）。单引号不处理转义。
3. **Linux 的模块要能找到扩展层**：`toolkit_*.py` 用的是
   `a2_ext_py/desktop_linux.py`（uinput、X11、剪贴板那一套）。它跟着 a2 一起装，
   扩展服务器跑的时候 `a2_ext_py` 在 `sys.path` 上，所以不用另外拷——但如果把模块
   拷到别处单独跑，就得把 `sched/` 加进 `PYTHONPATH`。

## 测

```sh
python3 tests/smoke_ext_desktop.py           # 23 项：加载、schema、截图、区域、缩放、键码
python3 tests/smoke_ext_desktop.py --live    # 再加 3 项：真的动鼠标、真的打字
```

`--live` 会往**当前焦点窗口**里打字，所以只在知道自己在往哪打字的时候跑。
