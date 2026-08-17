#!/usr/bin/env bash
set -euo pipefail
trap 'status=$?; echo "ERROR: native build failed at line $LINENO: $BASH_COMMAND" >&2; exit $status' ERR

if [[ $# -ne 5 ]]; then
    echo "usage: build_pyav_runtime.sh SOURCE_CACHE WORK_ROOT PREFIX CONFIG_ARGS REPO_ROOT" >&2
    exit 2
fi

source_cache=$1
work_root=$2
prefix=$3
config_args_file=$4
repo_root=$5

for required in "$source_cache" "$config_args_file" "$repo_root"; do
    if [[ ! -e "$required" ]]; then
        echo "missing build input: $required" >&2
        exit 2
    fi
done
if [[ -e "$work_root" || -e "$prefix" ]]; then
    echo "native work and prefix paths must not already exist" >&2
    exit 2
fi

export LC_ALL=C
export LANG=C
export TZ=UTC
export SOURCE_DATE_EPOCH=1767139200
export ZERO_AR_DATE=1
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
export PATH="$prefix/bin:/ucrt64/bin:/usr/bin:$PATH"
unset CL _CL_ LINK _LINK_ INCLUDE LIB LIBPATH

jobs=${MOMENTO_BUILD_JOBS:-$(nproc)}
mkdir -p "$work_root/src" "$work_root/build" "$prefix"

extract_source() {
    local archive=$1
    local destination=$2
    mkdir -p "$destination"
    tar -xf "$source_cache/$archive" -C "$destination" --strip-components=1
}

extract_source "x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.bz2" "$work_root/src/x264"
extract_source "nv-codec-headers-13.0.19.0.tar.gz" "$work_root/src/nv-codec-headers"
extract_source "AMF-headers-v1.5.0.tar.gz" "$work_root/src/amf-headers"
extract_source "libvpl-2.16.0.tar.gz" "$work_root/src/libvpl"
extract_source "ffmpeg-8.0.1.tar.xz" "$work_root/src/ffmpeg"

common_cflags="-O3 -ffile-prefix-map=$work_root=/momento-pyav-build -fdebug-prefix-map=$work_root=/momento-pyav-build"
common_ldflags="-Wl,--no-insert-timestamp"
export CFLAGS="$common_cflags"
export CXXFLAGS="$common_cflags"
export LDFLAGS="$common_ldflags"

pushd "$work_root/src/nv-codec-headers" >/dev/null
echo "Building NVIDIA codec headers"
make PREFIX="$prefix" install
popd >/dev/null

mkdir -p "$prefix/include"
echo "Installing AMD AMF headers"
cp -R "$work_root/src/amf-headers/AMF" "$prefix/include/AMF"

echo "Building Intel oneVPL"
cmake \
    -S "$work_root/src/libvpl" \
    -B "$work_root/build/libvpl" \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=ON \
    -DINSTALL_LIB=ON \
    -DINSTALL_DEV=ON \
    -DINSTALL_EXAMPLES=OFF \
    -DBUILD_EXPERIMENTAL=OFF \
    -DBUILD_TESTS=OFF \
    -DBUILD_EXAMPLES=OFF \
    -DCMAKE_C_FLAGS_RELEASE="$common_cflags -DNDEBUG" \
    -DCMAKE_CXX_FLAGS_RELEASE="$common_cflags -DNDEBUG" \
    -DCMAKE_SHARED_LINKER_FLAGS="$common_ldflags"
cmake --build "$work_root/build/libvpl" --parallel "$jobs"
cmake --install "$work_root/build/libvpl"

pushd "$work_root/src/x264" >/dev/null
echo "Building x264"
./configure \
    --prefix="$prefix" \
    --host=x86_64-w64-mingw32 \
    --enable-shared \
    --disable-cli \
    --disable-lsmash \
    --disable-swscale \
    --disable-ffms \
    --disable-opencl \
    --enable-strip \
    --extra-cflags="$common_cflags" \
    --extra-ldflags="$common_ldflags"
if ! make -j"$jobs" >"$work_root/build/x264.log" 2>&1; then
    tail -n 200 "$work_root/build/x264.log" >&2
    exit 1
fi
make install
popd >/dev/null

ffmpeg_configure=()
while IFS= read -r argument; do
    ffmpeg_configure+=("${argument%$'\r'}")
done < "$config_args_file"
pushd "$work_root/src/ffmpeg" >/dev/null
echo "Configuring FFmpeg"
if ! ./configure \
    --prefix="$prefix" \
    "${ffmpeg_configure[@]}" \
    --extra-cflags="-I$prefix/include $common_cflags" \
    --extra-ldflags="-L$prefix/lib $common_ldflags" \
    >"$work_root/build/ffmpeg-configure.log" 2>&1; then
    cat "$work_root/build/ffmpeg-configure.log" >&2
    exit 1
fi
echo "Building FFmpeg"
if ! make -j"$jobs" >"$work_root/build/ffmpeg.log" 2>&1; then
    tail -n 200 "$work_root/build/ffmpeg.log" >&2
    exit 1
fi
make install
popd >/dev/null

for library in avcodec avdevice avfilter avformat avutil swresample swscale; do
    if [[ -f "$prefix/bin/$library.lib" ]]; then
        mv "$prefix/bin/$library.lib" "$prefix/lib/$library.lib"
    fi
done

for runtime in libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll; do
    cp "/ucrt64/bin/$runtime" "$prefix/bin/$runtime"
done

mkdir -p "$prefix/licenses"
cp "$work_root/src/ffmpeg/COPYING.GPLv3" "$prefix/licenses/FFmpeg-COPYING.GPLv3.txt"
cp "$work_root/src/x264/COPYING" "$prefix/licenses/x264-COPYING.txt"
cp "$work_root/src/libvpl/LICENSE" "$prefix/licenses/oneVPL-LICENSE.txt"
cp "/ucrt64/share/licenses/gcc-libs/COPYING.LIB" "$prefix/licenses/GCC-COPYING.LIB.txt"
cp "/ucrt64/share/licenses/gcc-libs/COPYING.RUNTIME" "$prefix/licenses/GCC-COPYING.RUNTIME.txt"
cp "/ucrt64/share/licenses/libwinpthread/COPYING" "$prefix/licenses/winpthreads-COPYING.txt"

echo "PASS: minimal FFmpeg prefix built at $prefix"
