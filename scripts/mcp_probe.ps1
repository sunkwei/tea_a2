<#
.SYNOPSIS
  Talk to an MCP server directly, without tea_agent.

.DESCRIPTION
  Starts the given program, does the MCP handshake (initialize / initialized /
  tools/list) and prints what the server offers. With -Tool it calls one tool
  and prints the answer.

  The point is to separate "is the server itself all right?" from "does the
  agent use it correctly?": if this probe lists the tools, the server is fine
  and any problem left is in the agent (or in the model).

.EXAMPLE
  pwsh -File scripts\mcp_probe.ps1 -Command C:\Tools\uiautomation-mcp\uiautomation-mcp.exe

.EXAMPLE
  pwsh -File scripts\mcp_probe.ps1 -Command C:\Tools\uiautomation-mcp\uiautomation-mcp.exe `
        -Tool SearchElements -ToolArguments '{"name":"OK"}'
#>
param(
  [Parameter(Mandatory = $true)][string]$Command,
  [string[]]$Arguments = @(),
  [string]$Tool = "",
  [string]$ToolArguments = "{}",
  [int]$ProtocolVersion = 0,
  [switch]$KeepAlive
)

$ErrorActionPreference = "Stop"

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Command
if ($Arguments.Count -gt 0) {
  # Windows PowerShell 5.1 has no ArgumentList; quoting by hand is good
  # enough for a probe.
  $psi.Arguments = ($Arguments | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join " "
}
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
if (-not $proc.Start()) {
  Write-Host "cannot start '$Command'" -ForegroundColor Red
  exit 2
}
Write-Host "started: $Command $($psi.Arguments)" -ForegroundColor DarkGray

# The server logs to stderr; keep it out of the way but show it on demand.
$errJob = Start-Job -ScriptBlock {
  param($p)
  while ($true) {
    $line = $p.StandardError.ReadLine()
    if ($null -eq $line) { break }
    Write-Host "[stderr] $line" -ForegroundColor DarkYellow
  }
} -ArgumentList $proc

function Send-Mcp($obj) {
  $json = $obj | ConvertTo-Json -Depth 30 -Compress
  $proc.StandardInput.WriteLine($json)
  $proc.StandardInput.Flush()
  Write-Host "-> $json" -ForegroundColor DarkGray
}

function Read-Mcp {
  $line = $proc.StandardOutput.ReadLine()
  if ($null -eq $line) { return $null }
  Write-Host ("<- " + $line.Substring(0, [Math]::Min(400, $line.Length))) -ForegroundColor DarkGray
  return ($line | ConvertFrom-Json)
}

try {
  $version = if ($ProtocolVersion -gt 0) { "$ProtocolVersion" } else { "2024-11-05" }
  Send-Mcp @{
    jsonrpc = "2.0"; id = 1; method = "initialize"
    params = @{
      protocolVersion = $version
      capabilities = @{}
      clientInfo = @{ name = "mcp_probe"; version = "1.0" }
    }
  }
  $init = Read-Mcp
  if ($null -eq $init) { throw "the server closed its output" }
  if ($init.error) { throw ("initialize failed: " + ($init.error | ConvertTo-Json -Compress)) }
  Write-Host ("protocol: {0}   server: {1} {2}" -f $init.result.protocolVersion,
               $init.result.serverInfo.name, $init.result.serverInfo.version) -ForegroundColor Green

  Send-Mcp @{ jsonrpc = "2.0"; method = "notifications/initialized" }
  Send-Mcp @{ jsonrpc = "2.0"; id = 2; method = "tools/list"; params = @{} }
  $list = Read-Mcp
  if ($null -eq $list) { throw "the server closed its output" }
  if ($list.error) { throw ("tools/list failed: " + ($list.error | ConvertTo-Json -Compress)) }

  Write-Host ""
  Write-Host ("{0} tool(s):" -f $list.result.tools.Count) -ForegroundColor Cyan
  foreach ($t in $list.result.tools) {
    Write-Host ("  {0}" -f $t.name) -ForegroundColor White
    if ($t.description) {
      $desc = $t.description -replace "\s+", " "
      Write-Host ("      {0}" -f $desc.Substring(0, [Math]::Min(160, $desc.Length))) -ForegroundColor DarkGray
    }
  }

  if ($Tool) {
    $args = $ToolArguments | ConvertFrom-Json
    Send-Mcp @{ jsonrpc = "2.0"; id = 3; method = "tools/call"
                params = @{ name = $Tool; arguments = $args } }
    $call = Read-Mcp
    Write-Host ""
    if ($null -eq $call) { throw "the server closed its output" }
    if ($call.error) {
      Write-Host ("the call failed: " + ($call.error | ConvertTo-Json -Compress)) -ForegroundColor Red
    } else {
      Write-Host ("isError: {0}" -f $call.result.isError) -ForegroundColor Yellow
      foreach ($c in $call.result.content) {
        if ($c.type -eq "text") { Write-Host $c.text }
        elseif ($c.type -eq "image") { Write-Host ("[an image, {0} base64 chars]" -f $c.data.Length) }
        else { Write-Host ("[{0}]" -f $c.type) }
      }
    }
  }
} catch {
  Write-Host $_.Exception.Message -ForegroundColor Red
  exit 1
} finally {
  if (-not $KeepAlive) {
    try { $proc.StandardInput.Close() } catch {}
    if (-not $proc.HasExited) { try { $proc.Kill() } catch {} }
  }
  try { Stop-Job $errJob -ErrorAction SilentlyContinue; Remove-Job $errJob -Force -ErrorAction SilentlyContinue } catch {}
}
