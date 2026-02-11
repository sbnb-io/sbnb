FROM ubuntu:24.04

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install QEMU and required utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 \
    qemu-utils \
    ovmf \
    genisoimage \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create bridge config directory
RUN mkdir -p /etc/qemu

# Set default command
CMD ["/bin/bash"]
