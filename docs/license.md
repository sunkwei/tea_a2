# 许可证

本项目代码采用 **GPL-3.0-or-later** 许可证，见根目录 [`LICENSE`](../LICENSE)。

Copyright (c) 2026 sunkwei

> **2026-11-14 之前本项目是 MIT。** MIT 允许作者改用别的条款分发，但要求「版权声明与
> 许可条款随每一份副本一起给出」，所以那份声明保留在 `LICENSE` 末尾。
>
> 改成 GPL 的原因只有一个：远程桌面的 H.264 编码器用的是 **x264**（GPL-2.0-or-later），
> 把 GPL 代码链接进来，整个程序就只能是 GPL。

## 第三方依赖

`3rd/` 下的依赖各自遵循其原有许可证，详见各目录内的 `LICENSE` 文件：

| 依赖 | 许可证 | 与 GPL-3.0 兼容？ |
| --- | --- | --- |
| x264 | GPL-2.0-or-later | 是（按 GPL-2.0-or-later 使用，整包即 GPL-3.0-or-later） |
| nlohmann/json | MIT | 是 |
| cpp-httplib | MIT | 是 |
| yaml-cpp | MIT | 是 |
| mbedTLS | Apache-2.0（双许可，另有 GPL-2.0） | 是（按 Apache-2.0 使用） |
| libdatachannel | MPL-2.0 | 是（MPL-2.0 与 GPLv3 兼容；MPL 那几个文件本身仍是 MPL） |
| miniz | MIT / zlib | 是 |
| sqlite3 | 公有领域 | 是 |
| CA 证书包（`3rd/ca-bundle/cacert.pem`） | MPL-2.0 | 是（数据，附带即可） |

分发二进制时注意：

- **GPL 要求提供完整源码**。x264 已整份收在 `3rd/x264/` 里 ✓，随源码分发即满足；
  若只发二进制，需按 GPL-3.0 第 6 条附上「完整对应源码」的获取方式（写清仓库地址
  与版本号即可 ✓）。
- mbedTLS 的 Apache-2.0 条款要求保留其许可证与声明，`3rd/mbedtls/LICENSE` 需随发行物
  一起带上 ✓。
- libdatachannel 的 MPL-2.0 要求被修改过的 MPL 文件仍以 MPL 发布 ✓（我们没改 ✓）。

## 许可证兼容性（新增依赖时先看这一段）

本项目是 GPL-3.0-or-later，所以 **GPL-2.0-or-later / GPL-3.0 的库可以直接链接进来**
（x264 就是 ✓）。其余常见许可证也都没问题：

- MIT / BSD / zlib / Apache-2.0 / ISC：**可以** ✓。
- LGPL：可以，但要满足「用户能替换这个库」（动态链接最省事 ✓；静态链接则要提供
  目标文件或链接说明 ✗，能免则免 ✗）。
- MPL-2.0：可以 ✓（文件级 copyleft，被改过的文件仍按 MPL 发布 ✓）。
- **AGPL**：要当心 —— 它的网络条款比 GPL 还强（通过网络提供服务也要开源 ✓）；
  本项目本身就有网络服务，所以 AGPL 依赖会让整个项目变成 AGPL ⚠️。

一句话：GPL 之下唯一还要躲的是 **AGPL**。
