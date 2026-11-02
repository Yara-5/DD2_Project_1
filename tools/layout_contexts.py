"""Stage an isolated course runtime and call the unchanged physical checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import klayout.db as db
import verify_drc
from project import execute, linux_path, load_config, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frame', required=True, type=Path)
    args = parser.parse_args()
    config = load_config()
    runtime = ROOT / 'build/course_runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    sources = {}
    for name in ('cells.py', 'plan.json', 'requirements.txt', 'verify.py', 'flow.py', 'tools.sh', 'abutment.py'):
        source = Path(config['tutorial_root']) / name
        shutil.copy2(source, runtime / name)
        sources[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    # All instructor files above are byte-for-byte copies; no instructor tree is written.
    if os.name == 'nt':
        execute(['wsl', '-d', config['distro'], '--', 'ln', '-sfn', config['pdk'], linux_path(runtime / 'pdk')], ROOT / 'results/checkpoint1/pdk_link.log')
    elif not (runtime / 'pdk').exists():
        (runtime / 'pdk').symlink_to(Path(config['pdk']).resolve(), target_is_directory=True)
    selected = json.loads((ROOT / 'build/selected_cells.json').read_text())
    staged = ROOT / 'build/selected_generated'
    staged.mkdir(parents=True, exist_ok=True)
    # Avoid including unrelated previous demo cells in a checkpoint run.
    for path in staged.iterdir():
        if path.is_dir() and path.name not in selected:
            raise ValueError('Stale selected_generated directory: run make clean before switching cell sets')
    for name in selected:
        shutil.copytree(ROOT / 'generated' / name, staged / name, dirs_exist_ok=True)
    reference = ROOT / 'build/reference/hd.gds'
    library = db.Layout()
    library.read(str(reference))
    for name in selected:
        spec = json.loads((staged / name / 'input.json').read_text())
        suffix = 'inv_1' if len(spec['inputs']) == 1 else 'nand2_1'
        refname = 'sky130_fd_sc_hd__' + suffix
        out = db.Layout()
        cell = verify_drc.copy_cell(out, library, library.cell(refname), refname)
        destination = ROOT / 'build/reference/cells' / (refname + '.gds')
        destination.parent.mkdir(parents=True, exist_ok=True)
        verify_drc.save_top(out, cell, destination)
    execute([sys.executable, '-B', str(ROOT / 'verify_drc.py'), '--tutorial-root', str(runtime),
             '--reference-gds', str(reference), '--generated', str(staged),
             '--outdir', str(ROOT / 'results/checkpoint1/drc'), '--image', config['image']],
            ROOT / 'results/checkpoint1/drc_driver.log', timeout=3600)
    execute([sys.executable, '-B', str(ROOT / 'compare_hd.py'), '--reference-gds', str(reference),
             '--frame', str(args.frame), '--generated', str(staged), '--outdir', str(ROOT / 'results/checkpoint1/hd_comparison')],
            ROOT / 'results/checkpoint1/comparison.log')
    write_json(ROOT / 'results/checkpoint1/instructor_sources.json', sources)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
