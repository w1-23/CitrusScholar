from copy import deepcopy

import pytest

from src.agent.identity import ExecutionIdentity
from src.core import figures


def test_recovery_preserves_numbers_attempts_and_link_bytes():
    identity = ExecutionIdentity(1, "session", "request", "job")
    figures.begin_request()
    assert figures.bump_attempts() == 1
    assert figures.register("<svg>first</svg>", download_url="/api/v2/file-one")[0] == 1
    stored = figures.export_recovery(identity)
    figures.begin_request()
    figures.restore_recovery(identity, stored)
    assert figures.bump_attempts() == 2
    assert figures.register("<svg>second</svg>")[0] == 2
    result = figures.expand("[[FIGURE:1]] and [[FIGURE:2]]")
    assert "<svg>first</svg>" in result and "/api/v2/file-one" in result and "<svg>second</svg>" in result
    with pytest.raises(ValueError, match="figure_checkpoint_invalid"):
        figures.restore_recovery(ExecutionIdentity(2, "session", "request", "job"), stored)
    tampered = deepcopy(stored)
    tampered["entries"][0]["figure"]["svg"] = "<svg>tampered</svg>"
    with pytest.raises(ValueError, match="figure_checkpoint_invalid"):
        figures.restore_recovery(identity, tampered)


def test_legacy_placeholder_is_never_reused_after_checkpoint_resume():
    identity = ExecutionIdentity(1, "session", "request", "job")
    figures.begin_request()
    figures.reserve_legacy_indices(["Earlier [[FIGURE:2]]"])
    assert figures.register("<svg>new</svg>")[0] == 3
    stored = figures.export_recovery(identity)
    figures.begin_request()
    figures.restore_recovery(identity, stored)
    assert figures.expand("[[FIGURE:2]]") == "[[FIGURE:2]]"
    assert "<svg>new</svg>" in figures.expand("[[FIGURE:3]]")
    assert figures.register("<svg>later</svg>")[0] == 4
