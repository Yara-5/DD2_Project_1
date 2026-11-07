"""Section 4.1, Table 4: compare each generated cell with its installed HD cell.

Reads both GDS files back, measures width in sites, area, gate pitch, W/L,
diffusion strips and breaks, gate-to-diffusion-end distances, contacts and pins,
XORs the frame layers of each against given/plan_hd.json, and draws the pair
side by side at the same scale. The HD cell to compare with is the stage spec's
'hd_reference'; cells without one are skipped.
Must match (pass/fail): the frame layers, met1 only on the rails, and the pins (Table 4
"Pins": the installed cell's pin names; inputs on li1 in the npc band, the output on li1,
VGND/VPWR on met1 in their rails, VPB at the top edge, VNB at the bottom).
Reported: width, area, gate order and pitch, diffusion breaks; parasitics (R and C on
each port net, from the generated cell's RC netlist (verify_lvs.py) and the installed
cell's, extracted with the same Magic flow by characterize.py); timing (both cells
characterized by characterize.py, compared at the nominal point and over the tables).
Parasitics or timing whose files do not exist yet are reported as not measured.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import klayout.db as db
import parasitics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.path import Path as PlotPath
from matplotlib.patches import Patch,PathPatch


LAYERS=[('nwell',(64,20),'#ebe0fa',1),('nsdm',(93,44),'#a9e6b3',.45),
        ('psdm',(94,20),'#ffd0b3',.45),('hvtp',(78,44),'#dfae86',.25),
        ('diff',(65,20),'#47a66c',1),('poly',(66,20),'#dd524d',1),
        ('npc',(95,20),'#eacccc',.65),('li1',(67,20),'#b78bea',.65),
        ('met1',(68,20),'#519ae5',.65),('licon',(66,44),'#25283a',1),
        ('mcon',(67,44),'#ffffff',1)]


def reg(l,c,pair):return db.Region(c.begin_shapes_rec(l.layer(*pair))).merged()
def box_um(b,u):return [round(v*u,6) for v in (b.left,b.bottom,b.right,b.top)]


def measure(l,c,frame):
    u=l.dbu;get=lambda p:reg(l,c,p)
    # Rows are told apart by the n-well edge; sites by the frame's site width.
    nwell_bottom=frame['nwell']['y'][0];site=frame['site']['width']
    boundary=get((236,0)).bbox();diff=get((65,20));poly=get((66,20));li=get((67,20))
    polys=list(poly.each());conductors=list(li.each())
    labels=[]
    for s in c.shapes(l.layer(67,5)).each():
        if s.is_text():labels.append(s.text)
    gates=[]
    for channel in (diff&poly).each():
        b=channel.bbox();names=set()
        p=next(p for p in polys if not (db.Region(p)&db.Region(channel)).is_empty())
        contacts=get((66,44))&db.Region(p)
        for conductor in conductors:
            if (contacts&db.Region(conductor)).is_empty():continue
            names.update(t.string for t in labels if conductor.inside(db.Point(t.x,t.y)))
        strip=next(p for p in diff.each() if not (db.Region(p)&db.Region(channel)).is_empty())
        end=strip.bbox()
        gates.append({'row':'nmos' if b.center().y*u<nwell_bottom else 'pmos',
                      'gate':sorted(names),'x_um':round(b.center().x*u,6),
                      'w_um':round(b.height()*u,6),'l_um':round(b.width()*u,6),
                      'left_diffusion_distance_um':round((b.left-end.left)*u,6),
                      'right_diffusion_distance_um':round((end.right-b.right)*u,6)})
    pins=[]
    for pair,label_pair in [((67,16),(67,5)),((68,16),(68,5)),((64,16),(64,5)),((122,16),(64,59))]:
        markers=get(pair)
        for s in c.shapes(l.layer(*label_pair)).each():
            if not s.is_text():continue
            t=s.text
            shapes=[p for p in markers.each() if p.inside(db.Point(t.x,t.y))]
            pins.append({'name':t.string,'layer':list(pair),'marker_boxes_um':[box_um(p.bbox(),u) for p in shapes]})
    x=[d['x_um'] for d in gates if d['row']=='nmos'];x.sort()
    return {'width_um':round(boundary.width()*u,6),'height_um':round(boundary.height()*u,6),
            'width_sites':round(boundary.width()*u/site),'area_um2':round(boundary.area()*u*u,6),
            'gate_pitch_um':[round(b-a,6) for a,b in zip(x,x[1:])],
            'diffusion_strips':[box_um(p.bbox(),u) for p in diff.each()],
            'diffusion_breaks':max(0,diff.count()-2),
            'devices':sorted(gates,key=lambda d:(d['row'],d['x_um'])),
            'pins':pins,'layer_areas_um2':{n:round(get(p).area()*u*u,6) for n,p,_,_ in LAYERS},
            'diffusion_licon_count':(get((66,44))&diff).count(),
            'poly_licon_count':(get((66,44))&poly).count(),
            'npc_connected_regions':get((95,20)).count(),
            'li1_connected_regions':li.count()}


def frame_check(l,c,contract):
    u=l.dbu;w=reg(l,c,(236,0)).bbox().width()*u
    def x(value):
        if isinstance(value,(float,int)):return value
        match=re.fullmatch(r'width(?:\s*([+-])\s*([0-9.]+))?',value)
        if not match:raise ValueError(f'Unsupported symbolic frame coordinate: {value}')
        offset=float(match[2]) if match[2] else 0
        return w-offset if match[1]=='-' else w+offset
    entries={k:contract[k] for k in ('boundary','areaid_sc','nwell','nsdm','psdm','hvtp')}
    checks={}
    for name,item in entries.items():
        box=db.DBox(x(item['x'][0]),item['y'][0],x(item['x'][1]),item['y'][1]).to_itype(u)
        error=reg(l,c,item['gds'])^db.Region(box)
        checks[name]={'passed':error.is_empty(),'xor_area_um2':round(error.area()*u*u,9)}
    expected=db.Region()
    for rail in contract['rails'].values():
        m=rail['met1'];expected.insert(db.DBox(0,m['y'][0],w,m['y'][1]).to_itype(u))
    error=reg(l,c,(68,20))^expected
    checks['met1_only_fixed_rails']={'passed':error.is_empty(),'xor_area_um2':round(error.area()*u*u,9)}
    return checks


def pin_check(m,contract,inputs,output):
    """Table 4 'Pins' on one measured cell: layer and band of every pin shape."""
    band=contract['npc_band']['y'];rails=contract['rails'];top=contract['site']['height']
    shapes={}
    for p in m['pins']:shapes.setdefault((p['name'],tuple(p['layer'])),[]).extend(p['marker_boxes_um'])
    def boxes(name,layer):return shapes.get((name,layer),[])
    inside=lambda b,y:b and all(y[0]-1e-9<=x[1] and x[3]<=y[1]+1e-9 for x in b)
    checks={}
    for name in inputs:
        checks[f'{name}_on_li1_in_npc_band']=inside(boxes(name,(67,16)),band)
    checks[f'{output}_on_li1']=bool(boxes(output,(67,16)))
    for net in ('VGND','VPWR'):
        checks[f'{net}_on_met1_rail']=inside(boxes(net,(68,16)),rails[net]['met1']['y'])
    checks['VPB_at_top_edge']=any(b[1]-1e-9<=top<=b[3]+1e-9 for b in boxes('VPB',(64,16)))
    checks['VNB_at_bottom_edge']=any(b[1]-1e-9<=0<=b[3]+1e-9 for b in boxes('VNB',(122,16)))
    return checks


def net_parasitics(generated,installed):
    """R and C on each port net of both RC netlists (parasitics.summarize)."""
    if not (generated.is_file() and installed.is_file()):
        return None
    a,b=parasitics.summarize(generated)['nets'],parasitics.summarize(installed)['nets']
    ports=[n for n in b if not n.startswith('internal:')]
    def internal(nets):
        inner=[v for k,v in nets.items() if k.startswith('internal:')]
        return {'nets':len(inner),'sum_resistance_ohm':round(sum(v['sum_resistance_ohm'] for v in inner),3),
                'incident_capacitance_ff':round(sum(v['incident_capacitance_ff'] for v in inner),6)}
    return {'definition':parasitics.summarize(generated)['definition'],
            'ports':{n:{'generated':a.get(n),'installed':b[n]} for n in ports},
            'internal':{'generated':internal(a),'installed':internal(b)},
            'generated_netlist':str(generated),'installed_netlist':str(installed)}


def interpolate(table,slews,loads,slew,load):
    """Bilinear interpolation in a Liberty table (as an STA tool reads it); None outside
    the table's index range."""
    if not (slews[0]<=slew<=slews[-1] and loads[0]<=load<=loads[-1]):
        return None
    def bracket(index,x):
        j=max(0,min(len(index)-2,next((k for k in range(len(index)-1) if x<=index[k+1]),len(index)-2)))
        return j,(x-index[j])/(index[j+1]-index[j])
    i,u=bracket(slews,slew);j,v=bracket(loads,load)
    return ((1-u)*(1-v)*table[i][j]+(1-u)*v*table[i][j+1]
            +u*(1-v)*table[i+1][j]+u*v*table[i+1][j+1])


def timing_comparison(generated,installed):
    """Both cells characterized by characterize.py: per arc and table, the generated over
    the installed value at the installed cell's nominal point (the HD point nearest
    100 ps / 10 fF), and the median relative difference over the installed cell's table
    points. Generated cells use one template's indices (HD nand2_1's), the installed cells
    their own, so the generated tables are interpolated bilinearly at the installed
    cell's points; points outside the generated index range are left out and counted."""
    if not (generated.is_file() and installed.is_file()):
        return None
    g,h=json.loads(generated.read_text()),json.loads(installed.read_text())
    if not (g.get('passed') and h.get('passed')):
        return None
    nominal=h['nominal_point'];slews,loads=h['setup']['slews_ns'],h['setup']['loads_pf']
    gs,gl=g['setup']['slews_ns'],g['setup']['loads_pf']
    si,li=slews.index(nominal['slew_ns']),loads.index(nominal['load_pf'])
    theirs={(t['pin'],t['sense']):t['tables'] for t in h['timing']}
    arcs,diffs,outside=[],[],0
    for t in g['timing']:
        ref=theirs.get((t['pin'],t['sense']))
        if ref is None:continue
        row={'pin':t['pin'],'sense':t['sense']}
        for key,table in t['tables'].items():
            pairs=[]
            for a,s in enumerate(slews):
                for b,c in enumerate(loads):
                    ours=interpolate(table,gs,gl,s,c)
                    if ours is None:
                        outside+=1
                    elif ref[key][a][b]>0:
                        pairs.append((ours,ref[key][a][b]))
            diffs+=[(a-b)/b for a,b in pairs]
            ours=interpolate(table,gs,gl,nominal['slew_ns'],nominal['load_pf'])
            row[key]={'generated_ns':None if ours is None else round(ours,6),'installed_ns':ref[key][si][li],
                      'difference_percent':None if ours is None else round(100*(ours/ref[key][si][li]-1),3),
                      'median_difference_percent':round(100*statistics.median((a-b)/b for a,b in pairs),3) if pairs else None}
        arcs.append(row)
    caps={p:{'generated_pf':g['capacitance_pf'][p]['capacitance'],'installed_pf':v['capacitance']}
          for p,v in h['capacitance_pf'].items() if p in g['capacitance_pf']}
    return {'nominal_point':nominal,'arcs':arcs,'input_capacitance':caps,
            'same_indices':gs==slews and gl==loads,'points_outside_generated_index':outside,
            'median_difference_percent':round(100*statistics.median(diffs),3) if diffs else None,
            'generated_report':str(generated),'installed_report':str(installed)}


def draw(ax,l,c,width,title):
    u=l.dbu
    for name,pair,color,alpha in LAYERS:
        for p in reg(l,c,pair).each():
            vertices,codes=[],[]
            rings=[list(p.each_point_hull())]+[list(p.each_point_hole(i)) for i in range(p.holes())]
            for i,ring in enumerate(rings):
                points=[(pt.x*u,pt.y*u) for pt in ring]
                area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1]))
                if (area>0)!=(i==0):points.reverse()
                vertices.extend(points+[points[0]])
                codes.extend([PlotPath.MOVETO]+[PlotPath.LINETO]*(len(points)-1)+[PlotPath.CLOSEPOLY])
            ax.add_patch(PathPatch(PlotPath(vertices,codes),facecolor=color,
                                  edgecolor='#555' if name=='mcon' else color,lw=.3,alpha=alpha))
    b=reg(l,c,(236,0)).bbox();x0,y0,x1,y1=box_um(b,u)
    ax.plot([x0,x1,x1,x0,x0],[y0,y0,y1,y1,y0],ls='--',color='#374151',lw=1)
    seen=set()
    for pair in ((67,5),(68,5)):
        for s in c.shapes(l.layer(*pair)).each():
            if not s.is_text():continue
            t=s.text;key=(t.string,t.x,t.y)
            if key in seen:continue
            seen.add(key)
            ax.text(t.x*u,t.y*u,t.string,fontsize=9,ha='center',va='center',weight='bold',
                    bbox={'facecolor':'white','alpha':.85,'edgecolor':'none','pad':1.5})
    ax.set(xlim=(-.25,width+.25),ylim=(-.30,3.02),aspect='equal',xlabel='x (µm)',ylabel='y (µm)')
    ax.set_title(f'{title}\n{x1-x0:.2f} × {y1-y0:.2f} µm',fontsize=12)


def main():
    base=Path(__file__).resolve().parent
    ap=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--reference-gds',type=Path,required=True)
    ap.add_argument('--frame',type=Path,required=True)
    ap.add_argument('--generated',type=Path,default=base.parent/'cells')
    ap.add_argument('--outdir',type=Path,default=base.parent/'results'/'hd_comparison')
    ap.add_argument('--installed-characterization',type=Path,nargs='*',
                    default=[base.parent/'results'/'characterization'/'hd',base.parent/'results'/'characterization'/'hd_reference'],
                    help="folders with characterize.py's <cell>.json and <cell>.pex.spice for installed cells")
    args=ap.parse_args();args.outdir.mkdir(parents=True,exist_ok=True)
    library=db.Layout();library.read(str(args.reference_gds));contract=json.loads(args.frame.read_text())['frame']
    results={};skipped=[]
    for folder in sorted(args.generated.iterdir()):
        if not (folder/'cell.gds').is_file():continue
        spec=json.loads((folder/'input.json').read_text())
        reference_name=spec.get('hd_reference')
        if not reference_name:
            skipped.append(folder.name);continue
        reference=library.cell(reference_name)
        if reference is None:raise ValueError(f'{reference_name} is not in {args.reference_gds}')
        suffix=reference_name.removeprefix('sky130_fd_sc_hd__')
        l=db.Layout();l.read(str(folder/'cell.gds'));generated=l.top_cell()
        a,b=measure(library,reference,contract),measure(l,generated,contract)
        pins={'installed':pin_check(a,contract,spec['inputs'],spec['output']),
              'generated':pin_check(b,contract,spec['inputs'],spec['output'])}
        names=lambda m:sorted({p['name'] for p in m['pins']})
        pins['generated']['pin_names_equal_installed']=names(a)==names(b)
        found=[d for d in args.installed_characterization if (d/f'{suffix}.json').is_file()]
        installed_dir=found[0] if found else args.installed_characterization[0]
        results[suffix]={'generated_cell':folder.name,'installed':a,'generated':b,
                         'frame_checks':{'installed':frame_check(library,reference,contract),
                                         'generated':frame_check(l,generated,contract)},
                         'pin_checks':pins,
                         'extra_sites':b['width_sites']-a['width_sites'],
                         'area_increase_percent':round(100*(b['area_um2']/a['area_um2']-1),3),
                         'gate_order':{'installed':[d['gate'] for d in a['devices'] if d['row']=='nmos'],
                                       'generated':[d['gate'] for d in b['devices'] if d['row']=='nmos']},
                         'parasitics':net_parasitics(folder/f'{folder.name}_pex.spice',installed_dir/f'{suffix}.pex.spice'),
                         'timing':timing_comparison(folder/'characterized.json',installed_dir/f'{suffix}.json')}
        fig,axes=plt.subplots(1,2,figsize=(12,7),layout='constrained')
        maxwidth=max(a['width_um'],b['width_um'])
        draw(axes[0],library,reference,maxwidth,'Installed HD '+suffix)
        draw(axes[1],l,generated,maxwidth,'Generated '+suffix)
        fig.legend(handles=[Patch(facecolor=color,alpha=alpha,label=n) for n,_,color,alpha in LAYERS],
                   loc='outside lower center',ncol=6,frameon=False)
        fig.savefig(args.outdir/(suffix+'_side_by_side.png'),dpi=170,bbox_inches='tight');plt.close(fig)
    report={'reference_gds_sha256':hashlib.sha256(args.reference_gds.read_bytes()).hexdigest(),
            'cells':results,'skipped_no_hd_reference':skipped,
            'timing_measured':sorted(k for k,v in results.items() if v['timing']),
            'parasitics_measured':sorted(k for k,v in results.items() if v['parasitics'])}
    # Table 4: the frame, the wiring layers and the pins must match; the rest is reported.
    report['passed']=bool(results) and all(c['passed'] for v in results.values()
                                           for c in v['frame_checks']['generated'].values()) \
        and all(all(v['pin_checks']['generated'].values()) for v in results.values())
    (args.outdir/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:{'extra_sites':v['extra_sites'],'area_increase_percent':v['area_increase_percent'],
                        'all_frame_checks_pass':all(c['passed'] for group in v['frame_checks'].values() for c in group.values()),
                        'pins_pass':all(v['pin_checks']['generated'].values()),
                        'pins_failing':[c for c,ok in v['pin_checks']['generated'].items() if not ok],
                        'parasitics':bool(v['parasitics']),'timing':bool(v['timing'])}
                      for k,v in results.items()},indent=2))
    print('PASS' if report['passed'] else 'FAIL',': frame, wiring layers and pins of the generated cells',sep='')
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
