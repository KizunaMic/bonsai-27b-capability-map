# KVMem 引擎可调参数清单与研判

> 引擎：`E:\ninfer-kvmem\engine\bin\llama-kvmem-server.exe`（KVMem 分支，CUDA 13.4）
> 模型：`Ternary-Bonsai-2-27B-PTQ1_0.gguf`（5.9GB，三元 1.58-bit）
> 硬件：RTX 4060 8GB + 16GB 内存 + Windows
> 当前启动脚本：`E:\ninfer-kvmem\start_kvmem.ps1`

状态图例：🟢 已启用 ｜ ⚪ 默认（未设，用引擎默认值） ｜ 🔵 可启用（值得试） ｜ ⚫ 对你无意义（多卡/legacy/用不上）

---

## ① KVMem 长上下文内存管理（核心，21 项）

这是你这套"128K 长上下文跑在 8GB 卡上"的根基。

| 参数 | 默认 | 当前 | 作用 / 你的 4060 研判 |
|---|---|---|---|
| `--kvmem` / `--no-kvmem` | 开 | 🟢开 | 总开关。关掉就退化成普通 llama.cpp（显存放不下 128K）。**别关。** |
| `--kvmem-budget N` | 0=n_ctx | 🟢16384 | GPU 常驻工作集 token 数。16384≈占一部分显存当"活跃记忆"，其余换出到 CPU。调大=更快但更吃显存；调小=省显存但换入换出更频繁。**核心旋钮。** |
| `--kvmem-block-tokens N` | 128 | 🟢128 | 换出/换入的最小块大小。128 是较细粒度，够用。 |
| `--kvmem-sink-tokens N` | 0(=1块) | 🟢4096 | 永远留在显存的"系统前缀"（如 system prompt）。4096 保证开头稳定不被换出。 |
| `--kvmem-gen-reserve N` | 256 | 🟢4096 | 解码时给显存预留的 slack，防 OOM。4096 偏宽松，可试降到 2048 省显存。 |
| `--kvmem-recent-tokens N` | 0 | ⚪ | 永远保留的"最新后缀"token 数。0=不强制保留最新。长对话想保住最近几句可设个值（如 2048）。 |
| `--kvmem-method NAME` | retrieval | 🟢retrieval | 换出策略：recency(纯最近) vs retrieval(按相关性召回)。retrieval 更智能但每次请求多一点检索开销。**可试 recency 对比速度。** |
| `--kvmem-query-last N` | 64 | ⚪ | retrieval 模式下的回退查询长度。一般不用动。 |
| `--kvmem-query-max-tokens N` | 512 | ⚪ | retrieval 查询最多取最后多少 token。默认 512 合理。 |
| `--kvmem-query-replay MODE` | auto | ⚪ | 查询重放策略。一般不用动。 |
| `--kvmem-query-policy MODE` | user | ⚪ | 查询来源策略（legacy/user）。默认 user 即可。 |
| `--kvmem-mtp-state MODE` | replay | ⚪ | 推测解码状态快照策略。因你 spec-type=none，此项基本不生效。 |
| `--kvmem-gpu-ratio R` | 0.50 | ⚪ | KV 槽位池上限占显存比例。0.50 即最多用一半显存给 KV。和 budget 配合，一般不用动。 |
| `--kvmem-cpu-gb GB` | 0(关) | 🟢6 | CPU 内存溢出竞技场（KV 换出目的地）。6GB 是你 16GB 内存里划给长上下文的。调大=能撑更长上下文但占内存；调小=省内存。 |
| `--kvmem-nvme-gb GB` | 0(关) | ⚪ | 把溢出的 KV 存到 NVMe 固态盘（比内存慢但比没有强）。**若 16GB 内存吃紧可开**，如 `--kvmem-nvme-gb 20`。 |
| `--kvmem-nvme-dir PATH` | /tmp/kvmem_nvme | ⚪ | NVMe 文件目录。Windows 上要改到真实盘符（如 `E:/kvmem_nvme`）。 |
| `--kvmem-harvest-v` | 关 | ⚪ | 预填充时把 V 以 raw-K 方式 D2H。实验性，一般不动。 |
| `--kvmem-raw-k-nvme` | 关 | ⚪ | 把 raw-K 和 V 存 NVMe（需配合 --kvmem-nvme-gb）。实验性。 |
| `--kvmem-trace` / `--no-kvmem-trace` | 关 | 🟢开 | KVMEM_* 原始诊断日志。调试用，**跑顺了可关掉减日志**。 |
| `--kvmem-block-tokens` 见上 | | | |
| `--kvmem-gen-reserve` 见上 | | | |

**研判优先级：高。** budget / gen-reserve / cpu-gb / method 四个是最值得你动手试的。

---

## ② 采样参数（控制生成风格，8 项，全默认）

这些决定"模型说话像不像样"。可在请求级覆盖，也可在启动级设默认值。

| 参数 | 默认 | 当前 | 作用 |
|---|---|---|---|
| `--temp` / `--temperature T` | 1.0/0.7 | ⚪ | 温度。[0,2]，0=贪心最确定。想稳定输出调低（0.3~0.7）。 |
| `--top-p P` | 0.95/0.80 | ⚪ | 核采样阈值。调低更聚焦。 |
| `--top-k K` | 20 | ⚪ | 每步只从概率最高的 K 个词选。0=关闭。 |
| `--min-p P` | 0 | ⚪ | 最小相对概率过滤。0=不过滤。 |
| `--presence-penalty P` | 0/1.5 | ⚪ | 话题新鲜度惩罚，防重复啰嗦。 |
| `--frequency-penalty P` | 0 | ⚪ | 词频惩罚，防同一个词反复出现。 |
| `--repeat-penalty P` | 1 | ⚪ | 重复惩罚。>1 抑制复读。 |
| `--seed N` | 随机 | ⚪ | 随机种子。设固定值=可复现输出，调试/对比时有用。 |

**研判优先级：中。** 想要更"听话/不啰嗦"的输出，调 temp+repeat-penalty 即可。

---

## ③ 模型与视觉（6 项，🔵 重点）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `-m, --model PATH` | 无 | 🟢PTQ1_0 | 权重文件。已是 q1(PTQ1_0)。 |
| `--mmproj PATH` | 无 | ⚪ | **视觉投影器**（给模型装眼睛）。盘上有：`E:/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf`。加上即解锁看图能力。 |
| `--mmproj-offload` | 开(GPU) | ⚪ | 投影器放 GPU。 |
| `--no-mmproj-offload` | — | ⚪ | 投影器放 CPU，省 ~0.9GB 显存。**8GB 卡建议用这个。** |
| `--image-min-tokens N` | — | ⚪ | 单图最少视觉 token。 |
| `--image-max-tokens N` | — | ⚪ | 单图最多视觉 token。4060 上建议设 1024，防一张图预填充几千 token 拖慢。 |

**研判优先级：高。** 上次确认过：模型本来能看图，就差加 `--mmproj`。

---

## ④ 上下文 / 批处理 / 设备（25 项，含大量 ⚫）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `-c, --ctx-size N` | 2048 | 🟢131072 | 上下文窗口。128K 是你的核心卖点。 |
| `-n, --n-predict N` | -1(不封顶) | ⚪ | 单次生成最大 token。-1=不设上限（受客户端控）。可设个值防跑飞。 |
| `-b, --batch-size N` | 512 | 🟢256 | 逻辑批大小。256 偏保守，可试 512 提速（多吃点显存）。 |
| `-ngl, --n-gpu-layers N` | 99 | 🟢99 | 卸载到 GPU 的层数。99=全卸（8GB 够）。**别动。** |
| `--gpu-layers N` | — | ⚪ | -ngl 的别名。 |
| `-t, --threads N` | 硬件并发 | ⚪ | CPU 生成线程。4060 全 GPU 卸载时影响小。 |
| `-tb, --threads-batch N` | =threads | ⚪ | CPU 批处理线程。 |
| `-ub, --ubatch-size N` | =batch | 🟢64 | 物理批大小。64 偏小，可试 128/256 提速。 |
| `-fa, --flash-attn MODE` | auto | ⚪ | 闪存注意力。auto 通常已开，长上下文建议确认是 on。 |
| `-a, --alias NAME` | — | ⚪ | API 暴露的模型名。给 WorkBuddy/ChatBox 识别用。 |
| `--api-key KEY` / `--api-key-file` | 无 | ⚪ | **API 鉴权。** 若要走 Cloudflare 隧道公网，必须加 key 防裸奔。 |
| `-np, --parallel N` | 1 | ⚪ | 并发。仅支持 1，**别设别的。** |
| `-lm, --load-mode MODE` | auto | ⚪ | 加载模式：mmap/mlock/dio 等。默认 auto 即可。 |
| `--mmap`/`--no-mmap`/`--mlock` | — | ⚪ | legacy 别名。⚫ 忽略。 |
| `--timeout, -to N` | 1800秒 | ⚪ | HTTP 超时。**上次 122k 卡死就因客户端超时太短。** 可设大点（如 3600）。 |
| `--threads-http N` | 自动 | ⚪ | HTTP 工作线程。一般不动。 |
| `--device, -dev NAME` | CUDA0 | ⚪ | 卸载设备。单卡不用动。 |
| `--list-devices` | — | ⚪ | 列出设备后退出。诊断用。 |
| `--main-gpu, -mg N` | 0 | ⚪ | 主 GPU 索引。单卡不动。 |
| `--split-mode, -sm MODE` | — | ⚪ | 多卡切分。⚫ 单卡无意义。 |
| `--tensor-split, -ts N` | — | ⚪ | 多卡比例。⚫ 单卡无意义。 |

**研判优先级：中。** 重点看 `-b`/`-ub`(提速)、`--api-key`(安全)、`--timeout`(上次踩坑)、`-fa`(长上下文)。

---

## ⑤ KV 缓存量化（3 项）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `--kv-dtype NAME` | q8_0 | 🟢q8_0 | GPU KV 缓存精度。q8_0 质量好。可试 q5_0/q4_0 **省显存**（代价：长上下文质量略降）。 |
| `-ctk, --cache-type-k TYPE` | q8_0 | ⚪ | 单独设 K 缓存类型。 |
| `-ctv, --cache-type-v TYPE` | q8_0 | ⚪ | 单独设 V 缓存类型（可独立 q8_0/q5_0/q4_0）。 |

**研判优先级：低-中。** 显存紧时可降 q5_0 换空间；当前 q8_0 已平衡。

---

## ⑥ 思考链 / 聊天模板（9 项，🔵）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `--jinja` | 总是开 | 🟢 | 原生 Jinja 模板渲染。无需管。 |
| `--chat-template TEMPLATE` | 模型自带 | ⚪ | 覆盖聊天模板。一般不需要。 |
| `--chat-template-file PATH` | — | ⚪ | 从文件加载模板。 |
| `--chat-template-kwargs JSON` | — | ⚪ | 模板默认参数。 |
| `--reasoning-effort LEVEL` | 模板默认 | ⚪ | 思考力度。可设 low/medium/high。 |
| `--enable-thinking` | 关 | ⚪ | **开启 Qwen 思考链。** 27B 开了思考质量明显提升，但更慢更费 token。值得试。 |
| `--no-think` | — | ⚪ | 强制关思考。要快回答时用。 |
| `--reasoning-budget N` | -1(无限) | ⚪ | 思考 token 预算。-1 不限；设 N 强制 N 后结束思考。防思考跑飞可设 1024。 |
| `--reasoning-budget-message MSG` | 无 | ⚪ | 强制结束思考前注入的提示。 |

**研判优先级：高。** `--enable-thinking` + `--reasoning-budget` 是提升 27B 回答质量的关键开关。

---

## ⑦ 推测解码（4 项，⚫ 基本锁死）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `--spec-type TYPE` | none | 🟢none | 推测解码类型。draft-mtp 需模型带 MTP 头，Bonsai 没有 → **只能 none**。 |
| `--spec-kv-dtype TYPE` | f16 | ⚪ | MTP 的 KV 类型。因 spec-type=none 不生效。 |
| `--spec-draft-n-max N` | 3 | ⚪ | MTP 草稿 token 数。不生效。 |
| `--spec-draft-p-min P` | 0 | ⚪ | 最小草稿概率。不生效。 |

**研判优先级：无。** 之前已确认：模型无 MTP 头，推测解码这条路在 4060 上走不通。

---

## ⑧ 服务 / 网络（6 项）

| 参数 | 默认 | 当前 | 作用 / 研判 |
|---|---|---|---|
| `--host HOST` | 127.0.0.1 | 🟢127.0.0.1 | 绑定地址。127.0.0.1=仅本机。要走公网隧道再议。 |
| `--port N` | 8080 | 🟢18200 | 服务端口。已定。 |
| `--ui-dir PATH` | — | ⚪ | 托管静态 UI 目录。你用自建 8787，不用管。 |
| `--no-ui` | — | ⚪ | 关掉引擎自带 UI。 |
| `--timeout, -to N` | 1800 | ⚪ | 见④。 |
| `--threads-http N` | 自动 | ⚪ | 见④。 |

**研判优先级：中。** `--api-key`(在④) 是公网暴露前的必选项。

---

## ⑨ 日志诊断（3 项）

| 参数 | 默认 | 当前 | 作用 |
|---|---|---|---|
| `-lv, --verbosity N` | 3(info) | ⚪ | 日志级别 0~5。调试设 4/5，跑顺设 2。 |
| `--kvmem-trace` | 关 | 🟢开 | 见①。 |
| `--no-kvmem-trace` | — | ⚪ | 关诊断。 |

**研判优先级：低。** 跑顺了把 trace 关掉减日志噪音。

---

## ⑩ WebUI（3 项）

| 参数 | 默认 | 当前 | 作用 |
|---|---|---|---|
| `--webui` | 开 | ⚪ | 引擎自带聊天 UI。 |
| `--ui` / `--webui` | — | ⚪ | 别名。 |
| `--no-webui` | — | ⚪ | 关自带 UI（你用 8787 中转，可关）。 |

**研判优先级：低。** 你 8787 是自建中转，引擎 UI 可关可不管。

---

## 建议的研判顺序（由高到低）

1. **① KVMem 核心**：budget / gen-reserve / cpu-gb / method — 直接决定速度与能撑多长。
2. **③ 视觉**：加 `--mmproj` 解锁看图（你最终愿景的拼图）。
3. **⑥ 思考链**：`--enable-thinking` + `--reasoning-budget` 提质量。
4. **④ 性能/安全**：`-b`/`-ub` 提速、`--api-key` 安全、`--timeout` 防卡死、`-fa` 确认长上下文。
5. **② 采样**：temp/repeat-penalty 调输出风格。
6. **⑤ KV 量化**：显存紧时降 q5_0。
7. **⑦⑨⑩**：推测解码(锁死)、日志(跑顺关 trace)、WebUI(不管)。

> 注：所有采样/思考参数也可在**每次请求**时由客户端覆盖，不一定写在启动脚本里。启动级设的是"默认值"。
