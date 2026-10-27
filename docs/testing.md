# 测试与验证状态

## 测试

### 跑得快：并行 + 共用一个浏览器

```bash
bash scripts/test-all.sh              # 全部：32 个 python + 32 个浏览器测试
bash scripts/test-all.sh ui           # 只跑浏览器测试
bash scripts/test-all.sh smoke_todo   # 只跑名字里含 smoke_todo 的
JOBS=4 bash scripts/test-all.sh       # 少几个并行任务
```

两件事让它从「十几分钟」变成「约 100 秒」：

- **一个浏览器给所有浏览器测试**（`tests/chrome_pool.mjs`）。新起的 Chrome 在某些 Linux
  容器（WSL2）里**第一次打开页面要等 25 秒**——渲染进程得从冷缓存里拉起来。29 个测试各起
  一个浏览器，就是 29 次 25 秒。改成共用一个（`TEA_TEST_CDP`），每个测试开自己的标签页，
  那 25 秒只付一次。单独跑某个测试时行为不变（它自己起浏览器）。
- **并行跑**（`xargs -P`）。测试之间互不相干（各自临时目录、随机端口）。

有个测试在机器忙的时候会飘（它用固定 sleep 等一个界面状态），所以并行跑完失败的会**再单独跑
一次**：还是不过才算失败。日志在输出里给的那次运行目录下。

Windows 上是 `pwsh -File scripts\test-all.ps1`（同样的思路，自己起浏览器）。

```bash
pwsh -File tests/e2e.ps1                                  # 默认构建产物
pwsh -File tests/e2e.ps1 -Exe build-msvc/bin/tea_agent.exe
pwsh -File tests/autostart.ps1                            # 登录自启动：launcher/任务 XML + 隐藏启动实测
pwsh -File tests/relink-while-running.ps1 -Exe build-gcc/bin/tea_agent.exe  # 运行中也能构建（改名让位）

python3 tests/smoke_web_tools.py                          # 网页工具（本地 mock，不产生费用）
python3 tests/smoke_provider_usage.py                      # 订阅额度查询：声明/归一化/错误处理
python3 tests/smoke_provider_models.py                     # 查模型 + 回填 provider.yaml：形状/默认值/只改 models 块
node tests/ui_provider_models.mjs                          # 设置页「更新模型」：列表、改数字、写入、热加载（需 google-chrome）
node tests/ui_usage.mjs                                   # 顶栏额度徽标 + 设置页按 provider 查询（需 google-chrome）
python3 tests/smoke_vision.py                             # 粘贴图片：校验/落盘/多模态请求体
python3 tests/smoke_asr.py                                # 语音输入：asr: 段 + 转写端点的契约（本地 mock）
python3 tests/smoke_mcp.py                                # MCP：mcp: 段、stdio 上的 JSON-RPC、工具并进工具表、手工调工具、
                                                          # 连不上的服务器不影响别的（本地 mock MCP 服务器）
node tests/ui_mcp.mjs                                    # 设置页的 MCP 服务器卡片：状态/工具/刷新（需 google-chrome）
node tests/ui_asr.mjs                                     # 麦克风按钮：录音 → 16k 单声道 WAV → 文字填进输入框（需 google-chrome）
node tests/ui_asr_e2e.mjs                                 # 同一套流程对着**真的** SenseVoiceSmall（没装模型就跳过）
python3 tests/smoke_tool_call_structure.py               # 工具调用结构：重复/孤儿/没答复的调用都先修好再发
python3 tests/smoke_tool_images.py                        # toolkit_load_image：磁盘图片进对话/上限/拒绝
node tests/ui_tool_images.mjs                             # 加载的图片在对话里可见（含刷新后，需 google-chrome）
python3 tests/smoke_console_level.py                      # 控制台只回声 WARN/ERROR、日志文件仍是 INFO
python3 tests/smoke_bind_urls.py                          # --host 0.0.0.0 打印活跃地址，且该地址真能打开 UI
node tests/ui_boot_probe.mjs                              # 不启浏览器也能跑：app.js 启动路径不抛异常
python3 tests/smoke_config_quickstart.py                  # 首次运行只填 key 生成 provider.yaml
node tests/ui_setup_flow.mjs                              # 首次运行上传配置（需 google-chrome）
node tests/ui_setup_quickstart.mjs                        # 首次运行快捷通道：粘贴 key 回车即开始（需 google-chrome）
node tests/ui_paste_image.mjs                             # 粘贴图片的浏览器端行为（需 google-chrome）
node tests/ui_metrics_layout.mjs                          # 指标栏始终单行且不截断（需 google-chrome）
python3 tests/smoke_single_file.py                        # 内嵌证书 + 静态链接 + 静态下的 DNS
python3 tests/smoke_shell.py                              # 网页终端：开一个 shell、写进去、读出来、改大小、结束
node tests/ui_shell.mjs                                  # 网页终端：入口、按键翻译、终端画面（需 google-chrome）
python3 tests/smoke_prompt_queue.py                       # 消息插队：采纳 / 取消 / 退回
python3 tests/smoke_concurrency.py                        # 并发写：锁 + 原子替换，一个事件都不能丢
python3 tests/smoke_task_server.py                         # 任务队列（Python 服务）+ 额度闸门 + 进度/结果/串行/幂等/事件/导出
python3 tests/smoke_fleet.py                                 # 机群：台账 + 并发采集 + 批量作业（灰度/回滚/校验）
                                                          # + 审计 + 机群服务 API + 真二进制的 /api/status 与 /api/exec
                                                          # + 多级链路（服务器 a2 → toolkit_fleet → 机群服务 → 设备 a2）
python3 tests/smoke_task_order.py                          # 任务单：解析/报错/命令行排队/模板可下载
python3 tests/smoke_telegram_bot.py                        # Telegram 前端：轮询/命令/文件/白名单/offset
python3 tests/smoke_utf8_truncation.py                    # 文件被截断在字符中间：轮次不崩/结果是合法 UTF-8
python3 tests/smoke_invalid_utf8.py                       # 内容不是 UTF-8 的文件与命令输出：轮次不崩/字节被替换
python3 tests/smoke_utf8_roundtrip.py                     # 往返：模型的坏字节不丢事件、压缩不切坏字符、落盘合法
bash scripts/test-all.sh                                  # 整套测试：并行 + 共用一个浏览器（~100 s）
python3 tests/smoke_agent_doc.py                          # 工作区经验 agent.md：注入/写入/上限
python3 tests/smoke_system_prompt.py                      # 工作区 system prompt：读写/下一轮生效/上限
node tests/ui_workspace_add.mjs                           # 新增工作区：完整路径 + 就地提示 + 创建
node tests/ui_workspace_delete.mjs                       # 删除工作区：确认框 + 只摘登记，文件不动（需 google-chrome）
node tests/ui_workspace_access.mjs                        # 文件访问范围开关：受限 / 全局 / 命令行锁死（需 google-chrome）
node tests/smoke_turn_resume.mjs                          # 断线不取消轮次：回放 / 续播 / stop 仍然有效
node tests/ui_stream_resume.mjs                           # 关掉页面再打开，接回正在跑的那一轮（需 google-chrome）
node tests/ui_second_client.mjs                           # 冷启动不被其它工作区的轮次拽走，点开才接上（需 google-chrome）
node tests/ui_two_clients.mjs                             # 两个浏览器一个主题：?s= 接上 / 排队 / 离开 / 停止（需 google-chrome）
node tests/ui_topic_url.mjs                               # 同浏览器两个标签页各看各的主题，刷新仍在原主题（需 google-chrome）
python3 tests/smoke_multi_session.py                      # 多会话并发 + 默认排队 + interrupt + 跟随者上限
python3 tests/smoke_todo.py                               # 任务列表：分解/进度/唯一的 active/落盘/导入
python3 tests/smoke_artifacts.py                          # 产出文件与 /download：收录/去重/流式/超限
node tests/ui_artifacts.mjs                               # 最终回答下方的下载链接（需 google-chrome）
node tests/ui_todo.mjs                                    # 输入框上方的进度面板与侧栏徽章（需 google-chrome）
python3 tests/smoke_export_html.py                        # 导出会话为单文件 HTML：内联/转义/往返导入
node tests/ui_session_export.mjs                          # 导出按钮 → 下载 → 离线打开 → 导入（需 google-chrome）
python3 tests/smoke_run_archive.py                        # 工作区归档下载：真 zip / 沙箱 / 超限跳过
node tests/ui_files_download.mjs                          # 文件页「下载」按钮真的产出 zip（需 google-chrome）
node tests/ui_stream_guard.mjs                            # 生成期间切换/新建被明确拒绝
node tests/ui_queue.mjs                                   # 生成中 Enter 排队、取消、采纳（需 google-chrome）
node tests/ui_sidebar.mjs                                 # 侧栏隐藏/显示与正文宽度自适应（需 google-chrome）
node tests/ui_reasoning_follow.mjs                        # 「深度思考」展开时贴着最新输出走（需 google-chrome）
node tests/ui_agent_doc.mjs                               # 记忆页的 agent.md 编辑框（需 google-chrome）
node tests/ui_system_prompt.mjs                           # 设置页的系统提示词编辑/预览/恢复默认（需 google-chrome）
python3 tests/smoke_destroy.py                            # 销毁：预览/确认/删除 tea home 与各 .tea_agent_run
node tests/ui_destroy.mjs                                 # 设置页「销毁」确认框与删除后回到首次运行（需 google-chrome）
node tests/ui_render_perf.mjs                             # 流式渲染不重建气泡（需 google-chrome）
node tests/ui_crumbs.mjs                                  # 顶栏显示工作区与会话标题（需 google-chrome）
node tests/ui_turn_index.mjs                              # 对话跳转索引：64 字 tip / 点击定位（需 google-chrome）
node tests/ui_fork.mjs                                    # 分叉：选中编号→输入即开新会话，再点/Esc 取消（需 google-chrome）
node tests/ui_status.mjs                                  # 连接状态灯：抖动容忍 / 自愈 / 可点（需 google-chrome）
```

`tests/smoke_web_tools.py` 用两个假的 provider（**两个端口**，好分辨请求是谁应的）同时模拟
chat 与 `/v1/tools/{search,fetch}`，覆盖：工具按 provider 声明才出现在工具表、未声明时强制
调用被拒且不外呼、端点卡住时按 `web_tool_timeout_ms` 超时而不是拖住整轮、`/api/chat/stop`
能中断传输中的请求；以及独立的 search provider（`provider.yaml` 顶层 `search:` 段）：对话
provider 不能搜时工具照样出现、请求发给 search provider 并用它的 key、`always: true` 时连
「自己能搜」的 provider 也让位、段写错（provider 不存在）时报错且不给工具。

`tests/smoke_tool_call_structure.py` 覆盖「请求里的工具调用结构」：provider 要把每条工具结果
对回它答复的那次调用，而且必须一对一。MiniMax 对不是一对一的历史整个请求都拒
（`HTTP 400`、`invalid params, 400 (2013)`——2026-10-23 在 api.minimax.cn 实测），报错里一个字都
看不出原因；别的 provider 也会拒，只是措辞不同（`tool call id is invalid`、`tool result's tool
id(x) not found`、`tool call and result not match`）。测试用一个按 MiniMax 规则办事的假 provider
盯住三件事：**同一个调用被答复两次**、**没有对应调用的结果**、**没有答复的调用**——发出去之前
都要先修好（`src/message.cpp` 的 `repair_tool_calls`），轮次照常跑完；provider 复用了工具调用 id
时（我们退回 `call_1` 这类名字时也会撞车）要改名，不能让历史里出现两份答复。

`tests/smoke_tool_images.py` 覆盖 `toolkit_load_image`：视觉模型的工具表里有它、纯文本模型没有；
工具结果本身是文本（不是几百 KB 的 base64），图片作为 follow-up user 消息进入**下一次请求**，
data URL 解码后与磁盘文件逐字节相同；副本落在 `artifacts/images/`，会话里存的是工作区相对路径、
`/raw` 能取回，源文件删掉之后下一轮照样发送；不存在的文件、非图片、越出工作区分别给出可操作的
报错且不附加图片；一轮最多附加 6 张，其余在工具结果里说明。

`tests/ui_tool_images.mjs` 在浏览器里看效果：一轮中 `tool_images` 事件让那条消息连同图片出现，
`<img>` 真的解码成功（`naturalWidth` 正确）、`src` 走 `/raw`；工具行展开后写着副本路径与 MIME；
刷新后图片仍在原位且没有重复的气泡。

`tests/smoke_vision.py` 覆盖图片通路：没有 `supports_vision` 的模型被拒且**不会**请求 provider、
坏 base64 / 非图片字节 / 超过 4 张 / 空载荷分别报对应错误码、合法图片落盘且字节一致、
请求体是 `[{type:text},{type:image_url}]` 且 base64 往返无损、会话只存路径、`/raw` 能取回原图、
纯图片消息与纯文本消息都仍然可用。

`tests/ui_paste_image.mjs` 用真实粘贴事件（`ClipboardEvent` + `DataTransfer`）验证缩略图出现、
× 能移除、发送后缩略图条清空、用户气泡经 `/raw` 渲染且图片真的加载成功（`naturalWidth > 0`）。

`tests/ui_metrics_layout.mjs` 在 9 个窗口宽度（1440→600）下把每个指标填成最宽的真实数值，
断言指标栏**只有一行**、且最长的那项没有被截断。这类「不许换行」的布局约束加一个指标或
改一个标签就会破，所以单独钉住。

`tests/smoke_prompt_queue.py` 用三个假 provider 覆盖插队语义：工具执行中入队后，agent 在
下一轮迭代采纳它并出现在后续请求里、落盘为 user 消息；模型已经流式输出完最终答案时入队，
本轮会继续一次迭代而不是结束；按 id 取消 / 全部取消；停止轮次后未采纳的消息通过
`queue_unconsumed` 原样退回且不进入会话；没有进行中的轮次返回 409；带图片 / 空消息返回 400。

`tests/smoke_telegram_bot.py` 覆盖 Telegram 这一端（Bot API 是假的，任务队列也是假的）：
任务，`/tasks` `/status` `/result` `/start` `/cancel` `/answer` `/del` `/quota` `/retry` 都摸到
队列的 API；`/tasks@botname`（群里发的那种）也认；**名单外的 chat 一句都不回**（bot 是公开的，

`tests/smoke_agent_doc.py` 覆盖工作区经验文件：打开工作区即生成带模板的 `agent.md` 与
`scripts/`；每次模型请求的 system prompt 都带着它；`op=append` 落盘带日期并立刻出现在同一轮的
下一次请求里；`op=read/replace/scripts` 各自生效；超过 32 KB 的 append 报错并提示压缩且不
改动文件；`GET/PUT /api/workspaces/{id}/agent-doc` 能读、能改、超限报 400。

`tests/smoke_system_prompt.py` 覆盖工作区级 system prompt：默认回落到内置规则（或
`settings.yaml` 的 `system_prompt`）；`GET/PUT /api/workspaces/{id}/system-prompt` 能读能写，
`current_prompt`/`source` 反映真正生效的人设；保存后**下一次模型请求**就以新内容开头，且替换
而非追加内置人设，环境块与 agent.md 仍在；把默认文本原样保存回去不会产生覆盖；
空白内容恢复默认；超过 64 KB 报 400 且不改文件。

`tests/smoke_destroy.py` 覆盖「销毁」：`GET /api/destroy` 列出的正是 tea home 与各工作区的
`.tea_agent_run`（目录已不存在的也照实标 `exists:false`）；不带 `confirm` 的 POST 返回 400 且
一个文件都不动；有轮次在跑时返回 409；确认后 tea home（含 provider.yaml 与**正被打开的**全局
日志）和各 `.tea_agent_run` 都消失，而项目目录与其中的文件还在；随后 `/api/health` 报
`config_loaded=false`、`/api/config/status` 要求重新配置、`/api/workspaces` 为空；再删一次是
幂等的；删除后重新添加同一工作区也正常。

`tests/ui_destroy.mjs` 在真实浏览器里点一遍：设置页有红色「销毁…」按钮，确认框写着
「是否真的彻底删除痕迹？」并逐条列出 tea home 与两个工作区的 `.tea_agent_run`；取消后什么都没
删；确认后目录消失、页面自动刷新并回到上传 provider.yaml 的首次运行界面，localStorage 里
指向已删工作区/会话的 id 也被清掉。

`tests/ui_system_prompt.mjs` 在真实浏览器里钉住「设置 → 系统提示词」：打开面板编辑框里就是
当前生效的人设（没有覆盖时是默认提示词，不是空框）且不算「未保存」、编辑后出现「未保存」、
还原回到当前人设、保存落盘且 `/prompt` 反映新内容、预览弹窗展示完整提示词、切换工作区各自
显示自己的提示词、恢复默认后保存会删掉覆盖。

`tests/ui_status.mjs` 覆盖右上角状态灯：用 CDP 只屏蔽 `/api/health`（其它接口照常工作，正是
报告里的情形）。断言单次失败**不会**翻成「未连接」（一次抖动不是掉线）、连续两次才翻且横幅
说明原因、恢复后**不用刷新**自动变回「已连接」、一次普通请求成功即可提前自愈（不等 10s 轮询）、
状态灯可点击并立即重查。旧实现在这套断言下会一直停在同一个状态（它只在启动时查一次）。

`tests/ui_crumbs.mjs` 钉住顶栏面包屑：启动后必须显示「工作区 / 会话标题」而不是占位符
「无工作区 / 新会话」（旧实现只在切换视图时刷新，启动时工作区还没加载完就再也不会更新）；
首轮回答后标题换成服务端从第一条消息生成的标题、刷新后保持、切换工作区、新建会话、
切回原工作区、重命名，六个时机都要跟着变。

`tests/smoke_console_level.py` 钉住两个阈值的分工：默认启动 + 一次 `/api/providers` 请求后，
终端上的日志行**没有一条** `INFO`（只剩 WARN/ERROR），而日志文件里照样有 `[INFO]`，横幅仍然
只打印一次；`--console-level info` 让终端重新出现 `INFO`；`--no-console` 与 `--quiet` 都不回声
日志（`--quiet` 还省掉横幅），但文件照写；`--console-level verbose` 报错并按 `warn` 处理；
`--log-level error` 只收窄文件、不影响控制台；`--help` 同时列出两个开关。

`tests/smoke_bind_urls.py` 钉住启动信息里的 `Web UI` 行：`--host 0.0.0.0` 时第一条必须是
本机**走默认路由的活跃地址**（与测试自己用 UDP `connect()` 选出的源地址一致），而不是
`127.0.0.1`；该地址在服务运行期间真的能取到 `/` 且返回内置 UI；回环地址仍然列出并标注
`(this machine only)`；setup 模式的 `hint: open …` 用同一个地址；`--host 127.0.0.1` 只打印一条
且没有多余标注；绑定某个具体地址时原样打印。

`tests/ui_turn_index.mjs` 预置一个 30 轮的会话再打开页面：右侧索引必须有 30 个编号；第 10 条
用户输入故意超过 64 个字符，断言 `title` 与悬停 tip 都只保留前 64 个字符加省略号，且 tip
位于编号左侧、不遮住编号；提示卡片宽度必须是 260–320px、预览最多折 4 行——用 `left` 定位 +
`max-width` 时浏览器拿「left 到视口右缘」当可用宽度，索引条贴右就会把提示压成一列单字，
所以 950px 窄窗下再验一次；点第 10 个编号后对应的用户气泡滚到视口顶部且高亮第 10 项；滚到
顶部/底部分别高亮第 1/30 项；新建会话时索引隐藏，重新打开会话后恢复。

`tests/ui_fork.mjs` 预置一个 4 轮会话并开一个只会回「分支回答」的假 provider：点第 3 个编号
后编号必须有分叉描边、`aria-pressed=true`、输入框上方出现「第 3 条」提示条且占位符变成分叉
模式；再点同一个编号、点提示条的 ×、按 Esc 都能撤销（提示条隐藏、占位符复原）；重新选第 2 条并发送一句话
后，必须**多出一个会话**（`forked_from` / `fork_turn=2`，标题以 `分叉 2 · ` 开头），它的用户
消息恰好是第 1、2 轮加上这句新输入，父会话仍是原来的 4 轮且不含新输入；页面本身也切到分支
（3 个用户气泡、顶栏跟着换标题）。分叉结束后再发一句普通消息，仍然留在分支里，不会冒出第三个
会话。

`tests/ui_render_perf.mjs` 起一轮 320 条 delta、中间夹一次工具调用的回答，用
`MutationObserver` 数 live 气泡被替换的次数：**每来一条 delta 就重建整块**是这次的回归点
（旧实现 320 条 delta → 324 次重建），现在必须是个位数。同时给已完成的段落和工具行打标记，
断言它们在后续流式过程中没有被重建；结束后的最终渲染仍然完整（正文、工具行、光标消失）。

`tests/ui_agent_doc.mjs` 在浏览器里操作「记忆」页的 agent.md 卡片：内容来自服务端、路径显示为
`.tea_agent_run/agent.md`、脚本列在下面；改动后提示「未保存」、还原按钮恢复服务端副本、保存后
磁盘与 API 都拿到新内容；切到另一个工作区时加载的是它自己的那份（不串内容、不串脚本）。

`tests/ui_workspace_add.mjs` 覆盖「添加工作区」这唯一一条入口：`/api/health` 不再有
`dir_picker`、`/api/dialog/directory` 返回 404、点菜单直接出表单且**没有「浏览…」按钮**；空路径和
相对路径在表单里就地报错（且不产生目录、不发请求）；完整路径能添加并切换（标签页标题跟着变）；
目录不存在时给出创建提示并在确认后真的建目录；路径是个文件时报服务端原因且对话框不关；服务端
自己也拒绝相对路径（`path must be a full path`）。
`tests/ui_workspace_delete.mjs` 覆盖「删除工作区」：下拉里每一行都有删除按钮；确认框写明是
哪个路径、并说清**文件不会被删**；取消不动任何东西；确认后服务端索引与下拉列表都少了这一条，
而项目目录和其中的 `.tea_agent_run` 还在（重新添加同一路径即可接着用）；删掉正在打开的那个
工作区时页面落到剩下的第一个，没有了就回空态；删不存在的 id 返回 404。


`tests/smoke_turn_resume.mjs` 在 HTTP 层钉住「轮次比连接活得久」：客户端读到第 8 帧就断开，
服务端那一轮和它对 provider 的请求都继续（事件序号还在涨）；`GET /api/chat/turn` 报
`running:true` 与完整回放窗口；`POST /api/chat/attach` 从 `seq 1` 连续回放到 `done`，最终答案
一字不少地落盘；`since=` 只回放尾巴；轮次结束后 attach 返回 404；`/api/chat/stop` 对没人看的
轮次照样生效（并真的掐断上游请求）。

`tests/ui_stream_resume.mjs` 在真浏览器里走同一条路：发消息 → 导航到 about:blank（等价于关掉
标签页）→ 确认服务端还在跑 → 导航回来，断言页面重新进入生成态、live 气泡追上了期间生成的内容、
**没有重复渲染**，然后跟着直播到结束。

`tests/ui_second_client.mjs` 换一台「机器」：服务端注册两个工作区，一个纯 HTTP 客户端在
**第二个**工作区里发起轮次、读到第 9 帧就断开，然后用一个全新的浏览器 profile（没有
localStorage）打开页面——它必须落在 `proj_b`（而不是索引里的第一个 `proj_a`）、直接进入生成态、
回放已经生成的内容、继续直播到结束，标签页标题也跟着工作区走。

`tests/smoke_todo.py` 覆盖任务列表：`set` 写入整份计划、`list`/`add`/`update`/`clear` 各自生效；
列表出现在**下一轮**的 system prompt 里（压缩或重启后仍然在）；每次变更都发一个 `todo` 事件并
带 `items`/`total`/`done`；状态落盘在 `session.json`，索引里的 `todo_total`/`todo_done` 不用打开
会话就能读；把另一条置为 `active` 会把原来那条降回 `pending`（永远只有一条进行中）；全部完成时
工具结果明说「All steps are done」；两条 active / 越界下标 / 未知 op 都被拒绝且不改动现有列表；
`PUT /api/sessions/{id}/todos` 能整体替换也能勾选一条，非法状态 400、未知下标 404；进程重启后
列表还在，并且仍然注入提示词；导出的 HTML 与其中的 JSON 都带列表，导入后进度一并恢复。

`tests/smoke_artifacts.py` 覆盖产出文件：写的文档、命令生成的 SVG 都进 `artifacts`（带种类、
大小、turn、产出工具），`.o` 之类的构建噪声与 `.tea_agent_run/` 下的内部副本都不收录；
同一路径改写两次只出现一次；进程重启后仍在；`/download` 返回 `attachment` 与逐字节相同的内容，
`&inline=1` 变成 `inline`，17 MB 的文件 `/raw` 拒绝（413）而 `/download` 正常发完；越过工作区
的路径被拒、缺文件 404、缺参数 400；导出的 HTML 里有静态清单且不含死链接。

`tests/ui_artifacts.mjs` 在真浏览器里看「本轮产出」：它贴在**最后一条助手消息**下面、只列
本轮的若干个文件（上一轮的 `old.csv` 不出现）、每行有种类与大小、下载链接指向 `/download` 并
带 `download` 属性、文档没有「查看」而图片/视频有；点链接真的拿到文件（状态 200、
`Content-Disposition: attachment`、内容与磁盘一致）；一轮运行中产出的图片在结束后自动出现且
可下载；没有产出的那一轮不显示这块。

`tests/ui_todo.mjs` 在真浏览器里看这块面板：有任务列表的会话显示面板、`1/4`、进度条 25%、三态
标记与 `aria-checked`；侧栏会话行同步显示「1/4」；点第三条即勾选完成且**写进 session.json**，
刷新后仍是 `2/4` 且样式为已完成；点标题行折叠/展开；一轮运行中 agent 改列表时面板**实时**更新
（`todo` 事件）；没有列表的会话整个面板隐藏；新建草稿也会清掉。

`tests/smoke_export_html.py` 把导出当成「要离开这台机器」的文件来验：HTML 完整、CSS 内联、
没有任何 `<link>` 或外部 `src`；Markdown 各元素都渲染出来；模型回答里的 `<script>` 被转义、
`javascript:` 链接不会变成活链接；图片内联后的字节与磁盘逐字节相同；工具输出按 8 KB 截断
并注明；末尾的 JSON 能解析且条数一致；带着 `</script>` 的消息不会截断那个区块；把这份 HTML
POST 到**另一个工作区**能新建会话（文本一致、图片写进新工作区并可按 `/raw` 取回），再导一次
是第二个会话而不是覆盖，纯 JSON 也能导，乱发的 HTML / 空 payload 返回 400 且不产生会话。

`tests/ui_session_export.mjs` 在真实浏览器里走完整链路：顶栏导出按钮在打开会话时出现（草稿时
隐藏）、点击后拿到 `text/html` blob 且文件名是 `.html`、内容里有会话文本/内联样式/`data:` 图片、
不含本机地址；随后把文件写到磁盘并用 **`file://`** 打开——这正是「拷到别的机器上双击」的场景，
断言样式生效、消息气泡与图片都在（`naturalWidth > 0`）、没有任何网络请求；最后验证导入按钮
弹出 `.html` 文件选择器，导入后侧栏多出一个会话。

`tests/smoke_run_archive.py` 把归档当**真 zip** 验：`zipfile.testzip()` 通过、条目名全部以
`.tea_agent_run/` 开头（那个点必须保住）、空目录也在、`note.txt` 解出来与磁盘逐字节相同且确实被
压缩过、`X-Archive-Entries` 与包内条目数一致；`?path=` 指子目录只打那一棵；`../..` 返回 403、
不存在返回 404、指到文件返回 400 `not_a_directory`、未知工作区 404；放一个 33 MB 的文件进去，
它被跳过（`X-Archive-Skipped: 1`）而其余内容照常打包。

`tests/ui_files_download.mjs` 在真浏览器里点那个按钮：按钮存在、文案与 tooltip 说清下载什么、
点击后立刻进入「打包中…」并禁用；用 `URL.createObjectURL` 的替身接住 blob，断言它是
`application/zip`、magic 是 `PK\x03\x04`、大小合理；结束后按钮恢复、toast 报出文件名与条目数；
最后再直接打一次接口，确认包里真的有这个工作区的标记文件。

`tests/ui_two_clients.mjs` 开两个独立 profile 的浏览器看同一个主题：A 发起一轮后拿到 `?s=` 链接，
B **用这条链接**打开并接上（内容长度始终接近，B 的地址栏停在同一个主题）；B 插队一条消息，
**两边**都看到它被采纳成第二个问题；B 点「离开」后自己不再跟随、侧栏标出「· 进行中」、
可以自由开新会话，而服务端和 A 都不受影响；B 点会话又能接回同一个 `?s=`；最后 A 点「停止」，
两边同时结束。

`tests/ui_second_client.mjs` 反过来钉"不该发生的事"：proj_b 里有一轮在跑，另一台机器的全新
profile 打开服务器地址时**仍然落在 proj_a**（不再被"谁在跑就跳谁"拽走），标题也是 proj_a；
切到 proj_b 后侧栏把那个会话标成「· 进行中」，点它才 attach 并回放；重新打开 `?s=` 直接回到
这个主题；清空 localStorage + sessionStorage 再来一次，仍然落在 proj_a。

`tests/ui_topic_url.mjs` 用同一个浏览器 profile 开两个标签页（`?s=A` / `?s=B`）：两边同时流式、
各自只看到自己的问题与回答，两个会话文件互不串味；刷新 A 仍回到 A 并保有完整对话；「复制链接」
复制的是 `…?s=A`；「新会话」只清掉本标签页的 `?s=`，另一个标签页不受影响；再开第三个标签页
也不会动前两个。

`tests/ui_stream_guard.mjs` 起一轮慢速流式回答，在生成过程中点侧边栏：工作区按钮仍可点开菜单，
**切换工作区是允许的**——这一页自动「离开」那一轮（服务端照跑），「添加工作区」正常弹对话框，
「新建会话」只清掉本页的主题（草稿），点回那个会话又能接上；最后「停止」把它结束。

`tests/ui_queue.mjs` 在流式回答期间用真实的 Enter 按键入队：排队条按顺序出现、输入框清空、
没有产生第二个会话；点 × 取消第一条；随后被采纳的一条从排队条消失并变成对话里的 user 气泡，
本轮在同一 SSE 内继续（恰好两次模型请求），结束后排队条隐藏、输入框恢复「发送」。

`tests/ui_sidebar.mjs` 覆盖侧栏收起：点隐藏按钮后侧栏宽度归零、顶栏出现展开按钮、正文栏
变宽（780 → 1040）；刷新后仍是收起状态；再点展开（或 `Ctrl+B`）后侧栏与列宽复原，选择被
写进 localStorage。同一个脚本还断言标签页标题带上了当前工作区。

`tests/ui_boot_probe.mjs` 不需要浏览器：它把真实的 `web/index.html` 解析成一个最小 DOM，然后
在 Node 里执行真实的 `web/app.js`、按浏览器的方式触发 `DOMContentLoaded`，喂给它与服务端同形的
JSON。任何同步异常或未处理的 Promise rejection 都算失败，状态灯没走到「已连接」也算失败——
`e2e.ps1` 只检查 HTML/JS 里**有没有**某些字符串，`init()` 里抛错时页面会一直停在
`index.html` 里写死的「连接中…」，那种静默故障只有真执行一遍才能发现。它支持
`--base <url>`（跑服务端实际发出来的页面）、`--state noworkspace|setup|…`、`--all` 等变体。

`tests/smoke_config_quickstart.py` 覆盖首次运行的快捷通道：没有配置时 `/api/health` 报
`config_loaded=false`；只传 `api_key` 就写出 `provider.yaml`（DeepSeek 单个 provider，
`models:` 里 `deepseek-flash` 排第一，于是它就是默认 —— 文件里不写 `default_model:` 也不用
`roles:`），进程无需重启即可用；key 不出现在
任何响应里；POSIX 下文件权限是 `0600`；带引号/反斜杠的 key 以单引号标量落盘且仍能解析；缺 key、
空白 key、控制字符、未知 provider 一律 400 且不写文件；再调一次是替换而不是一次性。
`provider: minimax` 走同一条路：写出 minimax 单 provider 的配置，默认模型 `MiniMax-M3.1-Flash`、
1M 上下文；显式传的 `api_url` 仍然优先于默认端点。**生成的配置不查额度**（`supports_usage`
关着，只有注释里写着一旦要开怎么开，控制台链接仍在）：批量部署时每台设备都去问一次额度，
就是一个账号上成千上万个请求，最容易把账号弄进风控；上传的 `provider.yaml` 不受此限
（测试里也钉住了这一条：上传一份带 `supports_usage: true` 的，额度查询照旧开着）。
`provider: mimo`（小米 MiMo）也走同一条路，钉住了三件事：**网关由 key 的形状决定**
（`tp-…`/`ttp-…` → `token-plan-cn.xiaomimimo.com`，`sk-…` → `api.xiaomimimo.com`，两者不能混用）、
默认模型是 `mimo-v2.6-flash`（另有 `mimo-v2.6-pro`）、生成的配置里带
`reasoning_content: keep`（v2.6 默认思考，小米的文档要求思考模式下把上一轮的
`reasoning_content` 带回去）。

`tests/ui_setup_quickstart.mjs` 在浏览器里走这条快捷通道：首次运行对话框里有 provider 下拉
（DeepSeek / MiniMax / 小米 MiMo）、`password` 类型的 key 输入框和「用 key 创建配置」按钮，
光标已在输入框里，完整上传路径也还在；切到 MiniMax 时提示文字和 key 占位符一起变（`sk-cp-…`），
切到 MiMo 时变成 `sk-… / tp-…`；
空 key 就地报错且不写文件；粘贴 key 按回车后 `provider.yaml` 出现、页面自动刷新、配置对话框
消失、状态栏变「已连接」、模型按钮显示 `deepseek-flash`；再打开一次对话框选 MiMo 粘一个
`tp-…` key，文件里就换成 Token Plan 网关 + `mimo-v2.6-flash`，服务端也跟着切过去。

`tests/ui_setup_flow.mjs` 起一个**没有** `provider.yaml` 的实例，再用 headless Chrome 通过
CDP 操作真实页面：断言弹出配置对话框、Esc/点遮罩关不掉、坏 YAML 报错且不落盘、文件选择器
能填入内容、上传成功后页面自动重载且状态栏变回「已连接」。

`tests/e2e.ps1` 起一个假 OpenAI 服务（`tools/mock_llm_server.mjs`，按 4 步脚本驱动工具调用），
再以独立 `--tea-home` 启动 agent，覆盖 **155 项断言**：健康检查、key 脱敏、工具注册、工作区索引、
SSE 事件序列与 `metrics` 指标块、两个工具的落盘副作用、`.tea_agent_run/` 全部子目录、会话持久化、
会话分叉（复制到选中轮次、血缘落盘、父会话不变、非法下标/未知会话报错）、目录树与文件读写、
记忆与日志接口、内置 UI 交付（含指标栏）且无 CDN 依赖。全程本地 `http://`，不需要 api_key。

其中几条是**回归测试**：

1. **思考回传**：mock 像真实 thinking 网关那样，一旦发现被重放的 assistant 消息丢了
   `reasoning_content` 就回 400。用 0.1.0 的二进制跑会挂 7 项。
2. **上下文压缩**：fixture 里有一个 `max_context_tokens: 1500` 的 `mock_small` provider，
   `e2e.ps1` 用它 + 复用同一会话 + 工作区级 `settings.yaml` 强制触发压缩，断言 SSE 出现
   `compaction` 事件、会话落盘了 `summary` / `summary_upto`、且原始消息没有被删除
   （压缩是「不再发送」而不是「删除」）。
3. **流正确收尾**：`tests/sse_probe.mjs` 用 Node 的 fetch 当严格客户端读 SSE。curl 容忍
   未终止的 chunked 响应，浏览器/Node 不容忍（`error: Error in input stream` /
   `TypeError: terminated`），所以必须显式断言。用 0.1.1 的二进制跑，44 项里恰好挂这 1 项。
4. **指标块**：mock 只在 `stream_options.include_usage` 打开时才回传 usage（真实网关就是这个
   行为）。默认那一轮因此必须回落成本地估算并置 `estimated: true`；打开 include_usage 后
   `context_tokens` 必须等于 provider 报的 prompt tokens；再换用忽略该选项的 `mock_nousage`
   provider，必须又回到估算，而不是 0 或空。

脚本开跑前会检查端口占用，发现残留监听就直接报错退出——否则会静默连到上一次遗留的实例上，
表现为「卡住」而不是失败。

`tests/ui_reasoning_follow.mjs` 钉住「深度思考」面板的滚动：面板自己是可滚的盒子
（`.reason-panel`，max-height 240px），而推理文字是一段段长进去的——面板不动的话，用户看到的
永远是**最早**的那段推理，正在生成的新内容全在框外。测试用 mock provider 慢慢吐 reasoning，
断言：面板开着时贴着底部（不是顶部）、点开就跳到最新输出、用户自己往上滚过之后新的 delta 不
把他拽回底部、这一轮结束后面板里的推理是完整的。

`tests/smoke_invalid_utf8.py` 钉住「不是 UTF-8 的字节不许杀死整轮」：agent 读到的文件、命令打印的
原始字节、模型自己吐出来的文本，最后都要进 JSON 文档，而 nlohmann 的解析器接受字符串里的非法字节、
序列化器却拒绝它们（`invalid UTF-8 byte at index N`）——那正是「agent crashed」的来源。测试用一个
中间带坏字节的文件（`broken \xe4 byte`）和一个打印原始字节的命令各跑一轮：轮次不崩、每个事件帧
都是合法 JSON、坏字节变成 U+FFFD 而可读文字原样保留、发给 provider 的请求体是合法 UTF-8、落盘的
会话文件也是合法 UTF-8。

`tests/smoke_utf8_roundtrip.py` 钉住「往返」：nlohmann 的**解析器也拒绝**非法 UTF-8
（`parse_error.101: ill-formed UTF-8 byte`），所以 provider 吐出一个坏字节时，整个 SSE 事件会被
丢掉——模型的回答少了一块而没人知道。现在解析前先清洗字节（坏字节变 U+FFFD，事件保住）。测试用一个
会吐坏字节的 mock provider，再加一个「历史长到要压缩」的场景：压缩的 transcript 会按 4000 字节
逐条切、按 60000 字节整体切，两处都必须切在字符边界上——否则摘要请求里会混进半个字符。断言：事件帧
都是合法 JSON、provider 收到的请求都是合法 UTF-8、落盘的会话文件是合法 UTF-8、请求里没有 U+FFFD
（说明没有字符被切坏）。

`tests/smoke_utf8_truncation.py` 钉住同一个问题的另一半：`toolkit_file` 按字节截断时如果切在多字节
字符中间，结果就不是合法 UTF-8。读现在停在字符边界上，尾读会丢掉开头的半个字符。

---


## 当前的验证状态

诚实标注，避免误导：

| 项 | 状态 |
| --- | --- |
| Windows x64 + MinGW-w64 GCC 16.2 | ✅ 构建 0 warning，E2E 155/155 |
| Windows x64 + MSVC | ✅ 构建 0 error，E2E 155/155 |
| C++14 语言模式（GCC 6.5 兼容性） | ✅ 全量编译通过 |
| 真实 `api.deepseek.com` / `opencode.ai` | ✅ 含多轮工具调用 |
| 真实 `www.minimax.cn` 额度接口 | ✅ M Plan 订阅 Key 实查：5 小时窗口与周窗口的剩余百分比、重置时间都对得上 |
| 交叉编译宿主工具机制（`TEA_AGENT_USE_HOST_ASSET_TOOL=ON`） | ✅ 在本机以原生方式验证该路径 |
| **Linux 构建与运行** | ✅ 在 wsl2, ubuntu20.04 等环境测试 |
| **macOS 构建与运行** | ✅ 在 Mac OS X 10.6 Snow Leopard 测试通过 |
| **真实 ARM 交叉编译产物** | ✅ aarch64-linux-gnu-gcc (Linaro GCC 6.5-2018.12) 6.5.0 编译，到 horizon x3 执行正常 GLIBC 2.28 |
| Linux→Windows MinGW-w64 交叉构建（`win64-mingw-cross` preset） | ✅ 产出 PE32+ `tea_agent.exe` 且内嵌 CA；运行需在 Windows 上验证 |
| **ARM64 设备实跑**（x3sdbx3, kernel 4.14.74, glibc 2.28） | ✅ 全静态 11.3 产物：启动不崩，`/`、`/app.css`、`/app.js`、`/api/health`、`/api/workspaces` 全 200，`/favicon.ico` 204，驻留 15s+ |

Linux / macOS 首次构建若报错，最可能集中在 `src/command.cpp`（`fork`/`setsid`/进程组）与
`src/platform_fs.cpp`（POSIX `stat`/`dirent`），欢迎提 issue 附报错。

---


---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
