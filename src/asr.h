// asr.h -- speech to text: forward a recorded clip to an ASR service.
//
// The service speaks the OpenAI transcription API
// (`POST /v1/audio/transcriptions`, multipart/form-data, answer
// `{"text": "..."}`), because that is what SenseVoiceSmall is usually served
// behind: the local sidecar in sched/asr_server.py and the cloud gateways that
// host the model all use the same shape. One client, two deployments.
//
// The browser does the audio work (it records and encodes 16 kHz mono WAV),
// so this side only has to move bytes and read one JSON field.
#pragma once

#include <cstdint>
#include <string>

#include "config.h"

namespace tea {

/// The result of one transcription.
struct AsrResult {
  bool ok = false;
  /// The transcript, exactly as the service returned it.
  std::string text;
  /// Why it failed. Empty when `ok`.
  std::string error;
  /// The HTTP status the service answered with (0 = no answer at all).
  int http_status = 0;
  long long elapsed_ms = 0;
};

/// The transcription endpoint for `cfg` (its `url`, already resolved).
std::string asr_endpoint_url(const AsrConfig& cfg);

/// The endpoint a base url points at: the base plus the standard
/// `/audio/transcriptions` path. A base that already ends in `/v1` gets the
/// path appended, a bare host gets `/v1` first - the same rule the chat
/// endpoint uses.
std::string asr_join_endpoint(const std::string& base_url);

/// True when `bytes` starts with a RIFF/WAVE header.
bool is_wav_file(const std::string& bytes);

/// Transcribe one WAV file. `wav` is a RIFF/WAVE file with 16-bit PCM
/// samples - what the browser produces (16 kHz, mono).
///
/// `cfg` must be configured (a url); otherwise the result carries the
/// reason. The service's own error message is passed through, because a
/// transcription service that says "no such model" is worth showing as it is.
AsrResult asr_transcribe(const AsrConfig& cfg, const std::string& wav);

}  // namespace tea
