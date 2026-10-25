// model_discovery.h -- ask a provider which models it offers, and work out the
// default configuration for each of them.
//
// Almost every OpenAI-compatible gateway exposes the same endpoint
// (GET {api_url}/v1/models), but they disagree about what it answers: the
// OpenAI shape is {"data":[{"id":...}]}, Ollama answers {"models":[{"name":...}]},
// OpenRouter adds a context_length and a list of supported parameters. tea_a2
// reads all of them and fills the blanks from what it knows about the model
// name, so the result is a ready-to-write `models:` block.
//
// The point of the whole exercise is the settings page: "更新模型" asks the
// provider what it has, shows the list with the defaults, and writes the
// confirmed result into provider.yaml.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "config.h"

namespace tea {

/// One model, with the configuration tea_a2 would write for it.
struct DiscoveredModel {
  std::string id;
  int64_t max_context_tokens = 0;
  int64_t max_output_tokens = 0;
  bool supports_tools = true;
  bool supports_reasoning = false;
  bool supports_vision = false;
  std::string note;
  /// Where the numbers came from:
  ///   "provider" the endpoint stated them,
  ///   "config"   they were in provider.yaml and the endpoint said nothing,
  ///   "builtin"  tea_a2 knows this model,
  ///   "guess"    the name gave it away ("...-reasoner", "-vl", ...),
  ///   "default"  nothing is known - the user has to fill it in.
  std::string source = "default";
  /// The model is already listed in provider.yaml.
  bool in_config = false;
};

struct ModelDiscovery {
  bool ok = false;
  std::string error;
  /// The endpoint that was queried.
  std::string url;
  /// What the provider offers, in the order it listed them.
  std::vector<DiscoveredModel> models;
  /// What provider.yaml lists today, in the same shape.
  std::vector<DiscoveredModel> current;
  /// Discovered models that provider.yaml does not know yet.
  std::vector<std::string> added;
  /// Models provider.yaml lists that the provider does not offer (any more).
  std::vector<std::string> removed;
  std::vector<std::string> warnings;
};

/// Query `provider` for its model list and fill in the defaults. Never throws:
/// on failure `out->error` says what went wrong and `out->ok` stays false.
bool discover_provider_models(const ProviderInfo& provider, ModelDiscovery* out);

/// What tea_a2 knows about a model name (context window, output limit, the
/// capability flags). False when the name is not in the table.
bool builtin_model_defaults(const std::string& model_id, ModelInfo* out);

}  // namespace tea
