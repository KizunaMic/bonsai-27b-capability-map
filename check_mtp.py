PATH = r"E:\ninfer-kvmem\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
TOKENS = [b"mtp", b"nextn", b"draft", b"ssm", b"gated_delta", b"replay", b"spec"]

with open(PATH, "rb") as f:
    head = f.read(64 * 1024 * 1024)  # first 64 MiB: metadata + tensor info + start of weights

low = head.lower()
print(f"scanned {len(head)/1024/1024:.1f} MiB")
found = {}
for tok in TOKENS:
    idxs = []
    start = 0
    while True:
        i = low.find(tok, start)
        if i < 0:
            break
        # extract a readable window around the hit
        s = max(0, i - 20)
        e = min(len(head), i + 40)
        snippet = head[s:e].replace(b"\x00", b" ").decode("latin-1", "replace")
        idxs.append(snippet)
        start = i + 1
        if len(idxs) >= 8:
            break
    if idxs:
        found[tok.decode()] = idxs

if not found:
    print("RESULT: NO MTP / draft / SSM markers found -> model has NO speculative head")
else:
    for k, v in found.items():
        print(f"\n### token '{k}' ({len(v)} shown)")
        for sn in v:
            print("   ...", sn, "...")
