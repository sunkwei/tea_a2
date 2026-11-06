# 许可证

本项目代码采用 **MIT** 许可证，见根目录 [`LICENSE`](../LICENSE)。

Copyright (c) 2026 sunkwei

## 第三方依赖

`3rd/` 下的依赖各自遵循其原有许可证，**不被 MIT 覆盖**，详见各目录内的 `LICENSE` 文件：

| 依赖 | 许可证 |
| --- | --- |
| nlohmann/json | MIT |
| cpp-httplib | MIT |
| yaml-cpp | MIT |
| mbedTLS | Apache-2.0（双许可，另有 GPL-2.0） |
| miniz | MIT |

分发二进制时注意：mbedTLS 的 Apache-2.0 条款要求保留其许可证与声明，`3rd/mbedtls/LICENSE`
需随发行物一起带上。

## 许可证兼容性（新增依赖时先看这一段）

本项目是 MIT，`3rd/` 里 nlohmann/json、cpp-httplib、yaml-cpp、miniz 也都是 MIT，互相兼容、
可直接静态链接进同一个二进制，只需保留各自的版权声明。两处需要留意，但都**不构成冲突**：

- **mbedTLS** 是 Apache-2.0 / GPL-2.0 双许可。这里按 **Apache-2.0** 使用：Apache-2.0 与 MIT
  单向兼容（MIT 代码可以并入 Apache-2.0 分发），只要保留 `3rd/mbedtls/LICENSE` 与声明即可；
  **不要**选 GPL-2.0 那一支——GPL-2.0 与 MIT 组合会把整个发行物变成 GPL，那才是真冲突。
- **CA 证书包**（`3rd/ca-bundle/cacert.pem`）是 MPL-2.0。它是**数据**而不是链接进程序的代码，
  随发行物附带并保留出处即可，不影响二进制的 MIT 许可。

也就是说：新增依赖时优先选 MIT / BSD / zlib / Apache-2.0；**GPL / AGPL 一律不要**（会传染整个
二进制），LGPL 需要能保证重新链接（静态链接下要提供目标文件），也不建议。
