#!/usr/bin/env python3
"""agent 全工具自测：每个工具跑一遍，输出健康报告。

    python3 selftest.py          # 跑一遍
    echo $?                      # 0 = 全部正常

有些工具"报错"是预期行为（比如 write 拒绝二进制扩展名），
用 expect_error=True 标记，不算失败。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent  # noqa: E402

RESULTS = []


def check(name, fn, expect_error=False):
    t0 = time.time()
    try:
        r = fn()
        text = str(r)
        is_err = text.startswith("错误")
        ok = (is_err == expect_error)
        RESULTS.append((name, "OK" if ok else "FAIL", time.time() - t0,
                        text[:95].replace("\n", " ")))
    except Exception as exc:                                   # noqa: BLE001
        RESULTS.append((name, "EXC", time.time() - t0, str(exc)[:95]))


def main():
    wd = agent.WORK_DIR
    print("工作目录: %s" % wd)
    print("schema 约束: %s" % ("开" if agent.USE_SCHEMA else "关"))
    print("音频输出: %s" % ("开" if os.environ.get("AGENT_AUDIO") in ("1", "true", "yes") else "关（只出 MIDI）"))
    print()

    # ---- 基础工具
    check("list", lambda: agent.run_list("/tmp"))
    check("du", lambda: agent.run_du("/tmp"))
    check("write", lambda: agent.run_write("selftest.txt", "hello 世界"))
    check("read", lambda: agent.run_read("selftest.txt"))
    check("read(二进制拒绝)", lambda: agent.run_read("x.bin"), expect_error=True)
    check("calc", lambda: agent.run_calc("(3+4)*5 + sqrt(16)"))
    check("calc(非法)", lambda: agent.run_calc("import os"), expect_error=True)
    check("shell", lambda: agent.run_shell("ls /tmp | head -2"))
    check("sysinfo", lambda: agent.run_sysinfo())
    check("grep", lambda: agent.run_grep("hello", "."))
    check("download(非法URL)", lambda: agent.run_download("file:///etc/x"),
          expect_error=True)
    check("http_get(非法URL)", lambda: agent.run_http_get("file:///etc/x"),
          expect_error=True)
    check("shell(jq)", lambda: agent.run_shell("echo aabb | wc -c"))
    check("shell(重定向被拒)", lambda: agent.run_shell("ls /tmp > /tmp/x"),
          expect_error=True)
    check("shell(白名单)", lambda: agent.run_shell("rm -rf /"), expect_error=True)
    check("shell(非.py拒绝)", lambda: agent.run_shell("python3 /dev/null"),
          expect_error=True)
    check("shell(node-e拒绝)", lambda: agent.run_shell("node -e 1"),
          expect_error=True)
    check("shell(find-delete拒绝)", lambda: agent.run_shell("find /tmp -delete"),
          expect_error=True)
    check("shell(sed-i拒绝)", lambda: agent.run_shell("sed -i s/a/b/ f"),
          expect_error=True)
    check("shell(npm-install拒绝)", lambda: agent.run_shell("npm install x"),
          expect_error=True)
    check("write(/etc拒绝)", lambda: agent.run_write("/etc/config/x", "y"),
          expect_error=True)
    check("write(/usr拒绝)", lambda: agent.run_write("/usr/bin/x", "y"),
          expect_error=True)
    # ask 在非 tty 下不再返回"错误："，而是告诉模型"直接 final 把问题问给用户"
    check("ask(非tty降级)", lambda: agent.run_ask("测试"))

    # ---- 文档
    check("pptx(node版)", lambda: agent.run_pptx("st.pptx", [
        {"type": "cover", "title": "自测"},
        {"type": "bullets", "title": "要点", "bullets": ["a", "b"]},
        {"type": "chart", "title": "图", "chartType": "bar",
         "labels": ["x", "y"], "values": [1, 2]},
    ]))
    check("write(pptx拒绝)", lambda: agent.run_write("bad.pptx", "x"),
          expect_error=True)

    # ---- 音乐
    check("midi(FC风格)", lambda: agent.run_midi("st.mid", "fc_action", 150, 16))
    check("midi(其它风格)", lambda: agent.run_midi("st2.mid", "calm", 90, 8))
    check("music_start", lambda: agent.run_music_start("st_song", 150))
    check("music_bar", lambda: agent.run_music_bar(
        "Am", [{"pitch": 69, "dur": 2}, {"pitch": 72, "dur": 2}]))
    check("music_notes", lambda: agent.run_music_notes([{"pitch": 76, "dur": 1}]))
    check("music_render", lambda: agent.run_music_render("st_song"))
    check("music_import", lambda: agent.run_music_import("st.mid"))

    # ---- 方程求解
    check("solve(三元组)", lambda: agent.run_solve(["R+Y=21", "Y+W=20", "R+W=19"]))
    check("solve(隐式乘法)", lambda: agent.run_solve(["2x+3y=13", "x-y=1"]))
    check("solve(幂)", lambda: agent.run_solve(["x^2=16"]))
    check("solve(参数非法)", lambda: agent.run_solve(""), expect_error=True)

    # ---- 协议层
    check("JSON修复(尾逗号)", lambda: "ok" if agent.extract_jsons('{"a":1,}') else "空")
    check("JSON修复(单引号)", lambda: "ok" if agent.extract_jsons(
        '{"tool":"final","answer":"\'abc."\'}') else "空")
    check("JSON修复(多动作)", lambda: "ok" if len(agent.extract_jsons(
        '{"a":1}{"b":2}')) == 2 else "少")
    check("路径解析", lambda: "ok" if agent._resolve("x.txt").startswith(wd) else "错")

    # ---- 报告
    print("%-24s %-4s %7s  %s" % ("工具", "状态", "耗时", "返回"))
    print("-" * 100)
    for name, st, sec, msg in RESULTS:
        print("%-24s %-4s %6.2fs  %s" % (name, st, sec, msg))
    fails = [r for r in RESULTS if r[1] != "OK"]
    print()
    print("%d/%d 通过%s" % (len(RESULTS) - len(fails), len(RESULTS),
                            "" if not fails else "  ← 失败: " +
                            ", ".join(f[0] for f in fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
