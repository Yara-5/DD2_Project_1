"""Step 5, part 2: draw a prepared layout plan with GDSfactory.

Every rectangle and label is read from cells/<name>/layout_plan.json. This part
does not size transistors, search Euler paths, compute dimensions, place devices
or route (AGENTS.md, rule 8). Writes cell.gds and layout_preview.png beside the
plan, using Tutorial 3's unchanged cells.py::rect for every Tutorial 3 layer.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import sys
import gdsfactory as gf
import klayout.db as kdb
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.path import Path as PlotPath
from matplotlib.patches import Patch, PathPatch



def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_tutorial_rect(root):
    """Import the unchanged instructor generator; reuse its rectangle primitive."""
    root=Path(root).resolve()
    for file in ('cells.py','plan.json','requirements.txt'):
        if not (root/file).is_file():raise ValueError(f'Tutorial 3 source missing: {root/file}')
    pins={}
    for line in (root/'requirements.txt').read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            package,pin=line.split('==')
            actual=version(package)
            if actual!=pin:raise ValueError(f'Tutorial pin mismatch: {package} is {actual}, expected {pin}')
            pins[package]=actual
    name='dd2_instructor_tutorial3_cells'
    spec=importlib.util.spec_from_file_location(name,root/'cells.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    module.course_provenance={
        'root':root.name,'cells_sha256':sha256(root/'cells.py'),
        'plan_sha256':sha256(root/'plan.json'),'python_packages':pins,
        'reused_function':'cells.py::rect',
    }
    return module


def course_rectangle(c,tutorial,layer_name,layer_pair,box):
    if layer_name in tutorial.LAYER and tuple(tutorial.LAYER[layer_name])==tuple(layer_pair):
        tutorial.rect(c,layer_name,*box)
        return
    # HD-only layers and the changed HD boundary layer are not in the tutorial.
    # Apply the same grid/emptiness guard, then draw the explicit numeric layer.
    x0,y0,x1,y1=box
    for coordinate in box:
        if abs(coordinate/tutorial.GRID-round(coordinate/tutorial.GRID))>1e-6:
            raise ValueError(f'Off the Tutorial 3 grid: {box}')
    if x1<=x0 or y1<=y0:raise ValueError(f'Empty rectangle: {box}')
    c.add_polygon([(x0,y0),(x1,y0),(x1,y1),(x0,y1)],layer=layer_pair)


def picture(gds_path,png_path):
    """Tutorial 3's GDS-readback picture flow, with the HD layer map.

    Plot exported geometry, including polygon holes. Dimensions in the title
    describe the HD cell boundary; wells and rails may extend beyond it.
    """
    layout=kdb.Layout()
    layout.read(str(gds_path))
    cell=layout.top_cell()
    if cell is None:raise ValueError('Cannot render a GDS without a top cell')
    dbu=layout.dbu
    layers=[
        ('nwell',(64,20),'#ebe0fa',1),
        ('nsdm',(93,44),'#a9e6b3',.45),
        ('psdm',(94,20),'#ffd0b3',.45),
        ('hvtp',(78,44),'#dfae86',.25),
        ('diff',(65,20),'#47a66c',1),
        ('tap',(65,44),'#7aab56',1),
        ('poly',(66,20),'#dd524d',1),
        ('npc',(95,20),'#eacccc',.65),
        ('li1',(67,20),'#b78bea',.65),
        ('met1',(68,20),'#519ae5',.65),
        ('met2',(69,20),'#edb640',.65),
        ('licon',(66,44),'#25283a',1),
        ('mcon',(67,44),'#ffffff',1),
        ('via1',(68,44),'#282c44',1),
    ]
    fig,ax=plt.subplots(figsize=(7,8),layout='constrained')
    legend=[]
    try:
        for name,pair,color,alpha in layers:
            region=kdb.Region(cell.begin_shapes_rec(layout.layer(*pair))).merged()
            if not region.is_empty():
                legend.append(Patch(facecolor=color,edgecolor='#666666',alpha=alpha,label=name))
            for polygon in region.each():
                rings=[list(polygon.each_point_hull())]
                rings.extend(list(polygon.each_point_hole(i)) for i in range(polygon.holes()))
                vertices,codes=[],[]
                for i,ring in enumerate(rings):
                    points=[(p.x*dbu,p.y*dbu) for p in ring]
                    area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1]))
                    if (area>0)!=(i==0):points.reverse()
                    vertices.extend(points+[points[0]])
                    codes.extend([PlotPath.MOVETO]+[PlotPath.LINETO]*(len(points)-1)+[PlotPath.CLOSEPOLY])
                ax.add_patch(PathPatch(PlotPath(vertices,codes),facecolor=color,
                                       edgecolor='#555555' if name=='mcon' else color,
                                       linewidth=.3,alpha=alpha))
        boundary=kdb.Region(cell.begin_shapes_rec(layout.layer(236,0)))
        if boundary.is_empty():raise ValueError('GDS is missing its HD boundary (236/0)')
        bb=boundary.bbox()
        x0,y0,x1,y1=[v*dbu for v in (bb.left,bb.bottom,bb.right,bb.top)]
        ax.plot([x0,x1,x1,x0,x0],[y0,y0,y1,y1,y0],color='#374151',linestyle='--',linewidth=1)
        seen=set()
        for pair in ((67,5),(68,5)):
            for shape in cell.shapes(layout.layer(*pair)).each():
                if not shape.is_text():continue
                label=shape.text
                key=(label.string,label.x,label.y)
                if key in seen:continue
                seen.add(key)
                ax.text(label.x*dbu,label.y*dbu,label.string,ha='center',va='center',
                        fontsize=9,fontweight='bold',color='#17202a',
                        bbox={'facecolor':'white','edgecolor':'none','alpha':.85,'pad':1.5})
        bounds=cell.dbbox()
        ax.set(xlim=(min(x0,bounds.left)-.25,max(x1,bounds.right)+.25),
               ylim=(min(y0,bounds.bottom)-.25,max(y1,bounds.top)+.25),
               aspect='equal',xlabel='x (µm)',ylabel='y (µm)')
        ax.set_title(f'{cell.name}\nHD boundary: {x1-x0:.2f} × {y1-y0:.2f} µm',fontsize=12)
        ax.legend(handles=legend,loc='upper left',bbox_to_anchor=(1.01,1),frameon=False)
        fig.savefig(png_path,dpi=170,bbox_inches='tight')
    finally:
        plt.close(fig)


def generate_gds(plan_path,tutorial_root,outdir):
    """Draw prepared rectangles/labels exactly, with no layout decisions."""
    plan_path=Path(plan_path).resolve()
    document=json.loads(plan_path.read_text(encoding='utf-8'))
    if document.get('schema')!='dd2.cp1.layout-plan.v1' or document.get('units')!='um':
        raise ValueError('Expected the checkpoint 1 prepared-plan schema in micrometers')
    schematic=plan_path.with_name('schematic.spice')
    if not schematic.is_file():raise ValueError('Part 1 schematic.spice is missing beside the plan')
    tutorial=load_tutorial_rect(tutorial_root)
    if abs(document['grid_um']-tutorial.GRID)>1e-9:
        raise ValueError('Prepared grid differs from Tutorial 3')
    gf.Pdk(name='dd2_checkpoint1_numeric_layers',cells={},cross_sections={}).activate()
    c=gf.Component(name=document['name'])
    for shape in document['geometry']['shapes']:
        course_rectangle(c,tutorial,shape['layer'],tuple(shape['gds']),shape['box'])
    for label in document['labels']:
        c.add_label(label['text'],position=tuple(label['position_um']),layer=tuple(label['gds']))
    destination=Path(outdir)/document['name']
    destination.mkdir(parents=True,exist_ok=True)
    gds_path=destination/'cell.gds'
    c.write_gds(gds_path,with_metadata=False)
    png_path=destination/'layout_preview.png'
    picture(gds_path,png_path)
    # These are prepared data, serialized for the existing verification flow.
    for filename,data in [('input.json',document['input']),('dimensions.json',document['dimensions']),
                          ('physical_plan.json',document['geometry'])]:
        (destination/filename).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    (destination/'schematic.spice').write_bytes(schematic.read_bytes())
    report={'status':'generated_unverified','prepared_plan_sha256':sha256(plan_path),
            'gds_sha256':sha256(gds_path),'png_sha256':sha256(png_path),
            'preview_source':'cell.gds read back with KLayout',
            'gdsfactory_version':version('gdsfactory'),
            'klayout_version':version('klayout'),'tutorial_source':tutorial.course_provenance,
            'preparation_performed_here':False,
            'checks_not_implied':['DRC','LVS','RC-LVS','functional simulation','HD abutment']}
    (destination/'generation_report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return gds_path


def main():
    base=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('layout_plan',type=Path)
    parser.add_argument('--tutorial-root',type=Path,required=True,help='Unchanged instructor Tutorial 3 student folder')
    parser.add_argument('--outdir',type=Path,default=base.parent/'cells')
    args=parser.parse_args()
    path=generate_gds(args.layout_plan,args.tutorial_root,args.outdir)
    print(f'Wrote {path} and {path.with_name("layout_preview.png")}; dimensions and routes came entirely from the prepared plan.')


if __name__=='__main__':
    main()
