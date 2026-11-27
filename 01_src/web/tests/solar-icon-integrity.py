"""Integrity and reference coverage for the one shared Solar icon catalog."""
import hashlib
import json
import re
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

WEB = Path(__file__).resolve().parents[1]
ASSETS = WEB / 'assets'
VENDOR = WEB / 'vendor' / 'solar-icons'


class SolarAssets(unittest.TestCase):
    def test_originals_and_sprite_match_manifest(self):
        manifest = json.loads((ASSETS / 'icon-manifest.json').read_bytes())
        self.assertEqual(manifest['upstream'], json.loads((VENDOR / 'upstream-lock.json').read_bytes()))
        selection = json.loads((VENDOR / 'selection.json').read_bytes())
        actual = {x['semantic_name']: x for x in manifest['icons']}
        self.assertEqual({name: x['solar_icon_name'] for name, x in actual.items()}, selection)
        self.assertEqual(len(actual), len(manifest['icons']))
        sprite = ET.parse(ASSETS / 'citrus-icons.svg').getroot()
        self.assertEqual(len(sprite), len(actual))
        self.assertEqual({x.attrib['id'] for x in sprite}, {'ui-' + name for name in actual})
        for name, entry in actual.items():
            raw = (WEB / entry['local_path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['sha256'], name)
            self.assertEqual(entry['style'], 'BoldDuotone', name)

    def test_literal_references_resolve(self):
        names = set(json.loads((VENDOR / 'selection.json').read_bytes()))
        sources = [*WEB.glob('*.html'), *ASSETS.glob('*.html'), *ASSETS.glob('*.js'),
                   *ASSETS.joinpath('workbench').glob('*.js')]
        for path in sources:
            if path.name in {'xlsx.full.min.js', 'app-icon-names.js'}:
                continue
            text = path.read_text(encoding='utf-8')
            refs = set(re.findall(r'citrus-icons\.svg#ui-([a-z][a-z0-9-]*)|\b(?:AppIcon\.html|uiIcon)\([\'"]([a-z][a-z0-9-]*)[\'"]', text))
            missing = {a or b for a, b in refs} - names
            self.assertFalse(missing, f'{path.relative_to(WEB)} has unresolved icons {missing}')


if __name__ == '__main__':
    unittest.main()
