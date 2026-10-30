"""Draw a network.json as a transistor-level schematic (synthesize.py's picture).

One panel per stage, in signal order: the pull-up network between the VPWR rail
and the stage output, the pull-down network between the output and VGND. Series
transistors are stacked, parallel ones side by side. Each gate is labelled with
the net that drives it, so stages connect by net name (A_N is an input inverter's
output, n<k> an internal stage). Read only from network.json; decides nothing.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

CELL_W, CELL_H = 1.5, 1.0     # one transistor, in drawing units
GAP = 0.7                     # between the pull-up, the output node and the pull-down
INK = '#1f2933'
COLOR = {'nmos': '#2b6cb0', 'pmos': '#b7472a'}


def size(t):
    if 'gate' in t:
        return CELL_W, CELL_H
    sizes = [size(c) for c in t['children']]
    if t['op'] == 'series':
        return max(w for w, _ in sizes), sum(h for _, h in sizes)
    return sum(w for w, _ in sizes), max(h for _, h in sizes)


def line(ax, xs, ys, **kw):
    ax.plot(xs, ys, color=kw.pop('color', INK), lw=kw.pop('lw', 1.2), solid_capstyle='round', **kw)


def transistor(ax, cx, top, gate, kind):
    """A MOS symbol whose drain/source terminals are (cx, top) and (cx, top - 1)."""
    c = COLOR[kind]
    line(ax, [cx, cx], [top, top - 0.25])
    line(ax, [cx, cx], [top - 0.75, top - 1])
    line(ax, [cx, cx], [top - 0.25, top - 0.75], color=c, lw=2.6)          # channel
    gx = cx - 0.16
    line(ax, [gx, gx], [top - 0.3, top - 0.7], color=c, lw=1.6)             # gate plate
    lead = gx - (0.12 if kind == 'pmos' else 0)
    if kind == 'pmos':
        ax.add_patch(plt.Circle((gx - 0.06, top - 0.5), 0.06, fill=False, ec=c, lw=1.2))
    line(ax, [lead, cx - 0.55], [top - 0.5, top - 0.5], color=c)
    ax.text(cx - 0.6, top - 0.5, gate, ha='right', va='center', fontsize=8.5, color=INK)


def draw(ax, t, cx, top, kind):
    """Draw a series/parallel tree; its terminals are (cx, top) and (cx, top - height)."""
    if 'gate' in t:
        transistor(ax, cx, top, t['gate'], kind)
        return
    w, h = size(t)
    if t['op'] == 'series':
        y = top
        for c in t['children']:
            draw(ax, c, cx, y, kind)
            y -= size(c)[1]
        return
    x = cx - w / 2
    centres = []
    for c in t['children']:
        cw, ch = size(c)
        ccx = x + cw / 2
        draw(ax, c, ccx, top, kind)
        if ch < h:
            line(ax, [ccx, ccx], [top - ch, top - h])
        centres.append(ccx)
        x += cw
    line(ax, [min(centres), max(centres)], [top, top])
    line(ax, [min(centres), max(centres)], [top - h, top - h])


def draw_network(network, path):
    stages = network['stages']
    panels, x = [], 0.0
    for st in stages:
        wu, hu = size(st['pull_up'])
        wd, hd = size(st['pull_down'])
        width = max(wu, wd) + 1.6
        panels.append((st, x + width / 2 + 0.3, hu, hd, width))
        x += width
    height = max(hu + hd for _, _, hu, hd, _ in panels) + 2 * GAP
    fig, ax = plt.subplots(figsize=(max(5.0, x * 0.95), (height + 2.2) * 0.85))
    for st, cx, hu, hd, width in panels:
        vpwr = height                     # every stage shares the same two rails
        out_y = (hd + vpwr - hu) / 2
        left, right = cx - width / 2 + 0.15, cx + width / 2 - 0.15
        line(ax, [left, right], [vpwr, vpwr], lw=2)
        line(ax, [left, right], [0, 0], lw=2)
        ax.text(left, vpwr + 0.12, 'VPWR', fontsize=7.5, color='#52606d')
        ax.text(left, -0.3, 'VGND', fontsize=7.5, color='#52606d')
        draw(ax, st['pull_up'], cx, vpwr, 'pmos')
        draw(ax, st['pull_down'], cx, hd, 'nmos')
        line(ax, [cx, cx], [hd, vpwr - hu])
        line(ax, [cx, right - 0.1], [out_y, out_y])
        ax.plot([cx], [out_y], 'o', color=INK, ms=3.5)
        ax.text(right - 0.1, out_y + 0.12, st['output'], ha='right', fontsize=10, weight='bold', color=INK)
        ax.text(cx, height + 0.35, st['function'], ha='center', fontsize=8.5, color=INK)
    c = network['cost']
    ax.set_title(f"{network['name']}:  {network['formula']}\n"
                 f"{c['transistors']} transistors, {c['stages']} stages, "
                 f"{c['inverters']} inverter{'s' if c['inverters'] != 1 else ''} added, "
                 f"at most {max(c['max_series_pull_down'], c['max_series_pull_up'])} in series",
                 fontsize=10.5, loc='left')
    ax.set_xlim(0, x + 0.6)
    ax.set_ylim(-0.6, height + 0.8)
    ax.set_aspect('equal')
    ax.axis('off')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
