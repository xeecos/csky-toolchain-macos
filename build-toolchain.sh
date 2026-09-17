#!/usr/bin/env bash
# 在 macOS (Apple Silicon / Intel) 上原生构建 csky-elfabiv2 裸机交叉工具链
#   binutils + GCC + newlib/libgloss，用于 C-SKY CK80x 内核（如 TXW828 的 CK804DF/CK804D）
#
# 用法: ./build-toolchain.sh
# 可用环境变量覆盖:
#   GCC_VERSION      默认 15.3.0
#   BINUTILS_VERSION 默认 2.46.1
#   NEWLIB_VERSION   默认 4.6.0.20260123
#   LANGUAGES        默认 "c,c++"（SDK 为纯 C 时可设为 "c" 加快构建）
#   TRIM_MULTILIB    默认 1：只保留 ck803 系列 multilib（TXW828 用；设 0 保留全部，但 ck807/810/860
#                    变体编译 libgcc 有单文件数小时的已知病理，且需 >16GB 内存才不会被换页拖死）
#   CSKY_ROOT        默认 ~/csky-toolchain（源码/构建目录/安装目录都在此之下）
#   JOBS             默认 CPU 核数；16GB 内存的机器建议 JOBS=4
set -euo pipefail

GCC_VERSION="${GCC_VERSION:-15.3.0}"
BINUTILS_VERSION="${BINUTILS_VERSION:-2.46.1}"
NEWLIB_VERSION="${NEWLIB_VERSION:-4.6.0.20260123}"
LANGUAGES="${LANGUAGES:-c,c++}"
CSKY_ROOT="${CSKY_ROOT:-$HOME/csky-toolchain}"
# 默认并行度: min(CPU核数, 4)。libgcc 部分文件单任务内存峰值可达 2GB+，
# 并行过高会在 16GB 内存的机器上触发换页风暴（表现为"卡死"）
JOBS="${JOBS:-$(( $(sysctl -n hw.ncpu) < 4 ? $(sysctl -n hw.ncpu) : 4 ))}"

TARGET=csky-elfabiv2
SRC="$CSKY_ROOT/src"
BUILD="$CSKY_ROOT/build"
PREFIX="$CSKY_ROOT/install"
BREW="$(brew --prefix)"

MIRRORS=(
  "https://mirrors.tuna.tsinghua.edu.cn/gnu"
  "https://mirrors.aliyun.com/gnu"
  "https://ftp.gnu.org/gnu"
)

log()  { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

command -v brew  >/dev/null || die "未找到 brew，请先运行 ./install-deps.sh"
command -v clang >/dev/null || die "未找到 clang（需要 Xcode Command Line Tools）"
GMAKE="$(command -v gmake || command -v make)"

# 固定用 Xcode 的 clang 作为宿主编译器：
# PATH 中若存在 Homebrew gcc（真 GNU gcc）会被 configure 优先选中，
# 它不经过 xcrun、找不到 macOS SDK，链接报 "ld: library 'System' not found"
export CC=/usr/bin/cc
export CXX=/usr/bin/c++
export SDKROOT="$(xcrun --show-sdk-path)"

mkdir -p "$SRC" "$BUILD" "$PREFIX"
exec > >(tee -a "$CSKY_ROOT/build.log") 2>&1

log "GCC=$GCC_VERSION  binutils=$BINUTILS_VERSION  newlib=$NEWLIB_VERSION  target=$TARGET  jobs=$JOBS  prefix=$PREFIX"

# ---------- 0. 获取源码 ----------
NEWLIB_MIRRORS=(
  "https://sourceware.org/pub/newlib"
  "https://mirrors.kernel.org/sourceware/newlib"
)

fetch() { # fetch <镜像列表> <子目录> <文件名>
  local mirrors=$1 dir=$2 file=$3 m
  local -a urls=("${!mirrors}")
  [ -s "$SRC/$file" ] && { log "已存在 ${file}，跳过下载"; return 0; }
  for m in "${urls[@]}"; do
    log "下载 $m/$dir/$file"
    if curl -fL --retry 3 --connect-timeout 20 -o "$SRC/$file.part" "$m/$dir/$file"; then
      mv "$SRC/$file.part" "$SRC/$file"; return 0
    fi
  done
  die "下载失败: $file"
}

fetch MIRRORS[@] "gcc/gcc-$GCC_VERSION" "gcc-$GCC_VERSION.tar.xz"
fetch MIRRORS[@] "binutils" "binutils-$BINUTILS_VERSION.tar.xz"
fetch NEWLIB_MIRRORS[@] "" "newlib-$NEWLIB_VERSION.tar.gz"

[ -d "$SRC/gcc-$GCC_VERSION" ]     || tar -xJf "$SRC/gcc-$GCC_VERSION.tar.xz" -C "$SRC"
[ -d "$SRC/binutils-$BINUTILS_VERSION" ] || tar -xJf "$SRC/binutils-$BINUTILS_VERSION.tar.xz" -C "$SRC"
NEWLIB_SRC="$SRC/newlib-$NEWLIB_VERSION"
if [ ! -d "$NEWLIB_SRC" ]; then
  mkdir -p "$NEWLIB_SRC" && tar -xzf "$SRC/newlib-$NEWLIB_VERSION.tar.gz" -C "$NEWLIB_SRC" --strip-components=1
fi

GCC_SRC="$SRC/gcc-$GCC_VERSION"
# 把 newlib/libgloss 放入 GCC 顶层源码树，构建时作为目标库一起编译
[ -d "$GCC_SRC/newlib" ]   || cp -R "$NEWLIB_SRC/newlib"   "$GCC_SRC/"
[ -d "$GCC_SRC/libgloss" ] || cp -R "$NEWLIB_SRC/libgloss" "$GCC_SRC/"

# ---------- 0.5 裁剪 multilib ----------
# 原版 t-csky-elf 定义 ck801/802/803/807/810/860 × 大小端 × 3 种浮点 ABI 共 ~44 个变体；
# 其中 ck807/810/860 变体编译 libgcc/unwind-dw2-fde.c 存在严重耗时/内存病理（单文件 >1h、2GB+）。
# TXW828 的 CK804DF 兼容 ck803 基线（主线 GCC 无 ck804/ck805），只保留 ck803 系列 + 3 种浮点 ABI。
# 需要完整 multilib 时: TRIM_MULTILIB=0 ./build-toolchain.sh
TCSKY_ELF="$GCC_SRC/gcc/config/csky/t-csky-elf"
if [ "${TRIM_MULTILIB:-1}" = "1" ] && ! grep -q 'TXW828-trim' "$TCSKY_ELF"; then
  [ -f "$TCSKY_ELF.full" ] || cp -f "$TCSKY_ELF" "$TCSKY_ELF.full"
  cat > "$TCSKY_ELF" <<'EOF'
# Multilib configuration for csky*-elf -- TXW828-trim
# Trimmed for TXW828 (CK804DF/CK804D, little-endian): ck803 baseline + float ABIs.
# Full version preserved in t-csky-elf.full (restore it and reconfigure for all cores).

MULTILIB_OPTIONS    = mcpu=ck803f
MULTILIB_DIRNAMES   = ck803
MULTILIB_MATCHES    =
MULTILIB_EXCEPTIONS =

MULTILIB_OPTIONS    += mfloat-abi=soft/mfloat-abi=softfp/mfloat-abi=hard
MULTILIB_DIRNAMES   += soft soft-fp hard-fp

# ck803 / ck803s 系列 CPU 全部映射到 ck803f 变体
MULTILIB_MATCHES    += mcpu?ck803f=march?ck803
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803fh
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803h
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803t
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ht
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803e
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803eh
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803et
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803eht
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ef
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803efh
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ft
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803eft
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803efht
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803r1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803fr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803fhr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803hr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803tr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803htr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803er1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ehr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803etr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ehtr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803efr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803efhr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803ftr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803eftr1
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803efhtr1
MULTILIB_MATCHES    += mcpu?ck803f=march?ck803s
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803s
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803st
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803se
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803sf
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803sef
MULTILIB_MATCHES    += mcpu?ck803f=mcpu?ck803seft
EOF
  log "multilib 已裁剪为 ck803 系列（原始文件备份为 t-csky-elf.full）"
fi

# ---------- 1. binutils ----------
if [ ! -x "$PREFIX/bin/$TARGET-as" ]; then
  log "构建 binutils $BINUTILS_VERSION"
  mkdir -p "$BUILD/binutils" && cd "$BUILD/binutils"
  "$SRC/binutils-$BINUTILS_VERSION/configure" \
    --target="$TARGET" \
    --prefix="$PREFIX" \
    --disable-nls \
    --disable-werror \
    --with-system-zlib
  "$GMAKE" -j"$JOBS"
  "$GMAKE" install
else
  log "binutils 已安装，跳过"
fi

export PATH="$PREFIX/bin:/opt/homebrew/opt/texinfo/bin:$PATH"

# ---------- 2. GCC + newlib ----------
if [ ! -x "$PREFIX/bin/$TARGET-gcc" ]; then
  log "构建 GCC $GCC_VERSION (languages: $LANGUAGES)"
  mkdir -p "$BUILD/gcc" && cd "$BUILD/gcc"
  "$GCC_SRC/configure" \
    --target="$TARGET" \
    --prefix="$PREFIX" \
    --with-newlib \
    --enable-languages="$LANGUAGES" \
    --enable-static \
    --disable-shared \
    --disable-threads \
    --disable-libgomp \
    --disable-libssp \
    --disable-libquadmath \
    --disable-libada \
    --disable-bootstrap \
    --disable-nls \
    --with-system-zlib \
    --with-gmp="$BREW/opt/gmp" \
    --with-mpfr="$BREW/opt/mpfr" \
    --with-mpc="$BREW/opt/libmpc"
  "$GMAKE" -j"$JOBS"
  "$GMAKE" install
else
  log "GCC 已安装，跳过"
fi

log "工具链安装完成: $PREFIX/bin/$TARGET-gcc"

# GCC driver 以裸命令名调用 as/ld，若不加处理会解析到 Xcode clang 的 as（不认识 C-SKY）。
# 按发行版惯例在 gcc 的 libexec 目录放指向交叉 as/ld 的符号链接（Debian 等即如此）。
GCC_LIBDIR="$PREFIX/lib/gcc/$TARGET/$GCC_VERSION"
ln -sf "../../../../bin/$TARGET-as" "$GCC_LIBDIR/as"
ln -sf "../../../../bin/$TARGET-ld" "$GCC_LIBDIR/ld"

log "验证版本:"
"$PREFIX/bin/$TARGET-gcc" --version | head -1
"$PREFIX/bin/$TARGET-as"  --version | head -1
"$PREFIX/bin/$TARGET-ld"  --version | head -1
log "下一步: source ./env.sh && cd test && make"
