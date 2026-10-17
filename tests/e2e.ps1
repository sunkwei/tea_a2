# tests/e2e.ps1 -- end-to-end test for tea_agent.
#
# Starts tools/mock_llm_server.mjs, starts the agent against it, and drives a
# full multi-turn tool-calling conversation over the HTTP API. Everything runs
# on 127.0.0.1 over plain http, so it works without any API key or TLS.
#
#   pwsh -File tests/e2e.ps1
#   pwsh -File tests/e2e.ps1 -Exe build-gcc/bin/tea_agent.exe
#
# Exits 0 when every check passes.

[CmdletBinding()]
param(
    [string]$Exe = "",
    [int]$AgentPort = 8399,
    [int]$MockPort = 8790
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrEmpty($Exe)) {
    foreach ($candidate in @(
            (Join-Path $root 'build-gcc\bin\tea_agent.exe'),
            (Join-Path $root 'build\bin\tea_agent.exe'),
            (Join-Path $root 'build-mingw\bin\tea_agent.exe'))) {
        if (Test-Path $candidate) { $Exe = $candidate; break }
    }
}
if (-not (Test-Path $Exe)) {
    Write-Host "FAIL: tea_agent binary not found; build it first (scripts\build-mingw.bat)" -ForegroundColor Red
    exit 2
}

$scratch = Join-Path $PSScriptRoot '.scratch'
if (Test-Path $scratch) { Remove-Item -Recurse -Force $scratch }
$homeDir = Join-Path $scratch 'home'
$wsDir = Join-Path $scratch 'workspace'
New-Item -ItemType Directory -Force -Path $homeDir, $wsDir | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'fixtures\provider.yaml') (Join-Path $homeDir 'provider.yaml')

$failures = @()
$checks = 0

function Check([string]$name, [bool]$ok, [string]$detail = "") {
    $script:checks++
    if ($ok) {
        Write-Host ("  [ok]   {0}" -f $name) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0} {1}" -f $name, $detail) -ForegroundColor Red
        $script:failures += $name
    }
}

# Runs one /api/chat turn and returns the parsed SSE events. Used by the checks
# that need a specific provider or a specific session.
function Invoke-ChatTurn([string]$provider, [string]$sessionId, [string]$message) {
    $bodyFile = Join-Path $scratch ("chat-" + [guid]::NewGuid().ToString('N') + ".json")
    (@{
            workspace_id = $wsId
            session_id   = $sessionId
            provider     = $provider
            message      = $message
        } | ConvertTo-Json -Compress) | Set-Content -Path $bodyFile -Encoding UTF8
    $raw = & curl.exe -N -s --max-time 180 -X POST -H "Content-Type: application/json" `
        --data-binary "@$bodyFile" "$base/api/chat" 2>&1 | Out-String
    $out = @()
    foreach ($line in ($raw -split "`n")) {
        $t = $line.Trim()
        if (-not $t.StartsWith('data:')) { continue }
        $payload = $t.Substring(5).Trim()
        if ($payload -and $payload -ne '[DONE]') {
            try { $out += ($payload | ConvertFrom-Json) } catch { }
        }
    }
    return $out
}

function Wait-Port([int]$port, [int]$timeoutSec = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect('127.0.0.1', $port)
            $c.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    return $false
}

# A leftover listener on one of our ports used to make this script silently talk
# to a stale instance (and hang). Refuse to run instead.
function Assert-PortFree([int]$port, [string]$what) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        $owners = ($busy | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique) -join ', '
        throw "$what port $port is already in use (pid $owners). Stop it or pass another port."
    }
}

$mock = $null
$agent = $null
try {
    Assert-PortFree $MockPort 'mock LLM'
    Assert-PortFree $AgentPort 'agent'

    Write-Host "`n== starting mock LLM on $MockPort ==" -ForegroundColor Cyan
    $mock = Start-Process -FilePath 'node' `
        -ArgumentList (Join-Path $root 'tools\mock_llm_server.mjs'), '--port', "$MockPort" `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $scratch 'mock.out') `
        -RedirectStandardError (Join-Path $scratch 'mock.err')
    if (-not (Wait-Port $MockPort 20)) { throw "mock llm did not start" }

    Write-Host "== starting tea_agent on $AgentPort ==" -ForegroundColor Cyan
    $agent = Start-Process -FilePath $Exe `
        -ArgumentList '--host', '127.0.0.1', '--port', "$AgentPort",
                      '--tea-home', $homeDir, '--workspace', $wsDir,
                      '--log-level', 'debug' `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $scratch 'agent.out') `
        -RedirectStandardError (Join-Path $scratch 'agent.err')
    if (-not (Wait-Port $AgentPort 20)) { throw "agent did not start" }

    $base = "http://127.0.0.1:$AgentPort"

    Write-Host "`n== API checks ==" -ForegroundColor Cyan
    Start-Sleep -Milliseconds 400

    $health = Invoke-RestMethod "$base/api/health"
    Check "GET /api/health ok" ($health.ok -eq $true)
    Check "health reports provider mock" ($health.provider -eq 'mock') "got '$($health.provider)'"
    Check "health reports model mock-model" ($health.model -eq 'mock-model') "got '$($health.model)'"

    $providers = Invoke-RestMethod "$base/api/providers"
    Check "GET /api/providers lists mock" (($providers.providers | ForEach-Object { $_.name }) -contains 'mock')
    Check "provider key is not exposed" (-not ($providers.PSObject.Properties.Name -contains 'api_key'))

    $tools = Invoke-RestMethod "$base/api/tools"
    $toolNames = $tools.tools | ForEach-Object { $_.name }
    Check "toolkit_exec registered" ($toolNames -contains 'toolkit_exec')
    Check "toolkit_file registered" ($toolNames -contains 'toolkit_file')

    $ws = Invoke-RestMethod "$base/api/workspaces"
    Check "workspace index exists" (-not [string]::IsNullOrEmpty($ws.index_path))
    Check "workspace registered" (($ws.workspaces | ForEach-Object { $_.path }) -contains (Resolve-Path $wsDir).Path)
    $wsId = ($ws.workspaces | Select-Object -First 1).id
    Check "workspace id present" (-not [string]::IsNullOrEmpty($wsId))

    $sessions = Invoke-RestMethod "$base/api/workspaces/$wsId/sessions"
    Check "session list endpoint works" ($null -ne $sessions.sessions)

    # --- system prompt: probed environment, stable prefix, no volatile clock ---
    $prompt = Invoke-RestMethod "$base/api/workspaces/$wsId/prompt"
    Check "prompt endpoint returns a system prompt" `
        ($prompt.system_prompt -match 'Environment \(probed from this device at startup\)')
    Check "prompt reports a real OS and architecture" `
        ($prompt.system_prompt -match '(?m)^- OS: .+ \((x86_64|aarch64|arm|x86|armv7l|unknown)\)$')
    # A per-turn timestamp at the head of the conversation destroys the provider's
    # prefix cache, so the prompt must not carry a wall clock.
    Check "prompt has no volatile wall clock" `
        (-not ($prompt.system_prompt -match 'Current time \(UTC\)'))
    Check "prompt is byte-stable across calls" `
        ($prompt.system_prompt -ceq (Invoke-RestMethod "$base/api/workspaces/$wsId/prompt").system_prompt)

    # --- built-in output filter self test ------------------------------------
    $selfOut = & $Exe --selftest 2>&1 | Out-String
    $selfCode = $LASTEXITCODE
    Check "built-in --selftest passes" ($selfCode -eq 0 -and $selfOut -match 'PASS') $selfOut.Trim()

    Write-Host "`n== web UI assets (embedded) ==" -ForegroundColor Cyan
    $indexRes = Invoke-WebRequest "$base/" -UseBasicParsing
    Check "GET / serves the web UI" ($indexRes.StatusCode -eq 200 -and $indexRes.Content -match 'app\.js')
    $appJs = Invoke-WebRequest "$base/app.js" -UseBasicParsing
    Check "GET /app.js served" ($appJs.StatusCode -eq 200 -and $appJs.RawContentLength -gt 10000) `
        "size=$($appJs.RawContentLength)"
    $appCss = Invoke-WebRequest "$base/app.css" -UseBasicParsing
    Check "GET /app.css served" ($appCss.StatusCode -eq 200)
    $tokens = Invoke-WebRequest "$base/tokens.css" -UseBasicParsing
    Check "GET /tokens.css served" ($tokens.StatusCode -eq 200)
    $allUi = $indexRes.Content + $appJs.Content + $appCss.Content + $tokens.Content
    Check "UI has no external CDN dependency" (-not ($allUi -match 'cdn\.|unpkg|jsdelivr|fonts\.googleapis'))
    Check "UI ships the composer metrics bar" ($indexRes.Content -match 'id="metric-cache"')
    Check "app.js renders the metrics block" ($appJs.Content -match 'applyMetrics')
    Check "app.css styles the metrics bar" ($appCss.Content -match '\.metric \.m-bar')

    Write-Host "`n== streaming chat (multi-turn tool calling) ==" -ForegroundColor Cyan
    $bodyFile = Join-Path $scratch 'chat.json'
    @{ workspace_id = $wsId; message = 'please run the mock tool sequence' } |
        ConvertTo-Json -Compress | Set-Content -Path $bodyFile -Encoding UTF8

    # --max-time so a stalled stream fails the check instead of hanging forever.
    $raw = & curl.exe -N -s --max-time 300 -X POST -H "Content-Type: application/json" `
        --data-binary "@$bodyFile" "$base/api/chat" 2>&1 | Out-String

    $events = @()
    foreach ($line in ($raw -split "`n")) {
        $t = $line.Trim()
        if ($t.StartsWith('data:')) {
            $payload = $t.Substring(5).Trim()
            if ($payload -and $payload -ne '[DONE]') {
                try { $events += ($payload | ConvertFrom-Json) } catch { }
            }
        }
    }
    $types = $events | ForEach-Object { $_.type }

    Check "stream has start event" ($types -contains 'start')
    Check "stream has content_delta" ($types -contains 'content_delta')
    Check "stream has reasoning_delta" ($types -contains 'reasoning_delta')
    Check "stream has tool_call" ($types -contains 'tool_call')
    Check "stream has tool_result" ($types -contains 'tool_result')
    Check "stream has iteration_end" ($types -contains 'iteration_end')
    Check "stream has done" ($types -contains 'done')
    # Streamed tool-call arguments: they are output tokens, so the UI counts them
    # for the live t/s figure instead of showing a speed that decays while the
    # model is writing a tool call.
    Check "stream has tool_args_delta" ($types -contains 'tool_args_delta')
    # Regression: thinking-mode gateways reject the follow-up request when the
    # replayed assistant turn lost its reasoning_content. The mock enforces that
    # contract and answers 400, which surfaces here as an error event.
    Check "no error event (reasoning_content echoed back)" ($types -notcontains 'error')

    $execResults = $events | Where-Object { $_.type -eq 'tool_result' -and $_.name -eq 'toolkit_exec' }
    Check "toolkit_exec ran" ($execResults.Count -ge 1)
    if ($execResults.Count -ge 1) {
        Check "toolkit_exec succeeded" ($execResults[0].ok -eq $true) "ok=$($execResults[0].ok)"
        Check "toolkit_exec captured output" ($execResults[0].output -match 'mock-tool-exec-ok') `
            "output='$($execResults[0].output)'"
    }
    $fileResults = $events | Where-Object { $_.type -eq 'tool_result' -and $_.name -eq 'toolkit_file' }
    Check "toolkit_file ran twice (write+read)" ($fileResults.Count -ge 2) "count=$($fileResults.Count)"

    $iterations = ($events | Where-Object { $_.type -eq 'iteration_end' }).Count
    Check "multiple LLM round trips" ($iterations -ge 4) "iterations=$iterations"

    # --- composer metrics: output speed / cache hit rate / context usage ------
    # Every iteration_end and the final done carry a `metrics` block; the UI
    # renders t/s, cache hit rate and max_context_tokens usage from it. This mock
    # reports no usage unless the client asks for it, so the numbers here must be
    # local estimates and must be flagged as such.
    $doneEvent = $events | Where-Object { $_.type -eq 'done' } | Select-Object -First 1
    Check "done carries a metrics block" ($null -ne $doneEvent.metrics)
    Check "done reports the model context window" `
        ($doneEvent.metrics.max_context_tokens -eq 128000) "got $($doneEvent.metrics.max_context_tokens)"
    Check "done reports context usage" ($doneEvent.metrics.context_tokens -gt 0) `
        "got $($doneEvent.metrics.context_tokens)"
    Check "done reports generated output tokens" ($doneEvent.metrics.output_tokens -gt 0) `
        "got $($doneEvent.metrics.output_tokens)"
    Check "done reports elapsed and generation timing" `
        (($doneEvent.metrics.elapsed_ms -gt 0) -and ($doneEvent.metrics.generation_ms -ge 0))
    Check "metrics are flagged as estimates without provider usage" `
        ($doneEvent.metrics.estimated -eq $true)
    Check "no provider usage means no cache figure" ($doneEvent.usage.prompt_tokens -eq 0)
    $iterMetrics = @($events | Where-Object { $_.type -eq 'iteration_end' -and $null -ne $_.metrics })
    Check "every iteration_end carries a metrics block" ($iterMetrics.Count -eq $iterations) `
        "metrics=$($iterMetrics.Count) iterations=$iterations"

    # curl tolerates a chunked body that is not properly terminated; browsers and
    # Node reject it ("Error in input stream" / "TypeError: terminated"). Assert
    # the closing chunk explicitly with a strict client.
    $probeOut = & node (Join-Path $PSScriptRoot 'sse_probe.mjs') $base $wsId 1 2>&1
    $probeCode = $LASTEXITCODE
    Check "stream terminates cleanly for a strict client" ($probeCode -eq 0) `
        (($probeOut -join ' ').Trim())

    Write-Host "`n== side effects on disk ==" -ForegroundColor Cyan
    $runDir = Join-Path $wsDir '.tea_agent_run'
    Check ".tea_agent_run created" (Test-Path $runDir)
    Check ".tea_agent_run/logs/agent.log created" (Test-Path (Join-Path $runDir 'logs\agent.log'))
    Check ".tea_agent_run/sessions created" (Test-Path (Join-Path $runDir 'sessions'))
    Check ".tea_agent_run/memory/memory.json created" (Test-Path (Join-Path $runDir 'memory\memory.json'))
    Check ".tea_agent_run/meta.json created" (Test-Path (Join-Path $runDir 'meta.json'))
    Check "toolkit_file wrote mock_note.txt" (Test-Path (Join-Path $wsDir 'mock_note.txt'))
    if (Test-Path (Join-Path $wsDir 'mock_note.txt')) {
        $note = Get-Content (Join-Path $wsDir 'mock_note.txt') -Raw
        Check "mock_note.txt has expected content" ($note -match 'written by mock llm') "got '$note'"
    }
    Check "workspace index written to tea home" (Test-Path (Join-Path $homeDir 'workspaces.json'))

    $sessionFiles = Get-ChildItem (Join-Path $runDir 'sessions') -Filter '*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'index.json' }
    Check "session transcript persisted" ($sessionFiles.Count -ge 1)
    if ($sessionFiles.Count -ge 1) {
        $session = Get-Content $sessionFiles[0].FullName -Raw | ConvertFrom-Json
        $roles = $session.messages | ForEach-Object { $_.role }
        Check "transcript has user/assistant/tool messages" `
            (($roles -contains 'user') -and ($roles -contains 'assistant') -and ($roles -contains 'tool'))
    }

    $fileEcho = Invoke-RestMethod "$base/api/workspaces/$wsId/file?path=mock_note.txt"
    Check "GET file endpoint returns the note" ($fileEcho.content -match 'written by mock llm')

    $tree = Invoke-RestMethod "$base/api/workspaces/$wsId/tree"
    Check "GET tree endpoint lists files" (($tree.entries | ForEach-Object { $_.name }) -contains 'mock_note.txt')

    $memAdd = Invoke-RestMethod "$base/api/workspaces/$wsId/memory" -Method Post `
        -ContentType 'application/json' -Body '{"text":"e2e test note"}'
    Check "POST memory creates a note" (-not [string]::IsNullOrEmpty($memAdd.id))

    $logs = Invoke-RestMethod "$base/api/workspaces/$wsId/logs?tail=50"
    Check "GET logs returns lines" ($logs.lines.Count -ge 1)
    # --- history compaction: tiny window + reused session + workspace settings --
    # Per-workspace settings are also covered here: keep_turns and
    # compaction_keep_recent come from <run_dir>/settings.yaml, not the global config.
    $settingsPath = Join-Path $wsDir ".tea_agent_run\settings.yaml"
    Set-Content -Path $settingsPath -Encoding ASCII -Value @(
        "settings:",
        "  keep_turns: 1",
        "  compaction_keep_recent: 2",
        "  tool_output_max_lines: 50",
        "  stream_include_usage: true"
    )

    # --- provider-reported usage: the metrics must stop being estimates -------
    $usageSession = (Invoke-RestMethod -Method Post -Uri "$base/api/workspaces/$wsId/sessions" `
            -ContentType 'application/json' -Body '{"title":"usage"}' -TimeoutSec 10).id
    $usageEvents = Invoke-ChatTurn 'mock' $usageSession 'report usage please'
    $usageDone = $usageEvents | Where-Object { $_.type -eq 'done' } | Select-Object -First 1
    Check "usage-enabled turn reports real metrics" ($usageDone.metrics.estimated -eq $false)
    # `context_tokens` is the largest single request of the turn, not the sum of
    # all requests: the UI shows how full the window got, so summing would lie.
    $usageLastIter = $usageEvents | Where-Object { $_.type -eq 'iteration_end' } | Select-Object -Last 1
    Check "context usage equals the last request's prompt tokens" `
        ($usageDone.metrics.context_tokens -eq $usageLastIter.usage.prompt_tokens) `
        "metrics=$($usageDone.metrics.context_tokens) last=$($usageLastIter.usage.prompt_tokens)"
    Check "output tokens equal the reported completion tokens" `
        ($usageDone.metrics.output_tokens -eq $usageDone.usage.completion_tokens) `
        "metrics=$($usageDone.metrics.output_tokens) usage=$($usageDone.usage.completion_tokens)"
    Check "cache hit rate becomes measurable" `
        (($usageDone.usage.prompt_tokens -gt 0) -and ($usageDone.usage.cached_tokens -eq 0)) `
        "prompt=$($usageDone.usage.prompt_tokens) cached=$($usageDone.usage.cached_tokens)"

    # A gateway that ignores stream_options.include_usage must degrade to
    # estimates, not to zeros or a broken UI.
    $noUsageEvents = Invoke-ChatTurn 'mock_nousage' $usageSession 'report usage again'
    $noUsageDone = $noUsageEvents | Where-Object { $_.type -eq 'done' } | Select-Object -First 1
    Check "gateway ignoring include_usage falls back to estimates" `
        ($noUsageDone.metrics.estimated -eq $true)
    Check "estimated context usage is still populated" `
        (($noUsageDone.metrics.context_tokens -gt 0) -and ($noUsageDone.metrics.max_context_tokens -eq 64000)) `
        "context=$($noUsageDone.metrics.context_tokens) max=$($noUsageDone.metrics.max_context_tokens)"

    $csess = (Invoke-RestMethod -Method Post -Uri "$base/api/workspaces/$wsId/sessions" `
            -ContentType 'application/json' -Body '{"title":"compaction"}' -TimeoutSec 10).id
    $compactions = 0
    for ($round = 0; $round -lt 3; $round++) {
        $pad = 'x' * 400
        $cbody = (@{
                workspace_id = $wsId
                session_id   = $csess
                provider     = 'mock_small'
                model        = 'mock-model'
                message      = "probe $round $pad"
            } | ConvertTo-Json -Compress)
        $cfile = Join-Path $scratch "compaction-$round.json"
        Set-Content -Path $cfile -Encoding UTF8 -Value $cbody
        $craw = & curl.exe -N -s --max-time 120 -X POST -H "Content-Type: application/json" `
            --data-binary "@$cfile" "$base/api/chat" 2>&1 | Out-String
        $compactions += ([regex]::Matches($craw, '"type":\s*"compaction"')).Count
    }
    Check "compaction event emitted" ($compactions -ge 1) "count=$compactions"
    $cSessFile = Join-Path $wsDir ".tea_agent_run\sessions\$csess.json"
    $cSess = Get-Content -Path $cSessFile -Raw | ConvertFrom-Json
    Check "session stores a rolling summary" `
        (($cSess.summary_upto -gt 0) -and (-not [string]::IsNullOrEmpty($cSess.summary))) `
        "upto=$($cSess.summary_upto) bytes=$($cSess.summary.Length)"
    Check "compaction keeps the original messages" ($cSess.messages.Count -gt $cSess.summary_upto)}
catch {
    Write-Host "`nUNEXPECTED ERROR: $_" -ForegroundColor Red
    $failures += "exception"
}
finally {
    if ($agent -and -not $agent.HasExited) { Stop-Process -Id $agent.Id -Force -ErrorAction SilentlyContinue }
    if ($mock -and -not $mock.HasExited) { Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 300
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "ALL $checks CHECKS PASSED" -ForegroundColor Green
    exit 0
}
Write-Host "$($failures.Count) of $checks CHECKS FAILED:" -ForegroundColor Red
$failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
exit 1

