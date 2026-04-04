/-!
# Benchmark: Countability

Theorems related to countability and cardinality.
-/
import Mathlib.Topology.Basic
import Mathlib.Order.Filter.Basic
import Mathlib.Data.Set.Countable
import Mathlib.Data.Real.Basic

open Set

/-- The natural numbers are countable. -/
theorem nat_countable : Set.Countable (Set.univ : Set ℕ) := Set.countable_univ

/-- The integers are countable. -/
theorem int_countable : Set.Countable (Set.univ : Set ℤ) := Set.countable_univ

/-- The rationals are countable. -/
theorem rat_countable : Set.Countable (Set.univ : Set ℚ) := Set.countable_univ

/-- TODO: The reals are uncountable — a meaningful benchmark target.
    This requires Cardinal or measure-theoretic arguments in mathlib.
    Hint: use `Cardinal.mk_real` or Cantor diagonal argument.
-/
-- theorem real_uncountable : ¬ Set.Countable (Set.univ : Set ℝ) := by
--   sorry

/-- TODO: [0,1] is uncountable — the classic benchmark.
    Approach: show injection from ℝ or use `Cardinal.mk_Icc_real`.
-/
-- theorem unit_interval_uncountable : ¬ Set.Countable (Set.Icc (0:ℝ) 1) := by
--   sorry
