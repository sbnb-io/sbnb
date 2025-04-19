docker run -it --gpus all \
    -v /tmp:/tmp \
    -v /root/.cache:/root/.cache \
    sbnb/perceptionlm \
    conda run -n perception_models --no-capture-output \
    python3 /perception_models/apps/plm/generate.py \
    --ckpt facebook/Perception-LM-1B --media_type video\
    --question 'Describe the bar plot in the video with most details possible.' \
    --media_path  /tmp/demo.mp4
