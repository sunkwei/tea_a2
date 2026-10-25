// provider_yaml.h -- rewrite one provider's `models:` block inside
// provider.yaml, leaving the rest of the file alone.
//
// The settings page writes provider.yaml, and provider.yaml is a *human*
// file: it carries comments that explain the flags, it holds several
// providers, and it is often hand-edited. Re-emitting the whole document
// from the parsed model would throw all of that away on every click, so the
// update is done on the text: the `models:` block of the one provider that
// changed is cut out and replaced, and every other byte stays where it was.
#pragma once

#include <string>
#include <vector>

#include "config.h"

namespace tea {

/// Replace the `models:` block of `provider` in `yaml_text` with `models`.
/// The other providers, the comments and the settings block are kept as they
/// are. False with `err` set when the provider is not in the document.
bool patch_provider_models(const std::string& yaml_text, const std::string& provider,
                            const std::vector<ModelInfo>& models, std::string* out,
                            std::string* err);

}  // namespace tea
