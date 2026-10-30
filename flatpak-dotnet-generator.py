#!/usr/bin/env python3

__license__ = "MIT"

import argparse
import base64
import binascii
import json
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def get_package_source(package_dir: Path) -> str:
    metadata_path = package_dir / ".nupkg.metadata"

    if not metadata_path.is_file():
        raise RuntimeError(
            f"Missing {metadata_path}. NuGet did not provide package source metadata."
        )

    with metadata_path.open(encoding="utf-8") as fp:
        metadata = json.load(fp)

    source = metadata.get("source")
    if not isinstance(source, str) or not source:
        raise RuntimeError(
            f"No 'source' value found in {metadata_path}; cannot determine which feed supplied the package."
        )

    return source


def get_v3_package_base_address(source: str) -> str | None:
    request = urllib.request.Request(
        source,
        headers={"User-Agent": "flatpak-dotnet-generator"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            service_index = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    for resource in service_index.get("resources", []):
        resource_type = resource.get("@type", [])
        if isinstance(resource_type, str):
            resource_type = [resource_type]

        if any(
            isinstance(item, str) and item.startswith("PackageBaseAddress/")
            for item in resource_type
        ):
            address = resource.get("@id")
            if isinstance(address, str) and address:
                return address.rstrip("/") + "/"

    return None


def get_v2_package_url(source: str, package_id: str, version: str) -> str | None:
    query = urllib.parse.urlencode({"$filter": f"Id eq '{package_id}'"})
    separator = "&" if "?" in source else "?"
    request_url = source.rstrip("/") + "/Packages" + separator + query
    request = urllib.request.Request(
        request_url,
        headers={
            "Accept": "application/atom+xml,application/xml",
            "User-Agent": "flatpak-dotnet-generator",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = ET.fromstring(response.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError):
        return None

    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    }

    for entry in document.findall("atom:entry", namespaces):
        package_version = entry.findtext(
            "m:properties/d:Version", namespaces=namespaces
        )
        if package_version != version:
            continue

        content = entry.find("atom:content", namespaces)
        if content is not None and content.get("src"):
            return urllib.parse.urljoin(request_url, content.get("src"))

        for link in entry.findall("atom:link", namespaces):
            if link.get("rel") == "edit" and link.get("href"):
                return urllib.parse.urljoin(request_url, link.get("href"))

    return None


def get_package_url(
    source: str,
    package_id: str,
    version: str,
    service_index_cache: dict[str, str | None],
) -> str:
    if source not in service_index_cache:
        service_index_cache[source] = get_v3_package_base_address(source)

    base_address = service_index_cache[source]
    if base_address:
        package_path = "/".join(
            urllib.parse.quote(part, safe="")
            for part in (package_id.lower(), version.lower(), f"{package_id}.{version}.nupkg".lower())
        )
        return urllib.parse.urljoin(base_address, package_path)

    if source.lower().rstrip("/").endswith(("/v2", "/api/v2")):
        package_url = get_v2_package_url(source, package_id, version)
        if package_url:
            return package_url

    raise RuntimeError(
        f"Could not resolve a downloadable package URL for {package_id} {version} from {source!r}. "
        "The feed must expose a NuGet v3 PackageBaseAddress resource or a NuGet v2 package endpoint. "
        "Authenticated feeds must also allow the generated Flatpak source URL to be downloaded by the build."
    )


def make_source(
    path: Path,
    destdir: str,
    service_index_cache: dict[str, str | None],
) -> dict:
    package_id = path.parent.parent.name
    version = path.parent.name
    filename = f"{package_id}.{version}.nupkg"
    feed = get_package_source(path.parent)
    url = get_package_url(feed, package_id, version, service_index_cache)

    with path.open(encoding="ascii") as fp:
        encoded_hash = fp.read().strip()

    try:
        sha512 = binascii.hexlify(
            base64.b64decode(encoded_hash, validate=True)
        ).decode("ascii")
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"Invalid SHA-512 value in {path}") from exc

    if len(sha512) != 128:
        raise RuntimeError(f"Invalid SHA-512 length in {path}")

    return {
        "type": "file",
        "url": url,
        "sha512": sha512,
        "dest": destdir,
        "dest-filename": filename,
    }



def discover_projects(projects: list[str], recursive: bool) -> list[str]:
    discovered = []
    seen = set()
    ignored_directories = {".git", ".vs", "bin", "obj", "node_modules"}

    for value in projects:
        path = Path(value).resolve()
        scan_root = path if path.is_dir() else None

        if recursive and scan_root is None:
            scan_root = next(
                (parent for parent in path.parents if (parent / ".git").exists()),
                None,
            )

        if scan_root is not None:
            for candidate in scan_root.rglob("*.csproj"):
                if any(part in ignored_directories for part in candidate.relative_to(scan_root).parts):
                    continue
                resolved = str(candidate.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    discovered.append(resolved)
        else:
            resolved = str(path)
            if resolved not in seen:
                seen.add(resolved)
                discovered.append(resolved)

    if not discovered:
        raise RuntimeError("No project or solution files were found in the supplied paths.")

    return discovered


def get_declared_runtimes(project: str) -> set[str]:
    runtimes = set()
    project_path = Path(project)
    property_files = [project_path]
    property_files.extend(
        parent / filename
        for parent in project_path.parents
        for filename in ("Directory.Build.props", "Directory.Build.targets")
        if (parent / filename).is_file()
    )

    for property_file in property_files:
        try:
            root = ET.parse(property_file).getroot()
        except (ET.ParseError, OSError):
            continue

        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1]
            if name not in ("RuntimeIdentifier", "RuntimeIdentifiers") or not element.text:
                continue
            for runtime in element.text.split(";"):
                runtime = runtime.strip()
                if runtime and "$" not in runtime and "{" not in runtime:
                    runtimes.add(runtime)

    return runtimes


def main() -> None:
    freedesktop_default = "25.08"
    dotnet_default = "8"

    parser = argparse.ArgumentParser()
    parser.add_argument("output", help="The output JSON sources file")
    parser.add_argument("project", nargs="+", help="Project file(s), solution file(s), or directories to scan recursively")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When a project file is supplied, scan its Git repository for additional projects, including submodules",
    )
    parser.add_argument(
        "--runtime",
        "-r",
        nargs="+",
        default=[],
        help="Additional target runtime(s) to restore packages for; project RuntimeIdentifier(s) are also detected",
    )
    parser.add_argument(
        "--freedesktop",
        "-f",
        default=freedesktop_default,
        help="The target version of the freedesktop SDK to use",
    )
    parser.add_argument(
        "--dotnet",
        "-d",
        default=dotnet_default,
        help="The target version of the dotnet SDK extension to use",
    )
    parser.add_argument(
        "--destdir",
        default="nuget-sources",
        help="The directory the generated sources file will save sources to",
    )
    parser.add_argument(
        "--dotnet-args",
        "-a",
        nargs=argparse.REMAINDER,
        help="Additional arguments to pass to the dotnet restore command",
    )
    args = parser.parse_args()

    projects = discover_projects(args.project, args.recursive)
    runtime_by_project = {
        project: sorted(get_declared_runtimes(project) | set(args.runtime))
        for project in projects
    }
    sources_by_filename = {}
    service_index_cache = {}

    with tempfile.TemporaryDirectory(dir=Path()) as tmp:
        def restore_project(project: str, runtime: str | None) -> None:
            command = [
                "flatpak",
                "run",
                "--env=DOTNET_CLI_TELEMETRY_OPTOUT=true",
                "--env=DOTNET_SKIP_FIRST_TIME_EXPERIENCE=true",
                "--command=sh",
                f"--runtime=org.freedesktop.Sdk//{args.freedesktop}",
                "--share=network",
                "--filesystem=host",
                f"org.freedesktop.Sdk.Extension.dotnet{args.dotnet}//{args.freedesktop}",
                "-c",
                f'PATH="${{PATH}}:/usr/lib/sdk/dotnet{args.dotnet}/bin" '
                f'LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/lib/sdk/dotnet{args.dotnet}/lib" '
                'exec dotnet restore "$@"',
                "--",
                "--packages",
                tmp,
                project,
            ]

            if runtime:
                command.extend(["-r", runtime])

            command.extend(args.dotnet_args or [])
            subprocess.run(command, check=True)

        for project in projects:
            for runtime in [None, *runtime_by_project[project]]:
                restore_project(project, runtime)

        for path in Path(tmp).glob("**/*.nupkg.sha512"):
            source = make_source(path, args.destdir, service_index_cache)
            filename = source["dest-filename"]
            previous = sources_by_filename.get(filename)

            if previous and previous["sha512"] != source["sha512"]:
                raise RuntimeError(
                    f"Different package contents found for {filename}. "
                    "The same package ID/version was restored from different feeds."
                )

            sources_by_filename[filename] = source

    sources = sorted(
        sources_by_filename.values(),
        key=lambda item: item["dest-filename"].lower(),
    )

    with open(args.output, "w", encoding="utf-8") as fp:
        json.dump(sources, fp, indent=4)
        fp.write("\n")


if __name__ == "__main__":
    main()
