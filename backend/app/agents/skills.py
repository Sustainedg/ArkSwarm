"""技能（Skill）的共享数据结构、解析与物化计划。

技能是各智能体内核自带的能力包：一个目录里含 ``SKILL.md``（YAML frontmatter
描述元数据），以及可选的 ``scripts/`` ``references/`` ``assets/`` 等资源。

平台只读扫描这些目录，把各内核的技能**统一编目**、分配给智能体；运行时再由适配器
声明「我的内核从哪里读技能」（:class:`SkillMount`），框架按这份声明把分配到的技能
物化进该内核能发现的隔离目录，从而保证「智能体能用的技能 == 分配到的技能」。

本模块是叶子层：适配器与框架都依赖它，它只依赖标准库与 PyYAML。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

#: 技能元数据文件名
SKILL_FILE = "SKILL.md"
#: 单个技能最多收录的文件数与总字节数
MAX_SKILL_FILES = 300
MAX_SKILL_BYTES = 5 * 1024 * 1024
#: 一次编目的技能数量上限，避免插件市场里的巨型目录拖慢启动
MAX_SKILLS_PER_ROOT = 500
#: 扫描时跳过的目录名
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}

_KNOWN_META_KEYS = {
    "name", "description", "version", "author", "when_to_use", "when-to-use",
    "allowed-tools", "license", "metadata",
}


@dataclass(slots=True)
class SkillFile:
    """技能目录内的一个文件（相对路径 + 字节数）。"""

    path: str
    size: int


@dataclass(slots=True)
class SkillSpec:
    """一个技能的只读描述；平台编目、分配与物化都以它为单位。"""

    type_id: str
    slug: str
    name: str
    description: str = ""
    source_kind: str = "user"
    path: str = ""
    version: str | None = None
    author: str | None = None
    when_to_use: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    files: list[SkillFile] = field(default_factory=list)
    total_bytes: int = 0
    fingerprint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    #: 来源是「技能市场」时，市场里的技能标识（slug）；其余来源为空
    source_slug: str | None = None
    #: 来源是「技能市场」时，市场技能页地址
    source_url: str | None = None

    @property
    def id(self) -> str:
        """稳定标识：``<智能体类型>:<技能目录名>``。"""
        return f"{self.type_id}:{self.slug}"

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type_id": self.type_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "source_kind": self.source_kind,
            "path": self.path,
            "fingerprint": self.fingerprint,
            "version": self.version,
            "author": self.author,
            "when_to_use": self.when_to_use,
            "allowed_tools": self.allowed_tools,
            "files": [{"path": f.path, "size": f.size} for f in self.files],
            "total_bytes": self.total_bytes,
            "meta": self.extra,
            "note": self.note,
            "source_url": self.source_url or "",
        }


@dataclass(slots=True)
class SkillMount:
    """把「分配到的技能」交给某个内核时要做的物化计划。

    框架只按这份声明执行，不关心是哪个内核：

    * 在 ``<对话级技能主目录>/<home_name>`` 下维护 ``skills_subdir`` 目录，
      内容严格等于智能体分配到的技能（每次运行重建，取消分配即消失）；
    * 复制 ``carry`` 里列出的配置文件（源绝对路径 → 目标相对路径）；
    * 对 ``patch`` 里列出的文件做顶层键合并（例如关掉内核自带插件）；
    * ``notes`` 是需要提示给用户的一次性说明。

    适配器据此在自己的 ``build_invocation()`` 里用 ``ctx.skill_home_dir``
    设置内核的配置目录环境变量。
    """

    home_name: str
    skills_subdir: str = "skills"
    carry: list[tuple[str, str]] = field(default_factory=list)
    patch: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


#: 「技能市场」来源：平台从远程技能库下载、安装进内核原生技能根的技能。
#: 只有这种来源的技能是「由平台装入」的，可以卸载；其余来源（user/plugin/system…）只读。
MARKET_SOURCE = "market"


@dataclass(slots=True)
class SkillRoot:
    """适配器声明的「本内核的技能根」之一。

    这是「哪个内核把技能放在哪」的权威声明：适配器用 :meth:`AgentDefinition.skill_roots`
    列出自己的技能根，框架据此**统一扫描**（编目）、并在安装技能时落到
    ``installable`` 的那个根里。框架不认识任何具体内核，也不硬编码任何路径。

    * ``path``：技能根目录的绝对路径（每个技能是它下面的一个子目录）。
    * ``source_kind``：从该根扫出来的技能归为什么来源（``user`` / ``plugin`` /
      ``system`` / ``market``…），用于界面分组与「能否卸载」判定。
    * ``installable``：新技能装到这个根是否合适。插件市场那种由别人托管的目录为
      ``False``（只读编目），平台自己的市场目录与个人技能目录为 ``True``。
    """

    path: str
    source_kind: str = "user"
    installable: bool = True
    max_depth: int = 1


@dataclass(slots=True)
class MarketInstallInfo:
    """一个由「技能市场」装入的技能在磁盘上的凭据（写在技能目录内的标记文件里）。

    它的存在就是「这个技能是平台从市场装进来的、可以卸载」的判据——不依赖数据库，
    因为技能目录本身是权威来源（删掉目录，编目自然会把它标成失效）。
    """

    #: 市场里对应的技能标识（slug）
    slug: str = ""
    #: 安装时的版本
    version: str | None = None
    #: 市场技能页地址
    source_url: str | None = None
    #: 安装时间（ISO8601 文本）
    installed_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "version": self.version,
            "source_url": self.source_url or "",
            "installed_at": self.installed_at,
        }


#: 市场安装标记文件名（放在技能目录内）
MARKET_MARKER_FILE = ".skillhub.json"


def scan_skill_roots(roots: Iterable[SkillRoot], *, type_id: str) -> list[SkillSpec]:
    """按适配器声明的技能根统一扫描，返回其中所有技能。

    这是「适配器只声明路径、框架负责扫描」的落点：适配器给出 :class:`SkillRoot`，
    这里逐个调用 :func:`scan_skill_root`，来源与深度都照根的声明来。
    """
    specs: list[SkillSpec] = []
    for root in roots:
        scanned = scan_skill_root(
            root.path, type_id=type_id, source_kind=root.source_kind, max_depth=root.max_depth
        )
        for spec in scanned:
            _annotate_market(spec, root.source_kind)
        specs.extend(scanned)
    return specs


def _annotate_market(spec: SkillSpec, source_kind: str) -> None:
    """把「由市场装入」的技能补上来源信息（读取目录内的标记文件，尽力而为）。"""
    if source_kind != MARKET_SOURCE:
        return
    info = read_market_marker(spec.path)
    if info is None:
        return
    spec.source_slug = info.slug or spec.slug
    spec.source_url = info.source_url


def read_market_marker(directory: str | os.PathLike[str]) -> MarketInstallInfo | None:
    """读取技能目录内的市场安装标记；不是市场安装的技能返回 ``None``。"""
    path = Path(directory) / MARKET_MARKER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return MarketInstallInfo(
        slug=str(data.get("slug") or ""),
        version=data.get("version") or None,
        source_url=data.get("source_url") or None,
        installed_at=data.get("installed_at") or None,
    )


def write_market_marker(directory: str | os.PathLike[str], info: MarketInstallInfo) -> None:
    """把市场安装标记写进技能目录（幂等；写入失败不致命）。"""
    path = Path(directory) / MARKET_MARKER_FILE
    path.write_text(json.dumps(info.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 ``---`` 包裹的 YAML frontmatter 与正文。

    解析不了就按「没有 frontmatter」处理：技能是别人写的文件，不能因为一处
    格式错误就让整个编目失败。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1:]).lstrip("\n")
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                # 元数据坏了也要保留正文：技能是别人写的文件，编目必须尽力而为。
                return {}, body
            return (data if isinstance(data, dict) else {}), body
    return {}, text


def parse_skill_dir(
    directory: str | os.PathLike[str],
    *,
    type_id: str,
    source_kind: str = "user",
    slug: str | None = None,
) -> SkillSpec | None:
    """读取一个技能目录；没有 ``SKILL.md`` 时返回 ``None``。"""
    base = Path(directory)
    try:
        text = (base / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    front, body = split_frontmatter(text)
    metadata = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    slug = slug or base.name
    name = _text(front.get("name")) or slug
    description = (
        _text(front.get("description"))
        or _text(metadata.get("short-description"))
        or _first_paragraph(body)
    )
    files, total = scan_skill_files(base)
    oversized = total > MAX_SKILL_BYTES or len(files) >= MAX_SKILL_FILES
    return SkillSpec(
        type_id=type_id,
        slug=slug,
        name=name,
        description=description,
        source_kind=source_kind,
        path=str(base),
        version=_text(front.get("version")) or None,
        author=_text(front.get("author")) or None,
        when_to_use=_text(front.get("when_to_use")) or _text(front.get("when-to-use")) or None,
        allowed_tools=_string_list(front.get("allowed-tools")),
        files=files,
        total_bytes=total,
        fingerprint=_fingerprint(text, files),
        extra={k: v for k, v in front.items() if k not in _KNOWN_META_KEYS},
        note="技能体量较大，文件清单已截断" if oversized else None,
    )


def scan_skill_root(
    root: str | os.PathLike[str],
    *,
    type_id: str,
    source_kind: str = "user",
    max_depth: int = 1,
) -> list[SkillSpec]:
    """扫描一个技能根目录，返回其中所有技能（目录不存在时返回空列表）。"""
    specs: list[SkillSpec] = []
    for meta in find_skill_files(root, max_depth=max_depth):
        spec = parse_skill_dir(meta.parent, type_id=type_id, source_kind=source_kind)
        if spec is not None:
            specs.append(spec)
        if len(specs) >= MAX_SKILLS_PER_ROOT:
            break
    return specs


def find_skill_files(root: str | os.PathLike[str], *, max_depth: int = 1) -> list[Path]:
    """在 ``root`` 下按深度查找 ``SKILL.md``（不跟随符号链接）。"""
    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    base = base.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        depth = len(current.relative_to(base).parts)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in SKIP_DIRS and not (current / name).is_symlink()
        ]
        if depth > max_depth:
            dirnames[:] = []
            continue
        if SKILL_FILE in filenames:
            found.append(current / SKILL_FILE)
        if len(found) >= MAX_SKILLS_PER_ROOT:
            break
    return found


def scan_skill_files(base: Path) -> tuple[list[SkillFile], int]:
    """列出技能目录内的文件（跳过符号链接与常见噪声目录）。"""
    files: list[SkillFile] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in SKIP_DIRS and not (current / name).is_symlink()
        ]
        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(SkillFile(path=str(path.relative_to(base)), size=size))
            total += size
            if len(files) >= MAX_SKILL_FILES:
                return files, total
    return files, total


def safe_skill_dirname(slug: str) -> str | None:
    """把技能目录名限制成单个安全路径段，防止复制时越出目标目录。"""
    name = (slug or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(ch not in allowed for ch in name):
        return None
    return name


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _first_paragraph(body: str) -> str:
    for line in body.splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text[:240]
    return ""


def _fingerprint(text: str, files: list[SkillFile]) -> str:
    digest = hashlib.sha1(text.encode("utf-8", "replace"))
    for item in files:
        digest.update(f"{item.path}:{item.size}".encode())
    return digest.hexdigest()[:16]
