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
    for key in ('boundary','areaid_sc','nwell','nsdm','psdm','hvtp'):
        item=f[key]
        yield key,tuple(item['gds']),[
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
            for i, identifier in enumerate(seg_ids):
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
            ids += seg_ids
        if len(set(ids)) != len(ids) or set(ids) != {d['id'] for d in devices if d['type'] == kind}:
            raise ValueError(f'{kind}: the Euler path must traverse every device of this row exactly once')
        # Uniform rows keep every diffusion strip a simple rectangle.
        # A stepped-diffusion algorithm must be added before relaxing this.
        if max(widths) - min(widths) > 1e-9:
            raise ValueError(f'{kind}: unequal finger widths need stepped diffusion, not implemented yet')
        gate_orders.append([by_id[x]['gate'] for x in ids])
    if gate_orders[0] != gate_orders[1]:
        raise ValueError('Supplied Euler paths must have identical gate order for common poly columns')
    if not set(spec['inputs']) <= set(gate_orders[0]):
        raise ValueError('Every declared input must drive at least one gate')
    return by_id


def derive_dimensions(spec, frame, r, by_id, rows, channels=()):
    g = r['grid']
    snap = lambda value: round(math.ceil(value / g - 1e-9) * g, 9)
    # Even grid counts keep centered square edges on the same grid.
    centered_size = lambda value: round(math.ceil(value / (2 * g) - 1e-9) * 2 * g, 9)
    l = max(d['l_um'] for d in spec['devices'])
    li_pad = centered_size(max(r['licon_size'] + 2 * r['li_contact_enclosure'], math.sqrt(r['li_min_area'])))
    poly_pad = centered_size(r['licon_size'] + 2 * r['licon_poly_enclosure'])
    npc_pad = centered_size(r['licon_size'] + 2 * r['licon_npc_enclosure'])
    m1_pad = centered_size(max(r['mcon_size'] + 2 * r['met1_contact_enclosure'], math.sqrt(r['met1_min_area'])))
    pitch_bounds = {
        'poly_stripe_spacing': l + r['poly_space'],
        'diffusion_contact_between_gates': l + r['licon_size'] + 2 * r['licon_gate_space'],
        'poly_contact_landing_spacing': poly_pad + r['poly_space'],
        'li1_pin_spacing': li_pad + r['li_space'],
        'separate_npc_openings': npc_pad + r['npc_space'],
    }
    pitch = snap(max(pitch_bounds.values()))
    contact_offset = r['licon_diff_enclosure'] + r['licon_size'] / 2
    end_bounds = {
        'diffusion_extension': r['diff_end_extension'],
        'contact_fit': r['licon_gate_space'] + r['licon_size'] + r['licon_diff_enclosure'],
        # If there is only one gate, its two end-region landings must still
        # have legal inter-net li1 spacing. Contact enclosure alone misses this.
        'end_contact_li1_spacing': contact_offset + (li_pad + r['li_space'] - l) / 2,
    }
    end = snap(max(end_bounds.values()))
    # A diffusion break between two segments: the strips, the end landings on either
    # side, and the gates on either side must all stay legal.
    gap_bounds = {
        'diffusion_spacing': r['diff_space'],
        'end_landing_li1_spacing': li_pad + r['li_space'] - 2 * contact_offset,
        'gate_pitch_across_break': pitch - 2 * end - l,
    }
    gap = snap(max(gap_bounds.values()))
    margin = snap(r['implant_enclosure'] + r['li_width'] + r['li_space'])
    # A routing channel: one gap between two gates of a segment widened so an li1
    # wire passes between their gate landings, across the npc band.
    channel_pitch = snap(max(pitch, li_pad + 2 * r['li_space'] + r['li_width']))
    x, active, gates, contacts_x = margin, [], [], []
    for s, segment in enumerate(rows['nmos']):
        n = len(segment['devices'])
        steps = [channel_pitch if (s, i) in channels else pitch for i in range(1, n)]
        # A channel at a segment end (node 0 or n) lengthens that end instead.
        left = end + (channel_pitch - pitch if (s, 0) in channels else 0)
        right = end + (channel_pitch - pitch if (s, n) in channels else 0)
        length = snap(left + right + l + sum(steps))
        seg_gates = [round(x + left + l / 2 + sum(steps[:i]), 9) for i in range(n)]
        seg_contacts = [round(x + contact_offset, 9)]
        seg_contacts += [round((seg_gates[i] + seg_gates[i + 1]) / 2, 9) for i in range(n - 1)]
        seg_contacts += [round(x + length - contact_offset, 9)]
        active.append([round(x, 9), round(x + length, 9)])
        gates += seg_gates
        contacts_x.append(seg_contacts)
        x = round(x + length + gap, 9)
    raw_width = active[-1][1] + margin
    sites = math.ceil(raw_width / frame['site_width'] - 1e-9)
    return {
        'gate_length_um': l, 'gate_pitch_um': pitch,
        'pitch_lower_bounds_um': pitch_bounds,
        'dominant_pitch_constraint': max(pitch_bounds, key=pitch_bounds.get),
        'li1_landing_um': li_pad, 'poly_landing_um': poly_pad,
        'npc_landing_um': npc_pad, 'met1_landing_um': m1_pad,
        'diffusion_end_extension_um': end, 'side_margin_um': margin,
        'end_extension_lower_bounds_um': end_bounds,
        'diffusion_break_um': gap, 'diffusion_break_lower_bounds_um': gap_bounds,
        'channel_pitch_um': channel_pitch, 'channels': sorted(channels),
        'diffusion_segments': len(active),
        'active_x_um': active,
        'gate_x_um': gates, 'diffusion_node_x_um': contacts_x,
        'raw_width_um': round(raw_width, 9), 'minimum_width_sites': sites,
        'formulas': {
            'pitch': 'snap_up(max(poly stripe, contacted diffusion gap, poly landing, li1 pin, NPC bounds))',
            'end_extension': 'max(poly.7, contact fit, contact_offset + (li1_landing + li1_space - L)/2)',
            'diffusion_break': 'snap_up(max(diff/tap.3, end-landing li1 spacing, gate pitch across the break))',
            'channel_pitch': 'snap_up(max(pitch, li1 landing + 2*li1 spacing + li1 width)), only at listed channels',
            'side_margin': 'implant enclosure + li1 wire width + li1 spacing; conservative routing policy',
            'raw_width': '2*side_margin + sum over segments(2*end_extension + L + (gates-1)*pitch) + (segments-1)*break',
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

    counts = Counter(node for segments in rows.values() for seg in segments for node in seg['nodes'])
    # Stage outputs drive gates elsewhere in the cell, so they need a contact too.
    exposed = {*spec['inputs'], spec['output'], 'VPWR', 'VGND'} | {d['gate'] for d in spec['devices']}
    for kind in ('nmos', 'pmos'):
        w = by_id[rows[kind][0]['devices'][0]]['w_um']
        band = frame[f'{kind}_band']
        bottom, top = (band[0], band[0]+w) if kind == 'nmos' else (band[1]-w, band[1])
        column = 0
        for s, segment in enumerate(rows[kind]):
            active_left, active_right = dim['active_x_um'][s]
            rect('diff', (active_left, bottom, active_right, top))
            for i, identifier in enumerate(segment['devices']):
                d, x = by_id[identifier], dim['gate_x_um'][column + i]
                devices.append({**d, 'gate_x_um': x, 'diffusion_y_um': [bottom, top], 'segment': s,
                                'left_net': segment['nodes'][i], 'right_net': segment['nodes'][i+1],
                                # BSIM4 SA/SB: gate edge to the end of its own diffusion strip.
                                'diffusion_edge_distances_um': {
                                    'left': round(x-d['l_um']/2-active_left, 9),
                                    'right': round(active_right-x-d['l_um']/2, 9),
                                }})
            column += len(segment['devices'])
            for x, net in zip(dim['diffusion_node_x_um'][s], segment['nodes']):
                # A unique internal shared-diffusion node needs no contact or wire.
                if counts[net] == 1 and net not in exposed:
                    continue
                inset = r['licon_diff_enclosure'] + r['licon_size']/2
                if kind == 'nmos':
                    y = max(bottom+inset,frame['_contract']['rails']['VGND']['li1']['y'][1]+r['li_space']+dim['li1_landing_um']/2) if net == 'VGND' else min(top-inset, gate_y-dim['li1_landing_um']-r['li_space'])
                else:
                    y = min(top-inset,frame['_contract']['rails']['VPWR']['li1']['y'][0]-r['li_space']-dim['li1_landing_um']/2) if net == 'VPWR' else max(bottom+inset, gate_y+dim['li1_landing_um']+r['li_space'])
                if y < bottom+inset-1e-9 or y > top-inset+1e-9:
                    raise ValueError(f'{kind}: contact landing cannot fit both diffusion enclosure and gate-pin clearance')
                square('licon', x, y, r['licon_size'], net)
                square('li1', x, y, dim['li1_landing_um'], net)
                terminals[net].append((x, round(y, 9)))
                contacts.append({'type': 'diffusion', 'net': net, 'point_um': [x, round(y, 9)], 'row': kind})

    n_bottom = next(d['diffusion_y_um'][0] for d in devices if d['type']=='nmos')
    p_top = next(d['diffusion_y_um'][1] for d in devices if d['type']=='pmos')
    nmos_columns = [identifier for seg in rows['nmos'] for identifier in seg['devices']]
    for x, identifier in zip(dim['gate_x_um'], nmos_columns):
        d, net = by_id[identifier], by_id[identifier]['gate']
        rect('poly', (x-d['l_um']/2, n_bottom-r['poly_endcap'], x+d['l_um']/2, p_top+r['poly_endcap']), net)
        square('poly', x, gate_y, dim['poly_landing_um'], net)
        square('licon', x, gate_y, r['licon_size'], net)
        square('npc', x, gate_y, dim['npc_landing_um'])
        square('li1', x, gate_y, dim['li1_landing_um'], net)
        # Only primary inputs are pins; an internal stage output is wired, not exposed.
        if net in spec['inputs']:
            square('li1_pin', x, gate_y, r['licon_size'], net)
        terminals[net].append((x, gate_y))
        contacts.append({'type':'poly','net':net,'point_um':[x,gate_y]})

    # The output pin sits on one of the output's own drain landings: HD only asks for
    # a li1 pin on the wire joining the drains (handout Table 2), and a separate pin at
    # the cell edge would need its own route.
    output_point = terminals[spec['output']][0]
    square('li1_pin', *output_point, r['licon_size'], spec['output'])
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


def channel_candidates(spec, rows):
    """Gaps worth widening into channels: where a net that needs both rows and drives no
    gate (so it has no landing of its own in the npc band, like the cell output) has a
    contact between two gates."""
    gate_nets = {d['gate'] for d in spec['devices']}
    kinds, gaps, every = defaultdict(set), defaultdict(set), set()
    for kind, segments in rows.items():
        for s, segment in enumerate(segments):
            for k, net in enumerate(segment['nodes']):
                kinds[net].add(kind)
                gaps[net].add((s, k))          # node k sits between gates k-1 and k
                every.add((s, k))
    nets = [n for n in sorted(kinds) if len(kinds[n]) == 2 and n not in gate_nets
            and n not in ('VPWR', 'VGND')]
    near = sorted({gap for n in nets for gap in gaps[n]})
    # The gaps at the crossing nets' own contacts first, then every other gap.
    return near + sorted(every - set(near)) if nets else []


def route_li1(plan, spec, frame, r, policies, allow_met1=False):
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
    occupied = {layer: [(s['net'], s['box']) for s in plan['shapes'] if s['layer'] == layer]
                for layer in ('li1', 'met1')}
    mcons = [s['box'] for s in plan['shapes'] if s['layer'] == 'mcon']
    rail_y = {'VPWR': frame['power_y'], 'VGND': frame['ground_y']}

    def index(point):
        return tuple(round(v/step) for v in point)

    def raster(boxes, margin):
        """Grid points strictly closer than `margin` to any of the boxes."""
        points = set()
        for b in boxes:
            x0 = math.floor((b[0] - margin) / step + 1e-9) + 1
            x1 = math.ceil((b[2] + margin) / step - 1e-9) - 1
            y0 = math.floor((b[1] - margin) / step + 1e-9) + 1
            y1 = math.ceil((b[3] + margin) / step - 1e-9) - 1
            for ix in range(x0, x1 + 1):
                for iy in range(y0, y1 + 1):
                    points.add((ix, iy))
        return points

    def search(start, goal, net, allow_met1):
        others = {layer: [b for owner, b in occupied[layer] if owner != net] for layer in occupied}
        blocked = {'li1': raster(others['li1'], space['li1'] + wire['li1'] / 2),
                   'met1': raster(others['met1'], space['met1'] + wire['met1'] / 2)}
        pad_blocked = raster(others['met1'], space['met1'] + met1_pad / 2)
        via_blocked = raster(mcons, r['mcon_space'] + r['mcon_size'] / 2)
        edge = r['li_space'] if net not in rail_y else 0
        layers = ('li1', 'met1') if allow_met1 and net not in rail_y else ('li1',)

        def inside(ix, iy, layer):
            x = ix * step
            return (edge + wire[layer]/2 - 1e-9 <= x <= width_um - edge - wire[layer]/2 + 1e-9
                    and 0 <= iy <= ny)

        def allowed(ix, iy, layer):
            return inside(ix, iy, layer) and (ix, iy) not in blocked[layer]

        def via_ok(ix, iy):
            return (allowed(ix, iy, 'li1') and inside(ix, iy, 'met1')
                    and (ix, iy) not in pad_blocked and (ix, iy) not in via_blocked)

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
                if cost + c < best.get(nxt, math.inf):
                    best[nxt] = cost + c; parent[nxt] = p
                    heapq.heappush(queue, (cost + c + heuristic(nxt), cost + c, next(serial), nxt))
        raise ValueError(f"No {'/'.join(layers)} route found for {net} at this width")

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

    def lay(net, connected, path):
        # Split the path into runs on one layer; a layer change is an mcon via.
        runs, current = [], [path[0]]
        for p in path[1:]:
            if p[2] != current[-1][2]:
                runs.append(current); current = [p]
                x, y = p[0]*step, p[1]*step
                for layer, size in (('mcon', r['mcon_size']), ('li1', r['mcon_size']), ('met1', met1_pad)):
                    box = box_at((x, y), (x, y), size)
                    plan['shapes'].append({'layer': layer, 'box': box, 'net': net})
                    if layer == 'mcon':
                        mcons.append(box)
                    else:
                        occupied[layer].append((net, box))
                plan.setdefault('vias', []).append({'net': net, 'point_um': [round(x, 9), round(y, 9)]})
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
                plan['shapes'].append({'layer': layer, 'box': box, 'net': net})
                occupied[layer].append((net, box))
            if len(physical) > 1:
                plan['routes'].append({'net': net, 'layer': layer, 'points_um': physical})
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
                    path = search(origin, target, net, True)
                lay(net, connected, path)

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
        raise best[1]
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
    # Try, in order, for each placement euler.py ranked: li1 only with no channel, with
    # each candidate channel, with all of them; then, in a multi-stage cell, li1 + met1;
    # then a wider cell. A single-stage cell gets met1 only in a last pass after every
    # placement failed on li1: handout Table 3 keeps met1 for the rails there, so the
    # plan records 'met1_in_single_stage_cell' and the HD comparison flags it.
    failures=[]
    extra_sites=int(policies['maximum_extra_sites'])
    placements=[spec['euler_paths'],*spec.get('alternative_euler_paths',[])]
    def attempts():
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
            yield index,euler_paths,rows,every,multi_stage,min(2,extra_sites)
        if not multi_stage:
            for index,euler_paths in enumerate(placements):
                rows=path_segments(euler_paths)
                every=tuple(channel_candidates(spec,rows))
                for channels,extra in dict.fromkeys([((),0),(every,0),(every,min(2,extra_sites))]):
                    yield index,euler_paths,rows,channels,True,extra
    for index,euler_paths,rows,channels,allow_met1,extra in attempts():
        by_id=validate(spec,frame,r,rows)
        dimensions=derive_dimensions(spec,frame,r,by_id,rows,channels)
        plan=plan_geometry(spec,frame,r,dimensions,by_id,dimensions['minimum_width_sites']+extra,rows)
        try:
            route_li1(plan,spec,frame,r,policies,allow_met1)
            fill_li1_notches(plan,r)
            break
        except ValueError as error:
            failures.append({'placement':index,'channels':list(channels),'met1':allow_met1,
                             'extra_sites':extra,'reason':str(error)})
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
