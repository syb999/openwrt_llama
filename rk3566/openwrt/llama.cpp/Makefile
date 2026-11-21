include $(TOPDIR)/rules.mk

# llama.cpp —— llama-server / llama-cli / llama-bench，编译进固件
#
# 在线源码：官方 tag v1.0.0（板上跑过的 master 快照同为 LLAMA_VERSION 1.0）
#   PKG_HASH 固定，上游改动不会悄悄改变产物。
#
# 兼容 OpenWrt 21.02 的 gcc-8.4：
#   ggml 的 ARM NEON 内核用了 vld1q_s8_x4 / vld1q_u8_x4，这两个多向量
#   结构加载 intrinsic 是 GCC 9 才加的，gcc-8.4 会报
#     quants.c: error: incompatible types when assigning to type
#     'int8x16x4_t' from type 'int'
#   patches/001-gcc8-neon-fallback.patch 把"非 aarch64 的兼容实现"这条路
#   也对 GCC<9 打开复用（纯 vld1q_* 调用，GCC 8 完全支持），
#   于是可以用 OpenWrt 自带工具链正常编译，不再依赖外部交叉工具链。
PKG_NAME:=llama.cpp
PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_SOURCE:=llama.cpp-v$(PKG_VERSION).tar.gz
PKG_SOURCE_URL:=https://github.com/ggml-org/llama.cpp/archive/refs/tags/v$(PKG_VERSION).tar.gz
PKG_HASH:=3f5f750e36326aea4d65c7623e7d597a33e395d65f4d76e5e0532171f252532b
PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)
PKG_LICENSE:=MIT
PKG_LICENSE_FILES:=LICENSE
PKG_BUILD_PARALLEL:=1
PKG_USE_MIPS16:=0

include $(INCLUDE_DIR)/package.mk
include $(INCLUDE_DIR)/cmake.mk

define Package/llama.cpp
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=llama.cpp tools (server / cli / bench)
  DEPENDS:=+libstdcpp +libpthread
endef

define Package/llama.cpp/description
  llama.cpp built with the OpenWrt toolchain (musl/aarch64), installed to
  /opt/llama/. Flash-safe: the binaries ship inside the firmware image.
endef

# 只编工具；关 UI 资源、curl；静态库（交叉编译不给 GGML_NATIVE）
# 没有加 -mcpu=cortex-a55+dotprod：本包按 aarch64_cortex-a53 架构出，
# 加了 dotprod 在纯 A53 板子上会非法指令。要极限性能可以自己加。
# GCC 8 的 std::filesystem 在独立的 libstdc++fs 里（llama.cpp 用了 filesystem）
CMAKE_OPTIONS += \
	-DCMAKE_CXX_STANDARD_LIBRARIES="-lstdc++fs" \
	-DLLAMA_BUILD_SERVER=ON \
	-DLLAMA_BUILD_TOOLS=ON \
	-DLLAMA_BUILD_TESTS=OFF \
	-DLLAMA_BUILD_EXAMPLES=OFF \
	-DLLAMA_BUILD_HTML=OFF \
	-DLLAMA_CURL=OFF \
	-DGGML_NATIVE=OFF \
	-DGGML_OPENMP=OFF \
	-DGGML_BACKEND_DL=OFF \
	-DBUILD_SHARED_LIBS=OFF

# 三个都装：server 服务本体，cli 命令行试模型，bench 测速
define Package/llama.cpp/install
	$(INSTALL_DIR) $(1)/opt/llama
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/bin/llama-server $(1)/opt/llama/llama-server
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/bin/llama-cli    $(1)/opt/llama/llama-cli
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/bin/llama-bench  $(1)/opt/llama/llama-bench
endef

$(eval $(call BuildPackage,llama.cpp))
