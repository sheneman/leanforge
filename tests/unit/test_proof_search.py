"""Unit tests for the proof search SearchTree."""
from __future__ import annotations

import pytest

from services.proof_search.main import SearchTree
from services.schemas import BranchState, ProofStatus


@pytest.fixture
def tree() -> SearchTree:
    return SearchTree()


@pytest.fixture
def root_branch() -> BranchState:
    return BranchState(
        branch_id="root-1",
        task_id="task-1",
        tactics=[],
        status=ProofStatus.PENDING,
        score=0.0,
        depth=0,
    )


# ---------------------------------------------------------------------------
# Branch creation
# ---------------------------------------------------------------------------

class TestBranchCreation:
    def test_add_branch(self, tree: SearchTree, root_branch: BranchState):
        result = tree.add(root_branch)
        assert result.branch_id == "root-1"
        assert result.task_id == "task-1"

    def test_get_branch(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        fetched = tree.get("root-1")
        assert fetched is not None
        assert fetched.branch_id == "root-1"

    def test_get_nonexistent_branch(self, tree: SearchTree):
        assert tree.get("does-not-exist") is None

    def test_list_for_task(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        branches = tree.list_for_task("task-1")
        assert len(branches) == 1
        assert branches[0].branch_id == "root-1"

    def test_list_for_empty_task(self, tree: SearchTree):
        branches = tree.list_for_task("no-such-task")
        assert branches == []


# ---------------------------------------------------------------------------
# Scoring and best-first ordering
# ---------------------------------------------------------------------------

class TestScoring:
    def test_update_score(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        updated = tree.update_score("root-1", 0.95)
        assert updated is not None
        assert updated.score == 0.95

    def test_update_score_nonexistent(self, tree: SearchTree):
        assert tree.update_score("missing", 1.0) is None

    def test_best_for_task_highest_score(self, tree: SearchTree):
        b1 = BranchState(branch_id="b1", task_id="t1", score=0.3, status=ProofStatus.PENDING)
        b2 = BranchState(branch_id="b2", task_id="t1", score=0.9, status=ProofStatus.PENDING)
        b3 = BranchState(branch_id="b3", task_id="t1", score=0.5, status=ProofStatus.PENDING)
        tree.add(b1)
        tree.add(b2)
        tree.add(b3)
        best = tree.best_for_task("t1")
        assert best is not None
        assert best.branch_id == "b2"

    def test_best_prefers_verified(self, tree: SearchTree):
        pending = BranchState(
            branch_id="p1", task_id="t1", score=0.99, status=ProofStatus.PENDING,
        )
        verified = BranchState(
            branch_id="v1", task_id="t1", score=0.5, status=ProofStatus.VERIFIED,
        )
        tree.add(pending)
        tree.add(verified)
        best = tree.best_for_task("t1")
        assert best is not None
        assert best.branch_id == "v1"
        assert best.status == ProofStatus.VERIFIED

    def test_best_for_empty_task(self, tree: SearchTree):
        assert tree.best_for_task("empty") is None


# ---------------------------------------------------------------------------
# Child expansion
# ---------------------------------------------------------------------------

class TestExpansion:
    def test_expand_creates_children(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        children = tree.expand("root-1", ["simp", "ring"], [0.8, 0.6])
        assert len(children) == 2
        assert children[0].tactics == ["simp"]
        assert children[1].tactics == ["ring"]
        assert children[0].parent_id == "root-1"
        assert children[1].parent_id == "root-1"
        assert children[0].depth == 1
        assert children[0].score == 0.8
        assert children[1].score == 0.6

    def test_expand_default_scores(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        children = tree.expand("root-1", ["exact?", "omega"])
        assert all(c.score == 0.0 for c in children)

    def test_expand_inherits_parent_tactics(self, tree: SearchTree, root_branch: BranchState):
        root_branch.tactics = ["simp"]
        tree.add(root_branch)
        children = tree.expand("root-1", ["ring"])
        assert children[0].tactics == ["simp", "ring"]

    def test_expand_nonexistent_parent(self, tree: SearchTree):
        children = tree.expand("missing", ["simp"])
        assert children == []

    def test_expand_children_are_indexed(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        children = tree.expand("root-1", ["simp", "ring"])
        all_branches = tree.list_for_task("task-1")
        # root + 2 children
        assert len(all_branches) == 3


# ---------------------------------------------------------------------------
# Budget tracking
# ---------------------------------------------------------------------------

class TestBudgetTracking:
    def test_branch_count_grows(self, tree: SearchTree, root_branch: BranchState):
        tree.add(root_branch)
        assert len(tree.list_for_task("task-1")) == 1

        tree.expand("root-1", ["simp", "ring", "omega"])
        assert len(tree.list_for_task("task-1")) == 4

    def test_multiple_tasks_isolated(self, tree: SearchTree):
        b1 = BranchState(branch_id="a1", task_id="task-a")
        b2 = BranchState(branch_id="b1", task_id="task-b")
        tree.add(b1)
        tree.add(b2)
        assert len(tree.list_for_task("task-a")) == 1
        assert len(tree.list_for_task("task-b")) == 1
