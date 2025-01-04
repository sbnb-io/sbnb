### Starting an Ubuntu Virtual Machine (VM) on Sbnb Linux

1. **Initialize the Development Environment**
    
    SSH into the sbnb Linux machine and start the development environment by executing:
    
    ```
    sbnb-dev-env.sh
    
    ```
    
2. **Prepare the Cloud-init Configuration**
    
    Create a `user-data` file for cloud-init. Replace `tskey-auth-KEY` with your actual Tailscale key.
    
    ```
    cat > user-data << EOF
    #cloud-config
    runcmd:
      - ['sh', '-c', 'curl -fsSL https://tailscale.com/install.sh | sh']
      - ['tailscale', 'up', '--ssh', '--auth-key=tskey-auth-KEY']
    EOF
    ```
    
    Next, create an empty `meta-data` file:
    
    ```
    touch meta-data
    
    ```
    
3. **Generate a Configuration ISO**
    
    Use the following command to create a `seed.iso` file that will serve as the VM's configuration source:
    
    ```
    genisoimage -output seed.iso -volid cidata -joliet -rock user-data meta-data
    
    ```
    
4. **Download and Prepare the Ubuntu VM Image**
    
    Download the Ubuntu 24.04 cloud image and create a working copy for the VM instance:
    
    ```
    wget "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    cp noble-server-cloudimg-amd64.img vm-instance.img
    
    ```
    
5. **Launch the Virtual Machine**
    
    Start the VM using the downloaded image and generated ISO:
    
    ```
    qemu-system-x86_64 -accel kvm -hda vm-instance.img -cdrom seed.iso -nographic -m 1G -smp 2 -cpu host
    
    ```
    
    *You can adjust the memory (`-m`) and vCPU count (`-smp`) to suit your needs.*
    
6. **Verify VM in Tailscale**
    
    Once the VM boots, it should appear as a new machine in your Tailscale device list. You can then SSH into it directly.
