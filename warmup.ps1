# warmup.ps1 - absorb the engine's one-time cold-start cost after (re)start.
# Evidence from engine.log: the first 1-2 requests after a restart pay
# 16-19s prefill even for tiny prompts; after warmup, prefill drops to ~1-2s.
# This script waits for the engine, then sends 3 tiny DIRECT-to-engine pings
# (they do NOT touch the dieting proxy session / history).
$ErrorActionPreference = "SilentlyContinue"

Write-Host " waiting for engine on :18200 (model load ~20-40s)..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18200/props" -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host " [WARN] engine not ready within ~120s - skipping warmup." -ForegroundColor Yellow
    Write-Host "        Check E:\ninfer-kvmem\engine.log if this keeps happening."
    exit 1
}

Write-Host " engine up. sending 3 warmup pings (one-time cold-start cost, ~10-60s)..." -ForegroundColor Cyan
$body = '{"model":"bonsai","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
for ($i = 1; $i -le 3; $i++) {
    $t0 = Get-Date
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18200/v1/chat/completions" `
            -Method Post -ContentType "application/json" -Body $body -TimeoutSec 300 | Out-Null
    } catch { }
    $dt = ((Get-Date) - $t0).TotalSeconds
    Write-Host ("   warmup {0}/3 done in {1:N1} s" -f $i, $dt)
}

# Also warm the TOOL-mode path on :18200, so the first real tool call isn't cold
# (tool schemas add prompt tokens; prewarming keeps the tool context fast too).
Write-Host " warming tool-mode context (1 ping)..." -ForegroundColor Cyan
$toolbody = '{"model":"bonsai","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","function":{"name":"noop","description":"no-op placeholder","parameters":{"type":"object","properties":{}}}}],"max_tokens":1}'
try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18200/v1/chat/completions" `
        -Method Post -ContentType "application/json" -Body $toolbody -TimeoutSec 300 | Out-Null
} catch { }
Write-Host " tool-mode warmup done." -ForegroundColor Cyan
Write-Host " warmup complete - your first real message won't pay the cold-start cost." -ForegroundColor Green
