# sqlite3 (the amalgamation)

SQLite 3.54.1，**官方的 amalgamation**（`sqlite3.c` + `sqlite3.h` + `sqlite3ext.h` 三个文件）。

- 下载：<https://www.sqlite.org/2026/sqlite-amalgamation-3540100.zip>
- SHA3-256（官网页面上写的）：`92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30`

## 为什么是它

一个 `.c` 一个 `.h`，**编进去就是一个静态库**，不装系统包、不带 .so，
Windows / Linux / aarch64 都能编——跟这个仓里其它 vendor 的东西（mbedtls、
libdatachannel）一个路子。

## 谁在用

`a2_broker`：从 sqlite 库里读 TURN 的账号/口令（`~/.tea_agent/tunnel/turn.db`）。
用法见 `docs/tunnel_broker_manual.md`。

## 编译时的开关

`tunnel/CMakeLists.txt` 里编成静态库时关了这些：

| 开关 | 为什么 |
| --- | --- |
| `SQLITE_OMIT_LOAD_EXTENSION` | 不需要动态装扩展（那是一块攻击面） |
| `SQLITE_OMIT_DEPRECATED` | 去掉老 API |
| `SQLITE_DQS=0` | 双引号字符串不当标识符用（默认是 3，但 0 更严） |
| `SQLITE_THREADSAFE=1` | 多线程用（broker 的信令和 TURN 是两个线程） |
| `SQLITE_DEFAULT_MEMSTATUS=0` | 少一块统计代码，小一点 |

`SQLITE_OMIT_LOAD_EXTENSION` 之类的宏写在 `tunnel/CMakeLists.txt` 里，
**不要改这个目录里的文件**（下次升级 sqlite 时会被覆盖）。
