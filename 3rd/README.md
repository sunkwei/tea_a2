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
| `mbedtls` | TLS/DTLS, for the agent **and** the tunnel | 3.5.2 | Apache-2.0 OR GPL-2.0 |
| `libdatachannel` | WebRTC data channel (the tunnel) | 0.22.0 | MPL-2.0 |

## mbedTLS: one tree for everything

The agent and the tunnel binaries both use **mbedTLS 3.5.2** (`mbedtls/`).

It used to be two versions: the agent was on 2.28 LTS while the tunnel needed
3.x (libdatachannel 0.22 uses `mbedtls_ssl_key_export_type` and friends, which
do not exist in 2.x). 2.28 reaches end of life in November 2026, and the
agent's TLS code turned out to compile against 3.x **unchanged** - the whole
migration was swapping the tree.

> **Why the tunnel is still a separate CMake project**: libdatachannel brings
> its own build system and defines targets called `mbedtls`, `mbedx509` and
> `mbedcrypto` - the same names mbedTLS uses, and CMake refuses to create a
> target twice. So the tunnel is built by a nested project
> (`tunnel/CMakeLists.txt`) that points at the same `3rd/mbedtls` tree.

> **Later**: mbedTLS 3.6 is the current LTS (supported until 2029). 3.6 split
> the framework into a separate repository, so upgrading means vendoring that
> too - a mechanical step, not a code change.

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
