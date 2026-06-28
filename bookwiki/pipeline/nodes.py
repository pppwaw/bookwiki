"""Pipeline node facade.

The implementation is split across stage modules (``convert``/``structure``/
``generate``/``concepts``/``integrate``/``check``/``index``) plus shared
infrastructure in ``_shared``. This module re-exports every public node and
helper so existing imports of ``bookwiki.pipeline.nodes`` keep working, and
defines the :data:`NODE_FUNCTIONS` registry consumed by the runner.
"""

from __future__ import annotations

# ``shutil``/``subprocess`` stay imported here so tests can monkeypatch
# ``bookwiki.pipeline.nodes.shutil.which`` / ``...subprocess.run`` (the patched
# attribute lives on the shared module object, so check_node sees it too).
import shutil  # noqa: F401
import subprocess  # noqa: F401

from bookwiki.pipeline._shared import *  # noqa: F401,F403
from bookwiki.pipeline.check import *  # noqa: F401,F403
from bookwiki.pipeline.check import check_node, repair_node
from bookwiki.pipeline.concepts import *  # noqa: F401,F403
from bookwiki.pipeline.concepts import concept_pages_node, reconcile_node
from bookwiki.pipeline.convert import *  # noqa: F401,F403
from bookwiki.pipeline.convert import caption_node, convert_node
from bookwiki.pipeline.generate import *  # noqa: F401,F403
from bookwiki.pipeline.generate import generate_node
from bookwiki.pipeline.index import *  # noqa: F401,F403
from bookwiki.pipeline.index import index_node
from bookwiki.pipeline.integrate import *  # noqa: F401,F403
from bookwiki.pipeline.integrate import integrate_node
from bookwiki.pipeline.structure import *  # noqa: F401,F403
from bookwiki.pipeline.structure import build_skeleton_node, split_node, structure_node

NODE_FUNCTIONS = {
    "convert": convert_node,
    "caption": caption_node,
    "structure": structure_node,
    "split": split_node,
    "build_skeleton": build_skeleton_node,
    "generate": generate_node,
    "reconcile_concepts": reconcile_node,
    "concept_pages": concept_pages_node,
    "integrate": integrate_node,
    "check": check_node,
    "repair": repair_node,
    "index": index_node,
}
