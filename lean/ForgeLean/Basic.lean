/-!
# ForgeLean.Basic

Core utilities and helpers for the forge-lean-prover system.
-/
import Mathlib.Tactic

/-- A trivial test theorem used by smoke tests. -/
theorem forge_trivial : 1 + 1 = 2 := by norm_num

/-- Another simple test. -/
theorem forge_and_comm (p q : Prop) : p ∧ q → q ∧ p := by
  intro ⟨hp, hq⟩
  exact ⟨hq, hp⟩

/-- Nat addition is commutative (trivial via mathlib). -/
theorem forge_add_comm (a b : Nat) : a + b = b + a := Nat.add_comm a b
