// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_conn.h -- one accepted connection, and the TLS side of the broker.
//
// Moved out of tunnel_broker.cpp so the MQTT server (same port, same
// certificate, same accept loop) can wrap a connection too. Pure move: the
// signalling code is unchanged.
#pragma once

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

#include <cstring>
#include <memory>
#include <string>

#include "tunnel_net.h"
#include "tunnel_tls.h"

namespace tea_tunnel {

// ---- one connected signalling client ---------------------------------------
//
// A connection is a socket, and sometimes a TLS session on top of it. The
// two live together in one object: mbedTLS' socket helpers want a pointer to
// the socket, so the socket has to outlive the TLS session, and the TLS
// session has to outlive the handshake.
struct Connection {
  tea_socket_t fd = kTeaInvalidSocket;
  mbedtls_ssl_context ssl{};
  bool tls = false;

  ~Connection() {
    if (tls) {
      mbedtls_ssl_free(&ssl);
      tea_net_close(fd);
    } else if (fd != kTeaInvalidSocket) {
      tea_net_close(fd);
    }
  }
};

struct TlsMaterial {
  std::string cert_pem;
  std::string key_pem;
  std::string fingerprint;   // sha256 of the certificate, lower case hex
  std::string cert_path;
};
/// The TLS side of the signalling server: one configuration, many sessions.
///
/// The sessions are made on the accepted socket, one at a time, in the thread
/// that serves the peer - the handshake is quick and doing it there keeps the
/// accept loop free.
class TlsServer {
 public:
  bool start(const TlsMaterial& material, std::string* err) {
    mbedtls_ssl_config_init(&conf_);
    mbedtls_x509_crt_init(&cert_);
    mbedtls_pk_init(&key_);
    mbedtls_entropy_init(&entropy_);
    mbedtls_ctr_drbg_init(&drbg_);

    const int seeded = mbedtls_ctr_drbg_seed(&drbg_, mbedtls_entropy_func, &entropy_,
                                            reinterpret_cast<const unsigned char*>("a2-broker"),
                                            std::strlen("a2-broker"));
    if (seeded != 0) {
      *err = "cannot seed the random generator";
      return false;
    }
    int rc = mbedtls_ssl_config_defaults(&conf_, MBEDTLS_SSL_IS_SERVER,
                                         MBEDTLS_SSL_TRANSPORT_STREAM,
                                         MBEDTLS_SSL_PRESET_DEFAULT);
    if (rc != 0) {
      *err = "cannot set up the TLS configuration: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    mbedtls_ssl_conf_rng(&conf_, mbedtls_ctr_drbg_random, &drbg_);
    // The clients do not present a certificate: the room token is what says
    // who may join, and it never leaves the TLS session.
    mbedtls_ssl_conf_authmode(&conf_, MBEDTLS_SSL_VERIFY_NONE);

    rc = mbedtls_x509_crt_parse(&cert_, reinterpret_cast<const unsigned char*>(material.cert_pem.data()),
                                material.cert_pem.size() + 1);
    if (rc != 0) {
      *err = "cannot read the certificate: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    rc = mbedtls_pk_parse_key(&key_, reinterpret_cast<const unsigned char*>(material.key_pem.data()),
                              material.key_pem.size() + 1, nullptr, 0, mbedtls_ctr_drbg_random,
                              &drbg_);
    if (rc != 0) {
      *err = "cannot read the private key: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    rc = mbedtls_ssl_conf_own_cert(&conf_, &cert_, &key_);
    if (rc != 0) {
      *err = "cannot use the certificate: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    return true;
  }

  /// Wrap an accepted socket in TLS. Returns null when the handshake failed.
  ///
  /// The socket lives in the Connection object, and the BIO helpers are given
  /// its address: mbedTLS keeps that pointer for as long as the session
  /// lives, so it must not be a local variable.
  bool wrap(const std::shared_ptr<Connection>& conn, std::string* err) {
    mbedtls_ssl_init(&conn->ssl);
    int rc = mbedtls_ssl_setup(&conn->ssl, &conf_);
    if (rc != 0) {
      *err = "cannot set up the TLS session: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    mbedtls_ssl_set_bio(&conn->ssl, &conn->fd, &TlsServer::bio_send, &TlsServer::bio_recv,
                        nullptr);
    rc = mbedtls_ssl_handshake(&conn->ssl);
    if (rc != 0) {
      *err = "the TLS handshake failed: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    conn->tls = true;
    return true;
  }

 private:
  // mbedTLS' own socket helpers work on POSIX file descriptors; on Windows a
  // socket is not one, so the two calls are done here instead.
  static int bio_send(void* ctx, const unsigned char* buf, size_t len) {
    tea_socket_t fd = *static_cast<tea_socket_t*>(ctx);
    const int n = tea_net_send(fd, buf, len);
    if (n >= 0) return n;
    if (tea_net_would_block(tea_net_errno())) return MBEDTLS_ERR_SSL_WANT_WRITE;
    return MBEDTLS_ERR_NET_SEND_FAILED;
  }

  static int bio_recv(void* ctx, unsigned char* buf, size_t len) {
    tea_socket_t fd = *static_cast<tea_socket_t*>(ctx);
    const int n = tea_net_recv(fd, buf, len);
    if (n >= 0) return n;
    if (tea_net_would_block(tea_net_errno())) return MBEDTLS_ERR_SSL_WANT_READ;
    return MBEDTLS_ERR_NET_RECV_FAILED;
  }

  mbedtls_ssl_config conf_{};
  mbedtls_x509_crt cert_{};
  mbedtls_pk_context key_{};
  mbedtls_entropy_context entropy_{};
  mbedtls_ctr_drbg_context drbg_{};
};


}  // namespace tea_tunnel
