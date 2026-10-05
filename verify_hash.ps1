$model = "E:\ninfer-kvmem\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
$expectedHash = "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3"
$expectedSize = 5946648928

$hash = (Get-FileHash -Algorithm SHA256 $model).Hash.ToLower()
$size = (Get-Item $model).Length

Write-Host "size   = $size (expected $expectedSize)"
Write-Host "sha256 = $hash"
Write-Host "expect = $expectedHash"

if ($size -eq $expectedSize -and $hash -eq $expectedHash) {
    Write-Host "RESULT: PASS"
} else {
    Write-Host "RESULT: FAIL"
}
