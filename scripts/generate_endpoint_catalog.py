from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


ENDPOINT_PATTERN = re.compile(
    r"(https?://api-web\.nhle\.com[^\s`]+"
    r"|https?://api\.nhle\.com/stats/rest[^\s`]+"
    r"|/v1/[^\s`;]+"
    r"|/model/v1/[^\s`;]+"
    r"|/\{lang\}[^\s`;]+"
    r"|/ping\b)"
)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower())
    return cleaned.strip("_")


def normalize_path(raw: str, base_hint: Optional[str]) -> Tuple[str, Optional[str]]:
    cleaned = raw.strip().strip("`").strip().rstrip(".,)")
    inferred_base = None

    if cleaned.startswith("http"):
        parsed = urlparse(cleaned)
        if "api-web.nhle.com" in parsed.netloc:
            inferred_base = "web"
            cleaned = parsed.path
        elif "api.nhle.com" in parsed.netloc and "/stats/rest" in parsed.path:
            inferred_base = "stats"
            cleaned = parsed.path.replace("/stats/rest", "", 1)

    base = inferred_base or base_hint

    if base == "web" and cleaned.startswith("/v1/"):
        cleaned = cleaned[len("/v1/") :]

    if cleaned.startswith("/"):
        cleaned = cleaned[1:]

    return cleaned, base


def normalize_tokens(path: str) -> Tuple[str, Dict[str, str]]:
    tokens = re.findall(r"\{([^}]+)\}", path)
    params = {}
    normalized_path = path
    for token in tokens:
        normalized = token.replace("-", "_")
        if normalized != token:
            normalized_path = normalized_path.replace("{" + token + "}", "{" + normalized + "}")
        params[normalized] = "string"
    return normalized_path, params


def cost_for(base: str, category: str, path: str) -> int:
    lowered = f"{category} {path}".lower()
    if "edge" in lowered:
        return 4
    if "gamecenter" in lowered or "play-by-play" in lowered or "wsc" in lowered:
        return 3
    if base == "stats":
        return 3
    if "schedule" in lowered or "standings" in lowered or "roster" in lowered:
        return 2
    return 1


def extract_endpoints(lines: Iterable[str]) -> List[Dict[str, str]]:
    base: Optional[str] = None
    h2: Optional[str] = None
    h3: Optional[str] = None
    h4: Optional[str] = None
    endpoints: List[Dict[str, str]] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if stripped.startswith("# NHL Web API Documentation"):
            base = "web"
            continue
        if stripped.startswith("# NHL Stats API Documentation"):
            base = "stats"
            continue
        if stripped.startswith("## "):
            h2 = stripped[3:].strip()
            h3 = None
            h4 = None
        elif stripped.startswith("### "):
            h3 = stripped[4:].strip()
            h4 = None
        elif stripped.startswith("#### "):
            h4 = stripped[5:].strip()

        if in_code_block or "Endpoint" not in line:
            continue

        matches = ENDPOINT_PATTERN.findall(line)
        if not matches:
            continue

        category_source = h3 or h2 or "uncategorized"
        category = slugify(category_source)
        description_source = h4 or h3 or h2 or "Endpoint"
        description = f"{description_source} endpoint"

        for match in matches:
            path, inferred_base = normalize_path(match, base)
            if not path or not (inferred_base or base):
                continue
            effective_base = inferred_base or base
            endpoints.append(
                {
                    "base": effective_base,
                    "path": path,
                    "category": category,
                    "description": description,
                }
            )

    return endpoints


def build_catalog(lines: Iterable[str]) -> List[Dict[str, object]]:
    raw_endpoints = extract_endpoints(lines)
    catalog: List[Dict[str, object]] = []
    name_counts: Dict[str, int] = {}

    for item in raw_endpoints:
        path, params = normalize_tokens(item["path"])
        base = item["base"]
        category = item["category"]
        name_base = slugify(path.replace("/", " "))
        name_counts[name_base] = name_counts.get(name_base, 0) + 1
        name = name_base if name_counts[name_base] == 1 else f"{name_base}_{name_counts[name_base]}"

        catalog.append(
            {
                "name": name,
                "base": base,
                "path": path,
                "method": "GET",
                "category": category,
                "cost": cost_for(base, category, path),
                "description": item["description"],
                "params_schema": {"path": params, "query": {}},
            }
        )

    return catalog


def write_json(path: Path, data: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate NHL endpoint catalog from README.")
    parser.add_argument("--readme", required=True, help="Path to the NHL API README markdown file.")
    parser.add_argument(
        "--output-generated",
        default="src/tools/endpoint_catalog.generated.json",
        help="Where to write the generated catalog JSON.",
    )
    parser.add_argument(
        "--output-merged",
        default="src/tools/endpoint_catalog.json",
        help="Where to write the merged catalog JSON.",
    )
    parser.add_argument(
        "--overrides",
        default="src/tools/endpoint_catalog.overrides.json",
        help="Optional overrides file to merge into the generated catalog.",
    )
    args = parser.parse_args()

    readme_path = Path(args.readme)
    lines = readme_path.read_text(encoding="utf-8").splitlines()
    catalog = build_catalog(lines)

    write_json(Path(args.output_generated), catalog)

    overrides_path = Path(args.overrides)
    merged = catalog
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        if isinstance(overrides, list) and overrides:
            by_path = {item.get("path"): dict(item) for item in catalog}
            by_name = {item.get("name"): dict(item) for item in catalog}
            for override in overrides:
                if not isinstance(override, dict):
                    continue
                key = override.get("path")
                if key in by_path:
                    by_path[key].update(override)
                else:
                    key = override.get("name")
                    if key in by_name:
                        by_name[key].update(override)
                    else:
                        by_path[override.get("path") or override.get("name") or ""] = dict(override)
            merged = list(by_path.values())

    write_json(Path(args.output_merged), merged)


if __name__ == "__main__":
    main()

