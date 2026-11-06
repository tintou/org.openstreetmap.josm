#!/usr/bin/env python3
"""Update existing AppStream release notes from JOSM's translated Trac history."""

import argparse
import datetime
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


BASE_URL = "https://josm.openstreetmap.de/wiki/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def fetch_page(page):
    url = BASE_URL + urllib.parse.quote(page, safe="/") + "?format=txt"
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def translated_lines(source):
    """Read MOTD translation groups, excluding comments and disabled translations."""
    active = False
    group = None
    for line in source.splitlines():
        if line.strip() == "#!motd":
            active = True
            continue
        if line.strip() == "}}}":
            active = False
        if not active:
            continue
        language, separator, text = line.partition(":")
        if not separator or not language or language.startswith("x"):
            continue
        if language == "en":
            if group:
                yield group
            group = {}
        if group is not None:
            group[language] = text
    if group:
        yield group


def plain_text(text):
    """Keep link labels and text, omitting Trac-specific formatting and targets."""
    text = re.sub(r"\{\{\{(.*?)\}\}\}", r"\1", text)
    text = re.sub(r"\[\[.*?\]\]", "", text)
    text = re.sub(r"\[([^\s\]]+)(?:\s+([^\]]+))?\]",
                  lambda match: match[2] or match[1], text)
    for marker in ("'''", "''", "`", "__", "~~"):
        text = text.replace(marker, "")
    return " ".join(text.split())


def release_date(english):
    """Extract an ISO date from the English release heading, if any."""
    match = re.match(r" \* (?:%\(y\|(\d{4})\)-%\(m\|(\d\d)\)-%\(d\|(\d\d)\)"
                     r"|(\d{4})-(\d\d)-(\d\d))", english)
    if not match:
        return None
    parts = [part for part in match.groups() if part]
    return "-".join(parts)


def release_notes(source, dates=None):
    """Map numeric revisions to lists of translated change entries.

    If a dates dict is given, it receives each release's ISO date.
    """
    notes = {}
    current = None
    for group in translated_lines(source):
        english = group["en"]
        if english.startswith(" * "):
            current = None
            if "stable-release-" in english:
                _, marker, remainder = english.partition("%(r|")
                if marker:
                    revision = remainder.partition(")")[0]
                else:
                    link = english.partition("stable-release-")[2].partition("]")[0]
                    revision = link.split()[-1]
                if not revision.isdecimal():
                    raise ValueError(f"Cannot identify release revision: {english}")
                current = revision
                notes.setdefault(current, [])
                date = release_date(english)
                if dates is not None and date:
                    dates[current] = date
        elif english.startswith("  ") and english.lstrip().startswith("*"):
            if current is not None:
                entry = {}
                for language, text in group.items():
                    # Strip list indentation, including deeper nested items.
                    text = text.lstrip()
                    cleaned = plain_text(text.removeprefix("*").strip())
                    if cleaned:
                        entry[language] = cleaned
                notes[current].append(entry)
    return notes


def history_year(source):
    years = []
    for group in translated_lines(source):
        english = group["en"]
        if not english.startswith(" * "):
            continue
        date = english.removeprefix(" * ").split()[0]
        if date.startswith("%(y|"):
            year = date[len("%(y|"):].partition(")")[0]
        else:
            year = date.partition("-")[0]
        if len(year) == 4 and year.isdecimal():
            years.append(int(year))
    if not years:
        raise ValueError("Cannot identify the year of the unarchived history")
    return max(years)


def add_missing_releases(root, notes, dates):
    """Add releases found in the history but absent from the AppStream file."""
    releases = root.find("./releases")
    known = {release.get("version") for release in releases.findall("release")}
    added = 0
    for version in notes:
        if version in known or version not in dates:
            continue
        ET.SubElement(releases, "release", {"version": version, "date": dates[version]})
        added += 1
    if added:
        children = list(releases)
        children.sort(key=lambda release: int(release.get("version")), reverse=True)
        releases[:] = children
    return added


def update_releases(root, notes):
    count = 0
    for release in root.findall("./releases/release"):
        entries = notes.get(release.get("version"))
        if not entries:
            continue
        description = release.find("description")
        if description is None:
            description = ET.SubElement(release, "description")
        else:
            description.clear()
        languages = set.intersection(*(set(entry) for entry in entries))
        if "en" not in languages:
            raise ValueError(f"Missing English notes for {release.get('version')}")
        changes = ET.SubElement(description, "ul")
        for entry in entries:
            for language in ["en"] + sorted(languages - {"en"}):
                attributes = {} if language == "en" else {XML_LANG: language}
                ET.SubElement(changes, "li", attributes).text = entry[language]
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metainfo", nargs="?", type=Path,
                        default=Path(__file__).with_name("org.openstreetmap.josm.metainfo.xml"))
    args = parser.parse_args()
    tree = ET.parse(args.metainfo)
    root = tree.getroot()
    releases = root.findall("./releases/release")
    if not releases:
        parser.error("The AppStream file has no releases")

    source = fetch_page("VersionHistorySource")
    dates = {}
    notes = release_notes(source, dates)
    # The unarchived source identifies the newest year still held on that page.
    newest_year = history_year(source)
    oldest_year = min(datetime.date.fromisoformat(release.attrib["date"]).year
                      for release in releases)
    for year in range(oldest_year, newest_year):
        notes.update(release_notes(fetch_page(f"VersionHistorySource/{year}"), dates))

    added = add_missing_releases(root, notes, dates)
    count = update_releases(root, notes)
    if not count:
        raise ValueError("No matching release notes found; file left unchanged")
    ET.indent(tree, space="  ")
    tree.write(args.metainfo, encoding="UTF-8", xml_declaration=True)
    print(f"Updated {count} releases ({added} added) in {args.metainfo}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as error:
        sys.exit(f"Cannot update changelog: {error}")
