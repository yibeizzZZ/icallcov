FROM ubuntu:22.04 AS llvm
ARG DEBIAN_FRONTEND=noninteractive
ARG JOBS=8
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake make gcc-10 g++-10 python3 binutils \
    zlib1g-dev libtinfo-dev libzstd-dev && rm -rf /var/lib/apt/lists/*
RUN git init /opt/llvm-project && cd /opt/llvm-project && \
    git remote add origin https://github.com/llvm/llvm-project.git && \
    git fetch --depth 1 origin e758b77161a70d7e7260d8f52bf161a89d73af8a && \
    git checkout --detach FETCH_HEAD
RUN cmake -S /opt/llvm-project/llvm -B /opt/llvm-project/build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc-10 \
    -DCMAKE_CXX_COMPILER=g++-10 -DLLVM_TARGETS_TO_BUILD="X86;ARM;AArch64" \
    -DLLVM_ENABLE_PROJECTS=clang -G "Unix Makefiles" && \
    cmake --build /opt/llvm-project/build -j${JOBS} && \
    cmake --install /opt/llvm-project/build --prefix /opt/llvm15-typedive

FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
ARG JOBS=8
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake make gcc-10 g++-10 python3 binutils \
    zlib1g-dev libtinfo-dev libzstd-dev && rm -rf /var/lib/apt/lists/*
COPY --from=llvm /opt/llvm15-typedive /opt/llvm15-typedive
RUN git init /opt/typedive && cd /opt/typedive && \
    git remote add origin https://github.com/umnsec/mlta.git && \
    git fetch --depth 1 origin 1f2b4b7babb3308710940573efacfe78a53c9f6b && \
    git checkout --detach FETCH_HEAD
COPY typedive.patch /opt/typedive/typedive.patch
RUN cd /opt/typedive && git apply --check typedive.patch && git apply typedive.patch && \
    cmake -S src -B build -DLLVM_DIR=/opt/llvm15-typedive/lib/cmake/llvm \
    -DCMAKE_C_COMPILER=/opt/llvm15-typedive/bin/clang \
    -DCMAKE_CXX_COMPILER=/opt/llvm15-typedive/bin/clang++ \
    -DCMAKE_BUILD_TYPE=Build -DCMAKE_CXX_FLAGS_BUILD="-std=c++14 -fpic -fno-rtti -g" && \
    cmake --build build --target kanalyzer -j${JOBS}
# These wrappers also work as CMake compilers for a same-configuration binary.
RUN mkdir -p /opt/typedive/bin && \
    printf '#!/bin/sh\nexec /opt/llvm15-typedive/bin/clang -Xclang -no-opaque-pointers "$@" -g\n' > /opt/typedive/bin/clang && \
    printf '#!/bin/sh\nexec /opt/llvm15-typedive/bin/clang++ -Xclang -no-opaque-pointers "$@" -g\n' > /opt/typedive/bin/clang++ && \
    chmod +x /opt/typedive/bin/clang /opt/typedive/bin/clang++ && \
    printf 'LLVM=e758b77161a70d7e7260d8f52bf161a89d73af8a\nTypeDive=1f2b4b7babb3308710940573efacfe78a53c9f6b\n' > /opt/typedive/versions.txt
ENV PATH="/opt/typedive/bin:/opt/llvm15-typedive/bin:${PATH}"
CMD ["/bin/bash"]
