#!/usr/bin/env python3
"""
Vergelijk latency sessies uit latency_log.csv.
Gebruik: python3 compare_sessions.py [--csv latency_log.csv] [--edge rise|fall|both]
"""
import csv, statistics, argparse, os, collections

parser = argparse.ArgumentParser()
parser.add_argument("--csv",  default=os.path.join(os.path.dirname(__file__), "latency_log.csv"))
parser.add_argument("--edge", default="rise", choices=["rise","fall","both"])
parser.add_argument("--min-n", type=int, default=5, help="Minimaal N metingen per sessie")
args = parser.parse_args()

if not os.path.exists(args.csv):
    print(f"Geen CSV gevonden: {args.csv}")
    exit(1)

rows = list(csv.DictReader(open(args.csv)))

# Groepeer per sessie
sessions = collections.defaultdict(list)
for r in rows:
    edge = r.get("edge","rise")
    if args.edge == "both" or edge == args.edge:
        try:
            sessions[r.get("session", "?")].append(float(r["latency_ms"]))
        except: pass

if not sessions:
    print("Geen data gevonden.")
    exit(1)

print(f"\n{'Sessie':<20} {'n':>4} {'mean':>8} {'median':>8} {'stdev':>8} {'min':>8} {'max':>8}  {'bar'}")
print("-" * 100)

# Sorteer op volgorde van eerste optreden
order = list(dict.fromkeys(r.get("session","?") for r in rows))
baseline_mean = None

for tag in order:
    vals = sessions.get(tag, [])
    if len(vals) < args.min_n:
        print(f"{tag:<20} {'(te weinig data: '+str(len(vals))+')':>50}")
        continue
    m   = statistics.mean(vals)
    med = statistics.median(vals)
    sd  = statistics.stdev(vals) if len(vals) > 1 else 0
    lo  = min(vals)
    hi  = max(vals)

    if baseline_mean is None:
        baseline_mean = m
        diff_str = " (baseline)"
    else:
        diff = m - baseline_mean
        diff_str = f" ({diff:+.1f}ms)"

    bar_len = max(1, int(m / 5))
    bar = "█" * min(bar_len, 50)

    print(f"{tag:<20} {len(vals):>4} {m:>7.1f}ms {med:>7.1f}ms {sd:>7.1f}ms {lo:>7.1f}ms {hi:>7.1f}ms  {bar}{diff_str}")

print()
if baseline_mean:
    print(f"Baseline ({order[0]}): {baseline_mean:.1f}ms")
    last_tag = [t for t in order if len(sessions.get(t,[])) >= args.min_n]
    if len(last_tag) > 1:
        last_mean = statistics.mean(sessions[last_tag[-1]])
        print(f"Laatste  ({last_tag[-1]}): {last_mean:.1f}ms  →  verschil: {last_mean-baseline_mean:+.1f}ms")
