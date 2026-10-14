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
    *ghcr.io/v2/*) printf '404' ;;
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


@pytest.mark.parametrize('response,code,pushes', [
    ('200', 0, 0), ('404', 0, 2),
    ('401', 2, 0), ('403', 2, 0), ('500', 2, 0),
    ('network', 2, 0), ('token-failure', 2, 0), ('empty-token', 2, 0),
])
def test_registry_errors_never_trigger_rebuild(tmp_path, response, code, pushes):
    log = tmp_path / 'calls.txt'
    harness = r'''set -euo pipefail
curl() {
  case "$*" in
    *ghcr.io/token*)
      [ "$RESPONSE" = token-failure ] && return 22
      [ "$RESPONSE" = empty-token ] && { printf '{}'; return 0; }
      printf '{"token": "test-token"}' ;;
    *ghcr.io/v2/*)
      [ "$RESPONSE" = network ] && return 7
      printf '%s' "$RESPONSE" ;;
  esac
}
docker() { printf '%s\n' "$*" >> "$CALL_LOG"; }
source "$1"
'''
    env = dict(os.environ, RESPONSE=response, GHCR_TOKEN='test-only',
               GITHUB_REPOSITORY_OWNER='example-fork', CALL_LOG=log.as_posix(),
               TMPDIR=tmp_path.as_posix())
    run = subprocess.run([BASH, '-c', harness, 'mirror-test', SCRIPT.as_posix()],
                         env=env, capture_output=True, text=True, encoding='utf-8')
    assert run.returncode == code, run.stderr
    calls = log.read_text() if log.exists() else ''
    assert calls.count('buildx build') == pushes
    if code:
        assert 'ERROR:' in run.stderr
        assert not calls, 'authorization/network failure must stop before Docker login'


def test_test_workflow_bootstraps_its_own_mirror_without_write_access_in_tests():
    import yaml
    source = (ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8')
    workflow = yaml.safe_load(source)
    jobs = workflow['jobs']
    producer = jobs['prepare-test-mirror']
    assert "head.repo.full_name == github.repository" in producer['if']
    assert producer['permissions']['packages'] == 'write'
    assert 'prepare-test-mirror' in jobs['integration-tests']['needs']
    for job in ('integration-tests', 'e2e-tests'):
        assert jobs[job]['permissions']['packages'] == 'read'
        builds = [step for step in jobs[job]['steps']
                  if str(step.get('uses', '')).startswith('docker/build-push-action@')]
        assert builds
        for step in builds:
            args = step['with']['build-args']
            assert 'PBS_MIRROR_IMAGE=${{ env.PBS_MIRROR_IMAGE }}' in args
            assert "head.repo.full_name != github.repository" in args
    assert 'image="ghcr.io/${GITHUB_REPOSITORY,,}"' in source
    assert 'createComment' not in source
    assert 'git fetch --no-tags https://github.com/new-usemame/' in source


@pytest.mark.parametrize('event,head,expected', [
    ('pull_request', 'example/repo', True),
    ('pull_request', 'outsider/repo', False),
    ('workflow_dispatch', '', True),
])
def test_dev_gate_builds_internal_prs_even_when_repository_is_a_fork(tmp_path, event, head, expected):
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/docker-image-build-dev.yml').read_text(encoding='utf-8'))
    gate = next(step for step in workflow['jobs']['classify']['steps'] if step.get('id') == 'gate')
    assert gate['env']['IS_FORK'] == "${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository }}"
    out = tmp_path / 'output'
    env = dict(os.environ, GITHUB_EVENT_NAME=event, CONCURRENCY='true', IMAGE='true',
               HEAD_SHA='test', IS_FORK=str(event == 'pull_request' and head != 'example/repo').lower(),
               GITHUB_OUTPUT=out.as_posix())
    subprocess.run([BASH, '-e', '-c', gate['run']], env=env, check=True)
    assert f'full_build={str(expected).lower()}' in out.read_text()
    assert 'alias_image=false' in out.read_text()
