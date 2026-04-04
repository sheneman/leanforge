import Mathlib.Tactic

/-!
# Human-Written Proof Examples

Reference proofs written by humans for comparison and retrieval training.
These demonstrate idiomatic Lean 4 + Mathlib proof style.
-/

/-- De Morgan's law for propositions. -/
theorem de_morgan_not_and (p q : Prop) : ¬(p ∧ q) ↔ ¬p ∨ ¬q := by
  constructor
  · intro h
    by_contra h'
    push_neg at h'
    exact h ⟨h'.1, h'.2⟩
  · intro h ⟨hp, hq⟩
    rcases h with hn | hn
    · exact hn hp
    · exact hn hq

/-- Sum of first n naturals. -/
theorem sum_range (n : ℕ) : 2 * (Finset.range n).sum id = n * (n - 1) := by
  induction n with
  | zero => simp
  | succ n ih =>
    simp [Finset.sum_range_succ]
    omega

/-- A list reversed twice is itself. -/
theorem list_reverse_reverse {α : Type*} (l : List α) : l.reverse.reverse = l :=
  List.reverse_reverse l

/-- Pigeonhole: if n+1 items in n boxes, some box has ≥ 2. -/
-- This is a good target for proof search but requires careful mathlib navigation.
-- theorem pigeonhole ... := by sorry
