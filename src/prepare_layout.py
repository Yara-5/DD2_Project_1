"""Step 5, part 1: plan the layout of one cell, without drawing it.

Input: a stage spec (transistor connections, Euler paths, W/L), the normalized
request, the instructor's plan_hd.json frame and src/rules.json.
Output in cells/<name>/: layout_plan.json (every rectangle and label),
dimensions.json (each dimension with the rules that bound it) and the intended
schematic.spice. This module does not import GDSfactory or write GDS
(AGENTS.md, rule 8).
"""
from __future__ import annotations
import argparse
import hashlib
import heapq
import json
import math
import random
import re
from collections import Counter, defaultdict
from itertools import count
from pathlib import Path
import klayout.db as kdb



LAYERS = {
    'nwell': (64, 20), 'diff': (65, 20), 'poly': (66, 20),
    'licon': (66, 44), 'li1': (67, 20), 'mcon': (67, 44),
    'met1': (68, 20), 'nsdm': (93, 44), 'psdm': (94, 20),
    'hvtp': (78, 44), 'npc': (95, 20), 'boundary': (236, 0),
    'areaid_sc': (81, 4), 'li1_pin': (67, 16), 'li1_label': (67, 5),
    'met1_pin': (68, 16), 'met1_label': (68, 5),
    'vpb_pin': (64, 16), 'vpb_label': (64, 5),
    'vnb_pin': (122, 16), 'vnb_label': (64, 59),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_plan_hd(path):
    path=Path(path).resolve()
    doc=json.loads(path.read_text(encoding='utf-8'))
    if doc.get('units')!='um' or 'frame' not in doc or 'reference' not in doc:
        raise ValueError('Expected the instructor plan_hd.json, with frame and reference sections')
    f,ref=doc['frame'],doc['reference']
    grid=doc['grid']
    for key in ['site','boundary','areaid_sc','rails','nwell','nsdm','psdm','hvtp','npc_band','devices']:
        if key not in f:raise ValueError(f'Missing instructor frame field {key}')
    # Read the rail contact formula as constrained data, never Python eval.
    for net in ('VGND','VPWR'):
        rail=f['rails'][net]
        match=re.fullmatch(r'([0-9.]+)\s*\+\s*([0-9.]+)\*k, one per site',rail['mcon']['x_centres'])
        if not match:raise ValueError('Unsupported rail mcon pattern; an explicit adapter is required')
        start,step=map(float,match.groups())
        if abs(start-f['site']['width']/2)>1e-9 or abs(step-f['site']['width'])>1e-9:
            raise ValueError('Rail pattern differs from the supported one-per-site contract')
    ground=f['rails']['VGND'];power=f['rails']['VPWR']
    result={
        'units':doc['units'],'grid':grid,'height':f['site']['height'],'site_width':f['site']['width'],
        'nmos_band':ref['nmos_diffusion_y'],'pmos_band':ref['pmos_diffusion_y'],
        'gate_contact_band':f['npc_band']['y'],'nwell_band':f['nwell']['y'],
        'nsdm_band':f['nsdm']['y'],'psdm_band':f['psdm']['y'],'hvtp_band':f['hvtp']['y'],
        'ground_y':sum(ground['met1']['y'])/2,'power_y':sum(power['met1']['y'])/2,
        'source':path.name,'source_sha256':sha256(path),
        '_contract':f,'_reference':ref,
    }
    return result


def resolve_x(value,width):
    if isinstance(value,(int,float)):return float(value)
    match=re.fullmatch(r'width(?:\s*([+-])\s*([0-9.]+))?',value)
    if not match:raise ValueError(f'Unsupported symbolic frame coordinate: {value}')
    offset=float(match[2]) if match[2] else 0
    return width+(-offset if match[1]=='-' else offset)


def frame_rectangles(frame,width):
    """Yield exact frame geometry, apart from its allowed site-count width."""
    f=frame['_contract']
    # The npc band covers every poly contact of the cell at once, as in HD.
    for key,layer in (('boundary','boundary'),('areaid_sc','areaid_sc'),('nwell','nwell'),
                      ('nsdm','nsdm'),('psdm','psdm'),('hvtp','hvtp'),('npc_band','npc')):
        item=f[key]
        yield layer,tuple(item['gds']),[
            resolve_x(item['x'][0],width),item['y'][0],
            resolve_x(item['x'][1],width),item['y'][1],
        ],None
    sites=round(width/frame['site_width'])
    for net,rail in f['rails'].items():
        for layer in ('met1','li1'):
            item=rail[layer]
            yield layer,tuple(item['gds']),[
                resolve_x(item['x'][0],width),item['y'][0],
                resolve_x(item['x'][1],width),item['y'][1],
            ],net
        cut=rail['mcon'];size=cut['size']
        match=re.fullmatch(r'([0-9.]+)\s*\+\s*([0-9.]+)\*k, one per site',cut['x_centres'])
        start,step=map(float,match.groups())
        for k in range(sites):
            x=start+step*k
            yield 'mcon',tuple(cut['gds']),[x-size/2,cut['y'][0],x+size/2,cut['y'][1]],net


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_rules(document):
    if document.get('units') != 'um':
        raise ValueError('Rule dimensions must use um, with area rules in um^2')
    result = {}
    for key, entry in document['rules'].items():
        value = float(entry['value'])
        if not math.isfinite(value) or value <= 0 or not entry.get('source'):
            raise ValueError(f'Invalid/unreferenced rule {key}')
        result[key] = value
    return result


def path_segments(euler_paths):
    """Each row's Euler path as a list of diffusion segments (dual trails).

    A row is either one {'devices', 'nodes'} path or a list of them; between two
    segments the diffusion is broken. Both rows have the same segments, column by
    column, because the NMOS and PMOS of one column share a poly gate.
    """
    return {kind: [p] if isinstance(p, dict) else list(p) for kind, p in euler_paths.items()}


def strip_columns(segment):
    """Columns of a row segment that have diffusion in that row: a transistor, or a node
    carried on under a column whose transistor is in the other row only (folding)."""
    nodes = segment['nodes']
    return [i for i in range(len(segment['devices'])) if nodes[i] is not None and nodes[i + 1] is not None]


def node_positions(segment):
    """Node positions of a row segment that are diffusion regions of their own: a node
    carried on under a column without a transistor is the region of the position
    before it, so it is contacted (and counted) once."""
    keep = []
    for k, net in enumerate(segment['nodes']):
        if net is None:
            continue
        if k > 0 and segment['devices'][k - 1] is None and segment['nodes'][k - 1] == net:
            continue
        keep.append(k)
    return keep


def column_devices(rows, by_id):
    """Per column, left to right: (a transistor on its gate, has NMOS, has PMOS)."""
    out = []
    for s in range(len(rows['nmos'])):
        for i in range(len(rows['nmos'][s]['devices'])):
            n, p = rows['nmos'][s]['devices'][i], rows['pmos'][s]['devices'][i]
            out.append((by_id[n if n is not None else p], n is not None, p is not None))
    return out


def gate_runs(rows, by_id, any_gate=False):
    """Runs of adjacent columns whose polys one poly bar joins.

    Adjacent columns of one diffusion segment form a run when they carry the same gate
    net and each holds a finger of a folded transistor (euler.py puts the fingers of a
    leaf side by side). With any_gate (policy poly_bars_same_gate) any two adjacent
    columns on the same gate net form a run, across a diffusion break too, as HD joins
    an input's neighbouring gates in poly (maj3_1: ten gates, four poly contacts).
    The run's polys are joined by a poly bar in the npc band, so the gate of the row
    with fewer fingers runs straight into the bar, and the run needs one poly contact:
    on a column with a transistor in both rows (the middle one), else on its middle
    column. Returns (run index per column, contact per column, [first, last] column of
    every run of two or more columns).
    """
    columns = []
    for s, seg in enumerate(rows['nmos']):
        for i in range(len(seg['devices'])):
            ids = [rows[kind][s]['devices'][i] for kind in ('nmos', 'pmos')]
            devices = [by_id[d] for d in ids if d is not None]
            columns.append((s, devices[0]['gate'], any('finger_of' in d for d in devices), all(ids)))
    run_of, runs = [], []
    for k, (s, net, finger, _) in enumerate(columns):
        joined = any_gate or (finger and columns[k - 1][2] and columns[k - 1][0] == s)
        if k and joined and columns[k - 1][1] == net:
            runs[-1][1] = k
        else:
            runs.append([k, k])
        run_of.append(len(runs) - 1)
    contact = [False] * len(columns)
    for first, last in runs:
        both = [k for k in range(first, last + 1) if columns[k][3]]
        pool = both or list(range(first, last + 1))
        contact[pool[(len(pool) - 1) // 2]] = True
    return run_of, contact, [r for r in runs if r[1] > r[0]]


def validate(spec, frame, r, rows):
    """Validate the supplied devices and Euler paths; do not search for an Euler path."""
    if frame.get('units') != 'um' or '_contract' not in frame or abs(frame['grid']-r['grid'])>1e-9:
        raise ValueError('Use the actual instructor frame and its manufacturing grid')
    names = [spec['name'], spec['output'], *spec['inputs']]
    devices = spec['devices']
    names += [d['id'] for d in devices]
    names += [d[k] for d in devices for k in ('gate', 'source', 'drain', 'bulk', 'model')]
    if any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x) for x in names):
        raise ValueError('Names must be simple identifiers')
    if not 1 <= len(spec['inputs']) <= 4 or len(set(spec['inputs'])) != len(spec['inputs']):
        raise ValueError('Expected one to four unique primary inputs')
    if set(spec['inputs']) & {spec['output'], 'VPWR', 'VGND', 'VPB', 'VNB'}:
        raise ValueError('Input/output/supply names must not collide')
    if spec['output'] in {'VPWR', 'VGND', 'VPB', 'VNB'}:
        raise ValueError('Output cannot be a supply or bulk net')
    if any(d['type'] not in ('nmos', 'pmos') for d in devices):
        raise ValueError('Unknown transistor type')
    by_id = {d['id']: d for d in devices}
    if len(by_id) != len(devices):
        raise ValueError('Device IDs must be unique')
    # A gate is driven by a primary input or by a stage output inside the cell.
    driven = {n for d in devices for n in (d['source'], d['drain'])} - {'VPWR', 'VGND'}
    for d in devices:
        if d['gate'] not in spec['inputs'] and d['gate'] not in driven:
            raise ValueError(f"{d['id']}: gate {d['gate']} is neither an input nor a stage output")
    if [len(s['devices']) for s in rows['nmos']] != [len(s['devices']) for s in rows['pmos']]:
        raise ValueError('The NMOS and PMOS rows must have the same diffusion segments, column by column')
    gate_orders = []
    models = frame['_contract']['devices']
    for kind, model, bulk in (
        ('nmos', models['nmos'], 'VNB'),
        ('pmos', models['pmos'], 'VPB'),
    ):
        ids, widths = [], []
        for segment in rows[kind]:
            seg_ids, nodes = segment['devices'], segment['nodes']
            if not seg_ids or len(nodes) != len(seg_ids) + 1:
                raise ValueError(f'{kind}: every Euler path segment needs n devices and n+1 nodes')
            present = [k for k, n in enumerate(nodes) if n is not None]
            if present and present != list(range(present[0], present[-1] + 1)):
                raise ValueError(f'{kind}: a row strip must be contiguous within its diffusion segment')
            for i, identifier in enumerate(seg_ids):
                if identifier is None:
                    # Folding: a column whose transistor is in the other row only.
                    if nodes[i] is not None and nodes[i + 1] is not None and nodes[i] != nodes[i + 1]:
                        raise ValueError(f'{kind}: a column without a transistor must carry one node on')
                    continue
                if nodes[i] is None or nodes[i + 1] is None:
                    raise ValueError(f'{identifier}: a transistor needs diffusion on both sides')
                d = by_id[identifier]
                if d['type'] != kind or d['model'] != model or d['bulk'] != bulk:
                    raise ValueError(f'{identifier}: unsupported type/model/bulk for this row of the HD frame')
                if {d['source'], d['drain']} != {nodes[i], nodes[i + 1]} or nodes[i] == nodes[i + 1]:
                    raise ValueError(f'{identifier}: Euler node pair disagrees with supplied source/drain')
                w, length = float(d['w_um']), float(d['l_um'])
                if not all(math.isfinite(v) for v in (w, length)):
                    raise ValueError('W/L must be finite')
                band = frame[f'{kind}_band']
                if not r['channel_min_width'] <= w <= band[1] - band[0] + 1e-9:
                    raise ValueError(f'{identifier}: finger width does not fit row; supply legal folded fingers upstream')
                # The selected contact-to-gate rule is specific to the frame's
                # device length; do not apply it to other L silently.
                if abs(length - models['l']) > 1e-9:
                    raise ValueError(f"This profile supports the frame's L={models['l']} um devices only")
                if abs(w / r['grid'] - round(w / r['grid'])) > 1e-7:
                    raise ValueError('Supplied widths must lie on the configured manufacturing grid')
                widths.append(w)
            ids += [x for x in seg_ids if x is not None]
        if len(set(ids)) != len(ids) or set(ids) != {d['id'] for d in devices if d['type'] == kind}:
            raise ValueError(f'{kind}: the Euler path must traverse every device of this row exactly once')
        # Unequal widths in one strip are drawn as a stepped strip (see plan_geometry).
    # Every column is one poly gate: a transistor in at least one row, one gate net.
    for s in range(len(rows['nmos'])):
        for i in range(len(rows['nmos'][s]['devices'])):
            present = [rows[k][s]['devices'][i] for k in rows if rows[k][s]['devices'][i] is not None]
            if not present:
                raise ValueError('Every poly column needs a transistor in at least one row')
            if len({by_id[x]['gate'] for x in present}) != 1:
                raise ValueError('Supplied Euler paths must have identical gate order for common poly columns')
            gate_orders.append(by_id[present[0]]['gate'])
    if not set(spec['inputs']) <= set(gate_orders):
        raise ValueError('Every declared input must drive at least one gate')
    return by_id


def contacted_nodes(spec, rows):
    """(row, segment, node) positions that get a diffusion contact.

    Supplies, the output, stage outputs that drive gates and nets that appear more
    than once need one; a unique internal shared-diffusion node does not.
    """
    counts = Counter(seg['nodes'][k] for segments in rows.values() for seg in segments
                     for k in node_positions(seg))
    exposed = {*spec['inputs'], spec['output'], 'VPWR', 'VGND'} | {d['gate'] for d in spec['devices']}
    return {(kind, s, k) for kind, segments in rows.items() for s, seg in enumerate(segments)
            for k in node_positions(seg) if counts[seg['nodes'][k]] > 1 or seg['nodes'][k] in exposed}


def derive_dimensions(spec, frame, r, by_id, rows, channels=(), policies=None, extra_sites=0):
    """Place the gates along x and choose where each gate's poly contact sits.

    In the npc band every gate has a poly contact; the frame's npc band covers them
    all, so the openings need no spacing of their own. A contact may sit beside its
    gate stripe (HD shifts nand2_1's outwards); its poly pad then joins the stripe.
    A dynamic program over the gates, left to right, chooses each contact's offset
    from its stripe and the smallest distance to the previous gate that keeps:
      - the stripes apart (poly.2), or a diffusion contact between them (licon.11);
      - the pads (pad + stripe hull) apart and from the next stripe (poly.2);
      - the gate li1 landings apart (li.3);
      - at a routing channel, a free li1 track between the two landings, so a net
        that has no landing of its own (the output) can cross the band;
      - across a diffusion break, both diffusion ends and the break (diff/tap.3).
    Cell edges count half of each spacing rule: every HD neighbour keeps the other half.
    Folded fingers joined by one poly bar (gate_runs) share one contact: the others have
    no pad and no li1 landing, so the pad and li1 bounds apply only to columns that have
    them, and none between two columns of one run (one net, one bar).
    """
    g = r['grid']
    step = float((policies or {}).get('routing_grid', 2 * g))
    snap = lambda value, q=g: round(math.ceil(value / q - 1e-9) * q, 9)
    # Even grid counts keep centered shapes on the same grid.
    centered_size = lambda value: snap(value, 2 * g)
    l = max(d['l_um'] for d in spec['devices'])
    # Directional enclosures: li.5 and licon.8a above and below the contact,
    # li1 covering it and licon.8 on the other two sides.
    li_w = r['licon_size']
    # Magic's resistance extraction starts a pin's net just outside the lower-left
    # corner of its pin shape; a pin flush with its 0.17 um landing starts it off li1
    # ("Couldn't find wire"). Inset pins keep that point on li1 (HD's pins sit inside
    # wider li1 too).
    pin = round(r['licon_size'] - 2 * float((policies or {}).get('li1_pin_inset', 0)), 9)
    li_h = centered_size(max(r['licon_size'] + 2 * r['li_contact_enclosure'], r['li_min_area'] / li_w))
    pad_w = centered_size(r['licon_size'] + 2 * r['licon_poly_enclosure_min'])
    pad_h = centered_size(r['licon_size'] + 2 * r['licon_poly_enclosure'])
    m1_pad = centered_size(max(r['mcon_size'] + 2 * r['met1_contact_enclosure'], math.sqrt(r['met1_min_area'])))
    # A contact at a diffusion end: licon.5a along the strip; licon.5c holds across
    # it because contacts sit at least that far from the strip's top and bottom.
    contact_inset_x = r['licon_diff_enclosure_min'] + r['licon_size'] / 2
    end_bounds = {
        'diffusion_extension': r['diff_end_extension'],
        'contact_fit': r['licon_gate_space'] + r['licon_size'] + r['licon_diff_enclosure_min'],
    }
    end = snap(max(end_bounds.values()))
    gap_bounds = {
        'diffusion_spacing': r['diff_space'],
        'end_landing_li1_spacing': li_w + r['li_space'] - 2 * contact_inset_x,
    }
    gap = snap(max(gap_bounds.values()))
    margin_bounds = {'half_diffusion_spacing': r['diff_space'] / 2,
                     'implant_enclosure': r['implant_enclosure']}
    margin = snap(max(margin_bounds.values()))
    pitch_bounds = {
        'poly_stripe_spacing': l + r['poly_space'],
        'diffusion_contact_between_gates': l + r['licon_size'] + 2 * r['licon_gate_space'],
    }
    track = li_w + 2 * r['li_space'] + step   # one routable li1 wire plus grid slack

    contacts = contacted_nodes(spec, rows)
    columns = [(s, i) for s, seg in enumerate(rows['nmos']) for i in range(len(seg['devices']))]
    last_segment = len(rows['nmos']) - 1
    # Which gap each channel (segment, node) asks for: gap j lies between columns
    # j-1 and j; gap 0 is the left edge and gap len(columns) the right edge.
    gap_of = {}
    j = 0
    for s, seg in enumerate(rows['nmos']):
        n = len(seg['devices'])
        for k in range(n + 1):
            gap_of[(s, k)] = j + k
        j += n
    crossing = {gap_of[c] for c in channels}

    offsets = [round(k * step, 9) for k in range(-round(0.3 / step), round(0.3 / step) + 1)]
    run_of, has_contact, runs = gate_runs(rows, by_id, bool((policies or {}).get('poly_bars_same_gate', False)))
    offsets_of = lambda j: offsets if has_contact[j] else [0.0]
    # Pad + stripe hull, from the gate; a column without a contact has its stripe only.
    left_ext = lambda c, j: min(c - pad_w / 2, -l / 2) if has_contact[j] else -l / 2
    right_ext = lambda c, j: max(c + pad_w / 2, l / 2) if has_contact[j] else l / 2

    def pair(j, a, b):
        """Smallest distance from column j-1 (contact offset a) to column j (offset b)."""
        (s0, i0), (s1, i1) = columns[j - 1], columns[j]
        bounds = {}
        if s0 == s1:
            node = i0 + 1
            contacted = any((kind, s0, node) in contacts for kind in rows)
            key = 'diffusion_contact_between_gates' if contacted else 'poly_stripe_spacing'
            bounds[key] = pitch_bounds[key]
            # A stepped strip: the wider transistor's diffusion runs poly.7 past its
            # gate and stops poly.4 short of the narrower one's gate.
            pair_ids = [(rows[kind][s0]['devices'][i0], rows[kind][s0]['devices'][i1]) for kind in rows]
            if any(a is not None and b is not None and abs(by_id[a]['w_um'] - by_id[b]['w_um']) > 1e-9
                   for a, b in pair_ids):
                bounds['diffusion_step'] = l + r['diff_end_extension'] + r['poly_diff_space']
        else:
            bounds['diffusion_break'] = 2 * end + gap + l
        if run_of[j - 1] != run_of[j]:
            bounds['poly_pad_spacing'] = right_ext(a, j - 1) - left_ext(b, j) + r['poly_space']
            if has_contact[j - 1] and has_contact[j]:
                bounds['gate_li1_spacing'] = (a + li_w / 2) - (b - li_w / 2) + r['li_space']
        if j in crossing:
            # A column without a landing counts as one on its stripe (conservative).
            bounds['li1_track_through_band'] = (a + li_w / 2) - (b - li_w / 2) + track
        return bounds

    def left_bounds(c):
        b = {'diffusion_margin': margin + end + l / 2,
             'poly_pad_to_edge': r['poly_space'] / 2 - left_ext(c, 0)}
        if has_contact[0]:
            b['gate_li1_to_edge'] = r['li_space'] / 2 - (c - li_w / 2)
        if 0 in crossing:
            b['li1_track_through_band'] = r['li_space'] / 2 + track - r['li_space'] - (c - li_w / 2)
        return b

    def right_bounds(c):
        last = len(columns) - 1
        b = {'diffusion_margin': l / 2 + end + margin,
             'poly_pad_to_edge': right_ext(c, last) + r['poly_space'] / 2}
        if has_contact[last]:
            b['gate_li1_to_edge'] = c + li_w / 2 + r['li_space'] / 2
        if len(columns) in crossing:
            b['li1_track_through_band'] = c + li_w / 2 + track - r['li_space'] + r['li_space'] / 2
        return b

    # Distances are rounded up to two routing steps, so contacts midway between two
    # gates stay on the routing grid; the cost adds a small preference for contacts
    # close to their own stripe among equally narrow solutions.
    best = {c: ((snap(max(left_bounds(c).values()), step), abs(c)), None) for c in offsets_of(0)}
    history = [best]
    for j in range(1, len(columns)):
        current = {}
        for b in offsets_of(j):
            choice = None
            for a, ((xa, sa), _) in history[-1].items():
                cost = (round(xa + snap(max(pair(j, a, b).values()), 2 * step), 9), round(sa + abs(b), 9))
                if choice is None or cost < choice[0]:
                    choice = (cost, a)
            current[b] = choice
        history.append(current)
    end_cost = {c: (snap(x + max(right_bounds(c).values()), step), s) for c, ((x, s), _) in history[-1].items()}
    c_last = min(end_cost, key=lambda c: (end_cost[c], abs(c), c))
    chosen = [c_last]
    for j in range(len(columns) - 1, 0, -1):
        chosen.append(history[j][chosen[-1]][1])
    chosen.reverse()
    gates = [history[0][chosen[0]][0][0]]
    gaps = []
    for j in range(1, len(columns)):
        bounds = pair(j, chosen[j - 1], chosen[j])
        d = snap(max(bounds.values()), 2 * step)
        gates.append(round(gates[-1] + d, 9))
        gaps.append({'between_columns': [j - 1, j], 'pitch_um': d,
                     'binding': max(bounds, key=bounds.get),
                     'lower_bounds_um': {k: round(v, 9) for k, v in bounds.items()}})
    raw_width = end_cost[c_last][0]
    sites = math.ceil(raw_width / frame['site_width'] - 1e-9) + extra_sites
    # Centre the cell's content, so both edges keep the same room for routing.
    shift = math.floor((sites * frame['site_width'] - raw_width) / 2 / step + 1e-9) * step
    gates = [round(x + shift, 9) for x in gates]

    active, contacts_x, column = [], [], 0
    row_active, row_nodes_x = {k: [] for k in rows}, {k: [] for k in rows}
    for s, segment in enumerate(rows['nmos']):
        n = len(segment['devices'])
        seg_gates = gates[column:column + n]
        left, right = round(seg_gates[0] - l / 2 - end, 9), round(seg_gates[-1] + l / 2 + end, 9)
        active.append([left, right])
        contacts_x.append([round(left + contact_inset_x, 9)]
                          + [round((seg_gates[i] + seg_gates[i + 1]) / 2, 9) for i in range(n - 1)]
                          + [round(right - contact_inset_x, 9)])
        # Per row: its strip may start or end inside the segment (folding); its ends
        # then sit poly.7 / contact-fit past its own first and last gates.
        for kind in rows:
            strip = strip_columns(rows[kind][s])
            if not strip:
                row_active[kind].append(None)
                row_nodes_x[kind].append([None] * (n + 1))
                continue
            a, b = strip[0], strip[-1]
            lo, hi = round(seg_gates[a] - l / 2 - end, 9), round(seg_gates[b] + l / 2 + end, 9)
            row_active[kind].append([lo, hi])
            xs = [None] * (n + 1)
            xs[a], xs[b + 1] = round(lo + contact_inset_x, 9), round(hi - contact_inset_x, 9)
            for k in range(a + 1, b + 1):
                xs[k] = round((seg_gates[k - 1] + seg_gates[k]) / 2, 9)
            row_nodes_x[kind].append(xs)
        column += n
    return {
        'gate_length_um': l,
        'gate_pitches_um': [x['pitch_um'] for x in gaps],
        'gate_gaps': gaps,
        'pitch_lower_bounds_um': pitch_bounds,
        'gate_contact_offset_um': chosen,
        'gate_has_contact': has_contact,
        'poly_bar_runs': runs,
        'li1_landing_um': [li_w, li_h], 'li1_pin_um': pin, 'poly_landing_um': [pad_w, pad_h], 'met1_landing_um': m1_pad,
        'diffusion_end_extension_um': end, 'end_extension_lower_bounds_um': end_bounds,
        'diffusion_break_um': gap, 'diffusion_break_lower_bounds_um': gap_bounds,
        'side_margin_um': margin, 'side_margin_lower_bounds_um': margin_bounds,
        'left_edge_lower_bounds_um': {k: round(v, 9) for k, v in left_bounds(chosen[0]).items()},
        'right_edge_lower_bounds_um': {k: round(v, 9) for k, v in right_bounds(chosen[-1]).items()},
        'li1_track_um': round(track, 9), 'channels': sorted(channels),
        'crossing_gaps': sorted(crossing),
        'diffusion_segments': len(active),
        'active_x_um': active, 'centering_shift_um': round(shift, 9),
        'gate_x_um': gates, 'diffusion_node_x_um': contacts_x,
        'row_active_x_um': row_active, 'row_node_x_um': row_nodes_x,
        'raw_width_um': round(raw_width, 9), 'minimum_width_sites': sites - extra_sites,
        'width_sites': sites,
        'formulas': {
            'pitch': 'per gap: snap_up(max(L + poly.2 or L + licon + 2 licon.11, pad hull spacing poly.2, '
                     'gate li1 spacing li.3, li1 track at a channel), 2 routing steps)',
            'gate_contact_offset': 'chosen per gate by the dynamic program: narrowest cell, then contacts nearest their stripe',
            'li1_landing': 'licon wide (li1 covers it), licon + 2 li.5 tall, at least li.6 in area',
            'poly_landing': 'licon + 2 licon.8 wide, licon + 2 licon.8a tall, joined to the stripe',
            'end_extension': 'max(poly.7, licon.11 + licon + licon.5a)',
            'diffusion_break': 'snap_up(max(diff/tap.3, end-landing li1 spacing))',
            'side_margin': 'max(diff/tap.3 / 2, implant enclosure): the neighbour keeps the other half',
            'width_sites': 'ceil(raw_width / site_width), plus explicit routing retries if required',
        },
    }


def via_pad(r):
    """Side of the square met1 pad of an mcon via: met1.4 enclosure and met1.6 area,
    centred on the 2-grid."""
    pad = max(r['mcon_size'] + 2 * r['met1_contact_enclosure'], math.sqrt(r['met1_min_area']))
    return round(math.ceil(pad / (2 * r['grid']) - 1e-9) * 2 * r['grid'], 9)


def pin_access_pads(shapes, r, only=None):
    """One met1 access pad per signal pin shape: the via a router placing the cell will
    drop on the pin (an mcon at the pin's centre with this cell's own via pad). Another
    net's met1 must keep met1.2 from it, so every input and output pin stays reachable
    from met1 (policy pin_access_met1). only: [(net, point)], the pins to keep reachable
    (one per port); None keeps every pin shape reachable."""
    pad = via_pad(r)
    pads = []
    for s in shapes:
        if s['layer'] == 'li1_pin' and s['net'] not in ('VPWR', 'VGND'):
            cx, cy = (s['box'][0] + s['box'][2]) / 2, (s['box'][1] + s['box'][3]) / 2
            if only is not None and not any(n == s['net'] and abs(q[0] - cx) < 1e-6 and abs(q[1] - cy) < 1e-6
                                            for n, q in only):
                continue
            pads.append({'net': s['net'], 'point_um': [round(cx, 9), round(cy, 9)],
                         'met1_box': [round(cx - pad / 2, 9), round(cy - pad / 2, 9),
                                      round(cx + pad / 2, 9), round(cy + pad / 2, 9)]})
    return pads


def square_gap(a, b):
    """Spacing between two boxes with square corners, as Magic checks it."""
    return max(max(a[0] - b[2], b[0] - a[2], 0), max(a[1] - b[3], b[1] - a[3], 0))


def check_pin_access(plan, r):
    """Raise if another net's met1 comes closer than met1.2 to a pin's access pad."""
    met1 = [s for s in plan['shapes'] if s['layer'] == 'met1']
    for p in plan.get('pin_access', []):
        for s in met1:
            if s['net'] != p['net'] and square_gap(p['met1_box'], s['box']) < r['met1_space'] - 1e-6:
                raise ValueError(f"met1 of {s['net']} blocks the met1 access of pin {p['net']} at {p['point_um']}")


def plan_geometry(spec, frame, r, dim, by_id, sites, rows, pin_access=False, movable=False):
    """Build a pure-data plan before constructing the GDSfactory component. With movable,
    signal diffusion contacts are left for route_negotiated to place."""
    width, height = round(sites * frame['site_width'], 9), frame['height']
    gate_y = sum(frame['gate_contact_band'])/2
    shapes, contacts, devices = [], [], []
    terminals = defaultdict(list)

    def rect(layer, box, net=None):
        box = [round(float(x), 9) for x in box]
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError(f'Invalid box {box}')
        shapes.append({'layer': layer, 'box': box, 'net': net})

    def square(layer, x, y, size, net=None):
        rect(layer, (x-size/2, y-size/2, x+size/2, y+size/2), net)

    for layer,pair,box,net in frame_rectangles(frame,width):
        if LAYERS[layer]!=pair:
            raise ValueError(f'Unexpected instructor GDS layer mapping: {layer} {pair}')
        rect(layer,box,net)
        if layer=='mcon':
            contacts.append({'type':'mcon','net':net,'point_um':[(box[0]+box[2])/2,(box[1]+box[3])/2],
                             'source':'instructor frame: one rail contact per site'})

    contacted = contacted_nodes(spec, rows)
    far = rail_side_contacts(rows, contacted, dim, r)
    pin_size = dim['li1_pin_um']
    li_w, li_h = dim['li1_landing_um']
    pad_w, pad_h = dim['poly_landing_um']
    rails = frame['_contract']['rails']
    gate_li1 = (gate_y - li_h / 2, gate_y + li_h / 2)
    # Across a contact: licon.5c from the strip's top and bottom.
    inset = r['licon_diff_enclosure'] + r['licon_size'] / 2
    for kind in ('nmos', 'pmos'):
        band = frame[f'{kind}_band']
        # Every strip is aligned to the band edge on the rail side, as in HD.
        span = lambda w: (band[0], round(band[0] + w, 9)) if kind == 'nmos' else (round(band[1] - w, 9), band[1])
        column = 0
        for s, segment in enumerate(rows[kind]):
            ids = segment['devices']
            xs = dim['gate_x_um'][column:column + len(ids)]
            strip = strip_columns(segment)
            if not strip:
                column += len(ids)
                continue
            active_left, active_right = dim['row_active_x_um'][kind][s]
            # Width of each strip column: its transistor's W; under a column without one
            # (folding), the narrower of the nearest transistors on either side.
            ws = {i: by_id[ids[i]]['w_um'] for i in strip if ids[i] is not None}
            for i in strip:
                if ids[i] is None:
                    near = [ws[j] for j in (max((j for j in ws if j < i), default=None),
                                            min((j for j in ws if j > i), default=None)) if j is not None]
                    ws[i] = min(near)
            # One rectangle per column. Towards an equal or wider neighbour it runs to
            # that neighbour's gate centre (inside the neighbour's strip); towards a
            # narrower one it stops poly.7 past its own gate (a stepped strip, as HD's
            # o21ai_1), which the 'diffusion_step' pitch keeps poly.4 from that gate.
            half_l = dim['gate_length_um'] / 2
            for i in strip:
                x, w = xs[i], ws[i]
                def reach(j, sign):
                    if j not in ws:
                        return active_left if sign < 0 else active_right
                    if ws[j] >= w - 1e-9:
                        return xs[j]
                    return round(x + sign * (half_l + r['diff_end_extension']), 9)
                rect('diff', (reach(i - 1, -1), span(w)[0], reach(i + 1, 1), span(w)[1]))
            # The y range a node's contact may use: inside both neighbours' diffusion.
            def node_span(k):
                near = [ws[j] for j in (k - 1, k) if j in ws]
                return span(min(near))
            for i, identifier in enumerate(ids):
                if identifier is None:
                    continue
                d, x = by_id[identifier], dim['gate_x_um'][column + i]
                bottom, top = span(d['w_um'])
                devices.append({**d, 'gate_x_um': x, 'diffusion_y_um': [bottom, top], 'segment': s,
                                'left_net': segment['nodes'][i], 'right_net': segment['nodes'][i+1],
                                # BSIM4 SA/SB: gate edge to the end of its own diffusion strip.
                                'diffusion_edge_distances_um': {
                                    'left': round(x-d['l_um']/2-active_left, 9),
                                    'right': round(active_right-x-d['l_um']/2, 9),
                                }})
            column += len(segment['devices'])
            for k, (x, net) in enumerate(zip(dim['row_node_x_um'][kind][s], segment['nodes'])):
                # A unique internal shared-diffusion node needs no contact or wire.
                if net is None or (kind, s, k) not in contacted:
                    continue
                bottom, top = node_span(k)
                supply = net == ('VGND' if kind == 'nmos' else 'VPWR')
                wide = r['licon_size'] + 2 * r['li_contact_enclosure']
                if supply:
                    # As far from the band as licon.5c allows, its li1 merged into the
                    # rail strip. In the 0.65 um NMOS row li.5 is met sideways (a wide
                    # stub), which frees the row above it for a horizontal li1 track, as
                    # in HD. In the PMOS row li.5 is met below the contact (a narrow
                    # stub), so a rail-side landing of another net fits beside it.
                    y = bottom + inset if kind == 'nmos' else top - inset
                    rail = rails[net]['li1']['y']
                    if kind == 'nmos':
                        rect('li1', (x - wide / 2, rail[0], x + wide / 2, y + r['licon_size'] / 2), net)
                    else:
                        rect('li1', (x - li_w / 2, y - li_h / 2, x + li_w / 2, rail[1]), net)
                    terminals[net].append((x, frame['ground_y'] if kind == 'nmos' else frame['power_y']))
                elif movable:
                    # route_negotiated places the licon and its landing in this range.
                    contacts.append({'type': 'diffusion', 'net': net, 'point_um': None, 'row': kind,
                                     'x_um': x, 'movable': True,
                                     'y_range_um': [round(bottom + inset, 9), round(top - inset, 9)]})
                    continue
                elif (kind, s, k) in far:
                    # Rail side, li.5 met sideways: the net's own wire runs along the
                    # rail side, past the band-side landings of the nets between.
                    y = top - inset
                    landing = (x - wide / 2, y - r['licon_size'] / 2, x + wide / 2, y + r['licon_size'] / 2)
                    rect('li1', landing, net)
                    terminals[net].append((x, round(y, 9)))
                else:
                    # As close to the band as the gate landings' li1 spacing allows.
                    y = (min(top - inset, gate_li1[0] - r['li_space'] - li_h / 2) if kind == 'nmos'
                         else max(bottom + inset, gate_li1[1] + r['li_space'] + li_h / 2))
                    landing = (x - li_w / 2, y - li_h / 2, x + li_w / 2, y + li_h / 2)
                    rect('li1', landing, net)
                    terminals[net].append((x, round(y, 9)))
                if y < bottom + inset - 1e-9 or y > top - inset + 1e-9:
                    raise ValueError(f'{kind}: contact landing cannot fit both diffusion enclosure and gate-pin clearance')
                square('licon', x, y, r['licon_size'], net)
                contacts.append({'type': 'diffusion', 'net': net, 'point_um': [x, round(y, 9)], 'row': kind})
                # Where the contact's licons may sit (licon.5c inside its node's
                # diffusion): multiply_cuts() fills the range; stack_contacts() may move a
                # signal contact and its landing under a via.
                contacts[-1]['y_range_um'] = [round(bottom + inset, 9), round(top - inset, 9)]
                if not supply:
                    contacts[-1]['landing_box'] = [round(v, 9) for v in landing]

    n_bottom = min(d['diffusion_y_um'][0] for d in devices if d['type']=='nmos')
    p_top = max(d['diffusion_y_um'][1] for d in devices if d['type']=='pmos')
    n_top = max(d['diffusion_y_um'][1] for d in devices if d['type']=='nmos')
    p_bottom = min(d['diffusion_y_um'][0] for d in devices if d['type']=='pmos')
    # The npc band's position decides these; check them rather than assume them.
    for name, value, rule in (
            ('poly pad to NMOS diffusion', gate_y - pad_h/2 - n_top, 'poly_diff_space'),
            ('poly pad to PMOS diffusion', p_bottom - gate_y - pad_h/2, 'poly_diff_space'),
            ('poly contact to NMOS diffusion', gate_y - r['licon_size']/2 - n_top, 'poly_contact_diff_space'),
            ('poly contact to PMOS diffusion', p_bottom - gate_y - r['licon_size']/2, 'poly_contact_pdiff_space')):
        if value < r[rule] - 1e-9:
            raise ValueError(f'{name} is {value:.3f} um, below {rule} ({r[rule]} um)')
    columns = column_devices(rows, by_id)
    # Folded fingers of one leaf side by side: one poly bar across the npc band joins
    # their polys (and the other row's gate runs into it), so the run needs one contact.
    for first, last in dim.get('poly_bar_runs', []):
        d = columns[first][0]
        rect('poly', (dim['gate_x_um'][first]-d['l_um']/2, gate_y-pad_h/2,
                      dim['gate_x_um'][last]+d['l_um']/2, gate_y+pad_h/2), d['gate'])
    has_contact = dim.get('gate_has_contact', [True]*len(columns))
    for k, (x, c, (d, has_n, has_p)) in enumerate(zip(dim['gate_x_um'], dim['gate_contact_offset_um'], columns)):
        net = d['gate']
        half = d['l_um']/2
        # A column with a transistor in one row only (folding) stops in the npc band
        # on the other side (at its pad or its run's poly bar), so it makes no
        # transistor in that row's diffusion.
        bottom = n_bottom-r['poly_endcap'] if has_n else gate_y-pad_h/2
        top = p_top+r['poly_endcap'] if has_p else gate_y+pad_h/2
        rect('poly', (x-half, bottom, x+half, top), net)
        if not has_contact[k]:
            continue
        # The pad encloses its contact and reaches its stripe.
        rect('poly', (min(x+c-pad_w/2, x-half), gate_y-pad_h/2, max(x+c+pad_w/2, x+half), gate_y+pad_h/2), net)
        square('licon', x+c, gate_y, r['licon_size'], net)
        rect('li1', (x+c-li_w/2, gate_li1[0], x+c+li_w/2, gate_li1[1]), net)
        # Only primary inputs are pins; an internal stage output is wired, not exposed.
        if net in spec['inputs']:
            square('li1_pin', x+c, gate_y, pin_size, net)
        terminals[net].append((round(x+c, 9), gate_y))
        contacts.append({'type':'poly','net':net,'point_um':[round(x+c, 9),gate_y],'gate_x_um':x})

    # The output pin sits on one of the output's own drain landings: HD only asks for
    # a li1 pin on the wire joining the drains (handout Table 2), and a separate pin at
    # the cell edge would need its own route.
    # With movable contacts the output may have no landing yet: route_negotiated places
    # its pin after routing.
    output_point = terminals[spec['output']][0] if terminals.get(spec['output']) else None
    if pin_access and output_point is not None:
        # The first landing whose met1 access pad keeps met1.2 from the frame's met1
        # (the rails); pins are reachable from met1 (policy pin_access_met1).
        rails = [s['box'] for s in shapes if s['layer'] == 'met1']
        pad = via_pad(r)
        for point in terminals[spec['output']]:
            box = (point[0] - pad/2, point[1] - pad/2, point[0] + pad/2, point[1] + pad/2)
            if all(square_gap(box, b) >= r['met1_space'] - 1e-6 for b in rails):
                output_point = point
                break
    if output_point is not None:
        square('li1_pin', *output_point, pin_size, spec['output'])
    # The actual frame supplies all rail mcons, one per site. Each source
    # connects to the continuous li1 rail without adding off-pattern mcons.
    for net, y in (('VGND',frame['ground_y']), ('VPWR',frame['power_y'])):
        starts = list(terminals[net])
        if not starts:
            raise ValueError(f'No diffusion connection to {net}')
        for x, _ in starts:
            terminals[net].append((x,y))

    # Bulk markers match the handout's layer conventions; actual body
    # extraction/connection still requires the course technology and tap cells.
    pin_x=frame['site_width']/2
    pin_size=frame['_contract']['rails']['VGND']['mcon']['size']
    for net,y in (('VPWR',frame['power_y']),('VGND',frame['ground_y'])):
        for layer in ('li1_pin','met1_pin'):
            square(layer,pin_x,y,pin_size,net)
    for net,y,layer in (('VPB',height,'vpb_pin'),('VNB',0,'vnb_pin')):
        square(layer,pin_x,y,pin_size,net)
    return {'width_um':width,'width_sites':sites,'height_um':height,
            'shapes':shapes,'terminals':dict(terminals),'contacts':contacts,'devices':devices,
            'output_pin_um':output_point,'gate_contact_y_um':gate_y,
            'supply_pin_x_um':pin_x,'routes':[]}


def rail_side_contacts(rows, contacted, dim, r):
    """PMOS-row contacts that go to the rail side of the 1.0 um row.

    A net that appears again later in the row must be joined along the row. Where a
    contact of another signal net lies between two consecutive appearances (that net
    leaves towards the band) and no VPWR contact does, the joining wire can only pass
    on the rail side, so both landings go there. Two such wide landings of different
    nets need li.3 between them, so a pair stays on the band side if one of its
    contacts would be too close to another net's rail-side contact.
    """
    sequence = []
    for s, seg in enumerate(rows['pmos']):
        for k, net in enumerate(seg['nodes']):
            if ('pmos', s, k) in contacted:
                sequence.append(((s, k), dim['row_node_x_um']['pmos'][s][k], net))
    # Two wide landings side by side: li.3 between them.
    apart = r['licon_size'] + 2 * r['li_contact_enclosure'] + r['li_space']
    chosen = {}
    for i, (place, x, net) in enumerate(sequence):
        if net == 'VPWR':
            continue
        j = next((j for j in range(i + 1, len(sequence)) if sequence[j][2] == net), None)
        if j is None:
            continue
        between = {n for _, _, n in sequence[i + 1:j]}
        if not between or 'VPWR' in between:
            continue
        pair = [(place, x), sequence[j][:2]]
        if all(abs(px - qx) >= apart - 1e-9 for _, px in pair for q, (qx, qnet) in chosen.items()
               if qnet != net):
            for p, px in pair:
                chosen[p] = (px, net)
    return {('pmos', s, k) for s, k in chosen}


def channel_candidates(spec, rows):
    """Gaps worth widening into channels: where a net that needs both rows and drives no
    gate (so it has no landing of its own in the npc band, like the cell output) has a
    contact between two gates."""
    gate_nets = {d['gate'] for d in spec['devices']}
    kinds, gaps, every = defaultdict(set), defaultdict(set), set()
    for kind, segments in rows.items():
        for s, segment in enumerate(segments):
            for k, net in enumerate(segment['nodes']):
                if net is None:
                    continue
                kinds[net].add(kind)
                gaps[net].add((s, k))          # node k sits between gates k-1 and k
                every.add((s, k))
    nets = [n for n in sorted(kinds) if len(kinds[n]) == 2 and n not in gate_nets
            and n not in ('VPWR', 'VGND')]
    near = sorted({gap for n in nets for gap in gaps[n]})
    # The gaps at the crossing nets' own contacts first, then every other gap.
    return near + sorted(every - set(near)) if nets else []


def route_li1(plan, spec, frame, r, policies, allow_met1=False, rip_up=False):
    """Deterministic Manhattan A* on li1, with met1 through mcon only where li1 cannot pass.

    Every terminal is on li1. A net may step onto met1 through an mcon via; a met1
    step and a via cost more than an li1 step (policies met1_step_cost, via_cost), so
    met1 appears only where li1 alone cannot connect a multi-stage cell (handout step
    5). Supplies stay on li1: each supply contact gets a straight stub to its li1 rail.
    A connection that needs met1 (li1 alone failed) pays met1_route_li1_step_cost for an
    li1 step, so it climbs to met1 at its first landing and comes down at its last, and
    it may start from the net's met1 wire as well as its li1 points (a multi-source
    search, so one via serves several branches). With the policy
    folded_finger_nets_on_met1 (default false), the source/drain nets of folded fingers
    (supplies excepted) are routed that way from the start. This is a deliberate exception to handout Tables 1 and 3 (met1 for the
    rails in a single-stage cell), for lower resistance; the plan lists those nets.
    Clearances come from rules.json. Not a substitute for DRC, extraction or LVS.
    """
    step = float(policies['routing_grid'])
    g = r['grid']
    centered = lambda v: round(math.ceil(v / (2 * g) - 1e-9) * 2 * g, 9)
    wire = {'li1': r['li_width'], 'met1': r['met1_width']}
    space = {'li1': r['li_space'], 'met1': r['met1_space']}
    met1_pad = via_pad(r)
    step_cost = {'li1': 1, 'met1': float(policies.get('met1_step_cost', 4))}
    via_cost = float(policies.get('via_cost', 40))
    met1_nets = set()
    if policies.get('folded_finger_nets_on_met1', False):
        met1_nets = {n for d in spec['devices'] if d.get('finger_of') for n in (d['drain'], d['source'])
                     if n not in ('VPWR', 'VGND')}
    # A connection that uses met1 anyway (the policy's nets, or one li1 alone could not
    # make) pays this for an li1 step, so it climbs to met1 at its first landing and comes
    # down at its last: as little li1 as possible in a path that has met1 anyway.
    met1_route_li1_cost = float(policies.get('met1_route_li1_step_cost', 100))
    met1_points = {}     # met1 grid points of each net routed so far (this order's attempt)
    if step <= 0 or abs(step/(2*g) - round(step/(2*g))) > 1e-7:
        raise ValueError('Routing grid must be a positive multiple of twice the manufacturing grid')
    for w in (*wire.values(), r['mcon_size'], met1_pad):
        if abs(w/(2*g) - round(w/(2*g))) > 1e-7:
            raise ValueError('Centered wire widths must be multiples of twice the manufacturing grid')
    width_um, ny = plan['width_um'], round(frame['height'] / step)
    nx = math.floor(width_um / step)
    columns = nx + 1
    occupied = {layer: [(s['net'], s['box']) for s in plan['shapes'] if s['layer'] == layer]
                for layer in ('li1', 'met1')}
    mcons = [s['box'] for s in plan['shapes'] if s['layer'] == 'mcon']
    # Each signal pin's met1 access pad counts as met1 of its net: other nets keep met1.2
    # from it (policy pin_access_met1), its own net may pass over it.
    occupied['met1'] += [(p['net'], p['met1_box']) for p in plan.get('pin_access', [])]
    rail_y = {'VPWR': frame['power_y'], 'VGND': frame['ground_y']}

    def index(point):
        return tuple(round(v/step) for v in point)

    def raster(boxes, margin):
        """Grid points strictly closer than `margin` to any of the boxes, as a byte grid."""
        grid = bytearray(columns * (ny + 1))
        for b in boxes:
            x0 = max(0, math.floor((b[0] - margin) / step + 1e-9) + 1)
            x1 = min(nx, math.ceil((b[2] + margin) / step - 1e-9) - 1)
            y0 = max(0, math.floor((b[1] - margin) / step + 1e-9) + 1)
            y1 = min(ny, math.ceil((b[3] + margin) / step - 1e-9) - 1)
            if x1 < x0:
                continue
            ones = b'\x01' * (x1 - x0 + 1)
            for iy in range(y0, y1 + 1):
                grid[iy * columns + x0:iy * columns + x1 + 1] = ones
        return grid

    def footprint(boxes):
        """Where another net's wire centre or via may not go, near these shapes."""
        return {'li1': raster(boxes['li1'], space['li1'] + wire['li1'] / 2),
                'met1': raster(boxes['met1'], space['met1'] + wire['met1'] / 2),
                'pad': raster(boxes['met1'], space['met1'] + met1_pad / 2),
                'via': raster(boxes['mcon'], r['mcon_space'] + r['mcon_size'] / 2)}

    def union(grids):
        """Byte grids OR-ed together (one C-level operation per grid)."""
        size = columns * (ny + 1)
        total = 0
        for g in grids:
            total |= int.from_bytes(g, 'little')
        return bytearray(total.to_bytes(size, 'little'))

    def search(start, goal, net, allow_met1, hard=None, soft=None, penalty=0, no_via=frozenset(),
               more_starts=(), prefer_met1=False):
        """A* from start to goal. hard: {layer: [(net, box)], 'mcon': [box]} replaces the
        shapes routed so far (rip-up mode); soft: {net: footprint} of other nets' routed
        wires, which the path may cross at `penalty` per grid step. Returns the path, or
        (path, nets crossed) when soft is given."""
        source = hard if hard is not None else occupied
        others = {layer: [b for owner, b in source[layer] if owner != net] for layer in ('li1', 'met1')}
        blocked = {'li1': raster(others['li1'], space['li1'] + wire['li1'] / 2),
                   'met1': raster(others['met1'], space['met1'] + wire['met1'] / 2)}
        layers = ('li1', 'met1') if (allow_met1 or net in met1_nets) and net not in rail_y else ('li1',)
        cost_of = dict(step_cost, li1=met1_route_li1_cost) if prefer_met1 and len(layers) == 2 else step_cost
        if len(layers) == 2:
            pad_blocked = raster(others['met1'], space['met1'] + met1_pad / 2)
            via_blocked = raster(hard['mcon'] if hard is not None else mcons, r['mcon_space'] + r['mcon_size'] / 2)
        soft = soft or {}
        cross = {k: union([f[k] for f in soft.values()]) for k in ('li1', 'met1', 'pad', 'via')} if soft else None
        # Half the spacing at the cell edge: every HD neighbour keeps the other half.
        edge = r['li_space'] / 2 if net not in rail_y else 0

        def inside(ix, iy, layer):
            x = ix * step
            return (edge + wire[layer]/2 - 1e-9 <= x <= width_um - edge - wire[layer]/2 + 1e-9
                    and 0 <= iy <= ny)

        def allowed(ix, iy, layer):
            return inside(ix, iy, layer) and not blocked[layer][iy * columns + ix]

        def via_ok(ix, iy):
            return (allowed(ix, iy, 'li1') and inside(ix, iy, 'met1')
                    and not pad_blocked[iy * columns + ix] and not via_blocked[iy * columns + ix]
                    and (ix, iy) not in no_via)

        start3, goal3 = (*start, 'li1'), (*goal, 'li1')
        if not allowed(*start3) or not allowed(*goal3):
            raise ValueError(f'LI1 terminal clearance prevents routing net {net}')
        queue, serial, best, parent = [], count(), {}, {}
        heuristic = lambda p: abs(p[0]-goal[0]) + abs(p[1]-goal[1])
        # more_starts: further (ix, iy, layer) points the net already reaches (a
        # multi-source search: the cheapest of them wins).
        for s in (start3, *more_starts):
            if s not in best and allowed(*s):
                best[s] = 0
                heapq.heappush(queue, (heuristic(s), 0, next(serial), s))
        while queue:
            _, cost, _, p = heapq.heappop(queue)
            if cost != best.get(p):
                continue
            if p == goal3:
                result = [p]
                while p in parent:
                    p = parent[p]; result.append(p)
                return list(reversed(result))
            ix, iy, layer = p
            moves = [((ix+dx, iy+dy, layer), cost_of[layer]) for dx, dy in ((1,0),(-1,0),(0,1),(0,-1))
                     if allowed(ix+dx, iy+dy, layer)]
            if len(layers) == 2 and via_ok(ix, iy):
                moves.append(((ix, iy, 'met1' if layer == 'li1' else 'li1'), via_cost))
            for nxt, c in moves:
                if cross is not None:
                    k = nxt[1] * columns + nxt[0]
                    if nxt[2] != layer:      # a via: its pads on both layers and its cut
                        c += penalty * (cross['li1'][k] + cross['pad'][k] + cross['via'][k])
                    else:
                        c += penalty * cross[layer][k]
                if cost + c < best.get(nxt, math.inf):
                    best[nxt] = cost + c; parent[nxt] = p
                    heapq.heappush(queue, (cost + c + heuristic(nxt), cost + c, next(serial), nxt))
        raise ValueError(f"No {'/'.join(layers)} route found for {net} at this width")

    def spaced_search(*args, **kwargs):
        """search, but a path may not put two of its own vias closer than mcon.2 allows
        (the rasters only hold vias laid before this path): the later via's point is
        forbidden and the search repeated."""
        pitch = r['mcon_size'] + r['mcon_space']
        no_via = set()
        while True:
            path = search(*args, **kwargs, no_via=frozenset(no_via))
            vias = [(b[0], b[1]) for a, b in zip(path, path[1:]) if a[2] != b[2]]
            close = [v for i, u in enumerate(vias) for v in vias[i + 1:]
                     if abs(u[0] - v[0]) * step < pitch - 1e-9 and abs(u[1] - v[1]) * step < pitch - 1e-9]
            if not close:
                return path
            no_via.add(close[0])
            if len(no_via) > int(policies['rip_up_limit']):
                raise ValueError(f'no route for {args[2]} with its vias spaced')

    def crossed(path, soft):
        """The nets whose routed wires a soft path comes too close to."""
        hit = set()
        for i, (ix, iy, layer) in enumerate(path):
            k = iy * columns + ix
            via = i > 0 and path[i - 1][2] != layer
            for other, f in soft.items():
                if f[layer][k] or (via and (f['li1'][k] or f['pad'][k] or f['via'][k])):
                    hit.add(other)
        return hit

    def connections(net):
        """(origin, target) pairs to route; origins come from what is already connected."""
        points = list(dict.fromkeys(index(p) for p in plan['terminals'][net]))
        if net in rail_y:
            # The li1 rail strip is continuous: each contact needs only its own stub.
            rail = index((0, rail_y[net]))[1]
            for p in points:
                if p[1] != rail:
                    yield [p], (p[0], rail)
            return
        connected = points[:1]
        for target in points[1:]:
            yield connected, target
            connected.append(target)

    def box_at(a, b, w):
        return [round(v, 9) for v in (min(a[0], b[0]) - w/2, min(a[1], b[1]) - w/2,
                                      max(a[0], b[0]) + w/2, max(a[1], b[1]) + w/2)]

    def lay(net, connected, path, sink=None):
        """Add the path's wires and vias: to the plan, or to a rip-up record (sink)."""
        if sink is not None:
            shapes, routes, vias = sink['shapes'], sink['routes'], sink['vias']
            def keep(layer, box):
                sink[layer].append(box)
        else:
            shapes, routes, vias = plan['shapes'], plan['routes'], plan.setdefault('vias', [])
            def keep(layer, box):
                (mcons if layer == 'mcon' else occupied[layer]).append(box if layer == 'mcon' else (net, box))
        # Split the path into runs on one layer; a layer change is an mcon via.
        runs, current = [], [path[0]]
        for p in path[1:]:
            if p[2] != current[-1][2]:
                runs.append(current); current = [p]
                x, y = p[0]*step, p[1]*step
                for layer, size in (('mcon', r['mcon_size']), ('li1', r['mcon_size']), ('met1', met1_pad)):
                    box = box_at((x, y), (x, y), size)
                    shapes.append({'layer': layer, 'box': box, 'net': net})
                    keep(layer, box)
                vias.append({'net': net, 'point_um': [round(x, 9), round(y, 9)]})
            else:
                current.append(p)
        runs.append(current)
        for run in runs:
            layer = run[0][2]
            # Collapse straight runs without changing the routed geometry.
            turns = [run[0]]
            for i in range(1, len(run)-1):
                a, b, c = run[i-1], run[i], run[i+1]
                if (b[0]-a[0], b[1]-a[1]) != (c[0]-b[0], c[1]-b[1]):
                    turns.append(b)
            if len(run) > 1:
                turns.append(run[-1])
            physical = [(round(x*step, 9), round(y*step, 9)) for x, y, _ in turns]
            for a, b in zip(physical, physical[1:]):
                box = box_at(a, b, wire[layer])
                shapes.append({'layer': layer, 'box': box, 'net': net})
                keep(layer, box)
            if len(physical) > 1:
                routes.append({'net': net, 'layer': layer, 'points_um': physical})
        connected.extend((ix, iy) for ix, iy, layer in path if layer == 'li1')
        if sink is None:
            met1_points.setdefault(net, []).extend((ix, iy) for ix, iy, layer in path if layer == 'met1')

    def route_all(order):
        for net in order:
            for connected, target in connections(net):
                origin = min(connected, key=lambda p: abs(p[0]-target[0])+abs(p[1]-target[1]))
                # met1 only where li1 alone cannot connect, and only if allowed; the
                # folded-finger nets of the policy go to met1 directly.
                # A connection on met1 starts from every point the net already
                # reaches, its met1 wire included: otherwise a second via has to sit an
                # mcon pitch away from the first, joined to it by an li1 jog.
                starts = tuple((*p, 'li1') for p in connected) + tuple((*p, 'met1') for p in met1_points.get(net, []))
                if net in met1_nets:
                    path = spaced_search(origin, target, net, True, more_starts=starts, prefer_met1=True)
                    lay(net, connected, path)
                    continue
                try:
                    path = search(origin, target, net, False)
                except ValueError:
                    if not allow_met1:
                        raise
                    path = spaced_search(origin, target, net, True, more_starts=starts, prefer_met1=True)
                lay(net, connected, path)

    def rip_up_and_reroute(order, first_error):
        """Rip-up and reroute, used only when every net order above failed. The supply
        stubs are laid first and kept. Each signal net is then routed with the other
        signal nets' wires soft: the path may cross them at a cost per grid step that
        grows with every rip-up, and each net it crosses is removed and queued again.
        Fixed shapes (landings, rails, supply stubs) stay hard. Gives up after
        policies['rip_up_limit'] rip-ups."""
        plan['shapes'][:], plan['routes'][:] = saved[0], []
        plan['vias'] = []
        met1_points.clear()
        for k in occupied:
            occupied[k][:] = saved[1][k]
        mcons[:] = saved[2]
        route_all(['VPWR', 'VGND'])
        hard = {'li1': list(occupied['li1']), 'met1': list(occupied['met1']), 'mcon': list(mcons)}
        limit = int(policies['rip_up_limit'])
        base_penalty = float(policies['rip_up_penalty'])
        records, prints, queue, rips = {}, {}, list(order), 0
        while queue:
            net = queue.pop(0)
            record = {'shapes': [], 'routes': [], 'vias': [], 'li1': [], 'met1': [], 'mcon': []}
            for connected, target in connections(net):
                origin = min(connected, key=lambda p: abs(p[0]-target[0])+abs(p[1]-target[1]))
                soft = {n: f for n, f in prints.items() if n != net}
                try:
                    # The net's own vias from its earlier connections are hard too (mcon.2).
                    own = dict(hard, mcon=hard['mcon'] + record['mcon'])
                    path = spaced_search(origin, target, net, allow_met1, own, soft, base_penalty * (1 + rips))
                except ValueError as exc:
                    raise ValueError(f'{exc} (rip-up and reroute; first failure: {first_error})') from None
                for other in sorted(crossed(path, soft)):
                    del records[other], prints[other]
                    queue.append(other)
                    rips += 1
                lay(net, connected, path, record)
            records[net], prints[net] = record, footprint(record)
            if rips > limit:
                raise ValueError(f'rip-up and reroute gave up after {rips} rip-ups (first failure: {first_error})')
        for net in order:
            plan['shapes'].extend(records[net]['shapes'])
            plan['routes'].extend(records[net]['routes'])
            plan['vias'].extend(records[net]['vias'])
        plan['routing_order'] = order
        plan['rip_ups'] = rips

    # Nets are routed one at a time, so an early net can wall off a later one. Try a
    # few deterministic orders (supply stubs always first) before giving up.
    signals = [net for net in plan['terminals'] if net not in rail_y]
    def span(net):
        xs = [p[0] for p in plan['terminals'][net]]
        return max(xs) - min(xs)
    orders = [sorted(signals), sorted(signals, key=span), sorted(signals, key=lambda n: -span(n)),
              sorted(signals, key=lambda n: -len(plan['terminals'][n]))]
    shuffler = random.Random(0)
    for _ in range(int(policies.get('routing_orders', 12)) - len(orders)):
        orders.append(shuffler.sample(signals, len(signals)))
    saved = (list(plan['shapes']), {k: list(v) for k, v in occupied.items()}, list(mcons))
    best = None
    for order in orders:
        plan['shapes'][:], plan['routes'][:] = saved[0], []
        plan['vias'] = []
        met1_points.clear()      # met1 wires of the policy's nets belong to one order's attempt
        for k in occupied:
            occupied[k][:] = saved[1][k]
        mcons[:] = saved[2]
        try:
            route_all(['VPWR', 'VGND'] + order)
        except ValueError as exc:
            best = best or (None, exc)
            continue
        # Prefer the order that needs the fewest vias (ideally li1 only).
        result = (len(plan['vias']), list(plan['shapes']), list(plan['routes']), list(plan['vias']), order)
        if best is None or best[0] is None or result[0] < best[0]:
            best = result
        if result[0] == 0:
            break
    if best is None or best[0] is None:
        if not rip_up:
            raise best[1]
        rip_up_and_reroute(orders[0], best[1])
        return
    _, plan['shapes'][:], plan['routes'][:], plan['vias'], plan['routing_order'] = best


def route_negotiated(plan, spec, frame, r, policies, dim, first_pass_only=False):
    """li1-only router with negotiated congestion that also places the diffusion contacts.

    plan_geometry(movable=True) leaves every signal diffusion contact unplaced: only its
    x (between its gates) and the range its licon may take (licon.5c inside its node's
    diffusion, `y_range_um`) are fixed. The router lands each net on a contact anywhere
    in that range, so a contact sits where its wire passes (HD's diffusion contacts are
    long li1 bars on the net's own route), and the li1 landing (licon + li.5, as
    before) is drawn there. Gate landings, supply contacts and the rails stay fixed and
    are hard obstacles to other nets.

    Negotiated congestion (PathFinder, [MB95]): every signal net is routed in turn by A*
    from everything it already reaches to the nearest terminal it does not (Prim), at a
    cost per grid step of (1 + history) x (1 + present x nets already there), where
    "there" means inside another net's li.3 keep-out. In the first iteration present
    is 0, so each net takes its shortest legal path; a net that has none even then fails
    at once. While two nets still share room, every net is ripped up and routed again,
    present grows by `negotiation_present_growth` per iteration and history by
    `negotiation_history_cost` where nets overlapped, so the nets that have other
    options move. After `negotiation_iterations` iterations without a legal result the
    attempt fails. Clearances are those of route_li1 (rules.json, square corners, half
    li.3 at the cell edges); a landing off the routing grid by up to half a step keeps
    that half step more. Not a substitute for DRC, extraction or LVS.
    """
    step = float(policies['routing_grid'])
    li_w, li_h = dim['li1_landing_um']
    wire, space = r['li_width'], r['li_space']
    width_um = plan['width_um']
    nx, ny = math.floor(width_um / step + 1e-9), round(frame['height'] / step)
    columns = nx + 1
    size = columns * (ny + 1)
    supplies = ('VPWR', 'VGND')
    movable = [c for c in plan['contacts'] if c.get('movable')]
    # A landing off the grid by dx keeps dx more room on each side.
    off_grid = max([abs(c['x_um'] - round(c['x_um'] / step) * step) for c in movable] + [0.0])

    def raster(boxes, mx, my):
        """Grid points strictly closer than (mx, my) to any of the boxes, as an int of bytes."""
        grid = bytearray(size)
        for b in boxes:
            x0 = max(0, math.floor((b[0] - mx) / step + 1e-9) + 1)
            x1 = min(nx, math.ceil((b[2] + mx) / step - 1e-9) - 1)
            y0 = max(0, math.floor((b[1] - my) / step + 1e-9) + 1)
            y1 = min(ny, math.ceil((b[3] + my) / step - 1e-9) - 1)
            if x1 < x0 or y1 < y0:
                continue
            ones = b'\x01' * (x1 - x0 + 1)
            for iy in range(y0, y1 + 1):
                grid[iy * columns + x0:iy * columns + x1 + 1] = ones
        return int.from_bytes(grid, 'little')

    wire_margin = (space + wire / 2, space + wire / 2)
    # A movable contact's landing is tall (li.5 above and below the licon) or wide (li.5
    # left and right of it, as HD's o22ai_1 Y contact, which then sits 0.08 um lower).
    sizes = ((li_w, li_h), (li_h, li_w))
    land_margins = [(space + w / 2 + off_grid, space + h / 2) for w, h in sizes]
    # Half of li.3 at the cell edges: every HD neighbour keeps the other half.
    lo_x = math.ceil((space / 2 + wire / 2) / step - 1e-9)
    hi_x = math.floor((width_um - space / 2 - wire / 2) / step + 1e-9)
    edge = bytearray(size)
    for iy in range(ny + 1):
        edge[iy * columns:iy * columns + lo_x] = b'\x01' * lo_x
        if hi_x < nx:
            edge[iy * columns + hi_x + 1:(iy + 1) * columns] = b'\x01' * (nx - hi_x)
    edge = int.from_bytes(edge, 'little')

    fixed = defaultdict(list)
    for s in plan['shapes']:
        if s['layer'] == 'li1':
            fixed[s['net']].append(s['box'])
    fixed_w = {n: raster(b, *wire_margin) for n, b in fixed.items()}
    fixed_l = [{n: raster(b, *m) for n, b in fixed.items()} for m in land_margins]
    nets = sorted({n for n in plan['terminals'] if n not in supplies} | {c['net'] for c in movable})

    def blocked_for(net, rasters):
        total = edge
        for owner, value in rasters.items():
            if owner != net:
                total |= value
        return total.to_bytes(size, 'little')

    hard_w = {n: blocked_for(n, fixed_w) for n in nets}
    hard_l = [{n: blocked_for(n, f) for n in nets} for f in fixed_l]

    # Terminals: a fixed landing (its grid points inside the net's own li1 shape) or a
    # movable contact (its column of legal licon positions).
    terms = {}
    for net in nets:
        own = []
        for p in dict.fromkeys(tuple(p) for p in plan['terminals'].get(net, [])):
            box = next((b for b in fixed[net] if b[0] - 1e-9 <= p[0] <= b[2] + 1e-9
                        and b[1] - 1e-9 <= p[1] <= b[3] + 1e-9), None)
            if box is None:
                box = [p[0], p[1], p[0], p[1]]
            # A wire joins a fixed landing on its centre line only (any point along it):
            # the wire then covers the landing's full width. Off the centre line a wire
            # and a landing can overlap corner to corner, a neck narrower than li.1
            # (nor4_stack_1, first version: 0.13 um diagonally).
            x0, x1 = math.ceil(box[0] / step - 1e-9), math.floor(box[2] / step + 1e-9)
            y0, y1 = math.ceil(box[1] / step - 1e-9), math.floor(box[3] / step + 1e-9)
            if box[3] - box[1] >= box[2] - box[0]:
                x0 = x1 = round(p[0] / step)
            else:
                y0 = y1 = round(p[1] / step)
            if x1 < x0:
                x0 = x1 = round(p[0] / step)
            if y1 < y0:
                y0 = y1 = round(p[1] / step)
            sinks = [iy * columns + ix for iy in range(y0, y1 + 1) for ix in range(x0, x1 + 1)
                     if not hard_w[net][iy * columns + ix]]
            if not sinks:
                raise ValueError(f'LI1 terminal clearance prevents routing net {net}')
            own.append({'kind': 'fixed', 'sinks': sinks, 'rect': (x0, x1, y0, y1)})
        for c in movable:
            if c['net'] != net:
                continue
            ix = round(c['x_um'] / step)
            low, high = c['y_range_um']
            y0, y1 = math.ceil(low / step - 1e-9), math.floor(high / step + 1e-9)
            # Orientations whose landing stays half li.3 inside the cell edges.
            fits = [o for o, (w, _) in enumerate(sizes)
                    if c['x_um'] - w / 2 >= space / 2 - 1e-9 and c['x_um'] + w / 2 <= width_um - space / 2 + 1e-9]
            orients = {}
            for iy in range(y0, y1 + 1):
                k = iy * columns + ix
                legal = [o for o in fits if not hard_l[o][net][k]]
                if legal and not hard_w[net][k]:
                    orients[k] = legal
            if not orients:
                raise ValueError(f'No legal li1 landing for the {net} contact at x={c["x_um"]}')
            own.append({'kind': 'column', 'sinks': list(orients), 'orients': orients,
                        'rect': (ix, ix, y0, y1), 'contact': c})
        terms[net] = own

    hist = [0.0] * size

    def search(net, sources, targets, occ_w, occ_l, pres):
        """A* from sources {k: cost} to the nearest sink of any target terminal."""
        T = terms[net]
        sink_of, landing = {}, {}
        for t in targets:
            for k in T[t]['sinks']:
                sink_of.setdefault(k, t)
                if T[t]['kind'] == 'column':
                    landing[k] = T[t]['orients'][k]
        rects = [T[t]['rect'] for t in targets]

        def h(k):
            iy, ix = divmod(k, columns)
            return min(max(0, x0 - ix, ix - x1) + max(0, y0 - iy, iy - y1) for x0, x1, y0, y1 in rects)

        blocked = hard_w[net]
        best, parent, heap, serial = {}, {}, [], count()
        for k, c0 in sources.items():
            best[k] = c0
            heapq.heappush(heap, (c0 + h(k), c0, next(serial), k))
        while heap:
            _, g, _, k = heapq.heappop(heap)
            if g != best.get(k):
                continue
            if k in sink_of:
                path = [k]
                while path[-1] in parent:
                    path.append(parent[path[-1]])
                return list(reversed(path)), sink_of[k]
            iy, ix = divmod(k, columns)
            for nk, ok in ((k + 1, ix < nx), (k - 1, ix > 0), (k + columns, iy < ny), (k - columns, iy > 0)):
                if not ok or blocked[nk]:
                    continue
                weight = 1.0 + hist[nk]
                c = g + weight * (1.0 + pres * occ_w[nk])
                if nk in landing:
                    c += weight * pres * min(occ_l[o][nk] for o in landing[nk])
                if c < best.get(nk, math.inf):
                    best[nk] = c
                    parent[nk] = k
                    heapq.heappush(heap, (c + h(nk), c, next(serial), nk))
        raise ValueError(f'No li1 route found for {net} at this width (negotiated)')

    def route_net(net, occ_w, occ_l, pres):
        """Prim over the net's terminals; returns (paths, {terminal: (landing point,
        orientation)}), the orientation the least crowded legal one."""
        T = terms[net]

        def orient(t, k):
            return min(T[t]['orients'][k], key=lambda o: (occ_l[o][k], o))

        first = min(range(len(T)), key=lambda i: T[i]['kind'] != 'fixed')
        remaining = [i for i in range(len(T)) if i != first]
        tree, paths, placed = {}, [], {}
        if T[first]['kind'] == 'fixed':
            for k in T[first]['sinks']:
                tree[k] = 0.0
            sources, pending = tree, None
        else:
            sources = {k: (1.0 + hist[k]) * pres * occ_l[orient(first, k)][k] for k in T[first]['sinks']}
            pending = first
            if not remaining:
                k = min(sources, key=lambda q: (sources[q], q))
                return [[k]], {first: (k, orient(first, k))}
        while remaining:
            path, t = search(net, sources, remaining, occ_w, occ_l, pres)
            paths.append(path)
            if pending is not None:
                placed[pending], pending = (path[0], orient(pending, path[0])), None
            if T[t]['kind'] == 'column':
                placed[t] = (path[-1], orient(t, path[-1]))
            else:
                for k in T[t]['sinks']:
                    tree[k] = 0.0
            for k in path:
                tree[k] = 0.0
            remaining.remove(t)
            sources = tree
        return paths, placed

    def point(k):
        iy, ix = divmod(k, columns)
        return round(ix * step, 9), round(iy * step, 9)

    def wires(paths):
        """Straight runs of the paths as (from, to) point pairs."""
        out = []
        for path in paths:
            if len(path) < 2:
                continue
            turns = [path[0]]
            for a, b, c in zip(path, path[1:], path[2:]):
                if b - a != c - b:
                    turns.append(b)
            turns.append(path[-1])
            pts = [point(k) for k in turns]
            out.append(pts)
        return out

    def boxes_of(net, paths, placed):
        out = []
        for pts in wires(paths):
            for a, b in zip(pts, pts[1:]):
                out.append([round(min(a[0], b[0]) - wire / 2, 9), round(min(a[1], b[1]) - wire / 2, 9),
                            round(max(a[0], b[0]) + wire / 2, 9), round(max(a[1], b[1]) + wire / 2, 9)])
        for t, (k, o) in placed.items():
            x, y = terms[net][t]['contact']['x_um'], point(k)[1]
            w, h = sizes[o]
            out.append([round(x - w / 2, 9), round(y - h / 2, 9), round(x + w / 2, 9), round(y + h / 2, 9)])
        return out

    order = sorted(nets, key=lambda n: (-len(terms[n]), n))
    iterations = int(policies.get('negotiation_iterations', 30))
    present0 = float(policies.get('negotiation_present_cost', 0.5))
    growth = float(policies.get('negotiation_present_growth', 1.5))
    history_step = float(policies.get('negotiation_history_cost', 1.0))
    patience = int(policies.get('negotiation_patience', 8))
    fp_w = {n: 0 for n in nets}
    fp_l = [{n: 0 for n in nets} for _ in sizes]
    total_w, total_l = 0, [0 for _ in sizes]
    result = {}
    for iteration in range(iterations):
        pres = 0.0 if iteration == 0 else present0 * growth ** (iteration - 1)
        for net in order:
            total_w -= fp_w[net]
            for o in range(len(sizes)):
                total_l[o] -= fp_l[o][net]
            paths, placed = route_net(net, total_w.to_bytes(size, 'little'),
                                      [v.to_bytes(size, 'little') for v in total_l], pres)
            boxes = boxes_of(net, paths, placed)
            fp_w[net] = raster(boxes, *wire_margin)
            total_w += fp_w[net]
            for o, m in enumerate(land_margins):
                fp_l[o][net] = raster(boxes, *m)
                total_l[o] += fp_l[o][net]
            result[net] = (paths, placed, boxes)
        if first_pass_only:
            # Every net connected on its own (no other signal net present): the quick
            # check of the driver (section 9.6) needs nothing more.
            return True
        overlap, crowded = set(), set()
        for net in order:
            others_w = (total_w - fp_w[net]).to_bytes(size, 'little')
            others_l = [(total_l[o] - fp_l[o][net]).to_bytes(size, 'little') for o in range(len(sizes))]
            paths, placed, _ = result[net]
            mine = {k for path in paths for k in path if others_w[k]}
            mine |= {k for k, o in placed.values() if others_l[o][k] or others_w[k]}
            if mine:
                crowded.add(net)
            overlap |= mine
        trace = plan.setdefault('negotiation_trace', [])
        trace.append({'present': round(pres, 3), 'overlap_points': len(overlap), 'nets': sorted(crowded)})
        if not overlap:
            break
        # Stop early once the overlap has not shrunk for `negotiation_patience` iterations.
        fewest = min(t['overlap_points'] for t in trace)
        if len(trace) > patience and min(t['overlap_points'] for t in trace[-patience:]) > fewest:
            plan['negotiation_last_boxes'] = {n: v[2] for n, v in result.items()}
            raise ValueError(f'No li1 route found at this width (negotiated: {", ".join(sorted(crowded))} '
                             f'still overlap; no progress in {patience} iterations)')
        for k in overlap:
            hist[k] += history_step
    else:
        raise ValueError(f'No li1 route found at this width (negotiated: {", ".join(sorted(crowded))} '
                         f'still overlap after {iterations} iterations)')

    pin_size = dim['li1_pin_um']
    plan['vias'] = []
    for net in order:
        paths, placed, _ = result[net]
        for pts in wires(paths):
            for a, b in zip(pts, pts[1:]):
                plan['shapes'].append({'layer': 'li1', 'net': net, 'box': [
                    round(min(a[0], b[0]) - wire / 2, 9), round(min(a[1], b[1]) - wire / 2, 9),
                    round(max(a[0], b[0]) + wire / 2, 9), round(max(a[1], b[1]) + wire / 2, 9)]})
            plan['routes'].append({'net': net, 'layer': 'li1', 'points_um': [list(p) for p in pts]})
        for t, (k, o) in sorted(placed.items()):
            c = terms[net][t]['contact']
            x, y = c['x_um'], point(k)[1]
            w, h = sizes[o]
            landing = [round(x - w / 2, 9), round(y - h / 2, 9), round(x + w / 2, 9), round(y + h / 2, 9)]
            plan['shapes'].append({'layer': 'li1', 'box': landing, 'net': net})
            half = r['licon_size'] / 2
            plan['shapes'].append({'layer': 'licon', 'net': net,
                                   'box': [round(x - half, 9), round(y - half, 9), round(x + half, 9), round(y + half, 9)]})
            c['point_um'], c['landing_box'] = [x, y], landing
            plan['terminals'].setdefault(net, []).append((x, y))
    if plan.get('output_pin_um') is None:
        # The first output landing whose met1 access pad keeps met1.2 from the rails.
        rails = [s['box'] for s in plan['shapes'] if s['layer'] == 'met1']
        pad = via_pad(r)
        points = plan['terminals'][spec['output']]
        output = next((p for p in points if all(square_gap((p[0] - pad / 2, p[1] - pad / 2, p[0] + pad / 2, p[1] + pad / 2), b)
                                                 >= r['met1_space'] - 1e-6 for b in rails)), points[0])
        plan['output_pin_um'] = list(output)
        plan['shapes'].append({'layer': 'li1_pin', 'net': spec['output'], 'box': [
            round(output[0] - pin_size / 2, 9), round(output[1] - pin_size / 2, 9),
            round(output[0] + pin_size / 2, 9), round(output[1] + pin_size / 2, 9)]})
    plan['router'] = 'negotiated'
    plan['routing_order'] = order
    plan['negotiation_iterations'] = iteration + 1


def stack_contacts(plan, r, li_w, li_h):
    """Put a diffusion contact directly under the via its net climbs to met1 through.

    The router lands every connection on a contact's li1 landing and may climb to met1
    a little away from it (a via's met1 pad cannot sit next to a met1 rail, for
    example), joined by an li1 stub: current then runs licon -> li1 stub -> mcon. Where
    a contact's only connection is one straight vertical li1 stub to a via of its net,
    the via point lies inside the contact's own diffusion range (licon.5c), and an li1
    landing centred there keeps li.3 to every other net, the licon, its landing and any
    pin move under the via and the stub and the old landing go: licon and mcon stack,
    with no li1 in the current path. The contact keeps its x, so licon.11 to the gates
    holds. Returns the moves made.
    """
    eps = 1e-6
    same = lambda a, b: abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps
    vias = plan.get('vias', [])

    def gap(a, b):
        dx = max(a[0] - b[2], b[0] - a[2], 0)
        dy = max(a[1] - b[3], b[1] - a[3], 0)
        # Square corners, as Magic checks spacing (and the router's rasters assume).
        return max(dx, dy)

    moves = []
    for c in plan['contacts']:
        if c['type'] != 'diffusion' or 'landing_box' not in c:
            continue
        net, here = c['net'], tuple(c['point_um'])
        touching = [rt for rt in plan['routes'] if rt['net'] == net and rt['layer'] == 'li1'
                    and any(same(p, here) for p in rt['points_um'])]
        if len(touching) != 1 or len(touching[0]['points_um']) != 2:
            continue
        a, b = touching[0]['points_um']
        if not (same(a, here) or same(b, here)):
            continue
        there = tuple(b if same(a, here) else a)
        if abs(there[0] - here[0]) > eps or not any(v['net'] == net and same(v['point_um'], there) for v in vias):
            continue
        low, high = c['y_range_um']
        if not low - eps <= there[1] <= high + eps:
            continue
        x, y = there
        stub = [round(v, 9) for v in (x - r['li_width'] / 2, min(here[1], y) - r['li_width'] / 2,
                                      x + r['li_width'] / 2, max(here[1], y) + r['li_width'] / 2)]
        others = [s['box'] for s in plan['shapes'] if s['layer'] == 'li1' and s['net'] != net]
        landing = None
        for w, h in ((li_w, li_h), (li_h, li_w)):
            box = [round(v, 9) for v in (x - w / 2, y - h / 2, x + w / 2, y + h / 2)]
            if box[0] >= r['li_space'] / 2 - eps and box[2] <= plan['width_um'] - r['li_space'] / 2 + eps \
                    and all(gap(box, o) >= r['li_space'] - eps for o in others):
                landing = box
                break
        if landing is None:
            continue
        old_landing = c['landing_box']
        keep = []
        for s in plan['shapes']:
            if s['net'] == net and s['layer'] == 'li1' and (s['box'] == stub or s['box'] == old_landing):
                continue
            if s['net'] == net and s['layer'] in ('licon', 'li1_pin') and same(
                    ((s['box'][0] + s['box'][2]) / 2, (s['box'][1] + s['box'][3]) / 2), here):
                dx, dy = x - here[0], y - here[1]
                s = dict(s, box=[round(s['box'][0] + dx, 9), round(s['box'][1] + dy, 9),
                                 round(s['box'][2] + dx, 9), round(s['box'][3] + dy, 9)])
            keep.append(s)
        keep.append({'layer': 'li1', 'box': landing, 'net': net})
        plan['shapes'][:] = keep
        plan['routes'].remove(touching[0])
        plan['terminals'][net] = [list(there) if same(p, here) else p for p in plan['terminals'][net]]
        if same(plan.get('output_pin_um', (None, None)), here):
            plan['output_pin_um'] = [x, y]
        c['point_um'], c['landing_box'] = [x, y], landing
        moves.append({'net': net, 'from_um': list(here), 'to_um': [x, y]})
    return moves


def multiply_cuts(plan, r, li_w, licons=True, mcons=True):
    """More cuts in parallel, after routing: nothing already placed moves.

    Diffusion contacts (signal and supply): the contact's licon is replaced by the
    largest column of licons, on the licon.2 pitch, that fits inside the node's
    diffusion range (licon.5c, `y_range_um`) under one li1 bar (li.5's 0.08 um past both
    ends, or on both sides) kept li.3 from every other net's li1 and covering the
    contact point; among
    columns of that size the one centred nearest the original licon wins. The landing
    the route attaches to stays. The contact keeps its x, so licon.11 to the gates holds.
    Vias: a via that sits on a diffusion contact's licon column (licon and mcon stacked)
    gets its single mcon replaced by the largest array along that column, on the mcon.2
    pitch, spanning the via and as nearly centred on it as fits (on a two-licon column
    the two mcons sit over the two licons), each mcon inside the column's li1 bar (no li1
    is added), under one met1 bar
    (the via's pad wide), while the cuts keep mcon.2 from every other cut and the met1
    bar keeps met1.2 from other nets' met1 (rails and pin access pads included) and half
    of it from the cell edges: current from each licon then climbs straight to met1. A via elsewhere (on a wire, on
    a single-licon landing) keeps one mcon: further mcons along a wire, joined by li1
    added for them, would carry next to no current (both layers already run there) and
    take room from other nets and from the pins' met1 access. Returns the cut counts.
    """
    eps = 1e-6
    width, height = plan['width_um'], plan['height_um']

    def gap(a, b):
        dx = max(a[0] - b[2], b[0] - a[2], 0)
        dy = max(a[1] - b[3], b[1] - a[3], 0)
        # Square corners, as Magic checks spacing (and the router's rasters assume).
        return max(dx, dy)

    def box(cx, cy, w, h=None):
        h = w if h is None else h
        return [round(cx - w / 2, 9), round(cy - h / 2, 9), round(cx + w / 2, 9), round(cy + h / 2, 9)]

    def bar(p, q, w):
        return [round(min(p[0], q[0]) - w / 2, 9), round(min(p[1], q[1]) - w / 2, 9),
                round(max(p[0], q[0]) + w / 2, 9), round(max(p[1], q[1]) + w / 2, 9)]

    def clear(candidate, layer, net, space, same_net_too=False):
        return all(gap(candidate, s['box']) >= space - eps for s in plan['shapes']
                   if s['layer'] == layer and (same_net_too or s['net'] != net))

    def inside(b, margin):
        return b[0] >= margin - eps and b[2] <= width - margin + eps and b[1] >= -eps and b[3] <= height + eps

    counts = {'licons_added': 0, 'contacts': [], 'mcons_added': 0, 'vias': []}
    if licons:
        pitch = r['licon_size'] + r['licon_space']
        reach = r['licon_size'] / 2 + r['li_contact_enclosure']
        step = 2 * r['grid']
        for c in plan['contacts']:
            if c['type'] != 'diffusion' or 'y_range_um' not in c:
                continue
            net, (x, y) = c['net'], c['point_um']
            low, high = c['y_range_um']
            mine = [s for s in plan['shapes'] if s['layer'] == 'licon' and s['net'] == net
                    and abs((s['box'][0] + s['box'][2]) / 2 - x) < eps and abs((s['box'][1] + s['box'][3]) / 2 - y) < eps]
            others = [s['box'] for s in plan['shapes'] if s['layer'] == 'licon' and s not in mine]
            best = None
            # Most licons first; for each count every grid placement inside the range,
            # nearest the original licon first. Its li1 (one bar over the column, li.5
            # past both ends) must keep li.3 from other nets; the landing stays.
            for n in range(int((high - low + eps) // pitch) + 1, 1, -1):
                span = (n - 1) * pitch
                places = [round(low + i * step, 9) for i in range(int((high - span - low + eps) / step) + 1)]
                places.sort(key=lambda a: abs(a + span / 2 - y))
                for a in places:
                    ys = [round(a + i * pitch, 9) for i in range(n)]
                    cuts = [box(x, v, r['licon_size']) for v in ys]
                    if not all(gap(cut, o) >= r['licon_space'] - eps for cut in cuts for o in others):
                        continue
                    # li.5: 0.08 um past both ends of the column (narrow bar), or on both
                    # sides of it (wide bar, flush with the end licons).
                    side = r['licon_size'] / 2 + r['li_contact_enclosure']
                    for li in ([round(x - li_w / 2, 9), round(ys[0] - reach, 9), round(x + li_w / 2, 9), round(ys[-1] + reach, 9)],
                               [round(x - side, 9), round(ys[0] - r['licon_size'] / 2, 9),
                                round(x + side, 9), round(ys[-1] + r['licon_size'] / 2, 9)]):
                        if (clear(li, 'li1', net, r['li_space']) and inside(li, r['li_space'] / 2)
                                and li[1] <= y + eps and li[3] >= y - eps):
                            best = (ys, li, cuts)
                            break
                    if best:
                        break
                if best:
                    break
            if best:
                ys, li, cuts = best
                plan['shapes'][:] = [s for s in plan['shapes'] if s not in mine]
                plan['shapes'] += [{'layer': 'licon', 'box': cut, 'net': net} for cut in cuts]
                plan['shapes'].append({'layer': 'li1', 'box': li, 'net': net})
                c['licon_y_um'] = ys
                c['li1_bar_box'] = li
            n = len(best[0]) if best else 1
            c['licons'] = n
            counts['licons_added'] += n - 1
            counts['contacts'].append({'net': net, 'point_um': [x, y], 'licons': n})
    if mcons:
        pitch = r['mcon_size'] + r['mcon_space']
        pad = via_pad(r)
        access = [{'layer': 'met1', 'box': p['met1_box'], 'net': p['net']} for p in plan.get('pin_access', [])]

        def within(b, outer):
            return b[0] >= outer[0] - eps and b[1] >= outer[1] - eps and b[2] <= outer[2] + eps and b[3] <= outer[3] + eps

        for v in plan.get('vias', []):
            net, start = v['net'], tuple(v['point_um'])
            array = [start]
            # The licon column (its li1 bar) the via stands on, if any.
            columns = [c['li1_bar_box'] for c in plan['contacts']
                       if c['type'] == 'diffusion' and c['net'] == net and c.get('li1_bar_box')
                       and within(box(*start, r['mcon_size']), c['li1_bar_box'])]
            if columns:
                column = columns[0]
                vertical = column[3] - column[1] >= column[2] - column[0]
                length = (column[3] - column[1]) if vertical else (column[2] - column[0])
                own = box(*start, r['mcon_size'])
                others = [s['box'] for s in plan['shapes'] if s['layer'] == 'mcon' and s['box'] != own]
                # The largest array along the column that spans the via (its first and
                # last mcon on either side of it), nearest centred on the via first; it
                # replaces the via's single mcon (the via's li1 and met1 stay, so the
                # route still lands on it).
                axis = 1 if vertical else 0
                low, high = column[axis] + r['mcon_size'] / 2, column[axis + 2] - r['mcon_size'] / 2
                step = 2 * r['grid']
                found = None
                for n in range(int((length - r['mcon_size'] + eps) // pitch) + 1, 1, -1):
                    span = (n - 1) * pitch
                    firsts = [round(low + i * step, 9) for i in range(int((high - span - low + eps) / step) + 1)]
                    firsts = [a for a in firsts if a - eps <= start[axis] <= a + span + eps]
                    firsts.sort(key=lambda a: abs(a + span / 2 - start[axis]))
                    for a in firsts:
                        points = [(start[0], round(a + i * pitch, 9)) if vertical else (round(a + i * pitch, 9), start[1])
                                  for i in range(n)]
                        cuts = [box(*p, r['mcon_size']) for p in points]
                        m1 = bar(points[0], points[-1], pad)
                        if (all(within(c, column) for c in cuts)
                                and all(gap(c, o) >= r['mcon_space'] - eps for c in cuts for o in others)
                                and clear(m1, 'met1', net, r['met1_space']) and inside(m1, r['met1_space'] / 2)
                                and all(gap(m1, a2['box']) >= r['met1_space'] - eps for a2 in access if a2['net'] != net)):
                            found = (points, cuts, m1)
                            break
                    if found:
                        break
                if found:
                    points, cuts, m1 = found
                    plan['shapes'][:] = [s for s in plan['shapes'] if not (s['layer'] == 'mcon' and s['box'] == own)]
                    plan['shapes'] += [{'layer': 'mcon', 'box': c, 'net': net} for c in cuts]
                    plan['shapes'].append({'layer': 'met1', 'box': m1, 'net': net})
                    array = points
                    v['mcon_points_um'] = [list(p) for p in points]
            v['mcons'] = len(array)
            counts['mcons_added'] += len(array) - 1
            counts['vias'].append({'net': net, 'point_um': list(start), 'mcons': len(array)})
    return counts


def fill_notches(plan, r, layer, space, skip=(), width=None):
    """Prepare legal same-net wire shapes on one layer before the drawing stage sees them.
    With width, every merged net is also checked for necks narrower than it (Euclidean, as
    Magic measures a diagonal neck), so the attempt fails here rather than in DRC."""
    grid=r['grid']
    regions={}
    for shape in plan['shapes']:
        if shape['layer']==layer and shape['net'] not in skip:
            regions.setdefault(shape['net'],kdb.Region()).insert(
                kdb.Box(*[round(x/grid) for x in shape['box']]))
    growth=math.ceil((space/2+grid)/grid-1e-9)
    minimum=round(space/grid)
    merged={net:(region.merged().sized(growth).sized(-growth)|region).merged()
            for net,region in regions.items()}
    names=sorted(merged)
    for i,a in enumerate(names):
        if not merged[a].space_check(minimum).is_empty():
            raise ValueError(f'Unresolved same-net {layer} notch on {a}')
        if width is not None and not merged[a].width_check(round(width/grid)).is_empty():
            raise ValueError(f'{layer} of {a} narrower than {width} um somewhere')
        for b in names[i+1:]:
            if not (merged[a]&merged[b]).is_empty() or not merged[a].separation_check(merged[b],minimum).is_empty():
                raise ValueError(f'{layer} notch filling cannot preserve spacing between {a} and {b}')
    plan['shapes']=[shape for shape in plan['shapes'] if shape['layer']!=layer or shape['net'] in skip]
    added={}
    for net,region in merged.items():
        added[net]=round((region.area()-regions[net].merged().area())*grid*grid,9)
        for polygon in region.each():
            for tile in polygon.decompose_trapezoids():
                box=tile.bbox()
                if tile.area()!=box.area():raise ValueError(f'Non-Manhattan {layer} notch fill')
                plan['shapes'].append({'layer':layer,'net':net,'box':[
                    round(x*grid,9) for x in (box.left,box.bottom,box.right,box.top)]})
    plan[f'{layer}_notch_fill_added_area_um2']=added


def fill_li1_notches(plan,r):
    fill_notches(plan, r, 'li1', r['li_space'], width=r['li_width'])
    # The met1 rails are the frame's; only routed signal met1 is merged.
    fill_notches(plan, r, 'met1', r['met1_space'], skip=('VPWR', 'VGND'), width=r['met1_width'])


def write_schematic(spec, destination):
    """Write intended input connectivity; this is not an extracted/LVS result."""
    ports=[*spec['inputs'],'VGND','VNB','VPB','VPWR',spec['output']]
    lines=['* Intended schematic from supplied transistor connections.',
           '* SKY130 model files use um-scale W/L convention; load the course model setup.',
           '.subckt '+spec['name']+' '+' '.join(ports)]
    for d in spec['devices']:
        lines.append(f"X{d['id']} {d['drain']} {d['gate']} {d['source']} {d['bulk']} "
                     f"{d['model']} w={d['w_um']:.9g} l={d['l_um']:.9g}")
    lines.append('.ends '+spec['name'])
    destination.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def bind_normalized_request(spec,reader_output):
    """Consume the existing request reader's result without duplicating its parser."""
    if reader_output.get('status')!='pass':
        raise ValueError('The existing request reader must accept the request first')
    request=reader_output['request']
    if request['inputs']!=spec['inputs'] or request['output']!=spec['output']:
        raise ValueError('Request pins do not match the supplied physical cell specification')
    if request['sizing']!=spec.get('sizing','hd'):
        raise ValueError('Upstream must supply transistor sizes for the requested sizing policy')
    if 'truth_table' not in spec or int(request['truth_table'],16)!=int(spec['truth_table'],16):
        raise ValueError('Request function does not match the supplied physical example')
    result=dict(spec)
    result['name']=request['name']
    result['request_id']=request['request_id']
    return result


def prepare_layout(spec,rules_document,frame):
    """Return the complete dimensions/connections/geometry contract, with no GDS."""
    r=load_rules(rules_document)
    policies=rules_document['policies']
    # The handout allows met1 for wiring only inside a multi-stage cell.
    stage_outputs={d['drain'] for d in spec['devices']}&{d['source'] for d in spec['devices']}
    stage_outputs={n for n in stage_outputs if n not in ('VPWR','VGND') and
                   {d['type'] for d in spec['devices'] if n in (d['source'],d['drain'])}=={'nmos','pmos'}}
    multi_stage=len(stage_outputs)>1
    # Try, for each placement euler.py ranked: li1 only with no channel, with each
    # candidate channel, with all of them, then a wider cell; narrowest first, first
    # without and then with rip-up and reroute. Only when every li1-only attempt has
    # failed does a multi-stage cell get li1 + met1 (handout Table 1: met1 "inside a
    # multi-stage cell only where li1 cannot route"; Table 4: "li1 inside"). A
    # single-stage cell gets met1 only in a last pass: handout Table 3 keeps met1 for the
    # rails there, so the plan records 'met1_in_single_stage_cell' and the HD comparison
    # flags it.
    failures=[]
    extra_sites=int(policies['maximum_extra_sites'])
    placements=[spec['euler_paths'],*spec.get('alternative_euler_paths',[])]
    def attempts(last_resort):
        if last_resort:
            if not multi_stage:
                for index,euler_paths in enumerate(placements):
                    rows=path_segments(euler_paths)
                    every=tuple(channel_candidates(spec,rows))
                    for channels,extra in dict.fromkeys([((),0),(every,0),(every,min(2,extra_sites))]):
                        yield index,euler_paths,rows,channels,True,extra
            return
        # Every placement at its own width first; extra sites only after all have failed
        # (HD nand2_1's mirror image routes at 3 sites where the first placement does not).
        for index,euler_paths in enumerate(placements):
            rows=path_segments(euler_paths)
            candidates=channel_candidates(spec,rows)
            every=tuple(candidates)
            options=[()]+[(c,) for c in candidates]+([every] if len(candidates)>1 else [])
            for channels in options:
                yield index,euler_paths,rows,channels,False,0
            if multi_stage:
                for channels in dict.fromkeys([(),every]):
                    yield index,euler_paths,rows,channels,True,0
        for index,euler_paths in enumerate(placements):
            rows=path_segments(euler_paths)
            every=tuple(channel_candidates(spec,rows))
            yield index,euler_paths,rows,every,False,min(2,extra_sites)
            if multi_stage:
                yield index,euler_paths,rows,every,True,min(2,extra_sites)
    # Tiers, each first without and then with rip-up and reroute: the li1-only attempts
    # above, then the li1 + met1 ones (multi-stage cells), then (single-stage cells only)
    # met1 as a last resort.
    def width_of(attempt):
        """The width an attempt would get (channels and extra sites included)."""
        index,euler_paths,rows,channels,allow_met1,extra=attempt
        try:
            by_id=validate(spec,frame,r,rows)
            return derive_dimensions(spec,frame,r,by_id,rows,channels,policies,extra)['width_sites']
        except ValueError:
            return math.inf
    negotiated=policies.get('negotiated_router',False)
    def tiers():
        normal=list(attempts(False))
        groups=[[a for a in normal if not a[4]],[a for a in normal if a[4]],list(attempts(True))]
        for g,group in enumerate(groups):
            # Narrowest first; equal widths keep the order above.
            ordered=sorted(group,key=width_of)
            for attempt in ordered:
                yield (*attempt,'fixed')
                # li1 only: at each width the negotiated router, which also places the
                # diffusion contacts, follows the fixed-landing one (route_negotiated).
                if negotiated and g==0:
                    yield (*attempt,'negotiated')
            # In the li1-only group the negotiated router does what rip-up and reroute
            # does and moves the contacts as well; in the 27 cells re-planned on 10
            # November no cell was routed by rip-up. With policy rip_up_with_negotiated
            # false (default) it is skipped there; the met1 groups keep it.
            if g==0 and negotiated and not policies.get('rip_up_with_negotiated',False):
                continue
            for attempt in ordered:
                yield (*attempt,'rip_up')
    # Pin access (policy pin_access_met1): every pin shape reachable from met1 first; at
    # the same width, one pin per port (the labelled one) before a wider attempt.
    pin_access=policies.get('pin_access_met1',True)
    modes=('every_pin','one_pin_per_port') if pin_access else (None,)
    def ports(plan):
        return [(n,plan['terminals'][n][0]) for n in spec['inputs']]+[(spec['output'],plan['output_pin_um'])]
    plan=None
    lone={}     # quick check result per (placement, channels, extra sites): None or the reason
    for index,euler_paths,rows,channels,allow_met1,extra,router in tiers():
        rip_up=router=='rip_up'
        by_id=validate(spec,frame,r,rows)
        dimensions=derive_dimensions(spec,frame,r,by_id,rows,channels,policies,extra)
        attempt=(index,tuple(channels),extra)
        if negotiated and not allow_met1 and router in ('fixed','negotiated') and policies.get('lone_net_check',True):
            # Quick check (policy lone_net_check): route every net once with movable
            # contacts and no other signal net present (route_negotiated's first pass).
            # If one cannot connect even so, neither router can at this attempt: the
            # fixed router's net orders and the negotiated iterations are skipped.
            if attempt not in lone:
                probe=plan_geometry(spec,frame,r,dimensions,by_id,dimensions['width_sites'],rows,pin_access,movable=True)
                try:
                    lone[attempt]=route_negotiated(probe,spec,frame,r,policies,dimensions,first_pass_only=True) and None
                except ValueError as error:
                    lone[attempt]=str(error)
            if lone[attempt]:
                failures.append({'placement':index,'channels':list(channels),'met1':False,'extra_sites':extra,
                                 'rip_up':False,'router':router,'pin_access':None,
                                 'reason':f'skipped: a net cannot route even alone ({lone[attempt]})'})
                continue
        for mode in modes:
            # li1 only: no signal met1 can cover a pin, so one mode is enough.
            if router=='negotiated' and mode!=modes[0]:
                continue
            plan=plan_geometry(spec,frame,r,dimensions,by_id,dimensions['width_sites'],rows,pin_access,
                               movable=router=='negotiated')
            if mode:
                plan['pin_access_mode']=mode
                plan['pin_access']=pin_access_pads(plan['shapes'],r,ports(plan) if mode=='one_pin_per_port' else None)
            try:
                if router=='negotiated':
                    route_negotiated(plan,spec,frame,r,policies,dimensions)
                else:
                    route_li1(plan,spec,frame,r,policies,allow_met1,rip_up)
                    plan['router']=router
                if policies.get('stack_contacts_under_vias',True) and plan.get('vias'):
                    plan['contacts_stacked_under_vias']=stack_contacts(plan,r,*dimensions['li1_landing_um'])
                if policies.get('multiple_licons',True) or policies.get('mcon_arrays',True):
                    plan['parallel_cuts']=multiply_cuts(plan,r,dimensions['li1_landing_um'][0],
                                                        policies.get('multiple_licons',True),policies.get('mcon_arrays',True))
                    if plan['parallel_cuts']['mcons_added']:
                        fill_notches(plan,r,'met1',r['met1_space'],skip=('VPWR','VGND'))
                fill_li1_notches(plan,r)
                if mode:
                    # stack_contacts may have moved the output pin (under a via of its own net).
                    plan['pin_access']=pin_access_pads(plan['shapes'],r,ports(plan) if mode=='one_pin_per_port' else None)
                    check_pin_access(plan,r)
                break
            except ValueError as error:
                failures.append({'placement':index,'channels':list(channels),'met1':allow_met1,
                                 'extra_sites':extra,'rip_up':rip_up,'router':router,'pin_access':mode,
                                 'reason':str(error)})
                plan=None
        if plan is not None:
            break
    else:raise ValueError('Routing failed for every placement, channel set and allowed width: '+json.dumps(failures))
    plan['met1_in_single_stage_cell']=bool(plan.get('vias')) and not multi_stage
    if policies.get('folded_finger_nets_on_met1',False):
        plan['met1_by_policy']=sorted({n for d in spec['devices'] if d.get('finger_of')
                                       for n in (d['drain'],d['source'])} - {'VPWR','VGND'})
    spec=dict(spec,euler_paths=euler_paths,placement_used=index)

    # Resolve numeric layers and all label positions here. The second part
    # must only draw this data, without reinterpreting the electrical circuit.
    for shape in plan['shapes']:shape['gds']=list(LAYERS[shape['layer']])
    labels=[]
    def label(net,point,layer):
        labels.append({'text':net,'position_um':list(point),'gds':list(LAYERS[layer])})
    for net,y in (('VPWR',frame['power_y']),('VGND',frame['ground_y'])):
        for layer in ('met1_label','li1_label'):
            label(net,(plan['supply_pin_x_um'],y),layer)
    for net in spec['inputs']:label(net,plan['terminals'][net][0],'li1_label')
    label(spec['output'],plan['output_pin_um'],'li1_label')
    label('VPB',(plan['supply_pin_x_um'],frame['height']),'vpb_label')
    label('VNB',(plan['supply_pin_x_um'],0),'vnb_label')
    dimensions.update({'width_sites':plan['width_sites'],'cell_width_um':plan['width_um'],
                       'cell_height_um':frame['height'],'routing_retries':failures,
                       'rule_values_and_sources':rules_document['rules'],
                       'frame_source':frame['source'],'frame_sha256':frame['source_sha256']})
    return {'schema':'dd2.cp1.layout-plan.v1','units':'um','name':spec['name'],
            'grid_um':r['grid'],'input':spec,'dimensions':dimensions,
            'geometry':plan,'labels':labels,'status':'prepared_unverified'}


def save_plan(document,outdir):
    destination=Path(outdir)/document['name']
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'layout_plan.json').write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8')
    (destination/'dimensions.json').write_text(json.dumps(document['dimensions'],indent=2)+'\n',encoding='utf-8')
    write_schematic(document['input'],destination/'schematic.spice')
    return destination/'layout_plan.json'


def main():
    base=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cell_spec',type=Path,help='Supplied connections, Euler paths and per-finger sizes')
    parser.add_argument('--request',type=Path,help='Optional JSON output of the existing request_reader.py')
    parser.add_argument('--frame',type=Path,required=True,help='Actual instructor given/plan_hd.json')
    parser.add_argument('--rules',type=Path,default=base/'rules.json')
    parser.add_argument('--outdir',type=Path,default=base.parent/'cells')
    parser.add_argument('--policy',action='append',default=[],metavar='NAME=JSON',
                        help='Override one rules.json policy for this run, e.g. folded_finger_nets_on_met1=true')
    args=parser.parse_args()
    spec=read_json(args.cell_spec)
    if args.request:spec=bind_normalized_request(spec,read_json(args.request))
    rules=read_json(args.rules)
    for item in args.policy:
        name,_,value=item.partition('=')
        if name not in rules['policies']:
            parser.error(f'unknown policy {name!r}')
        rules['policies'][name]=json.loads(value)
    document=prepare_layout(spec,rules,load_plan_hd(args.frame))
    path=save_plan(document,args.outdir)
    print(f'Prepared {path}; no GDSfactory component or GDS was created.')


if __name__=='__main__':
    main()
