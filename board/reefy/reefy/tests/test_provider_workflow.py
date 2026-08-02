import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / '.github/workflows/firmware-build.yml'


def test_amd_provider_sources_use_one_immutable_commit():
    workflow = WORKFLOW.read_text()
    builder = re.search(
        r'repository: reefyai/reefy-amd\s+ref: ([0-9a-f]{40})',
        workflow)
    publisher = re.search(
        r'uses: reefyai/reefy-amd/\.github/workflows/'
        r'publish\.yml@([0-9a-f]{40})', workflow)
    assert builder is not None
    assert publisher is not None
    assert builder.group(1) == publisher.group(1)
