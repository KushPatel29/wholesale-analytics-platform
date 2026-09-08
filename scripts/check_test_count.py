"""Keep the README test count tied to what this checkout actually collects."""
import re
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-o', 'addopts='],
        cwd=root, capture_output=True, text=True, check=False,
    )
    output = result.stdout + result.stderr
    collected = re.search(r'(\d+) tests? collected', output)
    claimed = re.search(
        r'\|\s*\*\*Structure\*\*\s*\|[^\n]*?([\d,]+)\s+tests\b',
        (root / 'README.md').read_text(encoding='utf-8'),
    )
    if result.returncode or not collected or not claimed:
        print(output)
        raise SystemExit('Could not compare collected tests with README Structure count.')
    actual, published = int(collected[1]), int(claimed[1].replace(',', ''))
    print(f'Collected: {actual}; README: {published}')
    if actual != published:
        raise SystemExit('Update README and portfolio/resume test totals together.')


if __name__ == '__main__':
    main()
