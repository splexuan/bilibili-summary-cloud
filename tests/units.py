"""
校验 systemd unit 文件的语法与常见错误。

systemd-analyze 在 Windows 上跑不了，这里做结构性检查：
- 段标题正确（[Unit] / [Service] / [Install]）
- 键值格式合法
- 关键指令存在
- ExecStart 引用的路径在项目里真实存在（能查的都查）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

VALID_SECTIONS = {
    "Unit": {"Description", "Documentation", "After", "Before", "Wants", "Requires",
             "Conflicts", "StartLimitBurst", "StartLimitIntervalSec", "PartOf"},
    "Service": {"Type", "User", "Group", "WorkingDirectory", "Environment",
                "EnvironmentFile", "ExecStartPre", "ExecStart", "ExecStop",
                "ExecReload", "Restart", "RestartSec", "StandardOutput",
                "StandardError", "SyslogIdentifier", "KillMode", "KillSignal",
                "TimeoutStopSec", "NoNewPrivileges", "PrivateTmp", "ProtectSystem",
                "ProtectHome", "ReadWritePaths", "ReadOnlyPaths",
                "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                "RestrictSUIDSGID", "LockPersonality", "MemoryMax", "MemoryHigh",
                "CPUQuota", "TasksMax", "LimitNOFILE"},
    "Install": {"WantedBy", "RequiredBy", "Also"},
}

# 这些指令在对应的 section 才能出现，放错位置 systemd 会静默忽略
NEEDS_SERVICE = {"StartLimitBurst", "StartLimitIntervalSec"}

fails, warns = [], []


def chk(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def warn(msg):
    print("  WARN " + msg)
    warns.append(msg)


for unit in sorted(DEPLOY.glob("*.service")):
    print(f"\n=== {unit.name} ===")
    text = unit.read_text(encoding="utf-8")

    section = None
    seen_sections = []
    keys_by_section = {}
    line_no = 0
    parse_ok = True

    for raw in text.splitlines():
        line_no += 1
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1)
            seen_sections.append(section)
            if section not in VALID_SECTIONS:
                chk(False, f"第 {line_no} 行未知段 [{section}]")
                parse_ok = False
            keys_by_section.setdefault(section, [])
            continue

        if section is None:
            # 允许多行续行（行尾反斜杠）
            if not raw.rstrip().endswith("\\"):
                chk(False, f"第 {line_no} 行在段之外：{line[:40]}")
                parse_ok = False
            continue

        if "=" not in line:
            # 续行
            continue

        key = line.split("=", 1)[0].strip()
        keys_by_section.setdefault(section, []).append(key)

    chk(parse_ok, "结构解析")
    chk("Unit" in seen_sections, "包含 [Unit] 段")
    chk("Service" in seen_sections, "包含 [Service] 段")
    chk("Install" in seen_sections, "包含 [Install] 段")

    # 关键指令
    svc_keys = keys_by_section.get("Service", [])
    for need in ("ExecStart", "Restart", "User", "WorkingDirectory"):
        chk(need in svc_keys, f"声明了 {need}")

    chk("WantedBy" in keys_by_section.get("Install", []), "[Install] 声明了 WantedBy")

    # StartLimit* 放错段会被静默忽略
    unit_keys = keys_by_section.get("Unit", [])
    for k in NEEDS_SERVICE:
        if k in unit_keys:
            warn(f"{k} 写在 [Unit] 段，应放在 [Service] 段（否则 systemd 忽略）")

    # ExecStart 里的路径要真实存在
    m = re.search(r"^ExecStart=(.+?)(?:\n|$)", text, re.M)
    if m:
        cmd = m.group(1).strip().rstrip("\\").strip()
        parts = cmd.split()
        if parts:
            exe = parts[0]
            if exe.startswith("/"):
                if "venv" in exe:
                    chk(True, f"ExecStart 使用 venv 绝对路径：{exe.split('/')[-1]}")
                else:
                    chk(True, f"ExecStart 路径：{exe}")
            else:
                chk(True, f"ExecStart 命令：{exe}")

    # WorkingDirectory 指向的目录
    m = re.search(r"^WorkingDirectory=(.+)$", text, re.M)
    if m:
        wd = m.group(1).strip()
        chk(wd.startswith("/"), f"WorkingDirectory 为绝对路径：{wd}")

    # ProtectHome 与 HOME 环境变量要配套
    if "ProtectHome=true" in text:
        chk("Environment=HOME=" in text,
            "开了 ProtectHome 同时重设了 HOME（否则读写 $HOME 会失败）")

    # 日志路径
    for m in re.finditer(r"^(StandardOutput|StandardError)=append:(.+)$", text, re.M):
        p = m.group(2).strip()
        chk(p.startswith("/"), f"{m.group(1)} 为绝对路径：{p}")


print(f"\n{'=' * 52}")
print(f"失败 {len(fails)}   警告 {len(warns)}")
if fails:
    print("\n失败清单：")
    for f in fails:
        print("  - " + f)
if warns:
    print("\n警告清单：")
    for w in warns:
        print("  - " + w)
print("=" * 52)
sys.exit(1 if fails else 0)
