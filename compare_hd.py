"""Measure actual GDS differences and draw pairs at the same physical scale."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import klayout.db as db
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


def measure(l,c):
    u=l.dbu;get=lambda p:reg(l,c,p)
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
        gates.append({'row':'nmos' if b.center().y*u<1.3 else 'pmos',
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
            'width_sites':round(boundary.width()*u/.46),'area_um2':round(boundary.area()*u*u,6),
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
        if value=='width':return w
        if value=='width + 0.19':return w+.19
        raise ValueError(value)
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
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--reference-gds',type=Path,required=True)
    ap.add_argument('--frame',type=Path,required=True)
    ap.add_argument('--generated',type=Path,default=base/'generated')
    ap.add_argument('--outdir',type=Path,default=base/'results'/'hd_comparison')
    args=ap.parse_args();args.outdir.mkdir(parents=True,exist_ok=True)
    library=db.Layout();library.read(str(args.reference_gds));contract=json.loads(args.frame.read_text())['frame']
    results={}
    for folder in sorted(args.generated.iterdir()):
        if not (folder/'cell.gds').is_file():continue
        inputs=json.loads((folder/'input.json').read_text())['inputs']
        suffix='inv_1' if len(inputs)==1 else 'nand2_1'
        reference=library.cell('sky130_fd_sc_hd__'+suffix)
        l=db.Layout();l.read(str(folder/'cell.gds'));generated=l.top_cell()
        a,b=measure(library,reference),measure(l,generated)
        results[suffix]={'installed':a,'generated':b,
                         'frame_checks':{'installed':frame_check(library,reference,contract),
                                         'generated':frame_check(l,generated,contract)},
                         'extra_sites':b['width_sites']-a['width_sites'],
                         'area_increase_percent':round(100*(b['area_um2']/a['area_um2']-1),3)}
        fig,axes=plt.subplots(1,2,figsize=(12,7),layout='constrained')
        maxwidth=max(a['width_um'],b['width_um'])
        draw(axes[0],library,reference,maxwidth,'Installed HD '+suffix)
        draw(axes[1],l,generated,maxwidth,'Generated '+suffix)
        fig.legend(handles=[Patch(facecolor=color,alpha=alpha,label=n) for n,_,color,alpha in LAYERS],
                   loc='outside lower center',ncol=6,frameon=False)
        fig.savefig(args.outdir/(suffix+'_side_by_side.png'),dpi=170,bbox_inches='tight');plt.close(fig)
    report={'reference_gds_sha256':hashlib.sha256(args.reference_gds.read_bytes()).hexdigest(),
            'cells':results,'timing_measured':False}
    (args.outdir/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:{'extra_sites':v['extra_sites'],'area_increase_percent':v['area_increase_percent'],
                        'all_frame_checks_pass':all(c['passed'] for group in v['frame_checks'].values() for c in group.values())}
                      for k,v in results.items()},indent=2))


if __name__=='__main__':main()
