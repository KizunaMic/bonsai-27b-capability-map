# monitor.ps1 - KVMem live monitor window (read-only, refresh 3s)
# Launched by start_kvmem_all.bat. Close this window anytime; it also
# auto-exits ~10s after both services go down (e.g. main window closed).
param([switch]$Once)
$ErrorActionPreference = "SilentlyContinue"
$engLog   = "E:\ninfer-kvmem\engine.log"
$proxyLog = "E:\ninfer-kvmem\proxy.log"
$sessFile = "E:\ninfer-kvmem\controller\session.json"

function Test-Port([int]$p) {
    $c = New-Object Net.Sockets.TcpClient
    $ok = $false
    try { $ok = $c.ConnectAsync("127.0.0.1", $p).Wait(300) -and $c.Connected } catch { $ok = $false }
    $c.Close()
    return $ok
}
function Fmt-Bytes([double]$b) {
    if ($b -ge 1GB) { return ("{0:N1} GB" -f ($b / 1GB)) }
    if ($b -ge 1MB) { return ("{0:N1} MB" -f ($b / 1MB)) }
    return ("{0} B" -f $b)
}

$engineWasUp = $false
$downCount = 0
while ($true) {
    Clear-Host
    $now = Get-Date
    $engUp = Test-Port 18200
    $prxUp = Test-Port 18201
    if ($engUp) { $engineWasUp = $true; $downCount = 0 }
    elseif ($engineWasUp) { $downCount++ }

    Write-Host "============ KVMem Monitor  $($now.ToString('yyyy-MM-dd HH:mm:ss')) ============" -ForegroundColor Cyan
    if (Test-Path $engLog) {
        $up = $now - (Get-Item $engLog).CreationTime
        Write-Host (" engine uptime    : {0:hh\:mm\:ss}" -f $up)
    }
    $tag1 = "RUNNING"; if (-not $engUp) { $tag1 = "DOWN" }
    $tag2 = "RUNNING"; if (-not $prxUp) { $tag2 = "DOWN" }
    Write-Host " engine :18200    : $tag1" -ForegroundColor $(if ($engUp) { "Green" } else { "Red" })
    Write-Host " proxy  :18201    : $tag2  (client Base URL: http://127.0.0.1:18201/v1)" -ForegroundColor $(if ($prxUp) { "Green" } else { "Red" })

    Write-Host ""
    Write-Host "---- last chat turn (from engine.log) ----" -ForegroundColor Yellow
    $turn = Select-String -Path $engLog -Pattern "KVMEM_CHAT_TURN" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($turn -and $turn.Line -match "n_prompt=(\d+).*n_gen=(\d+) prefill_ms=([\d.]+) gen_ms=([\d.]+).*gen_toks=([\d.]+)") {
        Write-Host (" prompt tokens    : {0}   (dieted context; raw client history is NOT re-sent)" -f $Matches[1])
        Write-Host (" prefill time     : {0:N1} s" -f ([double]$Matches[3] / 1000))
        Write-Host (" gen tokens/speed : {0} tok / {1:N1} tok/s" -f $Matches[2], [double]$Matches[5])
    } else {
        Write-Host " (no chat turns yet)"
    }
    $fin = Select-String -Path $engLog -Pattern "chat_stream .*finish=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($fin) {
        $fs = "unknown"
        if ($fin.Line -match "finish=(\w+)") { $fs = $Matches[1] }
        $cc = "0"; if ($fin.Line -match "content_chars=(\d+)") { $cc = $Matches[1] }
        Write-Host " last finish      : $fs  (content_chars=$cc)"
    }

    Write-Host ""
    Write-Host "---- KV cache / offload status ----" -ForegroundColor Yellow
    $tiers = Select-String -Path $engLog -Pattern "KVMEM_TIERS" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($tiers -and $tiers.Line -match "cpu_bytes=(\d+).*nvme_bytes=(\d+)") {
        Write-Host (" KV CPU tier      : {0}" -f (Fmt-Bytes ([long]$Matches[1])))
        $nv = [long]$Matches[2]
        $msg = " KV NVMe tier     : {0}" -f (Fmt-Bytes $nv)
        if ($nv -gt 0) { Write-Host "$msg  <-- NVMe offload ACTIVE" -ForegroundColor Magenta }
        else { Write-Host "$msg  (nvme offload not in use in this build)" }
    }
    $app = Select-String -Path $engLog -Pattern "KVMEM_TRACE append" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($app -and $app.Line -match "total=(\d+) resident=(\d+).*need_offload=(\d+) free_slots=(\d+)") {
        Write-Host (" KV total/resident: {0} / {1} tokens" -f $Matches[1], $Matches[2])
        $no = [int]$Matches[3]
        if ($no -gt 0) { Write-Host (" need_offload     : {0}  <-- KV offload happening" -f $no) -ForegroundColor Magenta }
        else { Write-Host " need_offload     : 0 (all KV resident, no offload)" -ForegroundColor Green }
        Write-Host (" free KV slots    : {0}" -f $Matches[4])
    }
    $harv = (Select-String -Path $engLog -Pattern "KVMEM_TRACE harvest" -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Host " harvest events   : $harv  (KV moved into retrieval index - normal)"

    Write-Host ""
    Write-Host "---- dieting proxy session ----" -ForegroundColor Yellow
    if (Test-Path $sessFile) {
        try {
            $j = Get-Content $sessFile -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host (" active turns     : {0}  (rolling window, cap = 6 rounds)" -f $j.turns.Count)
            $sl = 0; if ($j.summary) { $sl = $j.summary.Length }
            Write-Host (" history summary  : {0} chars  (old context compressed here)" -f $sl)
        } catch { Write-Host " session.json unreadable" }
    } else {
        Write-Host " (no session yet)"
    }
    if (Test-Path $proxyLog) {
        $tail = Get-Content $proxyLog -Tail 3 -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($tail) { Write-Host (" proxy log tail   : {0}" -f ($tail -join " | ")) }
    }

    Write-Host ""
    Write-Host " refreshing every 3s - read-only window, close anytime" -ForegroundColor DarkGray

    if ($Once) { break }
    # auto-exit: services were up, then both down for ~10s (main window closed)
    if ($engineWasUp -and -not $engUp -and -not $prxUp -and $downCount -ge 3) { break }
    Start-Sleep -Seconds 3
}
