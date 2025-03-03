FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y \
        git python3-pip python3-virtualenv cuda-nvcc-12-8 \
        && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN virtualenv /wan && . /wan/bin/activate \
    && pip3 install --no-cache-dir torch torchvision setuptools wheel huggingface_hub[cli]

RUN git clone https://github.com/Wan-Video/Wan2.1 \
    && . /wan/bin/activate && cd Wan2.1 && pip3 install --no-cache-dir -r requirements.txt
