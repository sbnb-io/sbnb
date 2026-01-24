FROM debian:sid AS build

# This Dockerfile automates the process of building SVSM and its dependent
# packages with support for AMD SEV-SNP confidential computing. The steps are
# based on the official SVSM documentation, which can be found at:
# https://github.com/coconut-svsm/svsm/blob/main/Documentation/docs/installation/INSTALL.md

# We use a multistep Docker build process to minimize the size of the final
# image. In this approach, the software is compiled in a temporary build
# container, and only the resulting binary artifacts are copied into the final
# container that end customers will pull.

# Install packages and src
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get build-dep -y qemu

RUN apt-get install -y python3-venv python3-tomli libcunit1-dev cbindgen git curl

# Install rust and cargo tools
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y \
    && . "$HOME/.cargo/env" \
    && cargo install bindgen-cli \
    && cargo install cargo-c

# Build igvm
RUN git clone --depth 1 https://github.com/microsoft/igvm \
    && cd igvm \
    && . "$HOME/.cargo/env" \
    && make -f igvm_c/Makefile \
    && make -f igvm_c/Makefile install

# Build qemu
# Using svsm-igvm branch HEAD (igvm patches are at branch tip)
RUN git clone -b svsm-igvm --depth 1 https://github.com/coconut-svsm/qemu \
    && cd qemu \
    && export PKG_CONFIG_PATH=/usr/lib/x86_64-linux-gnu/pkgconfig \
    && ./configure --prefix=/usr/qemu-svsm/ --target-list=x86_64-softmmu --enable-igvm --disable-docs \
    --disable-xen --disable-sndio \
    && ninja -C build/ \
    && make install

# Build guest OVMF firmware in a separate bookworm-based stage to avoid toolchain issues
FROM debian:bookworm AS ovmf-build
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get build-dep -y ovmf \
    && apt-get install -y git
RUN git clone -b svsm --depth 1 https://github.com/coconut-svsm/edk2.git \
    && cd edk2 \
    && git submodule init \
    && git submodule update \
    && export PYTHON3_ENABLE=TRUE \
    && export PYTHON_COMMAND=python3 \
    && make -j16 -C BaseTools/ \
    && . ./edksetup.sh --reconfig \
    && build -a X64 -b RELEASE -t GCC5 -DTPM2_ENABLE -p OvmfPkg/OvmfPkgX64.dsc

# Continue main build - copy OVMF from bookworm build
FROM build AS build-with-ovmf
COPY --from=ovmf-build /edk2/Build/OvmfX64/RELEASE_GCC5/FV/OVMF.fd /usr/qemu-svsm/OVMF.fd

# Build svsm
# Using main branch HEAD
RUN git clone --depth 1 https://github.com/coconut-svsm/svsm \
    && apt-get install -y autoconf-archive pkg-config m4 libtool automake autoconf libcunit1-dev libclang-dev cmake \
    && cd svsm \
    && . "$HOME/.cargo/env" \
    && git submodule update --init \
    && cargo install bindgen-cli \
    && FW_FILE=/usr/qemu-svsm/OVMF.fd ./build --release configs/qemu-target.json

# Prepare the final container with binary artifacts
FROM debian:sid
RUN apt-get update && apt-get install -y qemu-system-x86 genisoimage curl
COPY --from=build-with-ovmf /usr/qemu-svsm /usr/qemu-svsm
COPY --from=build-with-ovmf /svsm/bin/coconut-qemu.igvm /usr/qemu-svsm/coconut-qemu.igvm
COPY --from=build-with-ovmf /usr/lib/x86_64-linux-gnu/libigvm* /usr/lib/x86_64-linux-gnu/
RUN ldconfig
