#!/usr/bin/env bash
# Fast single-project iteration. Usage: bash iter.sh [project] [models...]
PROJ="${1:-gin}"; shift || true
MODELS="${*:-deepseek-v4-flash qwen3.5-flash}"
cd /home/neko/project/graphlens-mcp/benchmarks
find data -name "${PROJ}__graphlens__*.jsonl" -delete 2>/dev/null
echo ">>> $PROJ | models: $MODELS | seeds=1"
BENCH_CONCURRENCY=8 BENCH_SEEDS=1 uv run main.py --projects "$PROJ" --arms graphlens --models $MODELS --seeds 1 --no-setup 2>&1 | grep -E ' OK | ERR ' | tail -3
python3 - "$PROJ" <<'PY'
import json, glob, statistics, sys
proj=sys.argv[1]
def load(g):
    out=[]
    for f in glob.glob(g):
        for l in open(f):
            l=l.strip()
            if l:
                try:
                    r=json.loads(l)
                    if r.get('regime'): out.append(r)
                except: pass
    return out
post=load(f"data/{proj}__graphlens__*.jsonl")
mset=set(r['model'] for r in post)
pre=[r for r in load(f"data_precap_graphlens/{proj}__*.jsonl") if r['model'] in mset]
def sl(rows,reg):
    d=[r for r in rows if not str(r['answer']).startswith('__') and r['regime']==reg]
    a=statistics.mean([r['accuracy'] for r in rows if r['regime']==reg]) if any(r['regime']==reg for r in rows) else 0
    mt=statistics.median([r['total_tokens'] for r in d if r['total_tokens']]) if d else 0
    return a,mt,len(d)
print(f"  {'':12s}{'acc':>7s}{'med_tok':>9s}{'n':>4s}")
for lbl,rows in (("PRE ",pre),("POST",post)):
    for reg in ('SIMPLE','HARD'):
        a,mt,n=sl(rows,reg); print(f"  {lbl+reg:12s}{a:7.3f}{mt:9.0f}{n:4d}")
PY
