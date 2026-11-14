// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_tls.h -- the TLS bits the broker and the sidecar share.
//
// Two things live here:
//
//   * a **self-signed certificate** the broker can make for itself, so a
//     single file is all a server needs (no nginx, no certificate to buy,
//     no file to copy around);
//
//   * the **fingerprint** of a certificate (the SHA-256 of its DER form).
//     The client can be told to pin that fingerprint instead of trusting a
//     name. That matters because the server's address may change: a pinned
//     certificate keeps working when the IP behind it moves, while a name
//     check ("the certificate must say broker.example.com") does not.
//
// The TLS stack is mbedTLS, the same one the rest of the project uses, so
// nothing new has to be installed next to the binary.
#pragma once

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/pk.h>
#include <mbedtls/sha256.h>
#include <mbedtls/x509_crt.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace tea_tunnel {

/// A human readable form of an mbedTLS error code.
inline std::string tls_error_text(int code) {
  char buffer[256];
  std::memset(buffer, 0, sizeof(buffer));
  mbedtls_strerror(code, buffer, sizeof(buffer) - 1);
  std::string text(buffer);
  if (text.empty()) {
    char hex[32];
    std::snprintf(hex, sizeof(hex), "error -0x%04x", -code);
    text = hex;
  }
  return text;
}

/// The SHA-256 of some bytes, as lower case hex.
inline std::string sha256_hex(const unsigned char* data, std::size_t len) {
  unsigned char digest[32];
  mbedtls_sha256(data, len, digest, 0);
  static const char* kHex = "0123456789abcdef";
  std::string out;
  out.reserve(64);
  for (int i = 0; i < 32; ++i) {
    out.push_back(kHex[(digest[i] >> 4) & 0x0F]);
    out.push_back(kHex[digest[i] & 0x0F]);
  }
  return out;
}

/// Bring up the entropy source and the random generator. Both the broker
/// (making a key) and the client (the TLS handshake) need one.
class TlsRandom {
 public:
  TlsRandom() {
    mbedtls_entropy_init(&entropy_);
    mbedtls_ctr_drbg_init(&drbg_);
  }
  ~TlsRandom() {
    mbedtls_ctr_drbg_free(&drbg_);
    mbedtls_entropy_free(&entropy_);
  }

  bool seed(const char* personalisation) {
    const int rc = mbedtls_ctr_drbg_seed(
        &drbg_, mbedtls_entropy_func, &entropy_,
        reinterpret_cast<const unsigned char*>(personalisation),
        std::strlen(personalisation));
    return rc == 0;
  }

  mbedtls_ctr_drbg_context* drbg() { return &drbg_; }

 private:
  mbedtls_entropy_context entropy_;
  mbedtls_ctr_drbg_context drbg_;
};

/// Make a self-signed certificate for the given common name.
///
/// The key is an EC P-256 one: it is small, it is fast to generate (a server
/// should be able to start without a long pause) and every TLS stack knows
/// it. The optional names go into the certificate as subject alternative
/// names, so a client that wants to check a *name* has something to check.
inline bool make_self_signed(const std::string& common_name,
                              const std::vector<std::string>& alt_names,
                              std::string* cert_pem, std::string* key_pem,
                              std::string* err) {
  TlsRandom random;
  if (!random.seed("a2-broker")) {
    *err = "cannot seed the random generator";
    return false;
  }

  mbedtls_pk_context key;
  mbedtls_pk_init(&key);
  int rc = mbedtls_pk_setup(&key, mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY));
  if (rc != 0) {
    *err = "cannot set up the key: " + tls_error_text(rc);
    mbedtls_pk_free(&key);
    return false;
  }
  mbedtls_ecp_keypair* pair = mbedtls_pk_ec(key);
  if (pair == nullptr) {
    *err = "this build of mbedTLS has no elliptic curve keys";
    mbedtls_pk_free(&key);
    return false;
  }
  rc = mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1, pair, mbedtls_ctr_drbg_random, random.drbg());
  if (rc != 0) {
    *err = "cannot generate the key: " + tls_error_text(rc);
    mbedtls_pk_free(&key);
    return false;
  }

  mbedtls_x509write_cert request;
  mbedtls_x509write_crt_init(&request);
  mbedtls_x509write_crt_set_version(&request, 2);
  mbedtls_x509write_crt_set_md_alg(&request, MBEDTLS_MD_SHA256);
  const std::string subject = "O=a2-tunnel,CN=" + common_name;
  mbedtls_x509write_crt_set_subject_name(&request, subject.c_str());
  mbedtls_x509write_crt_set_issuer_name(&request, subject.c_str());
  mbedtls_x509write_crt_set_subject_key(&request, &key);
  mbedtls_x509write_crt_set_issuer_key(&request, &key);
  mbedtls_x509write_crt_set_validity(&request, "20260101000000", "20360101000000");
  unsigned char serial[8] = {0};
  rc = mbedtls_ctr_drbg_random(random.drbg(), serial, sizeof(serial));
  if (rc == 0) rc = mbedtls_x509write_crt_set_serial_raw(&request, serial, sizeof(serial));
  if (rc != 0) {
    *err = "cannot set the serial number: " + tls_error_text(rc);
    mbedtls_x509write_crt_free(&request);
    mbedtls_pk_free(&key);
    return false;
  }

  // The alternative names: a small linked list of DNS names.
  std::vector<mbedtls_x509_san_list> names;
  std::vector<std::string> name_storage;
  name_storage.reserve(alt_names.size());
  for (const std::string& alt : alt_names) {
    mbedtls_x509_san_list entry{};
    entry.node.type = MBEDTLS_X509_SAN_DNS_NAME;
    entry.next = nullptr;
    name_storage.push_back(alt);
    entry.node.san.unstructured_name.p =
        reinterpret_cast<unsigned char*>(const_cast<char*>(name_storage.back().c_str()));
    entry.node.san.unstructured_name.len = name_storage.back().size();
    names.push_back(entry);
  }
  for (std::size_t i = 0; i + 1 < names.size(); ++i) names[i].next = &names[i + 1];
  if (!names.empty()) {
    rc = mbedtls_x509write_crt_set_subject_alternative_name(&request, &names.front());
    if (rc != 0) {
      *err = "cannot set the alternative names: " + tls_error_text(rc);
      mbedtls_x509write_crt_free(&request);
      mbedtls_pk_free(&key);
      return false;
    }
  }

  std::vector<unsigned char> certificate(8192);
  rc = mbedtls_x509write_crt_pem(&request, certificate.data(), certificate.size(),
                                 mbedtls_ctr_drbg_random, random.drbg());
  mbedtls_x509write_crt_free(&request);
  if (rc != 0) {
    *err = "cannot write the certificate: " + tls_error_text(rc);
    mbedtls_pk_free(&key);
    return false;
  }
  certificate.resize(std::strlen(reinterpret_cast<const char*>(certificate.data())));

  std::vector<unsigned char> private_key(8192);
  rc = mbedtls_pk_write_key_pem(&key, private_key.data(), private_key.size());
  mbedtls_pk_free(&key);
  if (rc != 0) {
    *err = "cannot write the private key: " + tls_error_text(rc);
    return false;
  }
  private_key.resize(std::strlen(reinterpret_cast<const char*>(private_key.data())));

  cert_pem->assign(reinterpret_cast<const char*>(certificate.data()), certificate.size());
  key_pem->assign(reinterpret_cast<const char*>(private_key.data()), private_key.size());
  return true;
}

/// The fingerprint of a certificate in PEM form: the SHA-256 of its DER
/// form, as lower case hex.
inline bool certificate_fingerprint(const std::string& cert_pem, std::string* hex_out,
                                    std::string* err) {
  mbedtls_x509_crt cert;
  mbedtls_x509_crt_init(&cert);
  const int rc = mbedtls_x509_crt_parse(
      &cert, reinterpret_cast<const unsigned char*>(cert_pem.data()), cert_pem.size() + 1);
  if (rc != 0) {
    *err = "cannot read the certificate: " + tls_error_text(rc);
    mbedtls_x509_crt_free(&cert);
    return false;
  }
  *hex_out = sha256_hex(cert.raw.p, cert.raw.len);
  mbedtls_x509_crt_free(&cert);
  return true;
}

/// Put a fingerprint into the one form we compare: lower case hex without
/// separators. People copy fingerprints in many shapes ("ab:cd:...", upper
/// case, with spaces), so everything is folded into one.
inline std::string normalize_fingerprint(const std::string& text) {
  std::string out;
  out.reserve(text.size());
  for (char c : text) {
    if (c == ':' || c == ' ' || c == '-' || c == '\t' || c == '\r' || c == '\n') continue;
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) out.push_back(c);
  }
  return out;
}

/// Does the certificate in DER form have this fingerprint?
inline bool fingerprint_matches(const unsigned char* der, std::size_t len,
                                const std::string& expected) {
  const std::string wanted = normalize_fingerprint(expected);
  if (wanted.empty()) return false;
  return sha256_hex(der, len) == wanted;
}

}  // namespace tea_tunnel
