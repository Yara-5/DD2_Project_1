"""Draw the decomposition tree of a network.json (synthesize.py's second picture).

The tree is f's AND/OR formula as it is implemented. Each stage is a shaded box
around the part of the tree it builds; a sub-stage's box sits inside the box of the
stage that uses its output. For every operator the label says what it becomes in
that stage's pull-down (series or parallel); for every literal, the literal in f's
formula and the net that actually drives the gate. A dashed edge into a sub-stage
marks a cut: "flipped" when the sub-stage is built as the De Morgan dual of its
parent (every literal below needs the opposite polarity, no inverter), or an
inverter node when the sub-stage keeps its form and an inverter follows it.
Read only from network.json; decides nothing.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

DX, DY = 1.7, 1.45
INK = '#1f2933'
PALETTE = ['#4c6ef5', '#e8590c', '#2b8a3e', '#ae3ec9', '#c92a2a', '#1098ad', '#5f3dc4', '#d9480f']


def layout(node, depth, cursor, out):
    """Leaves left to right; a parent sits above the middle of its children."""
    children = node.get('children', [])
    if not children:
        x = cursor[0] * DX
        cursor[0] += 1
    else:
        xs = [layout(c, depth + 1, cursor, out) for c in children]
        x = sum(xs) / len(xs)
    out.append((node, x, -depth * DY))
    node['_xy'] = (x, -depth * DY)
    return x


def subtree(node):
    yield node
    for c in node.get('children', []):
        yield from subtree(c)


def stage_roots(node, level=0, parent_stage=None):
    """Nodes where a stage starts, with their nesting level."""
    if node['stage'] != parent_stage:
        yield node, level
        level += 1
    for c in node.get('children', []):
        yield from stage_roots(c, level, node['stage'])


def draw_tree(network, path):
    tree = network['decomposition_tree']
    placed = []
    layout(tree, 0, [0], placed)
    stages = {s['output']: s for s in network['stages']}
    order = [s['output'] for s in network['stages']]
    color = {net: PALETTE[i % len(PALETTE)] for i, net in enumerate(order)}

    xs = [x for _, x, _ in placed]
    ys = [y for _, _, y in placed]
    width = max(xs) - min(xs) + 2 * DX + 4.5
    height = -min(ys) + 2.6
    fig, ax = plt.subplots(figsize=(max(7.0, width * 1.05), max(4.0, height * 1.1)))

    # Stage boxes, outermost first, so inner ones are drawn on top.
    for root, level in sorted(stage_roots(tree), key=lambda r: r[1]):
        pts = [n['_xy'] for n in subtree(root)]
        pad = max(0.62 - 0.14 * level, 0.2)
        x0, x1 = min(p[0] for p in pts) - pad - 0.35, max(p[0] for p in pts) + pad + 0.35
        # Extra headroom above the stage's top node for its label.
        y0, y1 = min(p[1] for p in pts) - pad - 0.35, max(p[1] for p in pts) + pad + 0.3
        c = color[root['stage']]
        ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0, boxstyle='round,pad=0.02,rounding_size=0.25',
                                    fc=c, ec=c, alpha=0.10, lw=0))
        ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0, boxstyle='round,pad=0.02,rounding_size=0.25',
                                    fc='none', ec=c, lw=1.4))
        st = stages[root['stage']]
        ax.text(x0 + 0.12, y1 - 0.08, f"stage {st['output']}  ({st['form']})", color=c,
                fontsize=8.5, weight='bold', va='top')

    # Edges; a dashed edge enters a sub-stage.
    for node, x, y in placed:
        for ch in node.get('children', []):
            cx, cy = ch['_xy']
            cut = ch.get('cut') if ch['stage'] != node['stage'] else None
            ax.plot([x, cx], [y - 0.28, cy + 0.3], color=INK, lw=1.1, ls='--' if cut else '-', zorder=2)
            if cut == 'flip':
                ax.text((x + cx) / 2 + 0.08, (y + cy) / 2, 'flipped', fontsize=7.5, color='#52606d',
                        style='italic', zorder=5)

    # Nodes.
    for node, x, y in placed:
        c = color[node['stage']]
        if node['kind'] == 'op':
            ax.add_patch(plt.Circle((x, y), 0.32, fc='white', ec=c, lw=1.8, zorder=3))
            ax.text(x, y, node['op'].upper(), ha='center', va='center', fontsize=8.5, weight='bold',
                    color=c, zorder=4)
            ax.text(x + 0.38, y + 0.18, node['pull_down'], fontsize=7, color='#52606d', zorder=4)
        elif node['kind'] == 'inv':
            ax.add_patch(plt.Polygon([(x - 0.3, y + 0.25), (x - 0.3, y - 0.25), (x + 0.22, y)],
                                     fc='white', ec=c, lw=1.8, zorder=3))
            ax.add_patch(plt.Circle((x + 0.28, y), 0.06, fc='white', ec=c, lw=1.4, zorder=3))
            ax.text(x + 0.42, y + 0.18, f"INV -> {node['stage']}", fontsize=7.5, color=c, zorder=4)
        else:
            ax.add_patch(FancyBboxPatch((x - 0.36, y - 0.22), 0.72, 0.44,
                                        boxstyle='round,pad=0.02,rounding_size=0.1',
                                        fc='white', ec=c, lw=1.5, zorder=3))
            ax.text(x, y, node['text'], ha='center', va='center', fontsize=9.5, color=INK, zorder=4)
            note = node['gate'] + (' (input inv.)' if node['gate'].endswith('_N') else '')
            ax.text(x, y - 0.42, f"gate {note}", ha='center', va='top', fontsize=7, color='#52606d', zorder=4)

    # Output pin above the root.
    rx, ry = tree['_xy']
    ax.plot([rx, rx], [ry + 0.3, ry + 0.95], color=INK, lw=1.1)
    ax.text(rx, ry + 1.05, network['output'], ha='center', va='bottom', fontsize=11, weight='bold', color=INK)

    # Stage list, in signal order.
    lines = [f"{s['function']}   [{s['form']}, {s['transistors']} transistors, "
             f"series {s['pull_down_series']}/{s['pull_up_series']}]" for s in network['stages']]
    ax.text(max(xs) + DX * 0.9, 0.6, 'Stages (pull-down / pull-up series):\n' + '\n'.join(lines),
            fontsize=8, va='top', family='monospace', color=INK)
    c = network['cost']
    ax.set_title(f"{network['name']}: decomposition of {network['formula']}\n"
                 f"{c['stages']} stages, {c['transistors']} transistors, "
                 f"{c['inverters']} inverter{'s' if c['inverters'] != 1 else ''} added "
                 f"({c['input_inverters']} on inputs)", fontsize=10.5, loc='left')
    ax.set_xlim(min(xs) - DX, max(xs) + DX * 0.9 + 9.5)
    ax.set_ylim(min(ys) - 1.3, 1.9)
    ax.set_aspect('equal')
    ax.axis('off')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    for node in subtree(tree):
        node.pop('_xy', None)
