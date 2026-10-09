#!/usr/bin/env bash
# The title (app/dist/PPSA99220/eboot.bin) from source, step by step.
#
#   bash tools/build-all.sh [step...]     steps: vulkan sdk radv tls wine-tools wine-configure wine title
#                                         (no step: all of them, in that order)
#
# Expects vendor/ as tools/bootstrap.py leaves it: wine at the pinned revision,
# PS5_Vulkan, PS5_Mesa and PS5_PayloadSDK beside each other. Reproduced from a
# fresh clone on Ubuntu 24.04 (WSL) with clang 18, Meson 1.9 and LLVM 19.
#
# Environment:
#   MESON        a Meson of at least 1.4 (Mesa requires it; Ubuntu 24.04 has 1.3)
#   LLVM_CONFIG  llvm-config of the LLVM that libclc and the SPIR-V translator were
#                built for (Mesa's mesa_clc host tool); Meson would otherwise take
#                the newest llvm-config it finds, installed in full or not
#   PS5_CLANG    the host clang the SDK's compiler wrappers drive (default: clang)
#   JOBS         parallel jobs (default: nproc)
# Logs: build/logs/<step>.log
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
log=$root/build/logs
mkdir -p "$log"
export PS5_VULKAN_DIR=${PS5_VULKAN_DIR:-$root/vendor/PS5_Vulkan}
export PS5_MESA_FORK=${PS5_MESA_FORK:-$root/vendor/PS5_Mesa}
export PS5_PAYLOAD_SDK_FORK=${PS5_PAYLOAD_SDK_FORK:-$root/vendor/PS5_PayloadSDK}
export PS5_CLANG=${PS5_CLANG:-$(command -v clang)}
export JOBS=${JOBS:-$(nproc)}
export MESON=${MESON:-$(command -v meson)}
LLVM_CONFIG=${LLVM_CONFIG:-$(command -v llvm-config)}
app_sdk=$root/app/.deps/native/ps5-payload-sdk
mesa_revision=$(python3 -c "import json;print(json.load(open('$root/dependencies.json'))['PS5_Mesa'])")

step() { echo "==> $1 $(date +%T)"; }
fail() { echo "FAIL: $1 (log: $2)" >&2; tail -30 "$2" >&2; exit 1; }

vulkan() {   # PS5_Vulkan's payload SDK and static zlib, its native tool and libc.prx
    step vulkan
    (cd "$PS5_VULKAN_DIR" && bash tools/setup-native-dependencies.sh) > "$log/vulkan-deps.log" 2>&1 || fail "PS5_Vulkan dependencies" "$log/vulkan-deps.log"
    (cd "$PS5_VULKAN_DIR" && make -j"$JOBS") > "$log/vulkan-make.log" 2>&1 || true
    [[ -x $PS5_VULKAN_DIR/build/host/ps5-native-tool && -f $PS5_VULKAN_DIR/runtime/libc.prx ]] || fail "PS5_Vulkan native tool" "$log/vulkan-make.log"
}

sdk() {      # the title's SDK pin
    step sdk
    bash "$root/app/ps5/tools/setup-sdk.sh" > "$log/sdk.log" 2>&1 || fail "title SDK" "$log/sdk.log"
}

radv() {     # RADV at the pinned PS5_Mesa, then runtime/radv-ps5/*.patch (tools/wine-ps5/build-radv.sh)
    step radv
    local clc=$PS5_VULKAN_DIR/.deps/work/radv-clc-build src=$PS5_VULKAN_DIR/.deps/work/radv-src
    if [[ ! -f $clc/build.ninja ]]; then
        # mesa_clc's host build, configured as PS5_Vulkan's build-radv.sh would, with LLVM_CONFIG pinned
        if [[ ! -f $src/.revision ]]; then
            mkdir -p "$src"
            git -C "$PS5_MESA_FORK" archive "$mesa_revision" | tar -x -C "$src"
            echo "$mesa_revision" > "$src/.revision"
        fi
        printf "[binaries]\nllvm-config = '%s'\n" "$LLVM_CONFIG" > "$root/build/llvm-native.ini"
        "$MESON" setup "$clc" "$src" --native-file "$root/build/llvm-native.ini" -Dbuildtype=release -Dmesa-clc=enabled \
            -Dinstall-mesa-clc=true -Dgallium-drivers= -Dvulkan-drivers= -Dplatforms= -Dglx=disabled -Degl=disabled \
            -Dgbm=disabled -Dopengl=false -Dgles1=disabled -Dgles2=disabled -Dllvm=enabled -Dshared-llvm=enabled \
            -Dbuild-tests=false -Dvalgrind=disabled -Dlibunwind=disabled -Dzstd=disabled -Dxmlconfig=disabled \
            -Dtools= > "$log/radv-clc-setup.log" 2>&1 || fail "mesa_clc host build setup" "$log/radv-clc-setup.log"
    fi
    PATH=$(dirname "$LLVM_CONFIG"):$PATH bash "$PS5_VULKAN_DIR/tools/build-radv.sh" release > "$log/radv-release.log" 2>&1 ||
        fail "RADV release archive" "$log/radv-release.log"
    PATH=$(dirname "$LLVM_CONFIG"):$PATH bash "$root/tools/wine-ps5/build-radv.sh" > "$log/radv-patched.log" 2>&1 ||
        fail "RADV with this project's patches" "$log/radv-patched.log"
    cat "$root/build/radv/PROVENANCE.txt"
}

tls() {
    step tls
    bash "$root/tools/wine-ps5/build-tls.sh" > "$log/tls.log" 2>&1 || fail "TLS libraries" "$log/tls.log"
}

wine_tools() {  # host builds of Wine's build tools, for the cross tree; applies the patch to vendor/wine
    step wine-tools
    local src=$root/vendor/wine build=$root/build/wine-tools
    git -C "$src" apply --reverse --check "$root/runtime/wine-ps5/wine-ps5.patch" 2>/dev/null ||
        git -C "$src" apply "$root/runtime/wine-ps5/wine-ps5.patch"
    bash "$root/tools/wine-ps5/sync-memory-layer.sh" "$src"
    mkdir -p "$build"
    (
        cd "$build"
        [[ -f Makefile ]] || "$src/configure" --enable-win64 --enable-archs=x86_64 --disable-tests \
            --without-x --without-wayland --without-opengl --without-alsa --without-pulse --without-oss \
            --without-gstreamer --without-udev --without-cups --without-dbus --without-sdl --without-pcap \
            --without-freetype --without-fontconfig --without-hwloc --without-gnutls --without-vulkan
        make -j"$JOBS" tools/winebuild/winebuild tools/widl/widl tools/wrc/wrc tools/winegcc/winegcc
    ) > "$log/wine-tools.log" 2>&1 || fail "Wine host tools" "$log/wine-tools.log"
}

wine_configure() {   # work/wine-ps5-build-check: the console's cross tree tools/wine-ps5 builds in
    step wine-configure
    local src=$root/vendor/wine build=$root/work/wine-ps5-build-check tls=$root/build/ps5-tls
    mkdir -p "$build"
    (
        cd "$build"
        GNUTLS_CFLAGS="-I$tls/include" GNUTLS_LIBS="-L$tls/lib -lgnutls" ac_cv_lib_soname_gnutls=static-ps5 \
        "$src/configure" --host=x86_64-unknown-freebsd9 --with-wine-tools="$root/build/wine-tools" \
            --enable-win64 --enable-archs=x86_64 --disable-tests \
            --without-x --without-wayland --without-opengl --without-alsa --without-pulse --without-oss \
            --without-gstreamer --without-udev --without-cups --without-dbus --without-sdl --without-pcap \
            --without-freetype --without-fontconfig --without-hwloc --without-vulkan --with-gnutls \
            CC="$app_sdk/bin/prospero-clang" CXX="$app_sdk/bin/prospero-clang++" \
            CFLAGS='-O2 -g -DWINE_INPROC_SERVER' x86_64_CFLAGS='-O2 -g -DWINE_PS5'
    ) > "$log/wine-configure.log" 2>&1 || fail "Wine cross configure" "$log/wine-configure.log"
    grep -q 'SONAME_LIBGNUTLS "static-ps5"' "$build/include/config.h" || fail "GnuTLS not configured as static-ps5" "$log/wine-configure.log"
}

wine() {     # the in-process server and ntdll's Unix side, the other Unix libraries, wine-unix.o
    step wine
    bash "$root/tools/wine-ps5/build-native-server.sh" "$log/native-server" > "$log/native-server.log" 2>&1 ||
        fail "in-process server / ntdll.so" "$log/native-server.log"
    make -C "$root/work/wine-ps5-build-check" -j"$JOBS" -k dlls/ws2_32/ws2_32.so dlls/win32u/win32u.so \
        dlls/winevulkan/winevulkan.so dlls/crypt32/unixlib.o dlls/secur32/schannel_gnutls.o \
        dlls/xinput1_3/unixlib.o include/audioclient.h include/mmdeviceapi.h > "$log/wine-unixlibs.log" 2>&1 || true
    if grep -P "error: (?!unable to find library -lm|linker command failed)" "$log/wine-unixlibs.log"; then
        fail "Wine Unix libraries" "$log/wine-unixlibs.log"
    fi
    bash "$root/tools/wine-ps5/build-title-object.sh" > "$log/title-object.log" 2>&1 || fail "wine-unix.o" "$log/title-object.log"
}

title() {
    step title
    bash "$root/app/ps5/tools/build.sh" > "$log/title.log" 2>&1 || fail "the title" "$log/title.log"
    sha256sum "$root/app/dist/PPSA99220/eboot.bin"
}

steps=("$@")
(( ${#steps[@]} )) || steps=(vulkan sdk radv tls wine-tools wine-configure wine title)
for s in "${steps[@]}"; do
    case $s in
        vulkan) vulkan ;; sdk) sdk ;; radv) radv ;; tls) tls ;; wine-tools) wine_tools ;;
        wine-configure) wine_configure ;; wine) wine ;; title) title ;;
        *) echo "unknown step: $s" >&2; exit 2 ;;
    esac
done
echo "==> done $(date +%T)"
