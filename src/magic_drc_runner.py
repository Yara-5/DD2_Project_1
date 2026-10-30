"""Linux adapter: use Tutorial 3's exact full-DRC command prefix per case."""
from pathlib import Path
import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('tutorial-root','manifest','outdir'):ap.add_argument('--'+key,type=Path,required=True)
    args=ap.parse_args();root=args.tutorial_root.resolve();out=args.outdir.resolve()
    manifest=json.loads(args.manifest.read_text());os.environ['LAYOUT_IMAGE']=manifest['image']
    spec=importlib.util.spec_from_file_location('course_flow',root/'flow.py')
    flow=importlib.util.module_from_spec(spec);spec.loader.exec_module(flow)
    source=(root/'verify.py').read_text()
    assert hashlib.sha256(source.encode()).hexdigest()==manifest['source_verify_sha256']
    nodes=[n for n in ast.parse(source).body if
           (isinstance(n,ast.FunctionDef) and n.name=='magic_script') or
           (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='MAGIC_ERRORS' for t in n.targets))]
    ns={'TECH':flow.TECH}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root/'verify.py'),'exec'),ns)
    build=args.manifest.parent;results=[]
    for case in manifest['cases']:
        name=case['name']
        # The original prefix includes full DRC, catchup, count and reasons.
        script=ns['magic_script'](name,[]).split('extract all',1)[0]
        script+='puts "MAGIC_DONE"\nquit -noprompt\n'
        (build/(name+'.tcl')).write_text(script)
        output=flow.run('magic',['-dnull','-noconsole','-rcfile','/dev/null'],build,out/(name+'.log'),stdin=script)
        count=re.search(r'^DRC_COUNT (\d+)',output,re.M)
        clean=bool(count) and 'MAGIC_DONE' in output and not re.search(ns['MAGIC_ERRORS'],output)
        number=int(count[1]) if count else None
        passed=clean and (number>0 if case['kind']=='broken_drc' else number==0)
        result={**case,'drc_count':number,'completed_cleanly':clean,'passed':passed}
        results.append(result)
        print(f'{name}: DRC={number} '+('PASS' if passed else 'FAIL'),flush=True)
    report={**manifest,'cases':results,'instructor_checks_modified':False,
            'passed':all(c['passed'] for c in results) and all(c['passed'] for c in manifest['layers'].values())
                     and all(c['rejected'] for c in manifest['broken_layer_cases'])}
    (out/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PASS' if report['passed'] else 'FAIL',flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
