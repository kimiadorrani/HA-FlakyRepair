"""
Export FLAKYCAT spreadsheet metadata into a reproducibility-oriented CSV.

The raw FLAKYCAT repository stores Java test snippets under
`datasets/flakycat/raw/test_files_*` and richer project metadata in
`Dataset_informations.xlsx`.  This script uses only the Python standard library
to read that XLSX file so the import step does not add new dependencies.

Example:
  .venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from zipfile import ZipFile
from xml.etree import ElementTree as ET


ROOT_DIR = Path(__file__).resolve().parents[4]
FLAKYCAT_DIR = ROOT_DIR / "datasets" / "flakycat"
RAW_DIR = FLAKYCAT_DIR / "raw"
DEFAULT_XLSX = RAW_DIR / "Dataset_informations.xlsx"
DEFAULT_OUTPUT_CSV = FLAKYCAT_DIR / "flakycat-java-tests.csv"

MAIN_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = {
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
}
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

FIELDNAMES = [
    "Dataset",
    "Source Sheet",
    "Project",
    "Project URL",
    "Commit SHA",
    "Parent SHA",
    "Checkout SHA",
    "Module Path",
    "Test File",
    "Test Method",
    "Fully Qualified Test Name",
    "Category",
    "Issue Or Commit URL",
    "Snippet File",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export FLAKYCAT metadata to CSV.")
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX), help="Path to Dataset_informations.xlsx")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV), help="Destination CSV path")
    return parser.parse_args()


def col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    result = 0
    for ch in letters:
        result = result * 26 + ord(ch.upper()) - 64
    return result - 1


def load_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(text.text or "" for text in item.findall(".//a:t", MAIN_NS))
        for item in root.findall("a:si", MAIN_NS)
    ]


def read_xlsx_sheets(xlsx_path: Path) -> dict[str, list[list[str]]]:
    with ZipFile(xlsx_path) as archive:
        shared_strings = load_shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

        sheets: dict[str, list[list[str]]] = {}
        for sheet in workbook.findall("a:sheets/a:sheet", MAIN_NS):
            name = sheet.attrib["name"]
            target = relmap[sheet.attrib[OFFICE_REL]]
            path = f"xl/{target.lstrip('/')}" if not target.startswith("xl/") else target
            root = ET.fromstring(archive.read(path))
            rows: list[list[str]] = []
            for row in root.findall("a:sheetData/a:row", MAIN_NS):
                values: list[str] = []
                last_index = -1
                for cell in row.findall("a:c", MAIN_NS):
                    index = col_index(cell.attrib.get("r", "A"))
                    while last_index + 1 < index:
                        values.append("")
                        last_index += 1
                    value = cell.find("a:v", MAIN_NS)
                    text = value.text if value is not None and value.text is not None else ""
                    if cell.attrib.get("t") == "s" and text.isdigit():
                        text = shared_strings[int(text)]
                    values.append(text.strip())
                    last_index = index
                rows.append(values)
            sheets[name] = rows
        return sheets


def github_repo_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("http"):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc.lower() == "github.com" and len(parts) >= 2:
            return f"https://github.com/{parts[0]}/{parts[1]}"
        return ""
    if "/" in value and not value.startswith("/"):
        owner, repo = value.split("/", 1)
        return f"https://github.com/{owner}/{repo}"
    return ""


def project_from_url_or_name(project: str, url: str) -> str:
    repo_url = github_repo_url(url) if url else ""
    if repo_url:
        return repo_url.rstrip("/").split("/")[-1]
    return project.strip()


def snippet_lookup(raw_dir: Path) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for path in (raw_dir / "test_files_v0").glob("*.txt"):
        stem = path.stem
        if "@" not in stem:
            continue
        test_id, category = stem.rsplit("@", 1)
        lookup.setdefault((test_id.lower(), category.strip().lower()), str(path.relative_to(ROOT_DIR)))
    return lookup


def find_snippet(
    lookup: dict[tuple[str, str], str],
    project: str,
    test_method: str,
    category: str,
    fqn: str = "",
) -> str:
    category_key = category.strip().lower()
    candidates = []
    if fqn:
        short_fqn = ".".join(fqn.split(".")[-2:])
        candidates.append(f"{project}.{short_fqn}")
    if test_method:
        candidates.append(f"{project}.{test_method}")
    for (test_id, candidate_category), path in lookup.items():
        if candidate_category == category_key and any(candidate.lower() in test_id for candidate in candidates):
            return path
    return ""


def normalize_category(raw: str) -> str:
    return " ".join(raw.replace("_", " ").split()).strip()


def checkout_sha(commit_sha: str, parent_sha: str) -> str:
    parent = parent_sha.strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", parent):
        return parent
    return commit_sha.strip()


def rows_from_sheets(sheets: dict[str, list[list[str]]], raw_dir: Path) -> Iterable[dict[str, str]]:
    snippets = snippet_lookup(raw_dir)

    for sheet_name, rows in sheets.items():
        if not rows:
            continue
        header = [value.strip().lower() for value in rows[0]]
        for row in rows[1:]:
            data = {header[i]: row[i] if i < len(row) else "" for i in range(len(header))}

            project = project_url = commit_sha = parent_sha = module_path = ""
            test_file = test_method = fqn = category = issue_url = ""

            if sheet_name == "Costa et al,":
                project = data.get("repository/project", "")
                project_url = github_repo_url(data.get("url issue", ""))
                commit_sha = data.get("sha", "")
                test_file = data.get("testclass", "")
                test_method = data.get("testmethod", "")
                category = data.get("category", "")
                issue_url = data.get("url issue", "")
            elif sheet_name == "Habchi et al,":
                project = data.get("name", "")
                project_url = github_repo_url(project)
                commit_sha = data.get("commit", "")
                parent_sha = data.get("parent commit", "")
                test_method = data.get("test name", "")
                category = data.get("categories \nfinal decision", "")
                issue_url = data.get("github url", "")
            elif sheet_name == "IfixFlakies":
                project = data.get("project", "")
                commit_sha = data.get("sha detected", "")
                module_path = data.get("module path", "")
                fqn = data.get("fully-qualified test name (packagename.classname.methodname)", "")
                test_method = fqn.rsplit(".", 1)[-1] if fqn else ""
                category = data.get("category", "")
                issue_url = data.get("pr link", "")
                project_url = github_repo_url(issue_url)
            elif sheet_name == "collected":
                project = data.get("project", "")
                project_url = github_repo_url(project)
                commit_sha = data.get("sha", "")
                test_file = data.get("file", "")
                test_method = data.get("function name", "")
                if test_file and not test_file.endswith(".java") and "/" not in test_file:
                    fqn = test_file
                    test_file = ""
                category = data.get("category", "")
                issue_url = data.get("commit link", "")
            else:
                continue

            if not project or not commit_sha or not test_method:
                continue

            project_name = project_from_url_or_name(project, project_url)
            category = normalize_category(category)
            snippet = find_snippet(snippets, project_name, test_method, category, fqn=fqn)

            yield {
                "Dataset": "FLAKYCAT",
                "Source Sheet": sheet_name,
                "Project": project_name,
                "Project URL": project_url,
                "Commit SHA": commit_sha,
                "Parent SHA": parent_sha,
                "Checkout SHA": checkout_sha(commit_sha, parent_sha),
                "Module Path": module_path,
                "Test File": test_file,
                "Test Method": test_method,
                "Fully Qualified Test Name": fqn or test_method,
                "Category": category,
                "Issue Or Commit URL": issue_url,
                "Snippet File": snippet,
            }


def main() -> None:
    args = parse_args()
    xlsx_path = Path(args.xlsx)
    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    sheets = read_xlsx_sheets(xlsx_path)
    rows = list(rows_from_sheets(sheets, xlsx_path.parent))

    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} FLAKYCAT metadata row(s) to {output_csv}")


if __name__ == "__main__":
    main()
