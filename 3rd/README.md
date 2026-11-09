# `3rd/` — vendored dependencies

Everything the project needs is in this directory: no vcpkg, no conan, no system
development packages. That is what makes "one file, drop it on a device" real.

| Directory | What | Version | License |
| --- | --- | --- | --- |
| `nlohmann_json` | JSON | 3.11.3 | MIT |
| `cpp-httplib` | HTTP server/client | single header | MIT |
| `yaml-cpp` | YAML config | 0.8.0 | MIT |
| `miniz` | zlib replacement (Web UI assets) | 3.0.2 | MIT |
| `ca-bundle` | root certificates | Mozilla CA list | MPL-2.0 |
| `mbedtls` | TLS/DTLS for the agent | 2.28 LTS | Apache-2.0 OR GPL-2.0 |
| `libdatachannel` | WebRTC data channel (the tunnel) | 0.22.0 | MPL-2.0 |
| `mbedtls3` | TLS/DTLS **for the tunnel only** | 3.5.2 | Apache-2.0 OR GPL-2.0 |

## Why there are two mbedTLS versions

The agent itself uses **mbedTLS 2.28 LTS** (`mbedtls/`). The tunnel binaries use
**mbedTLS 3.5** (`mbedtls3/`), because libdatachannel 0.22 needs the 3.x API
(`mbedtls_ssl_key_export_type` and friends do not exist in 2.x).

The two never meet in one binary: the agent links the first, the tunnel
binaries link the second. They also never meet in one CMake project - both
define targets called `mbedtls`, `mbedx509` and `mbedcrypto`, and CMake refuses
to create a target twice. That is why the tunnel is built by its own nested
project (`tunnel/CMakeLists.txt`) instead of being added to the top-level one.

> Unifying them (agent -> mbedTLS 3.6 LTS) is worth doing on its own: 2.28
> reaches end of life in November 2026. The agent's TLS code already compiles
> unchanged against the 3.x headers; what it needs is a full test pass.

## Local changes to the vendored trees

The vendored sources are upstream releases with the build files trimmed
(the test suites, the examples and the generated docs are not needed to build
the product). `libdatachannel` and its dependencies (libjuice, usrsctp, plog)
are kept nested inside it, exactly as upstream expects them.

One behaviour difference to keep in mind: **a statically linked Linux binary
cannot resolve names** - glibc's NSS lives in shared libraries, so `getaddrinfo`
is unavailable in a `-static` build. The tunnel binaries take IP addresses, so
they do not need it, but a static build also loses the *host* ICE candidates
(it can still use srflx and relayed ones). Build with
`-DTEA_TUNNEL_STATIC_LIBC=OFF` if you need name resolution or host candidates.
