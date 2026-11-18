# C 版 agent 框架 + 插件
#
#   make            编译框架 agent 和所有插件
#   make install    拷到 /opt/llama/agent/（板上直接跑）
#   make clean
#
# 板上是 gcc 8.4（musl），没有 cJSON/curl，所以框架自带最小 JSON 解析和
# 裸 socket HTTP —— 不依赖任何第三方库，-static 也能编。

CC      ?= gcc
# -U_FORTIFY_SOURCE：OpenWrt 的 musl 没有 __snprintf_chk 这类 glibc fortify 符号，
# 不关掉的话板上 dlopen 会报 "symbol not found"
CFLAGS  := -O2 -Wall -Wextra -fPIC -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0
# musl（OpenWrt）没有独立的 libdl，dlopen 在 libc 里：板上用 make LDFLAGS=
LDFLAGS ?= -ldl

# 插件列表：新增插件只要往这里加一行
PLUGINS := math table batch doc net sysmon owrt

all: agent cgi $(PLUGINS:%=plugins/%.so)

# --wrap 把框架内的 malloc/free 接到 memcheck.c 的记账版本（--memcheck 模式用）
WRAP := -Wl,--wrap=malloc,--wrap=calloc,--wrap=realloc,--wrap=strdup,--wrap=free

agent: agent.c json.c memcheck.c plugin.h json.h
	$(CC) $(CFLAGS) -o $@ agent.c json.c memcheck.c -lm $(WRAP) $(LDFLAGS)

plugins/%.so: plugins/%.c plugin.h
	@mkdir -p plugins
	$(CC) $(CFLAGS) -I. -shared -o $@ $< -lm

install: all
	install -d /opt/llama/agent/plugins
	install -m 755 agent /opt/llama/agent/agent-c
	install -m 755 $(PLUGINS:%=plugins/%.so) /opt/llama/agent/plugins/

clean:
	rm -f agent cgi $(PLUGINS:%=plugins/%.so)

.PHONY: all install clean

cgi: cgi.c
	$(CC) $(CFLAGS) -o $@ cgi.c
