#include "agent.h"

#include <sstream>

#include "command.h"
#include "env.h"
#include "http.h"
#include "log.h"
#include "trajectory.h"
#include "util.h"
#include "version.h"

namespace tea {
namespace {

/// How much of agent.md goes into the system prompt. The file itself is capped
/// at 32 KB on disk; the prompt only needs the part that changes behaviour, and
/// a smaller cap keeps the per-turn prompt cache stable and cheap.
constexpr std::size_t kAgentDocPromptBytes = 6000;

/// How many images toolkit_load_image may attach in one turn. Each one costs
/// roughly a thousand tokens on every later request of the turn (and of every
/// turn after it, since they stay in the history), so the cap exists to keep a
/// curious model from filling the window with screenshots.
constexpr std::size_t kMaxToolImagesPerTurn = 6;

/// Base64 payload budget for the images re-sent with one request. A vision
/// conversation accumulates pictures; without a cap a long session would
/// re-upload everything on every iteration until the provider rejects it.
constexpr std::size_t kMaxEncodedImageBytesPerTurn = 24u * 1024u * 1024u;

void accumulate_usage(ChatUsage* total, const ChatUsage& part) {
  if (!part.present) return;
  total->present = true;
  total->prompt_tokens += part.prompt_tokens;
  total->completion_tokens += part.completion_tokens;
  total->total_tokens += part.total_tokens;
  // Cache hits have to be summed as well, otherwise a multi-iteration turn
  // reports a 0% hit rate even when every single request was a prefix hit.
  total->cached_tokens += part.cached_tokens;
  total->raw = part.raw;
}

nlohmann::json usage_json(const ChatUsage& usage) {
  if (!usage.present) {
    nlohmann::json j = nlohmann::json::object();
    j["prompt_tokens"] = 0;
    j["completion_tokens"] = 0;
    j["total_tokens"] = 0;
    j["cached_tokens"] = 0;
    return j;
  }
  return usage.to_json();
}

/// Prompt tokens a tool call adds: its name plus the raw JSON arguments.
long long estimate_tool_call_tokens(const std::vector<ToolCall>& calls) {
  long long total = 0;
  for (std::size_t i = 0; i < calls.size(); ++i) {
    total += Agent::estimate_tokens(calls[i].name) + Agent::estimate_tokens(calls[i].raw_arguments) + 4;
  }
  return total;
}

/// Output speed is measured over the streaming window, not over the whole
/// request: queueing and network latency would otherwise drag the number down.
/// A window shorter than a quarter second is noise (single-chunk replies,
/// non-streaming requests), so those fall back to the total request time.
long long effective_generation_ms(const ChatResult& chat) {
  return chat.generation_ms >= 250 ? chat.generation_ms : chat.elapsed_ms;
}

/// The `metrics` block of `iteration_end` / `done`: exactly what the UI needs to
/// render output speed, cache hit rate and context-window usage.
nlohmann::json metrics_json(long long max_context_tokens, long long context_tokens,
                            long long output_tokens, long long ttft_ms, long long generation_ms,
                            bool estimated, long long elapsed_ms) {
  nlohmann::json j = nlohmann::json::object();
  j["max_context_tokens"] = max_context_tokens;
  j["context_tokens"] = context_tokens;
  j["output_tokens"] = output_tokens;
  j["ttft_ms"] = ttft_ms;
  j["generation_ms"] = generation_ms;
  // true => the provider sent no usage and these are local estimates.
  j["estimated"] = estimated;
  j["elapsed_ms"] = elapsed_ms;
  return j;
}

/// Does this error say "I do not know the field `stream_options`"? Gateways
/// differ in wording, so look for the field name itself, plus the usual
/// "unknown/unrecognized/unexpected field" phrasing.
bool rejects_stream_options(const std::string& message) {
  const std::string lowered = to_lower(message);
  if (lowered.find("stream_options") == std::string::npos &&
      lowered.find("include_usage") == std::string::npos) {
    return false;
  }
  static const char* const kHints[] = {"unknown", "unrecognized", "unrecognised", "unexpected",
                                       "not supported", "unsupported", "invalid", "not allowed",
                                       "extra fields", "not permitted"};
  for (std::size_t i = 0; i < sizeof(kHints) / sizeof(kHints[0]); ++i) {
    if (lowered.find(kHints[i]) != std::string::npos) return true;
  }
  return false;
}

/// Providers word "your request does not fit" differently; recognise the family
/// so one forced compaction can rescue the request (dsh's overflow trigger).
bool looks_like_context_overflow(const std::string& lowered) {
  static const char* const kMarkers[] = {
      "context length",          "context_length_exceeded", "maximum context",
      "context window",          "too many tokens",         "prompt is too long",
      "reduce the length",       "exceeds the model",       "input is too long",
      "上下文长度",              "超出上下文"};
  for (std::size_t i = 0; i < sizeof(kMarkers) / sizeof(kMarkers[0]); ++i) {
    if (lowered.find(kMarkers[i]) != std::string::npos) return true;
  }
  return false;
}

bool resolve_reasoning_echo(const AppConfig& config, const ProviderInfo& provider) {
  bool learned = false;
  if (config.lookup_reasoning_echo(provider.name, &learned)) return learned;

  if (provider.reasoning_echo == ReasoningEcho::Keep) return true;
  if (provider.reasoning_echo == ReasoningEcho::Strip) return false;

  // Auto: the two behaviours are both real. DeepSeek's API reference documents
  // reasoning_content only as an *output* field of the assistant message, so we
  // do not send it there. opencode.ai and other thinking-mode gateways reject a
  // follow-up request that omits it ("The `reasoning_content` in the thinking
  // mode must be passed back to the API"), so we keep it everywhere else. If a
  // provider still disagrees, run_turn flips this and retries once.
  const ParsedUrl url = parse_url(provider.api_url);
  if (!url.valid) return false;
  const std::string host = to_lower(url.host);
  if (host == "deepseek.com" || ends_with(host, ".deepseek.com")) return false;
  return true;
}



}  // namespace

long long Agent::estimate_tokens(const std::string& text) {
  // Per-script heuristic: ASCII runs about 4 characters per token, CJK about one
  // token per character, and other scripts (Cyrillic, Arabic, emoji...) sit in
  // between. Markdown/JSON punctuation is counted with the ASCII run, which makes
  // the estimate slightly conservative - the direction we want.
  long long ascii = 0;
  long long cjk = 0;
  long long other = 0;
  for (std::size_t i = 0; i < text.size();) {
    const unsigned char c = static_cast<unsigned char>(text[i]);
    if (c < 0x80) {
      if (c != ' ' && c != '\n' && c != '\t' && c != '\r') ++ascii;
      ++i;
      continue;
    }
    unsigned int cp = 0;
    std::size_t len = 1;
    if ((c & 0xE0) == 0xC0) {
      cp = c & 0x1Fu;
      len = 2;
    } else if ((c & 0xF0) == 0xE0) {
      cp = c & 0x0Fu;
      len = 3;
    } else if ((c & 0xF8) == 0xF0) {
      cp = c & 0x07u;
      len = 4;
    } else {
      ++other;
      ++i;
      continue;
    }
    for (std::size_t k = 1; k < len && i + k < text.size(); ++k) {
      cp = (cp << 6) | (static_cast<unsigned char>(text[i + k]) & 0x3Fu);
    }
    i += len;
    const bool wide =
        (cp >= 0x3040 && cp <= 0x30FF) ||   // kana
        (cp >= 0x3400 && cp <= 0x4DBF) ||   // CJK extension A
        (cp >= 0x4E00 && cp <= 0x9FFF) ||   // CJK unified
        (cp >= 0xAC00 && cp <= 0xD7AF) ||   // hangul
        (cp >= 0xF900 && cp <= 0xFAFF) ||   // CJK compatibility
        (cp >= 0xFF00 && cp <= 0xFF60);     // fullwidth forms
    if (wide) {
      ++cjk;
    } else {
      ++other;
    }
  }
  const long long total = ascii / 4 + cjk + other / 2;
  return total > 0 ? total + 4 : 4;
}
long long Agent::estimate_tokens(const std::vector<Message>& messages) {
  long long total = 0;
  for (std::size_t i = 0; i < messages.size(); ++i) {
    const Message& m = messages[i];
    total += estimate_tokens(m.content);
    total += estimate_tokens(m.reasoning) / 2;
    for (std::size_t k = 0; k < m.tool_calls.size(); ++k) {
      total += estimate_tokens(m.tool_calls[k].name);
      total += estimate_tokens(m.tool_calls[k].raw_arguments);
    }
    total += 8;
  }
  return total;
}

/// The extra directories this turn may touch, as the prompt should say it.
static std::string allowed_roots_line(const Workspace& workspace) {
  const std::vector<std::string>& roots = workspace.allowed_roots();
  if (roots.empty()) return std::string();
  std::string out = "- File tools may also access these directories in this turn: ";
  for (std::size_t i = 0; i < roots.size(); ++i) {
    out += (i ? ", " : "");
    out += roots[i];
  }
  out += "\n";
  return out;
}

std::string Agent::build_system_prompt(const Workspace& workspace, const Session& session,
                                           const std::string& persona_override,
                                           const std::string& task_context) const {
  const EnvironmentInfo& env = machine_environment();
  const Settings& st = workspace.settings();
  std::ostringstream out;
  // Persona precedence, most specific first:
  //   1. `<workspace>/.tea_agent_run/system_prompt.md`, read fresh on every call
  //      so an edit in the UI reaches the *next* turn (or the next iteration of
  //      a running one) without a restart;
  //   2. the workspace's `settings.yaml: system_prompt` (loaded when opened);
  //   3. the machine-level default (tea_agent.yaml or the built-in rules).
  // A caller that drives its own policy may hand in the persona it wants;
  // it wins over the workspace file, which in turn wins over the machine default.
  const std::string override_prompt = trim(persona_override);
  const std::string workspace_prompt = trim(workspace.load_system_prompt());
  if (!override_prompt.empty()) {
    out << override_prompt << "\n\n";
  } else if (!workspace_prompt.empty()) {
    out << workspace_prompt << "\n\n";
  } else if (!st.system_prompt.empty()) {
    out << st.system_prompt << "\n\n";
  } else {
    out << config_->default_system_prompt() << "\n\n";
  }

  // Everything below is probed from the real device and kept byte-stable for the
  // process/session lifetime: a varying block at the head of the conversation
  // would invalidate the provider's prompt cache on every single turn.
  out << "Environment (probed from this device at startup)\n";
  out << "- OS: " << env.os_name;
  if (!env.os_version.empty()) out << " " << env.os_version;
  out << " (" << env.arch << ")\n";
  if (!env.hostname.empty()) out << "- Host: " << env.hostname << "\n";
  if (!env.timezone.empty()) out << "- Time zone: " << env.timezone << "\n";
  out << "- CPUs: " << env.cpu_count;
  if (env.memory_total_bytes > 0) out << ", memory: " << binary_size(env.memory_total_bytes);
  out << "\n";
  if (workspace.free_bytes() > 0) {
    out << "- Free space on the workspace volume: " << binary_size(workspace.free_bytes()) << "\n";
  }

  const std::string active_shell = st.shell.empty() ? default_shell() : st.shell;
  out << "- toolkit_exec shell: " << active_shell;
  if (!env.shells.empty()) {
    std::string others;
    for (std::size_t i = 0; i < env.shells.size(); ++i) {
      if (env.shells[i].name == active_shell) continue;
      if (!others.empty()) others += ", ";
      others += env.shells[i].name;
    }
    out << " (also installed: " << (others.empty() ? "none" : others) << ")";
  }
  out << "\n";
  out << "- Workspace root: " << workspace.root() << "\n";
  out << "- Agent run directory: " << workspace.run_dir() << "\n";
  out << "- Default command timeout: " << st.command_timeout_ms << " ms\n";
  if (st.allow_outside_workspace) {
    // Say so explicitly: the boundary being lifted changes what the tools will
    // actually accept, and a model that assumes it is still sandboxed wastes
    // turns on refusals (or, worse, tells the user it cannot do something it can).
    out << "- File tools may access paths OUTSIDE the workspace root: the operator "
           "lifted the workspace boundary for this workspace (allow_outside_workspace). "
           "Prefer staying inside the workspace unless the task really needs another "
           "path, and never modify anything outside it without being asked.\n";
  } else {
    out << "- File tools are restricted to the workspace root";
    const std::string extra = allowed_roots_line(workspace);
    if (!extra.empty()) {
      out << ", plus the directories this turn was given";
      out << extra;
    } else {
      out << ".\n";
    }
  }
  if (!session.info.created_at.empty()) {
    out << "- Session started (UTC): " << session.info.created_at << "\n";
  }
  out << "- The wall clock is intentionally not injected here; run `date` or "
         "`echo %DATE% %TIME%` when the current time matters.\n";

  const std::string& instructions = workspace.instructions();
  if (!instructions.empty()) {
    out << "\nWorkspace instructions (from .tea_agent_run/instructions.md)\n";
    out << instructions << "\n";
  }

  // Experience log: unlike instructions.md (the user's rules, cached for the
  // process lifetime) this file is written by the agent, so it is read fresh on
  // every turn. It is injected even when empty, together with the rule for
  // maintaining it - an empty file that nobody is told about never gets used.
  {
    bool doc_truncated = false;
    std::string doc = workspace.load_agent_doc(&doc_truncated);
    doc = trim(doc);
    if (doc.size() > kAgentDocPromptBytes) {
      bool cut = false;
      doc = truncate_utf8(doc, kAgentDocPromptBytes, &cut) +
            "\n... [agent.md truncated for the prompt; read the full file with toolkit_agent_doc]";
      doc_truncated = true;
    }
    out << "\nWorkspace experience (`.tea_agent_run/agent.md`, maintained by you across sessions)\n";
    if (doc.empty()) {
      out << "(empty so far)\n";
    } else {
      out << doc << "\n";
    }
    out << "Keep it current: when you hit something specific to THIS workspace that would save "
           "time next session - a pitfall, a non-obvious command, a script you wrote - append a "
           "one-line entry with toolkit_agent_doc (op=append). Put reusable scripts under "
           "`.tea_agent_run/scripts/` and record their path here. Merge or drop stale entries "
           "instead of piling up history.";
    if (doc_truncated) out << " (The copy above is truncated.)";
    out << "\n";
  }

  if (!session.summary.empty()) {
    out << "\nCompact history (older turns were summarised to save context; treat "
           "these as established facts)\n";
    out << session.summary << "\n";
  }

  std::vector<nlohmann::json> notes = workspace.load_memory(nullptr);
  if (!notes.empty()) {
    out << "\nWorkspace memory (durable notes previously stored for this workspace)\n";
    const std::size_t limit = notes.size() > 40 ? 40 : notes.size();
    for (std::size_t i = 0; i < limit; ++i) {
      out << "- " << notes[i].value("text", std::string()) << "\n";
    }
  }

  // The task list is last on purpose: it is the one block that changes during a
  // turn, and keeping it behind the (stable) environment/instructions/agent.md
  // text means a todo update only invalidates the tail of the prompt cache.
  // It is injected every turn, not just when it changes, so a compacted
  // conversation or a fresh process still knows what the plan was.
  if (!session.todos.empty()) {
    int total = 0, done = 0;
    todo_counts(session.todos, &total, &done);
    out << "\nTask list (" << done << "/" << total
        << " done; maintained with toolkit_todo, shown to the user as live progress)\n";
    for (std::size_t i = 0; i < session.todos.size(); ++i) {
      const TodoItem& t = session.todos[i];
      out << "- [" << (t.status == "done" ? "x" : (t.status == "active" ? "~" : " "))
          << "] " << t.text;
      if (t.status == "active") out << "   <- in progress";
      out << "\n";
    }
    out << "Keep it current: mark a step active before starting it and done as soon as it is "
           "finished (exactly one active at a time). Do not restate the list in your answer - "
           "the user already sees it.";
  }

  // A turn that works on a queued task. The model has to know: without this it
  // treats the work as an ordinary question, never reports progress, and never
  // asks the user when it is stuck - the task record then says "0%" for the
  // whole run and the scheduler has nothing to watch.
  if (!task_context.empty()) {
    out << "\nThis turn works on a QUEUED TASK: " << task_context << "\n";
    out << "The work was put in a queue to be done later; a scheduler started it when the "
           "provider had quota left. Two things follow from that:\n";
    out << "  * Report on the task with toolkit_task (op=update): progress=0-100 and a one-line "
           "note of what you just did. That report is what the task server records and shows - "
           "without it the task looks like nothing happened.\n";
    out << "  * If the work cannot go on without the user, report it with "
           "needs_confirmation=true and your question. The task then waits for an answer instead "
           "of being marked done - say what you need and stop there.\n";
    out << "Do the work as if the user had asked for it right now: the queue says when it "
           "starts, not what it is.\n";
  }
  return out.str();
}
namespace {

/// Context cost of one image, in tokens. Vision tokenisation depends on the
/// provider and the resolution, so this is a deliberately conservative flat
/// estimate (~1024x1024 on a tiled encoder). It only feeds history trimming and
/// the UI's context gauge; the provider's reported usage overrides it.
constexpr long long kImageTokenCost = 1024;

/// Bytes and tokens for one message, including its tool-call payload.
long long message_cost(const Message& m) {
  long long cost = Agent::estimate_tokens(m.content) + Agent::estimate_tokens(m.reasoning) / 2 + 8;
  cost += static_cast<long long>(m.images.size()) * kImageTokenCost;
  for (std::size_t k = 0; k < m.tool_calls.size(); ++k) {
    cost += Agent::estimate_tokens(m.tool_calls[k].name);
    cost += Agent::estimate_tokens(m.tool_calls[k].raw_arguments);
  }
  return cost;
}

/// Keep the head and the tail of an over-long single message. Used only when one
/// message alone would not fit, which happens with a huge pasted file.
std::string truncate_middle(const std::string& text, std::size_t keep_bytes) {
  if (text.size() <= keep_bytes || keep_bytes < 64) return text;
  const std::size_t head = keep_bytes * 2 / 3;
  const std::size_t tail = keep_bytes - head;
  // Both ends have to land on a character boundary. A cut in the middle of a
  // multi-byte character leaves the message invalid UTF-8, and the JSON layer
  // refuses to serialise that - which used to kill the turn with "invalid
  // UTF-8 byte at index N" right after a big tool result was trimmed.
  const std::string head_text = truncate_utf8(text, head);
  std::string tail_text = text.substr(text.size() - tail);
  std::size_t skip = 0;
  while (skip < 4 && skip < tail_text.size() &&
         (static_cast<unsigned char>(tail_text[skip]) & 0xC0) == 0x80) {
    ++skip;
  }
  if (skip > 0) tail_text.erase(0, skip);
  std::ostringstream out;
  out << head_text << "\n... [tea_agent truncated " << (text.size() - head - tail)
      << " bytes of a single oversized message] ...\n"
      << tail_text;
  return out.str();
}

}  // namespace

void Agent::build_context(const Workspace& workspace, const Session& session,
                          const ModelInfo* model, std::vector<Message>* out,
                          ContextStats* stats, const std::string& persona_override,
                          const std::string& task_context) const {
  const Settings& st = workspace.settings();
  out->clear();
  Message system;
  system.role = "system";
  system.content = build_system_prompt(workspace, session, persona_override, task_context);
  system.created_at = now_iso8601();
  out->push_back(system);

  const long long max_context =
      (model && model->max_context_tokens > 0) ? model->max_context_tokens : 128000;

  // Budget = context window - what the completion may consume - the parts of the
  // prompt that are not history (system block, tool schemas, framing). The old
  // code just took 55% of the window, which both under-used big windows and could
  // overflow when max_tokens was configured large.
  long long max_output = st.max_tokens > 0 ? st.max_tokens
                                           : (model && model->max_output_tokens > 0
                                                  ? model->max_output_tokens
                                                  : 8192);
  const long long output_cap = max_context / 3;
  if (max_output > output_cap) max_output = output_cap;

  // dsh compacts at a ratio of the window rather than at the hard boundary:
  // threshold 0.8 of the message budget, verbatim tail down to 0.16. Compacting
  // exactly "just enough" would re-trigger on the very next turn.
  // 4 core tool schemas (exec/file/memory/agent_doc); measured from the JSON that
  // /api/tools serves, ~4 bytes per token. Provider-scoped web tools add more,
  // which the callers cover by keeping this reserve on the generous side.
  const long long tool_schema_cost = 960;
  const long long fixed = estimate_tokens(system.content) + tool_schema_cost + 64;
  long long budget = max_context - max_output - fixed;
  const long long floor_budget = max_context / 5;
  if (budget < floor_budget) budget = floor_budget;

  // Walk backwards until the budget is exhausted. `keep_turns` is a floor, not a
  // suggestion: we keep pulling whole turns while fewer than that many user
  // messages are in the window, as long as we stay under the hard ceiling.
  std::vector<Message> tail;
  long long used = fixed;
  int user_turns = 0;
  const long long ceiling = max_context - max_output;
  const std::size_t first =
      session.summary_upto <= session.messages.size() ? session.summary_upto : session.messages.size();
  for (std::size_t i = session.messages.size(); i-- > first;) {
    const Message& m = session.messages[i];
    const long long cost = message_cost(m);
    const bool below_keep_turns = user_turns < st.keep_turns;
    if (!tail.empty() && used + cost > budget && !below_keep_turns) break;
    if (!tail.empty() && used + cost > ceiling) break;
    tail.push_back(m);
    used += cost;
    if (m.role == "user") ++user_turns;
  }
  std::reverse(tail.begin(), tail.end());
  while (!tail.empty() && tail.front().role != "user") tail.erase(tail.begin());

  if (tail.empty() && !session.messages.empty()) {
    // One message alone exceeds the budget; keep it but trim it to fit.
    Message m = session.messages.back();
    if (message_cost(m) > budget) {
      const std::size_t keep_bytes = static_cast<std::size_t>(budget > 4096 ? budget * 4 : 16384);
      m.content = truncate_middle(m.content, keep_bytes);
      if (!m.reasoning.empty()) m.reasoning = truncate_middle(m.reasoning, keep_bytes / 4);
    }
    tail.push_back(m);
  }

  // Cap absurdly long assistant replies in the wire copy only: the transcript on
  // disk keeps them in full, the model does not need a pasted file replayed in
  // every subsequent turn.
  if (st.max_assistant_content > 0) {
    const std::size_t cap = static_cast<std::size_t>(st.max_assistant_content);
    for (std::size_t i = 0; i < tail.size(); ++i) {
      if (tail[i].role != "assistant") continue;
      if (tail[i].content.size() <= cap) continue;
      tail[i].content = truncate_middle(tail[i].content, cap);
    }
  }

  LOG_DEBUG("context: max=", max_context, " reserve_output=", max_output, " fixed=", fixed,
            " budget=", budget, " kept=", tail.size(), "/", session.messages.size(),
            " used=", used, " keep_turns=", st.keep_turns,
            session.summary_upto ? " summarised=" : "", session.summary_upto);
  if (stats) {
    long long live_cost = 0;
    for (std::size_t i = first; i < session.messages.size(); ++i) {
      live_cost += message_cost(session.messages[i]);
    }
    stats->kept = tail.size();
    stats->total = session.messages.size();
    stats->budget = budget;
    stats->used = used;
    stats->live_used = live_cost;
    stats->trigger = static_cast<long long>(static_cast<double>(budget) * st.compaction_trigger_ratio);
    stats->trimmed = tail.size() < (session.messages.size() - first);
  }
  for (std::size_t i = 0; i < tail.size(); ++i) out->push_back(tail[i]);
}
bool Agent::compact_history(Workspace& workspace, Session* session, const std::string& provider,
                            const std::string& model, const ModelInfo* model_info,
                            std::atomic<bool>* cancel, long long retain_tokens) {
  if (!session) return false;
  const Settings& st = workspace.settings();
  const std::size_t total = session->messages.size();
  const std::size_t min_keep =
      st.compaction_keep_recent > 0 ? static_cast<std::size_t>(st.compaction_keep_recent) : 4;

  // Keep a verbatim tail worth `retain_tokens` (dsh: 16% of the budget) instead of
  // "the last N messages": compacting just enough makes the next turn compact
  // again, so we aim well below the trigger.
  long long accumulated = 0;
  std::size_t keep_from = total;
  for (std::size_t i = total; i-- > session->summary_upto;) {
    accumulated += message_cost(session->messages[i]);
    keep_from = i;
    if (accumulated >= retain_tokens && (total - keep_from) >= min_keep) break;
  }
  // Never split a turn: the kept window must start at a user message, which also
  // guarantees no assistant tool-call/result pair is cut in half.
  while (keep_from > session->summary_upto && session->messages[keep_from - 1].role != "user") {
    --keep_from;
  }
  if (keep_from <= session->summary_upto) return false;
  const std::size_t split = keep_from;

  // Bound the request: the segment can itself be large the first time compaction
  // runs on a long session. Keep the newest part and say so in the transcript.
  const std::size_t kTranscriptLimit = 60000;
  std::ostringstream transcript;
  for (std::size_t i = session->summary_upto; i < split; ++i) {
    const Message& m = session->messages[i];
    std::string content = m.content;
    if (content.size() > 4000) content = content.substr(0, 4000) + " ... [truncated]";
    transcript << "[" << m.role;
    if (!m.name.empty()) transcript << " " << m.name;
    if (!m.tool_calls.empty()) {
      transcript << " called ";
      for (std::size_t k = 0; k < m.tool_calls.size(); ++k) {
        transcript << (k ? ", " : "") << m.tool_calls[k].name;
      }
    }
    transcript << "] " << content << "\n";
  }
  std::string segment = transcript.str();
  if (segment.size() > kTranscriptLimit) {
    segment = std::string(
                  "Note: the oldest part of this segment was dropped before summarisation.\n") +
              segment.substr(segment.size() - kTranscriptLimit);
  }

  const ProviderInfo* provider_info = config_->provider(provider);
  if (!provider_info) return false;

  ChatRequest request;
  request.provider = provider;
  request.model = model;
  request.api_url = provider_info->api_url;
  request.api_key = provider_info->api_key;
  request.stream = false;
  request.headers = provider_info->headers;
  for (std::size_t i = 0; i < request.headers.size(); ++i) {
    request.headers[i].second =
        replace_all(request.headers[i].second, "${session_id}", session->info.id);
  }
  {
    bool has_session_header = false;
    for (std::size_t i = 0; i < request.headers.size(); ++i) {
      if (to_lower(request.headers[i].first) == "x-opencode-session") has_session_header = true;
    }
    const ParsedUrl parsed = parse_url(provider_info->api_url);
    if (!has_session_header && parsed.valid && ends_with(to_lower(parsed.host), "opencode.ai")) {
      request.headers.push_back(
          std::make_pair(std::string("x-opencode-session"), session->info.id));
    }
  }
  request.connect_timeout_ms = st.http_connect_timeout_ms;
  request.read_timeout_ms = st.http_read_timeout_ms;
  request.max_tokens = st.compaction_summary_max_tokens > 0 ? st.compaction_summary_max_tokens : 800;
  request.has_temperature = true;
  request.temperature = 0.0;
  request.send_reasoning_effort = false;

  Message system = Message::make("system",
                                 "You compress conversation history for a software-engineering "
                                 "agent. Reply with the updated summary only: terse, factual, "
                                 "bullet points, no preamble.");
  Message user = Message::make(
      "user",
      std::string("PRIOR checkpoint (may be empty):\n") +
          (session->summary.empty() ? "(none)" : session->summary) +
          "\n\nConversation segment to fold in:\n" + segment +
          "\n\nWrite the consolidated summary that replaces both.\n"
          "- If the PRIOR checkpoint exists, do not copy it forward verbatim: keep facts that are "
          "still true, drop stale ones, merge what is new.\n"
          "- Preserve exact file paths, commands, error strings, identifiers and numbers.\n"
          "- Do not mention this summarisation request or that the context was compacted.");
  request.messages.push_back(system.to_wire(false));
  request.messages.push_back(user.to_wire(false));

  const ChatResult chat = llm_chat(request, DeltaCallback(), cancel);
  if (chat.cancelled) return false;
  if (!chat.ok || trim(chat.content).empty()) {
    LOG_WARN("compaction: summarisation failed (", chat.http_status, "): ",
             chat.error.empty() ? chat.detail : chat.error);
    return false;
  }

  session->summary = trim(chat.content);
  session->summary_upto = split;
  session->info.updated_at = now_iso8601();
  LOG_INFO("compaction: summary now covers ", split, "/", total, " messages (", session->summary.size(),
           " bytes), folded ", segment.size(), " bytes of transcript");
  workspace.log("INFO", "compaction: summary now covers " + std::to_string(split) + " messages");
  return true;
}

bool Agent::maybe_compact(Workspace& workspace, Session* session, const std::string& provider,
                          const std::string& model, const ModelInfo* model_info,
                          std::atomic<bool>* cancel, const EventEmitter& emit, bool force) {
  if (!session) return false;
  const Settings& st = workspace.settings();
  if (!st.enable_compaction) return false;
  if (cancel && cancel->load()) return false;

  std::vector<Message> probe;
  ContextStats stats;
  build_context(workspace, *session, model_info, &probe, &stats);

  // Trigger on pressure, not on the hard boundary: compacting at 100% of the
  // budget means the next turn is immediately over it again.
  if (!force && stats.live_used < stats.trigger) return false;
  if (stats.trigger <= 0) return false;

  const long long retain =
      static_cast<long long>(static_cast<double>(stats.budget) * st.compaction_retain_ratio);
  LOG_DEBUG("compaction: pressure live=", stats.live_used, " trigger=", stats.trigger,
            " retain=", retain, " force=", force ? 1 : 0);

  // dsh retries once and reports a real failure if it still does not fit, rather
  // than pretending a partial compaction was enough.
  const int max_attempts = force ? 1 : (st.compaction_retries > 0 ? st.compaction_retries + 1 : 1);
  bool changed = false;
  for (int attempt = 0; attempt < max_attempts; ++attempt) {
    if (cancel && cancel->load()) break;
    if (!compact_history(workspace, session, provider, model, model_info, cancel, retain)) break;
    changed = true;
    probe.clear();
    build_context(workspace, *session, model_info, &probe, &stats);
    if (stats.live_used < stats.trigger) break;
    if (attempt + 1 < max_attempts) {
      LOG_INFO("compaction: still above threshold (", stats.live_used, " >= ", stats.trigger,
               "); retrying");
    } else {
      LOG_WARN("compaction: still above threshold after ", attempt + 1,
               " attempt(s) (", stats.live_used, " >= ", stats.trigger,
               "); the retained tail alone may exceed it");
    }
  }

  if (!changed) return false;
  std::string save_err;
  if (!workspace.save_session(*session, &save_err)) {
    LOG_WARN("compaction: cannot persist the session: ", save_err);
  }
  if (emit) {
    nlohmann::json event = nlohmann::json::object();
    event["type"] = "compaction";
    event["covered_messages"] = session->summary_upto;
    event["total_messages"] = session->messages.size();
    event["summary_bytes"] = session->summary.size();
    event["live_tokens"] = static_cast<long long>(stats.live_used);
    event["trigger_tokens"] = static_cast<long long>(stats.trigger);
    emit(event);
  }
  return true;
}
TurnResult Agent::run_turn(Workspace& workspace, Session* session, const std::string& user_text,
                           const std::vector<ImageAttachment>& images,
                           const TurnOptions& options, const EventEmitter& emit,
                           std::atomic<bool>* cancel) {
  TurnResult out;
  const int64_t started = now_ms();

  // The file tools' boundary: the workspace root, plus whatever this turn was
  // granted. A Workspace is one turn's copy, so the grant does not outlive it.
  workspace.set_allowed_roots(options.allow_paths);

  const std::string provider =
      options.provider.empty() ? config_->active_provider : options.provider;
  std::string model = options.model.empty() ? config_->active_model : options.model;
  out.provider = provider;

  const ProviderInfo* provider_info = config_->provider(provider);
  if (!provider_info) {
    out.error = "provider not found: " + provider;
    return out;
  }
  const ModelInfo* model_info = provider_info->find_model(model);
  if (!model_info && !provider_info->models.empty()) {
    model = provider_info->primary_model();
    model_info = provider_info->find_model(model);
  }
  out.model = model;
  if (model_info && model_info->max_context_tokens > 0) {
    out.max_context_tokens = model_info->max_context_tokens;
  }

  if (!provider_info->has_key()) {
    out.error = "provider '" + provider + "' has no api_key configured";
    return out;
  }

  const Settings& st = workspace.settings();
  bool echo_reasoning = resolve_reasoning_echo(*config_, *provider_info);
  LOG_DEBUG("agent: turn start provider=", provider, " model=", model,
            " reasoning_echo=", echo_reasoning ? 1 : 0, " history=", session->messages.size());

  // --- persist the user message -------------------------------------------
  Message user_message = Message::make("user", user_text);
  const bool is_vision_model = model_info && model_info->supports_vision;
  if (!images.empty()) {
    if (!is_vision_model) {
      LOG_WARN("agent: ", images.size(), " image(s) dropped: model ", provider, "/", model,
               " does not declare supports_vision");
    } else {
      user_message.images = images;
    }
  }
  session->messages.push_back(user_message);
  // Which user turn this is (1-based): artifacts carry it, so the UI can show
  // "this turn's output" under the final answer instead of an ever-growing list.
  int turn_number = 0;
  for (std::size_t i = 0; i < session->messages.size(); ++i) {
    if (session->messages[i].role == "user") ++turn_number;
  }

  // Vision input is stored as file references and encoded to data: URLs once
  // per turn (every iteration re-sends the whole context; re-encoding a few
  // megabytes each time would be pure waste).
  //
  // Every image still in the context is encoded, not just this turn's: the
  // session keeps paths, so without this a picture from an earlier turn would
  // silently degrade to a text-only message and a follow-up question about it
  // ("what about the top-left corner?") would be answered blind. Images folded
  // away by compaction are skipped, and the total is capped so one very long
  // vision conversation cannot balloon each request.
  std::map<std::string, std::string> image_urls;
  if (is_vision_model) {
    std::size_t encoded_bytes = 0;
    bool over_budget = false;
    for (std::size_t m = session->summary_upto; m < session->messages.size(); ++m) {
      const std::vector<ImageAttachment>& atts = session->messages[m].images;
      for (std::size_t i = 0; i < atts.size(); ++i) {
        if (image_urls.find(atts[i].path) != image_urls.end()) continue;
        if (encoded_bytes >= kMaxEncodedImageBytesPerTurn) {
          over_budget = true;
          continue;
        }
        std::string bytes;
        const std::string absolute = workspace.resolve_path(atts[i].path, nullptr);
        if (absolute.empty() || !read_file(absolute, &bytes)) {
          LOG_WARN("agent: cannot read image attachment ", atts[i].path, "; skipping");
          continue;
        }
        image_urls[atts[i].path] =
            "data:" + atts[i].mime + ";base64," + base64_encode(bytes);
        encoded_bytes += bytes.size();
      }
    }
    if (over_budget) {
      LOG_WARN("agent: more than ", kMaxEncodedImageBytesPerTurn,
               " bytes of images in this context; the oldest ones are sent as text only");
    }
  }
  if (session->info.id.empty()) session->info.id = make_id("s_");
  if (session->info.title.empty() || session->info.title == "新会话") {
    session->info.title = Workspace::derive_title(user_text);
    // An image-only message would otherwise leave every such session titled
    // "新会话".
    if (session->info.title == "新会话" && !user_message.images.empty()) {
      session->info.title = "图片";
    }
  }
  session->info.updated_at = now_iso8601();
  workspace.save_session(*session, nullptr);

  // --- trajectory ----------------------------------------------------------
  // One machine-readable record per turn, written under
  // <run_dir>/trajectories/<session_id>.jsonl. The header line lands on disk
  // before the first model request and is refreshed after every step, so a
  // killed process still leaves a usable partial trajectory.
  TrajectoryRecorder trajectory;
  trajectory.begin(workspace, session->info.id, workspace.id(), provider, model);
  const EventEmitter emit_user = emit;
  // Compaction is the one step that does not pass through this function's own
  // bookkeeping (it runs inside maybe_compact, both before the loop and during
  // overflow recovery), so it is recorded where both paths meet: the event.
  EventEmitter emit_all = [&trajectory, emit_user](const nlohmann::json& event) {
    if (event.is_object() && event.value("type", std::string()) == "compaction") {
      trajectory.record_compaction(event.value("covered_messages", static_cast<long long>(0)),
                                   event.value("summary_bytes", static_cast<long long>(0)));
    }
    emit_user(event);
  };

  // Fold the oldest turns into a summary before the context overflows, so the
  // model keeps the thread of the conversation instead of silently losing it.
  // Runs before the start event so a compaction event lands first in the stream.
  if (st.enable_compaction) {
    maybe_compact(workspace, session, provider, model, model_info, cancel, emit_all);
  }

  // --- announce the turn ---------------------------------------------------
  nlohmann::json start_event = nlohmann::json::object();
  start_event["type"] = "start";
  start_event["session_id"] = session->info.id;
  start_event["workspace_id"] = workspace.id();
  start_event["provider"] = provider;
  start_event["model"] = model;
  // Where the persisted history ends and this turn begins. A client that
  // re-attaches mid-turn has already fetched the messages this turn saved after
  // its earlier iterations; the replay rebuilds them in the live bubble, so it
  // drops everything past this index first instead of showing them twice.
  start_event["messages_before"] = static_cast<long long>(session->messages.size());
  emit(start_event);

  // --- build the tool schema ----------------------------------------------
  // Billed web-tool calls are counted per turn, not per iteration: a model that
  // keeps retrying a failing search would otherwise run to max_iterations.
  int web_tool_calls = 0;
  // toolkit_search/toolkit_fetch only appear when this turn's provider
  // declares them; the underlying protocol is provider-specific.
  nlohmann::json tools_json = tools_->openai_tools(provider_info);
  // A caller that drives its own policy can hand the agent exactly the tools it
  // wants - or none at all (an empty list turns the turn into a plain
  // conversation with the model).
  if (!options.tools.empty()) {
    nlohmann::json kept = nlohmann::json::array();
    for (std::size_t i = 0; i < tools_json.size(); ++i) {
      const std::string name = tools_json[i]["function"]["name"].get<std::string>();
      bool wanted = false;
      for (std::size_t k = 0; k < options.tools.size(); ++k) {
        if (options.tools[k] == name) { wanted = true; break; }
      }
      if (wanted) kept.push_back(tools_json[i]);
    }
    tools_json = kept;
  }
  if (model_info && !model_info->supports_tools) tools_json = nlohmann::json::array();
  // toolkit_load_image is only meaningful for a model that can see: advertising
  // it to a text-only model would buy a tool call that can only fail.
  if (model_info && !model_info->supports_vision) {
    nlohmann::json kept = nlohmann::json::array();
    for (std::size_t i = 0; i < tools_json.size(); ++i) {
      if (tools_json[i]["function"]["name"].get<std::string>() == "toolkit_load_image") continue;
      kept.push_back(tools_json[i]);
    }
    tools_json = kept;
  }

  // Once the billed web-tool budget for this turn is spent, stop advertising
  // them: an error result alone invites the model to retry until
  // max_iterations, which is slow and (per call billing) expensive.
  nlohmann::json tools_json_no_web;
  auto tools_for_request = [&]() -> const nlohmann::json& {
    const int budget = st.web_tool_max_calls_per_turn;
    if (budget <= 0 || web_tool_calls < budget) return tools_json;
    if (tools_json_no_web.is_null()) {
      tools_json_no_web = nlohmann::json::array();
      for (std::size_t i = 0; i < tools_json.size(); ++i) {
        const nlohmann::json& t = tools_json[i];
        const std::string name = t["function"]["name"].get<std::string>();
        if (name == "toolkit_search" || name == "toolkit_fetch") continue;
        tools_json_no_web.push_back(t);
      }
    }
    return tools_json_no_web;
  };

  const int max_iterations = st.max_iterations;
  bool finished = false;
  // Per-turn bookkeeping for toolkit_load_image: the collected attachments and
  // the budget that keeps one turn from filling the context with pictures.
  std::vector<ImageAttachment> tool_images;
  std::size_t tool_images_attached = 0;

  // Take the oldest prompt the user queued while this turn was running and
  // append it as a normal user message. Doing it here (rather than in the HTTP
  // layer) means the adopted prompt lands in the same context/session/trajectory
  // as everything else, and the model sees it together with all the tool output
  // that happened since the previous round.
  auto adopt_queued = [&](int iteration) -> bool {
    if (!options.inbox) return false;
    for (;;) {
      QueuedPrompt queued;
      if (!options.inbox->pop(&queued)) return false;
      const std::string text = trim(queued.text);
      if (text.empty()) continue;  // junk cannot happen via the API, but stay safe
      Message message = Message::make("user", text);
      message.created_at = queued.created_at.empty() ? now_iso8601() : queued.created_at;
      session->messages.push_back(message);
      session->info.updated_at = now_iso8601();
      workspace.save_session(*session, nullptr);

      nlohmann::json event = nlohmann::json::object();
      event["type"] = "queue_adopted";
      event["id"] = queued.id;
      event["content"] = text;
      event["iteration"] = iteration;
      event["pending"] = static_cast<int>(options.inbox->size());
      event["created_at"] = message.created_at;
      emit(event);
      LOG_INFO("queue: adopted prompt ", queued.id, " at iteration ", iteration,
               " (", options.inbox->size(), " still queued)");
      workspace.log("info", "queue: adopted a queued prompt at iteration " +
                                std::to_string(iteration));
      return true;
    }
  };

  for (int iteration = 0; iteration < max_iterations; ++iteration) {
    if (cancel && cancel->load()) {
      out.cancelled = true;
      break;
    }
    // Iteration 0 is the request the caller just started; a prompt queued in the
    // tiny window before the worker picked it up belongs to the *next* round, so
    // we only adopt from iteration 1 onwards.
    if (iteration > 0) adopt_queued(iteration);
    out.iterations = iteration + 1;
    // Wall-clock start of this iteration; the trajectory reports the whole
    // iteration (including a retry after a provider disagreement) rather than
    // just the last HTTP round trip.
    const int64_t iteration_started = now_ms();

    std::vector<Message> context;
    ContextStats ctx_stats;
    build_context(workspace, *session, model_info, &context, &ctx_stats, options.system_prompt,
                  options.task_context);

    ChatRequest request;
    request.provider = provider;
    request.model = model;
    request.api_url = provider_info->api_url;
    request.api_key = provider_info->api_key;
    request.tools = tools_for_request();
    request.stream = options.stream;

    // Per-provider headers, with ${session_id} substituted.
    request.headers = provider_info->headers;
    for (std::size_t i = 0; i < request.headers.size(); ++i) {
      request.headers[i].second =
          replace_all(request.headers[i].second, "${session_id}", session->info.id);
    }
    // opencode.ai gateways need a routing session id. Supply one automatically
    // so an unmodified shared provider.yaml works as-is.
    {
      bool has_opencode_session = false;
      for (std::size_t i = 0; i < request.headers.size(); ++i) {
        if (to_lower(request.headers[i].first) == "x-opencode-session") {
          has_opencode_session = true;
        }
      }
      const ParsedUrl parsed = parse_url(provider_info->api_url);
      if (!has_opencode_session && parsed.valid &&
          ends_with(to_lower(parsed.host), "opencode.ai")) {
        request.headers.push_back(
            std::make_pair(std::string("x-opencode-session"), session->info.id));
      }
    }
    // Token usage in a streaming response is opt-in per gateway
    // (`stream_options.include_usage`). The provider block may override the
    // global setting, and a gateway that answered 400 to the field is
    // remembered so we stop sending it.
    request.include_usage = st.stream_include_usage;
    if (provider_info->stream_include_usage >= 0) {
      request.include_usage = provider_info->stream_include_usage != 0;
    }
    bool usage_flag_supported = true;
    if (config_->lookup_usage_flag_supported(provider, &usage_flag_supported) &&
        !usage_flag_supported) {
      request.include_usage = false;
    }
    request.send_reasoning_effort = false;
    request.reasoning_effort =
        model_info ? model_info->reasoning_effort : st.reasoning_effort;
    request.connect_timeout_ms = st.http_connect_timeout_ms;
    request.read_timeout_ms = st.http_read_timeout_ms;

    if (st.max_tokens > 0) {
      request.max_tokens = st.max_tokens;
    } else if (model_info && model_info->max_output_tokens > 0) {
      request.max_tokens = model_info->max_output_tokens;
    }

    if (st.temperature >= 0) {
      request.has_temperature = true;
      request.temperature = st.temperature;
    } else if (model_info && model_info->has_temperature) {
      request.has_temperature = true;
      request.temperature = model_info->temperature;
    }
    if (st.top_p >= 0) {
      request.has_top_p = true;
      request.top_p = st.top_p;
    } else if (model_info && model_info->has_top_p) {
      request.has_top_p = true;
      request.top_p = model_info->top_p;
    }

    DeltaCallback on_delta = [&emit](DeltaKind kind, const std::string& text) {
      nlohmann::json event = nlohmann::json::object();
      event["type"] = (kind == DeltaKind::Reasoning)
                          ? "reasoning_delta"
                          : (kind == DeltaKind::ToolArgs ? "tool_args_delta" : "content_delta");
      event["text"] = text;
      emit(event);
    };

    // Thinking-mode gateways require the previous assistant reasoning_content on
    // the follow-up request; api.deepseek.com rejects it. The provider config
    // sets the initial policy, and if the provider disagrees we flip once,
    // remember the answer for the rest of the process, and retry.
    ChatResult chat;
    bool overflow_recovered = false;
    for (int attempt = 0;; ++attempt) {
      nlohmann::json wire_messages = nlohmann::json::array();
      for (std::size_t i = 0; i < context.size(); ++i) {
        wire_messages.push_back(context[i].to_wire(echo_reasoning, &image_urls));
      }
      request.messages = wire_messages;

      chat = llm_chat(request, options.stream ? on_delta : DeltaCallback(), cancel);
      LOG_DEBUG("agent: iteration ", iteration + 1, " chat done ok=", chat.ok ? 1 : 0,
                " status=", chat.http_status);

      // A gateway that does not know `stream_options` answers 400 and says so.
      // Drop the field, remember that for this provider, and try again - one
      // wasted round trip beats failing every turn.
      if (chat.http_status == 400 && request.include_usage &&
          rejects_stream_options(chat.error + " " + chat.detail)) {
        request.include_usage = false;
        config_->remember_usage_flag_supported(provider, false);
        LOG_WARN("llm: provider '", provider,
                 "' rejected stream_options.include_usage; not sending it again (no "
                 "provider token usage, the metrics fall back to local estimates)");
        continue;
      }

      const bool produced_output =
          !chat.content.empty() || !chat.reasoning.empty() || !chat.tool_calls.empty();
      if (chat.ok || chat.cancelled) break;

      const std::string failure = to_lower(chat.error + " " + chat.detail);

      // dsh's overflow trigger: when the provider says the request itself is too
      // long, the pressure threshold was wrong (bad estimate, huge tool output or
      // a concurrent growth), so compact below it and retry once.
      if (!overflow_recovered && !produced_output &&
          (chat.http_status == 400 || chat.http_status == 413) &&
          looks_like_context_overflow(failure)) {
        overflow_recovered = true;
        LOG_WARN("llm: provider reported a context overflow; compacting and retrying");
        if (maybe_compact(workspace, session, provider, model, model_info, cancel, emit_all, true)) {
          build_context(workspace, *session, model_info, &context, &ctx_stats,
                      options.system_prompt, options.task_context);
          continue;
        }
        break;
      }

      if (attempt >= 1 || produced_output) break;
      if (chat.http_status != 400 || !contains(failure, "reasoning_content")) break;

      echo_reasoning = !echo_reasoning;
      config_->remember_reasoning_echo(provider_info->name, echo_reasoning);
      LOG_WARN("llm: provider disagreed about reasoning_content; retrying with echo=",
               echo_reasoning ? "on" : "off");
    }

    if (chat.cancelled) {
      out.cancelled = true;
      break;
    }
    if (!chat.ok) {
      out.error = chat.error.empty() ? "the model request failed" : chat.error;
      out.detail = chat.detail;
      nlohmann::json event = nlohmann::json::object();
      event["type"] = "error";
      event["message"] = out.error;
      if (!out.detail.empty()) event["detail"] = out.detail;
      emit(event);
      break;
    }

    accumulate_usage(&out.usage, chat.usage);
    if (!chat.content.empty()) out.content = chat.content;

    // --- metrics for the UI --------------------------------------------------
    // Provider-reported usage wins. Most gateways only report usage when
    // stream_options.include_usage is on, so without it we fall back to the local
    // estimator and flag the numbers as estimates rather than showing nothing.
    const bool has_usage = chat.usage.present && chat.usage.prompt_tokens > 0;
    const long long iter_gen_ms = effective_generation_ms(chat);
    const long long iter_output =
        has_usage ? chat.usage.completion_tokens
                  : Agent::estimate_tokens(chat.content) + Agent::estimate_tokens(chat.reasoning) +
                        estimate_tool_call_tokens(chat.tool_calls);
    const long long iter_context = has_usage ? chat.usage.prompt_tokens : ctx_stats.used;
    if (!has_usage) out.usage_estimated = true;
    if (iter_context > out.context_tokens) out.context_tokens = iter_context;
    out.output_tokens += iter_output;
    out.generation_ms += iter_gen_ms;
    if (out.ttft_ms == 0) out.ttft_ms = chat.ttft_ms;

    // Trajectory: what this iteration produced and what it cost. `has_usage`
    // decides whether the token counts are provider facts or local estimates.
    trajectory.record_model(iteration_started, chat.reasoning, chat.content, chat.tool_calls,
                            chat.usage, !has_usage, chat.ttft_ms);

    Message assistant;
    assistant.role = "assistant";
    assistant.content = chat.content;
    assistant.reasoning = chat.reasoning;
    assistant.tool_calls = chat.tool_calls;
    assistant.created_at = now_iso8601();
    session->messages.push_back(assistant);
    session->info.updated_at = assistant.created_at;

    nlohmann::json iteration_event = nlohmann::json::object();
    iteration_event["type"] = "iteration_end";
    iteration_event["iteration"] = iteration;
    iteration_event["has_tool_calls"] = !chat.tool_calls.empty();
    iteration_event["usage"] = usage_json(chat.usage);
    iteration_event["metrics"] =
        metrics_json(out.max_context_tokens, iter_context, iter_output, chat.ttft_ms, iter_gen_ms,
                     !has_usage, chat.elapsed_ms);
    emit(iteration_event);

    if (chat.tool_calls.empty()) {
      // The model is done, but a prompt queued while it was answering must not
      // wait for a whole new HTTP turn: continue the loop and adopt it at the
      // top of the next iteration. When the iteration budget is spent we stop
      // and report the leftovers instead.
      if (options.inbox && iteration + 1 < max_iterations && !options.inbox->empty()) {
        LOG_DEBUG("queue: model finished but a prompt is queued; continuing");
        continue;
      }
      finished = true;
      break;
    }

    // Announce every tool call before running any of them.
    for (std::size_t i = 0; i < chat.tool_calls.size(); ++i) {
      nlohmann::json event = nlohmann::json::object();
      event["type"] = "tool_call";
      event["id"] = chat.tool_calls[i].id;
      event["name"] = chat.tool_calls[i].name;
      event["arguments"] = chat.tool_calls[i].arguments;
      event["index"] = static_cast<int>(i);
      emit(event);
    }

    for (std::size_t i = 0; i < chat.tool_calls.size(); ++i) {
      const ToolCall& call = chat.tool_calls[i];

      nlohmann::json start_ev = nlohmann::json::object();
      start_ev["type"] = "tool_start";
      start_ev["id"] = call.id;
      start_ev["name"] = call.name;
      start_ev["arguments"] = call.arguments;
      emit(start_ev);

      Message tool_message;
      tool_message.role = "tool";
      tool_message.tool_call_id = call.id;
      tool_message.name = call.name;
      tool_message.created_at = now_iso8601();

      const int64_t tool_started = now_ms();
      ToolResult tool_result;

      // Re-parse the raw arguments so a malformed payload is reported back to
      // the model instead of silently becoming an empty argument object.
      bool arguments_valid = true;
      nlohmann::json parsed_arguments = call.arguments;
      if (!call.raw_arguments.empty()) {
        try {
          parsed_arguments = nlohmann::json::parse(call.raw_arguments);
        } catch (const std::exception& e) {
          arguments_valid = false;
          tool_result.ok = false;
          tool_result.output = std::string("error: arguments are not valid JSON (") + e.what() +
                               "). Raw arguments:\n" + truncate_utf8(call.raw_arguments, 2000);
        }
      }

      if (arguments_valid && cancel && cancel->load()) {
        out.cancelled = true;
        tool_result.ok = false;
        tool_result.output = "cancelled by user";
      } else if (arguments_valid) {
        ToolContext tool_ctx;
        tool_ctx.config = config_;
        tool_ctx.provider = provider_info;
        tool_ctx.model = model_info;
        tool_ctx.workspace = &workspace;
        tool_ctx.session = session;
        tool_ctx.cancel = cancel;
        tool_ctx.web_tool_calls = &web_tool_calls;
        if (!options.task_id.empty()) tool_ctx.task_id = &options.task_id;
        tool_result = tools_->execute(call.name, parsed_arguments, tool_ctx);
      }

      // Images the tool produced: encode once, then hand them to the follow-up
      // user message below. A model without vision keeps the text result but
      // never gets an image it cannot read.
      for (std::size_t k = 0; k < tool_result.images.size(); ++k) {
        const ImageAttachment& att = tool_result.images[k];
        if (tool_images_attached >= kMaxToolImagesPerTurn) {
          tool_result.output +=
              "\n(toolkit_load_image: per-turn image limit reached; this image was not attached. "
              "Look at it in a later turn or downscale/combine the images.)";
          LOG_WARN("agent: toolkit_load_image per-turn limit reached");
          continue;
        }
        if (!model_info || !model_info->supports_vision) {
          tool_result.output +=
              "\n(the active model does not declare vision support, so the image was not "
              "attached)";
          continue;
        }
        std::string bytes;
        const std::string absolute = workspace.resolve_path(att.path, nullptr);
        if (absolute.empty() || !read_file(absolute, &bytes)) {
          tool_result.output += "\n(cannot read the stored image back; not attached)";
          LOG_WARN("agent: cannot read stored tool image ", att.path);
          continue;
        }
        image_urls[att.path] = "data:" + att.mime + ";base64," + base64_encode(bytes);
        tool_images.push_back(att);
        ++tool_images_attached;
      }

      // Files this call produced. toolkit_file/toolkit_load_image report the
      // path themselves; a command cannot, so its output window is scanned for
      // files with an interesting extension (bounded, noise dirs skipped).
      {
        bool changed = false;
        bool scan_truncated = false;
        const nlohmann::json& meta = tool_result.meta;
        if (tool_result.ok && meta.is_object()) {
          const std::string file_op =
              meta.contains("file_op") && meta["file_op"].is_string()
                  ? meta["file_op"].get<std::string>()
                  : std::string();
          const std::string reported =
              meta.contains("path") && meta["path"].is_string()
                  ? meta["path"].get<std::string>()
                  : std::string();
          // toolkit_load_image copies into .tea_agent_run/artifacts, which
          // record_artifact filters out on purpose: the user already has the
          // original, and offering an internal copy as "produced" is noise.
          if (!reported.empty() && (file_op == "write" || file_op == "append")) {
            changed |= workspace.record_artifact(session, reported, call.name, turn_number);
          }
          if (file_op == "move" && meta.contains("dest") && meta["dest"].is_string()) {
            changed |= workspace.record_artifact(
                session, meta["dest"].get<std::string>(), call.name, turn_number);
          }
          const std::string command =
              meta.contains("command") && meta["command"].is_string()
                  ? meta["command"].get<std::string>()
                  : std::string();
          if (!command.empty()) {
            const std::vector<std::string> touched =
                workspace.recent_artifacts(tool_started, &scan_truncated);
            for (std::size_t k = 0; k < touched.size(); ++k) {
              changed |= workspace.record_artifact(session, touched[k], call.name, turn_number);
            }
            if (scan_truncated) {
              // Say so: a silent cap would look like "that was all of them".
              tool_result.output +=
                  "\n(more files were produced than the list can show; the count above is "
                  "capped)";
            }
          }
        }
        if (changed) {
          nlohmann::json arts = nlohmann::json::array();
          for (std::size_t k = 0; k < session->artifacts.size(); ++k) {
            arts.push_back(session->artifacts[k].to_json());
          }
          nlohmann::json art_event = nlohmann::json::object();
          art_event["type"] = "artifacts";
          art_event["iteration"] = iteration;
          art_event["turn"] = turn_number;
          art_event["items"] = arts;
          art_event["truncated"] = scan_truncated;
          emit(art_event);
        }
      }

      // A changed task list is user-visible state, not just tool output: persist
      // it now (a long iteration must not lose it) and push it to every attached
      // client so progress updates while the turn is still running.
      if (tool_result.todos_changed) {
        nlohmann::json todo_event = nlohmann::json::object();
        todo_event["type"] = "todo";
        todo_event["iteration"] = iteration;
        int total = 0, done = 0;
        todo_counts(session->todos, &total, &done);
        todo_event["total"] = total;
        todo_event["done"] = done;
        nlohmann::json items = nlohmann::json::array();
        for (std::size_t k = 0; k < session->todos.size(); ++k) {
          items.push_back(session->todos[k].to_json());
        }
        todo_event["items"] = items;
        emit(todo_event);
        // The list has its own write path: the session save would write this
        // turn's (possibly older) copy of it over the file.
        std::string todo_save_err;
        if (!workspace.save_todos(session->info.id, session->todos, &todo_save_err)) {
          LOG_WARN("todo: cannot persist the task list: ", todo_save_err);
        }
      }

      tool_message.content = tool_result.output;
      tool_message.ok = tool_result.ok;
      tool_message.truncated = tool_result.truncated;
      tool_message.elapsed_ms = now_ms() - tool_started;
      session->messages.push_back(tool_message);

      nlohmann::json result_event = nlohmann::json::object();
      result_event["type"] = "tool_result";
      result_event["id"] = call.id;
      result_event["name"] = call.name;
      result_event["ok"] = tool_result.ok;
      result_event["elapsed_ms"] = tool_message.elapsed_ms;
      result_event["output"] = tool_result.output;
      result_event["truncated"] = tool_result.truncated;
      emit(result_event);

      // Trajectory: the raw arguments the model emitted plus what the tool
      // actually reported (paths, byte counts, exit code, filter decisions).
      trajectory.record_tool(call.name,
                             call.raw_arguments.empty() ? dump_json(call.arguments)
                                                        : call.raw_arguments,
                             tool_result, tool_message.elapsed_ms);

      if (out.cancelled) break;
    }

    // Images a tool loaded (toolkit_load_image) cannot ride on the tool result:
    // the wire format only carries images on user messages. Attach them as one
    // follow-up user message, after every tool result of this iteration, so the
    // next request sees "here is the picture you asked for" in the only place
    // providers accept it. `image_urls` gets the data: URLs here (and only
    // here) because the map is built from the user turn at the start of the run
    // and each image is encoded exactly once.
    if (!out.cancelled && !tool_images.empty()) {
      std::string text = "Images loaded by toolkit_load_image (attached so you can see them):";
      for (std::size_t i = 0; i < tool_images.size(); ++i) {
        text += "\n- " + tool_images[i].path + " (" + tool_images[i].mime + ")";
      }
      Message attach = Message::make("user", text);
      attach.created_at = now_iso8601();
      attach.images = tool_images;
      session->messages.push_back(attach);

      nlohmann::json event = nlohmann::json::object();
      event["type"] = "tool_images";
      event["iteration"] = iteration;
      event["content"] = attach.content;
      nlohmann::json list = nlohmann::json::array();
      for (std::size_t i = 0; i < tool_images.size(); ++i) {
        list.push_back({{"path", tool_images[i].path}, {"mime", tool_images[i].mime}});
      }
      event["images"] = list;
      emit(event);
      LOG_INFO("agent: attached ", tool_images.size(), " tool image(s) to the conversation");
      workspace.log("info", "toolkit_load_image: attached " + std::to_string(tool_images.size()) +
                                " image(s)");
      tool_images.clear();
    }

    session->info.updated_at = now_iso8601();
    workspace.save_session(*session, nullptr);
    workspace.log("info", "iteration " + std::to_string(iteration + 1) + " model=" + model +
                              " tool_calls=" + std::to_string(chat.tool_calls.size()));

    if (out.cancelled) break;
    if (iteration == max_iterations - 1) out.hit_iteration_limit = true;
  }

  // Anything still queued was never adopted: the turn was stopped, failed or hit
  // the iteration cap. Hand it back so the caller can restore the text instead of
  // dropping what the user typed.
  if (options.inbox) {
    std::vector<QueuedPrompt> leftovers = options.inbox->drain();
    if (!leftovers.empty()) {
      nlohmann::json items = nlohmann::json::array();
      for (std::size_t i = 0; i < leftovers.size(); ++i) {
        nlohmann::json item = nlohmann::json::object();
        item["id"] = leftovers[i].id;
        item["content"] = leftovers[i].text;
        item["created_at"] = leftovers[i].created_at;
        items.push_back(item);
      }
      nlohmann::json event = nlohmann::json::object();
      event["type"] = "queue_unconsumed";
      event["items"] = items;
      emit(event);
      LOG_INFO("queue: ", leftovers.size(), " queued prompt(s) not adopted (turn ended)");
    }
  }

  out.ok = out.error.empty() && !out.cancelled;
  out.elapsed_ms = now_ms() - started;

  session->info.updated_at = now_iso8601();
  workspace.save_session(*session, nullptr);

  // Close the trajectory (stamps finished_at/ok/usage, appends the final line
  // and applies retention) before the done event announces its id.
  trajectory.finish(out.ok, out.error, out.detail, out.iterations, out.usage,
                    out.usage_estimated);


  if (out.cancelled) {
    nlohmann::json event = nlohmann::json::object();
    event["type"] = "stopped";
    emit(event);
  } else if (out.ok) {
    nlohmann::json event = nlohmann::json::object();
    event["type"] = "done";
    event["iterations"] = out.iterations;
    event["elapsed_ms"] = out.elapsed_ms;
    event["usage"] = usage_json(out.usage);
    event["metrics"] = metrics_json(out.max_context_tokens, out.context_tokens, out.output_tokens,
                                    out.ttft_ms, out.generation_ms, out.usage_estimated,
                                    out.elapsed_ms);
    event["content"] = out.content;
    // The caller learns the audit-trail id without polling; the full record is
    // at GET /api/turns/<trajectory_id>/trajectory.
    if (trajectory.active()) event["trajectory_id"] = trajectory.turn_id();
    emit(event);
  }
  (void)finished;

  workspace.log(out.ok ? "info" : "warn",
                std::string("turn finished ok=") + (out.ok ? "true" : "false") +
                    " iterations=" + std::to_string(out.iterations) +
                    " elapsed_ms=" + std::to_string(out.elapsed_ms) +
                    " output_tokens=" + std::to_string(out.output_tokens) +
                    " ttft_ms=" + std::to_string(out.ttft_ms) +
                    " gen_ms=" + std::to_string(out.generation_ms) +
                    " context=" + std::to_string(out.context_tokens) + "/" +
                    std::to_string(out.max_context_tokens) +
                    (out.usage_estimated ? " (token counts estimated)" : ""));
  return out;
}

}  // namespace tea
