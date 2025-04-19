HF_TOKEN=hf_REPLACE_ME
QUESTION='What is happening in the video?'
MEDIA='/tmp/demo.mp4'

docker run -it --gpus all \
    -v /tmp:/tmp \
    -v /root/.cache:/root/.cache \
    -e HF_TOKEN="${HF_TOKEN}" \
    sbnb/perceptionlm \
    conda run -n perception_models --no-capture-output \
    python3 /perception_models/apps/plm/generate.py \
    --ckpt facebook/Perception-LM-1B --media_type video\
    --question "${QUESTION}" \
    --media_path "${MEDIA}"
