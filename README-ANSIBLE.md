# Automating Sbnb Linux Setup with Ansible

Days when system administrators manually installed Linux OS and configured services are gone. This was a labor-intensive and error-prone approach.
Nowadays, sysadmins and DevOps engineers automate all the steps. Configurations are now stored as files and then applied to the servers. This approach is called Infrastructure as Code (IaC) or sometimes Infrastructure as Data (IaD).

In this tutorial, we will configure a bare metal server booted into Sbnb Linux using the Ansible automation tool.

## Steps

### 1. Boot Bare Metal Server into Sbnb Linux
Confirm that the Sbnb Linux instance shows up in the Tailscale machine list. The machine has a unique hostname based on the machine serial number (see more on Sbnb Linux hostnames at [README-SERIAL-NUMBER.md](README-SERIAL-NUMBER.md)).

### 2. Connect Your Laptop to Tailscale
We will use a MacBook in this tutorial, but any machine, such as a Linux instance, should work the same.

### 3. Download Tailscale Dynamic Inventory Script
```sh
curl https://raw.githubusercontent.com/m4wh6k/ansible-tailscale-inventory/refs/heads/main/ansible_tailscale_inventory.py -O
chmod +x ansible_tailscale_inventory.py
```

### 4. Create a Simple Ansible Playbook
Create a file `docker.yaml`:

```yaml
---
- hosts: sbnb-F6S0R8000719
  gather_facts: false
  tasks:
    - ping:
    - name: Start a container with a command
      docker_container:
        name: sleepy
        image: ubuntu:24.04
        command: ["sleep", "infinity"]
```

Replace `hosts: sbnb-F6S0R8000719` with your host.
This Ansible Playbook will start an `ubuntu:24.04` container with an infinite sleep inside. This is only for demonstration purposes.

### 5. Apply Ansible Playbook to the Real Server
```sh
ansible-playbook -i ./ansible_tailscale_inventory.py docker.yml
```

A successful output should look like this:

```
# ansible-playbook -i ./ansible_tailscale_inventory.py docker.yml 

PLAY [sbnb-F6S0R8000719] **********************************************************************************************************************

TASK [ping] ***********************************************************************************************************************************
ok: [sbnb-F6S0R8000719]

TASK [Start a container with a command] *******************************************************************************************************
changed: [sbnb-F6S0R8000719]

PLAY RECAP ************************************************************************************************************************************
sbnb-F6S0R8000719          : ok=2    changed=1    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0   
```

### 6. Control the Docker Container Started on the Bare Metal
SSH into the Sbnb Linux instance and validate that the container with the name `sleepy` is running:
```sh
# docker ps
CONTAINER ID   IMAGE          COMMAND            CREATED         STATUS              PORTS     NAMES
54f3e9ffe50a   ubuntu:24.04   "sleep infinity"   5 minutes ago   Up About a minute             sleepy
```

### Congratulations!
You just customized a Sbnb Linux instance using an automated Infrastructure as Code (IaC) approach!
Now you can commit `docker.yaml` into a Git repository and extend it as needed!
