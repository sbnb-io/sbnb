docker run -it --gpus all \
    -v /tmp:/tmp \
    -v /root/.cache:/root/.cache \
    -e HF_TOKEN=hf_REPLACE_ME \
    sbnb/perceptionlm \
    conda run -n perception_models --no-capture-output \
    python3 /perception_models/apps/plm/generate.py \
    --ckpt facebook/Perception-LM-1B --media_type video\
    --question 'What is happening in the video?' \
    --media_path  /tmp/demo.mp4
