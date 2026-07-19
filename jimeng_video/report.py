"""校验结果的轻量数据结构与终端渲染。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Level(Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


_ICON = {Level.OK: "✓", Level.WARN: "!", Level.FAIL: "✗"}


@dataclass(frozen=True)
class Finding:
    level: Level
    scope: str      # 归属分组，如 "U03" / "结构" / "衔接帧"
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: Level, scope: str, message: str) -> None:
        self.findings.append(Finding(level, scope, message))

    def ok(self, scope: str, message: str) -> None:
        self.add(Level.OK, scope, message)

    def warn(self, scope: str, message: str) -> None:
        self.add(Level.WARN, scope, message)

    def fail(self, scope: str, message: str) -> None:
        self.add(Level.FAIL, scope, message)

    @property
    def failed(self) -> bool:
        return any(f.level is Level.FAIL for f in self.findings)

    @property
    def warned(self) -> bool:
        return any(f.level is Level.WARN for f in self.findings)

    def counts(self) -> dict[Level, int]:
        out = {Level.OK: 0, Level.WARN: 0, Level.FAIL: 0}
        for f in self.findings:
            out[f.level] += 1
        return out

    def render(self) -> str:
        lines = [f"{_ICON[f.level]} [{f.scope}] {f.message}" for f in self.findings]
        c = self.counts()
        lines.append("")
        lines.append(
            f"合计：{c[Level.OK]} 通过 / {c[Level.WARN]} 提醒 / {c[Level.FAIL]} 失败"
        )
        return "\n".join(lines)
