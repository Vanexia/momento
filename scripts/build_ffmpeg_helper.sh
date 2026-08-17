#!/usr/bin/env bash
set -euo pipefail

FFMPEG_VERSION="8.1.2"
FFMPEG_SHA256="464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
SOURCE_DATE_EPOCH="1781667960"
HELPER_REVISION="1"
SOURCE_URL="https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"

repo_root="$(pwd -P)"
work_root="$(realpath -m "${1:-$repo_root/build/ffmpeg-helper}")"
output_dir="$(realpath -m "${2:-$repo_root/dist/ffmpeg-helper}")"
archive="$work_root/ffmpeg-${FFMPEG_VERSION}.tar.xz"
source_dir="$work_root/ffmpeg-${FFMPEG_VERSION}"

case "$work_root" in
  "$repo_root"/build/*|"$repo_root"/tmp/*) ;;
  *) echo "Work directory must be under build/ or tmp/: $work_root" >&2; exit 2 ;;
esac
case "$output_dir" in
  "$repo_root"/dist/*|"$repo_root"/tmp/*) ;;
  *) echo "Output directory must be under dist/ or tmp/: $output_dir" >&2; exit 2 ;;
esac
if [[ "$work_root" == "$output_dir" ]]; then
  echo "Work and output directories must differ" >&2
  exit 2
fi

rm -rf -- "$work_root" "$output_dir"
mkdir -p -- "$work_root" "$output_dir"
curl --fail --location --proto '=https' --tlsv1.2 "$SOURCE_URL" --output "$archive"
echo "$FFMPEG_SHA256  $archive" | sha256sum --check --strict
tar -xf "$archive" -C "$work_root"

export SOURCE_DATE_EPOCH
cd "$source_dir"
./configure \
  --prefix=/opt/momento-ffmpeg-helper \
  --arch=x86_64 \
  --target-os=mingw32 \
  --enable-static \
  --disable-shared \
  --disable-autodetect \
  --disable-network \
  --disable-doc \
  --disable-debug \
  --disable-ffplay \
  --disable-avdevice \
  --disable-swresample \
  --disable-iconv \
  --disable-zlib \
  --disable-bzlib \
  --disable-lzma \
  --disable-encoders \
  --enable-encoder=mjpeg \
  --disable-decoders \
  --enable-decoder=h264 \
  --disable-demuxers \
  --enable-demuxer=matroska,mov \
  --disable-muxers \
  --enable-muxer=matroska,mov,mp4,image2 \
  --disable-protocols \
  --enable-protocol=file \
  --disable-filters \
  --enable-filter=thumbnail,scale,format \
  --disable-bsfs \
  --enable-bsf=aac_adtstoasc,extract_extradata \
  --disable-parsers \
  --enable-parser=aac,h264,mjpeg \
  --extra-ldflags=-static \
  --extra-ldflags=-Wl,--no-insert-timestamp \
  --extra-version="momento-helper-${HELPER_REVISION}"

make -j"$(nproc)"
strip -s ffmpeg.exe ffprobe.exe
cp ffmpeg.exe ffprobe.exe COPYING.LGPLv2.1 "$output_dir"
mv "$output_dir/COPYING.LGPLv2.1" "$output_dir/LICENSE.txt"

{
  echo "Momento minimal FFmpeg helper"
  echo
  echo "Source: $SOURCE_URL"
  echo "Source SHA-256: $FFMPEG_SHA256"
  echo "Helper revision: $HELPER_REVISION"
  echo "SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH"
  echo
  gcc --version | head -n 1
  nasm -v
  echo
  "$output_dir/ffmpeg.exe" -hide_banner -version
} > "$output_dir/README.txt"

(
  cd "$output_dir"
  sha256sum ffmpeg.exe ffprobe.exe LICENSE.txt README.txt > SHA256SUMS.txt
)
