import Mathlib.Topology.Basic
import Mathlib.Topology.Instances.Real
import Mathlib.Topology.Compactness.Compact

/-!
# Benchmark: Topology

Topological theorems for benchmarking.
-/

open TopologicalSpace Set

/-- The empty set is open in any topological space. -/
theorem empty_is_open {X : Type*} [TopologicalSpace X] : IsOpen (∅ : Set X) :=
  isOpen_empty

/-- The unit interval [0,1] is compact in ℝ. -/
theorem unit_interval_compact : IsCompact (Set.Icc (0:ℝ) 1) :=
  isCompact_Icc

/-- TODO: Intermediate Value Theorem variant. -/
-- theorem ivt_variant (f : ℝ → ℝ) (hf : Continuous f)
--     (h0 : f 0 < 0) (h1 : 0 < f 1) :
--     ∃ x ∈ Set.Icc (0:ℝ) 1, f x = 0 := by
--   sorry
