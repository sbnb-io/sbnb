
# Run Qwen2.5-VL in vLLM and SGLang in Automated Way

In this tutorial, we will run `"Qwen/Qwen2.5-VL-3B-Instruct"` in an automated way using the **Sbnb Linux** distro and **Ansible**.  
We will demonstrate how to use **vLLM** and **SGLang** as inference engines - you can choose either one based on your preference.

---

## Prerequisites

- Boot Bare Metal server into Sbnb Linux. Read more at [README-INSTALL.md](README-INSTALL.md).
- One or more Nvidia GPUs attached to the Bare Metal server
- Laptop with [Tailscale](https://tailscale.com/) configured to access the bare metal server for configuration.

---

## How-To

### 1. Boot Bare Metal Server into Sbnb Linux

Follow [README-INSTALL.md](README-INSTALL.md) to boot your server into Sbnb Linux. After boot, verify it appears in your **Tailscale machine list**:

![Sbnb Linux: Machine registered in Tailscale](images/serial-number-tailscale.png)

See [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md) for automatic hostname assignment.

---

### 2. Connect Your Laptop to Tailscale

We use a MacBook in this tutorial, but any Linux/Unix laptop should work.

---

### 3. Download Tailscale Dynamic Inventory Script

```sh
curl https://raw.githubusercontent.com/m4wh6k/ansible-tailscale-inventory/refs/heads/main/ansible_tailscale_inventory.py -O
chmod +x ansible_tailscale_inventory.py
```

---

### 4. Pull Sbnb Linux Repo

```sh
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb/automation/
```

---

### 5. Configure VM Settings

Edit `sbnb-example-vm.json`:

```json
{
    "vcpu": 2,
    "mem": "4G",
    "tskey": "your-tskey-auth",
    "attach_gpus": true,
    "image_size": "10G"
}
```

Replace `"your-tskey-auth"` with your actual **Tailscale auth key**.

---

### 6. Start VM with Ansible Playbook

```sh
export SBNB_HOSTS=sbnb-F6S0R8000719
ansible-playbook -i ./ansible_tailscale_inventory.py sbnb-start-vm.yaml
```

You should see the VM appear in Tailscale as `sbnb-vm-<VMID>` (e.g., `sbnb-vm-67f97659333f`).

> All Nvidia GPUs will be attached using vfio-pci.

![nvidia-vfio-sbnb-linux](images/nvidia-vfio-sbnb-linux.png)

---

### 7. Install Nvidia Drivers and Tools in the VM

```bash
export SBNB_HOSTS=sbnb-vm-67f97659333f

for playbook in install-docker.yaml install-nvidia.yaml install-nvidia-container-toolkit.yaml; do
  ansible-playbook -i ./ansible_tailscale_inventory.py $playbook
done
```

> Note that this time we set `SBNB_HOSTS` to the hostname of the VM we started in the previous step.

These commands will install Docker, Nvidia drivers, Nvidia container toolkit, and SGLang into the VM.

---

At this point, you have a VM running **Ubuntu 24.04** with **Nvidia GPU** attached.

Below are steps to run either **vLLM** or **SGLang**. Pick the one you prefer:

---

# Run with vLLM

## 1. Configure vLLM

```bash
export LLM_ARGS="--max-model-len 2048
  --gpu-memory-utilization 0.9
  --tensor-parallel-size 2
  --max-num-seqs 32
  --enforce-eager
  --model Qwen/Qwen2.5-VL-3B-Instruct
  --dtype bfloat16
  --limit-mm-per-prompt image=5,video=5"
```

> We use `--tensor-parallel-size 2` for 2 GPUs, and choose a small model to fit into 24GB total GPU RAM.

For full options, see [vLLM Engine Args](https://docs.vllm.ai/en/latest/serving/engine_args.html).

---

## 2. Start vLLM

```bash
ansible-playbook -i ./ansible_tailscale_inventory.py run-vllm.yaml
```

✅ vLLM is now up and running!

---

# Run with SGLang

## 1. Configure SGLang

```bash
export LLM_ARGS="python3
  -m sglang.launch_server
  --host 0.0.0.0
  --port 8000
  --model-path Qwen/Qwen2.5-VL-3B-Instruct
  --dp 2
  --disable-radix-cache
  --chunked-prefill-size -1
  --chat-template qwen2-vl
  --mem-fraction-static 0.7"
```

> We use `--dp 2` for 2 GPUs and a small model to fit within available memory.

See [SGLang Server Args](https://github.com/sgl-project/sglang/blob/main/docs/backend/server_arguments.md) for more details.

---

## 2. Start SGLang

```bash
ansible-playbook -i ./ansible_tailscale_inventory.py run-sglang.yaml
```

✅ SGLang is now up and running!

---

# ✅ Testing

Let’s test by asking the model to recognize text in the Nvidia logo image.

```bash
LLM_URL="http://sbnb-0123456789-vm-58514aaf-a2c0-5ba6-9d04-0054d7dbeee3.tail730ca.ts.net:8000/v1/chat/completions"
IMAGE_URL="https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/logo-and-brand/01-nvidia-logo-horiz-500x200-2c50-d@2x.png"

curl ${LLM_URL}  -H "Content-Type: application/json" -d '{
    "model": "Qwen/Qwen2.5-VL-3B-Instruct",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "image_url", "image_url": {"url":"'${IMAGE_URL}'"}},
          {"type": "text", "text": "What is the text in the illustrate?"}
        ]
      }
    ]
}' | jq '.'
```

Replace `LLM_URL` with your VM’s Tailscale DNS name. Update `IMAGE_URL` if needed.

We use `jq` to pretty-print the JSON response.

---

## ✅ Expected Output

```json
{
  "id": "501a5d9a893241afbab2e4fae5d81916",
  "object": "chat.completion",
  "created": 1743186554,
  "model": "Qwen/Qwen2.5-VL-3B-Instruct",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The text in the illustration is \"nVIDIA.\""
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 1154,
    "completion_tokens": 11,
    "total_tokens": 1165
  }
}
```

Look for `"content": "The text in the illustration is "nVIDIA.""` - that means it's working!

---

## 🎉 That’s it!

You're now running Qwen2.5-VL using either vLLM or SGLang on your own Sbnb Linux VM with full GPU acceleration.

Happy experimenting! Reach out if you have questions or improvements to share!

### 🚀 Bonus  
Want detailed NVIDIA GPU monitoring with Grafana?  

Follow this guide:
👉 [NVIDIA GPU Monitoring with Grafana](https://github.com/sbnb-io/sbnb/blob/main/README-NVIDIA-GPU-FRYER-GRAFANA.md)

You'll get insightful dashboards like these:

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-per-gpu.png)

![Sbnb Linux: vLLM - Monitoring GPU Load, Memory, Temp, FAN speed, Power consumption (Watt) with Grafana](images/vllm-benchmark-all-gpu.png)
