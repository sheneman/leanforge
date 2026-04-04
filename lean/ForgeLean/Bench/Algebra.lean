import Mathlib.Tactic
import Mathlib.GroupTheory.Subgroup.Basic
import Mathlib.RingTheory.Ideal.Basic

/-!
# Benchmark: Algebra

Basic algebraic theorems for benchmarking.
-/

/-- Every group has a unique identity. -/
theorem group_identity_unique {G : Type*} [Group G] (e : G)
    (h : ∀ g : G, e * g = g) : e = 1 := by
  have := h 1
  simp at this
  exact this

/-- Squaring preserves sign for integers. -/
theorem sq_nonneg_int (n : ℤ) : 0 ≤ n ^ 2 := sq_nonneg n

/-- TODO: Bezout's identity for integers. -/
-- theorem bezout_identity (a b : ℤ) : ∃ x y : ℤ, a * x + b * y = Int.gcd a b := by
--   sorry
