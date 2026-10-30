"""Step 6 checks: full Magic DRC in HD rows, abutment, and the layer check.

For every cells/<name>/cell.gds this stages, with KLayout, 8 placements next to
each of tapvpwrvgnd_1, inv_1 and fill_1 (side by side, mirrored, and flipped onto
a shared VPWR or VGND rail, in both orders), plus one broken DRC context, and
runs the layer check of handout section 3.2 (nsdm/psdm/hvtp/npc coverage), with
enclosures read from src/rules.json, on the generated and the installed cells.
Magic then runs in the course container through Tutorial 3's unchanged flow.py
(magic_drc_runner.py). --linux gives the command prefix that reaches Linux, e.g.
"wsl -d Ubuntu-26.04 --" on Windows; all paths passed across are relative to the
current directory, so they mean the same on both sides.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import klayout.db as db


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def region(layout,cell,pair):
    return db.Region(cell.begin_shapes_rec(layout.layer(*pair))).merged()


def boundary(layout,cell):
    r=region(layout,cell,(236,0))
    # Installed tap/fill GDS omit prBoundary; areaid.sc gives their footprint.
    if r.is_empty():r=region(layout,cell,(81,4))
    if r.is_empty():raise ValueError(f'{cell.name}: HD footprint missing')
    return r.bbox()


def layer_check(path,rules,cell_name=None):
    """Read actual masks; require coverage, not just the presence of layers."""
    layout=db.Layout();layout.read(str(path))
    cell=layout.cell(cell_name) if cell_name else layout.top_cell()
    if cell is None:raise ValueError(f'Cell missing in {path}: {cell_name}')
    get=lambda pair:region(layout,cell,pair)
    diff,nwell,poly,licon=map(get,[(65,20),(64,20),(66,20),(66,44)])
    pmos=diff&nwell;nmos=diff-nwell
    # The project requires these drawn-layer coverages in addition to Magic.
    # Enclosures come from rules.json (checked against the PDK), not the plan.
    implant_margin=round(rules['implant_enclosure']/layout.dbu)
    npc_margin=round(rules['licon_npc_enclosure']/layout.dbu)
    contacts=licon&poly
    failures={
        'nsdm_encloses_nmos':nmos.sized(implant_margin)-get((93,44)),
        'psdm_encloses_pmos':pmos.sized(implant_margin)-get((94,20)),
        'hvtp_covers_pmos':pmos-get((78,44)),
        'npc_encloses_poly_contacts':contacts.sized(npc_margin)-get((95,20)),
    }
    checks={k:{'passed':v.is_empty(),'uncovered_area_um2':round(v.area()*layout.dbu**2,9)}
            for k,v in failures.items()}
    nonempty=all(not x.is_empty() for x in (nmos,pmos,contacts))
    return {'cell':cell.name,'gds_sha256':sha256(path),'checks':checks,
            'required_device_and_contact_geometry_present':nonempty,
            'passed':nonempty and all(v['passed'] for v in checks.values())}


def copy_cell(target,source,cell,name):
    c=target.create_cell(name);c.copy_tree(cell)
    return c


def save_top(layout,cell,path):
    options=db.SaveLayoutOptions();options.select_cell(cell.cell_index())
    layout.write(str(path),options)


def stage_context(build,library,generated,reference,how,reverse,serial,h):
    core=db.Layout();core.read(str(generated))
    l=db.Layout();l.dbu=core.dbu
    gen=copy_cell(l,core,core.top_cell(),'generated')
    ref=copy_cell(l,library,library.cell(reference),'neighbor')
    tap=copy_cell(l,library,library.cell('sky130_fd_sc_hd__tapvpwrvgnd_1'),'tap')
    fill=copy_cell(l,library,library.cell('sky130_fd_sc_hd__fill_1'),'fill')
    top=l.create_cell(f'cp1_abut_{serial:03d}')
    wg=boundary(l,gen).width()*l.dbu;wr=boundary(l,ref).width()*l.dbu
    tw=boundary(l,tap).width()*l.dbu
    left,right=(ref,gen) if reverse else (gen,ref)
    wl,wb=(wr,wg) if reverse else (wg,wr)
    top.insert(db.DCellInstArray(left.cell_index(),db.DTrans()))
    if how=='R0':t=db.DTrans(wl,0)
    elif how=='MY':t=db.DTrans(2,True,wl+wb,0)
    elif how=='MX_above':t=db.DTrans(0,True,0,2*h)
    elif how=='MX_below':t=db.DTrans(0,True,0,0)
    else:raise ValueError(how)
    top.insert(db.DCellInstArray(right.cell_index(),t))
    # Real HD logic has no taps. Add legal taps at the row ends for every
    # orientation; no rule is waived and the entire context is checked.
    width=wl+wb if how in ('R0','MY') else max(wl,wb)
    if how.startswith('MX_'):
        # Complete both rows with installed fillers. An unfinished short row
        # leaves a well notch at its outer end, unrelated to the tested seam.
        for row_width,flipped in ((wl,False),(wb,True)):
            for k in range(round((width-row_width)/tw)):
                x=row_width+k*tw
                y=(2*h if how=='MX_above' else 0) if flipped else 0
                top.insert(db.DCellInstArray(fill.cell_index(),db.DTrans(0,flipped,x,y)))
    for x in (-tw,width):
        top.insert(db.DCellInstArray(tap.cell_index(),db.DTrans(x,0)))
        if how.startswith('MX_'):
            y=2*h if how=='MX_above' else 0
            top.insert(db.DCellInstArray(tap.cell_index(),db.DTrans(0,True,x,y)))
    top.flatten(True)
    # These are DRC-only assembled contexts. Do not merge unrelated net labels.
    for pair in ((67,5),(68,5),(69,5),(64,5),(64,59)):
        l.clear_layer(l.layer(*pair))
    # Tutorial 3 replaces individual boundaries in its assembled DRC fixture.
    # Keep the original cell files intact and use the HD layer for this one.
    l.clear_layer(l.layer(236,0))
    bottom=-h if how=='MX_below' else 0
    top_y=2*h if how=='MX_above' else h
    top.shapes(l.layer(236,0)).insert(db.DBox(-tw,bottom,width+tw,top_y))
    path=build/(top.name+'.gds');save_top(l,top,path)
    return {'name':top.name,'kind':'abutment','generated':core.top_cell().name,
            'neighbor':reference,'placement':how,'generated_position':'right/top' if reverse else 'left/bottom',
            'gds_sha256':sha256(path)}


def relative(path):
    """A path both sides of --linux agree on: relative, with forward slashes."""
    return Path(os.path.relpath(Path(path).resolve())).as_posix()


def main():
    base=Path(__file__).resolve().parent
    ap=argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tutorial-root',type=Path,required=True)
    ap.add_argument('--reference-gds',type=Path,required=True)
    ap.add_argument('--frame',type=Path,required=True,help='given/plan_hd.json')
    ap.add_argument('--rules',type=Path,default=base/'rules.json')
    ap.add_argument('--generated',type=Path,default=base.parent/'cells')
    ap.add_argument('--outdir',type=Path,default=base.parent/'results'/'drc')
    ap.add_argument('--image',default='dd2-layout-tools')
    ap.add_argument('--linux',default='',help='Command prefix that runs a command on Linux; empty on Linux')
    args=ap.parse_args()
    rules={k:v['value'] for k,v in json.loads(args.rules.read_text())['rules'].items()}
    height=json.loads(args.frame.read_text())['frame']['site']['height']
    root=args.tutorial_root.resolve();out=args.outdir.resolve();out.mkdir(parents=True,exist_ok=True)
    # An interrupted or failed rerun must never leave an old PASS as its result.
    (out/'verification.json').write_text(json.dumps({'passed':False,'status':'running_or_incomplete'})+'\n')
    build=root/'build'/'cp1_drc';build.mkdir(parents=True,exist_ok=True)
    library=db.Layout();library.read(str(args.reference_gds))
    references=['sky130_fd_sc_hd__'+s for s in ('tapvpwrvgnd_1','inv_1','fill_1')]
    cases=[];layer_results={};broken_layers=[]
    for folder in sorted(args.generated.iterdir()):
        gds=folder/'cell.gds'
        if not gds.is_file():continue
        name=folder.name
        layer_results[name]=layer_check(gds,rules)
        for neighbor in references:
            for reverse in (False,True):
                for how in ('R0','MY','MX_above','MX_below'):
                    cases.append(stage_context(build,library,gds,neighbor,how,reverse,len(cases),height))
        # Immutable derived broken inputs; never modify original input GDS.
        for pair,label in [((93,44),'nsdm'),((94,20),'psdm'),((78,44),'hvtp'),((95,20),'npc')]:
            broken=db.Layout();broken.read(str(gds));broken.clear_layer(broken.layer(*pair))
            path=build/(name+'_missing_'+label+'.gds');broken.write(str(path))
            result=layer_check(path,rules)
            broken_layers.append({'case':path.name,'removed':label,'rejected':not result['passed']})
    for reference in ('sky130_fd_sc_hd__inv_1','sky130_fd_sc_hd__nand2_1'):
        layer_results[reference]=layer_check(args.reference_gds,rules,reference)
    if not cases:raise ValueError('No generated GDS cells found')
    # DRC control: a deliberately narrow isolated metal stripe in a valid row.
    first=build/(cases[0]['name']+'.gds')
    broken=db.Layout();broken.read(str(first));cell=broken.top_cell()
    cell.name='cp1_broken_met1_width'
    cell.shapes(broken.layer(68,20)).insert(db.DBox(.2,1.10,.25,1.60))
    path=build/(cell.name+'.gds');save_top(broken,cell,path)
    cases.append({'name':cell.name,'kind':'broken_drc','expected':'nonzero','gds_sha256':sha256(path)})
    manifest={'image':args.image,'cases':cases,'layers':layer_results,'broken_layer_cases':broken_layers,
              'reference_gds_sha256':sha256(args.reference_gds),
              'source_verify_sha256':sha256(root/'verify.py'),'source_abutment_sha256':sha256(root/'abutment.py')}
    manifest_path=build/'manifest.json';manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    runner=base/'magic_drc_runner.py'
    prefix=shlex.split(args.linux)
    command=prefix+['python3' if prefix else sys.executable,relative(runner),'--tutorial-root',relative(root),
                    '--manifest',relative(manifest_path),'--outdir',relative(out)]
    result=subprocess.run(command,check=False)
    return result.returncode


if __name__=='__main__':raise SystemExit(main())
