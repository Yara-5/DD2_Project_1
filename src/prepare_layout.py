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


def validate(spec, frame, r):
    """Validate the supplied physical-stage inputs; do not discover an Euler path."""
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
    by_id = {d['id']: d for d in devices}
    if len(by_id) != len(devices):
        raise ValueError('Device IDs must be unique')
    gate_orders = []
    models = frame['_contract']['devices']
    for kind, model, bulk in (
        ('nmos', models['nmos'], 'VNB'),
        ('pmos', models['pmos'], 'VPB'),
    ):
        path = spec['euler_paths'][kind]
        ids, nodes = path['devices'], path['nodes']
        if not ids or len(nodes) != len(ids) + 1 or len(set(ids)) != len(ids):
            raise ValueError(f'{kind}: Euler path needs n devices and n+1 nodes, no repeated device ID')
        if set(ids) != {d['id'] for d in devices if d['type'] == kind}:
            raise ValueError(f'{kind}: Euler path must traverse every device of this row exactly once')
        widths = []
        for i, identifier in enumerate(ids):
            d = by_id[identifier]
            if d['model'] != model or d['bulk'] != bulk:
                raise ValueError(f'{identifier}: unsupported model/bulk for this HD frame')
            if {d['source'], d['drain']} != {nodes[i], nodes[i + 1]} or nodes[i] == nodes[i + 1]:
                raise ValueError(f'{identifier}: Euler node pair disagrees with supplied source/drain')
            if d['gate'] not in spec['inputs']:
                raise ValueError('This milestone generator accepts one stage controlled by primary inputs')
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
        # Uniform rows cover INV/NAND2 and preserve a simple continuous strip.
        # A stepped-diffusion algorithm must be added before relaxing this.
        if max(widths) - min(widths) > 1e-9:
            raise ValueError(f'{kind}: unequal finger widths need stepped diffusion, not implemented in this milestone')
        gate_orders.append([by_id[x]['gate'] for x in ids])
    if gate_orders[0] != gate_orders[1]:
        raise ValueError('Supplied Euler paths must have identical gate order for common poly columns')
    if any(d['type'] not in ('nmos', 'pmos') for d in devices):
        raise ValueError('Unknown transistor type')
    if set(gate_orders[0]) != set(spec['inputs']):
        raise ValueError('All declared inputs must appear in the supplied gate order')
    return by_id


def derive_dimensions(spec, frame, r, by_id):
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
    margin = snap(r['implant_enclosure'] + r['li_width'] + r['li_space'])
    n = len(spec['euler_paths']['nmos']['devices'])
    active_length = snap(2 * end + l + (n - 1) * pitch)
    raw_width = active_length + 2 * margin
    sites = math.ceil(raw_width / frame['site_width'] - 1e-9)
    gates = [round(margin + end + l / 2 + i * pitch, 9) for i in range(n)]
    contacts_x = [round(margin + contact_offset, 9)]
    contacts_x += [round((gates[i] + gates[i + 1]) / 2, 9) for i in range(n - 1)]
    contacts_x += [round(margin + active_length - contact_offset, 9)]
    return {
        'gate_length_um': l, 'gate_pitch_um': pitch,
        'pitch_lower_bounds_um': pitch_bounds,
        'dominant_pitch_constraint': max(pitch_bounds, key=pitch_bounds.get),
        'li1_landing_um': li_pad, 'poly_landing_um': poly_pad,
        'npc_landing_um': npc_pad, 'met1_landing_um': m1_pad,
        'diffusion_end_extension_um': end, 'side_margin_um': margin,
        'end_extension_lower_bounds_um': end_bounds,
        'active_x_um': [margin, round(margin + active_length, 9)],
        'gate_x_um': gates, 'diffusion_node_x_um': contacts_x,
        'raw_width_um': round(raw_width, 9), 'minimum_width_sites': sites,
        'formulas': {
            'pitch': 'snap_up(max(poly stripe, contacted diffusion gap, poly landing, li1 pin, NPC bounds))',
            'end_extension': 'max(poly.7, contact fit, contact_offset + (li1_landing + li1_space - L)/2)',
            'side_margin': 'implant enclosure + li1 wire width + li1 spacing; conservative routing policy',
            'raw_width': '2*side_margin + 2*end_extension + L + (gate_count-1)*pitch',
            'width_sites': 'ceil(raw_width / site_width), plus explicit routing retries if required',
        },
    }


def plan_geometry(spec, frame, r, dim, by_id, sites):
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

    counts = Counter(node for path in spec['euler_paths'].values() for node in path['nodes'])
    exposed = {*spec['inputs'], spec['output'], 'VPWR', 'VGND'}
    active_left, active_right = dim['active_x_um']
    for kind in ('nmos', 'pmos'):
        path = spec['euler_paths'][kind]
        ordered = [by_id[x] for x in path['devices']]
        w = ordered[0]['w_um']
        band = frame[f'{kind}_band']
        bottom, top = (band[0], band[0]+w) if kind == 'nmos' else (band[1]-w, band[1])
        rect('diff', (active_left, bottom, active_right, top))
        for i, d in enumerate(ordered):
            x = dim['gate_x_um'][i]
            devices.append({**d, 'gate_x_um': x, 'diffusion_y_um': [bottom, top],
                            'left_net': path['nodes'][i], 'right_net': path['nodes'][i+1],
                            'diffusion_edge_distances_um': {
                                'left': round(x-d['l_um']/2-active_left, 9),
                                'right': round(active_right-x-d['l_um']/2, 9),
                            }})
        for x, net in zip(dim['diffusion_node_x_um'], path['nodes']):
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
    for x, identifier in zip(dim['gate_x_um'], spec['euler_paths']['nmos']['devices']):
        d, net = by_id[identifier], by_id[identifier]['gate']
        rect('poly', (x-d['l_um']/2, n_bottom-r['poly_endcap'], x+d['l_um']/2, p_top+r['poly_endcap']), net)
        square('poly', x, gate_y, dim['poly_landing_um'], net)
        square('licon', x, gate_y, r['licon_size'], net)
        square('npc', x, gate_y, dim['npc_landing_um'])
        square('li1', x, gate_y, dim['li1_landing_um'], net)
        square('li1_pin', x, gate_y, r['licon_size'], net)
        terminals[net].append((x, gate_y))
        contacts.append({'type':'poly','net':net,'point_um':[x,gate_y]})

    output_point = (round(width-dim['li1_landing_um']/2-r['li_space'], 9), gate_y)
    square('li1', *output_point, dim['li1_landing_um'], spec['output'])
    square('li1_pin', *output_point, r['licon_size'], spec['output'])
    terminals[spec['output']].append(output_point)
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


def route_li1(plan, spec, frame, r, policies):
    """Deterministic Manhattan A* with clearance from other li1 nets.

    This router considers li1 wire/landing obstacles. It is not a complete
    multi-layer router or a substitute for DRC, extraction or LVS.
    """
    step, wire = float(policies['routing_grid']), r['li_width']
    if step <= 0 or abs(step/(2*r['grid']) - round(step/(2*r['grid']))) > 1e-7:
        raise ValueError('Routing grid must be a positive multiple of twice the manufacturing grid')
    if abs(wire/(2*r['grid']) - round(wire/(2*r['grid']))) > 1e-7:
        raise ValueError('Centered wire width must be a multiple of twice the manufacturing grid')
    nx = math.floor((plan['width_um']-wire/2)/step)
    low_x = math.ceil(wire/2/step)
    ny = round(frame['height']/step)
    occupied = [(s['net'],s['box']) for s in plan['shapes'] if s['layer']=='li1']
    clearance = r['li_space'] + wire/2

    def index(point):
        return tuple(round(v/step) for v in point)

    def search(start, goal, net):
        obstacles = [b for owner,b in occupied if owner != net]
        cache = {}
        def allowed(p):
            if p not in cache:
                ix,iy=p
                x,y=ix*step,iy*step
                edge=r['li_space'] if net not in ('VPWR','VGND') else 0
                cache[p] = (low_x <= ix <= nx and edge+wire/2-1e-9 <= x <= plan['width_um']-edge-wire/2+1e-9
                    and 0 <= iy <= ny and not any(
                    b[0]-clearance+1e-9 < x < b[2]+clearance-1e-9 and
                    b[1]-clearance+1e-9 < y < b[3]+clearance-1e-9 for b in obstacles))
            return cache[p]
        if not allowed(start) or not allowed(goal):
            raise ValueError(f'LI1 terminal clearance prevents routing net {net}')
        queue, serial, best, parent = [], count(), {start:0}, {}
        heuristic=lambda p:abs(p[0]-goal[0])+abs(p[1]-goal[1])
        heapq.heappush(queue,(heuristic(start),0,next(serial),start))
        while queue:
            _,cost,_,p=heapq.heappop(queue)
            if cost!=best.get(p):
                continue
            if p==goal:
                result=[p]
                while p in parent:
                    p=parent[p]; result.append(p)
                return list(reversed(result))
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                nxt=(p[0]+dx,p[1]+dy)
                if allowed(nxt) and cost+1 < best.get(nxt,math.inf):
                    best[nxt]=cost+1; parent[nxt]=p
                    heapq.heappush(queue,(cost+1+heuristic(nxt),cost+1,next(serial),nxt))
        raise ValueError(f'No li1 route found for {net} at this width')

    order = ['VPWR','VGND'] + sorted(net for net in plan['terminals'] if net not in ('VPWR','VGND'))
    for net in order:
        points=list(dict.fromkeys(index(p) for p in plan['terminals'][net]))
        connected=points[:1]
        for target in points[1:]:
            origin=min(connected,key=lambda p:abs(p[0]-target[0])+abs(p[1]-target[1]))
            path=search(origin,target,net)
            # Collapse straight runs without changing the routed geometry.
            turns=[path[0]]
            for i in range(1,len(path)-1):
                a,b,c=path[i-1],path[i],path[i+1]
                if (b[0]-a[0],b[1]-a[1]) != (c[0]-b[0],c[1]-b[1]):
                    turns.append(b)
            if len(path)>1:
                turns.append(path[-1])
            physical=[(round(x*step,9),round(y*step,9)) for x,y in turns]
            for a,b in zip(physical,physical[1:]):
                box=[min(a[0],b[0])-wire/2,min(a[1],b[1])-wire/2,
                     max(a[0],b[0])+wire/2,max(a[1],b[1])+wire/2]
                box=[round(v,9) for v in box]
                plan['shapes'].append({'layer':'li1','box':box,'net':net})
                occupied.append((net,box))
            plan['routes'].append({'net':net,'points_um':physical})
            connected.extend(path)


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


def fill_li1_notches(plan,r):
    """Prepare legal same-net wire shapes before the drawing stage sees them."""
    grid=r['grid']
    regions={}
    for shape in plan['shapes']:
        if shape['layer']=='li1':
            regions.setdefault(shape['net'],kdb.Region()).insert(
                kdb.Box(*[round(x/grid) for x in shape['box']]))
    growth=math.ceil((r['li_space']/2+grid)/grid-1e-9)
    minimum=round(r['li_space']/grid)
    merged={net:(region.merged().sized(growth).sized(-growth)|region).merged()
            for net,region in regions.items()}
    names=sorted(merged)
    for i,a in enumerate(names):
        if not merged[a].space_check(minimum).is_empty():
            raise ValueError(f'Unresolved same-net li1 notch on {a}')
        for b in names[i+1:]:
            if not (merged[a]&merged[b]).is_empty() or not merged[a].separation_check(merged[b],minimum).is_empty():
                raise ValueError(f'li1 notch filling cannot preserve spacing between {a} and {b}')
    plan['shapes']=[shape for shape in plan['shapes'] if shape['layer']!='li1']
    added={}
    for net,region in merged.items():
        added[net]=round((region.area()-regions[net].merged().area())*grid*grid,9)
        for polygon in region.each():
            for tile in polygon.decompose_trapezoids():
                box=tile.bbox()
                if tile.area()!=box.area():raise ValueError('Non-Manhattan li1 notch fill')
                plan['shapes'].append({'layer':'li1','net':net,'box':[
                    round(x*grid,9) for x in (box.left,box.bottom,box.right,box.top)]})
    plan['li1_notch_fill_added_area_um2']=added


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
    by_id=validate(spec,frame,r)
    dimensions=derive_dimensions(spec,frame,r,by_id)
    failures=[]
    for extra in range(int(rules_document['policies']['maximum_extra_sites'])+1):
        plan=plan_geometry(spec,frame,r,dimensions,by_id,dimensions['minimum_width_sites']+extra)
        try:
            route_li1(plan,spec,frame,r,rules_document['policies'])
            fill_li1_notches(plan,r)
            break
        except ValueError as error:
            failures.append({'extra_sites':extra,'reason':str(error)})
    else:raise ValueError('Routing failed at every allowed width: '+json.dumps(failures))

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
