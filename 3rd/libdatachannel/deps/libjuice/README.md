# libjuice - UDP Interactive Connectivity Establishment

> ## ⚠️ 这里有 tea_a2 自己的改动（升级 libjuice 时要重做）
>
> 这个目录**不是原版的 libjuice**：tea_a2 给它打了一个补丁，
> 好让 broker 知道**谁用了 TURN 中继、用了多少字节**。
> libjuice 原版没有这个接口（它只管转发，不管记账）。
>
> 改了四个文件（每一处都有 `tea_a2:` 的标记，用
> `grep -rn "tea_a2:" 3rd/libdatachannel/deps/libjuice/` 就能全部找到）：
>
> | 文件 | 改了什么 |
> | --- | --- |
> | `include/juice/juice.h` | 新增 `juice_turn_usage_t`（事件、账号、客户端地址、中继地址、字节数）和 `juice_cb_turn_usage_t` 回调；`juice_server_config_t` 多了 `cb_turn_usage` 和 `turn_usage_user_ptr` |
> | `src/server.h` | `server_turn_alloc_t` 多了 `bytes_in` / `bytes_out` / `packets_in` / `packets_out` / `relayed_record` |
> | `src/server.c` | 数字节（对端→客户端、客户端→对端），分配建立/结束时回调，`server_create()` 里把回调拷进 server 的配置 |
> | `src/agent.c` | **关掉 agent 时把 TURN 分配还掉**（发一个 lifetime=0 的 Refresh）：原来不还，中继端口要占满 10 分钟才释放，而中继端口池只有 100 个 |
>
> ⚠️ **Refresh 要写 LIFETIME 属性就得同时把 `lifetime_set` 置上**——
> 只写 `lifetime = 0` 的话属性根本没进包，服务端当成了「续期」（这个坑也踩过一次）。
>
> ⚠️ **`server_create()` 是逐个字段拷配置的** —— 忘了拷新加的回调，
> 回调就永远是 NULL（这个坑踩过一次）。
>
> 升级 libjuice 时：先看 `git log -p -- 3rd/libdatachannel/deps/libjuice`
> 把这些改动重新做一遍（或者先在新版上重新实现），
> 再跑 `tests/smoke_tunnel.py`。
>
> 用到它的地方：`src/tunnel_usage.h`（记录器）和 `src/tunnel_broker.cpp`（回调）。

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-blue.svg)](https://www.mozilla.org/en-US/MPL/2.0/)
[![Build and test](https://github.com/paullouisageneau/libjuice/actions/workflows/build.yml/badge.svg)](https://github.com/paullouisageneau/libjuice/actions/workflows/build.yml)
[![Packaging status](https://repology.org/badge/tiny-repos/libjuice.svg)](https://repology.org/project/libjuice/versions)
[![Latest packaged version](https://repology.org/badge/latest-versions/libjuice.svg)](https://repology.org/project/libjuice/versions)
[![Gitter](https://badges.gitter.im/libjuice/community.svg)](https://gitter.im/libjuice/community?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge&utm_content=badge)
[![Discord](https://img.shields.io/discord/903257095539925006?logo=discord)](https://discord.gg/jXAP8jp3Nn)

libjuice :lemon::sweat_drops: (_JUICE is a UDP Interactive Connectivity Establishment library_) allows to open bidirectionnal User Datagram Protocol (UDP) streams with Network Address Translator (NAT) traversal.

The library is a simplified implementation of the Interactive Connectivity Establishment (ICE) protocol, client-side and server-side, written in C without dependencies for multiple platforms, including GNU/Linux, Android, FreeBSD, Apple macOS, iOS, and Microsoft Windows. The client supports only a single component over UDP per session in a standard single-gateway network topology, as this should be sufficient for the majority of use cases nowadays.

libjuice is licensed under MPL 2.0, see [LICENSE](https://github.com/paullouisageneau/libjuice/blob/master/LICENSE).

libjuice is available on [AUR](https://aur.archlinux.org/packages/libjuice-git) and [vcpkg](https://vcpkg.io/en/getting-started). Bindings are available for [Rust](https://github.com/VollmondT/juice-rs).

For a STUN/TURN server application based on libjuice, see [Violet](https://github.com/paullouisageneau/violet).

## Compatibility

The library implements a simplified full ICE agent ([RFC5245](https://www.rfc-editor.org/rfc/rfc5245.html) then [RFC8445](https://www.rfc-editor.org/rfc/rfc8445.html)) featuring:
- STUN protocol ([RFC5389](https://www.rfc-editor.org/rfc/rfc5389.html) then [RFC8489](https://www.rfc-editor.org/rfc/rfc8489.html))
- TURN relaying ([RFC5766](https://www.rfc-editor.org/rfc/rfc5766.html) then [RFC8656](https://www.rfc-editor.org/rfc/rfc8656.html))
- TCP candidates ([RFC6544](https://www.rfc-editor.org/rfc/rfc6544.html))
- ICE Consent freshness ([RFC7675](https://www.rfc-editor.org/rfc/rfc7675.html))
- ICE Patiently Awaiting Connectivity ([RFC 8863](https://www.rfc-editor.org/rfc/rfc8863.html))
- SDP-based interface ([RFC8839](https://www.rfc-editor.org/rfc/rfc8839.html))
- IPv4 and IPv6 dual-stack support
- Optional multiplexing on a single UDP port

The limitations compared to a fully-featured ICE agent are:
- Only UDP is supported as transport protocol and other protocols are ignored.
- Only one component is supported, which is sufficient for WebRTC Data Channels and multiplexed RTP+RTCP.
- Only [RFC8828](https://www.rfc-editor.org/rfc/rfc8828) mode 2 is supported (default route + all local addresses).

It also implements a lightweight STUN/TURN server ([RFC8489](https://www.rfc-editor.org/rfc/rfc8489.html) and [RFC8656](https://www.rfc-editor.org/rfc/rfc8656.html)). The server can be disabled at compile-time with the `NO_SERVER` flag.

## Dependencies

None!

Optionally, [Nettle](https://www.lysator.liu.se/~nisse/nettle/) can provide SHA1 and SHA256 algorithms instead of the internal implementation.

## Building

### Clone repository

```bash
$ git clone https://github.com/paullouisageneau/libjuice.git
$ cd libjuice
```

### Build with CMake

The CMake library targets `libjuice` and `libjuice-static` respectively correspond to the shared and static libraries. The default target will build the library and tests. It exports the targets with namespace `LibJuice::LibJuice` and `LibJuice::LibJuiceStatic` to link the library from another CMake project.

#### POSIX-compliant operating systems (including Linux and Apple macOS)

```bash
$ cmake -B build
$ cd build
$ make -j2
```

The option `USE_NETTLE` allows to use the Nettle library instead of the internal implementation for HMAC-SHA1:
```bash
$ cmake -B build -DUSE_NETTLE=1
$ cd build
$ make -j2
```

#### Microsoft Windows with MinGW cross-compilation

```bash
$ cmake -B build -DCMAKE_TOOLCHAIN_FILE=/usr/share/mingw/toolchain-x86_64-w64-mingw32.cmake # replace with your toolchain file
$ cd build
$ make -j2
```

#### Microsoft Windows with Microsoft Visual C++

```bash
$ cmake -B build -G "NMake Makefiles"
$ cd build
$ nmake
```

### Build directly with Make (Linux only)

```bash
$ make
```

The option `USE_NETTLE` allows to use the Nettle library instead of the internal implementation for HMAC-SHA1:
```bash
$ make USE_NETTLE=1
```

## Example

See [test/connectivity.c](https://github.com/paullouisageneau/libjuice/blob/master/test/connectivity.c) for a complete local connection example.

See [test/server.c](https://github.com/paullouisageneau/libjuice/blob/master/test/server.c) for a server example.

## Contributing

See [CONTRIBUTING.md](https://github.com/paullouisageneau/libjuice/blob/master/CONTRIBUTING.md) for contribution guidelines.

