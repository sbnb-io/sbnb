import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / '.github/workflows/firmware-build.yml'


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
    def test_provider_workflows_use_immutable_commits(self):
        workflow = WORKFLOW.read_text()
        expected = {
            'reefyai/reefy-nvidia':
                'bae37235695f797f86c05efc06daa0b927752a19',
            'reefyai/reefy-intel':
                '03b2e8fbaab93a7523613b08d237737e50a5e5ad',
            'reefyai/reefy-amd':
                'ccaf743c5dd77af5aaab91ee8300d1d5402ee1f9',
            'reefyai/reefy-artifact-fixtures':
                '1e51a8edb8547ce173cf67b5973eb1ff61f88f50',
        }
        for repository, commit in expected.items():
            self.assertEqual(_workflow_ref(workflow, repository), commit)

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
