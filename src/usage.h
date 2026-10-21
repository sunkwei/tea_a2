// usage.h -- query a provider's quota endpoint ("how much of my subscription
// is left").
//
// This is NOT about the token counts of a chat response (that is
// `stream_include_usage` in the provider block). It is about a separate,
// provider-specific account endpoint - today only MiniMax M Plan, which
// exposes a 5-hour window and a weekly window:
//
//   GET https://www.minimax.cn/v1/token_plan/remains
//   Authorization: Bearer <M Plan subscription key>
//
// The endpoint is not OpenAI-standard, so a provider declares it
// (`supports_usage: true`, `usage_type: minimax_token_plan`) instead of tea_a2
// probing it.
#pragma once

#include <cstdint>
#include <string>

#include <nlohmann/json.hpp>

#include "config.h"

namespace tea {

/// One quota window. `name` is "5h" or "weekly" for the MiniMax M Plan.
struct UsageWindow {
  bool present = false;
  std::string name;
  /// How much of the window is left, 0..100. -1 = the provider did not say.
  double remaining_percent = -1.0;
  /// Epoch milliseconds.
  int64_t start_ms = 0;
  int64_t end_ms = 0;
  /// Milliseconds left until the window resets (0 = unknown).
  int64_t remains_ms = 0;
  /// The provider's own status code for this window (1 = healthy for M Plan).
  int status = -1;
};

struct UsageQuota {
  bool ok = false;
  std::string error;
  std::string provider;
  std::string type;        // "minimax_token_plan"
  std::string model_name;  // "general" for M Plan
  UsageWindow window;       // the short window (5 hours for M Plan)
  UsageWindow weekly;       // the long window (7 days for M Plan)

  nlohmann::json to_json() const;
};

/// The default endpoint of a usage type ("" when the type is unknown).
std::string usage_type_default_url(const std::string& type);
/// Known usage types, comma separated (for warning messages).
std::string usage_type_list();
/// Whether tea_a2 knows how to parse this kind of response.
bool usage_type_known(const std::string& type);

/// Query the quota of `provider`. Never throws: on failure `out->error` says
/// what went wrong and `out->ok` stays false.
bool query_usage(const ProviderInfo& provider, UsageQuota* out);

}  // namespace tea
