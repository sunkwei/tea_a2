<#
.SYNOPSIS
  Check the MCP servers of a running tea_agent.

.DESCRIPTION
  Prints what the agent knows about the MCP servers (the `mcp.yaml` next to
  provider.yaml): whether each one is connected, which protocol version the
  two sides agreed on, and which tools it offers. With -Tool it calls one tool
  directly, without a model in the loop - the quickest way to find out whether
  the server itself works.

  The agent must already be running (tea_agent.exe --port 8282 ...).

.EXAMPLE
  pwsh -File scripts\mcp-check.ps1

.EXAMPLE
  pwsh -File scripts\mcp-check.ps1 -Server uiautomation `
        -Tool mcp__uiautomation__SearchElements -Arguments '{"name":"OK"}'
#>
param(
  [string]$Base = "http://127.0.0.1:8282",
  [string]$Server = "",
  [string]$Tool = "",
  [string]$Arguments = "{}",
  [switch]$Refresh
)

$ErrorActionPreference = "Stop"

function Get-Json([string]$Url) {
  try {
    return Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 30
  } catch {
    Write-Host "cannot reach $Url : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "is the agent running? (tea_agent.exe --port 8282)" -ForegroundColor Yellow
    exit 2
  }
}

Write-Host "== MCP servers ==" -ForegroundColor Cyan
if ($Refresh) {
  Write-Host "(refreshing: every server is stopped and started again)"
  $null = Invoke-RestMethod -Uri "$Base/api/mcp/refresh" -Method Post -TimeoutSec 120
}

$state = Get-Json "$Base/api/mcp/servers"
if (-not $state.configured) {
  Write-Host "no MCP server is configured (write an mcp.yaml next to provider.yaml)" -ForegroundColor Yellow
  exit 1
}
if (-not $state.enabled) {
  Write-Host "the MCP configuration is switched off (enabled: false)" -ForegroundColor Yellow
}

foreach ($s in $state.servers) {
  $mark = if ($s.connected) { "OK  " } else { "FAIL" }
  $color = if ($s.connected) { "Green" } else { "Red" }
  Write-Host ("[{0}] {1}  ({2} tool(s))" -f $mark, $s.name, $s.tool_count) -ForegroundColor $color
  Write-Host ("       command : {0} {1}" -f $s.command, ($s.args -join " ")) -ForegroundColor DarkGray
  if ($s.connected) {
    Write-Host ("       protocol: {0}   server: {1} {2}" -f $s.protocol_version, $s.server_name, $s.server_version) -ForegroundColor DarkGray
  }
  if ($s.error) { Write-Host ("       error  : {0}" -f $s.error) -ForegroundColor Red }
  if ($s.tools -and $s.tools.Count -gt 0) {
    $names = ($s.tools | ForEach-Object { $_.name }) -join ", "
    Write-Host ("       tools  : {0}" -f $names) -ForegroundColor DarkGray
  }
}

if (-not $Tool) {
  Write-Host ""
  Write-Host "call one tool with: -Tool <full name> -Arguments '<json>'" -ForegroundColor DarkGray
  exit 0
}

if (-not $Server) {
  if ($state.servers.Count -ne 1) {
    Write-Host "-Server is needed when there is more than one server" -ForegroundColor Yellow
    exit 1
  }
  $Server = $state.servers[0].name
}

Write-Host ""
Write-Host "== calling $Tool on $Server ==" -ForegroundColor Cyan
$body = @{ tool = $Tool; arguments = ($Arguments | ConvertFrom-Json) } | ConvertTo-Json -Depth 20
try {
  $answer = Invoke-RestMethod -Uri "$Base/api/mcp/servers/$Server/call" -Method Post `
      -ContentType "application/json" -Body $body -TimeoutSec 120
} catch {
  Write-Host "the call failed: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
if ($answer.is_error) {
  Write-Host "the tool reported an error:" -ForegroundColor Red
} else {
  Write-Host "the tool answered:" -ForegroundColor Green
}
Write-Host $answer.text
if ($answer.images -gt 0) {
  Write-Host ("({0} image(s) came back)" -f $answer.images) -ForegroundColor DarkGray
}
