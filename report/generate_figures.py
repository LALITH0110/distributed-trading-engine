#!/usr/bin/env python3
"""Generate all benchmark figures for the report. Outputs PDF + PNG."""

import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import matplotlib.ticker as ticker

# ── Style ─────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    'font.family': 'sans-serif',
    'font.size': 11,
    'axes.titlesize': 13,
    'axes.titleweight': 'bold',
    'axes.labelsize': 12,
    'figure.facecolor': 'white',
    'axes.facecolor': '#FAFAFA',
    'axes.grid': True,
    'grid.alpha': 0.3,
    'grid.linestyle': '--',
    'legend.framealpha': 0.9,
    'legend.edgecolor': '#CCCCCC',
})

COLORS = {
    'primary': '#2563EB',    # blue
    'secondary': '#7C3AED',  # purple
    'accent': '#059669',     # green
    'warm': '#DC2626',       # red
    'orange': '#EA580C',
    'gbm': '#2563EB',
    'stress': '#DC2626',
    1: '#2563EB',
    2: '#7C3AED',
    4: '#059669',
    8: '#EA580C',
}

# ── Load data ─────────────────────────────────────────────────────────────────
with open('benchmark_results.json') as f:
    data = json.load(f)

def get_valid_trials(scenario, n_nodes):
    """Get trials that have valid metrics_raw."""
    trials = data[scenario][str(n_nodes)]
    return [t for t in trials if t.get('metrics_raw') and t['metrics_raw'].get('orders', 0) > 0]

NODE_COUNTS = [1, 2, 4, 8]
SCENARIOS = ['gbm', 'stress']


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 1: Throughput Scaling (Bar Chart with Error Bars)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 6))

x = np.arange(len(NODE_COUNTS))
width = 0.35

for i, scenario in enumerate(SCENARIOS):
    means, stds = [], []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        tp = [t['metrics_raw']['throughput'] for t in trials]
        means.append(np.mean(tp) if tp else 0)
        stds.append(np.std(tp) if tp else 0)

    bars = ax.bar(x + i * width - width/2, means, width,
                  yerr=stds, capsize=5, label=scenario.upper(),
                  color=COLORS[scenario], alpha=0.85, edgecolor='white', linewidth=0.5)

    for bar, mean in zip(bars, means):
        if mean > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 5,
                    f'{mean:.0f}', ha='center', va='bottom', fontsize=9, fontweight='bold')

ax.set_xlabel('Number of Strategy Nodes')
ax.set_ylabel('Throughput (orders/sec)')
ax.set_title('Throughput Scaling: 1 to 8 Distributed Strategy Nodes')
ax.set_xticks(x)
ax.set_xticklabels(NODE_COUNTS)
ax.legend(loc='upper left')
ax.set_ylim(0, 310)

# Add scaling annotation
ax.annotate('2.57x scaling\n(1 → 8 nodes)',
            xy=(3.175, 226), xytext=(2.5, 280),
            fontsize=10, fontweight='bold', color=COLORS['primary'],
            arrowprops=dict(arrowstyle='->', color=COLORS['primary'], lw=1.5))

fig.tight_layout()
fig.savefig('fig1_throughput_scaling.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig1_throughput_scaling.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig1_throughput_scaling.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 2: Latency Percentiles (Grouped Bar Chart)
# ══════════════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=False)

for ax_idx, scenario in enumerate(SCENARIOS):
    ax = axes[ax_idx]
    percentiles = ['p50_us', 'p95_us', 'p99_us']
    labels = ['p50', 'p95', 'p99']
    x = np.arange(len(NODE_COUNTS))
    width = 0.25

    for j, (pct, label) in enumerate(zip(percentiles, labels)):
        vals = []
        for n in NODE_COUNTS:
            trials = get_valid_trials(scenario, n)
            v = [t['metrics_raw'][pct] for t in trials]
            vals.append(np.mean(v) if v else 0)

        color_intensity = [0.6, 0.8, 1.0][j]
        bars = ax.bar(x + j * width - width, vals, width,
                      label=label, color=COLORS[scenario],
                      alpha=color_intensity, edgecolor='white', linewidth=0.5)

    ax.set_xlabel('Number of Strategy Nodes')
    ax.set_ylabel('Latency (us)')
    ax.set_title(f'{scenario.upper()} Scenario — Latency Percentiles')
    ax.set_xticks(x)
    ax.set_xticklabels(NODE_COUNTS)
    ax.legend()
    ax.set_yscale('log')
    ax.set_ylim(10, 50000)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda y, _: f'{y:,.0f}'))

fig.tight_layout()
fig.savefig('fig2_latency_percentiles.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig2_latency_percentiles.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig2_latency_percentiles.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 3: p50 Latency Stability Across Scales (Line Chart)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 5))

for scenario in SCENARIOS:
    p50_means, p50_stds = [], []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        p50s = [t['metrics_raw']['p50_us'] for t in trials]
        p50_means.append(np.mean(p50s) if p50s else 0)
        p50_stds.append(np.std(p50s) if p50s else 0)

    ax.errorbar(NODE_COUNTS, p50_means, yerr=p50_stds,
                marker='o', markersize=8, linewidth=2.5, capsize=5,
                label=f'{scenario.upper()}', color=COLORS[scenario])

# Add horizontal band showing "sub-100us zone"
ax.axhspan(0, 100, alpha=0.08, color='green')
ax.axhline(y=100, color='green', linestyle=':', alpha=0.5, linewidth=1)
ax.text(7.5, 102, 'Sub-100 us', fontsize=9, color='green', alpha=0.7, ha='right')

ax.set_xlabel('Number of Strategy Nodes')
ax.set_ylabel('Median Latency — p50 (us)')
ax.set_title('Median Latency Remains Stable at ~88 us Regardless of Node Count')
ax.set_xticks(NODE_COUNTS)
ax.set_ylim(60, 120)
ax.legend()

fig.tight_layout()
fig.savefig('fig3_p50_stability.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig3_p50_stability.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig3_p50_stability.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 4: Orders & Fills per Configuration (Stacked/Grouped)
# ══════════════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(1, 2, figsize=(14, 6))

for ax_idx, scenario in enumerate(SCENARIOS):
    ax = axes[ax_idx]
    x = np.arange(len(NODE_COUNTS))
    width = 0.4

    orders_mean, fills_mean = [], []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        orders_mean.append(np.mean([t['metrics_raw']['orders'] for t in trials]) if trials else 0)
        fills_mean.append(np.mean([t['metrics_raw']['fills'] for t in trials]) if trials else 0)

    bars1 = ax.bar(x - width/2, orders_mean, width, label='Orders',
                   color=COLORS[scenario], alpha=0.85, edgecolor='white')
    bars2 = ax.bar(x + width/2, fills_mean, width, label='Fills',
                   color=COLORS['accent'], alpha=0.85, edgecolor='white')

    for bar, val in zip(bars1, orders_mean):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 100,
                    f'{val:,.0f}', ha='center', va='bottom', fontsize=8, fontweight='bold')
    for bar, val in zip(bars2, fills_mean):
        if val > 10:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 100,
                    f'{val:,.0f}', ha='center', va='bottom', fontsize=8, fontweight='bold',
                    color=COLORS['accent'])

    ax.set_xlabel('Number of Strategy Nodes')
    ax.set_ylabel('Count (per trial)')
    ax.set_title(f'{scenario.upper()} — Orders Processed vs Fills')
    ax.set_xticks(x)
    ax.set_xticklabels(NODE_COUNTS)
    ax.legend()

fig.tight_layout()
fig.savefig('fig4_orders_fills.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig4_orders_fills.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig4_orders_fills.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 5: Tail Latency (p99/p99.9) vs Node Count
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 6))

markers = {'p99_us': 's', 'p999_us': '^'}
labels = {'p99_us': 'p99', 'p999_us': 'p99.9'}
line_styles = {'p99_us': '-', 'p999_us': '--'}

for scenario in SCENARIOS:
    for pct in ['p99_us', 'p999_us']:
        vals = []
        for n in NODE_COUNTS:
            trials = get_valid_trials(scenario, n)
            v = [t['metrics_raw'][pct] for t in trials]
            vals.append(np.mean(v) if v else 0)

        ax.plot(NODE_COUNTS, vals,
                marker=markers[pct], markersize=7, linewidth=2,
                linestyle=line_styles[pct],
                label=f'{scenario.upper()} {labels[pct]}',
                color=COLORS[scenario],
                alpha=0.8 if pct == 'p99_us' else 0.6)

ax.set_xlabel('Number of Strategy Nodes')
ax.set_ylabel('Latency (us) — log scale')
ax.set_title('Tail Latency Growth at Scale (p99 and p99.9)')
ax.set_xticks(NODE_COUNTS)
ax.set_yscale('log')
ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda y, _: f'{y:,.0f}'))
ax.legend(ncol=2, loc='upper left')

# Annotate the 8-node spike
ax.annotate('ZMQ contention\n+ GC pauses',
            xy=(8, 8644), xytext=(5, 30000),
            fontsize=9, fontstyle='italic', color='gray',
            arrowprops=dict(arrowstyle='->', color='gray', lw=1))

fig.tight_layout()
fig.savefig('fig5_tail_latency.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig5_tail_latency.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig5_tail_latency.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 6: Per-Trial Throughput Distribution (Box Plot)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 6))

positions = []
bp_data = []
colors_list = []
labels_list = []

pos = 0
for n in NODE_COUNTS:
    for scenario in SCENARIOS:
        trials = get_valid_trials(scenario, n)
        tp = [t['metrics_raw']['throughput'] for t in trials]
        if tp:
            bp_data.append(tp)
            positions.append(pos)
            colors_list.append(COLORS[scenario])
        pos += 1
    pos += 0.5  # gap between node groups

bp = ax.boxplot(bp_data, positions=positions, widths=0.7, patch_artist=True,
                showmeans=True, meanline=True,
                meanprops=dict(color='black', linewidth=1.5, linestyle='--'),
                medianprops=dict(color='white', linewidth=1.5),
                flierprops=dict(marker='o', markersize=4))

for patch, color in zip(bp['boxes'], colors_list):
    patch.set_facecolor(color)
    patch.set_alpha(0.75)

# Custom x-axis
group_centers = []
pos = 0
for n in NODE_COUNTS:
    group_centers.append(pos + 0.5)
    pos += 2.5

ax.set_xticks(group_centers)
ax.set_xticklabels([f'{n} node{"s" if n > 1 else ""}' for n in NODE_COUNTS])
ax.set_ylabel('Throughput (orders/sec)')
ax.set_title('Throughput Distribution Across Trials (5 trials per config)')

# Custom legend
from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(facecolor=COLORS['gbm'], alpha=0.75, label='GBM'),
    Patch(facecolor=COLORS['stress'], alpha=0.75, label='Stress'),
], loc='upper left')

fig.tight_layout()
fig.savefig('fig6_throughput_boxplot.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig6_throughput_boxplot.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig6_throughput_boxplot.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 7: Architecture Diagram (Programmatic)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(14, 8))
ax.set_xlim(0, 14)
ax.set_ylim(0, 10)
ax.set_aspect('equal')
ax.axis('off')

def draw_box(ax, x, y, w, h, label, sublabel='', color='#2563EB'):
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.15",
                         facecolor=color, alpha=0.15, edgecolor=color, linewidth=2)
    ax.add_patch(box)
    ax.text(x + w/2, y + h/2 + 0.15, label, ha='center', va='center',
            fontsize=11, fontweight='bold', color=color)
    if sublabel:
        ax.text(x + w/2, y + h/2 - 0.25, sublabel, ha='center', va='center',
                fontsize=8, color='gray')

def arrow(ax, x1, y1, x2, y2, label='', color='#666'):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle='->', color=color, lw=1.5))
    if label:
        mx, my = (x1+x2)/2, (y1+y2)/2
        ax.text(mx, my + 0.2, label, ha='center', va='bottom',
                fontsize=7, color=color, fontstyle='italic')

# Title
ax.text(7, 9.5, 'Distributed Trading Engine Architecture', ha='center',
        fontsize=16, fontweight='bold', color='#1a1a1a')
ax.text(7, 9.1, 'FABRIC Testbed  |  10 Nodes  |  L2 Data Plane (10.1.0.0/24)',
        ha='center', fontsize=10, color='gray')

# Feed Handler
draw_box(ax, 0.5, 6, 3, 1.5, 'Feed Handler', '10.1.0.1  |  GBM + Stress\n10K ticks/sec  |  5 assets', '#059669')

# Strategy Nodes
for i in range(4):
    y = 7.5 - i * 1.3
    draw_box(ax, 5, y, 2.8, 0.9, f'Strategy {i*2}-{i*2+1}',
             f'10.1.0.{10+i*2}  |  z-score', '#7C3AED')

# Risk Gateway
draw_box(ax, 9, 5.5, 3, 1.5, 'Risk Gateway',
         'Fat-finger | Position | Rate\nKill Switch  |  10.1.0.2', '#EA580C')

# Matching Engine
draw_box(ax, 9, 2.5, 3, 2, 'Matching Engine',
         'Ring Buffer -> LOB\nSortedDict price-time\nExec Reports (FIX-lite)\n10.1.0.2', '#2563EB')

# Arrows: Feed -> Strategies
for i in range(4):
    y = 7.5 - i * 1.3 + 0.45
    arrow(ax, 3.5, 6.75, 5, y, 'PUB/SUB' if i == 0 else '', '#059669')

# Arrows: Strategies -> Risk
for i in range(4):
    y = 7.5 - i * 1.3 + 0.45
    arrow(ax, 7.8, y, 9, 6.25, 'PUSH' if i == 1 else '', '#7C3AED')

# Arrow: Risk -> ME
arrow(ax, 10.5, 5.5, 10.5, 4.5, 'PULL (validated)', '#EA580C')

# Arrow: ME -> Strategies (exec reports)
ax.annotate('', xy=(7.8, 4.3), xytext=(9, 3.8),
            arrowprops=dict(arrowstyle='->', color='#2563EB', lw=1.5, linestyle='--'))
ax.text(8.2, 4.4, 'PUB exec reports', fontsize=7, color='#2563EB', fontstyle='italic')

# Network label
ax.text(7, 1.5, 'FABRIC L2 Network  (NIC_Basic, enp7s0, no NAT/firewall)',
        ha='center', fontsize=10, fontweight='bold', color='#666',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#F0F0F0', edgecolor='#CCC'))

# Key metrics callout
ax.text(1.5, 1.5, 'p50 = 88 us\n226 orders/sec @ 8 nodes\nSub-100us median at all scales',
        ha='center', fontsize=9, fontweight='bold', color='#2563EB',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='#EFF6FF', edgecolor='#2563EB', alpha=0.8))

fig.tight_layout()
fig.savefig('fig7_architecture.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig7_architecture.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig7_architecture.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 8: Scaling Efficiency (Ideal vs Actual)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 6))

gbm_tp = []
for n in NODE_COUNTS:
    trials = get_valid_trials('gbm', n)
    tp = [t['metrics_raw']['throughput'] for t in trials]
    gbm_tp.append(np.mean(tp) if tp else 0)

base = gbm_tp[0]  # 1-node throughput
ideal = [base * n for n in NODE_COUNTS]
actual = gbm_tp

ax.plot(NODE_COUNTS, ideal, '--', color='gray', linewidth=2, marker='D',
        markersize=6, label='Ideal Linear Scaling', alpha=0.6)
ax.plot(NODE_COUNTS, actual, '-', color=COLORS['primary'], linewidth=2.5,
        marker='o', markersize=8, label='Actual (GBM)')

# Fill between
ax.fill_between(NODE_COUNTS, actual, ideal, alpha=0.1, color=COLORS['primary'])

# Efficiency labels
for i, (n, a, ideal_v) in enumerate(zip(NODE_COUNTS, actual, ideal)):
    eff = (a / ideal_v) * 100 if ideal_v > 0 else 0
    if n > 1:
        ax.annotate(f'{eff:.0f}% efficient',
                    xy=(n, a), xytext=(n + 0.3, a + 20),
                    fontsize=9, color=COLORS['primary'],
                    arrowprops=dict(arrowstyle='->', color=COLORS['primary'], lw=1))

ax.set_xlabel('Number of Strategy Nodes')
ax.set_ylabel('Throughput (orders/sec)')
ax.set_title('Scaling Efficiency: Actual vs Ideal Linear Scaling (GBM)')
ax.set_xticks(NODE_COUNTS)
ax.legend(loc='upper left')

# Note about bottleneck
ax.text(5, 50, 'Bottleneck: single-threaded matching engine\n(production systems shard by symbol for linear scaling)',
        fontsize=9, fontstyle='italic', color='gray',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#FAFAFA', edgecolor='#DDD'))

fig.tight_layout()
fig.savefig('fig8_scaling_efficiency.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig8_scaling_efficiency.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig8_scaling_efficiency.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 9: Latency Heatmap (Scenarios x Nodes x Percentiles)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 5))

pcts = ['p50_us', 'p95_us', 'p99_us', 'p999_us']
pct_labels = ['p50', 'p95', 'p99', 'p99.9']
row_labels = []
heatmap_data = []

for scenario in SCENARIOS:
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        row = []
        for pct in pcts:
            v = [t['metrics_raw'][pct] for t in trials]
            row.append(np.mean(v) if v else 0)
        heatmap_data.append(row)
        row_labels.append(f'{scenario.upper()} {n}N')

heatmap_data = np.array(heatmap_data)
# Use log scale for color
log_data = np.log10(np.clip(heatmap_data, 1, None))

im = ax.imshow(log_data, cmap='YlOrRd', aspect='auto')

ax.set_xticks(range(len(pct_labels)))
ax.set_xticklabels(pct_labels)
ax.set_yticks(range(len(row_labels)))
ax.set_yticklabels(row_labels)

# Annotate cells with actual values
for i in range(len(row_labels)):
    for j in range(len(pct_labels)):
        val = heatmap_data[i][j]
        if val >= 1000:
            text = f'{val/1000:.1f}ms'
        else:
            text = f'{val:.0f}us'
        color = 'white' if log_data[i][j] > 3.5 else 'black'
        ax.text(j, i, text, ha='center', va='center', fontsize=9,
                fontweight='bold', color=color)

ax.set_title('Latency Heatmap: All Configurations and Percentiles')
cbar = fig.colorbar(im, ax=ax, shrink=0.8)
cbar.set_label('log10(latency us)')

fig.tight_layout()
fig.savefig('fig9_latency_heatmap.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig9_latency_heatmap.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig9_latency_heatmap.pdf/png')


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE 10: Summary Dashboard (Multi-panel)
# ══════════════════════════════════════════════════════════════════════════════
fig = plt.figure(figsize=(16, 10))
fig.suptitle('Distributed Trading Engine — Benchmark Summary Dashboard',
             fontsize=16, fontweight='bold', y=0.98)

# Panel 1: Throughput
ax1 = fig.add_subplot(2, 2, 1)
for scenario in SCENARIOS:
    means = []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        tp = [t['metrics_raw']['throughput'] for t in trials]
        means.append(np.mean(tp) if tp else 0)
    ax1.plot(NODE_COUNTS, means, '-o', label=scenario.upper(),
             color=COLORS[scenario], linewidth=2.5, markersize=8)
ax1.set_title('Throughput vs Scale')
ax1.set_xlabel('Nodes')
ax1.set_ylabel('Orders/sec')
ax1.set_xticks(NODE_COUNTS)
ax1.legend()

# Panel 2: p50 Latency
ax2 = fig.add_subplot(2, 2, 2)
for scenario in SCENARIOS:
    p50s = []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        v = [t['metrics_raw']['p50_us'] for t in trials]
        p50s.append(np.mean(v) if v else 0)
    ax2.plot(NODE_COUNTS, p50s, '-s', label=scenario.upper(),
             color=COLORS[scenario], linewidth=2.5, markersize=8)
ax2.axhline(y=100, color='green', linestyle=':', alpha=0.5)
ax2.set_title('Median Latency (p50)')
ax2.set_xlabel('Nodes')
ax2.set_ylabel('Latency (us)')
ax2.set_xticks(NODE_COUNTS)
ax2.set_ylim(60, 120)
ax2.legend()

# Panel 3: Orders & Fills
ax3 = fig.add_subplot(2, 2, 3)
x = np.arange(len(NODE_COUNTS))
width = 0.2
for i, scenario in enumerate(SCENARIOS):
    orders, fills = [], []
    for n in NODE_COUNTS:
        trials = get_valid_trials(scenario, n)
        orders.append(np.mean([t['metrics_raw']['orders'] for t in trials]) if trials else 0)
        fills.append(np.mean([t['metrics_raw']['fills'] for t in trials]) if trials else 0)
    ax3.bar(x + i * width * 2 - width, orders, width, label=f'{scenario.upper()} Orders',
            color=COLORS[scenario], alpha=0.8)
    ax3.bar(x + i * width * 2, fills, width, label=f'{scenario.upper()} Fills',
            color=COLORS[scenario], alpha=0.4)
ax3.set_title('Orders & Fills per Trial')
ax3.set_xlabel('Nodes')
ax3.set_ylabel('Count')
ax3.set_xticks(x)
ax3.set_xticklabels(NODE_COUNTS)
ax3.legend(fontsize=8)

# Panel 4: Key Stats Callout
ax4 = fig.add_subplot(2, 2, 4)
ax4.axis('off')

stats = [
    ('88 us', 'Median Latency (p50)', COLORS['primary']),
    ('226 orders/sec', 'Peak Throughput (8 nodes, GBM)', COLORS['accent']),
    ('2.57x', 'Scaling Factor (1 to 8 nodes)', COLORS['secondary']),
    ('< 190 us', 'p95 Latency (all configs)', COLORS['orange']),
    ('40 trials', 'Benchmark Runs (statistically robust)', '#666'),
    ('10 nodes', 'FABRIC EDUKY Deployment', '#666'),
]

for i, (value, label, color) in enumerate(stats):
    y = 0.85 - i * 0.15
    ax4.text(0.05, y, value, fontsize=18, fontweight='bold', color=color,
             transform=ax4.transAxes, va='center')
    ax4.text(0.45, y, label, fontsize=11, color='#444',
             transform=ax4.transAxes, va='center')

fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig('fig10_summary_dashboard.pdf', dpi=300, bbox_inches='tight')
fig.savefig('fig10_summary_dashboard.png', dpi=300, bbox_inches='tight')
plt.close()
print('  fig10_summary_dashboard.pdf/png')


print('\nAll figures generated successfully!')
print(f'Output: 10 figures x 2 formats (PDF + PNG) = 20 files')
