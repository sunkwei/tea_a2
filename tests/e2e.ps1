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
}
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
