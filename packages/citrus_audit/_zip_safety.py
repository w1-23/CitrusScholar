"""Bounded ZIP inspection for uploaded OOXML and spreadsheet documents."""
from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET


_WORKBOOK = '/xl/workbook.xml'
_MACRO_WORKBOOK = 'application/vnd.ms-excel.sheet.macroenabled.main+xml'
_PLAIN_WORKBOOK = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
_VBA_PROJECT = 'xl/vbaProject.bin'
_VBA_PROJECT_TYPE = 'application/vnd.ms-office.vbaproject'
_VBA_REL = 'http://schemas.microsoft.com/office/2006/relationships/vbaProject'
_ACTIVE_MARKERS = ('vbaproject', 'macrosheet', 'activex', 'embeddings')
_CONTENT_TYPES_NS = '{http://schemas.openxmlformats.org/package/2006/content-types}'
_RELS_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'


def _checked_xml(archive, path):
    info = archive.getinfo(path)
    if info.file_size > 1 << 20:
        raise ValueError('Workbook package metadata exceeds size limit')
    content = archive.read(info).decode('utf-8-sig')
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        raise ValueError('Unexpected workbook package XML declarations')
    return ET.fromstring(content)


def validate_macro_workbook(data: bytes, *, derived: bool = False) -> None:
    """Check native VBA input or its macro-free derived output before use."""
    validate_zip_bytes(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set()
            for item in archive.infolist():
                name = item.filename.lower()
                if name in names or item.flag_bits & 1 or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Unexpected duplicate, encrypted or linked workbook part')
                names.add(name)
                if any(marker in name for marker in _ACTIVE_MARKERS) and (derived or item.filename != _VBA_PROJECT):
                    raise ValueError('Unsupported workbook active part')
            manifest = _checked_xml(archive, '[Content_Types].xml')
            workbook_types = [node.get('ContentType', '').lower()
                              for node in manifest.findall(_CONTENT_TYPES_NS + 'Override')
                              if node.get('PartName') == _WORKBOOK]
            if workbook_types != ([_PLAIN_WORKBOOK] if derived else [_MACRO_WORKBOOK]):
                raise ValueError('Workbook package type does not match its format')
            macro_types = [node for node in manifest if any(
                marker in node.get('ContentType', '').lower() for marker in _ACTIVE_MARKERS)]
            if any(derived or node.get('ContentType', '').lower() != _VBA_PROJECT_TYPE or not (
                    (node.tag == _CONTENT_TYPES_NS + 'Default' and node.get('Extension') == 'bin') or
                    (node.tag == _CONTENT_TYPES_NS + 'Override' and node.get('PartName') == '/' + _VBA_PROJECT))
                   for node in macro_types) or len(macro_types) > 1:
                raise ValueError('Unsupported workbook active content type')
            rels = _checked_xml(archive, 'xl/_rels/workbook.xml.rels')
            macro_rels = [node for node in rels.findall(_RELS_NS + 'Relationship') if any(
                marker in (node.get('Type', '') + node.get('Target', '')).lower() for marker in _ACTIVE_MARKERS)]
            if any(derived or node.get('Type') != _VBA_REL or node.get('TargetMode') not in (None, 'Internal')
                   or node.get('Target') not in ('vbaProject.bin', '/' + _VBA_PROJECT) for node in macro_rels):
                raise ValueError('Unsupported workbook active relationship')
            expected = 1 if not derived and _VBA_PROJECT in archive.namelist() else 0
            if not (len(macro_types) == len(macro_rels) == expected):
                raise ValueError('Inconsistent workbook VBA package metadata')
    except (zipfile.BadZipFile, KeyError, UnicodeError, ET.ParseError) as exc:
        raise ValueError('Invalid workbook package metadata') from exc


MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024


def validate_zip_bytes(data: bytes, *, label: str = "文件") -> int:
    """Return total uncompressed size or raise ValueError before parsing XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"{label}包含过多压缩包成员，已停止读取")
            total = 0
            for info in entries:
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(f"{label}单个解压文件超过 64 MB，已停止读取")
                total += info.file_size
                if total > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise ValueError(f"{label}解压后超过 100 MB，已停止读取")
            return total
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{label}不是有效的 ZIP/OOXML 文件") from exc
