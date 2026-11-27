"""Robots policy matching and bounded request-spacing hints."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit


MIN_ORIGIN_INTERVAL = 0.5


def _robot_octets(value: str) -> str:
    def decode(match: re.Match) -> str:
        char = chr(int(match[0][1:], 16))
        return char if char in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~' else match[0].upper()
    return re.sub(r'%[0-9a-fA-F]{2}', decode, quote(value, safe="%/:?@!$&'()*+,;=-._~"))


def _robot_match(pattern: str, path: str) -> bool:
    anchored = pattern.endswith('$')
    parts = (pattern[:-1] if anchored else pattern).split('*')
    if len(parts) == 1:
        return path == parts[0] if anchored else path.startswith(parts[0])
    if not path.startswith(parts[0]):
        return False
    start = len(parts.pop(0))
    end = len(path)
    if anchored:
        suffix = parts.pop()
        if not path.endswith(suffix):
            return False
        end -= len(suffix)
    for part in filter(None, parts):
        offset = path.find(part, start, end)
        if offset < 0:
            return False
        start = offset + len(part)
    return start <= end


@dataclass(frozen=True)
class _Robots:
    rules: tuple[tuple[str, bool], ...] = ()
    interval: float = MIN_ORIGIN_INTERVAL

    @classmethod
    def parse(cls, data: bytes) -> _Robots:
        groups = []
        agents, rules, delays = [], [], []
        seen_rule = False
        for raw in data.decode('utf-8-sig', errors='replace').splitlines():
            field, sep, value = raw.split('#', 1)[0].partition(':')
            if not sep:
                continue
            field, value = field.strip().lower(), value.strip()
            if field == 'user-agent':
                if seen_rule:
                    groups.append((agents, rules, delays))
                    agents, rules, delays, seen_rule = [], [], [], False
                agents.append(value.lower())
            elif agents and field in {'allow', 'disallow', 'crawl-delay'}:
                seen_rule = True
                if field == 'crawl-delay':
                    if re.fullmatch(r'\d{1,8}(?:\.\d{1,6})?', value):
                        delays.append(float(value))
                elif value.startswith('/'):
                    rules.append((_robot_octets(value), field == 'allow'))
        groups.append((agents, rules, delays))
        selected = [group for group in groups if 'selfcorpus' in group[0]]
        if not selected:
            selected = [group for group in groups if '*' in group[0]]
        return cls(tuple(rule for _, entries, _ in selected for rule in entries),
                   max([MIN_ORIGIN_INTERVAL] + [delay for _, _, delays in selected for delay in delays]))

    def allows(self, url: str) -> bool:
        p = urlsplit(url)
        path = _robot_octets(p.path + ('?' + p.query if p.query else ''))
        best = (-1, True)
        for pattern, allow in self.rules:
            if _robot_match(pattern, path):
                literal = pattern.removesuffix('$').replace('*', '')
                specificity = len(re.sub(r'%[0-9A-F]{2}', 'X', literal))
                best = max(best, (specificity, allow))
        return best[1]
