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
    left_ext = lambda c: min(c - pad_w / 2, -l / 2)    # pad + stripe hull, from the gate
    right_ext = lambda c: max(c + pad_w / 2, l / 2)

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
        bounds['poly_pad_spacing'] = right_ext(a) - left_ext(b) + r['poly_space']
        bounds['gate_li1_spacing'] = (a + li_w / 2) - (b - li_w / 2) + r['li_space']
        if j in crossing:
            bounds['li1_track_through_band'] = (a + li_w / 2) - (b - li_w / 2) + track
        return bounds

    def left_bounds(c):
        b = {'diffusion_margin': margin + end + l / 2,
             'poly_pad_to_edge': r['poly_space'] / 2 - left_ext(c),
             'gate_li1_to_edge': r['li_space'] / 2 - (c - li_w / 2)}
        if 0 in crossing:
            b['li1_track_through_band'] = r['li_space'] / 2 + track - r['li_space'] - (c - li_w / 2)
        return b

    def right_bounds(c):
        b = {'diffusion_margin': l / 2 + end + margin,
             'poly_pad_to_edge': right_ext(c) + r['poly_space'] / 2,
             'gate_li1_to_edge': c + li_w / 2 + r['li_space'] / 2}
        if len(columns) in crossing:
            b['li1_track_through_band'] = c + li_w / 2 + track - r['li_space'] + r['li_space'] / 2
        return b

    # Distances are rounded up to two routing steps, so contacts midway between two
    # gates stay on the routing grid; the cost adds a small preference for contacts
    # close to their own stripe among equally narrow solutions.
    best = {c: ((snap(max(left_bounds(c).values()), step), abs(c)), None) for c in offsets}
    history = [best]
    for j in range(1, len(columns)):
        current = {}
        for b in offsets:
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


def plan_geometry(spec, frame, r, dim, by_id, sites, rows):
    """Build a pure-data plan before constructing the GDSfactory component."""
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
                elif (kind, s, k) in far:
                    # Rail side, li.5 met sideways: the net's own wire runs along the
                    # rail side, past the band-side landings of the nets between.
                    y = top - inset
                    rect('li1', (x - wide / 2, y - r['licon_size'] / 2, x + wide / 2, y + r['licon_size'] / 2), net)
                    terminals[net].append((x, round(y, 9)))
                else:
                    # As close to the band as the gate landings' li1 spacing allows.
                    y = (min(top - inset, gate_li1[0] - r['li_space'] - li_h / 2) if kind == 'nmos'
                         else max(bottom + inset, gate_li1[1] + r['li_space'] + li_h / 2))
                    rect('li1', (x - li_w / 2, y - li_h / 2, x + li_w / 2, y + li_h / 2), net)
                    terminals[net].append((x, round(y, 9)))
                if y < bottom + inset - 1e-9 or y > top - inset + 1e-9:
                    raise ValueError(f'{kind}: contact landing cannot fit both diffusion enclosure and gate-pin clearance')
                square('licon', x, y, r['licon_size'], net)
                contacts.append({'type': 'diffusion', 'net': net, 'point_um': [x, round(y, 9)], 'row': kind})

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
    for x, c, (d, has_n, has_p) in zip(dim['gate_x_um'], dim['gate_contact_offset_um'], column_devices(rows, by_id)):
        net = d['gate']
        half = d['l_um']/2
        # A column with a transistor in one row only (folding) stops at its contact pad
        # on the other side, so it makes no transistor in that row's diffusion.
        bottom = n_bottom-r['poly_endcap'] if has_n else gate_y-pad_h/2
        top = p_top+r['poly_endcap'] if has_p else gate_y+pad_h/2
        rect('poly', (x-half, bottom, x+half, top), net)
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
    output_point = terminals[spec['output']][0]
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
    Clearances come from rules.json. Not a substitute for DRC, extraction or LVS.
    """
    step = float(policies['routing_grid'])
    g = r['grid']
    centered = lambda v: round(math.ceil(v / (2 * g) - 1e-9) * 2 * g, 9)
    wire = {'li1': r['li_width'], 'met1': r['met1_width']}
    space = {'li1': r['li_space'], 'met1': r['met1_space']}
    met1_pad = centered(max(r['mcon_size'] + 2 * r['met1_contact_enclosure'], math.sqrt(r['met1_min_area'])))
    step_cost = {'li1': 1, 'met1': float(policies.get('met1_step_cost', 4))}
    via_cost = float(policies.get('via_cost', 40))
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

    def search(start, goal, net, allow_met1, hard=None, soft=None, penalty=0, no_via=frozenset()):
        """A* from start to goal. hard: {layer: [(net, box)], 'mcon': [box]} replaces the
        shapes routed so far (rip-up mode); soft: {net: footprint} of other nets' routed
        wires, which the path may cross at `penalty` per grid step. Returns the path, or
        (path, nets crossed) when soft is given."""
        source = hard if hard is not None else occupied
        others = {layer: [b for owner, b in source[layer] if owner != net] for layer in ('li1', 'met1')}
        blocked = {'li1': raster(others['li1'], space['li1'] + wire['li1'] / 2),
                   'met1': raster(others['met1'], space['met1'] + wire['met1'] / 2)}
        layers = ('li1', 'met1') if allow_met1 and net not in rail_y else ('li1',)
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
        queue, serial, best, parent = [], count(), {start3: 0}, {}
        heuristic = lambda p: abs(p[0]-goal[0]) + abs(p[1]-goal[1])
        heapq.heappush(queue, (heuristic(start3), 0, next(serial), start3))
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
            moves = [((ix+dx, iy+dy, layer), step_cost[layer]) for dx, dy in ((1,0),(-1,0),(0,1),(0,-1))
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

    def route_all(order):
        for net in order:
            for connected, target in connections(net):
                origin = min(connected, key=lambda p: abs(p[0]-target[0])+abs(p[1]-target[1]))
                # met1 only where li1 alone cannot connect, and only if allowed.
                try:
                    path = search(origin, target, net, False)
                except ValueError:
                    if not allow_met1:
                        raise
                    path = spaced_search(origin, target, net, True)
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


def fill_notches(plan, r, layer, space, skip=()):
    """Prepare legal same-net wire shapes on one layer before the drawing stage sees them."""
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
    fill_notches(plan, r, 'li1', r['li_space'])
    # The met1 rails are the frame's; only routed signal met1 is merged.
    fill_notches(plan, r, 'met1', r['met1_space'], skip=('VPWR', 'VGND'))


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
    def tiers():
        normal=list(attempts(False))
        groups=[[a for a in normal if not a[4]],[a for a in normal if a[4]],list(attempts(True))]
        for group in groups:
            # Narrowest first; equal widths keep the order above.
            ordered=sorted(group,key=width_of)
            for rip_up in (False,True):
                for attempt in ordered:
                    yield (*attempt,rip_up)
    for index,euler_paths,rows,channels,allow_met1,extra,rip_up in tiers():
        by_id=validate(spec,frame,r,rows)
        dimensions=derive_dimensions(spec,frame,r,by_id,rows,channels,policies,extra)
        plan=plan_geometry(spec,frame,r,dimensions,by_id,dimensions['width_sites'],rows)
        try:
            route_li1(plan,spec,frame,r,policies,allow_met1,rip_up)
            fill_li1_notches(plan,r)
            break
        except ValueError as error:
            failures.append({'placement':index,'channels':list(channels),'met1':allow_met1,
                             'extra_sites':extra,'rip_up':rip_up,'reason':str(error)})
    else:raise ValueError('Routing failed for every placement, channel set and allowed width: '+json.dumps(failures))
    plan['met1_in_single_stage_cell']=bool(plan.get('vias')) and not multi_stage
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
    args=parser.parse_args()
    spec=read_json(args.cell_spec)
    if args.request:spec=bind_normalized_request(spec,read_json(args.request))
    document=prepare_layout(spec,read_json(args.rules),load_plan_hd(args.frame))
    path=save_plan(document,args.outdir)
    print(f'Prepared {path}; no GDSfactory component or GDS was created.')


if __name__=='__main__':
    main()
