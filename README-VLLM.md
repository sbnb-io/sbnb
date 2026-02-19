
# Run vLLM with Nvidia GPU under Sbnb Linux in Automated Way

This tutorial will show how to get vLLM on a Bare Metal server up & running with Nvidia GPU in minutes.

We also start monitoring using Grafana and run a vLLM benchmark. At the end, you will be able to see the following vLLM benchmark results and monitoring graphs from your Bare Metal server.

The graph below shows GPU load during a vLLM benchmark test for a few minutes, leading to a GPU load spike to 100%. Memory allocation is at 90% per vLLM config.

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-per-gpu.png)

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-all-gpu.png)


## Prerequisites

- Boot Bare Metal server into Sbnb Linux. Read more at [README-INSTALL.md](README-INSTALL.md).
- One or more Nvidia GPUs attached to the Bare Metal server
- Laptop with [Tailscale](https://tailscale.com/) configured to access the bare metal server for configuration.


## Howto

### 1. Boot Bare Metal Server into Sbnb Linux
Boot the Bare Metal server into Sbnb Linux using the instructions in [README-INSTALL.md](README-INSTALL.md). After booting, verify that the server appears in your **Tailscale machine list**.

![Sbnb Linux: Machine registered in Tailscale (tailnet)](images/serial-number-tailscale.png)

For more details on automatic hostname assignments, refer to [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md).

### 2. Connect Your Laptop to Tailscale
We will use a MacBook in this tutorial, but any machine, such as a Linux instance, should work the same.

### 3. Clone the Sbnb Repository

```sh
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb
```

### 4. Start a VM with GPU Passthrough

```sh
ansible-playbook -i sbnb-F6S0R8000719, \
  collections/ansible_collections/sbnb/compute/playbooks/start-vm.yml \
  -e sbnb_vm_tskey="tskey-auth-xxxxx" \
  -e sbnb_vm_attach_gpus=true \
  -e sbnb_vm_vcpu=8 \
  -e sbnb_vm_mem=16G \
  -e sbnb_vm_image_size=100G
```

Replace `sbnb-F6S0R8000719` with your server's Tailscale hostname and `tskey-auth-xxxxx` with your Tailscale auth key.

See [README-COLLECTIONS.md](README-COLLECTIONS.md) for all VM options.

Once the VM starts, you should see it appear in the Tailscale network as `sbnb-vm-VMID`. For example, `sbnb-vm-67f97659333f`.

All Nvidia GPUs present in the system will be attached to this VM using a low-overhead vfio-pci mechanism:

![nvidia-vfio-sbnb-linux](images/nvidia-vfio-sbnb-linux.png)

### 5. Install Docker and NVIDIA Drivers in the VM

```sh
export VM_HOST=sbnb-vm-67f97659333f

ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/install-docker.yml

ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/install-nvidia.yml
```

### 6. Start vLLM

Start vLLM with default settings (serves on port 8000):

```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e sbnb_vllm_model="Qwen/Qwen3-0.6B"
```

For gated models (like Llama), you need a HuggingFace token:

```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e sbnb_vllm_model="meta-llama/Llama-3.1-8B-Instruct" \
  -e sbnb_vllm_hf_token="hf_xxxxx"
```

For multi-GPU setups, use extra args (JSON format for arguments with spaces):

```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e '{"sbnb_vllm_model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0", "sbnb_vllm_extra_args": "--tensor-parallel-size 2 --max-model-len 2048"}'
```

**Congratulations!** Now you have vLLM up and running.

Please refer to the vLLM engine arguments for more details:
https://docs.vllm.ai/en/latest/serving/engine_args.html

## Run Benchmark with NVIDIA GenAI-Perf

Run on the laptop using NVIDIA's GenAI-Perf tool (works with any OpenAI-compatible API):

```bash
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-genai-perf.yml \
  -e sbnb_genai_perf_model="Qwen/Qwen3-0.6B" \
  -e sbnb_genai_perf_concurrency=24
```

### Example Output of the Benchmark (RTX 5060 Ti 16GB)

```text
                                      NVIDIA GenAI-Perf | LLM Metrics
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃                         Statistic ┃       avg ┃      min ┃       max ┃       p99 ┃       p90 ┃       p75 ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│              Request Latency (ms) │ 10,483.07 │ 5,538.97 │ 16,605.04 │ 16,426.85 │ 12,728.09 │ 12,029.73 │
│   Output Sequence Length (tokens) │    885.89 │   453.00 │  1,395.00 │  1,383.95 │  1,092.00 │  1,016.75 │
│    Input Sequence Length (tokens) │    550.14 │   550.00 │    551.00 │    551.00 │    551.00 │    550.00 │
│ Output Token Throughput (per sec) │  1,674.54 │      N/A │       N/A │       N/A │       N/A │       N/A │
│      Request Throughput (per sec) │      1.89 │      N/A │       N/A │       N/A │       N/A │       N/A │
│             Request Count (count) │     66.00 │      N/A │       N/A │       N/A │       N/A │       N/A │
└───────────────────────────────────┴───────────┴──────────┴───────────┴───────────┴───────────┴───────────┘
```

## Display GPU Utilization in Grafana

Follow this guide:
[README-NVIDIA-GPU-FRYER-GRAFANA.md](README-NVIDIA-GPU-FRYER-GRAFANA.md)

## Stopping vLLM

```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e sbnb_vllm_state=absent
```

## Summary

You now have:

- A GPU-enabled VM on Bare Metal running Sbnb Linux
- vLLM deployed automatically via Ansible
- Full monitoring via Grafana
- vLLM benchmark results confirming throughput and performance

**Happy experimenting with vLLM!**
