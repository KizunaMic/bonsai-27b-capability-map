# Paths are derived from this script's location so the repo works
# from any drive. Override with environment variables if needed.
$root  = if ($env:KVME_ROOT)  { $env:KVME_ROOT }  else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$bin   = "$root\engine\bin"
$model = if ($env:KVME_MODEL) { $env:KVME_MODEL } else { "$root\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf" }

# 把引擎 bin 前置到 PATH（cudart64_13 / cublas*_13 在那里，免装 CUDA toolkit）
$env:PATH = "$bin;" + $env:PATH

# 注意：--kvmem-nvme-* 本构建已禁用(KVMEM_ENABLE_NVME=OFF，编译期关死，设环境变量无效)，
# 加上会启动失败 "NVMe offload is disabled in this build"；待引擎更新后再启用。
# 以下反引号 ` 为续行符，注释不能插在参数中间，只能写在这里。
& "$bin\llama-kvmem-server.exe" `
  -m $model --host 127.0.0.1 --port 18200 `
  -c 131072 -ngl 99 --kv-dtype q8_0 -ub 64 -b 256 `
  --mmproj $env:KVME_MMPROJ 或 <你的 mmproj 路径>\Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf `
  --no-mmproj-offload `
  --kvmem-block-tokens 128 `
  --kvmem-sink-tokens 4096 `
  --kvmem-budget 16384 `
  --kvmem-gen-reserve 16384 `
  --kvmem-cpu-gb 6 `
  --kvmem-method retrieval `
  --kvmem-trace `
  --enable-thinking `
  --reasoning-budget 2048 `
  --spec-type none
