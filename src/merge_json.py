#!/usr/bin/env python3
"""Broken cases (make test): a copy of a generated JSON file with some fields replaced.

Inputs: a JSON object (for example cells/<cell>/input.json) and a JSON object of fields to
replace (a broken case in tests/broken/). Output: the first object with those fields set,
written to a third path. The broken case then stays a small file that names only what it
breaks, while the rest follows the current flow.
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('base', type=Path)
    ap.add_argument('fields', type=Path)
    ap.add_argument('out', type=Path)
    args = ap.parse_args()
    try:
        data = json.loads(args.base.read_text(encoding='utf-8'))
        data.update(json.loads(args.fields.read_text(encoding='utf-8')))
        args.out.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, AttributeError) as exc:
        print(f'FAIL: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
