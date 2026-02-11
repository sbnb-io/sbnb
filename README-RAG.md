# Run RAGFlow on a Bare Metal Server in Minutes (Fully Automated)

* **Note:** We also have a separate guide for running **LightRAG** using the same approach. Check it out here: [README-LightRAG.md](README-LightRAG.md) if you're interested.

This guide walks you through setting up a full RAG pipeline on your own bare metal server in just minutes. You'll launch:

- A bare metal server with Nvidia GPUs running **Sbnb Linux**
- A **VM with Ubuntu 24.04**
- The open-source [**RAGFlow**](https://github.com/infiniflow/ragflow) project
- The [**vLLM inference engine**](https://github.com/vllm-project/vllm) with the `Qwen/Qwen2.5-VL-3B-Instruct` model
- Upload a PDF to RAGFlow and extract knowledge from it

---

## Why Run RAG on Your Own Bare Metal Server?

### Privacy & Security
- Full control over your data - nothing leaves your server
- Ideal for **sensitive or regulated data** (e.g., healthcare, finance, legal)
- Compliant with **data sovereignty** requirements (e.g., store and process data within specific countries or jurisdictions)
- No vendor lock-in - run everything locally or in your own cloud

### Performance
- Faster retrieval and inference

### Cost Efficiency
- No pay-per-call fees
- Predictable, one-time hardware investment

### Customization
- Your own retrievers, embeddings, chunking strategy
- Fine-tuned or quantized LLMs
- Flexible RAG pipelines (filters, rerankers, etc.)

### Offline / Edge Ready
- Fully local operation
- Great for air-gapped or remote deployments

---

## Prerequisites

- Boot Bare Metal server into Sbnb Linux. Read more at [README-INSTALL.md](README-INSTALL.md).
- One or more Nvidia GPUs attached to the Bare Metal server
- Laptop with [Tailscale](https://tailscale.com/) configured to access the bare metal server for configuration.

---

## Step-by-Step Setup

### 1. Boot Bare Metal Server into Sbnb Linux

Follow [README-INSTALL.md](README-INSTALL.md) to boot your server into Sbnb Linux. After boot, verify it appears in your **Tailscale machine list**:

![Sbnb Linux: Machine registered in Tailscale](images/serial-number-tailscale.png)

See [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md) for automatic hostname assignment.

---

### 2. Connect Your Laptop to Tailscale

We use a MacBook in this tutorial, but any Linux/Unix laptop should work.

---

### 3. Clone the Sbnb Repository

```sh
git clone https://github.com/sbnb-io/sbnb.git
cd sbnb
```

---

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

You should see the VM appear in Tailscale as `sbnb-vm-<VMID>` (e.g., `sbnb-vm-67f97659333f`).

> All Nvidia GPUs will be attached using vfio-pci.

![nvidia-vfio-sbnb-linux](images/nvidia-vfio-sbnb-linux.png)

---

### 5. Install Docker and NVIDIA Drivers in the VM

```sh
export VM_HOST=sbnb-vm-67f97659333f

ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/install-docker.yml

ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/install-nvidia.yml
```

---

At this point, you have a VM running **Ubuntu 24.04** with **Nvidia GPU** attached.

---

## Run vLLM

Start vLLM with a vision-capable model:

```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e 'sbnb_vllm_args="--model Qwen/Qwen2.5-VL-3B-Instruct --tensor-parallel-size 2 --max-model-len 2048 --dtype bfloat16 --limit-mm-per-prompt image=5,video=5"'
```

> We use `--tensor-parallel-size 2` for 2 GPUs, and choose a small model to fit into 24GB total GPU RAM.

For full options, see [vLLM Engine Args](https://docs.vllm.ai/en/latest/serving/engine_args.html).

vLLM is now up and running!

---

## Run RAGFlow

```bash
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-ragflow.yml
```

RAGFlow is up!

---

## Configure RAGFlow

### 1. Access the Web UI

Open a browser and navigate to the **VM hostname** from Tailscale, using port `80`. Example URL:

```
http://sbnb-vm-67f97659333f/
```

Create an admin account.

---

### 2. Add vLLM as a Model Provider

Go to: `Settings → Model Providers → Add Model`

Fill in:

- **Model type:** `chat`
- **Model name:** `Qwen/Qwen2.5-VL-3B-Instruct`
- **Base URL:** `http://YOUR_VM_HOSTNAME:8000/v1`
- **API Key:** _(leave empty)_
- **Max Tokens:** `1500`
- **Supports Vision:** `Yes`

![rag-add-vllm](images/rag-add-vllm.png)

---

### 3. Set System Models

Go to: `Settings → System Model Settings`
Select:

- **Chat model:** `Qwen/Qwen2.5-VL-3B-Instruct`
- **Embedding model:** `BAAI/bge-large-zh-v1.5`
- **Img2txt model:** `Qwen/Qwen2.5-VL-3B-Instruct`

![rag-model-settings](images/rag-model-settings.png)

---

### 4. Create a Knowledge Base

Navigate to `Knowledge Base → Create`, give it a name.

---

### 5. Upload Documents

For demo purposes, download the latest 2024 US government financial report (latest at the time of writing this tutorial):
[executive-summary-2024.pdf](https://www.fiscal.treasury.gov/files/reports-statements/financial-report/2024/executive-summary-2024.pdf)

Then:

- Click `Add File → Local files`
- Enable `Parse on creation`
- Upload the PDF

Wait for **Parsing Status** to turn **SUCCESS**.

![rag-upload-file](images/rag-upload-file.png)

---

### 6. Create an Assistant

- Go to `Chat → Create an Assistant`
- Set an **Assistant name**
- Choose your **Knowledge Base**

![rag-create-assistant](images/rag-create-assistant.png)

---

### 7. Start Chatting

Select your assistant and click `+` to start a chat.

Try this question (answer is only in the PDF):

> "How much tax was collected in the US in 2024?"

RAGFlow responds with the answer **"... 5.0 trillion for FY 2024"** and cites the PDF source.

![rag-ask-question](images/rag-ask-question.png)

---

## Stopping Services

To stop RAGFlow:
```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-ragflow.yml \
  -e sbnb_ragflow_state=absent
```

To stop vLLM:
```sh
ansible-playbook -i $VM_HOST, \
  collections/ansible_collections/sbnb/compute/playbooks/run-vllm.yml \
  -e sbnb_vllm_state=absent
```

---

That's it! You've successfully combined a stock LLM with your own custom knowledge base.

Happy experimenting - and solving real-world problems!
