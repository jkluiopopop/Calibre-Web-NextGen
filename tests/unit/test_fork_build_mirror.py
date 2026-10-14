"""Fork mirror producers and consumers must agree without contacting a registry."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/ensure-python-mirror.sh'
BASH = (r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt' else shutil.which('bash'))
pytestmark = pytest.mark.unit

@pytest.mark.skipif(not BASH or not Path(BASH).is_file(), reason='bash required')
@pytest.mark.parametrize('override,expected', [
    ('', 'ghcr.io/example-fork/pbs-cache'),
    ('ghcr.io/another-owner/custom-cache', 'ghcr.io/another-owner/custom-cache'),
])
def test_mirror_script_pushes_and_checks_the_selected_namespace(tmp_path, override, expected):
    log = tmp_path / 'calls.txt'
    # Shell functions replace every network/registry command. Real temp files stay
    # in pytest's supplied temporary directory; no registry credentials are used.
    harness = '''
set -euo pipefail
curl() {
  printf 'curl %s\\n' "$*" >> "$CALL_LOG"
  case "$*" in
    *ghcr.io/token*) printf '{"token":"test-token"}' ;;
    *ghcr.io/v2/*) return 22 ;;
  esac
}
docker() { printf 'docker %s\\n' "$*" >> "$CALL_LOG"; }
source "$1"
'''
    env = dict(os.environ, GITHUB_REPOSITORY_OWNER='example-fork',
               GITHUB_ACTOR='example-user', GHCR_TOKEN='test-only',
               PBS_MIRROR_IMAGE=override, TMPDIR=tmp_path.as_posix(),
               CALL_LOG=log.as_posix())
    env.pop('GHCR_USER', None)
    run = subprocess.run([BASH, '-c', harness, 'mirror-test', SCRIPT.as_posix()],
                         env=env, capture_output=True, text=True, encoding='utf-8')
    assert run.returncode == 0, run.stderr
    calls = log.read_text(encoding='utf-8')
    namespace = expected.removeprefix('ghcr.io/')
    assert f'scope=repository:{namespace}:pull' in calls
    assert f'ghcr.io/v2/{namespace}/manifests/' in calls
    pushes = [line for line in calls.splitlines() if 'docker buildx build' in line]
    assert len(pushes) == 2
    assert all(f'-t {expected}:' in line and '--push' in line for line in pushes)
    assert 'docker login ghcr.io -u example-user --password-stdin' in calls
    assert 'new-usemame' not in calls


def test_workflows_pass_one_mirror_reference_to_both_ends():
    for name, build_count in [('docker-image-build-dev.yml', 2),
                              ('docker-image-build-release.yml', 1)]:
        source = (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')
        assert '\nenv:\n  PBS_MIRROR_IMAGE: ghcr.io/${{ github.repository_owner }}/pbs-cache\n' in source
        assert 'run: bash scripts/ensure-python-mirror.sh' in source
        assert 'GHCR_USER: ${{ github.actor }}' in source
        assert source.count('PBS_MIRROR_IMAGE=${{ env.PBS_MIRROR_IMAGE }}') == build_count
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert dockerfile.index('ARG PBS_MIRROR_IMAGE=') < dockerfile.index('\nFROM ')
    assert 'FROM ${PBS_MIRROR_IMAGE}:cpython-' in dockerfile
    assert 'FROM ${PBS_MIRROR_IMAGE}:kepubify-' in dockerfile
    assert 'ARG PBS_SOURCE=upstream' in dockerfile
