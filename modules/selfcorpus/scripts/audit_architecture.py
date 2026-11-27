"""Check the relative import dependency graph in the actual remote source tree."""
import ast
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path


def main():
    modules = {}
    for path in Path('corpus').rglob('*.py'):
        pieces = path.with_suffix('').parts
        module = '.'.join(pieces[:-1] if pieces[-1]=='__init__' else pieces)
        modules[module] = path
    edges = {name:set() for name in modules}
    for module, path in modules.items():
        package = module if path.name=='__init__.py' else module.rsplit('.',1)[0]
        for item in ast.walk(ast.parse(path.read_text())):
            if not isinstance(item,ast.ImportFrom) or not item.level:
                continue
            anchor = resolve_name('.'*item.level+(item.module or ''),package)
            edges[module].update(x for x in [anchor, *(anchor+'.'+a.name for a in item.names)] if x in modules and x!=module)
    visited, active = set(), []
    def visit(module):
        if module in active:
            raise ValueError('Import cycle: ' + ' -> '.join([*active, module]))
        if module in visited:
            return
        active.append(module)
        for child in sorted(edges[module]):
            visit(child)
        active.pop()
        visited.add(module)
    for module in edges:
        visit(module)
    fanin = Counter(target for links in edges.values() for target in links)
    largest = sorted(((len(p.read_text().splitlines()),str(p)) for p in modules.values()), reverse=True)[:4]
    print(f'Python modules: {len(modules)}. Internal edges: {sum(map(len,edges.values()))}. Cycles: 0.')
    print('Largest:', largest)
    print('Fan-in:', fanin.most_common(4))
    print('Fan-out:', sorted(((len(children),parent) for parent,children in edges.items()),reverse=True)[:4])


if __name__ == '__main__':
    main()
