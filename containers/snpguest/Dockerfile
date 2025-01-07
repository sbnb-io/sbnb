FROM debian:sid AS build

RUN apt-get update && apt-get install -y curl git build-essential \
    && curl https://sh.rustup.rs -sSf | sh -s -- -y \
    && . "$HOME/.cargo/env" \
    && git clone https://github.com/virtee/snpguest.git \
    && cd snpguest/ \
    && cargo build --release

# Prepare the final container with binary artifacts
FROM debian:sid
RUN apt-get update && apt-get install -y ca-certificates
COPY --from=build /snpguest/target/release/snpguest /sbin/snpguest
COPY verify-attestation-report.sh /sbin/verify-attestation-report.sh
