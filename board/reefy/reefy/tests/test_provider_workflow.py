import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / '.github/workflows/firmware-build.yml'
POST_BUILD = ROOT / 'board/reefy/reefy/post_build.sh'
PROVIDER_PINS = ROOT / 'board/reefy/reefy/provider-publisher-pins'
BUILD_ID_TOOL = (
    ROOT / 'board/reefy/reefy/scripts/calculate_build_id.sh')


def _workflow_ref(workflow, repository):
    match = re.search(
        rf'uses: {re.escape(repository)}/\.github/workflows/'
        r'publish\.yml@([0-9a-f]{40})', workflow)
    assert match is not None
    return match.group(1)


def _checkout_ref(workflow, repository):
    match = re.search(
        rf'repository: {re.escape(repository)}\s+ref: ([0-9a-f]{{40}})',
        workflow)
    assert match is not None
    return match.group(1)


class ProviderWorkflowTests(unittest.TestCase):
    def test_provider_publisher_pins_match_workflow_commits(self):
        workflow = WORKFLOW.read_text()
        pins = dict(
            line.split('=', 1)
            for line in PROVIDER_PINS.read_text().splitlines()
            if line)

        self.assertEqual(set(pins), {'nvidia', 'intel', 'amd'})
        for name, commit in pins.items():
            self.assertRegex(commit, r'^[0-9a-f]{40}$')
            self.assertEqual(
                _workflow_ref(workflow, f'reefyai/reefy-{name}'),
                commit)

    def test_build_identity_includes_provider_publisher_pins(self):
        post_build = POST_BUILD.read_text()

        self.assertIn('provider-publisher-pins', post_build)
        self.assertIn('scripts/calculate_build_id.sh', post_build)

    def test_provider_pin_change_changes_build_identity(self):
        original = PROVIDER_PINS.read_text()
        changed_lines = original.splitlines()
        nvidia_index = next(
            index for index, line in enumerate(changed_lines)
            if line.startswith('nvidia='))
        name, commit = changed_lines[nvidia_index].split('=', 1)
        replacement = '0' if commit[0] != '0' else '1'
        changed_lines[nvidia_index] = f'{name}={replacement}{commit[1:]}'
        changed = '\n'.join(changed_lines) + '\n'

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            original_path = directory / 'original-pins'
            changed_path = directory / 'changed-pins'
            original_path.write_text(original)
            changed_path.write_text(changed)

            def build_id(path, salt=''):
                return subprocess.check_output([
                    'bash', str(BUILD_ID_TOOL),
                    'synthetic-base-build', str(path), salt,
                ], text=True).strip()

            first = build_id(original_path)
            self.assertEqual(first, build_id(original_path))
            self.assertNotEqual(first, build_id(changed_path))
            self.assertNotEqual(first, build_id(original_path, 'e2e-next'))
            self.assertRegex(first, r'^[0-9a-f]{64}$')

    def test_provider_workflows_use_immutable_commits(self):
        workflow = WORKFLOW.read_text()
        expected = {
            'reefyai/reefy-nvidia':
                'bae37235695f797f86c05efc06daa0b927752a19',
            'reefyai/reefy-intel':
                '2b3a2a5db74bc28758a03b7cde82525d6d521bdf',
            'reefyai/reefy-amd':
                'ccaf743c5dd77af5aaab91ee8300d1d5402ee1f9',
            'reefyai/reefy-artifact-fixtures':
                '1e51a8edb8547ce173cf67b5973eb1ff61f88f50',
        }
        for repository, commit in expected.items():
            self.assertEqual(_workflow_ref(workflow, repository), commit)

    def test_release_ready_marker_requires_every_provider(self):
        workflow = WORKFLOW.read_text()
        marker = re.search(
            r'^  release-ready:\n(?P<body>.*)\Z', workflow,
            re.MULTILINE | re.DOTALL)

        self.assertIsNotNone(marker)
        body = marker.group('body')
        dependencies = body.split('    steps:', 1)[0]
        for job in (
                'build',
                'publish-nvidia-provider',
                'publish-intel-provider',
                'publish-amd-provider',
                'publish-artifact-fixture',
                'provider-catalog'):
            self.assertIn(f'      - {job}\n', dependencies)
        self.assertIn('    if: ${{ success() }}', dependencies)
        self.assertIn('          name: reefy-release-ready', body)

    def test_checked_out_sources_match_publisher_commits(self):
        workflow = WORKFLOW.read_text()
        for repository in (
                'reefyai/reefy-amd',
                'reefyai/reefy-artifact-fixtures'):
            self.assertEqual(
                _checkout_ref(workflow, repository),
                _workflow_ref(workflow, repository))

    def test_intel_provider_receives_modules_and_firmware(self):
        workflow = WORKFLOW.read_text()

        self.assertIn(
            '${{ env.BR_OUTPUT }}/reefy-artifacts/intel/modules-root',
            workflow)
        self.assertIn(
            '${{ env.BR_OUTPUT }}/reefy-artifacts/intel/firmware-root',
            workflow)
        self.assertIn('for module in i915 xe intel_vpu; do', workflow)
        self.assertIn('missing Intel provider input ${module}', workflow)

    def test_intel_payload_is_rejected_from_base_image(self):
        workflow = WORKFLOW.read_text()

        self.assertIn('Intel provider modules leaked', workflow)
        self.assertIn('Intel provider firmware leaked', workflow)
        self.assertIn("-name 'i915.ko*'", workflow)
        self.assertIn("-name 'xe.ko*'", workflow)
        self.assertIn("-name 'intel_vpu.ko*'", workflow)
