#include "tls.h"

#include <cstdio>
#include <cstring>

#include "log.h"
#include "util.h"

#if defined(TEA_HAVE_MBEDTLS)
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ssl.h>
#include <mbedtls/version.h>
#include <mbedtls/x509_crt.h>
#endif

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#else
#include <errno.h>
#include <sys/socket.h>
#include <sys/types.h>
#endif

namespace tea {

#if defined(TEA_HAVE_MBEDTLS)

namespace {

#ifdef _WIN32
using tls_socket_t = SOCKET;
int socket_last_error() { return WSAGetLastError(); }
bool socket_would_block(int e) { return e == WSAEWOULDBLOCK; }
bool socket_timed_out(int e) { return e == WSAETIMEDOUT; }
#else
using tls_socket_t = int;
int socket_last_error() { return errno; }
bool socket_would_block(int e) { return e == EAGAIN || e == EWOULDBLOCK || e == EINTR; }
bool socket_timed_out(int e) { return e == ETIMEDOUT; }
#endif

std::string mbedtls_error_text(int code) {
  char buffer[256];
  std::memset(buffer, 0, sizeof(buffer));
  mbedtls_strerror(code, buffer, sizeof(buffer) - 1);
  std::string text = trim(buffer);
  if (text.empty()) {
    char hex[32];
    std::snprintf(hex, sizeof(hex), "error -0x%04x", -code);
    text = hex;
  }
  return text;
}

class MbedTlsStream : public TlsStream {
 public:
  explicit MbedTlsStream(tls_socket_t fd, const TlsOptions& options)
      : fd_(fd), options_(options) {
    mbedtls_ssl_init(&ssl_);
    mbedtls_ssl_config_init(&conf_);
    mbedtls_x509_crt_init(&ca_);
    mbedtls_ctr_drbg_init(&drbg_);
    mbedtls_entropy_init(&entropy_);
  }

  ~MbedTlsStream() {
    if (ready_) mbedtls_ssl_close_notify(&ssl_);
    mbedtls_ssl_free(&ssl_);
    mbedtls_ssl_config_free(&conf_);
    mbedtls_x509_crt_free(&ca_);
    mbedtls_ctr_drbg_free(&drbg_);
    mbedtls_entropy_free(&entropy_);
  }

  bool handshake(const std::string& host, std::string* err) {
    if (!options_.ca_bundle.empty()) {
      const int rc = mbedtls_x509_crt_parse_file(&ca_, options_.ca_bundle.c_str());
      if (rc < 0) {
        *err = "cannot load CA bundle '" + options_.ca_bundle + "': " + mbedtls_error_text(rc);
        return false;
      }
      ca_loaded_ = true;
    }
    if (options_.verify_peer && !ca_loaded_) {
      *err =
          "TLS peer verification is enabled but no CA bundle was loaded; pass --ca-bundle <pem> "
          "or --insecure";
      return false;
    }

    static const char* kPersonalisation = "tea_agent";
    int rc = mbedtls_ctr_drbg_seed(&drbg_, mbedtls_entropy_func, &entropy_,
                                   reinterpret_cast<const unsigned char*>(kPersonalisation),
                                   std::strlen(kPersonalisation));
    if (rc != 0) {
      *err = std::string("cannot seed the RNG: ") + mbedtls_error_text(rc);
      return false;
    }

    rc = mbedtls_ssl_config_defaults(&conf_, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                     MBEDTLS_SSL_PRESET_DEFAULT);
    if (rc != 0) {
      *err = std::string("cannot initialise the TLS config: ") + mbedtls_error_text(rc);
      return false;
    }
    mbedtls_ssl_conf_rng(&conf_, mbedtls_ctr_drbg_random, &drbg_);
    mbedtls_ssl_conf_min_version(&conf_, MBEDTLS_SSL_MAJOR_VERSION_3, MBEDTLS_SSL_MINOR_VERSION_3);
    if (options_.verify_peer && ca_loaded_) {
      mbedtls_ssl_conf_authmode(&conf_, MBEDTLS_SSL_VERIFY_REQUIRED);
      mbedtls_ssl_conf_ca_chain(&conf_, &ca_, nullptr);
    } else {
      mbedtls_ssl_conf_authmode(&conf_, MBEDTLS_SSL_VERIFY_NONE);
    }

    rc = mbedtls_ssl_setup(&ssl_, &conf_);
    if (rc != 0) {
      *err = std::string("cannot set up the TLS context: ") + mbedtls_error_text(rc);
      return false;
    }
    mbedtls_ssl_set_hostname(&ssl_, host.c_str());
    mbedtls_ssl_set_bio(&ssl_, this, &MbedTlsStream::bio_send, &MbedTlsStream::bio_recv, nullptr);

    for (;;) {
      rc = mbedtls_ssl_handshake(&ssl_);
      if (rc == 0) break;
      if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
      if (rc == MBEDTLS_ERR_SSL_TIMEOUT) {
        *err = "TLS handshake timed out";
        return false;
      }
      *err = std::string("TLS handshake with ") + host + " failed: " + mbedtls_error_text(rc);
      return false;
    }
    ready_ = true;

    if (options_.verify_peer) {
      const uint32_t flags = mbedtls_ssl_get_verify_result(&ssl_);
      if (flags != 0) {
        char info[512];
        std::memset(info, 0, sizeof(info));
        mbedtls_x509_crt_verify_info(info, sizeof(info) - 1, "", flags);
        *err = std::string("TLS certificate verification failed: ") + trim(info);
        return false;
      }
    }
    return true;
  }

  int read(char* buffer, std::size_t capacity, std::string* err) {
    if (capacity == 0) return 0;
    for (;;) {
      const int rc = mbedtls_ssl_read(&ssl_, reinterpret_cast<unsigned char*>(buffer), capacity);
      if (rc > 0) return rc;
      if (rc == 0 || rc == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) return 0;
      if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
      if (rc == MBEDTLS_ERR_SSL_TIMEOUT) {
        *err = "TLS read timed out";
        return -1;
      }
      *err = std::string("TLS read failed: ") + mbedtls_error_text(rc);
      return -1;
    }
  }

  bool write(const char* data, std::size_t length, std::string* err) {
    std::size_t written = 0;
    while (written < length) {
      const int rc = mbedtls_ssl_write(&ssl_,
                                       reinterpret_cast<const unsigned char*>(data + written),
                                       length - written);
      if (rc > 0) {
        written += static_cast<std::size_t>(rc);
        continue;
      }
      if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
      *err = std::string("TLS write failed: ") + mbedtls_error_text(rc);
      return false;
    }
    return true;
  }

  std::string description() const {
    const char* version = mbedtls_ssl_get_version(&ssl_);
    const char* suite = mbedtls_ssl_get_ciphersuite(&ssl_);
    std::string out = version ? version : "TLS";
    if (suite) {
      out += " / ";
      out += suite;
    }
    return out;
  }

 private:
  static int bio_recv(void* ctx, unsigned char* buffer, std::size_t length) {
    MbedTlsStream* self = static_cast<MbedTlsStream*>(ctx);
    const int want = length > 0x7fffffff ? 0x7fffffff : static_cast<int>(length);
    const int n = ::recv(self->fd_, reinterpret_cast<char*>(buffer), want, 0);
    if (n >= 0) return n;
    const int e = socket_last_error();
    if (socket_would_block(e)) return MBEDTLS_ERR_SSL_WANT_READ;
    if (socket_timed_out(e)) return MBEDTLS_ERR_SSL_TIMEOUT;
    return MBEDTLS_ERR_NET_RECV_FAILED;
  }

  static int bio_send(void* ctx, const unsigned char* buffer, std::size_t length) {
    MbedTlsStream* self = static_cast<MbedTlsStream*>(ctx);
    const int want = length > 0x7fffffff ? 0x7fffffff : static_cast<int>(length);
    const int n = ::send(self->fd_, reinterpret_cast<const char*>(buffer), want,
#ifdef _WIN32
                         0
#else
                         MSG_NOSIGNAL
#endif
    );
    if (n >= 0) return n;
    const int e = socket_last_error();
    if (socket_would_block(e)) return MBEDTLS_ERR_SSL_WANT_WRITE;
    return MBEDTLS_ERR_NET_SEND_FAILED;
  }

  tls_socket_t fd_;
  TlsOptions options_;
  bool ca_loaded_ = false;
  bool ready_ = false;
  mbedtls_ssl_context ssl_;
  mbedtls_ssl_config conf_;
  mbedtls_x509_crt ca_;
  mbedtls_ctr_drbg_context drbg_;
  mbedtls_entropy_context entropy_;
};

}  // namespace

bool tls_available() { return true; }

std::string tls_backend_name() { return std::string("mbedtls-") + MBEDTLS_VERSION_STRING; }

std::unique_ptr<TlsStream> tls_wrap_socket(std::intptr_t socket_handle, const TlsOptions& options,
                                           std::string* err) {
  (void)err;  // failures are reported by handshake()
  return std::unique_ptr<TlsStream>(
      new MbedTlsStream(static_cast<tls_socket_t>(socket_handle), options));
}

#else  // no TLS backend

bool tls_available() { return false; }
std::string tls_backend_name() { return "none"; }

std::unique_ptr<TlsStream> tls_wrap_socket(std::intptr_t, const TlsOptions&, std::string* err) {
  if (err) {
    *err =
        "this build has no TLS backend; https:// is only available through WinHTTP on Windows or "
        "by rebuilding with 3rd/mbedtls present";
  }
  return std::unique_ptr<TlsStream>();
}

#endif

}  // namespace tea
