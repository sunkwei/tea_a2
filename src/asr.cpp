#include "asr.h"

#include <chrono>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "http.h"
#include "log.h"
#include "util.h"

namespace tea {
namespace {

/// The boundary of the multipart body. A fixed string is fine here: the
/// boundary only has to be unique *within* one request, and the audio bytes
/// are WAV data (no text that could contain this marker).
const char* kBoundary = "----teaaudio7f3c1d";

const char* kTranscriptionPath = "/audio/transcriptions";

std::string strip_trailing_slashes(std::string s) {
  while (!s.empty() && s.back() == '/') s.pop_back();
  return s;
}

bool url_has_path(const std::string& url) {
  const std::size_t scheme_end = url.find("://");
  if (scheme_end == std::string::npos) return false;
  return url.find('/', scheme_end + 3) != std::string::npos;
}

/// The provider's own words, when it reports a failure. It ends up in an API
/// answer, so it has to be valid UTF-8.
std::string extract_error_message(const nlohmann::json& j) {
  if (!j.is_object()) return std::string();
  auto err = j.find("error");
  if (err != j.end() && !err->is_null()) {
    if (err->is_string()) return sanitize_utf8(err->get<std::string>());
    if (err->is_object()) {
      auto m = err->find("message");
      if (m != err->end() && m->is_string()) return sanitize_utf8(m->get<std::string>());
      return dump_json(*err);
    }
  }
  const char* keys[] = {"message", "detail", "msg"};
  for (std::size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
    auto it = j.find(keys[i]);
    if (it != j.end() && it->is_string()) return sanitize_utf8(it->get<std::string>());
  }
  return std::string();
}

/// Build the multipart/form-data body the OpenAI transcription API expects:
/// one part holding the audio, one holding the model name.
std::string build_multipart(const std::string& wav, const std::string& model) {
  std::string body;
  body.reserve(wav.size() + 512);
  body += "--";
  body += kBoundary;
  body += "\r\n";
  body += "Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n";
  body += "Content-Type: audio/wav\r\n\r\n";
  body += wav;
  body += "\r\n";
  if (!model.empty()) {
    body += "--";
    body += kBoundary;
    body += "\r\n";
    body += "Content-Disposition: form-data; name=\"model\"\r\n\r\n";
    body += model;
    body += "\r\n";
  }
  body += "--";
  body += kBoundary;
  body += "--\r\n";
  return body;
}

}  // namespace

std::string asr_join_endpoint(const std::string& base_url) {
  std::string base = strip_trailing_slashes(trim(base_url));
  if (base.empty()) return base;
  // An explicit endpoint is used as it is: somebody who wrote the full path
  // means it.
  if (ends_with(base, kTranscriptionPath)) return base;
  if (ends_with(base, "/v1")) return base + kTranscriptionPath;
  if (!url_has_path(base)) return base + "/v1" + kTranscriptionPath;
  return base + kTranscriptionPath;
}

std::string asr_endpoint_url(const AsrConfig& cfg) { return cfg.url; }

bool is_wav_file(const std::string& bytes) {
  return bytes.size() >= 12 && bytes.compare(0, 4, "RIFF") == 0 && bytes.compare(8, 4, "WAVE") == 0;
}

AsrResult asr_transcribe(const AsrConfig& cfg, const std::string& wav) {
  AsrResult result;
  if (!cfg.configured || cfg.url.empty()) {
    result.error = cfg.error.empty()
                        ? "no speech-to-text service is configured (add an 'asr:' section to provider.yaml)"
                        : cfg.error;
    return result;
  }
  if (wav.empty()) {
    result.error = "the audio clip is empty";
    return result;
  }
  if (!is_wav_file(wav)) {
    result.error = "the audio is not a WAV file";
    return result;
  }

  HttpRequest http;
  http.method = "POST";
  http.url = cfg.url;
  http.body = build_multipart(wav, cfg.model);
  http.content_type = std::string("multipart/form-data; boundary=") + kBoundary;
  http.connect_timeout_ms = 10000;
  http.read_timeout_ms = cfg.timeout_ms > 0 ? cfg.timeout_ms : 30000;
  if (!cfg.api_key.empty()) {
    http.headers.push_back(HttpHeader{"Authorization", "Bearer " + cfg.api_key});
  }

  const std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now();
  const HttpResponse res = http_perform(http);
  result.elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::steady_clock::now() - started)
                          .count();
  result.http_status = res.status;

  if (!res.ok) {
    result.error = res.error.empty()
                       ? ("the transcription service did not answer (HTTP " +
                          std::to_string(res.status) + ")")
                       : res.error;
    return result;
  }
  if (res.status < 200 || res.status >= 300) {
    // The service answered, but with a refusal. Its own message is the useful
    // part ("unknown model", "audio too short"), so pass it through.
    std::string message;
    try {
      nlohmann::json j = nlohmann::json::parse(res.body);
      message = extract_error_message(j);
    } catch (const std::exception&) {
    }
    if (message.empty()) message = trim(res.body);
    if (message.size() > 400) message = truncate_utf8(message, 400) + "...";
    result.error = "the transcription service answered HTTP " + std::to_string(res.status) +
                   (message.empty() ? "" : (": " + message));
    return result;
  }

  nlohmann::json j = nlohmann::json::parse(res.body, nullptr, false);
  if (!j.is_object()) {
    result.error = "the transcription service did not answer with JSON";
    return result;
  }
  auto text = j.find("text");
  if (text == j.end() || !text->is_string()) {
    // Some servers answer {"text": ["..."]}; accept that too.
    result.error = "the transcription service's answer has no 'text' field";
    return result;
  }
  result.text = sanitize_utf8(text->get<std::string>());
  result.ok = true;
  LOG_INFO("asr: transcribed ", wav.size(), " bytes in ", result.elapsed_ms, "ms -> \"",
           truncate_utf8(result.text, 120), "\"");
  return result;
}

}  // namespace tea
