"""Parser outputs and Markdown rendering shared by document formats."""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Block:
    text: str
    heading_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    kind: str = "paragraph"
    level: int | None = None

    def markdown(self) -> str:
        if self.kind == "heading":
            return f"{'#' * min(6, self.level or 1)} {self.text}"
        return self.text


@dataclass(frozen=True)
class Parsed:
    markdown: str
    structured: dict
    parser: str
    quality: dict
    metadata: dict
    blocks: tuple[Block, ...] = field(default_factory=tuple)


def build(
    parser: str, blocks: list[Block], metadata: dict | None = None,
    *, warnings: list[str] | None = None, markdown: str | None = None,
) -> Parsed:
    notes = list(dict.fromkeys(warnings or []))
    if not any(block.text.strip() and block.kind != "heading" for block in blocks):
        notes.append("no_extractable_body_text")
    metadata = metadata or {}
    prose = [block for block in blocks if block.kind != "heading"]
    quality = {"status": "partial" if notes else "complete", "warnings": notes,
               "extracted_fields": {key: bool(metadata.get(key)) for key in ("title", "authors", "doi")},
               "text_characters": sum(len(block.text) for block in prose),
               "sections": sum(block.kind == "heading" for block in blocks),
               "tables": sum(block.kind == "table" for block in blocks),
               "references": sum(block.kind in {"ref", "bibl", "biblStruct", "reference"} for block in blocks),
               "formulas": sum(block.kind in {"formula", "disp-formula"} for block in blocks),
               "blocks_with_page_mapping": sum(block.page_start is not None for block in prose),
               "replacement_characters": sum(block.text.count("\ufffd") for block in prose)}
    return Parsed(
        markdown=markdown if markdown is not None else "\n\n".join(b.markdown() for b in blocks),
        structured={"blocks": [dict(asdict(b), heading_path=list(b.heading_path)) for b in blocks]},
        parser=parser,
        quality=quality,
        metadata=metadata,
        blocks=tuple(blocks),
    )


def heading_path(path: list[tuple[int, str]], level: int, text: str) -> tuple[str, ...]:
    while path and path[-1][0] >= level:
        path.pop()
    path.append((level, text))
    return tuple(value for _, value in path)
