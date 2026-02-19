# Integration Testing

End-to-end integration tests for the SBNB compute collection. Tests run against a real bare metal host, creating and destroying VMs to verify the full stack from VM lifecycle to service deployment.

## Prerequisites

- A bare metal host with GPU(s) accessible via SSH
- A Tailscale auth key (reusable, ephemeral recommended)
- Ansible 2.14+ with required collections installed
- Python 3.9+

## Quick Start

```bash
cd collections/ansible_collections/sbnb/compute

./tests/integration/run-tests.sh \
  --host=bare-metal-host \
  --tskey=tskey-auth-xxx
```

### Options

```
--host=<host>         Bare metal host (IP or Tailscale hostname)
--tskey=<key>         Tailscale auth key for VM creation
--skip-gpu            Skip GPU tests (Phase 2-4) for CPU-only hosts
--hf-token=<token>    HuggingFace token for gated models
--vm-password=<pw>    VM root password (default: sbnb-test)
--runcmd=<cmd>        Command to run in VM on first boot (repeatable)
-v, -vv, -vvv        Ansible verbosity level
```

## Test Phases

Tests run sequentially in 5 phases. Phases 0-1 are required; phases 2-4 are skipped with `--skip-gpu`.

### Phase 0: Bare Metal Pre-checks

Confirms that the bare metal portion of Sbnb Linux has properly booted and configured the host: LVM storage mounted, bridge network up, QEMU container image available.

### Phase 1: CPU VM Lifecycle

Creates a small CPU-only VM, verifies SSH access via Tailscale, then destroys it. Validates the core VM create/destroy pipeline works.

### Phase 2: GPU VM Lifecycle

Creates a GPU VM with passthrough, verifies the GPU is visible inside the VM via `lspci`, then destroys it. Validates GPU passthrough works.

### Phase 3: GPU Services

Each service gets its own fresh VM with full isolation:

| Service | VM Name | What's Tested |
|---------|---------|---------------|
| gpu_fryer | `sbnb-test-fryer-*` | 30s GPU stress test completes |
| vLLM | `sbnb-test-vllm-*` | API responds, model loaded, inference works |
| SGLang | `sbnb-test-sglang-*` | API responds, model loaded, inference works |
| Frigate | `sbnb-test-frigate-*` | HTTPS endpoint accessible (200 or 401) |
| Ollama | `sbnb-test-ollama-*` | API responds, model pulled, inference works |
| LightRAG | `sbnb-test-ollama-*` | Health endpoint responds (shares VM with Ollama) |

Each service test follows the same pattern:
1. Create a fresh GPU VM
2. Wait for SSH via Tailscale
3. Mount encrypted data disk
4. Deploy the service (service playbook installs Docker + NVIDIA itself)
5. Run health checks and inference tests
6. Destroy the VM (guaranteed via `always` block)

### Phase 4: Non-GPU Services

| Service | VM Name | What's Tested |
|---------|---------|---------------|
| OpenClaw | `sbnb-test-openclaw-*` | Health endpoint responds |

Same isolation pattern as Phase 3 but without NVIDIA drivers.

## Example Output

```
══════════════════════════════════════════
SBNB INTEGRATION TEST SUMMARY
══════════════════════════════════════════
Started: 2026-02-19 06:40:52
Finished: 2026-02-19 07:11:32

Phase 0: Bare Metal: PASSED
Phase 1: CPU VM: PASSED
Phase 2: GPU VM: PASSED
  gpu_fryer: PASSED
  vLLM: PASSED
  SGLang: PASSED
  Frigate: PASSED
  Ollama: PASSED
  LightRAG: PASSED
  OpenClaw: PASSED

RESULT: ALL PASSED (10 phases)
══════════════════════════════════════════
```

## Architecture

### Per-Service VM Isolation

Every service test creates its own VM from scratch. This ensures:

- Each service is tested from a clean slate (no leftover state from previous services)
- A failure in one service does not affect others
- Tests match real deployment conditions (service playbooks are self-contained)

### Error Handling

- Each service is wrapped in Ansible `block/rescue/always` blocks
- Failures are recorded but don't abort the entire suite
- VMs are always destroyed in the `always` block, even on failure
- Ctrl-C triggers cleanup of all possible test VMs

### File Structure

```
collections/ansible_collections/sbnb/compute/tests/integration/
  run-tests.sh              # Wrapper script with CLI args and ctrl-c cleanup
  test-integration.yml      # Orchestrator playbook (phases 0-4)
  tasks/
    test-bare-metal.yml     # Phase 0: host pre-checks
    test-vm-no-gpu.yml      # Phase 1: CPU VM lifecycle
    test-vm-gpu.yml         # Phase 2: GPU VM lifecycle
    test-services-gpu.yml   # Phase 3: per-service GPU tests
    test-services-no-gpu.yml # Phase 4: per-service non-GPU tests
    setup-test-vm.yml       # Reusable: create VM, SSH, mount data disk
    cleanup-vm.yml          # Reusable: destroy VM (ignores errors)
```

### VM Naming

All VMs use a random 4-character suffix generated at the start of each run:

```
sbnb-test-{service}-{suffix}
```

For example: `sbnb-test-vllm-axkm`, `sbnb-test-frigate-axkm`.

This allows concurrent test runs on different hosts without name collisions, and enables the ctrl-c handler to clean up all VMs from the current run.

## Troubleshooting

### Test gets stuck

VMs are accessible via Tailscale during the test. SSH in to inspect:

```bash
ssh root@sbnb-test-vllm-axkm
```

### VM not cleaned up after failure

The ctrl-c handler and `always` blocks should handle cleanup. If a VM is left behind, remove it manually:

```bash
ansible-playbook -i bare-metal-host, playbooks/stop-vm.yml \
  -e sbnb_vm_name=sbnb-test-vllm-axkm \
  -e sbnb_vm_remove=true
```

### Logs

Each run saves a full log to `/tmp/sbnb-integration-test-{timestamp}.log`. Use `-vv` or `-vvv` for more detail.
