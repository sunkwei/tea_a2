// output_filter.h -- shrink tool output according to what it *is*, not just how
// big it is.
//
// A 40k-line build log is 95% progress noise and a handful of diagnostics, so
// blindly cutting it to the first N bytes throws away exactly the lines the
// model needs. The filter classifies the command, keeps the lines that carry
// meaning (errors, warnings, test failures) plus their context and the tail, and
// replaces everything else with an explicit marker so the model knows output was
// removed.
//
// It must be deterministic: results are stored in the session and replayed in
// later turns, so the same input has to produce byte-identical output.
#pragma once

#include <cstddef>
#include <string>

namespace tea {

struct OutputFilterOptions {
  std::size_t max_bytes = 131072;
  std::size_t max_lines = 400;
  std::size_t head_lines = 20;  // always kept verbatim from the top
  std::size_t tail_lines = 60;  // always kept verbatim from the bottom
  std::size_t context_lines = 2;  // kept after a diagnostic line
};

struct FilteredOutput {
  std::string text;
  bool changed = false;
  std::string strategy;  // "none" | "build" | "test" | "generic" | "bytes"
  std::size_t dropped_lines = 0;
};

/// Reduce `output` produced by `command`. `command` may be empty when unknown.
FilteredOutput filter_tool_output(const std::string& command, const std::string& output,
                                  const OutputFilterOptions& options);

/// Built-in self check, reachable as `tea_agent --selftest`. Verifies that build
/// noise is dropped, diagnostics and their context survive, markers are emitted,
/// the byte cap holds and the filter is deterministic.
bool output_filter_selftest(std::string* report);

}  // namespace tea
