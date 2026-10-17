#include "agent.h"

#include <sstream>

#include "command.h"
#include "env.h"
#include "http.h"
#include "log.h"
#include "util.h"
#include "version.h"

namespace tea {
namespace {

void accumulate_usage(ChatUsage* total, const ChatUsage& part) {
  if (!part.present) return;
  total->present = true;
  total->prompt_tokens += part.prompt_tokens;
  total->completion_tokens += part.completion_tokens;
  total->total_tokens += part.total_tokens;
  total->raw = part.raw;
}

nlohmann::json usage_json(const ChatUsage& usage) {
  if (!usage.present) {
    nlohmann::json j = nlohmann::json::object();
    j["prompt_tokens"] = 0;
    j["completion_tokens"] = 0;
    j["total_tokens"] = 0;
    return j;
  }
  return usage.to_json();
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

std::string Agent::build_system_prompt(const Workspace& workspace, const Session& session) const {
  const EnvironmentInfo& env = machine_environment();
  const Settings& st = workspace.settings();
  std::ostringstream out;
  out << config_->default_system_prompt() << "\n\n";

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
  if (!st.allow_outside_workspace) {
    out << "- File tools are restricted to the workspace root.\n";
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
  return out.str();
}
namespace {

/// Bytes and tokens for one message, including its tool-call payload.
long long message_cost(const Message& m) {
  long long cost = Agent::estimate_tokens(m.content) + Agent::estimate_tokens(m.reasoning) / 2 + 8;
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
  std::ostringstream out;
  out << text.substr(0, head) << "\n... [tea_agent truncated " << (text.size() - head - tail)
      << " bytes of a single oversized message] ...\n"
      << text.substr(text.size() - tail);
  return out.str();
}

}  // namespace

void Agent::build_context(const Workspace& workspace, const Session& session,
                          const ModelInfo* model, std::vector<Message>* out,
                          ContextStats* stats) const {
  const Settings& st = workspace.settings();
  out->clear();
  Message system;
  system.role = "system";
  system.content = build_system_prompt(workspace, session);
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

  const long long tool_schema_cost = 480;  // 3 tool schemas, measured on the wire
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

  LOG_DEBUG("context: max=", max_context, " reserve_output=", max_output, " fixed=", fixed,
            " budget=", budget, " kept=", tail.size(), "/", session.messages.size(),
            " used=", used, " keep_turns=", st.keep_turns,
            session.summary_upto ? " summarised=" : "", session.summary_upto);
  if (stats) {
    stats->kept = tail.size();
    stats->total = session.messages.size();
    stats->budget = budget;
    stats->used = used;
    stats->trimmed = tail.size() < (session.messages.size() - first);
  }
  for (std::size_t i = 0; i < tail.size(); ++i) out->push_back(tail[i]);
}
bool Agent::compact_history(Workspace& workspace, Session* session, const std::string& provider,
                            const std::string& model, const ModelInfo* model_info,
                            std::atomic<bool>* cancel) {
  if (!session) return false;
  const Settings& st = workspace.settings();
  const std::size_t total = session->messages.size();
  const std::size_t keep =
      st.compaction_keep_recent > 0 ? static_cast<std::size_t>(st.compaction_keep_recent) : 4;
  if (total <= keep + 2) return false;

  // Fold everything except the most recent messages into the summary.
  std::size_t split = total - keep;
  // Prefer to cut right before a user message, so the kept window starts a turn.
  while (split > session->summary_upto && session->messages[split - 1].role != "user") --split;
  if (split <= session->summary_upto) split = total - keep;
  if (split <= session->summary_upto) return false;

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
      "user", std::string("Existing summary (may be empty):\n") +
                  (session->summary.empty() ? "(none)" : session->summary) +
                  "\n\nConversation segment to fold in:\n" + segment +
                  "\n\nWrite the updated summary that replaces both.");
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
                          std::atomic<bool>* cancel, const EventEmitter& emit) {
  if (!session) return false;
  const Settings& st = workspace.settings();
  if (!st.enable_compaction) return false;
  if (cancel && cancel->load()) return false;

  std::vector<Message> probe;
  ContextStats stats;
  build_context(workspace, *session, model_info, &probe, &stats);
  if (!stats.trimmed) return false;

  if (!compact_history(workspace, session, provider, model, model_info, cancel)) return false;
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
    emit(event);
  }
  return true;
}
TurnResult Agent::run_turn(Workspace& workspace, Session* session, const std::string& user_text,
                           const TurnOptions& options, const EventEmitter& emit,
                           std::atomic<bool>* cancel) {
  TurnResult out;
  const int64_t started = now_ms();

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
    model = provider_info->default_model.empty() ? provider_info->models.front()
                                                 : provider_info->default_model;
    model_info = provider_info->find_model(model);
  }
  out.model = model;

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
  session->messages.push_back(user_message);
  if (session->info.id.empty()) session->info.id = make_id("s_");
  if (session->info.title.empty() || session->info.title == "新会话") {
    session->info.title = Workspace::derive_title(user_text);
  }
  session->info.updated_at = now_iso8601();
  workspace.save_session(*session, nullptr);

  // Fold the oldest turns into a summary before the context overflows, so the
  // model keeps the thread of the conversation instead of silently losing it.
  // Runs before the start event so a compaction event lands first in the stream.
  if (st.enable_compaction) {
    maybe_compact(workspace, session, provider, model, model_info, cancel, emit);
  }

  // --- announce the turn ---------------------------------------------------
  nlohmann::json start_event = nlohmann::json::object();
  start_event["type"] = "start";
  start_event["session_id"] = session->info.id;
  start_event["workspace_id"] = workspace.id();
  start_event["provider"] = provider;
  start_event["model"] = model;
  emit(start_event);

  // --- build the tool schema ----------------------------------------------
  nlohmann::json tools_json = tools_->openai_tools();
  if (model_info && !model_info->supports_tools) tools_json = nlohmann::json::array();

  const int max_iterations = st.max_iterations;
  bool finished = false;

  for (int iteration = 0; iteration < max_iterations; ++iteration) {
    if (cancel && cancel->load()) {
      out.cancelled = true;
      break;
    }
    out.iterations = iteration + 1;

    std::vector<Message> context;
    build_context(workspace, *session, model_info, &context);

    ChatRequest request;
    request.provider = provider;
    request.model = model;
    request.api_url = provider_info->api_url;
    request.api_key = provider_info->api_key;
    request.tools = tools_json;
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
    request.include_usage = st.stream_include_usage;
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
      event["type"] = (kind == DeltaKind::Reasoning) ? "reasoning_delta" : "content_delta";
      event["text"] = text;
      emit(event);
    };

    // Thinking-mode gateways require the previous assistant reasoning_content on
    // the follow-up request; api.deepseek.com rejects it. The provider config
    // sets the initial policy, and if the provider disagrees we flip once,
    // remember the answer for the rest of the process, and retry.
    ChatResult chat;
    for (int attempt = 0;; ++attempt) {
      nlohmann::json wire_messages = nlohmann::json::array();
      for (std::size_t i = 0; i < context.size(); ++i) {
        wire_messages.push_back(context[i].to_wire(echo_reasoning));
      }
      request.messages = wire_messages;

      chat = llm_chat(request, options.stream ? on_delta : DeltaCallback(), cancel);
      LOG_DEBUG("agent: iteration ", iteration + 1, " chat done ok=", chat.ok ? 1 : 0,
                " status=", chat.http_status);

      const bool produced_output =
          !chat.content.empty() || !chat.reasoning.empty() || !chat.tool_calls.empty();
      if (chat.ok || chat.cancelled || attempt >= 1) break;
      if (chat.http_status != 400 || produced_output) break;
      if (!contains(to_lower(chat.error + " " + chat.detail), "reasoning_content")) break;

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
    emit(iteration_event);

    if (chat.tool_calls.empty()) {
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
        tool_ctx.workspace = &workspace;
        tool_ctx.cancel = cancel;
        tool_result = tools_->execute(call.name, parsed_arguments, tool_ctx);
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

      if (out.cancelled) break;
    }

    session->info.updated_at = now_iso8601();
    workspace.save_session(*session, nullptr);
    workspace.log("info", "iteration " + std::to_string(iteration + 1) + " model=" + model +
                              " tool_calls=" + std::to_string(chat.tool_calls.size()));

    if (out.cancelled) break;
    if (iteration == max_iterations - 1) out.hit_iteration_limit = true;
  }

  out.ok = out.error.empty() && !out.cancelled;
  out.elapsed_ms = now_ms() - started;
  session->info.updated_at = now_iso8601();
  workspace.save_session(*session, nullptr);

  // Fold the oldest turns into a summary before the context overflows, so the
  // model keeps the thread of the conversation instead of silently losing it.
  // Runs before the start event so a compaction event lands first in the stream.
  if (st.enable_compaction) {
    maybe_compact(workspace, session, provider, model, model_info, cancel, emit);
  }

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
    event["content"] = out.content;
    emit(event);
  }
  (void)finished;

  workspace.log(out.ok ? "info" : "warn",
                std::string("turn finished ok=") + (out.ok ? "true" : "false") +
                    " iterations=" + std::to_string(out.iterations) +
                    " elapsed_ms=" + std::to_string(out.elapsed_ms));
  return out;
}

}  // namespace tea
