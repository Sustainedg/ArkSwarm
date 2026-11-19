"""技能市场（SkillHub）客户端：浏览、详情与下载。

SkillHub（https://skillhub.cn）是一个第三方技能社区，技能以标准 ``SKILL.md`` 目录
打包成 zip 提供。本模块只负责**拉取字节**（列表 / 详情 / 下载 + 安全解压），
「装到哪个内核、怎么编目」留给 :mod:`app.runtime.skills`。

它是叶子工具模块：只依赖标准库与 ``httpx``，不认识任何具体内核，也不碰数据库。
"""
from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

#: 市场后端地址（页面 skillhub.cn 是 SPA，数据由 api.skillhub.cn 提供）
DEFAULT_API_BASE = "https://api.skillhub.cn"
#: 技能页地址前缀（用于拼给用户看的来源链接）
SITE_BASE = "https://skillhub.cn"
#: 允许的排序字段（其余一律忽略，回落到默认）
SORT_FIELDS = ("score", "downloads", "updated", "stars")
#: 单次列表返回的最大条数（防止一次性拉爆）
MAX_PAGE_SIZE = 60
#: 联网超时
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: 允许解压的额度：与内核技能体量上限对齐
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 500


class SkillHubError(Exception):
    """技能市场操作失败：消息面向用户。"""


@dataclass(slots=True)
class MarketSkill:
    """市场里的一个技能（列表 / 详情共用的只读视图）。"""

    slug: str
    name: str
    description: str = ""
    version: str | None = None
    author: str | None = None
    category: str | None = None
    downloads: int = 0
    stars: int = 0
    score: float = 0.0
    owner: str | None = None
    source: str | None = None
    verified: bool = False
    requires_api_key: bool = False
    security: str | None = None
    icon_url: str | None = None
    updated_at: int | None = None

    @property
    def url(self) -> str:
        return f"{SITE_BASE}/skills/{self.slug}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "category": self.category,
            "downloads": self.downloads,
            "stars": self.stars,
            "score": self.score,
            "owner": self.owner,
            "source": self.source,
            "verified": self.verified,
            "requires_api_key": self.requires_api_key,
            "security": self.security,
            "icon_url": self.icon_url,
            "updated_at": self.updated_at,
            "url": self.url,
        }


def _client_kwargs(
    client: httpx.AsyncClient | None,
) -> tuple[httpx.AsyncClient | None, dict[str, Any]]:
    """调用方给了客户端就用它（测试注入用），否则临时新建一个。"""
    if client is not None:
        return client, {}
    return None, {"timeout": TIMEOUT, "follow_redirects": True}


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise SkillHubError("技能市场返回了无法解析的内容") from None
    if not isinstance(data, dict):
        raise SkillHubError("技能市场返回了意外的数据结构")
    return data


def _unwrap(response: httpx.Response) -> Any:
    """统一处理 SkillHub 的 ``{code, data}`` / 直接对象两种返回形状。"""
    data = _payload(response)
    if "code" in data and data.get("code") not in (0, None):
        raise SkillHubError(str(data.get("message") or "技能市场返回了错误"))
    return data.get("data", data)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _security_label(reports: Any) -> str | None:
    """把多份安全审核报告压成一句中文结论（全部通过才算「安全」）。"""
    if not isinstance(reports, dict) or not reports:
        return None
    statuses = [
        str((row or {}).get("status") or "")
        for row in reports.values()
        if isinstance(row, dict)
    ]
    statuses = [status for status in statuses if status]
    if not statuses:
        return None
    if all(status in {"benign", "safe", "pass"} for status in statuses):
        return "安全，无风险"
    return "存在风险提示"


def _parse_market_skill(raw: dict[str, Any], detail: bool = False) -> MarketSkill:
    namespace = raw.get("namespace") if isinstance(raw.get("namespace"), dict) else {}
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    latest = raw.get("latestVersion") if isinstance(raw.get("latestVersion"), dict) else {}
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    nested = raw.get("skill") if isinstance(raw.get("skill"), dict) else {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if raw.get(key) not in (None, ""):
                return raw[key]
            if nested.get(key) not in (None, ""):
                return nested[key]
        return None

    slug = str(pick("slug", "publicSlug") or namespace.get("publicSlug") or "")
    name = str(pick("display_name", "displayName", "name") or nested.get("displayName") or slug)
    description = str(
        raw.get("description_zh") or raw.get("description") or nested.get("description") or ""
    )
    return MarketSkill(
        slug=slug,
        name=name,
        description=description,
        version=str(latest.get("version") or pick("version") or "") or None,
        author=str(owner.get("displayName") or raw.get("author") or "") or None,
        category=str(pick("category") or "") or None,
        downloads=_int(pick("downloads")),
        stars=_int(pick("stars")),
        score=_float(pick("score")),
        owner=str(owner.get("handle") or raw.get("ownerName") or "") or None,
        source=str(pick("source") or "") or None,
        verified=bool(pick("verified")),
        requires_api_key=str(labels.get("requires_api_key") or "").lower() == "true",
        security=_security_label(raw.get("securityReports")) if detail else None,
        icon_url=str(pick("iconUrl") or "") or None,
        updated_at=_int(pick("updated_at", "updatedAt")) or None,
    )


async def list_skills(
    *,
    page: int = 1,
    page_size: int = 24,
    sort_by: str = "score",
    order: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    source: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """从市场拉一页技能；返回 ``{"items", "page", "page_size", "total", "has_more"}``。"""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 24), MAX_PAGE_SIZE))
    sort_by = sort_by if sort_by in SORT_FIELDS else "score"
    params: dict[str, Any] = {"page": page, "pageSize": page_size, "sortBy": sort_by}
    if order:
        params["order"] = order
    if keyword and keyword.strip():
        params["keyword"] = keyword.strip()
    if category:
        params["category"] = category
    if source:
        params["source"] = source

    shared, kwargs = _client_kwargs(client)
    try:
        if shared is not None:
            response = await shared.get(f"{DEFAULT_API_BASE}/api/skills", params=params)
        else:
            async with httpx.AsyncClient(**kwargs) as http:
                response = await http.get(f"{DEFAULT_API_BASE}/api/skills", params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SkillHubError(f"无法连接技能市场：{exc}") from None

    data = _unwrap(response)
    if not isinstance(data, dict):
        raise SkillHubError("技能市场返回了意外的列表结构")
    rows = data.get("skills") if isinstance(data.get("skills"), list) else []
    items = [_parse_market_skill(row).to_dict() for row in rows if isinstance(row, dict)]
    total = _int(data.get("total"))
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": (page * page_size) < total if total else len(items) >= page_size,
    }


async def get_skill(slug: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """拉一个技能的详情。"""
    slug = (slug or "").strip()
    if not slug:
        raise SkillHubError("缺少技能标识")
    shared, kwargs = _client_kwargs(client)
    try:
        if shared is not None:
            response = await shared.get(f"{DEFAULT_API_BASE}/api/v1/skills/{slug}")
        else:
            async with httpx.AsyncClient(**kwargs) as http:
                response = await http.get(f"{DEFAULT_API_BASE}/api/v1/skills/{slug}")
        if response.status_code == 404:
            raise SkillHubError(f"技能不存在：{slug}")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SkillHubError(f"无法连接技能市场：{exc}") from None
    return _parse_market_skill(_payload(response), detail=True).to_dict()


async def download_skill(
    slug: str, *, version: str | None = None, client: httpx.AsyncClient | None = None
) -> bytes:
    """下载一个技能的 zip（原始字节）。"""
    slug = (slug or "").strip()
    if not slug:
        raise SkillHubError("缺少技能标识")
    params: dict[str, Any] = {"slug": slug}
    if version:
        params["version"] = version
    shared, kwargs = _client_kwargs(client)
    try:
        if shared is not None:
            response = await shared.get(f"{DEFAULT_API_BASE}/api/v1/download", params=params)
        else:
            async with httpx.AsyncClient(**kwargs) as http:
                response = await http.get(f"{DEFAULT_API_BASE}/api/v1/download", params=params)
        if response.status_code == 404:
            raise SkillHubError(f"技能不存在：{slug}")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SkillHubError(f"下载技能失败：{exc}") from None
    content = response.content
    if not content:
        raise SkillHubError("技能市场返回了空的压缩包")
    if len(content) > MAX_ARCHIVE_BYTES:
        raise SkillHubError("技能压缩包过大，已拒绝下载")
    return content


def extract_skill_archive(archive: bytes, target: str | Path) -> int:
    """把技能 zip 安全解压到 ``target``（先清空再解压），返回写入的文件数。

    安全约束：拒绝绝对路径 / 父级穿越（zip-slip）、跳过符号链接、限制文件数与总字节数。
    常见形状是**恰好一层顶层目录**（SkillHub 的导出就是这样）；这时剥掉顶层目录，
    让技能文件直接落在 ``target`` 下。没有顶层目录（技能文件在压缩包根部）也支持。
    """
    dest = Path(target)
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile:
        raise SkillHubError("下载到的不是有效的压缩包") from None

    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            raise SkillHubError("压缩包里没有文件")
        if len(infos) > MAX_ARCHIVE_FILES:
            raise SkillHubError("技能文件过多，已拒绝安装")
        total = sum(info.file_size for info in infos)
        if total > MAX_ARCHIVE_BYTES:
            raise SkillHubError("技能解压后体积过大，已拒绝安装")

        tops: set[str] = set()
        nested = False
        for info in infos:
            parts = [part for part in Path(info.filename).parts if part not in ("", ".")]
            if not parts or ".." in parts or Path(info.filename).is_absolute():
                raise SkillHubError(f"压缩包内路径不安全：{info.filename}")
            tops.add(parts[0])
            nested = nested or len(parts) > 1
        # 只有一个顶层条目时剥掉它（SkillHub 导出的形状）；但顶层若是「一个文件」
        # （例如压缩包里只有 SKILL.md），剥掉就什么都不剩了 —— 那种情况原样解压。
        strip = len(tops) == 1 and nested

        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        written = 0
        for info in infos:
            parts = [part for part in Path(info.filename).parts if part not in ("", ".")]
            rel = parts[1:] if strip else parts
            if not rel:
                continue
            out = dest / Path(*rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src:
                out.write_bytes(src.read())
            written += 1
    if written == 0:
        raise SkillHubError("压缩包里没有可用文件")
    return written
