# a2_ext_py/examples —— 两个现成的工具模块

模块是**一个文件**放在模块目录里（默认 `~/.tea_agent/ext/`，可用 `TEA_EXT_DIR` 或
`mcp.yaml` 里的 `--modules` 改）。这两个是 Windows 上做 GUI 测试常用的：

| 文件 | 工具 | 干什么 |
| --- | --- | --- |
| `toolkit_screenshot.py` | `toolkit_screenshot` | 抓屏，写成 PNG（默认落在工作区的 `.tea_agent_run/artifacts/`），再用 `toolkit_load_image` 读回来 |
| `toolkit_mouse.py` | `toolkit_mouse` | 鼠标：移动 / 单击 / 双击 / 右键 / 拖拽 / 滚轮 / 当前位置 |
| `toolkit_keyboard.py` | `toolkit_keyboard` | 键盘：打字（中文也行）、按一个键（enter/tab/esc/f5…）、按组合键（ctrl+v） |

**装上**（复制到模块目录，再刷新 MCP 服务器）：

```bat
copy sched\a2_ext_py\examples\toolkit_*.py  %USERPROFILE%\.tea_agent\ext\
curl -s -X POST http://127.0.0.1:8282/api/mcp/refresh
```

两个工具都**只用标准库**（Win32 走 ctypes，PNG 容器自己写，zlib 是标准库），
不需要 pip 装任何东西。

## 抓屏

```python
toolkit_screenshot()                       # 整屏 → .tea_agent_run/artifacts/screenshot-<时间>.png
toolkit_screenshot(region="0,0,800,600")   # 一块区域
toolkit_screenshot(max_width=1024)         # 缩小（省 token）
```

工具只能返回文本，所以它返回**路径**；模型再用 `toolkit_load_image(path=…)`
把那�� PNG 挂进对话里——这样模型才真的"看见"了。

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

事件走 `SendInput`，所以在窗口看来就是**真的鼠标**（盯着真实输入的程序能认出来）。

## 键盘

```python
toolkit_keyboard(action="type", text="测试 tea_a2 自动发消息")   # 中文/emoji 都行
toolkit_keyboard(action="type", text="慢一点", delay_ms=40)      # 输入法跟不上时
toolkit_keyboard(action="key", key="enter")
toolkit_keyboard(action="key", key="backspace", times=3)
toolkit_keyboard(action="hotkey", keys=["ctrl", "v"])
```

文字走 `KEYEVENTF_UNICODE`（一个字符一对事件），不走虚拟键码——这样中文、重音、
emoji，以及「在这个键盘布局下要按 Shift、在那个布局下不用」的字符，都原样到达。
键名和组合键才是真正的**键盘事件**（窗口的快捷键只认这个）。

## 三个坑（写模块/配 MCP 时会撞上）

1. **模块可以自己 import**——加载器（`sched/a2_ext_py/toolkit/loader.py`）用同一个
   字典当 globals 和 locals，所以模块级的 `import os` 在函数和类体里都看得见。
2. **`mcp.yaml` 里的 Windows 路径要用单引号**。YAML 的双引号标量会处理反斜杠转义，
   `command: "C:\Users\..."` 里的 `\U` 不是合法转义 → yaml-cpp 拒绝整个文件 →
   MCP 配置被**静默丢掉**（表现是「文件不存在、服务器一个都没起来」）。单引号不处理转义。
3. **打字之前先确认焦点在目标窗口上**。`SendInput` 的按键进的是**当前焦点窗口**，
   不是你以为的那个——焦点在浏览器里，那条消息就打到浏览器里去了。用 `focus_window.py`
   （AttachThreadInput + SetForegroundWindow）把它请到前台，**再读一次**
   `GetForegroundWindow()` 核对，然后才按键盘。抓屏只能给你坐标，给不了焦点。
