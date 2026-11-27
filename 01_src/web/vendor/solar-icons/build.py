"""Build the app sprite from exact, integrity-pinned official SVG originals."""
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path
from xml.etree import ElementTree as ET

root = Path(__file__).parent
web = root.parent.parent
archive = Path(sys.argv[1])
digest = '9619c702ed637506e0493400e11be96879bb9221af1cf89b6e6ed9f1861bf5bf'
assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
version = '2.7.0'
commit = 'ab241e1bf578dcfe45b0197aee59bd9a08b5fe68'
lock = dict(source_package='@solar-icons/static', source_version=version, source_commit=commit,
            archive_sha256=digest, license='CC BY 4.0', packaging_license='MIT',
            source_url='https://registry.npmjs.org/@solar-icons/static/-/static-2.7.0.tgz',
            design_url='https://github.com/480-Design/Solar-icon-set')
selection = json.loads((root/'selection.json').read_text())
symbols, records = [], []
source_files = [*web.glob('*.html'), *web.joinpath('assets').glob('*.html'),
                *web.joinpath('assets').glob('*.js'), *web.joinpath('assets/workbench').glob('*.js')]
source_text = {f.relative_to(web).as_posix(): f.read_text(encoding='utf-8')
               for f in source_files if f.name not in {'app-icon-names.js','xlsx.full.min.js'}}

def uses(semantic):
    name = re.escape(semantic)
    evidence = re.compile(r'citrus-icons\.svg#ui-' + name + r'(?![a-z0-9-])|' +
                          r'(?<![a-zA-Z0-9_-])[\'"]' + name + r'[\'"]')
    return sorted(path for path, text in source_text.items() if evidence.search(text))

with tarfile.open(archive) as package:
    (root/'PACKAGE-LICENSE').write_bytes(package.extractfile('package/LICENSE').read())
    for semantic, name in sorted(selection.items()):
        assert re.fullmatch(r'[a-z][a-z0-9-]*', semantic) and re.fullmatch(r'[a-z][a-z0-9-]*', name)
        source = f'package/dist/icons/bold-duotone/{name}.svg'
        data = package.extractfile(source).read()
        doc = ET.fromstring(data)
        assert doc.get('viewBox') == '0 0 24 24'
        assert all(node.tag.rsplit('}', 1)[-1] in {'svg','path','circle','ellipse','rect','g','line','polyline','polygon'}
                   and not any(k.lower().startswith('on') or k in {'href','id'} for k in node.attrib)
                   for node in doc.iter()), name
        body = data.decode().split('>', 1)[1].rsplit('</svg>', 1)[0]
        # CurrentColor is inherited from the use element. The explicit secondary
        # color coefficient is opt-in via the root token, including inverse.
        local = root/'source/bold-duotone'/f'{name}.svg'
        local.write_bytes(data)
        symbols.append(f'<symbol id="ui-{semantic}" viewBox="0 0 24 24"><g fill="none" stroke="none">{body}</g></symbol>')
        records.append(dict(semantic_name=semantic,solar_icon_name=name,style='BoldDuotone',source_path=source,
                            local_path=local.relative_to(web).as_posix(),sha256=hashlib.sha256(data).hexdigest(),
                            used_in=uses(semantic),fallback=None))
(web/'assets/citrus-icons.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg">\n'+'\n'.join(symbols)+'\n</svg>\n')
(root/'upstream-lock.json').write_text(json.dumps(lock,indent=2)+'\n')
(web/'assets/icon-manifest.json').write_text(json.dumps({'upstream': lock,'icons':records},indent=2)+'\n')
(web/'assets/app-icon-names.js').write_text('window.AppIconNames=Object.freeze('+json.dumps(sorted(selection)) + ');\n')
