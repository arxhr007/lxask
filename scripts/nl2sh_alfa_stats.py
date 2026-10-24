"""Pass rates, Wilson 95% intervals and paired McNemar tests from the scores_<variant>.json files."""
import json
import math
import os
from pathlib import Path
B = Path(os.environ.get("LXASK_BENCH_DIR", Path.home() / "bench-alfa"))
leak = json.load(open(B / "leak.json"))["flags"]
clean = [i for i, f in enumerate(leak) if not (f["nl"] or f["cmd"])]

def load(v):
    p = B / f"scores_{v}.json"
    return {int(k): int(x) for k, x in json.load(open(p)).items()} if p.exists() else {}

def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h

def mcnemar(a, b, idx):
    n10 = sum(1 for i in idx if a[i] and not b[i])
    n01 = sum(1 for i in idx if b[i] and not a[i])
    n = n10 + n01
    if n == 0:
        return n10, n01, 1.0
    k = min(n10, n01)
    p = min(1.0, 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n)
    return n10, n01, p

S = {v: load(v) for v in ("gold", "lxask", "base", "base_clean", "base_fewshot", "no_retry", "no_rag")}
for v, s in S.items():
    if not s:
        print(v, "no scores")
        continue
    for name, idx in (("all", sorted(s)), ("clean", [i for i in clean if i in s])):
        k = sum(s[i] for i in idx)
        lo, hi = wilson(k, len(idx))
        print(f"{v:9s} {name:5s} {k}/{len(idx)} = {k/len(idx):.3f}  95% CI {lo:.3f}-{hi:.3f}")
print("clean subset size", len(clean), "| overlapping", 300 - len(clean))
for other in ("base", "base_clean", "base_fewshot", "no_retry", "no_rag"):
    if S["lxask"] and S[other]:
        for name, idx in (("all", range(300)), ("clean", clean)):
            idx = [i for i in idx if i in S["lxask"] and i in S[other]]
            print("lxask vs", other, name, "only-lxask/only-other/p:", mcnemar(S["lxask"], S[other], idx))
